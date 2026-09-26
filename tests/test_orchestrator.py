from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from research_artifacts.orchestrator import (
    CheckpointConflict,
    InvalidEvent,
    OrchestratorEvent,
    OrchestratorValidationError,
    ResearchOrchestrator,
    StateReducer,
    derive_route,
    new_checkpoint,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
AT = "2026-08-21T10:00:00Z"


def event(event_type: str, **payload):
    return OrchestratorEvent(event_type, AT, payload)


class ReducerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.reducer = StateReducer()
        self.checkpoint = new_checkpoint("proj-demo", "run-demo", COMMIT, at=AT)

    def reduce(self, event_type: str, **payload):
        result = self.reducer.reduce(self.checkpoint, event(event_type, **payload))
        self.checkpoint = result.checkpoint
        return result

    def drive_to_routing(self) -> None:
        self.reduce("QUESTION_ACTIVATED")
        self.reduce("LITERATURE_READY")
        self.reduce("GATE_RECORDED", gate=self.gate("PASS", "FEASIBILITY"))
        self.reduce("PLAN_VALIDATED")
        self.reduce(
            "PLAN_APPROVED",
            plan_ref={"id": "plan-demo-main", "kind": "ResearchPlan", "revision": 1},
        )

    @staticmethod
    def progress(route: str):
        value = {}
        if route in {"THEORY", "MIXED"}:
            value["theory-main"] = {
                "track": "theory",
                "verification_profile": "ADVERSARIAL",
                "artifact_ref": {"id": "claim-demo-main", "kind": "ScientificClaim", "revision": 1},
                "required_skills": ["theory-development", "theory-verification"],
                "completed_skills": [],
                "active_skill": None,
                "status": "PENDING",
            }
        if route in {"EXPERIMENT", "MIXED"}:
            value["experiment-main"] = {
                "track": "experiment",
                "verification_profile": None,
                "artifact_ref": {"id": "exp-demo-main", "kind": "Experiment", "revision": 1},
                "required_skills": ["experiment-design", "experiment-execution", "experiment-verification"],
                "completed_skills": [],
                "active_skill": None,
                "status": "PENDING",
            }
        return value

    def complete_skill(self, output_id: str, skill_id: str) -> None:
        self.reduce("SKILL_COMPLETED", output_id=output_id, skill_id=skill_id)

    @staticmethod
    def gate(verdict: str, gate_type: str = "COMPLETION"):
        return {
            "gate_id": f"gate-{verdict.lower().replace('_', '-')}",
            "gate_type": gate_type,
            "verdict": verdict,
            "based_on": [
                {"id": "proj-demo", "kind": "Project", "revision": 1}
            ],
            "target_refs": [],
            "target_output_ids": [],
            "review_refs": [],
            "rationale": "Deterministic test outcome.",
            "recorded_at": AT,
        }

    def test_discovery_and_plan_approval_path(self) -> None:
        self.drive_to_routing()
        self.assertEqual(self.checkpoint["state"], "PLANNING_ROUTING")
        self.assertEqual(self.checkpoint["active_plan"]["revision"], 1)

    def test_action_retry_budget_resets_after_successful_action(self) -> None:
        self.reduce(
            "ACTION_FAILED", action_id="action-question-1", retry_key="action",
            retryable=True, reason="temporary",
        )
        self.assertEqual(self.checkpoint["retry_counters"]["action"], 1)
        self.reduce("QUESTION_ACTIVATED", action_id="action-question-2")
        self.assertNotIn("action", self.checkpoint["retry_counters"])
        self.reduce(
            "ACTION_FAILED", action_id="action-literature-1", retry_key="action",
            retryable=True, reason="different action",
        )
        self.assertEqual(self.checkpoint["retry_counters"]["action"], 1)

    def test_nonpassing_theory_review_reopens_only_target_development(self) -> None:
        self.checkpoint["state"] = "EXECUTION_TRACKS"
        self.checkpoint["route"] = "THEORY"
        self.checkpoint["branch_states"] = {
            "theory": "VERIFY", "experiment": "NOT_SELECTED"
        }
        self.checkpoint["skill_progress"] = self.progress("THEORY")
        self.checkpoint["skill_progress"]["theory-main"]["completed_skills"] = [
            "theory-development"
        ]
        self.checkpoint["skill_progress"]["theory-main"]["status"] = "IN_PROGRESS"

        self.reduce(
            "SKILL_REVISION_REQUIRED",
            action_id="action-theory-review-gap",
            output_id="theory-main",
            skill_id="theory-verification",
            artifact_ids=["review-demo-gap"],
        )

        self.assertEqual(self.checkpoint["branch_states"]["theory"], "DEVELOP")
        progress = self.checkpoint["skill_progress"]["theory-main"]
        self.assertEqual(progress["completed_skills"], [])
        self.assertEqual(progress["status"], "IN_PROGRESS")
        self.assertEqual(
            self.checkpoint["retry_counters"]["theory_revision:theory-main"], 1
        )

    def test_passing_review_promotes_the_reviewed_replacement_claim(self) -> None:
        self.checkpoint["state"] = "EXECUTION_TRACKS"
        self.checkpoint["route"] = "THEORY"
        self.checkpoint["branch_states"] = {
            "theory": "VERIFY", "experiment": "NOT_SELECTED"
        }
        self.checkpoint["skill_progress"] = self.progress("THEORY")
        progress = self.checkpoint["skill_progress"]["theory-main"]
        progress["completed_skills"] = ["theory-development"]
        progress["status"] = "IN_PROGRESS"
        replacement = {
            "id": "claim-demo-replacement",
            "kind": "ScientificClaim",
            "revision": 1,
        }

        result = self.reduce(
            "SKILL_COMPLETED",
            action_id="action-theory-review-pass",
            output_id="theory-main",
            skill_id="theory-verification",
            artifact_ids=["review-demo-replacement-pass"],
            verified_target_ref=replacement,
        )

        updated = self.checkpoint["skill_progress"]["theory-main"]
        self.assertEqual(updated["artifact_ref"], replacement)
        self.assertEqual(updated["status"], "COMPLETED")
        self.assertEqual(result.commands[0].type, "PROMOTE_VERIFIED_CLAIM")
        self.assertEqual(result.commands[0].payload["artifact_ref"], replacement)

    def test_proposal_novelty_can_fail_closed_back_to_structuring(self) -> None:
        self.checkpoint = new_checkpoint(
            "proj-demo", "run-demo", COMMIT, at=AT,
            workflow_mode="PROPOSAL_REVIEW",
        )
        self.reduce("PROPOSAL_STRUCTURED")
        self.assertEqual(self.checkpoint["state"], "PROPOSAL_NOVELTY_REVIEW")
        self.reduce(
            "PROPOSAL_STRUCTURING_REQUIRED",
            reason="active proposal revision has no field-level source_map",
        )
        self.assertEqual(self.checkpoint["state"], "PROPOSAL_STRUCTURING")

    def test_mixed_tracks_join_only_after_both_complete(self) -> None:
        self.drive_to_routing()
        snapshot = {"git_commit": COMMIT, "artifacts": []}
        self.reduce("ROUTE_SELECTED", route="MIXED", snapshot=snapshot)
        self.reduce("OUTPUTS_MATERIALIZED", skill_progress=self.progress("MIXED"))
        self.reduce("TRACK_ADVANCED", track="theory", status="DEVELOP")
        self.complete_skill("theory-main", "theory-development")
        self.reduce("TRACK_ADVANCED", track="theory", status="VERIFY")
        self.complete_skill("theory-main", "theory-verification")
        self.reduce("TRACK_ADVANCED", track="theory", status="COMPLETED")
        with self.assertRaises(InvalidEvent):
            self.reduce("TRACKS_JOINED")
        self.reduce("TRACK_ADVANCED", track="experiment", status="PREPARE")
        self.complete_skill("experiment-main", "experiment-design")
        self.reduce("TRACK_ADVANCED", track="experiment", status="RUN")
        self.complete_skill("experiment-main", "experiment-execution")
        self.reduce("TRACK_ADVANCED", track="experiment", status="ASSESS")
        self.complete_skill("experiment-main", "experiment-verification")
        self.reduce("TRACK_ADVANCED", track="experiment", status="COMPLETED")
        self.reduce("TRACKS_JOINED")
        self.assertEqual(self.checkpoint["state"], "EXECUTION_JOIN")

    def test_complete_theory_workflow_reaches_done(self) -> None:
        self.drive_to_routing()
        self.reduce(
            "ROUTE_SELECTED",
            route="THEORY",
            snapshot={"git_commit": COMMIT, "artifacts": []},
        )
        self.reduce("OUTPUTS_MATERIALIZED", skill_progress=self.progress("THEORY"))
        self.reduce("TRACK_ADVANCED", track="theory", status="DEVELOP")
        self.complete_skill("theory-main", "theory-development")
        self.reduce("TRACK_ADVANCED", track="theory", status="VERIFY")
        self.complete_skill("theory-main", "theory-verification")
        self.reduce("TRACK_ADVANCED", track="theory", status="COMPLETED")
        self.reduce("TRACKS_JOINED")
        self.reduce("JOIN_COMPLETED")
        self.reduce("SYNTHESIS_COMPLETED")
        self.reduce("GATE_RECORDED", gate=self.gate("COMPLETE"))
        self.reduce(
            "GATE_RECORDED", gate=self.gate("PASS", "CONSISTENCY")
        )
        self.reduce("WRITING_COMPLETED")
        self.reduce("GATE_RECORDED", gate=self.gate("PASS", "FINAL"))
        result = self.reduce("FINAL_APPROVED", reason="human accepted final")
        self.assertEqual(self.checkpoint["state"], "DONE")
        self.assertEqual(self.checkpoint["status"], "DONE")
        self.assertTrue(
            any(command.type == "UPDATE_PROJECT_STATUS" for command in result.commands)
        )

    def test_final_review_revision_stays_in_writing(self) -> None:
        checkpoint = copy.deepcopy(self.checkpoint)
        checkpoint["state"] = "WRITING_FINAL_REVIEW"
        result = self.reducer.reduce(
            checkpoint,
            event(
                "GATE_RECORDED",
                gate=self.gate("REVISION_REQUIRED", "FINAL"),
            ),
        )
        self.assertEqual(result.checkpoint["state"], "WRITING_TARGETED_REVISION")
        revised = self.reducer.reduce(
            result.checkpoint, event("REVISION_COMPLETED")
        )
        self.assertEqual(revised.checkpoint["state"], "WRITING_FINAL_REVIEW")

    def test_plan_revision_persists_feedback_decision_command(self) -> None:
        checkpoint = copy.deepcopy(self.checkpoint)
        checkpoint["state"] = "PLANNING_APPROVAL"
        result = self.reducer.reduce(
            checkpoint,
            event(
                "PLAN_REVISION_REQUIRED",
                reason="analyzer refuses mixed dataset hashes",
                actor_type="human",
                actor_id="researcher",
            ),
        )
        self.assertEqual(result.checkpoint["state"], "PLANNING_DRAFT")
        self.assertTrue(
            any(
                command.type == "CREATE_PLAN_REVISION_DECISION"
                and command.payload.get("reason")
                == "analyzer refuses mixed dataset hashes"
                for command in result.commands
            )
        )

    def test_completion_routes_all_rethink_levels(self) -> None:
        expected = {
            "RETHINK_CLAIM": "FOLLOWUP_CLAIM_RETHINK",
            "RETHINK_PLAN": "PLANNING_DRAFT",
            "RETHINK_QUESTION": "DISCOVERY_QUESTION",
        }
        for verdict, state in expected.items():
            checkpoint = copy.deepcopy(self.checkpoint)
            checkpoint["state"] = "SYNTHESIS_COMPLETION_GATE"
            checkpoint["active_plan"] = {
                "id": "plan-demo-main",
                "kind": "ResearchPlan",
                "revision": 1,
            }
            result = self.reducer.reduce(
                checkpoint,
                event("GATE_RECORDED", gate=self.gate(verdict)),
            )
            self.assertEqual(result.checkpoint["state"], state)
            if verdict in {"RETHINK_PLAN", "RETHINK_QUESTION"}:
                self.assertIsNone(result.checkpoint["active_plan"])

    def test_action_failure_rethink_plan_resets_scope_without_blocking(self) -> None:
        checkpoint = copy.deepcopy(self.checkpoint)
        checkpoint["state"] = "EXECUTION_TRACKS"
        checkpoint["active_plan"] = {
            "id": "plan-demo-main",
            "kind": "ResearchPlan",
            "revision": 1,
        }
        checkpoint["route"] = "MIXED"
        checkpoint["branch_states"] = {
            "theory": "DEVELOP",
            "experiment": "RUN",
        }
        checkpoint["skill_progress"] = {"bad-protocol": {"status": "IN_PROGRESS"}}
        checkpoint["retry_counters"]["action"] = 2

        result = self.reducer.reduce(
            checkpoint,
            event(
                "ACTION_FAILED",
                action_id="action-invalid-protocol",
                retry_key="action",
                retryable=False,
                reason="RETHINK_PLAN",
            ),
        )

        self.assertEqual(result.checkpoint["state"], "PLANNING_DRAFT")
        self.assertEqual(result.checkpoint["status"], "RUNNING")
        self.assertIsNone(result.checkpoint["active_plan"])
        self.assertIsNone(result.checkpoint["route"])
        self.assertEqual(result.checkpoint["skill_progress"], {})
        self.assertEqual(
            result.checkpoint["branch_states"],
            {"theory": "NOT_SELECTED", "experiment": "NOT_SELECTED"},
        )
        self.assertNotIn("action", result.checkpoint["retry_counters"])
        self.assertTrue(
            any(
                command.type == "ENTER_STATE"
                and command.payload.get("state") == "PLANNING_DRAFT"
                for command in result.commands
            )
        )

    def test_unblock_legacy_rethink_plan_enters_planning(self) -> None:
        checkpoint = copy.deepcopy(self.checkpoint)
        checkpoint["state"] = "BLOCKED"
        checkpoint["status"] = "BLOCKED"
        checkpoint["resume_state"] = "EXECUTION_TRACKS"
        checkpoint["active_plan"] = {
            "id": "plan-demo-main",
            "kind": "ResearchPlan",
            "revision": 1,
        }
        checkpoint["route"] = "EXPERIMENT"
        checkpoint["branch_states"] = {
            "theory": "NOT_SELECTED",
            "experiment": "RUN",
        }
        checkpoint["skill_progress"] = {"bad-protocol": {"status": "IN_PROGRESS"}}
        checkpoint["blocker"] = {
            "reason": "RETHINK_PLAN",
            "recorded_at": "2026-01-01T00:00:00Z",
        }

        result = self.reducer.reduce(
            checkpoint,
            event("UNBLOCK", reason="recovery decision recorded"),
        )

        self.assertEqual(result.checkpoint["state"], "PLANNING_DRAFT")
        self.assertIsNone(result.checkpoint["active_plan"])
        self.assertEqual(result.checkpoint["skill_progress"], {})

    def test_pause_cancel_and_blocked_are_distinct(self) -> None:
        paused = self.reducer.reduce(
            self.checkpoint, event("USER_PAUSE", reason="researcher requested pause")
        ).checkpoint
        self.assertEqual(paused["status"], "PAUSED_BY_USER")
        resumed = self.reducer.reduce(paused, event("RESUME")).checkpoint
        self.assertEqual(resumed["state"], "DISCOVERY_QUESTION")

        cancelled = self.reducer.reduce(
            resumed, event("USER_CANCEL", reason="project abandoned")
        )
        self.assertEqual(cancelled.checkpoint["status"], "CANCELLED_BY_USER")
        self.assertTrue(
            any(command.type == "CREATE_CANCELLATION_DECISION" for command in cancelled.commands)
        )

        blocked = resumed
        for _ in range(3):
            blocked = self.reducer.reduce(
                blocked, event("ACTION_FAILED", reason="compute unavailable")
            ).checkpoint
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertNotEqual(blocked["status"], paused["status"])

    def test_rethink_does_not_automatically_escalate(self) -> None:
        checkpoint = copy.deepcopy(self.checkpoint)
        checkpoint["state"] = "FOLLOWUP_CLAIM_RETHINK"
        for _ in range(3):
            result = self.reducer.reduce(
                checkpoint,
                event(
                    "ACTION_FAILED",
                    retry_key="claim_rethink",
                    reason="claim still unsupported",
                ),
            )
            checkpoint = result.checkpoint
        self.assertEqual(checkpoint["state"], "BLOCKED")
        self.assertEqual(checkpoint["resume_state"], "FOLLOWUP_CLAIM_RETHINK")


class OrchestratorIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "schemas", self.root / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "tests/fixtures/projects", self.root / "projects")
        shutil.rmtree(self.root / "projects" / "proj-demo" / "orchestrator", ignore_errors=True)
        self.orchestrator = ResearchOrchestrator(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_checkpoint_store_detects_concurrent_writer(self) -> None:
        checkpoint = self.orchestrator.initialize("proj-demo", "run-new", COMMIT, at=AT)
        candidate = copy.deepcopy(checkpoint)
        candidate["checkpoint_seq"] = 1
        with self.assertRaises(CheckpointConflict):
            self.orchestrator.store.save(candidate, expected_seq=99)

    def test_controller_derives_mixed_route_from_plan(self) -> None:
        checkpoint = self.orchestrator.initialize("proj-demo", "run-new", COMMIT, at=AT)
        checkpoint["state"] = "PLANNING_ROUTING"
        checkpoint["active_plan"] = {
            "id": "plan-demo-main",
            "kind": "ResearchPlan",
            "revision": 1,
        }
        checkpoint["checkpoint_seq"] = 1
        self.orchestrator.store.save(checkpoint, expected_seq=0)
        reduction = self.orchestrator.apply(
            "proj-demo",
            event(
                "ROUTE_SELECTED",
                snapshot={"git_commit": COMMIT, "artifacts": []},
            ),
            expected_seq=1,
        )
        self.assertEqual(reduction.checkpoint["route"], "MIXED")

    def test_followup_plan_selection_replaces_active_plan(self) -> None:
        checkpoint = self.orchestrator.initialize("proj-demo", "run-new", COMMIT, at=AT)
        checkpoint["state"] = "EXECUTION_MATERIALIZE"
        checkpoint["active_plan"] = {
            "id": "plan-demo-main", "kind": "ResearchPlan", "revision": 1,
        }
        checkpoint["checkpoint_seq"] = 1
        self.orchestrator.store.save(checkpoint, expected_seq=0)

        plan = copy.deepcopy(self.orchestrator.workspace.get("plan-demo-main").data)
        plan["id"] = "plan-demo-followup"
        plan["revision"] = 1
        plan["status"] = "IN_PROGRESS"
        plan["work_packages"][0]["materialized_outputs"] = []
        path = self.root / "projects/proj-demo/plans/plan-demo-followup.yaml"
        path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")

        reduction = self.orchestrator.apply(
            "proj-demo",
            event(
                "FOLLOWUP_PLAN_SELECTED",
                plan_ref={
                    "id": "plan-demo-followup", "kind": "ResearchPlan", "revision": 1,
                },
            ),
            expected_seq=1,
        )
        self.assertEqual(reduction.checkpoint["state"], "EXECUTION_MATERIALIZE")
        self.assertEqual(reduction.checkpoint["active_plan"]["id"], "plan-demo-followup")
        self.assertTrue(any(c.type == "UPDATE_PROJECT_ACTIVE_PLAN" for c in reduction.commands))

    def test_gate_rejects_unknown_planned_output(self) -> None:
        checkpoint = self.orchestrator.initialize("proj-demo", "run-new", COMMIT, at=AT)
        checkpoint["state"] = "SYNTHESIS_COMPLETION_GATE"
        checkpoint["active_plan"] = {
            "id": "plan-demo-main",
            "kind": "ResearchPlan",
            "revision": 1,
        }
        checkpoint["checkpoint_seq"] = 1
        self.orchestrator.store.save(checkpoint, expected_seq=0)
        gate = ReducerTestCase.gate("INCOMPLETE")
        gate["target_output_ids"] = ["missing-output"]
        with self.assertRaises(OrchestratorValidationError):
            self.orchestrator.apply(
                "proj-demo",
                event("GATE_RECORDED", gate=gate),
                expected_seq=1,
            )

    def test_final_pass_requires_project_targeted_review(self) -> None:
        plan = self.orchestrator.workspace.get("plan-demo-main")
        gate = ReducerTestCase.gate("PASS", "FINAL")
        gate["based_on"] = [
            {"id": "proj-demo", "kind": "Project", "revision": 1}
        ]
        gate["review_refs"] = [
            {
                "id": "review-demo-claim-001",
                "kind": "Review",
                "revision": 1,
            }
        ]
        with self.assertRaises(OrchestratorValidationError):
            self.orchestrator.gates.require_valid(gate, plan.data)

    def test_gate_validation_accepts_atomic_staged_review_overlay(self) -> None:
        plan = self.orchestrator.workspace.get("plan-demo-main")
        review = copy.deepcopy(
            self.orchestrator.workspace.get("review-demo-claim-001").data
        )
        review_id = "review-demo-atomic-consistency"
        review["id"] = review_id
        review["title"] = "Atomic consistency review"
        review["assessment"] = {
            "scheme": "CONSISTENCY",
            "outcome": "PASS",
            "items": [{
                "subject": "plan-demo-main",
                "outcome": "PASS",
                "confidence": 0.99,
                "evidence_refs": [
                    {"id": "plan-demo-main", "kind": "ResearchPlan", "revision": 1}
                ],
            }],
        }
        review["target"] = {
            "artifact_ref": {
                "id": "plan-demo-main", "kind": "ResearchPlan", "revision": 1,
            },
            "git_commit": COMMIT,
        }
        review["reviewer"]["reviewer_type"] = "CONSISTENCY"
        review["recommendation"] = "ACCEPT"
        review["status"] = "RESOLVED"
        review["issues"] = []
        gate = ReducerTestCase.gate("PASS", "CONSISTENCY")
        ref = {"id": review_id, "kind": "Review", "revision": 1}
        gate["based_on"] = [ref, {
            "id": "plan-demo-main", "kind": "ResearchPlan", "revision": 1,
        }]
        gate["review_refs"] = [ref]
        gate["target_refs"] = [{
            "id": "plan-demo-main", "kind": "ResearchPlan", "revision": 1,
        }]
        self.orchestrator.gates.require_valid(
            gate, plan.data, artifact_overrides={review_id: review}
        )

    def test_begin_action_is_idempotence_checkpoint(self) -> None:
        self.orchestrator.initialize("proj-demo", "run-new", COMMIT, at=AT)
        checkpoint = self.orchestrator.begin_action(
            "proj-demo", "action-001", COMMIT, expected_seq=0, at=AT
        )
        self.assertEqual(checkpoint["pending_action"]["action_id"], "action-001")
        with self.assertRaises(InvalidEvent):
            self.orchestrator.begin_action(
                "proj-demo", "action-002", COMMIT, expected_seq=1, at=AT
            )

    def test_route_has_no_scientific_track_is_rejected(self) -> None:
        plan = copy.deepcopy(self.orchestrator.workspace.get("plan-demo-main").data)
        for package in plan["work_packages"]:
            if package["track"] in {"THEORY", "EXPERIMENT"}:
                package["status"] = "CANCELLED"
        with self.assertRaises(OrchestratorValidationError):
            derive_route(plan)

    def test_consistency_gate_detects_stale_experiment_claim(self) -> None:
        claim = self.orchestrator.workspace.transition(
            "claim-demo-recoverable-state",
            expected_revision=1,
            new_status="SUPPORTED",
            actor_type="agent",
            actor_id="theory-test",
        )
        plan = self.orchestrator.workspace.get("plan-demo-main")
        gate = ReducerTestCase.gate("PASS", "CONSISTENCY")
        gate["based_on"] = [
            {"id": claim.id, "kind": claim.kind, "revision": 2},
            {
                "id": "exp-demo-state-recovery",
                "kind": "Experiment",
                "revision": 1,
            },
        ]
        with self.assertRaises(OrchestratorValidationError):
            self.orchestrator.gates.require_valid(gate, plan.data)

    def test_gate_verdict_must_match_structured_review_assessment(self) -> None:
        source = copy.deepcopy(
            self.orchestrator.workspace.get("review-demo-claim-001").data
        )
        source["id"] = "review-demo-completion-mapping"
        source["title"] = "Completion mapping review"
        source["target"]["artifact_ref"] = {
            "id": "plan-demo-main",
            "kind": "ResearchPlan",
            "revision": 1,
        }
        source["reviewer"]["reviewer_type"] = "COMPLETION"
        source["assessment"] = {
            "scheme": "COMPLETION",
            "outcome": "INCOMPLETE",
            "items": [],
        }
        path = (
            self.root
            / "projects/proj-demo/reviews/review-demo-completion-mapping.yaml"
        )
        path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")
        plan = self.orchestrator.workspace.get("plan-demo-main")
        gate = ReducerTestCase.gate("COMPLETE")
        gate["review_refs"] = [
            {
                "id": source["id"],
                "kind": "Review",
                "revision": 1,
            }
        ]
        with self.assertRaises(OrchestratorValidationError) as caught:
            self.orchestrator.gates.require_valid(gate, plan.data)
        self.assertIn("Review-derived INCOMPLETE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
