"""One-command MVP controller plus a deterministic synthetic Skill runtime.

The production runtime boundary remains the DeepSeek Cordis plugin.  The
synthetic adapter exists solely to exercise the complete scientific wiring in
CI without a model or network dependency.
"""

from __future__ import annotations

import copy
import hashlib
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

import yaml

from .action_compiler import ActionCompiler
from .artifact_service import ArtifactService
from .cost_control import BudgetApprovalRequired, BudgetStore
from .orchestrator import CheckpointConflict, OrchestratorError, utc_now
from .runtime_types import (
    ContextLimitError,
    GitConflict,
    RuntimeIntegrationError,
    RuntimeValidationError,
    atomic_write_json,
    atomic_write_text,
)
from .tool_adapters import ExperimentExecutionAdapter, LeanToolAdapter
from .transactions import ScientificTransaction
from .workspace import ArtifactValidationError, ArtifactWorkspace, RevisionConflict


STOP_STATES = {"BLOCKED", "PAUSED_BY_USER", "CANCELLED_BY_USER", "DONE"}
DIRECT_EVENTS = {
    "DISCOVERY_QUESTION": "QUESTION_ACTIVATED",
    "DISCOVERY_LITERATURE": "LITERATURE_READY",
    "PLANNING_DRAFT": "PLAN_VALIDATED",
    "SYNTHESIS_BUILD": "SYNTHESIS_COMPLETED",
    "FOLLOWUP_TARGETED": "FOLLOWUP_PLANNED",
    "FOLLOWUP_CLAIM_RETHINK": "CLAIM_REPLACED",
    "WRITING_DRAFT": "WRITING_COMPLETED",
    "WRITING_TARGETED_REVISION": "REVISION_COMPLETED",
    "PROPOSAL_STRUCTURING": "PROPOSAL_STRUCTURED",
    "PROPOSAL_NOVELTY_REVIEW": "PROPOSAL_NOVELTY_REVIEWED",
    "PROPOSAL_PLAN_RECONSTRUCTION": "PROPOSAL_PLAN_VALIDATED",
    "PROPOSAL_CORRECTNESS_REVIEW": "PROPOSAL_CORRECTNESS_REVIEWED",
    "PROPOSAL_REVISION": "PROPOSAL_REVISION_COMPLETED",
}


class SkillRuntime(Protocol):
    def execute(self, action: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]: ...


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=check)


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip().lower()


class MVPController:
    """Deterministic top-level loop; it never asks a Skill for the next state."""

    def __init__(self, root: str | Path, runtime: SkillRuntime):
        self.root = Path(root).resolve()
        self.runtime = runtime
        self.transactions = ScientificTransaction(self.root)
        self.compiler = ActionCompiler(self.root)
        self.artifacts = ArtifactService(self.root)
        self.budgets = BudgetStore(self.root)

    def init(
        self, project: str, objective: str, *, mode: str = "full-research",
        proposal: str | Path | None = None, rubric: str | Path | None = None,
        source_materials: Iterable[str | Path] | None = None,
    ) -> dict[str, Any]:
        project_id = project if project.startswith("proj-") else f"proj-{project}"
        workflow_mode = mode.replace("-", "_").upper()
        return self.transactions.initialize_project(
            project_id=project_id, objective=objective, commit=True,
            workflow_mode=workflow_mode, proposal_path=proposal, rubric_path=rubric,
            source_materials=source_materials,
        )["checkpoint"]

    def status(self, project_id: str) -> dict[str, Any]:
        checkpoint = self._load_checkpoint(project_id)
        preview = self.compiler.preview(project_id)
        pending = [
            skill for item in checkpoint.get("skill_progress", {}).values()
            for skill in item.get("required_skills", [])
            if skill not in item.get("completed_skills", [])
        ]
        completed = [
            skill for item in checkpoint.get("skill_progress", {}).values()
            for skill in item.get("completed_skills", [])
        ]
        result = {
            "project": project_id, "state": checkpoint["state"],
            "workflow_mode": checkpoint.get("workflow_mode", "FULL_RESEARCH"),
            "status": checkpoint["status"], "active_target": preview.get("target_ref"),
            "current_skill": preview.get("skill"), "completed_skills": completed,
            "pending_skills": pending,
            "open_blockers": [] if checkpoint.get("blocker") is None else [checkpoint["blocker"]],
            "next_expected_action": self._next_action(checkpoint, preview),
            "checkpoint_seq": checkpoint["checkpoint_seq"],
        }
        result["budget"] = self.budgets.status(project_id)
        return result

    def run(
        self, project_id: str, max_actions: int = 100,
        budget_percent: float | None = None,
    ) -> dict[str, Any]:
        checkpoint = self._load_checkpoint(project_id)
        budget = self.budgets.load(project_id)
        if budget is None:
            if budget_percent is None:
                return {"reason": "WAITING_HUMAN:BUDGET", "checkpoint": checkpoint,
                        "budget": self.budgets.status(project_id)}
            self.budgets.approve_initial(
                project_id, str(checkpoint["run_id"]), budget_percent
            )
        elif budget["status"] == "WAITING_HUMAN_BUDGET":
            return {"reason": "WAITING_HUMAN:BUDGET", "checkpoint": checkpoint,
                    "budget": self.budgets.status(project_id)}
        for _ in range(max_actions):
            checkpoint = self._load_checkpoint(project_id)
            if checkpoint["state"] in {"WRITING_FINAL_APPROVAL", "PROPOSAL_FINAL_APPROVAL"}:
                return {"reason": "WAITING_FINAL_APPROVAL", "checkpoint": checkpoint}
            if checkpoint["state"] == "PROPOSAL_WAITING_DECISIONS":
                return {
                    "reason": "WAITING_HUMAN:PROPOSAL_DECISIONS",
                    "checkpoint": checkpoint,
                    "decision_file": str(self._decision_template_path(project_id)),
                }
            if checkpoint["state"] in STOP_STATES:
                return {"reason": checkpoint["state"], "checkpoint": checkpoint}
            try:
                if self._advance_controller(checkpoint):
                    continue
            except RuntimeValidationError as exc:
                if checkpoint["state"] == "PLANNING_APPROVAL" and isinstance(exc.details, Mapping) and exc.details.get("blocking_reviews"):
                    return {"reason": "WAITING_HUMAN", "checkpoint": checkpoint, "blocker": exc.details}
                raise
            budget_wait = self._execute_action(checkpoint)
            if budget_wait is not None:
                return {"reason": "WAITING_HUMAN:BUDGET",
                        "checkpoint": self._load_checkpoint(project_id),
                        "budget": self.budgets.status(project_id),
                        "request": budget_wait}
        return {"reason": "ACTION_LIMIT", "checkpoint": self.transactions.orchestrator.store.load(project_id)}

    def approve_budget(self, project_id: str, additional_percent: float) -> dict[str, Any]:
        return self.budgets.increase(project_id, additional_percent)

    def pause(self, project_id: str, reason: str) -> None:
        self._event(project_id, "USER_PAUSE", {"reason": reason})

    def resume(self, project_id: str) -> None:
        self._event(project_id, "RESUME", {})

    def unblock(self, project_id: str, reason: str) -> None:
        decision = self.transactions.record_recovery_decision(
            project_id=project_id,
            reason=reason,
            expected_git_commit=_head(self.root),
        )
        self._event(
            project_id,
            "UNBLOCK",
            {
                "reason": reason,
                "recovery_decision_ref": {
                    "id": decision["id"],
                    "kind": "Decision",
                    "revision": decision["revision"],
                },
            },
        )

    def cancel(self, project_id: str, reason: str) -> None:
        self._event(project_id, "USER_CANCEL", {"reason": reason})

    def approve(self, project_id: str, reason: str = "Human approved final submission") -> None:
        checkpoint = self._load_checkpoint(project_id)
        if checkpoint["state"] == "PLANNING_APPROVAL":
            self.transactions.approve_plan(
                project_id=project_id, expected_git_commit=_head(self.root),
                expected_checkpoint_seq=int(checkpoint["checkpoint_seq"]),
                actor_id="researcher", allow_blockers=True, commit=True,
            )
            return
        if checkpoint["state"] == "PROPOSAL_FINAL_APPROVAL":
            self._event(project_id, "PROPOSAL_FINAL_APPROVED", {"reason": reason})
            return
        if checkpoint["state"] != "WRITING_FINAL_APPROVAL":
            raise RuntimeValidationError(f"final approval is invalid in {checkpoint['state']}")
        self._event(project_id, "FINAL_APPROVED", {"reason": reason})

    def proposal_decisions(self, project_id: str, path: str | Path) -> dict[str, Any]:
        result = self.transactions.record_proposal_decisions(
            project_id=project_id, decisions_path=path,
            expected_git_commit=_head(self.root),
        )
        if not result["all_authorized"]:
            return {"reason": "WAITING_HUMAN:PROPOSAL_DECISIONS", **result}
        self._event(project_id, "PROPOSAL_DECISIONS_RECORDED", {
            "decision_refs": [
                {"id": item["id"], "kind": "Decision", "revision": item["revision"]}
                for item in result["decisions"]
            ],
        })
        return {**result, "checkpoint": self._load_checkpoint(project_id)}

    def convert(self, project_id: str, derived_project: str) -> dict[str, Any]:
        derived_id = derived_project if derived_project.startswith("proj-") else f"proj-{derived_project}"
        return self.transactions.convert_approved_proposal(
            project_id=project_id, derived_project_id=derived_id,
            expected_git_commit=_head(self.root), commit=True,
        )

    def _advance_controller(self, checkpoint: Mapping[str, Any]) -> bool:
        project_id = str(checkpoint["project_id"])
        state = checkpoint["state"]
        if state == "PROPOSAL_REVIEW_GATE":
            gate = self._proposal_gate(project_id, checkpoint)
            self._event(project_id, "GATE_RECORDED", {"gate": gate})
            if (
                gate["verdict"] == "REVISION_REQUIRED"
                and self._load_checkpoint(project_id)["state"] == "PROPOSAL_WAITING_DECISIONS"
            ):
                self._write_decision_template(project_id)
            return True
        if state == "PLANNING_APPROVAL":
            self.transactions.approve_plan(
                project_id=project_id, expected_git_commit=_head(self.root),
                expected_checkpoint_seq=int(checkpoint["checkpoint_seq"]), commit=True,
            )
            return True
        if state == "PLANNING_ROUTING":
            refs = [
                {"id": record.id, "kind": record.kind, "revision": record.data["revision"]}
                for record in ArtifactWorkspace(self.root).query(project_id=project_id)
            ]
            self._event(project_id, "ROUTE_SELECTED", {
                "snapshot": {"git_commit": _head(self.root), "artifacts": refs}
            })
            return True
        if state == "EXECUTION_MATERIALIZE":
            self.transactions.materialize_outputs(
                project_id=project_id,
                action_id=f"{checkpoint['run_id']}-{checkpoint['checkpoint_seq'] + 1}-materialize",
                expected_git_commit=_head(self.root),
                expected_checkpoint_seq=int(checkpoint["checkpoint_seq"]), commit=True, push=False,
            )
            return True
        if state == "EXECUTION_TRACKS":
            for track in ("theory", "experiment"):
                status = checkpoint["branch_states"][track]
                next_status = self._next_track_status(checkpoint, track, status)
                if next_status:
                    self._event(project_id, "TRACK_ADVANCED", {"track": track, "status": next_status})
                    return True
            selected = [value for value in checkpoint["branch_states"].values() if value != "NOT_SELECTED"]
            if selected and all(value in {"COMPLETED", "WAIVED"} for value in selected):
                self._event(project_id, "TRACKS_JOINED", {})
                return True
        if state == "EXECUTION_JOIN":
            self._event(project_id, "JOIN_COMPLETED", {})
            return True
        return False

    @staticmethod
    def _next_action(checkpoint: Mapping[str, Any], preview: Mapping[str, Any]) -> str:
        state = checkpoint["state"]
        if state in {"WRITING_FINAL_APPROVAL", "PROPOSAL_FINAL_APPROVAL"}:
            return "human-final-approval"
        if state == "PROPOSAL_WAITING_DECISIONS":
            return "proposal-decisions"
        return str(preview.get("skill") or preview.get("state") or state)

    def _active_proposal(self, project_id: str):
        project = self.artifacts.workspace.get(project_id)
        ref = project.data.get("active_proposal")
        if not isinstance(ref, Mapping):
            raise RuntimeValidationError("project has no active proposal")
        record = self.artifacts.workspace.get(str(ref["id"]))
        if record.kind != "ResearchProposal":
            raise RuntimeValidationError("active_proposal does not identify a ResearchProposal")
        return record

    def _proposal_gate(self, project_id: str, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        del checkpoint
        return self.transactions.evaluate_proposal_gate(project_id=project_id)

    def _decision_template_path(self, project_id: str) -> Path:
        return self.root / ".harness" / "research" / "requests" / project_id / "proposal-decisions.yaml"

    def _write_decision_template(self, project_id: str) -> Path:
        result = self.transactions.write_proposal_decision_template(
            project_id=project_id
        )
        return Path(str(result["path"]))

    def _execute_action(self, checkpoint: Mapping[str, Any]) -> dict[str, Any] | None:
        project_id = str(checkpoint["project_id"])
        pending = checkpoint.get("pending_action")
        if isinstance(pending, Mapping):
            # Resume the exact persisted action and its frozen scientific input
            # snapshot.  A process interruption must not create a second action
            # or silently move the input boundary to the latest conversation or
            # repository state.
            action_id = str(pending["action_id"])
            input_head = str(pending["input_git_commit"])
        else:
            action_id = f"{checkpoint['run_id']}-{checkpoint['checkpoint_seq'] + 1}-{str(checkpoint['state']).lower().replace('_', '-')}"
            input_head = _head(self.root)
            begun = self.transactions.begin_action(
                project_id=project_id, action_id=action_id, input_git_commit=input_head,
                expected_seq=int(checkpoint["checkpoint_seq"]), commit=True,
            )
            checkpoint = begun["checkpoint"]
        try:
            compiled = self.compiler.compile(
                project_id=project_id, action_id=action_id, input_git_commit=input_head
            )
            try:
                reservation = self.budgets.reserve(project_id, compiled["action"])
            except BudgetApprovalRequired as exc:
                return dict(exc.details)
            runtime_action = dict(compiled["action"])
            for key in (
                "model_tier", "model_alias", "reasoning_effort", "quality_floor",
                "routing_reason", "estimated_cost_percent",
            ):
                runtime_action[key] = reservation[key]
            try:
                submission = self.runtime.execute(runtime_action, compiled["bundle"])
            except BaseException:
                self.budgets.consume(project_id, action_id)
                raise
            usage = submission.get("usage")
            self.budgets.consume(
                project_id, action_id,
                usage if isinstance(usage, Mapping) else None,
            )
        except (RevisionConflict, GitConflict, CheckpointConflict) as exc:
            self._event(project_id, "ACTION_CONFLICT", {
                "action_id": action_id, "reason": str(exc),
            })
            return
        except ContextLimitError as exc:
            self._record_action_failure(project_id, action_id, exc, retryable=False)
            return
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            self._record_action_failure(project_id, action_id, exc, retryable=True)
            return
        except (RuntimeValidationError, ArtifactValidationError, OSError) as exc:
            self._record_action_failure(project_id, action_id, exc, retryable=True)
            return
        if submission.get("outcome") != "SUBMITTED":
            self._event(project_id, "ACTION_FAILED", {
                "action_id": action_id, "retry_key": "action", "retryable": True,
                "reason": submission.get("failure_classification", "FAILED"),
            })
            return
        proposals = submission.get("proposal_ids", [])
        artifacts = []
        if proposals:
            artifacts = self.artifacts.validate_action(project_id, action_id, proposals)["artifacts"]
        if "gate_assessment" in submission:
            # Gate validation runs inside promotion after the Review overlay is
            # canonicalized in the same recoverable transaction.
            event = {"type": "GATE_RECORDED", "payload": {"action_id": action_id, "gate": submission["gate_assessment"]}}
        elif checkpoint["state"] == "EXECUTION_TRACKS":
            skill_id = str(compiled["action"]["skill"])
            event_type = "SKILL_COMPLETED"
            if skill_id == "theory-verification":
                staged = [
                    proposal
                    for proposal in self.artifacts.list_proposals(project_id, action_id)
                    if proposal.proposal_id in set(proposals)
                    and proposal.kind == "Review"
                    and proposal.candidate.get("assessment", {}).get("scheme") == "THEORY"
                ]
                outcomes = {
                    str(proposal.candidate.get("assessment", {}).get("outcome"))
                    for proposal in staged
                }
                if outcomes and "PASS" not in outcomes:
                    event_type = "SKILL_REVISION_REQUIRED"
            event = {"type": event_type, "payload": {
                "action_id": action_id, "output_id": compiled["action"]["target_output_id"],
                "skill_id": skill_id, "artifact_ids": artifacts,
            }}
        else:
            payload: dict[str, Any] = {
                "action_id": action_id, "artifact_ids": artifacts,
            }
            if checkpoint["state"] == "FOLLOWUP_TARGETED":
                plan_proposals = [
                    proposal
                    for proposal in self.artifacts.list_proposals(project_id, action_id)
                    if proposal.proposal_id in set(proposals)
                    and proposal.kind == "ResearchPlan"
                ]
                if len(plan_proposals) != 1:
                    raise RuntimeValidationError(
                        "targeted follow-up must submit exactly one ResearchPlan"
                    )
                plan = plan_proposals[0].candidate
                payload["plan_ref"] = {
                    "id": plan["id"], "kind": "ResearchPlan",
                    "revision": plan["revision"],
                }
            event = {
                "type": DIRECT_EVENTS[checkpoint["state"]],
                "payload": payload,
            }
        try:
            self.transactions.promote_action(
                project_id=project_id, action_id=action_id,
                expected_git_commit=_head(self.root),
                expected_checkpoint_seq=int(checkpoint["checkpoint_seq"]),
                event=event, commit=True, push=False,
            )
        except (RevisionConflict, GitConflict, CheckpointConflict) as exc:
            self._event(project_id, "ACTION_CONFLICT", {
                "action_id": action_id, "reason": str(exc),
            })
        except (RuntimeValidationError, ArtifactValidationError, OSError) as exc:
            self._record_action_failure(project_id, action_id, exc, retryable=True)
        return None

    def _event(self, project_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        checkpoint = self._load_checkpoint(project_id)
        self.transactions.apply_control_event(
            project_id=project_id, event={"type": event_type, "payload": dict(payload)},
            expected_git_commit=_head(self.root), expected_seq=int(checkpoint["checkpoint_seq"]),
            commit=True,
        )

    def _load_checkpoint(self, project_id: str) -> dict[str, Any]:
        try:
            return self.transactions.orchestrator.store.load(project_id)
        except OrchestratorError as exc:
            return self.transactions.fail_closed_checkpoint(
                project_id=project_id,
                reason=f"checkpoint corruption detected: {exc}",
            )["checkpoint"]

    def _record_action_failure(
        self, project_id: str, action_id: str,
        error: BaseException, *, retryable: bool,
    ) -> None:
        classification = (
            getattr(error, "code", None)
            if isinstance(error, RuntimeIntegrationError)
            else type(error).__name__.upper()
        )
        self._event(project_id, "ACTION_FAILED", {
            "action_id": action_id,
            "retry_key": "action",
            "retryable": retryable,
            "reason": f"{classification}: {error}",
            "recovery_condition": (
                "Provide a pinned recovery Decision after correcting the dependency or state."
                if not retryable else "Retry from the persisted artifact/checkpoint snapshot."
            ),
        })

    @staticmethod
    def _next_track_status(checkpoint: Mapping[str, Any], track: str, status: str) -> str | None:
        if status == "PENDING":
            return "DEVELOP" if track == "theory" else "PREPARE"
        phases = {
            ("theory", "DEVELOP"): ("VERIFY", {"theory-development"}),
            ("theory", "VERIFY"): ("COMPLETED", {"theory-verification", "lean-formalization", "lean-verification", "semantic-alignment-review"}),
            ("experiment", "PREPARE"): ("RUN", {"experiment-design"}),
            ("experiment", "RUN"): ("ASSESS", {"experiment-execution"}),
            ("experiment", "ASSESS"): ("COMPLETED", {"experiment-verification"}),
        }
        target = phases.get((track, status))
        if target is None:
            return None
        next_status, skills = target
        relevant = [item for item in checkpoint.get("skill_progress", {}).values() if item.get("track") == track and any(skill in skills for skill in item.get("required_skills", []))]
        if relevant and all(all(skill in item.get("completed_skills", []) for skill in item.get("required_skills", []) if skill in skills) for item in relevant):
            return next_status
        return None


class SyntheticSkillRuntime:
    """Schema-valid deterministic Skill fixtures used only for MVP E2E tests."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.service = ArtifactService(self.root)

    def execute(self, action: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
        role = str(action["skill"])
        handler = getattr(self, f"_skill_{role.replace('-', '_')}")
        proposals, gate = handler(action, bundle)
        return self.service.submit_action(
            project_id=str(action["project_id"]), action_id=str(action["action_id"]),
            bundle_sha256=str(bundle["bundle_sha256"]), proposal_ids=proposals,
            outcome="SUBMITTED", gate_assessment=gate,
        )

    @staticmethod
    def _artifact(bundle: Mapping[str, Any], kind: str) -> dict[str, Any]:
        for item in bundle["artifacts"]:
            if item["kind"] == kind:
                return copy.deepcopy(item["content"])
        raise RuntimeValidationError(f"synthetic fixture lacks {kind}")

    @staticmethod
    def _artifact_by_id(bundle: Mapping[str, Any], artifact_id: str) -> dict[str, Any]:
        for item in bundle["artifacts"]:
            if item["id"] == artifact_id:
                return copy.deepcopy(item["content"])
        raise RuntimeValidationError(f"synthetic fixture lacks {artifact_id}")

    @staticmethod
    def _target(bundle: Mapping[str, Any]) -> dict[str, Any]:
        ids = set(bundle["allowed_outputs"]["artifact_ids"])
        for item in bundle["artifacts"]:
            if item["id"] in ids:
                return copy.deepcopy(item["content"])
        raise RuntimeValidationError("synthetic fixture lacks its target artifact")

    @staticmethod
    def _actor(role: str, action: Mapping[str, Any]) -> dict[str, Any]:
        return {"actor_type": "agent", "actor_id": role, "session_id": f"synthetic-{action['action_id']}"}

    def _envelope(self, action: Mapping[str, Any], kind: str, artifact_id: str, title: str, status: str) -> dict[str, Any]:
        now = utc_now(); actor = self._actor(str(action["skill"]), action)
        proposal_schema = str(action.get("state", "")).startswith("PROPOSAL_") and kind in {"ResearchProposal", "Review", "Decision"}
        return {"schema_version": "research-artifact/v0.1.3" if proposal_schema else "research-artifact/v0.1.2", "kind": kind, "id": artifact_id,
                "project_id": action["project_id"], "title": title, "status": status, "revision": 1,
                "created_at": now, "updated_at": now,
                "provenance": {"created_by": actor, "updated_by": actor}, "tags": ["synthetic"]}

    def _create(self, action: Mapping[str, Any], bundle: Mapping[str, Any], candidate: Mapping[str, Any], suffix: str) -> str:
        proposal_id = f"proposal-{suffix}"
        self.service.stage_proposal(
            proposal_id=proposal_id, project_id=str(action["project_id"]), action_id=str(action["action_id"]),
            operation="CREATE", artifact_id=str(candidate["id"]), kind=str(candidate["kind"]), base_revision=None,
            bundle_sha256=str(bundle["bundle_sha256"]), proposer_session_id=f"synthetic-{action['action_id']}", candidate=candidate,
        )
        return proposal_id

    def _revise(self, action: Mapping[str, Any], bundle: Mapping[str, Any], candidate: dict[str, Any], suffix: str) -> str:
        current = ArtifactWorkspace(self.root).get(candidate["id"])
        candidate["revision"] = int(current.data["revision"]) + 1
        candidate["updated_at"] = utc_now()
        candidate["provenance"]["updated_by"] = self._actor(str(action["skill"]), action)
        proposal_id = f"proposal-{suffix}"
        self.service.stage_proposal(
            proposal_id=proposal_id, project_id=str(action["project_id"]), action_id=str(action["action_id"]),
            operation="REVISE", artifact_id=str(candidate["id"]), kind=str(candidate["kind"]),
            base_revision=int(current.data["revision"]), bundle_sha256=str(bundle["bundle_sha256"]),
            proposer_session_id=f"synthetic-{action['action_id']}", candidate=candidate,
        )
        return proposal_id

    def _review(self, action: Mapping[str, Any], bundle: Mapping[str, Any], *, review_id: str,
                target: Mapping[str, Any], scheme: str, outcome: str, reviewer_type: str,
                resources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        value = self._envelope(action, "Review", review_id, f"{scheme} synthetic review", "RESOLVED")
        value.update({
            "target": {"artifact_ref": {"id": target["id"], "kind": target["kind"], "revision": target["revision"]}, "git_commit": bundle["input_git_commit"]},
            "reviewer": {"actor": self._actor(str(action["skill"]), action), "reviewer_type": reviewer_type},
            "summary": f"Deterministic synthetic {scheme} check returned {outcome}.",
            "assessment": {"scheme": scheme, "outcome": outcome, "items": [{"subject": target["id"], "outcome": outcome, "confidence": 1.0, "evidence_refs": [{"id": target["id"], "kind": target["kind"], "revision": target["revision"]}]}]},
            "evidence_resources": resources or [], "issues": [], "recommendation": "ACCEPT",
            "resolution_summary": "All deterministic synthetic checks passed.",
        })
        return value

    def _gate(self, bundle: Mapping[str, Any], gate_type: str, verdict: str, review: Mapping[str, Any], based_on: list[dict[str, Any]]) -> dict[str, Any]:
        return {"gate_id": f"gate-{bundle['action_id']}", "gate_type": gate_type, "verdict": verdict,
                "based_on": based_on, "target_refs": [], "target_output_ids": [],
                "review_refs": [{"id": review["id"], "kind": "Review", "revision": 1}],
                "rationale": review["summary"], "recorded_at": utc_now()}

    def _skill_question_framing(self, action, bundle):
        artifact_id = bundle["allowed_outputs"]["create_ids"][0]
        value = self._envelope(action, "ResearchQuestion", artifact_id, "Synthetic correctness question", "ACTIVE")
        value.update({"problem_definition": "Can revision-pinned artifacts preserve scientific correctness across an interrupted mixed workflow?",
                      "background": "Scientific agents require durable evidence beyond chat history.",
                      "scope": {"included": ["deterministic state and evidence"], "excluded": ["real-world performance claims"]},
                      "known_results": [], "research_gap": "The complete wiring has not been exercised end to end.",
                      "hypotheses": [{"statement": "The harness reaches final approval while enforcing formal and protocol gates."}],
                      "expected_contributions": ["An executable scientific-control trace."],
                      "risks": [{"category": "THEORY", "description": "A gate could be bypassed.", "likelihood": "LOW", "impact": "HIGH", "mitigation": "Use adversarial tests."}]})
        proposals = [self._create(action, bundle, value, "question")]
        if action["state"] == "PROPOSAL_STRUCTURING":
            proposal = self._artifact(bundle, "ResearchProposal")
            map_path = (
                self.root / "projects" / action["project_id"] / "resources" / "proposal"
                / "source-maps" / f"proposal-r{proposal['revision']:03d}.json"
            )
            source_map = {
                "proposal_ref": {"id": proposal["id"], "kind": "ResearchProposal", "revision": proposal["revision"]},
                "question_ref": {"id": value["id"], "kind": "ResearchQuestion", "revision": 1},
                "fields": {
                    "problem_definition": proposal["section_index"][:1] or [{"locator": "page 1, line 1"}],
                    "hypotheses": proposal["section_index"],
                    "expected_contributions": proposal["section_index"],
                },
            }
            atomic_write_json(map_path, source_map)
            proposal["status"] = "STRUCTURED"
            proposal["question_refs"] = [{"id": value["id"], "kind": "ResearchQuestion", "revision": 1}]
            proposal["source_map"] = {
                "uri": str(map_path.relative_to(self.root)),
                "sha256": hashlib.sha256(map_path.read_bytes()).hexdigest(),
                "media_type": "application/json",
                "description": "Field-level proposal to ResearchQuestion source map",
            }
            proposals.append(self._revise(action, bundle, proposal, "proposal-structured"))
        return proposals, None

    def _skill_literature_novelty(self, action, bundle):
        if action["state"] == "PROPOSAL_NOVELTY_REVIEW":
            proposal_target = self._artifact(bundle, "ResearchProposal")
            question = self._artifact_by_id(
                bundle, proposal_target["question_refs"][-1]["id"]
            )
            lit_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("lit-"))
            evidence = self._envelope(action, "LiteratureEvidence", lit_id, "Synthetic proposal novelty search", "VERIFIED")
            evidence.update({
                "paper": {"title": "Synthetic novelty baseline", "authors": [{"name": "Synthetic Author"}], "year": 2026, "venue": "Synthetic Archive", "identifiers": {"url": "https://example.invalid/proposal-novelty"}},
                "relations": [{"target_ref": {"id": question["id"], "kind": "ResearchQuestion", "revision": question["revision"]}, "type": "BASELINE", "explanation": "Deterministic offline test evidence."}],
                "main_results": [{"statement": "No equivalent result exists in the deterministic fixture corpus.", "locator": "fixture:1"}],
                "differences": [{"dimension": "scope", "cited_work": "Baseline only", "current_research": "Proposal contribution"}],
                "novelty_impact": {"assessment": "SUPPORTS", "rationale": "Fixture intentionally leaves the contribution open."},
                "confidence": {"score": 1.0, "rationale": "Synthetic runtime validates orchestration, not real novelty."},
            })
            review_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("review-"))
            review = self._review(action, bundle, review_id=review_id, target=proposal_target, scheme="NOVELTY", outcome="OPEN", reviewer_type="LITERATURE")
            review["summary"] = "Deterministic fixture found no equivalent result; real deployments must use academic search evidence."
            review["assessment"]["items"][0]["evidence_refs"] = [
                {"id": evidence["id"], "kind": "LiteratureEvidence", "revision": 1}
            ]
            return [
                self._create(action, bundle, evidence, "proposal-literature"),
                self._create(action, bundle, review, "proposal-novelty-review"),
            ], None
        if action["state"] == "DISCOVERY_LITERATURE":
            question = self._artifact(bundle, "ResearchQuestion")
            artifact_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("lit-"))
            value = self._envelope(action, "LiteratureEvidence", artifact_id, "Synthetic prior workflow evidence", "VERIFIED")
            value.update({"paper": {"title": "Artifact-driven research workflows", "authors": [{"name": "Synthetic Author"}], "year": 2026, "venue": "Synthetic Archive", "identifiers": {"url": "https://example.invalid/artifact-workflows"}},
                          "relations": [{"target_ref": {"id": question["id"], "kind": "ResearchQuestion", "revision": question["revision"]}, "type": "BASELINE", "explanation": "Provides an artifact baseline but not the complete gate wiring."}],
                          "main_results": [{"statement": "Persisted artifacts support recoverable research state.", "locator": "Section 1"}],
                          "differences": [{"dimension": "verification", "cited_work": "No formal plus experimental gate.", "current_research": "Requires both."}],
                          "novelty_impact": {"assessment": "SUPPORTS", "rationale": "The target integration remains distinct."},
                          "confidence": {"score": 1.0, "rationale": "Synthetic source is explicitly scoped as an evaluation fixture."}})
            return [self._create(action, bundle, value, "literature")], None
        question = self._artifact(bundle, "ResearchQuestion"); literature = self._artifact(bundle, "LiteratureEvidence")
        review_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("review-"))
        review = self._review(action, bundle, review_id=review_id, target=question, scheme="NOVELTY", outcome="OPEN", reviewer_type="LITERATURE")
        proposal = self._create(action, bundle, review, "novelty-review")
        based = [{"id": question["id"], "kind": question["kind"], "revision": question["revision"]}, {"id": literature["id"], "kind": literature["kind"], "revision": literature["revision"]}]
        return [proposal], self._gate(bundle, "FEASIBILITY", "PASS", review, based)

    def _skill_research_planning(self, action, bundle):
        question = self._artifact(bundle, "ResearchQuestion")
        if action["state"] == "PROPOSAL_PLAN_RECONSTRUCTION":
            proposal = self._artifact(bundle, "ResearchProposal")
            question = self._artifact_by_id(bundle, proposal["question_refs"][-1]["id"])
        literature = self._artifact(bundle, "LiteratureEvidence")
        plan_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("plan-"))
        value = self._envelope(action, "ResearchPlan", plan_id, "Synthetic mixed research plan", "DRAFT")
        value.update({"question_refs": [{"id": question["id"], "kind": "ResearchQuestion", "revision": question["revision"]}],
                      "strategy": "Verify a core formal claim and execute a protocol-locked experiment from one frozen Plan.",
                      "work_packages": [
                          {"id": "literature", "title": "Novelty evidence", "track": "LITERATURE", "objective": "Fix the evidence baseline.", "input_refs": [], "depends_on": [],
                           "planned_outputs": [{"local_id": "literature-baseline", "kind": "LiteratureEvidence", "description": "Verified baseline.", "required": True, "depends_on_outputs": []}],
                           "materialized_outputs": [{"local_id": "literature-baseline", "artifact_ref": {"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]}}], "success_criteria": ["Verified evidence exists."], "status": "DONE"},
                          {"id": "theory", "title": "Core theorem", "track": "THEORY", "objective": "Prove the durable-state claim.", "input_refs": [{"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]}], "depends_on": ["literature"],
                           "planned_outputs": [{"local_id": "core-correctness", "kind": "ScientificClaim", "verification_profile": "CORE_FORMAL", "description": "Revision-pinned control preserves accepted scientific state.", "required": True, "depends_on_outputs": ["literature-baseline"]}], "materialized_outputs": [], "success_criteria": ["All four CORE_FORMAL reviews pass."], "status": "TODO"},
                          {"id": "experiment", "title": "Protocol integrity", "track": "EXPERIMENT", "objective": "Exercise locked runs and preserve raw outcomes.", "input_refs": [], "depends_on": ["theory"],
                           "planned_outputs": [{"local_id": "protocol-integrity", "kind": "Experiment", "description": "Execute two frozen runs without selective reporting.", "required": True, "depends_on_outputs": ["core-correctness"]}], "materialized_outputs": [], "success_criteria": ["All preregistered runs and verifier outcome are preserved."], "status": "TODO"}],
                      "milestones": [{"id": "mixed-complete", "description": "Both tracks satisfy their gates.", "acceptance_criteria": ["Theory and experiment packages are DONE."], "status": "PENDING"}]})
        return [self._create(action, bundle, value, "plan")], None

    def _skill_proposal_correctness_review(self, action, bundle):
        proposal = self._artifact(bundle, "ResearchProposal")
        review_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("review-"))
        review = self._review(
            action, bundle, review_id=review_id, target=proposal,
            scheme="PROPOSAL_CORRECTNESS", outcome="PASS",
            reviewer_type="PROPOSAL_CORRECTNESS",
        )
        review["summary"] = "Deterministic contract check passed; no experiment or unverified hypothesis was treated as established fact."
        return [self._create(action, bundle, review, "proposal-correctness")], None

    def _skill_proposal_revision(self, action, bundle):
        proposal = self._artifact(bundle, "ResearchProposal")
        if int(proposal.get("revision_cycle", 0)) >= 3:
            raise RuntimeValidationError("proposal revision limit of 3 has been reached")
        current_uri = proposal["current_document"]["uri"]
        current_path = self.root / current_uri
        next_cycle = int(proposal.get("revision_cycle", 0)) + 1
        revision_path = self.root / "projects" / action["project_id"] / "resources" / "proposal" / "revisions" / f"rev-{next_cycle + 1:03d}.md"
        change_path = self.root / "projects" / action["project_id"] / "resources" / "proposal" / "changes" / f"rev-{next_cycle + 1:03d}.yaml"
        text_value = current_path.read_text(encoding="utf-8")
        atomic_write_text(revision_path, text_value)
        atomic_write_text(change_path, yaml.safe_dump({"from_revision": proposal["revision"], "issues": [], "note": "Synthetic authorized revision; text unchanged."}, sort_keys=False))
        def resource(path: Path, media: str, description: str) -> dict[str, str]:
            return {"uri": str(path.relative_to(self.root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "media_type": media, "description": description}
        proposal["current_document"] = resource(revision_path, "text/markdown", f"Proposal revision {next_cycle + 1}")
        proposal.setdefault("change_log_refs", []).append(resource(change_path, "application/yaml", f"Change log for proposal revision {next_cycle + 1}"))
        proposal["revision_cycle"] = next_cycle
        proposal["status"] = "STRUCTURED"
        proposal["review_refs"] = []
        return [self._revise(action, bundle, proposal, "proposal-revision")], None

    def _skill_theory_development(self, action, bundle):
        claim = self._target(bundle); literature = self._artifact(bundle, "LiteratureEvidence")
        claim.update({"claim_type": "THEOREM", "status": "SUPPORTED",
                      "statement": "Given immutable revision pins and atomic commits, accepted scientific state is recoverable without conversation history.",
                      "assumptions": [{"id": "atomic-commit", "statement": "Canonical artifact and checkpoint writes are atomic."}],
                      "evidence": [{"relation": "SUPPORTS", "source_ref": {"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]}, "summary": "Artifact persistence supports the premise."}],
                      "verification": {"method": "NONE", "protocol": "Adversarial, Lean, axiom, and semantic checks are required.", "resource_refs": [{"uri": "https://example.invalid/proof/core.txt", "sha256": "1"*64, "media_type": "text/plain", "description": "Synthetic informal proof"}], "review_refs": [], "conclusion": "Pending independent verification."}})
        return [self._revise(action, bundle, claim, "theory")], None

    def _skill_lean_formalization(self, action, bundle):
        claim = self._target(bundle)
        claim["verification"]["protocol"] = "Pinned Lean 4 synthetic theorem with explicit assumption/conclusion mapping."
        claim["verification"]["resource_refs"].extend([
            {"uri": "https://example.invalid/lean/Core.lean", "sha256": "2"*64, "media_type": "text/plain", "description": "Lean theorem source"},
            {"uri": "https://example.invalid/lean/mapping.json", "sha256": "3"*64, "media_type": "application/json", "description": "Bidirectional semantic mapping"},
        ])
        return [self._revise(action, bundle, claim, "lean-formalization")], None

    def _skill_theory_verification(self, action, bundle):
        claim = self._target(bundle); review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=claim, scheme="THEORY", outcome="PASS", reviewer_type="THEORY")
        return [self._create(action, bundle, review, "theory-review")], None

    def _skill_lean_verification(self, action, bundle):
        claim = self._target(bundle)
        lean_dir = (
            self.root / "projects" / str(action["project_id"])
            / "resources" / "lean" / "synthetic"
        ); lean_dir.mkdir(parents=True, exist_ok=True)
        source = lean_dir / "Core.lean"; source.write_text("theorem core (p : Prop) (h : p) : p := h\n", encoding="utf-8")
        class SuccessRunner:
            def __call__(self, *args, **kwargs):
                return subprocess.CompletedProcess(args[0], 0, "build ok", "")
        report = lean_dir / f"{action['action_id']}-report.json"
        result = LeanToolAdapter(SuccessRunner()).verify(project_dir=lean_dir, source_paths=[source], lean_version="4.19.0", mathlib_commit="a"*40, report_path=report)
        if result.outcome != "PASS":
            raise RuntimeValidationError(f"synthetic Lean adapter failed: {result.outcome}")
        resource = {
            "uri": str(report.relative_to(self.root)),
            "sha256": result.report_sha256,
            "media_type": "application/json",
            "description": "Deterministic Lean build and axiom audit",
        }
        ids = bundle["allowed_outputs"]["create_ids"]
        lean_review = self._review(action, bundle, review_id=ids[0], target=claim, scheme="LEAN", outcome="PASS", reviewer_type="LEAN", resources=[resource])
        axiom_review = self._review(action, bundle, review_id=ids[1], target=claim, scheme="AXIOM_AUDIT", outcome="PASS", reviewer_type="AXIOM_AUDIT", resources=[resource])
        return [self._create(action, bundle, lean_review, "lean-review"), self._create(action, bundle, axiom_review, "axiom-review")], None

    def _skill_semantic_alignment_review(self, action, bundle):
        claim = self._target(bundle); review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=claim, scheme="SEMANTIC_ALIGNMENT", outcome="PASS", reviewer_type="SEMANTIC_ALIGNMENT")
        return [self._create(action, bundle, review, "semantic-review")], None

    def _skill_experiment_design(self, action, bundle):
        experiment = self._target(bundle); claim = self._artifact(bundle, "ScientificClaim")
        experiment.update({"status": "READY", "hypothesis": {"claim_ref": {"id": claim["id"], "kind": "ScientificClaim", "revision": claim["revision"]}, "operationalization": "Both preregistered runs are retained and their aggregate reaches the fixed threshold."},
                           "method": "Execute two deterministic protocol-integrity runs.",
                           "baselines": [{"name": "uncontrolled workflow", "version": "v1", "configuration": {"protocol_lock": False}}],
                           "datasets": [{"name": "synthetic", "version": "v1", "uri": "https://example.invalid/data.json", "sha256": "4"*64, "split_definition": "Fixed complete fixture", "license": "MIT"}],
                           "metrics": [{"name": "integrity", "definition": "Fraction of preregistered checks retained.", "direction": "HIGHER_BETTER", "aggregation": "mean"}],
                           "configuration": {"parameters": {"threshold": 0.5}, "environment": "Python 3.11 synthetic adapter"},
                           "interpretation_plan": {"supported_when": ["mean integrity >= 0.5"], "contradicted_when": ["mean integrity < 0.5"], "inconclusive_when": ["either preregistered run is unavailable"], "analysis_plan": "Average both seeds without exclusion.", "seeds": [0, 1], "repetitions": 1, "hyperparameter_budget": "No tuning", "ablations": ["no-session-history"], "statistical_procedure": "Report both values and arithmetic mean.", "exclusions": []},
                           "code_location": {"repository": str(self.root), "git_commit": bundle["input_git_commit"], "entrypoint": "python3 -c print(1)"}})
        experiment["revision"] += 1
        protocol_sha = ExperimentExecutionAdapter.protocol_hash(experiment)
        experiment["revision"] -= 1
        experiment["protocol_lock"] = {"revision": experiment["revision"] + 1, "sha256": protocol_sha, "locked_at": utc_now()}
        return [self._revise(action, bundle, experiment, "experiment-design")], None

    def _skill_experiment_execution(self, action, bundle):
        experiment = self._target(bundle); adapter = ExperimentExecutionAdapter()
        runs = []
        for seed in experiment["interpretation_plan"]["seeds"]:
            result = adapter.execute(experiment=experiment, seed=seed, repetition=0,
                                     run_root=self.root / ".harness/research/synthetic-runs")
            run = result["run"]
            run["metric_values"] = {"integrity": 1.0 if seed == 0 else -0.1}
            runs.append(run)
        experiment["runs"] = runs
        experiment["status"] = "RUNNING"
        return [self._revise(action, bundle, experiment, "experiment-execution")], None

    def _skill_experiment_verification(self, action, bundle):
        experiment = self._target(bundle); review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=experiment, scheme="EXPERIMENT", outcome="SUPPORTED", reviewer_type="EXPERIMENT")
        review["summary"] = "Both preregistered seeds, including the negative result, were preserved; the fixed mean exceeds the threshold."
        return [self._create(action, bundle, review, "experiment-review")], None

    def _skill_research_synthesis(self, action, bundle):
        target = self._artifact(bundle, "Project")
        review_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("review-"))
        review = self._review(action, bundle, review_id=review_id, target=target, scheme="CONSISTENCY", outcome="PASS", reviewer_type="CONSISTENCY")
        review["summary"] = "Synthesis preserves the negative seed and the supported aggregate without changing verified claims."
        return [self._create(action, bundle, review, "synthesis")], None

    def _skill_completion_review(self, action, bundle):
        plan = self._artifact(bundle, "ResearchPlan"); review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=plan, scheme="COMPLETION", outcome="COMPLETE", reviewer_type="COMPLETION")
        proposal = self._create(action, bundle, review, "completion")
        based = [{"id": item["id"], "kind": item["kind"], "revision": item["revision"]} for item in bundle["artifacts"] if item["kind"] in {"ResearchPlan", "ScientificClaim", "Experiment"}]
        return [proposal], self._gate(bundle, "COMPLETION", "COMPLETE", review, based)

    def _skill_consistency_review(self, action, bundle):
        project = self._artifact(bundle, "Project"); review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=project, scheme="CONSISTENCY", outcome="PASS", reviewer_type="CONSISTENCY")
        proposal = self._create(action, bundle, review, "consistency")
        based = [{"id": item["id"], "kind": item["kind"], "revision": item["revision"]} for item in bundle["artifacts"]]
        return [proposal], self._gate(bundle, "CONSISTENCY", "PASS", review, based)

    def _skill_scientific_writing(self, action, bundle):
        claim = next(item["content"] for item in bundle["artifacts"] if item["kind"] == "ScientificClaim" and item["content"]["status"] == "VERIFIED")
        paper = self.root / "projects" / action["project_id"] / "paper"
        manuscript = paper / "manuscript" / "draft.md"; manuscript.parent.mkdir(parents=True, exist_ok=True)
        manuscript.write_text("# Synthetic Research Report\n\n<a id=\"theorem-core\"></a>\nThe revision-pinned theorem is verified. The experiment retained a negative seed.\n", encoding="utf-8")
        trace = {"traceability_version": "research-traceability/v0.1", "project_id": action["project_id"], "manuscript_paths": ["paper/manuscript/draft.md"], "entries": [{"anchor": "theorem-core", "statement_type": "THEOREM", "source_refs": [{"id": claim["id"], "kind": "ScientificClaim", "revision": claim["revision"]}], "resource_refs": []}]}
        atomic_write_text(paper / "traceability.yaml", yaml.safe_dump(trace, sort_keys=False))
        return [], None

    def _skill_final_review(self, action, bundle):
        project = self._artifact(bundle, "Project"); review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=project, scheme="FINAL", outcome="PASS", reviewer_type="FINAL")
        proposal = self._create(action, bundle, review, "final")
        based = [{"id": project["id"], "kind": "Project", "revision": project["revision"]}]
        return [proposal], self._gate(bundle, "FINAL", "PASS", review, based)
