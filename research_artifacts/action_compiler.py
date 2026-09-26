"""Deterministic compilation from checkpoint state to one bounded Skill action."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .resources import resource_path
import re
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator

from .context_builder import CONTROLLER_ONLY_STATES, ResearchContextBuilder
from .orchestrator import CheckpointStore
from .runtime_types import RuntimeValidationError
from .workspace import ArtifactWorkspace


ACTION_VERSION = "research-action/v0.1"


class ActionCompiler:
    """Compile policy; never execute an agent or choose a state transition."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.workspace = ArtifactWorkspace(self.root)
        self.checkpoints = CheckpointStore(self.root)
        self.contexts = ResearchContextBuilder(self.root)
        self.skills_root = resource_path(self.root, "integrations/deepseek-harness/skills")
        action_schema = json.loads(
            (resource_path(self.root, "schemas/runtime/v0.1/action.schema.json")).read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(action_schema)
        self.action_validator = Draft202012Validator(action_schema)

    def preview(self, project_id: str) -> dict[str, Any]:
        checkpoint = self.checkpoints.load(project_id)
        state = str(checkpoint["state"])
        if state in CONTROLLER_ONLY_STATES:
            return {
                "action_version": ACTION_VERSION,
                "project_id": project_id,
                "state": state,
                "controller_only": True,
                "track": self._selected_track(checkpoint),
                "skill": None,
                "preset": None,
                "target_output_id": None,
                "target_ref": None,
            }
        output_dependencies = self.contexts._planned_output_dependencies(  # noqa: SLF001
            checkpoint, self.workspace.index()
        )
        track = self._selected_track(checkpoint)
        try:
            if state == "EXECUTION_TRACKS":
                role = None
                failures: list[RuntimeValidationError] = []
                for candidate_track in self._active_tracks(checkpoint):
                    try:
                        role = (*self.contexts._role_for(  # noqa: SLF001
                            state, candidate_track, checkpoint, output_dependencies
                        ), candidate_track)
                        break
                    except RuntimeValidationError as exc:
                        if not str(exc).startswith("no pending Skill matches"):
                            raise
                        failures.append(exc)
                if role is None:
                    if failures:
                        raise failures[0]
                    raise RuntimeValidationError("execution checkpoint has no active track")
                skill, preset, output_id, track = role
            else:
                skill, preset, output_id = self.contexts._role_for(  # noqa: SLF001
                    state, track, checkpoint, output_dependencies
                )
        except RuntimeValidationError as exc:
            # A Skill completion and its deterministic TRACK_ADVANCED event
            # are separate commits.  During that narrow checkpoint window,
            # status inspection must remain available even though there is no
            # further agent action to compile.
            if state == "EXECUTION_TRACKS" and str(exc).startswith("no pending Skill matches"):
                return {
                    "action_version": ACTION_VERSION,
                    "project_id": project_id,
                    "state": state,
                    "controller_only": True,
                    "controller_transition": "TRACK_ADVANCED",
                    "track": track,
                    "skill": None,
                    "preset": None,
                    "target_output_id": None,
                    "target_ref": None,
                }
            raise
        target_ref = None
        if output_id is not None:
            progress = checkpoint.get("skill_progress", {}).get(output_id, {})
            stored = progress.get("artifact_ref")
            if not isinstance(stored, Mapping):
                raise RuntimeValidationError(
                    f"planned output {output_id} has no materialized artifact"
                )
            record = self.workspace.get(str(stored["id"]))
            target_ref = {
                "id": record.id,
                "kind": record.kind,
                "revision": record.data["revision"],
            }
        return {
            "action_version": ACTION_VERSION,
            "project_id": project_id,
            "state": state,
            "controller_only": False,
            "track": track,
            "skill": skill,
            "preset": preset,
            "target_output_id": output_id,
            "target_ref": target_ref,
        }

    def compile(
        self,
        *,
        project_id: str,
        action_id: str,
        input_git_commit: str,
    ) -> dict[str, Any]:
        preview = self.preview(project_id)
        if preview["controller_only"]:
            raise RuntimeValidationError(
                f"{preview['state']} is controller-only and cannot compile an agent action"
            )
        target_ref = preview["target_ref"]
        target_output_id = preview["target_output_id"]
        build_args: dict[str, Any] = {
            "project_id": project_id,
            "action_id": action_id,
            "input_git_commit": input_git_commit,
        }
        if preview["track"] is not None:
            build_args["track"] = preview["track"]
        if target_ref is not None:
            build_args["target_refs"] = [target_ref]
            build_args["allowed_artifact_ids"] = [target_ref["id"]]
        if target_output_id is not None:
            build_args["target_output_ids"] = [target_output_id]
        # A pending action owns its original frozen bundle. Operational commits
        # such as budget approval may advance Git HEAD but must not silently
        # rebuild the scientific input snapshot or replay completed work.
        context_path = (
            self.contexts.artifacts.action_dir(project_id, action_id)
            / "context.json"
        )
        if context_path.is_file():
            bundle = json.loads(context_path.read_text(encoding="utf-8"))
            if (
                bundle.get("project_id") != project_id
                or bundle.get("action_id") != action_id
                or bundle.get("input_git_commit") != input_git_commit.lower()
            ):
                raise RuntimeValidationError(
                    "persisted action context does not match pending action"
                )
        else:
            bundle = self.contexts.build(**build_args)
        contract = self._skill_contract(str(preview["skill"]))
        action = {
            **preview,
            "action_id": action_id,
            "run_id": bundle["run_id"],
            "input_git_commit": input_git_commit.lower(),
            "required_input_refs": [
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    "revision": item["revision"],
                }
                for item in bundle["artifacts"]
            ],
            "allowed_outputs": copy.deepcopy(bundle["allowed_outputs"]),
            "allowed_tools": self._allowed_tools(str(preview["skill"]), contract),
            "field_permissions": copy.deepcopy(
                bundle["allowed_outputs"].get("rules", [])
            ),
            "working_directory_policy": self._working_directory_policy(
                str(preview["preset"])
            ),
            "completion_contract": {
                "conditions": list(contract["completion_conditions"]),
                "failure_classes": list(contract["failure_classes"]),
                "forbidden_behaviors": list(contract["forbidden_behaviors"]),
            },
            "model_routing": self._model_routing(
                str(preview["skill"]), str(preview["state"]), bundle
            ),
        }
        errors = sorted(
            self.action_validator.iter_errors(action),
            key=lambda error: (list(error.absolute_path), error.message),
        )
        if errors:
            raise RuntimeValidationError(
                "compiled action violates its runtime contract",
                details=[
                    {
                        "path": ".".join(str(part) for part in error.absolute_path)
                        or "$",
                        "message": error.message,
                    }
                    for error in errors
                ],
            )
        return {"action": action, "bundle": bundle}

    @staticmethod
    def _model_routing(
        skill: str, state: str, bundle: Mapping[str, Any]
    ) -> dict[str, str]:
        if state.startswith("PROPOSAL_"):
            return {
                "scientific_risk": "HIGH",
                "difficulty": "HIGH",
                "uncertainty": "HIGH",
                "reason": "PROPOSAL_REVIEW is quality-first; all actions route to frontier",
            }
        critical = {
            "theory-development", "theory-verification",
            "semantic-alignment-review", "experiment-verification",
            "consistency-review", "final-review",
        }
        high_uncertainty = (
            state == "DISCOVERY_FEASIBILITY_GATE"
            or skill in {"literature-novelty", "theory-development"}
        )
        feasibility_recovery = state in {"DISCOVERY_QUESTION", "DISCOVERY_LITERATURE"} and any(
            item.get("kind") == "Decision"
            and item.get("content", {}).get("title") == "Controller decision for GATE_RECORDED"
            and "CREATE_RETHINK_DECISION" in str(item.get("content", {}).get("context", ""))
            for item in bundle.get("artifacts", [])
        )
        return {
            "scientific_risk": "HIGH" if skill in critical else "MEDIUM",
            "difficulty": "HIGH" if skill in critical else "MEDIUM",
            "uncertainty": "HIGH" if high_uncertainty else "MEDIUM",
            "reason": (
                "feasibility re-evaluation after a recorded RETHINK_QUESTION; "
                "the corrective framing and evidence action belongs to gate verification"
                if feasibility_recovery else
                "deterministic Skill/state risk policy; budget may stop execution "
                "but may not lower this quality requirement"
            ),
        }

    def _skill_contract(self, skill_id: str) -> dict[str, Any]:
        path = self.skills_root / skill_id / "skill.yaml"
        if not path.is_file():
            raise RuntimeValidationError(f"Skill contract not found: {skill_id}")
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("id") != skill_id:
            raise RuntimeValidationError(f"invalid Skill contract: {skill_id}")
        return value

    @staticmethod
    def _allowed_tools(skill_id: str, contract: Mapping[str, Any]) -> list[str]:
        tools = {str(item) for item in contract["tools"]}
        aliases = {
            "artifact-read": "research_artifact_read",
            "artifact-query": "research_artifact_query",
            "artifact-resolve": "research_artifact_resolve",
            "artifact-validate": "research_artifact_validate",
            "artifact-propose": "research_artifact_propose",
            "action-submit": "research_action_submit",
        }
        allowed = {
            aliases.get(item, item)
            for item in tools
            if re.fullmatch(r"[a-z][a-z0-9_-]*", item)
        }
        # Every executable scientific handler must finish with the structured
        # submission tool, even when its contract uses a prose tool alias.
        allowed.add("research_action_submit")
        if skill_id == "lean-verification":
            allowed.add("research_lean_verify")
        if skill_id == "experiment-execution":
            allowed.add("research_experiment_run")
        return sorted(allowed)

    @staticmethod
    def _selected_track(checkpoint: Mapping[str, Any]) -> str | None:
        if checkpoint.get("state") != "EXECUTION_TRACKS":
            return None
        branches = checkpoint.get("branch_states", {})
        theory = branches.get("theory")
        if theory not in {"NOT_SELECTED", "COMPLETED", "WAIVED"}:
            return "theory"
        experiment = branches.get("experiment")
        if experiment not in {"NOT_SELECTED", "COMPLETED", "WAIVED"}:
            return "experiment"
        return None

    @staticmethod
    def _active_tracks(checkpoint: Mapping[str, Any]) -> list[str]:
        branches = checkpoint.get("branch_states", {})
        return [
            track
            for track in ("theory", "experiment")
            if branches.get(track) not in {"NOT_SELECTED", "COMPLETED", "WAIVED"}
        ]

    @staticmethod
    def _working_directory_policy(preset: str) -> str:
        if preset == "research-writer":
            return "PROJECT_PAPER"
        if preset in {
            "research-theory", "research-formal", "research-formal-reviewer",
            "research-experiment", "research-experiment-reviewer",
        }:
            return "ACTION_SCRATCH"
        return "READ_ONLY"
