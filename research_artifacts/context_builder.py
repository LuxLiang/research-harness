"""Deterministic bounded context construction for research state handlers."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .resources import resource_path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .artifact_service import ArtifactService
from .orchestrator import CheckpointStore
from .runtime_types import (
    CONTEXT_VERSION,
    ContextLimitError,
    RuntimeValidationError,
    atomic_write_json,
    canonical_json_bytes,
    sha256_json,
)
from .workspace import ArtifactRecord, ArtifactWorkspace, iter_artifact_refs


ROLE_POLICIES: dict[str, tuple[str, str]] = {
    "DISCOVERY_QUESTION": ("question-framing", "research-producer"),
    "DISCOVERY_LITERATURE": ("literature-novelty", "research-producer"),
    "DISCOVERY_FEASIBILITY_GATE": ("literature-novelty", "research-reviewer"),
    "PLANNING_DRAFT": ("research-planning", "research-producer"),
    "SYNTHESIS_BUILD": ("research-synthesis", "research-producer"),
    "SYNTHESIS_COMPLETION_GATE": ("completion-review", "research-reviewer"),
    "FOLLOWUP_TARGETED": ("targeted-followup", "research-producer"),
    "FOLLOWUP_CLAIM_RETHINK": ("theory-development", "research-theory"),
    "CONSISTENCY_GATE": ("consistency-review", "research-reviewer"),
    "WRITING_DRAFT": ("scientific-writing", "research-writer"),
    "WRITING_FINAL_REVIEW": ("final-review", "research-reviewer"),
    "WRITING_TARGETED_REVISION": ("scientific-writing", "research-writer"),
    "PROPOSAL_STRUCTURING": ("question-framing", "research-producer"),
    "PROPOSAL_NOVELTY_REVIEW": ("literature-novelty", "research-reviewer"),
    "PROPOSAL_PLAN_RECONSTRUCTION": ("research-planning", "research-producer"),
    "PROPOSAL_CORRECTNESS_REVIEW": ("proposal-correctness-review", "research-reviewer"),
    "PROPOSAL_REVISION": ("proposal-revision", "research-producer"),
}

SKILL_PRESETS = {
    "theory-development": "research-theory",
    "theory-verification": "research-reviewer",
    "lean-formalization": "research-formal",
    "lean-verification": "research-formal-reviewer",
    "semantic-alignment-review": "research-reviewer",
    "experiment-design": "research-producer",
    "experiment-execution": "research-experiment",
    "experiment-verification": "research-experiment-reviewer",
}

COMMON_REVISION_FIELDS = ["revision", "updated_at", "provenance.updated_by"]
SKILL_OUTPUT_RULES: dict[str, list[dict[str, Any]]] = {
    "question-framing": [
        {"kind": "ResearchQuestion", "operations": ["CREATE", "REVISE"], "fields": ["*"]},
        {
            "kind": "ResearchProposal",
            "operations": ["REVISE"],
            "fields": [
                *COMMON_REVISION_FIELDS, "status", "question_refs", "source_map",
            ],
        },
    ],
    "literature-novelty": [
        {"kind": "LiteratureEvidence", "operations": ["CREATE", "REVISE"], "fields": ["*"]},
        {"kind": "Review", "operations": ["CREATE"], "fields": ["*"]},
    ],
    "research-planning": [
        {"kind": "ResearchPlan", "operations": ["CREATE", "REVISE"], "fields": ["*"]},
    ],
    "theory-development": [{"kind": "ScientificClaim", "operations": ["CREATE", "REVISE"], "fields": ["*"]}],
    "theory-verification": [{"kind": "Review", "operations": ["CREATE"], "fields": ["*"]}],
    "lean-formalization": [{
        "kind": "ScientificClaim", "operations": ["REVISE"],
        "fields": [*COMMON_REVISION_FIELDS, "verification.protocol", "verification.resource_refs"],
    }],
    "lean-verification": [{"kind": "Review", "operations": ["CREATE"], "fields": ["*"]}],
    "semantic-alignment-review": [{"kind": "Review", "operations": ["CREATE"], "fields": ["*"]}],
    "experiment-design": [{"kind": "Experiment", "operations": ["CREATE", "REVISE"], "fields": ["*"]}],
    "experiment-execution": [{
        "kind": "Experiment", "operations": ["REVISE"],
        "fields": [*COMMON_REVISION_FIELDS, "status", "runs"],
    }],
    "experiment-verification": [{"kind": "Review", "operations": ["CREATE"], "fields": ["*"]}],
    "research-synthesis": [
        {"kind": "ScientificClaim", "operations": ["REVISE"], "fields": [*COMMON_REVISION_FIELDS, "status", "evidence", "verification.conclusion"]},
        {"kind": "Review", "operations": ["CREATE"], "fields": ["*"]},
        {"kind": "Decision", "operations": ["CREATE"], "fields": ["*"]},
    ],
    "completion-review": [{"kind": "Review", "operations": ["CREATE", "REVISE"], "fields": ["*"]}],
    "consistency-review": [{"kind": "Review", "operations": ["CREATE"], "fields": ["*"]}],
    "targeted-followup": [{"kind": "ResearchPlan", "operations": ["CREATE", "REVISE"], "fields": ["*"]}],
    "scientific-writing": [],
    "final-review": [{"kind": "Review", "operations": ["CREATE"], "fields": ["*"]}],
    "proposal-correctness-review": [{"kind": "Review", "operations": ["CREATE"], "fields": ["*"]}],
    "proposal-revision": [{"kind": "ResearchProposal", "operations": ["REVISE"], "fields": ["*"]}],
}

CONTROLLER_ONLY_STATES = {
    "PLANNING_ROUTING",
    "EXECUTION_MATERIALIZE",
    "EXECUTION_JOIN",
    "WRITING_FINAL_APPROVAL",
    "PROPOSAL_REVIEW_GATE",
    "PROPOSAL_WAITING_DECISIONS",
    "PROPOSAL_FINAL_APPROVAL",
    "DONE",
    "BLOCKED",
    "PAUSED_BY_USER",
    "CANCELLED_BY_USER",
}

STATE_KIND_POLICY: dict[str, set[str]] = {
    "DISCOVERY_QUESTION": {"Project", "ResearchQuestion", "Decision"},
    "DISCOVERY_LITERATURE": {
        "Project", "ResearchQuestion", "LiteratureEvidence", "ScientificClaim", "Review", "Decision",
    },
    "DISCOVERY_FEASIBILITY_GATE": {
        "Project", "ResearchQuestion", "LiteratureEvidence", "Review", "Decision",
    },
    "PLANNING_DRAFT": {
        "Project", "ResearchQuestion", "LiteratureEvidence", "ResearchPlan", "Review", "Decision",
    },
    "PLANNING_APPROVAL": {
        "Project", "ResearchQuestion", "ResearchPlan", "Review", "Decision",
    },
    "EXECUTION_MATERIALIZE": {"Project", "ResearchPlan"},
    "EXECUTION_TRACKS": {
        # The active plan is added explicitly from the checkpoint below.  Old
        # plans and orchestration Decisions are not scientific inputs to an
        # execution Skill; retaining them makes long-running projects grow the
        # ContextBundle without bound and can crowd out target evidence.
        "Project", "LiteratureEvidence", "ScientificClaim", "Experiment", "Review",
    },
    "EXECUTION_JOIN": {
        "Project", "ResearchPlan", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "SYNTHESIS_BUILD": {
        "Project", "ResearchPlan", "LiteratureEvidence", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "SYNTHESIS_COMPLETION_GATE": {
        "Project", "ResearchPlan", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "FOLLOWUP_TARGETED": {
        "Project", "ResearchQuestion", "ResearchPlan", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "FOLLOWUP_CLAIM_RETHINK": {
        "Project", "ResearchPlan", "LiteratureEvidence", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "CONSISTENCY_GATE": {
        "Project", "ResearchPlan", "LiteratureEvidence", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "WRITING_DRAFT": {
        "Project", "LiteratureEvidence", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "WRITING_FINAL_REVIEW": {
        "Project", "LiteratureEvidence", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "WRITING_TARGETED_REVISION": {
        "Project", "LiteratureEvidence", "ScientificClaim", "Experiment", "Review", "Decision",
    },
    "PROPOSAL_STRUCTURING": {"Project", "ResearchProposal", "ResearchQuestion", "Decision"},
    "PROPOSAL_NOVELTY_REVIEW": {"Project", "ResearchProposal", "ResearchQuestion", "LiteratureEvidence", "Review", "Decision"},
    "PROPOSAL_PLAN_RECONSTRUCTION": {"Project", "ResearchProposal", "ResearchQuestion", "LiteratureEvidence", "ResearchPlan", "Review", "Decision"},
    "PROPOSAL_CORRECTNESS_REVIEW": {"Project", "ResearchProposal", "ResearchQuestion", "LiteratureEvidence", "ResearchPlan", "Review", "Decision"},
    "PROPOSAL_REVISION": {"Project", "ResearchProposal", "ResearchQuestion", "LiteratureEvidence", "ResearchPlan", "Review", "Decision"},
}

STATE_OUTPUT_KINDS: dict[str, set[str]] = {
    "DISCOVERY_QUESTION": {"ResearchQuestion"},
    "DISCOVERY_LITERATURE": {"LiteratureEvidence", "Review"},
    "DISCOVERY_FEASIBILITY_GATE": {"Review", "Decision"},
    "PLANNING_DRAFT": {"ResearchPlan"},
    "EXECUTION_MATERIALIZE": set(),
    "EXECUTION_TRACKS": {"ScientificClaim", "Experiment", "Review"},
    "SYNTHESIS_BUILD": {"ScientificClaim", "Experiment", "Review", "Decision"},
    "SYNTHESIS_COMPLETION_GATE": {"Review"},
    "FOLLOWUP_TARGETED": {"ResearchPlan"},
    "FOLLOWUP_CLAIM_RETHINK": {"ScientificClaim"},
    "CONSISTENCY_GATE": {"Review"},
    "WRITING_DRAFT": set(),
    "WRITING_FINAL_REVIEW": {"Review"},
    "WRITING_TARGETED_REVISION": set(),
    "PROPOSAL_STRUCTURING": {"ResearchQuestion", "ResearchProposal"},
    "PROPOSAL_NOVELTY_REVIEW": {"LiteratureEvidence", "Review"},
    "PROPOSAL_PLAN_RECONSTRUCTION": {"ResearchPlan"},
    "PROPOSAL_CORRECTNESS_REVIEW": {"Review"},
    "PROPOSAL_REVISION": {"ResearchProposal"},
}


class ResearchContextBuilder:
    """Build and persist one immutable, revision-pinned handler context."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_artifacts: int = 64,
        max_serialized_bytes: int = 512 * 1024,
    ):
        self.root = Path(root).resolve()
        self.workspace = ArtifactWorkspace(self.root)
        self.artifacts = ArtifactService(self.root)
        self.checkpoints = CheckpointStore(self.root)
        self.max_artifacts = max_artifacts
        self.max_serialized_bytes = max_serialized_bytes
        schema_path = resource_path(self.root, "schemas/runtime/v0.1/context.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.context_validator = Draft202012Validator(
            schema, format_checker=FormatChecker()
        )

    def build(
        self,
        *,
        project_id: str,
        action_id: str,
        input_git_commit: str,
        state: str | None = None,
        target_refs: Iterable[Mapping[str, Any]] = (),
        target_output_ids: Iterable[str] = (),
        allowed_artifact_ids: Iterable[str] = (),
        allowed_create_ids: Iterable[str] = (),
        track: str | None = None,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", input_git_commit):
            raise RuntimeValidationError("input_git_commit must be a 40-character Git commit")
        checkpoint = self.checkpoints.load(project_id)
        chosen_state = state or str(checkpoint["state"])
        if chosen_state != checkpoint["state"]:
            raise RuntimeValidationError(
                f"requested state {chosen_state} differs from checkpoint {checkpoint['state']}"
            )
        if chosen_state in CONTROLLER_ONLY_STATES:
            raise RuntimeValidationError(f"{chosen_state} is not an agent handler state")

        index = self.workspace.index()
        output_dependencies = self._planned_output_dependencies(checkpoint, index)
        role, preset, progress_output_id = self._role_for(
            chosen_state, track, checkpoint, output_dependencies
        )
        requested_output_ids = sorted(set(target_output_ids))
        if progress_output_id is not None:
            if requested_output_ids and requested_output_ids != [progress_output_id]:
                raise RuntimeValidationError(
                    "target_output_ids do not match the next deterministic Skill"
                )
            requested_output_ids = [progress_output_id]
        selected = self._select_records(
            project_id=project_id,
            state=chosen_state,
            index=index,
            checkpoint=checkpoint,
            target_refs=target_refs,
        )
        if chosen_state == "SYNTHESIS_BUILD":
            selected = self._synthesis_records(project_id, checkpoint, index)
        if chosen_state == "SYNTHESIS_COMPLETION_GATE":
            selected = self._completion_records(project_id, checkpoint, index)
        if chosen_state == "FOLLOWUP_TARGETED":
            selected = self._followup_records(project_id, checkpoint, index)
        if chosen_state == "EXECUTION_TRACKS" and progress_output_id is not None:
            selected = self._execution_records(
                project_id, checkpoint, index, progress_output_id,
                output_dependencies,
            )
        if len(selected) > self.max_artifacts:
            raise ContextLimitError(
                f"context selects {len(selected)} artifacts, limit is {self.max_artifacts}",
                details={"selected_ids": sorted(selected)},
            )

        records = [index[artifact_id] for artifact_id in sorted(selected)]
        artifact_values = [self._snapshot(record) for record in records]
        dependencies = self._dependencies(records)
        allowed_ids = sorted(set(allowed_artifact_ids))
        if not allowed_ids:
            allowed_ids = self._default_allowed_ids(
                chosen_state, role, checkpoint, records
            )
        create_ids = sorted(set(allowed_create_ids))
        if not create_ids:
            create_ids = self._default_create_ids(
                chosen_state, role, project_id, action_id, checkpoint, index
            )
        if role == "lean-verification" and create_ids:
            create_ids = sorted({*create_ids, f"{create_ids[0]}-axiom-audit"})
        if chosen_state == "DISCOVERY_QUESTION":
            rethink_gates = [
                gate for gate in checkpoint.get("gate_results", [])
                if gate.get("gate_type") == "FEASIBILITY"
                and gate.get("verdict") == "RETHINK_QUESTION"
            ]
            if rethink_gates:
                latest_targets = [
                    str(ref.get("id"))
                    for ref in rethink_gates[-1].get("target_refs", [])
                    if ref.get("kind") == "ResearchQuestion"
                    and str(ref.get("id")) in index
                ]
                if len(latest_targets) != 1:
                    raise RuntimeValidationError(
                        "feasibility reframe requires exactly one canonical ResearchQuestion target"
                    )
                allowed_ids = latest_targets
                create_ids = [
                    artifact_id for artifact_id in create_ids
                    if not artifact_id.startswith("rq-")
                ]
        body: dict[str, Any] = {
            "bundle_version": CONTEXT_VERSION,
            "project_id": project_id,
            "run_id": checkpoint["run_id"],
            "action_id": action_id,
            "state": chosen_state,
            "role": role,
            "preset": preset,
            "input_git_commit": input_git_commit.lower(),
            "artifacts": artifact_values,
            "direct_dependencies": dependencies,
            "target_output_ids": requested_output_ids,
            "allowed_outputs": {
                "artifact_kinds": sorted(
                    {rule["kind"] for rule in SKILL_OUTPUT_RULES.get(role, [])}
                ),
                "artifact_ids": allowed_ids,
                "create_ids": create_ids,
                "rules": copy.deepcopy(SKILL_OUTPUT_RULES.get(role, [])),
            },
            "gate_contract": self._gate_contract(chosen_state),
            "limits": {
                "max_artifacts": self.max_artifacts,
                "max_serialized_bytes": self.max_serialized_bytes,
            },
        }
        size_without_hash = len(canonical_json_bytes(body))
        if size_without_hash > self.max_serialized_bytes:
            raise ContextLimitError(
                f"context is {size_without_hash} bytes, limit is {self.max_serialized_bytes}",
                details={"selected_ids": [record.id for record in records]},
            )
        body["bundle_sha256"] = sha256_json(body)
        schema_issues = sorted(
            self.context_validator.iter_errors(body),
            key=lambda error: (list(error.absolute_path), error.message),
        )
        if schema_issues:
            raise RuntimeValidationError(
                "generated context bundle is invalid",
                details=[
                    {
                        "path": ".".join(str(part) for part in error.absolute_path) or "$",
                        "message": error.message,
                    }
                    for error in schema_issues
                ],
            )
        path = self.artifacts.action_dir(project_id, action_id) / "context.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != body:
                raise RuntimeValidationError(
                    "action context already exists with different inputs"
                )
            return existing
        atomic_write_json(path, body)
        return body

    def _select_records(
        self,
        *,
        project_id: str,
        state: str,
        index: Mapping[str, ArtifactRecord],
        checkpoint: Mapping[str, Any],
        target_refs: Iterable[Mapping[str, Any]],
    ) -> set[str]:
        kinds = STATE_KIND_POLICY.get(state, {"Project"})
        members = {
            artifact_id
            for artifact_id, record in index.items()
            if (record.id if record.kind == "Project" else record.data.get("project_id"))
            == project_id
            and record.kind in kinds
            and self._state_filter(state, record)
        }
        members.add(project_id)
        active_plan = checkpoint.get("active_plan")
        if isinstance(active_plan, dict):
            members.add(str(active_plan["id"]))
        frontier: list[str] = []
        for ref in target_refs:
            artifact_id = str(ref.get("id", ""))
            record = index.get(artifact_id)
            if record is None:
                raise RuntimeValidationError(f"target artifact does not exist: {artifact_id}")
            if ref.get("kind") != record.kind:
                raise RuntimeValidationError(f"target kind mismatch for {artifact_id}")
            if ref.get("revision") not in {None, record.data.get("revision")}:
                raise RuntimeValidationError(f"target revision mismatch for {artifact_id}")
            members.add(artifact_id)
            frontier.append(artifact_id)

        # Explicit target dependencies are expanded two levels. General state
        # selection remains kind/status bounded and never walks the whole graph.
        for _ in range(2):
            next_frontier: list[str] = []
            for artifact_id in frontier:
                for ref in iter_artifact_refs(index[artifact_id].data):
                    target = index.get(ref["id"])
                    if target is None:
                        continue
                    target_project = (
                        target.id if target.kind == "Project" else target.data.get("project_id")
                    )
                    if target_project == project_id and target.id not in members:
                        members.add(target.id)
                        next_frontier.append(target.id)
            frontier = next_frontier
        return members

    @staticmethod
    def _synthesis_records(
        project_id: str,
        checkpoint: Mapping[str, Any],
        index: Mapping[str, ArtifactRecord],
    ) -> set[str]:
        """Select current planned outputs and their evidence for synthesis.

        Synthesis should not ingest every superseded plan, output, Decision,
        and rejected Review accumulated during a long run.  The active Plan's
        materialized outputs are the authoritative frontier.  Their bounded
        dependency closure plus the latest Review revision for each frontier
        artifact is sufficient and deterministic.
        """
        active_plan = checkpoint.get("active_plan")
        if not isinstance(active_plan, Mapping):
            raise RuntimeValidationError("synthesis requires an active Plan")
        plan_id = str(active_plan.get("id", ""))
        plan_record = index.get(plan_id)
        if plan_record is None or plan_record.kind != "ResearchPlan":
            raise RuntimeValidationError("active synthesis Plan does not exist")

        selected = {project_id, plan_id}
        frontier: list[str] = []
        for package in plan_record.data.get("work_packages", []):
            for mapping in package.get("materialized_outputs", []):
                artifact_id = str(mapping.get("artifact_ref", {}).get("id", ""))
                if artifact_id in index:
                    selected.add(artifact_id)
                    frontier.append(artifact_id)

        # Preserve assessed literature context, then expand explicit output
        # dependencies without walking unrelated historical artifacts.
        selected.update(
            artifact_id for artifact_id, record in index.items()
            if record.kind == "LiteratureEvidence"
            and record.data.get("project_id") == project_id
            and ResearchContextBuilder._state_filter("SYNTHESIS_BUILD", record)
        )
        relevant_targets = set(frontier)
        for _ in range(2):
            next_frontier: list[str] = []
            for artifact_id in frontier:
                for ref in iter_artifact_refs(index[artifact_id].data):
                    dependency_id = str(ref["id"])
                    dependency = index.get(dependency_id)
                    if dependency is None:
                        continue
                    dependency_project = (
                        dependency.id if dependency.kind == "Project"
                        else dependency.data.get("project_id")
                    )
                    if dependency_project == project_id and dependency_id not in selected:
                        selected.add(dependency_id)
                        next_frontier.append(dependency_id)
                        relevant_targets.add(dependency_id)
            frontier = next_frontier

        reviews_by_target: dict[str, list[ArtifactRecord]] = {}
        for record in index.values():
            if record.kind != "Review" or record.data.get("project_id") != project_id:
                continue
            target_id = str(
                record.data.get("target", {}).get("artifact_ref", {}).get("id", "")
            )
            if target_id in relevant_targets:
                reviews_by_target.setdefault(target_id, []).append(record)
        for reviews in reviews_by_target.values():
            latest_target_revision = max(
                int(review.data["target"]["artifact_ref"].get("revision", -1))
                for review in reviews
            )
            selected.update(
                review.id for review in reviews
                if int(review.data["target"]["artifact_ref"].get("revision", -1))
                == latest_target_revision
            )
        return selected

    @staticmethod
    def _completion_records(
        project_id: str,
        checkpoint: Mapping[str, Any],
        index: Mapping[str, ArtifactRecord],
    ) -> set[str]:
        """Select the current synthesis frontier for completion review.

        A long execution can accumulate many superseded Reviews and lifecycle
        Decisions.  The completion reviewer needs the active Plan, its current
        materialized outputs, and the latest synthesis Review together with
        the exact artifacts cited by that Review.  It does not need every
        historical artifact in the project.
        """
        active_plan = checkpoint.get("active_plan")
        if not isinstance(active_plan, Mapping):
            raise RuntimeValidationError("completion review requires an active Plan")
        plan_id = str(active_plan.get("id", ""))
        plan_record = index.get(plan_id)
        if plan_record is None or plan_record.kind != "ResearchPlan":
            raise RuntimeValidationError("active completion Plan does not exist")

        selected = {project_id, plan_id}
        for package in plan_record.data.get("work_packages", []):
            for mapping in package.get("materialized_outputs", []):
                artifact_id = str(mapping.get("artifact_ref", {}).get("id", ""))
                if artifact_id in index:
                    selected.add(artifact_id)

        synthesis_reviews = [
            record for record in index.values()
            if record.kind == "Review"
            and record.data.get("project_id") == project_id
            and record.data.get("assessment", {}).get("scheme") == "COMPLETION"
            and (
                "synthesis" in record.data.get("tags", [])
                or record.data.get("reviewer", {}).get("actor", {}).get("actor_id")
                == "research-synthesis"
            )
        ]
        if not synthesis_reviews:
            # Legacy fixtures and interrupted pre-synthesis checkpoints may
            # already name the active materialized frontier without having a
            # synthesis Review yet.  Keep that bounded frontier available so
            # diagnostics and recovery tools can build a context; the
            # completion gate itself still cannot pass without its Review.
            return selected
        synthesis = max(
            synthesis_reviews,
            key=lambda record: (
                str(record.data.get("updated_at", "")),
                int(record.data.get("revision", 0)),
                record.id,
            ),
        )
        selected.add(synthesis.id)
        for ref in iter_artifact_refs(synthesis.data):
            record = index.get(str(ref["id"]))
            if record is None:
                continue
            record_project = (
                record.id if record.kind == "Project"
                else record.data.get("project_id")
            )
            if record_project == project_id:
                selected.add(record.id)
        return selected

    @staticmethod
    def _followup_records(
        project_id: str,
        checkpoint: Mapping[str, Any],
        index: Mapping[str, ArtifactRecord],
    ) -> set[str]:
        """Select only the failed completion frontier for targeted follow-up."""
        active_plan = checkpoint.get("active_plan")
        if not isinstance(active_plan, Mapping):
            raise RuntimeValidationError("targeted follow-up requires an active Plan")
        plan_id = str(active_plan.get("id", ""))
        plan_record = index.get(plan_id)
        if plan_record is None or plan_record.kind != "ResearchPlan":
            raise RuntimeValidationError("active follow-up Plan does not exist")

        selected = {project_id, plan_id}
        selected.update(
            record.id for record in index.values()
            if record.kind == "ResearchQuestion"
            and record.data.get("project_id") == project_id
        )
        for package in plan_record.data.get("work_packages", []):
            for mapping in package.get("materialized_outputs", []):
                artifact_id = str(mapping.get("artifact_ref", {}).get("id", ""))
                if artifact_id in index:
                    selected.add(artifact_id)

        gate_reviews = [
            record for record in index.values()
            if record.kind == "Review"
            and record.data.get("project_id") == project_id
            and record.data.get("assessment", {}).get("scheme") == "COMPLETION"
            and "synthesis" not in record.data.get("tags", [])
            and record.data.get("reviewer", {}).get("actor", {}).get("actor_id")
            != "research-synthesis"
        ]
        if not gate_reviews:
            raise RuntimeValidationError(
                "targeted follow-up requires a completion-gate Review"
            )
        gate_review = max(
            gate_reviews,
            key=lambda record: (
                str(record.data.get("updated_at", "")),
                int(record.data.get("revision", 0)),
                record.id,
            ),
        )
        selected.add(gate_review.id)

        # The gate cites the synthesis Review; the synthesis Review cites the
        # exact current Claims, Experiments, and independent Reviews behind
        # each unresolved issue.  Two bounded hops preserve that evidence
        # without importing superseded plans and Decisions.
        frontier = [gate_review.id]
        for _ in range(2):
            next_frontier: list[str] = []
            for artifact_id in frontier:
                for ref in iter_artifact_refs(index[artifact_id].data):
                    record = index.get(str(ref["id"]))
                    if record is None:
                        continue
                    record_project = (
                        record.id if record.kind == "Project"
                        else record.data.get("project_id")
                    )
                    if record_project == project_id and record.id not in selected:
                        selected.add(record.id)
                        next_frontier.append(record.id)
            frontier = next_frontier
        return selected

    @staticmethod
    def _execution_records(
        project_id: str,
        checkpoint: Mapping[str, Any],
        index: Mapping[str, ArtifactRecord],
        output_id: str,
        output_dependencies: Mapping[str, tuple[str, ...]],
    ) -> set[str]:
        """Build a bounded execution frontier for one deterministic Skill.

        A long run may contain dozens of immutable outputs and Reviews.  Only
        the active output, its planned-output ancestors, their artifact-ref
        closure, and Reviews directly assessing that closure are scientific
        inputs to the next Skill.  Unrelated historical outputs remain in the
        repository but cannot crowd the target out of a ContextBundle.
        """

        selected = {project_id}
        active_plan = checkpoint.get("active_plan")
        if isinstance(active_plan, Mapping):
            plan_id = str(active_plan.get("id", ""))
            if plan_id in index:
                selected.add(plan_id)

        # Literature is a shared, small project-level input and is required by
        # theory development/verification even when no Claim embeds a direct
        # LiteratureEvidence reference.
        selected.update(
            artifact_id
            for artifact_id, record in index.items()
            if record.kind == "LiteratureEvidence"
            and record.data.get("project_id") == project_id
            and record.data.get("status") in {"ASSESSED", "VERIFIED"}
        )

        relevant_outputs = {output_id}
        frontier_outputs = [output_id]
        while frontier_outputs:
            current = frontier_outputs.pop()
            for dependency in output_dependencies.get(current, ()):
                if dependency not in relevant_outputs:
                    relevant_outputs.add(dependency)
                    frontier_outputs.append(dependency)

        relevant_artifacts: set[str] = set()
        progress_map = checkpoint.get("skill_progress", {})
        for relevant_output in relevant_outputs:
            progress = progress_map.get(relevant_output, {})
            artifact_id = str(progress.get("artifact_ref", {}).get("id", ""))
            if artifact_id in index:
                relevant_artifacts.add(artifact_id)

        # Expand only a bounded artifact-ref closure.  This captures a target's
        # Claim/Experiment evidence without walking reverse history.
        frontier = list(relevant_artifacts)
        for _ in range(2):
            next_frontier: list[str] = []
            for current_id in frontier:
                # A Review is already a bounded, revision-pinned assessment of
                # its evidence.  Recursively expanding every evidence_ref from
                # historical Reviews recreates the entire project graph and can
                # exceed the byte cap even when the artifact count is small.
                # The Review stays visible and resources remain available via
                # resource_read; only its reverse history is not inlined.
                if index[current_id].kind == "Review":
                    continue
                for ref in iter_artifact_refs(index[current_id].data):
                    dependency_id = str(ref["id"])
                    dependency = index.get(dependency_id)
                    if dependency is None:
                        continue
                    dependency_project = (
                        dependency.id if dependency.kind == "Project"
                        else dependency.data.get("project_id")
                    )
                    if (
                        dependency_project == project_id
                        and dependency_id not in relevant_artifacts
                    ):
                        relevant_artifacts.add(dependency_id)
                        next_frontier.append(dependency_id)
            frontier = next_frontier
        selected.update(relevant_artifacts)

        # Preserve rejection feedback and verification evidence for exactly the
        # active closure, while excluding Reviews of unrelated old outputs.
        for artifact_id, record in index.items():
            if record.kind != "Review" or record.data.get("project_id") != project_id:
                continue
            target_id = str(
                record.data.get("target", {}).get("artifact_ref", {}).get("id", "")
            )
            if target_id in relevant_artifacts:
                selected.add(artifact_id)
        return selected

    @staticmethod
    def _execution_review_targets(
        checkpoint: Mapping[str, Any],
        index: Mapping[str, ArtifactRecord],
        output_id: str,
    ) -> set[str]:
        """Return the target/dependency closure whose Reviews are actionable.

        Reviews of unrelated outputs are immutable history, not inputs to the
        current execution Skill.  Keeping only reverse Reviews of the target
        and its bounded dependency closure prevents long projects from
        exceeding ContextBundle limits while preserving rejection feedback and
        evidence Reviews used by the target.
        """
        progress = checkpoint.get("skill_progress", {}).get(output_id, {})
        artifact_id = str(progress.get("artifact_ref", {}).get("id", ""))
        if not artifact_id or artifact_id not in index:
            return set()
        relevant = {artifact_id}
        frontier = [artifact_id]
        for _ in range(2):
            next_frontier: list[str] = []
            for current_id in frontier:
                for ref in iter_artifact_refs(index[current_id].data):
                    dependency_id = str(ref["id"])
                    if dependency_id in index and dependency_id not in relevant:
                        relevant.add(dependency_id)
                        next_frontier.append(dependency_id)
            frontier = next_frontier
        return relevant

    @staticmethod
    def _state_filter(state: str, record: ArtifactRecord) -> bool:
        status = record.data.get("status")
        if record.kind == "LiteratureEvidence":
            return status in {"ASSESSED", "VERIFIED"} or state == "DISCOVERY_LITERATURE"
        if record.kind == "ResearchQuestion":
            return status in {"ACTIVE", "REFRAMED"} or state == "DISCOVERY_QUESTION"
        if state.startswith("WRITING") and record.kind == "ScientificClaim":
            return status in {"VERIFIED", "IN_PAPER", "INVALIDATED"}
        if state.startswith("WRITING") and record.kind == "Experiment":
            return status in {"COMPLETED", "INVALIDATED"}
        if record.kind == "Decision":
            return status in {"ACCEPTED", "SUPERSEDED", "REVERSED"}
        return True

    @staticmethod
    def _snapshot(record: ArtifactRecord) -> dict[str, Any]:
        content = copy.deepcopy(record.data)
        return {
            "id": record.id,
            "kind": record.kind,
            "revision": content.get("revision"),
            "sha256": sha256_json(content),
            "content": content,
        }

    @staticmethod
    def _dependencies(records: Iterable[ArtifactRecord]) -> list[dict[str, Any]]:
        dependencies: dict[tuple[str, str, int | None], dict[str, Any]] = {}
        for record in records:
            for ref in iter_artifact_refs(record.data):
                key = (str(ref["id"]), str(ref["kind"]), ref.get("revision"))
                dependencies[key] = copy.deepcopy(ref)
        ordered = sorted(
            dependencies,
            key=lambda key: (key[0], key[1], -1 if key[2] is None else key[2]),
        )
        return [dependencies[key] for key in ordered]

    @staticmethod
    def _default_allowed_ids(
        state: str,
        role: str,
        checkpoint: Mapping[str, Any],
        records: Iterable[ArtifactRecord],
    ) -> list[str]:
        output_kinds = {
            rule["kind"] for rule in SKILL_OUTPUT_RULES.get(role, [])
        }
        result = [record.id for record in records if record.kind in output_kinds]
        active_plan = checkpoint.get("active_plan")
        if isinstance(active_plan, dict) and "ResearchPlan" in output_kinds:
            result.append(str(active_plan["id"]))
        return sorted(set(result))

    @staticmethod
    def _default_create_ids(
        state: str,
        role: str,
        project_id: str,
        action_id: str,
        checkpoint: Mapping[str, Any],
        index: Mapping[str, ArtifactRecord],
    ) -> list[str]:
        prefixes = {
            "ResearchQuestion": "rq",
            "ResearchPlan": "plan",
            "LiteratureEvidence": "lit",
            "ScientificClaim": "claim",
            "Experiment": "exp",
            "Review": "review",
            "Decision": "decision",
            "ResearchProposal": "proposal",
        }
        project_slug = project_id.removeprefix("proj-")
        if state == "EXECUTION_MATERIALIZE":
            active_plan = checkpoint.get("active_plan")
            if not isinstance(active_plan, dict) or active_plan.get("id") not in index:
                return []
            plan = index[str(active_plan["id"])].data
            materialized = {
                item["local_id"]
                for package in plan.get("work_packages", [])
                for item in package.get("materialized_outputs", [])
            }
            return sorted(
                f"{prefixes[output['kind']]}-{project_slug}-{output['local_id']}"
                for package in plan.get("work_packages", [])
                for output in package.get("planned_outputs", [])
                if output["local_id"] not in materialized
            )
        suffix = re.sub(r"[^a-z0-9]+", "-", action_id.lower()).strip("-")[-40:]
        output_kinds = {
            rule["kind"] for rule in SKILL_OUTPUT_RULES.get(role, [])
            if "CREATE" in rule.get("operations", [])
        }
        create_ids = sorted(
            f"{prefixes[kind]}-{project_slug}-{suffix}"
            for kind in output_kinds
            if kind in prefixes
        )
        if role == "literature-novelty" and "LiteratureEvidence" in output_kinds:
            single_literature_id = f"lit-{project_slug}-{suffix}"
            create_ids = [
                item for item in create_ids if item != single_literature_id
            ]
            create_ids.extend(
                f"{single_literature_id}-{position:02d}"
                for position in range(1, 9)
            )
        return sorted(create_ids)

    @staticmethod
    def _gate_contract(state: str) -> dict[str, Any] | None:
        contracts = {
            "DISCOVERY_FEASIBILITY_GATE": {
                "gate_type": "FEASIBILITY",
                "allowed_verdicts": ["PASS", "RETHINK_QUESTION"],
            },
            "SYNTHESIS_COMPLETION_GATE": {
                "gate_type": "COMPLETION",
                "allowed_verdicts": [
                    "COMPLETE", "INCOMPLETE", "RETHINK_CLAIM", "RETHINK_PLAN", "RETHINK_QUESTION",
                ],
            },
            "CONSISTENCY_GATE": {
                "gate_type": "CONSISTENCY",
                "allowed_verdicts": [
                    "PASS", "INCOMPLETE", "RETHINK_CLAIM", "RETHINK_PLAN", "RETHINK_QUESTION",
                ],
            },
            "WRITING_FINAL_REVIEW": {
                "gate_type": "FINAL",
                "allowed_verdicts": ["PASS", "REVISION_REQUIRED"],
            },
        }
        return copy.deepcopy(contracts.get(state))

    @staticmethod
    def _role_for(
        state: str,
        track: str | None,
        checkpoint: Mapping[str, Any],
        output_dependencies: Mapping[str, tuple[str, ...]] | None = None,
    ) -> tuple[str, str, str | None]:
        if state == "EXECUTION_TRACKS":
            if track not in {"theory", "experiment"}:
                raise RuntimeValidationError("execution context requires theory or experiment track")
            branch = checkpoint.get("branch_states", {}).get(track)
            phase_skills = {
                ("theory", "DEVELOP"): {"theory-development"},
                ("theory", "VERIFY"): {
                    "lean-formalization", "theory-verification",
                    "lean-verification", "semantic-alignment-review",
                },
                ("experiment", "PREPARE"): {"experiment-design"},
                ("experiment", "RUN"): {"experiment-execution"},
                ("experiment", "ASSESS"): {"experiment-verification"},
            }.get((track, branch), set())
            progress_map = checkpoint.get("skill_progress", {})
            dependencies = output_dependencies or {}
            for output_id in sorted(checkpoint.get("skill_progress", {})):
                progress = checkpoint["skill_progress"][output_id]
                if progress.get("track") != track or progress.get("status") == "COMPLETED":
                    continue
                dependency_progress = [
                    progress_map.get(dependency_id)
                    for dependency_id in dependencies.get(output_id, ())
                    if progress_map.get(dependency_id) is not None
                ]
                if branch == "DEVELOP" and not all(
                    (
                        "theory-development" in item.get("completed_skills", [])
                        if item.get("track") == "theory"
                        else item.get("status") == "COMPLETED"
                    )
                    for item in dependency_progress
                ):
                    continue
                if branch == "VERIFY" and not all(
                    item.get("status") == "COMPLETED"
                    for item in dependency_progress
                ):
                    continue
                completed = set(progress.get("completed_skills", []))
                skill = next(
                    (item for item in progress.get("required_skills", []) if item not in completed),
                    None,
                )
                if skill in phase_skills:
                    return skill, SKILL_PRESETS[skill], output_id
            raise RuntimeValidationError(
                f"no pending Skill matches {track} branch phase {branch}"
            )
        try:
            role, preset = ROLE_POLICIES[state]
            return role, preset, None
        except KeyError as exc:
            raise RuntimeValidationError(f"no handler policy for state {state}") from exc

    @staticmethod
    def _planned_output_dependencies(
        checkpoint: Mapping[str, Any], index: Mapping[str, Any]
    ) -> dict[str, tuple[str, ...]]:
        plan_ref = checkpoint.get("active_plan")
        if not isinstance(plan_ref, Mapping):
            return {}
        record = index.get(str(plan_ref.get("id", "")))
        if record is None or record.kind != "ResearchPlan":
            return {}
        return {
            str(output["local_id"]): tuple(str(item) for item in output.get("depends_on_outputs", []))
            for package in record.data.get("work_packages", [])
            for output in package.get("planned_outputs", [])
            if output.get("local_id")
        }
