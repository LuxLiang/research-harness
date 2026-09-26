"""Deterministic Research Orchestrator / State Machine v0.1.

The controller deliberately contains no agent loop.  It accepts typed events,
reduces them deterministically, and emits commands for an existing runtime.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .resources import resource_path
from typing import Any, Mapping, Protocol

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .workspace import ArtifactError, ArtifactRecord, ArtifactWorkspace


ORCHESTRATOR_VERSION = "research-orchestrator/v0.1"

STATES = {
    "DISCOVERY_QUESTION",
    "DISCOVERY_LITERATURE",
    "DISCOVERY_FEASIBILITY_GATE",
    "PLANNING_DRAFT",
    "PLANNING_APPROVAL",
    "PLANNING_ROUTING",
    "EXECUTION_MATERIALIZE",
    "EXECUTION_TRACKS",
    "EXECUTION_JOIN",
    "SYNTHESIS_BUILD",
    "SYNTHESIS_COMPLETION_GATE",
    "FOLLOWUP_TARGETED",
    "FOLLOWUP_CLAIM_RETHINK",
    "CONSISTENCY_GATE",
    "WRITING_DRAFT",
    "WRITING_FINAL_REVIEW",
    "WRITING_TARGETED_REVISION",
    "WRITING_FINAL_APPROVAL",
    "PROPOSAL_STRUCTURING",
    "PROPOSAL_NOVELTY_REVIEW",
    "PROPOSAL_PLAN_RECONSTRUCTION",
    "PROPOSAL_CORRECTNESS_REVIEW",
    "PROPOSAL_REVIEW_GATE",
    "PROPOSAL_WAITING_DECISIONS",
    "PROPOSAL_REVISION",
    "PROPOSAL_FINAL_APPROVAL",
    "DONE",
    "BLOCKED",
    "PAUSED_BY_USER",
    "CANCELLED_BY_USER",
}

TERMINAL_STATES = {"DONE", "CANCELLED_BY_USER"}
RETHINK_TARGETS = {
    "RETHINK_CLAIM": "FOLLOWUP_CLAIM_RETHINK",
    "RETHINK_PLAN": "PLANNING_DRAFT",
    "RETHINK_QUESTION": "DISCOVERY_QUESTION",
}
DEFAULT_RETRY_LIMITS = {
    "action": 2,
    "claim_rethink": 2,
    "plan_rethink": 2,
    "question_reframe": 2,
}


class OrchestratorError(ArtifactError):
    """Base error for deterministic orchestration."""


class InvalidEvent(OrchestratorError):
    """Raised when an event is invalid for the current checkpoint."""


class CheckpointConflict(OrchestratorError):
    """Raised when a checkpoint sequence changed concurrently."""


class OrchestratorValidationError(OrchestratorError):
    """Raised when a gate or checkpoint violates its control schema."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__("\n".join(issues))


@dataclass(frozen=True)
class OrchestratorEvent:
    """One typed input to the pure reducer."""

    type: str
    at: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeCommand:
    """A side effect request for the host harness."""

    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Reduction:
    """Pure reducer result."""

    checkpoint: dict[str, Any]
    commands: tuple[RuntimeCommand, ...]


class RuntimeAdapter(Protocol):
    """Minimal boundary implemented by an existing Codex-style runtime."""

    def dispatch(
        self, command: RuntimeCommand, checkpoint: Mapping[str, Any]
    ) -> None: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_checkpoint(
    project_id: str,
    run_id: str,
    git_commit: str,
    *,
    at: str | None = None,
    workflow_mode: str = "FULL_RESEARCH",
) -> dict[str, Any]:
    """Create the initial persisted controller state."""

    timestamp = at or utc_now()
    return {
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "checkpoint_seq": 0,
        "status": "RUNNING",
        "state": "PROPOSAL_STRUCTURING" if workflow_mode == "PROPOSAL_REVIEW" else "DISCOVERY_QUESTION",
        "workflow_mode": workflow_mode,
        "proposal_revision_cycles": 0,
        "resume_state": None,
        "route": None,
        "active_plan": None,
        "frozen_snapshot": {"git_commit": git_commit, "artifacts": []},
        "branch_states": {
            "theory": "NOT_SELECTED",
            "experiment": "NOT_SELECTED",
        },
        "skill_progress": {},
        "gate_results": [],
        "retry_counters": {},
        "pending_action": None,
        "last_completed_action": None,
        "blocker": None,
        "pause": None,
        "cancellation": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


class StateReducer:
    """Pure, deterministic state reducer."""

    def __init__(self, retry_limits: Mapping[str, int] | None = None):
        self.retry_limits = {**DEFAULT_RETRY_LIMITS, **(retry_limits or {})}

    def reduce(
        self, checkpoint: Mapping[str, Any], event: OrchestratorEvent
    ) -> Reduction:
        current = copy.deepcopy(dict(checkpoint))
        state = str(current["state"])
        status = str(current["status"])
        commands: list[RuntimeCommand] = []

        if event.type == "USER_PAUSE":
            self._require(status == "RUNNING" and state not in TERMINAL_STATES, event, state)
            current["resume_state"] = state
            current["state"] = "PAUSED_BY_USER"
            current["status"] = "PAUSED_BY_USER"
            current["pause"] = self._metadata(event)
            commands.append(RuntimeCommand("UPDATE_PROJECT_STATUS", {"status": "PAUSED"}))
            return self._finish(current, event, commands)

        if event.type == "USER_CANCEL":
            self._require(state not in TERMINAL_STATES, event, state)
            current["resume_state"] = None
            current["state"] = "CANCELLED_BY_USER"
            current["status"] = "CANCELLED_BY_USER"
            current["cancellation"] = self._metadata(event)
            commands.extend(
                [
                    RuntimeCommand("CREATE_CANCELLATION_DECISION", dict(event.payload)),
                    RuntimeCommand("UPDATE_PROJECT_STATUS", {"status": "ARCHIVED"}),
                ]
            )
            return self._finish(current, event, commands)

        if event.type == "RESUME":
            self._require(state == "PAUSED_BY_USER", event, state)
            current["state"] = current["resume_state"]
            current["resume_state"] = None
            current["status"] = "RUNNING"
            current["pause"] = None
            commands.append(RuntimeCommand("UPDATE_PROJECT_STATUS", {"status": "ACTIVE"}))
            commands.extend(self._entry_commands(str(current["state"])))
            return self._finish(current, event, commands)

        if event.type == "UNBLOCK":
            self._require(state == "BLOCKED", event, state)
            blocker_reason = str((current.get("blocker") or {}).get("reason", ""))
            rethink_target = RETHINK_TARGETS.get(blocker_reason)
            current["state"] = rethink_target or current["resume_state"]
            current["resume_state"] = None
            current["status"] = "RUNNING"
            current["blocker"] = None
            # Recovery is a new-authority boundary. Retaining an exhausted
            # transient action budget would immediately re-block repaired work.
            current["retry_counters"].pop("action", None)
            if blocker_reason in {"RETHINK_PLAN", "RETHINK_QUESTION"}:
                current["active_plan"] = None
                current["route"] = None
                current["branch_states"] = {
                    "theory": "NOT_SELECTED",
                    "experiment": "NOT_SELECTED",
                }
                current["skill_progress"] = {}
            commands.append(RuntimeCommand("UPDATE_PROJECT_STATUS", {"status": "ACTIVE"}))
            commands.extend(self._entry_commands(str(current["state"])))
            return self._finish(current, event, commands)

        if event.type == "ACTION_FAILED":
            self._require(status == "RUNNING" and state not in TERMINAL_STATES, event, state)
            reason = str(event.payload.get("reason", ""))
            rethink_target = RETHINK_TARGETS.get(reason)
            if rethink_target is not None:
                current["state"] = rethink_target
                current["retry_counters"].pop("action", None)
                # A plan/question rethink is a scope reset, not a recoverable
                # retry of the same compiled action.  Keeping the old plan or
                # its materialized skill progress would immediately select the
                # same invalid protocol again.
                if reason in {"RETHINK_PLAN", "RETHINK_QUESTION"}:
                    current["active_plan"] = None
                    current["route"] = None
                    current["branch_states"] = {
                        "theory": "NOT_SELECTED",
                        "experiment": "NOT_SELECTED",
                    }
                    current["skill_progress"] = {}
                commands.extend(self._entry_commands(rethink_target))
                return self._finish(current, event, commands)
            retry_key = str(event.payload.get("retry_key", "action"))
            count = int(current["retry_counters"].get(retry_key, 0)) + 1
            current["retry_counters"][retry_key] = count
            limit = int(self.retry_limits.get(retry_key, self.retry_limits["action"]))
            retryable = bool(event.payload.get("retryable", True))
            if retryable and count <= limit:
                commands.append(
                    RuntimeCommand(
                        "RETRY_ACTION", {"retry_key": retry_key, "attempt": count}
                    )
                )
            else:
                current["resume_state"] = state
                current["state"] = "BLOCKED"
                current["status"] = "BLOCKED"
                current["blocker"] = self._metadata(event, retry_key=retry_key)
                commands.append(
                    RuntimeCommand("UPDATE_PROJECT_STATUS", {"status": "BLOCKED"})
                )
            return self._finish(current, event, commands)

        if event.type == "ACTION_CONFLICT":
            self._require(status == "RUNNING" and state not in TERMINAL_STATES, event, state)
            commands.append(
                RuntimeCommand(
                    "RETRY_ACTION",
                    {
                        "retry_key": "conflict",
                        "attempt": 0,
                        "reason": str(event.payload.get("reason", "stale input snapshot")),
                    },
                )
            )
            # _finish clears the stale pending action without consuming a
            # scientific retry or changing the research state.
            return self._finish(current, event, commands)

        self._require(status == "RUNNING", event, state)

        direct: dict[tuple[str, str], str] = {
            ("DISCOVERY_QUESTION", "QUESTION_ACTIVATED"): "DISCOVERY_LITERATURE",
            ("DISCOVERY_LITERATURE", "LITERATURE_READY"): "DISCOVERY_FEASIBILITY_GATE",
            ("PLANNING_DRAFT", "PLAN_VALIDATED"): "PLANNING_APPROVAL",
            ("PLANNING_APPROVAL", "PLAN_APPROVED"): "PLANNING_ROUTING",
            ("PLANNING_APPROVAL", "PLAN_REVISION_REQUIRED"): "PLANNING_DRAFT",
            ("PLANNING_ROUTING", "ROUTE_SELECTED"): "EXECUTION_MATERIALIZE",
            ("EXECUTION_MATERIALIZE", "OUTPUTS_MATERIALIZED"): "EXECUTION_TRACKS",
            ("EXECUTION_TRACKS", "TRACKS_JOINED"): "EXECUTION_JOIN",
            ("EXECUTION_JOIN", "JOIN_COMPLETED"): "SYNTHESIS_BUILD",
            ("SYNTHESIS_BUILD", "SYNTHESIS_COMPLETED"): "SYNTHESIS_COMPLETION_GATE",
            ("FOLLOWUP_TARGETED", "FOLLOWUP_PLANNED"): "EXECUTION_MATERIALIZE",
            ("EXECUTION_MATERIALIZE", "FOLLOWUP_PLAN_SELECTED"): "EXECUTION_MATERIALIZE",
            ("FOLLOWUP_CLAIM_RETHINK", "CLAIM_REPLACED"): "EXECUTION_MATERIALIZE",
            ("WRITING_DRAFT", "WRITING_COMPLETED"): "WRITING_FINAL_REVIEW",
            ("WRITING_TARGETED_REVISION", "REVISION_COMPLETED"): "WRITING_FINAL_REVIEW",
            ("WRITING_FINAL_APPROVAL", "FINAL_APPROVED"): "DONE",
            ("WRITING_FINAL_APPROVAL", "FINAL_REVISION_REQUIRED"): "WRITING_TARGETED_REVISION",
            ("PROPOSAL_STRUCTURING", "PROPOSAL_STRUCTURED"): "PROPOSAL_NOVELTY_REVIEW",
            ("PROPOSAL_NOVELTY_REVIEW", "PROPOSAL_STRUCTURING_REQUIRED"): "PROPOSAL_STRUCTURING",
            ("PROPOSAL_NOVELTY_REVIEW", "PROPOSAL_NOVELTY_REVIEWED"): "PROPOSAL_PLAN_RECONSTRUCTION",
            ("PROPOSAL_PLAN_RECONSTRUCTION", "PROPOSAL_PLAN_VALIDATED"): "PROPOSAL_CORRECTNESS_REVIEW",
            ("PROPOSAL_CORRECTNESS_REVIEW", "PROPOSAL_CORRECTNESS_REVIEWED"): "PROPOSAL_REVIEW_GATE",
            ("PROPOSAL_WAITING_DECISIONS", "PROPOSAL_DECISIONS_RECORDED"): "PROPOSAL_REVISION",
            ("PROPOSAL_REVISION", "PROPOSAL_REVISION_COMPLETED"): "PROPOSAL_STRUCTURING",
            ("PROPOSAL_FINAL_APPROVAL", "PROPOSAL_FINAL_APPROVED"): "DONE",
        }

        if event.type == "GATE_RECORDED":
            self._apply_gate(current, event)
            if (
                state == "PROPOSAL_REVIEW_GATE"
                and current["state"] in {"PROPOSAL_WAITING_DECISIONS", "PROPOSAL_REVISION"}
                and int(current.get("proposal_revision_cycles", 0)) >= 3
            ):
                current["resume_state"] = "PROPOSAL_REVIEW_GATE"
                current["state"] = "BLOCKED"
                current["status"] = "BLOCKED"
                current["blocker"] = {
                    "reason": "proposal remains non-passing after 3 revision cycles",
                    "recorded_at": event.at,
                    "recovery_condition": "Start a new review project or materially revise the source proposal under explicit human authority.",
                }
                commands.append(
                    RuntimeCommand("UPDATE_PROJECT_STATUS", {"status": "BLOCKED"})
                )
            if state == "CONSISTENCY_GATE" and event.payload["gate"]["verdict"] == "PASS":
                commands.append(RuntimeCommand("PROMOTE_EMPIRICAL_CLAIMS", {}))
        elif event.type == "SKILL_COMPLETED":
            command = self._apply_skill_completed(current, event)
            if command is not None:
                commands.append(command)
        elif event.type == "SKILL_REVISION_REQUIRED":
            self._apply_skill_revision_required(current, event)
        elif event.type == "TRACK_ADVANCED":
            self._apply_track(current, event)
            if event.payload.get("status") == "COMPLETED":
                selected = [
                    status for status in current["branch_states"].values()
                    if status != "NOT_SELECTED"
                ]
                # Keep the active Plan revision frozen while semi-independent
                # branches execute.  Finalize all selected packages once at
                # the synchronization boundary, never after the first MIXED
                # branch completes.
                if selected and all(
                    status in {"COMPLETED", "WAIVED"} for status in selected
                ):
                    commands.append(
                        RuntimeCommand(
                            "FINALIZE_TRACK_WORK_PACKAGES", {"track": "all"}
                        )
                    )
        elif (state, event.type) in direct:
            target = direct[(state, event.type)]
            if event.type == "PROPOSAL_REVISION_COMPLETED":
                current["proposal_revision_cycles"] = int(current.get("proposal_revision_cycles", 0)) + 1
            if event.type == "PLAN_APPROVED":
                plan_ref = event.payload.get("plan_ref")
                self._require(isinstance(plan_ref, dict), event, state)
                current["active_plan"] = copy.deepcopy(plan_ref)
                commands.append(
                    RuntimeCommand("CREATE_PLAN_APPROVAL_DECISION", {"plan_ref": plan_ref})
                )
                commands.append(
                    RuntimeCommand("UPDATE_PROJECT_ACTIVE_PLAN", {"plan_ref": plan_ref})
                )
            elif event.type == "PLAN_REVISION_REQUIRED":
                # Human/scientific plan feedback must survive the transition.
                # Persist it as a Decision so the next planner ContextBundle
                # receives the exact rejection rationale rather than only the
                # fact that PLANNING_DRAFT was re-entered.
                commands.append(
                    RuntimeCommand(
                        "CREATE_PLAN_REVISION_DECISION", dict(event.payload)
                    )
                )
            elif event.type in {"FOLLOWUP_PLANNED", "FOLLOWUP_PLAN_SELECTED"}:
                plan_ref = event.payload.get("plan_ref")
                self._require(isinstance(plan_ref, dict), event, state)
                current["active_plan"] = copy.deepcopy(plan_ref)
                commands.append(
                    RuntimeCommand("UPDATE_PROJECT_ACTIVE_PLAN", {"plan_ref": plan_ref})
                )
            elif event.type in {"QUESTION_ACTIVATED", "PROPOSAL_STRUCTURED"}:
                artifact_ids = event.payload.get("artifact_ids", [])
                self._require(isinstance(artifact_ids, list), event, state)
                if artifact_ids:
                    commands.append(
                        RuntimeCommand("UPDATE_PROJECT_QUESTION", {"artifact_ids": artifact_ids})
                    )
            elif event.type == "ROUTE_SELECTED":
                route = event.payload.get("route")
                self._require(route in {"THEORY", "EXPERIMENT", "MIXED"}, event, state)
                current["route"] = route
                snapshot = event.payload.get("snapshot")
                self._require(isinstance(snapshot, dict), event, state)
                current["frozen_snapshot"] = copy.deepcopy(snapshot)
            elif event.type == "OUTPUTS_MATERIALIZED":
                plan_ref = event.payload.get("plan_ref")
                if isinstance(plan_ref, dict):
                    current["active_plan"] = copy.deepcopy(plan_ref)
                    commands.append(
                        RuntimeCommand("UPDATE_PROJECT_ACTIVE_PLAN", {"plan_ref": plan_ref})
                    )
                self._select_branches(current)
                progress = event.payload.get("skill_progress")
                self._require(isinstance(progress, dict) and bool(progress), event, state)
                current["skill_progress"] = copy.deepcopy(progress)
            elif event.type == "TRACKS_JOINED":
                self._require(self.can_join(current), event, state)
            elif event.type == "WRITING_COMPLETED":
                commands.append(RuntimeCommand("APPLY_PAPER_TRACEABILITY", {}))
            elif event.type == "PROPOSAL_FINAL_APPROVED":
                commands.append(RuntimeCommand("PROMOTE_PROPOSAL_APPROVED", {}))
            current["state"] = target
        else:
            raise InvalidEvent(f"{event.type} is invalid in {state}")

        # Retry budgets belong to one action, not the entire workflow.  Any
        # accepted action-scoped event proves that action completed and resets
        # the local transient-failure counter before the next action starts.
        if (
            event.type != "ACTION_FAILED"
            and (
                event.payload.get("action_id")
                or (
                    current["state"] != state
                    and event.type not in {"USER_PAUSE", "RESUME"}
                )
            )
        ):
            current["retry_counters"].pop("action", None)

        if current["state"] == "DONE":
            current["status"] = "DONE"
            commands.extend(
                [
                    RuntimeCommand("FINALIZE_DELIVERABLES", {}),
                    RuntimeCommand("UPDATE_PROJECT_STAGE", {"stage": "COMPLETE"}),
                    RuntimeCommand("UPDATE_PROJECT_STATUS", {"status": "COMPLETED"}),
                    RuntimeCommand("CREATE_FINAL_APPROVAL_DECISION", dict(event.payload)),
                ]
            )
        elif current["state"] != state:
            commands.extend(self._entry_commands(str(current["state"])))
            if event.type == "GATE_RECORDED" and event.payload["gate"]["verdict"] in {
                "RETHINK_PLAN",
                "RETHINK_QUESTION",
            }:
                commands.append(
                    RuntimeCommand(
                        "CREATE_RETHINK_DECISION",
                        {
                            "verdict": event.payload["gate"]["verdict"],
                            "gate_id": event.payload["gate"]["gate_id"],
                        },
                    )
                )
        return self._finish(current, event, commands)

    def _apply_gate(self, current: dict[str, Any], event: OrchestratorEvent) -> None:
        state = str(current["state"])
        gate = event.payload.get("gate")
        self._require(isinstance(gate, dict), event, state)
        expected_gate_type = {
            "DISCOVERY_FEASIBILITY_GATE": "FEASIBILITY",
            "SYNTHESIS_COMPLETION_GATE": "COMPLETION",
            "CONSISTENCY_GATE": "CONSISTENCY",
            "WRITING_FINAL_REVIEW": "FINAL",
            "WRITING_TARGETED_REVISION": "FINAL",
            "PROPOSAL_REVIEW_GATE": "PROPOSAL_REVIEW",
        }.get(state)
        self._require(gate.get("gate_type") == expected_gate_type, event, state)
        verdict = gate.get("verdict")
        allowed: dict[str, dict[str, str]] = {
            "DISCOVERY_FEASIBILITY_GATE": {
                "PASS": "PLANNING_DRAFT",
                "RETHINK_QUESTION": "DISCOVERY_QUESTION",
            },
            "SYNTHESIS_COMPLETION_GATE": {
                "COMPLETE": "CONSISTENCY_GATE",
                "INCOMPLETE": "FOLLOWUP_TARGETED",
                **RETHINK_TARGETS,
            },
            "CONSISTENCY_GATE": {
                "PASS": "WRITING_DRAFT",
                "INCOMPLETE": "FOLLOWUP_TARGETED",
                **RETHINK_TARGETS,
            },
            "WRITING_FINAL_REVIEW": {
                "PASS": "WRITING_FINAL_APPROVAL",
                "REVISION_REQUIRED": "WRITING_TARGETED_REVISION",
                **RETHINK_TARGETS,
            },
            "WRITING_TARGETED_REVISION": dict(RETHINK_TARGETS),
            "PROPOSAL_REVIEW_GATE": {
                "PASS": "PROPOSAL_FINAL_APPROVAL",
                "AUTO_REVISION": "PROPOSAL_REVISION",
                "REVISION_REQUIRED": "PROPOSAL_WAITING_DECISIONS",
            },
        }
        target = allowed.get(state, {}).get(str(verdict))
        self._require(target is not None, event, state)
        current["gate_results"].append(copy.deepcopy(gate))
        current["state"] = target
        if verdict == "RETHINK_PLAN":
            current["active_plan"] = None
            current["route"] = None
            current["branch_states"] = {
                "theory": "NOT_SELECTED",
                "experiment": "NOT_SELECTED",
            }
            current["skill_progress"] = {}
        elif verdict == "RETHINK_QUESTION":
            current["active_plan"] = None
            current["route"] = None
            current["branch_states"] = {
                "theory": "NOT_SELECTED",
                "experiment": "NOT_SELECTED",
            }
            current["skill_progress"] = {}

    def _apply_track(self, current: dict[str, Any], event: OrchestratorEvent) -> None:
        state = str(current["state"])
        self._require(state == "EXECUTION_TRACKS", event, state)
        track = event.payload.get("track")
        target = event.payload.get("status")
        self._require(track in {"theory", "experiment"}, event, state)
        progressions = {
            "theory": {
                "PENDING": {"DEVELOP", "FAILED", "WAIVED"},
                "DEVELOP": {"VERIFY", "FAILED"},
                "VERIFY": {"COMPLETED", "FAILED"},
                "FAILED": {"DEVELOP", "WAIVED"},
            },
            "experiment": {
                "PENDING": {"PREPARE", "FAILED", "WAIVED"},
                "PREPARE": {"RUN", "FAILED"},
                "RUN": {"ASSESS", "FAILED"},
                "ASSESS": {"COMPLETED", "FAILED"},
                "FAILED": {"PREPARE", "WAIVED"},
            },
        }
        current_status = current["branch_states"][track]
        self._require(target in progressions[track].get(current_status, set()), event, state)
        phase_requirements = {
            ("theory", "DEVELOP", "VERIFY"): {"theory-development"},
            ("theory", "VERIFY", "COMPLETED"): {
                "theory-verification", "lean-formalization",
                "lean-verification", "semantic-alignment-review",
            },
            ("experiment", "PREPARE", "RUN"): {"experiment-design"},
            ("experiment", "RUN", "ASSESS"): {"experiment-execution"},
            ("experiment", "ASSESS", "COMPLETED"): {"experiment-verification"},
        }.get((track, current_status, target))
        if phase_requirements is not None:
            relevant = [
                progress
                for progress in current.get("skill_progress", {}).values()
                if progress.get("track") == track
                and any(
                    skill in phase_requirements
                    for skill in progress.get("required_skills", [])
                )
            ]
            self._require(bool(relevant), event, state)
            self._require(
                all(
                    all(
                        skill in progress.get("completed_skills", [])
                        for skill in progress.get("required_skills", [])
                        if skill in phase_requirements
                    )
                    for progress in relevant
                ),
                event,
                state,
            )
        if target == "WAIVED":
            decision_ref = event.payload.get("decision_ref")
            self._require(
                isinstance(decision_ref, dict)
                and decision_ref.get("kind") == "Decision"
                and isinstance(decision_ref.get("revision"), int),
                event,
                state,
            )
        current["branch_states"][track] = target

    def _apply_skill_completed(
        self, current: dict[str, Any], event: OrchestratorEvent
    ) -> RuntimeCommand | None:
        state = str(current["state"])
        self._require(state == "EXECUTION_TRACKS", event, state)
        output_id = event.payload.get("output_id")
        skill_id = event.payload.get("skill_id")
        progress = current.get("skill_progress", {}).get(output_id)
        self._require(isinstance(progress, dict), event, state)
        if skill_id == "theory-verification":
            verified_ref = event.payload.get("verified_target_ref")
            if isinstance(verified_ref, dict):
                self._require(
                    verified_ref.get("kind") == "ScientificClaim"
                    and isinstance(verified_ref.get("id"), str)
                    and isinstance(verified_ref.get("revision"), int),
                    event,
                    state,
                )
                # A failed theorem may be replaced by a new immutable Claim.
                # The independent reviewer targets that replacement, so the
                # output pipeline must promote the reviewed Claim rather than
                # the rejected predecessor retained in the original Plan.
                progress["artifact_ref"] = copy.deepcopy(verified_ref)
        required = progress.get("required_skills", [])
        completed = progress.get("completed_skills", [])
        expected = next((item for item in required if item not in completed), None)
        self._require(skill_id == expected, event, state)
        progress["completed_skills"] = [*completed, skill_id]
        progress["active_skill"] = None
        progress["status"] = (
            "COMPLETED"
            if len(progress["completed_skills"]) == len(required)
            else "IN_PROGRESS"
        )
        if (
            progress["status"] == "COMPLETED"
            and progress.get("verification_profile") in {
                "ADVERSARIAL", "CORE_FORMAL"
            }
        ):
            self._require(
                isinstance(progress.get("artifact_ref"), dict)
                and progress["artifact_ref"].get("kind") == "ScientificClaim",
                event,
                state,
            )
            return RuntimeCommand(
                "PROMOTE_VERIFIED_CLAIM",
                {"artifact_ref": copy.deepcopy(progress.get("artifact_ref"))},
            )
        if (
            progress["status"] == "COMPLETED"
            and progress.get("track") == "experiment"
        ):
            self._require(
                isinstance(progress.get("artifact_ref"), dict)
                and progress["artifact_ref"].get("kind") == "Experiment",
                event,
                state,
            )
            return RuntimeCommand(
                "FINALIZE_EXPERIMENT",
                {"artifact_ref": copy.deepcopy(progress.get("artifact_ref"))},
            )
        return None

    def _apply_skill_revision_required(
        self, current: dict[str, Any], event: OrchestratorEvent
    ) -> None:
        """Record a valid negative verification result and reopen development.

        Completing the reviewer action is not the same as verifying its target.
        A proof gap, counterexample, or inconclusive audit must remain canonical
        evidence while the affected output returns to a fresh developer session.
        """

        state = str(current["state"])
        self._require(state == "EXECUTION_TRACKS", event, state)
        output_id = event.payload.get("output_id")
        skill_id = event.payload.get("skill_id")
        progress = current.get("skill_progress", {}).get(output_id)
        self._require(isinstance(progress, dict), event, state)
        self._require(skill_id == "theory-verification", event, state)
        self._require(progress.get("track") == "theory", event, state)
        self._require(
            "theory-development" in progress.get("required_skills", [])
            and "theory-verification" in progress.get("required_skills", []),
            event,
            state,
        )
        completed = progress.get("completed_skills", [])
        self._require("theory-development" in completed, event, state)
        self._require("theory-verification" not in completed, event, state)
        self._require(current["branch_states"].get("theory") == "VERIFY", event, state)

        # The next action must be a new theory-development session followed by
        # another independent verifier.  Other outputs keep their completed
        # development work and will not be repeated.
        progress["completed_skills"] = []
        progress["active_skill"] = None
        progress["status"] = "IN_PROGRESS"
        current["branch_states"]["theory"] = "DEVELOP"
        retry_key = f"theory_revision:{output_id}"
        current["retry_counters"][retry_key] = (
            int(current["retry_counters"].get(retry_key, 0)) + 1
        )

    @staticmethod
    def can_join(checkpoint: Mapping[str, Any]) -> bool:
        selected = [
            status
            for status in checkpoint["branch_states"].values()
            if status != "NOT_SELECTED"
        ]
        return bool(selected) and all(status in {"COMPLETED", "WAIVED"} for status in selected)

    @staticmethod
    def _select_branches(current: dict[str, Any]) -> None:
        route = current.get("route")
        current["branch_states"] = {
            "theory": "PENDING" if route in {"THEORY", "MIXED"} else "NOT_SELECTED",
            "experiment": "PENDING"
            if route in {"EXPERIMENT", "MIXED"}
            else "NOT_SELECTED",
        }

    @staticmethod
    def _metadata(
        event: OrchestratorEvent, *, retry_key: str | None = None
    ) -> dict[str, Any]:
        result = {
            "reason": str(event.payload.get("reason", event.type)),
            "recorded_at": event.at,
        }
        if retry_key:
            result["retry_key"] = retry_key
        recovery = event.payload.get("recovery_condition")
        if recovery:
            result["recovery_condition"] = str(recovery)
        return result

    @staticmethod
    def _require(condition: bool, event: OrchestratorEvent, state: str) -> None:
        if not condition:
            raise InvalidEvent(f"invalid {event.type} payload or transition in {state}")

    @staticmethod
    def _entry_commands(state: str) -> tuple[RuntimeCommand, ...]:
        responsibility = {
            "DISCOVERY_QUESTION": "question-agent",
            "DISCOVERY_LITERATURE": "literature-agent",
            "DISCOVERY_FEASIBILITY_GATE": "gate-evaluator",
            "PLANNING_DRAFT": "planner",
            "PLANNING_APPROVAL": "human-checkpoint",
            "PLANNING_ROUTING": "deterministic-router",
            "EXECUTION_MATERIALIZE": "runtime-adapter",
            "EXECUTION_TRACKS": "track-agents",
            "EXECUTION_JOIN": "track-synchronizer",
            "SYNTHESIS_BUILD": "synthesis-agent",
            "SYNTHESIS_COMPLETION_GATE": "completion-evaluator",
            "FOLLOWUP_TARGETED": "planner",
            "FOLLOWUP_CLAIM_RETHINK": "theory-agent",
            "CONSISTENCY_GATE": "consistency-evaluator",
            "WRITING_DRAFT": "writer",
            "WRITING_FINAL_REVIEW": "reviewers",
            "WRITING_TARGETED_REVISION": "writer-or-track-agent",
            "WRITING_FINAL_APPROVAL": "human-checkpoint",
            "PROPOSAL_STRUCTURING": "question-framer",
            "PROPOSAL_NOVELTY_REVIEW": "literature-reviewer",
            "PROPOSAL_PLAN_RECONSTRUCTION": "planner",
            "PROPOSAL_CORRECTNESS_REVIEW": "proposal-correctness-reviewer",
            "PROPOSAL_REVIEW_GATE": "controller",
            "PROPOSAL_WAITING_DECISIONS": "human-checkpoint",
            "PROPOSAL_REVISION": "proposal-reviser",
            "PROPOSAL_FINAL_APPROVAL": "human-checkpoint",
        }.get(state, "controller")
        commands = [
            RuntimeCommand(
                "ENTER_STATE", {"state": state, "responsible": responsibility}
            )
        ]
        project_stage = {
            "DISCOVERY_QUESTION": "QUESTION",
            "DISCOVERY_LITERATURE": "LITERATURE",
            "DISCOVERY_FEASIBILITY_GATE": "LITERATURE",
            "PLANNING_DRAFT": "PLANNING",
            "PLANNING_APPROVAL": "PLANNING",
            "PLANNING_ROUTING": "PLANNING",
            "EXECUTION_MATERIALIZE": "THEORY_EXPERIMENT",
            "EXECUTION_TRACKS": "THEORY_EXPERIMENT",
            "EXECUTION_JOIN": "THEORY_EXPERIMENT",
            "SYNTHESIS_BUILD": "REVIEW",
            "SYNTHESIS_COMPLETION_GATE": "REVIEW",
            "FOLLOWUP_TARGETED": "REVIEW",
            "FOLLOWUP_CLAIM_RETHINK": "THEORY_EXPERIMENT",
            "CONSISTENCY_GATE": "REVIEW",
            "WRITING_DRAFT": "WRITING",
            "WRITING_FINAL_REVIEW": "WRITING",
            "WRITING_TARGETED_REVISION": "WRITING",
            "WRITING_FINAL_APPROVAL": "WRITING",
            "PROPOSAL_STRUCTURING": "QUESTION",
            "PROPOSAL_NOVELTY_REVIEW": "LITERATURE",
            "PROPOSAL_PLAN_RECONSTRUCTION": "PLANNING",
            "PROPOSAL_CORRECTNESS_REVIEW": "REVIEW",
            "PROPOSAL_REVIEW_GATE": "REVIEW",
            "PROPOSAL_WAITING_DECISIONS": "REVIEW",
            "PROPOSAL_REVISION": "WRITING",
            "PROPOSAL_FINAL_APPROVAL": "WRITING",
        }.get(state)
        if project_stage:
            commands.append(
                RuntimeCommand("UPDATE_PROJECT_STAGE", {"stage": project_stage})
            )
        return tuple(commands)

    @staticmethod
    def _finish(
        current: dict[str, Any],
        event: OrchestratorEvent,
        commands: list[RuntimeCommand],
    ) -> Reduction:
        current["checkpoint_seq"] = int(current["checkpoint_seq"]) + 1
        current["updated_at"] = event.at
        action_id = event.payload.get("action_id")
        if action_id:
            current["last_completed_action"] = str(action_id)
            current["pending_action"] = None
        return Reduction(current, tuple(commands))


class _ControlSchemas:
    def __init__(self, root: Path):
        schema_dir = resource_path(root, "schemas/orchestrator/v0.1")
        schemas: dict[str, dict[str, Any]] = {}
        for name in ("gate.schema.json", "state.schema.json"):
            path = schema_dir / name
            if not path.is_file():
                raise OrchestratorError(f"control schema not found: {path}")
            schemas[name] = json.loads(path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schemas[name])
        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema))
            for schema in schemas.values()
        )
        checker = FormatChecker()
        self.gate = Draft202012Validator(
            schemas["gate.schema.json"], registry=registry, format_checker=checker
        )
        self.state = Draft202012Validator(
            schemas["state.schema.json"], registry=registry, format_checker=checker
        )


def _schema_issues(validator: Draft202012Validator, value: Any) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
        for error in sorted(
            validator.iter_errors(value),
            key=lambda error: (list(error.absolute_path), error.message),
        )
    ]


class GateEvaluator:
    """Validate machine-readable gate results against artifact state."""

    def __init__(self, workspace: ArtifactWorkspace):
        self.workspace = workspace
        self.schemas = _ControlSchemas(workspace.root)

    def require_valid(
        self,
        gate: Mapping[str, Any],
        active_plan: Mapping[str, Any] | None,
        artifact_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        issues = _schema_issues(self.schemas.gate, gate)
        index = self.workspace.index()
        # Gate Reviews are created atomically with the GATE_RECORDED event.  Use
        # the selected staged candidates as an explicit overlay so validation
        # never depends on a second ArtifactWorkspace instance observing a
        # transient file between promotion and reduction.
        for artifact_id, candidate in (artifact_overrides or {}).items():
            kind = str(candidate.get("kind", ""))
            project_id = candidate.get("project_id")
            path = self.workspace.artifact_path(
                kind=kind,
                artifact_id=artifact_id,
                project_id=None if kind == "Project" else str(project_id),
            )
            index[artifact_id] = ArtifactRecord(path, copy.deepcopy(dict(candidate)))
        project_id: str | None = None
        for collection in ("based_on", "target_refs", "review_refs"):
            for ref in gate.get(collection, []):
                record = index.get(ref.get("id"))
                if record is None:
                    issues.append(f"{collection}: missing artifact {ref.get('id')}")
                    continue
                if record.kind != ref.get("kind"):
                    issues.append(
                        f"{collection}: {record.id} is {record.kind}, not {ref.get('kind')}"
                    )
                member_project = (
                    record.id if record.kind == "Project" else record.data.get("project_id")
                )
                project_id = project_id or member_project
                if member_project != project_id:
                    issues.append(f"{collection}: cross-project gate reference")
                if ref.get("revision", 0) > record.data.get("revision", 0):
                    issues.append(f"{collection}: future revision for {record.id}")

        known_output_ids = {
            output["local_id"]
            for package in (active_plan or {}).get("work_packages", [])
            for output in package.get("planned_outputs", [])
        }
        unknown = sorted(set(gate.get("target_output_ids", [])) - known_output_ids)
        if unknown:
            issues.append(f"target_output_ids: unknown planned outputs: {', '.join(unknown)}")
        if gate.get("verdict") == "INCOMPLETE" and not (
            gate.get("target_refs") or gate.get("target_output_ids")
        ):
            issues.append("INCOMPLETE must identify a target artifact or planned output")

        review_records = [
            index[ref["id"]]
            for ref in gate.get("review_refs", [])
            if ref.get("id") in index
        ]
        expected_scheme = {
            "FEASIBILITY": "NOVELTY",
            "COMPLETION": "COMPLETION",
            "CONSISTENCY": "CONSISTENCY",
            "FINAL": "FINAL",
        }.get(gate.get("gate_type"))
        scheme_reviews = [
            review
            for review in review_records
            if review.data.get("assessment", {}).get("scheme") == expected_scheme
        ]
        if expected_scheme and not scheme_reviews:
            issues.append(
                f"{gate.get('gate_type')} gate requires a revision-pinned {expected_scheme} Review"
            )
        verdict_map = {
            "FEASIBILITY": {
                "OPEN": "PASS",
                "PARTIAL": "PASS",
                "KNOWN": "RETHINK_QUESTION",
                "EQUIVALENT_KNOWN": "RETHINK_QUESTION",
                "UNCERTAIN": "RETHINK_QUESTION",
            },
            "COMPLETION": {
                "COMPLETE": "COMPLETE",
                "INCOMPLETE": "INCOMPLETE",
                "RETHINK_CLAIM": "RETHINK_CLAIM",
                "RETHINK_PLAN": "RETHINK_PLAN",
                "RETHINK_QUESTION": "RETHINK_QUESTION",
            },
            "CONSISTENCY": {
                "PASS": "PASS",
                "TARGETED_FOLLOWUP": "INCOMPLETE",
                "RETHINK_CLAIM": "RETHINK_CLAIM",
                "RETHINK_PLAN": "RETHINK_PLAN",
                "RETHINK_QUESTION": "RETHINK_QUESTION",
            },
            "FINAL": {
                "PASS": "PASS",
                "EDITORIAL_REVISION": "REVISION_REQUIRED",
                "TARGETED_FOLLOWUP": "REVISION_REQUIRED",
                "RETHINK_CLAIM": "RETHINK_CLAIM",
                "RETHINK_PLAN": "RETHINK_PLAN",
                "RETHINK_QUESTION": "RETHINK_QUESTION",
            },
        }
        derived = {
            verdict_map.get(str(gate.get("gate_type")), {}).get(
                str(review.data.get("assessment", {}).get("outcome"))
            )
            for review in scheme_reviews
        }
        derived.discard(None)
        if len(derived) > 1:
            issues.append("gate Reviews imply conflicting deterministic verdicts")
        elif derived and gate.get("verdict") not in derived:
            issues.append(
                f"gate verdict {gate.get('verdict')} disagrees with Review-derived {next(iter(derived))}"
            )
        if scheme_reviews and not derived:
            issues.append(
                f"{expected_scheme} Review outcome has no legal deterministic gate mapping"
            )
        if gate.get("verdict") in {"PASS", "COMPLETE"}:
            unresolved = [
                review.id
                for review in review_records
                if any(
                    issue.get("severity") in {"BLOCKER", "MAJOR"}
                    and issue.get("status") in {"OPEN", "ACCEPTED"}
                    for issue in review.data.get("issues", [])
                )
            ]
            if unresolved:
                issues.append(
                    f"gate has unresolved BLOCKER/MAJOR reviews: {', '.join(sorted(unresolved))}"
                )

        if gate.get("gate_type") == "PROPOSAL_REVIEW":
            proposal_refs = [
                ref for ref in gate.get("target_refs", [])
                if ref.get("kind") == "ResearchProposal"
            ]
            if len(proposal_refs) != 1:
                issues.append("PROPOSAL_REVIEW requires exactly one revision-pinned ResearchProposal target")
            target_ref = proposal_refs[0] if len(proposal_refs) == 1 else None
            proposal_reviews = [
                review for review in review_records
                if review.data.get("assessment", {}).get("scheme")
                in {"NOVELTY", "PROPOSAL_CORRECTNESS"}
            ]
            schemes = {
                review.data.get("assessment", {}).get("scheme")
                for review in proposal_reviews
            }
            if schemes != {"NOVELTY", "PROPOSAL_CORRECTNESS"}:
                issues.append(
                    "PROPOSAL_REVIEW requires NOVELTY and PROPOSAL_CORRECTNESS Reviews"
                )
            if target_ref is not None:
                proposal_record = index.get(target_ref.get("id"))
                if proposal_record is not None:
                    question_refs = proposal_record.data.get("question_refs", [])
                    question_ids = {ref.get("id") for ref in question_refs}
                    if not question_ids:
                        issues.append("gated ResearchProposal has no reconstructed ResearchQuestion")
                    for ref in question_refs:
                        question = index.get(ref.get("id"))
                        if question is None or question.kind != "ResearchQuestion" or question.data.get("status") != "ACTIVE":
                            issues.append(f"proposal question is missing or inactive: {ref.get('id')}")
                        elif ref.get("revision") != question.data.get("revision"):
                            issues.append(f"proposal question pin is stale: {ref.get('id')}")
                    plans = [
                        record for record in index.values()
                        if record.kind == "ResearchPlan"
                        and record.data.get("project_id") == project_id
                    ]
                    if not plans:
                        issues.append("PROPOSAL_REVIEW requires a reconstructed ResearchPlan")
                    else:
                        matching_plans = [
                            plan for plan in plans
                            if question_ids.issubset({
                                ref.get("id") for ref in plan.data.get("question_refs", [])
                            })
                        ]
                        if not matching_plans:
                            issues.append("ResearchProposal and reconstructed ResearchPlan questions disagree")
                for review in proposal_reviews:
                    pinned = review.data.get("target", {}).get("artifact_ref", {})
                    if any(pinned.get(key) != target_ref.get(key) for key in ("id", "kind", "revision")):
                        issues.append(
                            f"Review {review.id} does not target the gated proposal revision"
                        )
            novelty = next((
                review for review in proposal_reviews
                if review.data.get("assessment", {}).get("scheme") == "NOVELTY"
            ), None)
            correctness = next((
                review for review in proposal_reviews
                if review.data.get("assessment", {}).get("scheme") == "PROPOSAL_CORRECTNESS"
            ), None)
            expected_verdict = None
            if novelty is not None and correctness is not None:
                novelty_evidence = {
                    ref.get("id")
                    for item in novelty.data.get("assessment", {}).get("items", [])
                    for ref in item.get("evidence_refs", [])
                    if ref.get("kind") == "LiteratureEvidence"
                }
                verified_novelty_evidence = {
                    evidence_id for evidence_id in novelty_evidence
                    if evidence_id in index
                    and index[evidence_id].data.get("status") in {"ASSESSED", "VERIFIED"}
                }
                if not verified_novelty_evidence:
                    issues.append("proposal novelty Review has no assessed LiteratureEvidence")
                elif target_ref is not None:
                    proposal = index.get(target_ref.get("id"))
                    current_questions = {
                        (ref.get("id"), ref.get("revision"))
                        for ref in (proposal.data.get("question_refs", []) if proposal else [])
                    }
                    unrelated = sorted(
                        evidence_id for evidence_id in verified_novelty_evidence
                        if not any(
                            (
                                relation.get("target_ref", {}).get("id"),
                                relation.get("target_ref", {}).get("revision"),
                            ) in current_questions
                            for relation in index[evidence_id].data.get("relations", [])
                        )
                    )
                    if unrelated:
                        issues.append(
                            "proposal novelty evidence is not related to the current "
                            "ResearchQuestion revision: " + ", ".join(unrelated)
                        )
                novelty_outcome = novelty.data.get("assessment", {}).get("outcome")
                correctness_outcome = correctness.data.get("assessment", {}).get("outcome")
                all_issues = [
                    issue for review in proposal_reviews
                    for issue in review.data.get("issues", [])
                    if issue.get("status") in {"OPEN", "ACCEPTED"}
                ]
                material = any(
                    issue.get("severity") in {"BLOCKER", "MAJOR"}
                    for issue in all_issues
                )
                minor = any(
                    issue.get("severity") in {"MINOR", "SUGGESTION"}
                    for issue in all_issues
                )
                if novelty_outcome not in {"OPEN", "PARTIAL"} and not material:
                    issues.append("non-passing proposal novelty outcome requires a BLOCKER/MAJOR issue")
                if correctness_outcome != "PASS" and not material:
                    issues.append("non-passing proposal correctness outcome requires a BLOCKER/MAJOR issue")
                if novelty_outcome not in {"OPEN", "PARTIAL"} or correctness_outcome != "PASS" or material:
                    expected_verdict = "REVISION_REQUIRED"
                elif minor:
                    expected_verdict = "AUTO_REVISION"
                else:
                    expected_verdict = "PASS"
            if expected_verdict is not None and gate.get("verdict") != expected_verdict:
                issues.append(
                    f"PROPOSAL_REVIEW verdict {gate.get('verdict')} disagrees with deterministic {expected_verdict}"
                )

        if gate.get("gate_type") == "FEASIBILITY" and gate.get("verdict") == "PASS":
            records = [
                index[ref["id"]]
                for ref in gate.get("based_on", [])
                if ref.get("id") in index
            ]
            has_question = any(
                record.kind == "ResearchQuestion" and record.data.get("status") == "ACTIVE"
                for record in records
            )
            has_evidence = any(
                record.kind == "LiteratureEvidence"
                and record.data.get("status") in {"ASSESSED", "VERIFIED"}
                for record in records
            ) or any(
                record.kind == "Decision" and record.data.get("status") == "ACCEPTED"
                for record in records
            )
            if not has_question or not has_evidence:
                issues.append(
                    "FEASIBILITY PASS requires an ACTIVE ResearchQuestion and assessed literature or an accepted Decision"
                )

        if gate.get("gate_type") == "COMPLETION" and gate.get("verdict") == "COMPLETE":
            if active_plan is None:
                issues.append("COMPLETION COMPLETE requires an active plan")
            else:
                issues.extend(materialization_issues(active_plan, index))
                incomplete_packages = [
                    package["id"]
                    for package in active_plan.get("work_packages", [])
                    if any(
                        output.get("required") and required_skills_for_output(output)
                        for output in package.get("planned_outputs", [])
                    )
                    and package.get("status") != "DONE"
                ]
                if incomplete_packages:
                    issues.append(
                        f"COMPLETION COMPLETE has unfinished work packages: {', '.join(incomplete_packages)}"
                    )

        if gate.get("gate_type") == "CONSISTENCY" and gate.get("verdict") == "PASS":
            stale = []
            for record in index.values():
                if record.kind != "Experiment" or record.data.get("status") != "COMPLETED":
                    continue
                if project_id is not None and record.data.get("project_id") != project_id:
                    continue
                ref = record.data.get("hypothesis", {}).get("claim_ref", {})
                claim = index.get(ref.get("id"))
                promoted_empirical_pin = bool(
                    claim is not None
                    and claim.data.get("status") in {"VERIFIED", "IN_PAPER"}
                    and claim.data.get("verification", {}).get("method") == "EXPERIMENT"
                    and isinstance(ref.get("revision"), int)
                    and ref.get("revision") < claim.data.get("revision")
                )
                if claim is not None and ref.get("revision") != claim.data.get("revision") and not promoted_empirical_pin:
                    stale.append(record.id)
            if stale:
                issues.append(
                    f"CONSISTENCY PASS has experiments pinned to stale claim revisions: {', '.join(sorted(stale))}"
                )

        if gate.get("gate_type") == "FINAL" and gate.get("verdict") == "PASS":
            unresolved = [
                review.id
                for review in review_records
                if review.data.get("status") not in {"RESOLVED", "WAIVED"}
            ]
            if not review_records:
                issues.append("FINAL PASS requires at least one revision-pinned Review")
            elif unresolved:
                issues.append(
                    f"FINAL PASS has unresolved reviews: {', '.join(sorted(unresolved))}"
                )
            if not any(
                review.data.get("target", {})
                .get("artifact_ref", {})
                .get("kind")
                == "Project"
                for review in review_records
            ):
                issues.append(
                    "FINAL PASS requires a Review targeting a pinned Project revision and Git commit"
                )
        if issues:
            raise OrchestratorValidationError(issues)


class CheckpointStore:
    """Atomic, optimistic-concurrency persistence for control manifests."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.schemas = _ControlSchemas(self.root)

    def path_for(self, project_id: str) -> Path:
        return self.root / "projects" / project_id / "orchestrator" / "state.yaml"

    def require_valid(self, checkpoint: Mapping[str, Any]) -> None:
        issues = _schema_issues(self.schemas.state, checkpoint)
        if checkpoint.get("status") == "RUNNING" and checkpoint.get("state") in {
            "BLOCKED",
            "PAUSED_BY_USER",
            "CANCELLED_BY_USER",
            "DONE",
        }:
            issues.append("$: RUNNING status cannot use an interrupt or terminal state")
        if checkpoint.get("status") != "RUNNING" and checkpoint.get("status") != checkpoint.get("state"):
            issues.append("$: non-running status must equal state")
        if issues:
            raise OrchestratorValidationError(issues)

    def load(self, project_id: str) -> dict[str, Any]:
        path = self.path_for(project_id)
        if not path.is_file():
            raise OrchestratorError(f"checkpoint not found: {path}")
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise OrchestratorError(f"cannot load checkpoint {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise OrchestratorValidationError(["$: checkpoint must be a YAML mapping"])
        self.require_valid(value)
        return value

    def save(
        self, checkpoint: Mapping[str, Any], *, expected_seq: int | None
    ) -> Path:
        self.require_valid(checkpoint)
        project_id = str(checkpoint["project_id"])
        path = self.path_for(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            current = self.load(project_id)
            if expected_seq is None or current["checkpoint_seq"] != expected_seq:
                raise CheckpointConflict(
                    f"{project_id} checkpoint is {current['checkpoint_seq']}, expected {expected_seq}"
                )
        elif expected_seq is not None:
            raise CheckpointConflict(f"{project_id} checkpoint does not exist")

        serialized = yaml.safe_dump(
            dict(checkpoint), sort_keys=False, allow_unicode=True
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=".state.yaml.", dir=path.parent, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        except Exception:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise
        return path


def derive_route(plan: Mapping[str, Any]) -> str:
    """Derive THEORY/EXPERIMENT/MIXED without LLM judgment."""

    tracks = {
        package.get("track")
        for package in plan.get("work_packages", [])
        if package.get("status") != "CANCELLED"
        and any(output.get("required") for output in package.get("planned_outputs", []))
    }
    has_theory = "THEORY" in tracks
    has_experiment = "EXPERIMENT" in tracks
    if has_theory and has_experiment:
        return "MIXED"
    if has_theory:
        return "THEORY"
    if has_experiment:
        return "EXPERIMENT"
    raise OrchestratorValidationError(
        ["active plan has no required THEORY or EXPERIMENT output"]
    )


def materialization_issues(
    plan: Mapping[str, Any], index: Mapping[str, Any], target_ids: set[str] | None = None
) -> list[str]:
    """Return missing/mismatched required planned-output mappings."""

    planned = {
        output["local_id"]: output
        for package in plan.get("work_packages", [])
        for output in package.get("planned_outputs", [])
    }
    mappings = {
        mapping["local_id"]: mapping["artifact_ref"]
        for package in plan.get("work_packages", [])
        for mapping in package.get("materialized_outputs", [])
    }
    wanted = target_ids or {
        output_id
        for output_id, output in planned.items()
        if output.get("required") and required_skills_for_output(output)
    }
    issues: list[str] = []
    for output_id in sorted(wanted):
        output = planned.get(output_id)
        if output is None:
            issues.append(f"unknown planned output {output_id}")
            continue
        ref = mappings.get(output_id)
        if ref is None:
            issues.append(f"required output {output_id} is not materialized")
            continue
        record = index.get(ref["id"])
        if record is None:
            issues.append(f"materialized artifact does not exist: {ref['id']}")
        elif record.kind != output["kind"] or ref["kind"] != output["kind"]:
            issues.append(f"materialized output {output_id} has the wrong kind")
    return issues


def required_skills_for_output(output: Mapping[str, Any]) -> list[str]:
    """Return the deterministic Step 4 pipeline for one planned output."""

    kind = output.get("kind")
    if kind == "Experiment":
        return [
            "experiment-design",
            "experiment-execution",
            "experiment-verification",
        ]
    if kind != "ScientificClaim":
        return []
    # An EMPIRICAL Claim is the preregistered hypothesis owned by its
    # Experiment work package.  It is materialized as FORMALIZED scientific
    # input, but it must not silently create a theory branch in an
    # EXPERIMENT-only project.  The independent experiment pipeline verifies
    # the linked Experiment; synthesis may later attach the outcome as
    # evidence without pretending that an empirical hypothesis was proved.
    if output.get("verification_profile") == "EMPIRICAL":
        return []
    if output.get("verification_profile", "ADVERSARIAL") == "CORE_FORMAL":
        return [
            "theory-development",
            "lean-formalization",
            "theory-verification",
            "lean-verification",
            "semantic-alignment-review",
        ]
    return ["theory-development", "theory-verification"]


def build_skill_progress(
    plan: Mapping[str, Any], target_ids: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Build persisted per-output Skill progress without adding workflow states."""

    wanted = target_ids or {
        output["local_id"]
        for package in plan.get("work_packages", [])
        for output in package.get("planned_outputs", [])
        if output.get("required")
    }
    progress: dict[str, dict[str, Any]] = {}
    mappings = {
        mapping["local_id"]: mapping["artifact_ref"]
        for package in plan.get("work_packages", [])
        for mapping in package.get("materialized_outputs", [])
    }
    for package in plan.get("work_packages", []):
        for output in package.get("planned_outputs", []):
            output_id = output["local_id"]
            if output_id not in wanted:
                continue
            skills = required_skills_for_output(output)
            if not skills:
                continue
            progress[output_id] = {
                "track": str(package["track"]).lower(),
                "verification_profile": output.get("verification_profile"),
                "artifact_ref": copy.deepcopy(mappings.get(output_id)),
                "required_skills": skills,
                "completed_skills": [],
                "active_skill": None,
                "status": "PENDING",
            }
    return progress


class ResearchOrchestrator:
    """Validated façade joining artifacts, reducer, gates, and checkpoints."""

    def __init__(self, root: str | Path, *, retry_limits: Mapping[str, int] | None = None):
        self.workspace = ArtifactWorkspace(root)
        self.store = CheckpointStore(root)
        self.gates = GateEvaluator(self.workspace)
        self.reducer = StateReducer(retry_limits)

    def initialize(
        self, project_id: str, run_id: str, git_commit: str, *, at: str | None = None,
        workflow_mode: str | None = None,
    ) -> dict[str, Any]:
        project = self.workspace.get(project_id)
        if project.kind != "Project":
            raise OrchestratorError(f"{project_id} is not a Project")
        mode = workflow_mode or str(project.data.get("workflow_mode", "FULL_RESEARCH"))
        checkpoint = new_checkpoint(project_id, run_id, git_commit, at=at, workflow_mode=mode)
        self.store.save(checkpoint, expected_seq=None)
        return checkpoint

    def apply(
        self,
        project_id: str,
        event: OrchestratorEvent,
        *,
        expected_seq: int,
        artifact_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> Reduction:
        checkpoint = self.store.load(project_id)
        if checkpoint["checkpoint_seq"] != expected_seq:
            raise CheckpointConflict(
                f"{project_id} checkpoint is {checkpoint['checkpoint_seq']}, expected {expected_seq}"
            )

        payload = dict(event.payload)
        if event.type == "OUTPUTS_MATERIALIZED" and isinstance(
            payload.get("plan_ref"), dict
        ):
            ref = payload["plan_ref"]
            previous = checkpoint.get("active_plan")
            if not isinstance(previous, dict) or ref.get("id") != previous.get("id"):
                raise InvalidEvent("materialization must revise the active ResearchPlan")
            active_plan = self.workspace.get(str(ref.get("id")))
            if (
                active_plan.kind != "ResearchPlan"
                or active_plan.data.get("revision") != ref.get("revision")
            ):
                raise InvalidEvent("materialization plan_ref must pin the current Plan")
        else:
            active_plan = self._active_plan(checkpoint)
        if event.type == "GATE_RECORDED":
            gate = payload.get("gate")
            if not isinstance(gate, dict):
                raise InvalidEvent("GATE_RECORDED requires payload.gate")
            self.gates.require_valid(
                gate,
                active_plan.data if active_plan else None,
                artifact_overrides=artifact_overrides,
            )
        elif event.type == "PLAN_APPROVED":
            ref = payload.get("plan_ref")
            if not isinstance(ref, dict):
                raise InvalidEvent("PLAN_APPROVED requires payload.plan_ref")
            plan = self.workspace.get(str(ref.get("id")))
            if plan.kind != "ResearchPlan" or plan.data.get("status") != "APPROVED":
                raise InvalidEvent("approved plan ref must identify an APPROVED ResearchPlan")
            if ref.get("revision") != plan.data.get("revision"):
                raise InvalidEvent("approved plan ref must pin the current revision")
        elif event.type in {"FOLLOWUP_PLANNED", "FOLLOWUP_PLAN_SELECTED"}:
            ref = payload.get("plan_ref")
            if not isinstance(ref, dict):
                raise InvalidEvent(f"{event.type} requires payload.plan_ref")
            plan = self.workspace.get(str(ref.get("id")))
            if (
                plan.kind != "ResearchPlan"
                or ref.get("revision") != plan.data.get("revision")
                or plan.data.get("status") not in {"APPROVED", "IN_PROGRESS"}
            ):
                raise InvalidEvent(
                    "follow-up plan ref must pin a current executable ResearchPlan"
                )
        elif event.type == "ROUTE_SELECTED":
            if active_plan is None:
                raise InvalidEvent("ROUTE_SELECTED requires an active plan")
            payload["route"] = derive_route(active_plan.data)
            snapshot = payload.get("snapshot")
            if not isinstance(snapshot, dict):
                raise InvalidEvent("ROUTE_SELECTED requires payload.snapshot")
        elif event.type == "OUTPUTS_MATERIALIZED":
            if active_plan is None:
                raise InvalidEvent("OUTPUTS_MATERIALIZED requires an active plan")
            target_ids = payload.get("target_output_ids")
            issues = materialization_issues(
                active_plan.data,
                self.workspace.index(),
                set(target_ids) if target_ids else None,
            )
            if issues:
                raise OrchestratorValidationError(issues)
            payload["skill_progress"] = build_skill_progress(
                active_plan.data, set(target_ids) if target_ids else None
            )
            if not payload["skill_progress"]:
                raise OrchestratorValidationError(
                    ["materialized execution outputs have no registered Skill pipeline"]
                )
        elif event.type == "SKILL_COMPLETED":
            output_id = payload.get("output_id")
            skill_id = payload.get("skill_id")
            progress = checkpoint.get("skill_progress", {}).get(output_id)
            if not isinstance(progress, dict) or skill_id not in progress.get(
                "required_skills", []
            ):
                raise InvalidEvent("SKILL_COMPLETED must identify a required output Skill")
            self._validate_skill_completion(str(skill_id), payload)
        elif event.type == "SKILL_REVISION_REQUIRED":
            output_id = payload.get("output_id")
            skill_id = payload.get("skill_id")
            progress = checkpoint.get("skill_progress", {}).get(output_id)
            if (
                not isinstance(progress, dict)
                or skill_id != "theory-verification"
                or skill_id not in progress.get("required_skills", [])
            ):
                raise InvalidEvent(
                    "SKILL_REVISION_REQUIRED must identify a pending theory verifier"
                )
            self._validate_skill_revision_required(payload)
        elif event.type == "TRACK_ADVANCED" and payload.get("status") == "WAIVED":
            decision_ref = payload.get("decision_ref")
            if not isinstance(decision_ref, dict):
                raise InvalidEvent("WAIVED track requires payload.decision_ref")
            decision = self.workspace.get(str(decision_ref.get("id")))
            if (
                decision.kind != "Decision"
                or decision.data.get("status") != "ACCEPTED"
                or decision.data.get("revision") != decision_ref.get("revision")
            ):
                raise InvalidEvent("WAIVED track requires a pinned ACCEPTED Decision")
        elif event.type == "UNBLOCK":
            decision_ref = payload.get("recovery_decision_ref")
            if not isinstance(decision_ref, dict):
                raise InvalidEvent("UNBLOCK requires payload.recovery_decision_ref")
            decision = self.workspace.get(str(decision_ref.get("id")))
            if (
                decision.kind != "Decision"
                or decision.data.get("project_id") != project_id
                or decision.data.get("status") != "ACCEPTED"
                or decision.data.get("revision") != decision_ref.get("revision")
                or "recovery" not in decision.data.get("tags", [])
            ):
                raise InvalidEvent(
                    "UNBLOCK requires a pinned ACCEPTED recovery Decision from this project"
                )

        normalized = OrchestratorEvent(event.type, event.at, payload)
        reduction = self.reducer.reduce(checkpoint, normalized)
        self.store.save(reduction.checkpoint, expected_seq=expected_seq)
        return reduction

    def begin_action(
        self,
        project_id: str,
        action_id: str,
        input_git_commit: str,
        *,
        expected_seq: int,
        at: str | None = None,
    ) -> dict[str, Any]:
        checkpoint = self.store.load(project_id)
        if checkpoint["checkpoint_seq"] != expected_seq:
            raise CheckpointConflict(
                f"{project_id} checkpoint is {checkpoint['checkpoint_seq']}, expected {expected_seq}"
            )
        if checkpoint["status"] != "RUNNING" or checkpoint["pending_action"] is not None:
            raise InvalidEvent("cannot begin an action in the current checkpoint")
        candidate = copy.deepcopy(checkpoint)
        candidate["pending_action"] = {
            "action_id": action_id,
            "input_git_commit": input_git_commit,
            "started_at": at or utc_now(),
        }
        candidate["checkpoint_seq"] += 1
        candidate["updated_at"] = at or utc_now()
        self.store.save(candidate, expected_seq=expected_seq)
        return candidate

    def _active_plan(self, checkpoint: Mapping[str, Any]):
        ref = checkpoint.get("active_plan")
        if not isinstance(ref, dict):
            return None
        record = self.workspace.get(str(ref["id"]))
        if record.data.get("revision") != ref.get("revision"):
            raise OrchestratorValidationError(
                [f"active plan revision drift: expected {ref.get('revision')}, found {record.data.get('revision')}"]
            )
        return record

    def _validate_skill_completion(
        self, skill_id: str, payload: Mapping[str, Any]
    ) -> None:
        artifact_ids = payload.get("artifact_ids")
        if not isinstance(artifact_ids, list) or not artifact_ids:
            raise InvalidEvent("SKILL_COMPLETED requires promoted artifact_ids")
        records = [self.workspace.get(str(artifact_id)) for artifact_id in artifact_ids]
        required_kind = {
            "theory-development": "ScientificClaim",
            "lean-formalization": "ScientificClaim",
            "theory-verification": "Review",
            "lean-verification": "Review",
            "semantic-alignment-review": "Review",
            "experiment-design": "Experiment",
            "experiment-execution": "Experiment",
            "experiment-verification": "Review",
        }.get(skill_id)
        if required_kind is None or not any(
            record.kind == required_kind for record in records
        ):
            raise InvalidEvent(
                f"{skill_id} completion requires a promoted {required_kind}"
            )
        review_requirements = {
            "theory-verification": {"THEORY": {"PASS"}},
            "lean-verification": {
                "LEAN": {"PASS"}, "AXIOM_AUDIT": {"PASS"}
            },
            "semantic-alignment-review": {"SEMANTIC_ALIGNMENT": {"PASS"}},
            "experiment-verification": {
                "EXPERIMENT": {"SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"}
            },
        }.get(skill_id, {})
        assessments: dict[str, set[str]] = {}
        for record in records:
            if record.kind != "Review":
                continue
            assessment = record.data.get("assessment", {})
            assessments.setdefault(str(assessment.get("scheme")), set()).add(
                str(assessment.get("outcome"))
            )
        for scheme, outcomes in review_requirements.items():
            if not assessments.get(scheme, set()) & outcomes:
                raise InvalidEvent(
                    f"{skill_id} requires {scheme} assessment in {sorted(outcomes)}"
                )

    def _validate_skill_revision_required(
        self, payload: Mapping[str, Any]
    ) -> None:
        artifact_ids = payload.get("artifact_ids")
        if not isinstance(artifact_ids, list) or not artifact_ids:
            raise InvalidEvent(
                "SKILL_REVISION_REQUIRED requires promoted artifact_ids"
            )
        records = [self.workspace.get(str(artifact_id)) for artifact_id in artifact_ids]
        outcomes = {
            str(record.data.get("assessment", {}).get("outcome"))
            for record in records
            if record.kind == "Review"
            and record.data.get("assessment", {}).get("scheme") == "THEORY"
        }
        allowed = {
            "PROOF_GAP", "ASSUMPTION_GAP", "FALSE_CLAIM",
            "RETHINK_CLAIM", "RETHINK_PLAN", "RETHINK_QUESTION",
            "INCONCLUSIVE", "REVISION_REQUIRED",
        }
        if not outcomes or not outcomes <= allowed:
            raise InvalidEvent(
                "SKILL_REVISION_REQUIRED requires a non-passing THEORY assessment"
            )

    def active_plan(self, checkpoint: Mapping[str, Any]):
        """Return the revision-pinned active plan for integration services."""

        return self._active_plan(checkpoint)
