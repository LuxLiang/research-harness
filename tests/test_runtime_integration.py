from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from research_artifacts import ArtifactService, ResearchContextBuilder
from research_artifacts.runtime_types import ContextLimitError
from research_artifacts.runtime_types import RuntimeValidationError
from research_artifacts.sidecar import SidecarServer
from research_artifacts.transactions import ScientificTransaction, _draft_plan_recency_key
from research_artifacts.tool_adapters import ExperimentExecutionAdapter
from research_artifacts.workspace import RevisionConflict


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RuntimeIntegrationTestCase(unittest.TestCase):
    def test_replanned_draft_wins_over_older_equal_revision(self) -> None:
        class Record:
            def __init__(self, artifact_id: str, revision: int, updated_at: str):
                self.id = artifact_id
                self.data = {"revision": revision, "updated_at": updated_at}

        older = Record("plan-study-001-74-planning-draft", 1, "2026-08-23T05:45:33Z")
        replacement = Record("plan-study-001-77-planning-draft", 1, "2026-08-23T05:45:33Z")
        self.assertIs(max([older, replacement], key=_draft_plan_recency_key), replacement)

    def test_theory_role_respects_output_dependency_order(self) -> None:
        checkpoint = {
            "branch_states": {"theory": "DEVELOP", "experiment": "NOT_SELECTED"},
            "skill_progress": {
                "out-dependent": {
                    "track": "theory", "status": "PENDING",
                    "required_skills": ["theory-development", "theory-verification"],
                    "completed_skills": [],
                },
                "out-root": {
                    "track": "theory", "status": "PENDING",
                    "required_skills": ["theory-development", "theory-verification"],
                    "completed_skills": [],
                },
            },
        }
        role = ResearchContextBuilder._role_for(
            "EXECUTION_TRACKS", "theory", checkpoint,
            {"out-dependent": ("out-root",), "out-root": ()},
        )
        self.assertEqual(role[2], "out-root")
        checkpoint["skill_progress"]["out-root"]["completed_skills"] = ["theory-development"]
        role = ResearchContextBuilder._role_for(
            "EXECUTION_TRACKS", "theory", checkpoint,
            {"out-dependent": ("out-root",), "out-root": ()},
        )
        self.assertEqual(role[2], "out-dependent")

        checkpoint["branch_states"]["theory"] = "VERIFY"
        checkpoint["skill_progress"]["out-dependent"]["completed_skills"] = ["theory-development"]
        checkpoint["skill_progress"]["out-root"]["status"] = "IN_PROGRESS"
        role = ResearchContextBuilder._role_for(
            "EXECUTION_TRACKS", "theory", checkpoint,
            {"out-dependent": ("out-root",), "out-root": ()},
        )
        self.assertEqual(role[2], "out-root")

    def test_theory_development_accepts_completed_experiment_dependency(self) -> None:
        checkpoint = {
            "branch_states": {"theory": "DEVELOP", "experiment": "COMPLETED"},
            "skill_progress": {
                "out-experiment": {
                    "track": "experiment", "status": "COMPLETED",
                    "required_skills": ["experiment-design", "experiment-execution", "experiment-verification"],
                    "completed_skills": ["experiment-design", "experiment-execution", "experiment-verification"],
                },
                "out-theory": {
                    "track": "theory", "status": "PENDING",
                    "required_skills": ["theory-development", "theory-verification"],
                    "completed_skills": [],
                },
            },
        }
        role = ResearchContextBuilder._role_for(
            "EXECUTION_TRACKS", "theory", checkpoint,
            {"out-theory": ("out-experiment",), "out-experiment": ()},
        )
        self.assertEqual(role, (
            "theory-development", "research-theory", "out-theory",
        ))

        checkpoint["skill_progress"]["out-experiment"]["status"] = "IN_PROGRESS"
        with self.assertRaisesRegex(RuntimeValidationError, "no pending Skill"):
            ResearchContextBuilder._role_for(
                "EXECUTION_TRACKS", "theory", checkpoint,
                {"out-theory": ("out-experiment",), "out-experiment": ()},
            )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "schemas", self.root / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "config", self.root / "config")
        shutil.copytree(REPOSITORY_ROOT / "tests/fixtures/projects", self.root / "projects")
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Research Harness Test")
        self._git("config", "user.email", "research-harness@example.invalid")
        self._git("add", "schemas", "projects")
        self._git("commit", "-m", "initial fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=True,
        )

    def _build_context(self, action_id: str) -> dict:
        return ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id=action_id,
            input_git_commit=self.head,
            allowed_artifact_ids=["review-demo-claim-001"],
            allowed_create_ids=["review-demo-completion-002"],
        )

    def _review_candidate(self, revision: int = 2) -> dict:
        path = self.root / "projects/proj-demo/reviews/review-demo-claim-001.yaml"
        candidate = yaml.safe_load(path.read_text(encoding="utf-8"))
        candidate["revision"] = revision
        candidate["updated_at"] = "2026-08-21T06:00:00Z"
        candidate["title"] = "Review recoverable scientific state (runtime revision)"
        candidate["provenance"]["updated_by"] = {
            "actor_type": "agent",
            "actor_id": "runtime-test",
            "session_id": "session-runtime-test",
        }
        return candidate

    def _write_checkpoint(self, **updates) -> None:
        path = self.root / "projects/proj-demo/orchestrator/state.yaml"
        checkpoint = yaml.safe_load(path.read_text(encoding="utf-8"))
        checkpoint.update(updates)
        path.write_text(yaml.safe_dump(checkpoint, sort_keys=False), encoding="utf-8")

    def test_context_is_pinned_persisted_and_schema_validated(self) -> None:
        first = self._build_context("action-context")
        second = self._build_context("action-context")
        self.assertEqual(first, second)
        self.assertEqual(first["input_git_commit"], self.head)
        self.assertEqual(len(first["bundle_sha256"]), 64)
        self.assertTrue(first["artifacts"])

    def test_experiment_design_proposal_gets_host_sealed_protocol_hash(self) -> None:
        action_id = "action-host-seals-protocol"
        bundle = self._build_context(action_id)
        context_path = (
            self.root / ".harness/research/staging/proj-demo" / action_id / "context.json"
        )
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["role"] = "experiment-design"
        context_path.write_text(json.dumps(context), encoding="utf-8")

        experiment = copy.deepcopy(yaml.safe_load(
            (self.root / "projects/proj-demo/experiments/exp-demo-state-recovery.yaml")
            .read_text(encoding="utf-8")
        ))
        experiment["status"] = "READY"
        experiment["protocol_lock"]["sha256"] = "0" * 64
        proposal = ArtifactService(self.root).stage_proposal(
            proposal_id="proposal-host-sealed-protocol",
            project_id="proj-demo",
            action_id=action_id,
            operation="REVISE",
            artifact_id=experiment["id"],
            kind="Experiment",
            base_revision=experiment["revision"],
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="experiment-designer",
            candidate=experiment,
        )
        self.assertEqual(
            proposal.candidate["protocol_lock"]["sha256"],
            ExperimentExecutionAdapter.protocol_hash(experiment),
        )

    def test_experiment_design_rejects_outcome_blind_claim_after_visible_results(self) -> None:
        action_id = "action-rejects-retroactive-outcome-blindness"
        bundle = self._build_context(action_id)
        context_path = (
            self.root / ".harness/research/staging/proj-demo" / action_id / "context.json"
        )
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["role"] = "experiment-design"
        context["allowed_outputs"] = {
            "artifact_ids": [],
            "artifact_kinds": ["Experiment"],
            "create_ids": ["exp-demo-retroactive-audit"],
            "rules": [{
                "kind": "Experiment",
                "operations": ["CREATE"],
                "fields": ["*"],
            }],
        }
        self.assertTrue(ArtifactService._context_exposes_experiment_outcomes(context))
        context_path.write_text(json.dumps(context), encoding="utf-8")

        experiment = copy.deepcopy(yaml.safe_load(
            (self.root / "projects/proj-demo/experiments/exp-demo-state-recovery.yaml")
            .read_text(encoding="utf-8")
        ))
        experiment["id"] = "exp-demo-retroactive-audit"
        experiment["revision"] = 1
        experiment["status"] = "READY"
        experiment["title"] = "Outcome-blind successor audit"
        experiment["protocol_lock"]["sha256"] = "0" * 64
        service = ArtifactService(self.root)
        service.stage_proposal(
            proposal_id="proposal-retroactive-outcome-blindness",
            project_id="proj-demo",
            action_id=action_id,
            operation="CREATE",
            artifact_id=experiment["id"],
            kind="Experiment",
            base_revision=None,
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="experiment-designer",
            candidate=experiment,
        )
        with self.assertRaisesRegex(
            RuntimeValidationError,
            "context already exposes prior experiment outcomes",
        ):
            service.validate_action(
                "proj-demo",
                action_id,
                ["proposal-retroactive-outcome-blindness"],
            )

    def test_experiment_execution_mutable_patch_expands_from_canonical(self) -> None:
        action_id = "action-host-expands-run-patch"
        experiment_path = (
            self.root / "projects/proj-demo/experiments/exp-demo-state-recovery.yaml"
        )
        canonical = yaml.safe_load(experiment_path.read_text(encoding="utf-8"))
        appended_run = copy.deepcopy(canonical["runs"][0])
        appended_run["run_id"] = "recovery-002"
        canonical["status"] = "READY"
        canonical["runs"] = []
        experiment_path.write_text(
            yaml.safe_dump(canonical, sort_keys=False), encoding="utf-8"
        )
        bundle = self._build_context(action_id)
        context_path = (
            self.root / ".harness/research/staging/proj-demo" / action_id / "context.json"
        )
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["role"] = "experiment-execution"
        context["allowed_outputs"] = {
            "artifact_ids": [canonical["id"]],
            "artifact_kinds": ["Experiment"],
            "create_ids": [],
            "rules": [{
                "kind": "Experiment",
                "operations": ["REVISE"],
                "fields": [
                    "revision", "updated_at", "provenance.updated_by",
                    "status", "runs",
                ],
            }],
        }
        context_path.write_text(json.dumps(context), encoding="utf-8")

        proposal = ArtifactService(self.root).stage_proposal(
            proposal_id="proposal-host-expanded-run-patch",
            project_id="proj-demo",
            action_id=action_id,
            operation="REVISE",
            artifact_id=canonical["id"],
            kind="Experiment",
            base_revision=canonical["revision"],
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="experiment-runner",
            candidate={
                "id": canonical["id"],
                "kind": canonical["kind"],
                "revision": 2,
                "updated_at": "2026-08-21T10:00:00Z",
                "provenance": {"updated_by": {
                    "actor_type": "agent", "actor_id": "experiment-execution",
                    "session_id": "experiment-runner",
                }},
                "status": "RUNNING",
                "runs": [appended_run],
            },
        )
        self.assertEqual(proposal.candidate["id"], canonical["id"])
        self.assertEqual(proposal.candidate["method"], canonical["method"])
        self.assertEqual(proposal.candidate["protocol_lock"], canonical["protocol_lock"])
        self.assertEqual(len(proposal.candidate["runs"]), 1)
        self.assertEqual(
            proposal.candidate["provenance"]["created_by"],
            canonical["provenance"]["created_by"],
        )

        with patch.object(
            ArtifactService, "validate_action", return_value={"valid": True}
        ):
            submission = ArtifactService(self.root).submit_action(
                project_id="proj-demo",
                action_id=action_id,
                bundle_sha256=bundle["bundle_sha256"],
                proposal_ids=[],
                outcome="FAILED",
                failure_classification="LOCAL_RETRY",
            )
        self.assertEqual(submission["outcome"], "SUBMITTED")
        self.assertEqual(submission["proposal_ids"], [proposal.proposal_id])
        self.assertNotIn("failure_classification", submission)

    def test_failed_execution_recovers_one_committed_run_without_proposal(self) -> None:
        action_id = "action-host-recovers-committed-run"
        experiment_path = (
            self.root / "projects/proj-demo/experiments/exp-demo-state-recovery.yaml"
        )
        canonical = yaml.safe_load(experiment_path.read_text(encoding="utf-8"))
        recovered_run = copy.deepcopy(canonical["runs"][0])
        recovered_run["run_id"] = "recovery-host-001"
        canonical["status"] = "READY"
        canonical["runs"] = []
        experiment_path.write_text(
            yaml.safe_dump(canonical, sort_keys=False), encoding="utf-8"
        )
        bundle = self._build_context(action_id)
        context_path = (
            self.root / ".harness/research/staging/proj-demo" / action_id / "context.json"
        )
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["role"] = "experiment-execution"
        context["allowed_outputs"] = {
            "artifact_ids": [canonical["id"]],
            "artifact_kinds": ["Experiment"],
            "create_ids": [],
            "rules": [{
                "kind": "Experiment", "operations": ["REVISE"],
                "fields": [
                    "revision", "updated_at", "provenance.updated_by",
                    "status", "runs",
                ],
            }],
        }
        context_path.write_text(json.dumps(context), encoding="utf-8")
        run_root = (
            self.root / ".harness/research/actions/proj-demo" / action_id / "run-one"
        )
        run_root.mkdir(parents=True)
        (run_root / "run-state.json").write_text(json.dumps({
            "status": "COMMITTED", "run": recovered_run,
        }), encoding="utf-8")

        with patch.object(
            ArtifactService, "validate_action", return_value={"valid": True}
        ):
            submission = ArtifactService(self.root).submit_action(
                project_id="proj-demo", action_id=action_id,
                bundle_sha256=bundle["bundle_sha256"], proposal_ids=[],
                outcome="FAILED", failure_classification="LOCAL_RETRY",
            )
        self.assertEqual(submission["outcome"], "SUBMITTED")
        self.assertEqual(len(submission["proposal_ids"]), 1)
        staged = ArtifactService(self.root).list_proposals("proj-demo", action_id)
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].candidate["runs"][0]["run_id"], "recovery-host-001")

    def test_literature_actions_offer_multiple_deterministic_evidence_slots(self) -> None:
        create_ids = ResearchContextBuilder._default_create_ids(
            "DISCOVERY_LITERATURE", "literature-novelty", "proj-demo",
            "run-demo-001-7-discovery-literature", {}, {},
        )
        literature_ids = [item for item in create_ids if item.startswith("lit-")]
        self.assertEqual(len(literature_ids), 8)
        self.assertEqual(len(set(literature_ids)), 8)
        self.assertTrue(all(item[-3:].startswith("-") for item in literature_ids))

    def test_feasibility_rethink_forces_in_place_question_revision(self) -> None:
        gate = {
            "gate_id": "gate-demo-feasibility-rethink",
            "gate_type": "FEASIBILITY",
            "verdict": "RETHINK_QUESTION",
            "based_on": [
                {"id": "rq-demo-correction", "kind": "ResearchQuestion", "revision": 1},
                {"id": "review-demo-claim-001", "kind": "Review", "revision": 1},
            ],
            "target_refs": [
                {"id": "rq-demo-correction", "kind": "ResearchQuestion", "revision": 1}
            ],
            "target_output_ids": [],
            "review_refs": [
                {"id": "review-demo-claim-001", "kind": "Review", "revision": 1}
            ],
            "rationale": "Narrow the question while preserving accumulated evidence.",
            "recorded_at": "2026-08-21T05:30:00Z",
        }
        self._write_checkpoint(
            state="DISCOVERY_QUESTION",
            route=None,
            active_plan=None,
            branch_states={"theory": "NOT_SELECTED", "experiment": "NOT_SELECTED"},
            skill_progress={},
            gate_results=[gate],
        )
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id="action-question-reframe",
            input_git_commit=self.head,
        )
        self.assertEqual(bundle["allowed_outputs"]["artifact_ids"], ["rq-demo-correction"])
        self.assertFalse(any(
            item.startswith("rq-") for item in bundle["allowed_outputs"]["create_ids"]
        ))

    def test_context_limit_fails_closed_instead_of_truncating(self) -> None:
        with self.assertRaises(ContextLimitError):
            ResearchContextBuilder(self.root, max_artifacts=1).build(
                project_id="proj-demo",
                action_id="action-too-large",
                input_git_commit=self.head,
            )

    def test_synthesis_context_uses_active_materialized_frontier(self) -> None:
        self._write_checkpoint(
            state="SYNTHESIS_BUILD",
            branch_states={"theory": "COMPLETED", "experiment": "COMPLETED"},
            skill_progress={},
        )
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id="action-focused-synthesis",
            input_git_commit=self.head,
        )
        records = {item["id"]: item for item in bundle["artifacts"]}
        self.assertFalse(any(item["kind"] == "Decision" for item in records.values()))
        self.assertEqual(
            [item["id"] for item in records.values() if item["kind"] == "ResearchPlan"],
            ["plan-demo-main"],
        )
        self.assertIn("claim-demo-recoverable-state", records)
        self.assertIn("exp-demo-state-recovery", records)
        self.assertIn("review-demo-claim-001", records)

    def test_completion_context_uses_latest_synthesis_frontier(self) -> None:
        synthesis = copy.deepcopy(
            yaml.safe_load(
                (self.root / "projects/proj-demo/reviews/review-demo-claim-001.yaml").read_text()
            )
        )
        synthesis.update(
            id="review-demo-current-synthesis",
            title="Current synthesis",
            tags=["synthesis"],
            reviewer={
                "reviewer_type": "COMPLETION",
                "actor": {"actor_type": "agent", "actor_id": "research-synthesis"},
            },
            assessment={"scheme": "COMPLETION", "outcome": "INCOMPLETE", "items": []},
            target={
                "artifact_ref": {
                    "id": "claim-demo-recoverable-state",
                    "kind": "ScientificClaim",
                    "revision": 1,
                },
                "git_commit": self.head,
            },
            evidence_resources=[],
            updated_at="2026-08-21T10:00:00Z",
        )
        synthesis["issues"] = []
        synthesis["recommendation"] = "MAJOR_REVISION"
        synthesis["summary"] = "Current bounded synthesis remains incomplete."
        synthesis["provenance"] = {
            "created_by": {"actor_type": "agent", "actor_id": "research-synthesis"},
            "updated_by": {"actor_type": "agent", "actor_id": "research-synthesis"},
        }
        path = self.root / "projects/proj-demo/reviews/review-demo-current-synthesis.yaml"
        path.write_text(yaml.safe_dump(synthesis, sort_keys=False), encoding="utf-8")
        for position in range(70):
            stale = copy.deepcopy(synthesis)
            stale["id"] = f"review-demo-stale-{position:02d}"
            stale["tags"] = ["historical"]
            stale["reviewer"]["actor"]["actor_id"] = "historical-reviewer"
            stale["assessment"]["scheme"] = "THEORY"
            stale["assessment"]["outcome"] = "PASS"
            (self.root / f"projects/proj-demo/reviews/{stale['id']}.yaml").write_text(
                yaml.safe_dump(stale, sort_keys=False), encoding="utf-8"
            )
        self._write_checkpoint(
            state="SYNTHESIS_COMPLETION_GATE",
            branch_states={"theory": "COMPLETED", "experiment": "COMPLETED"},
            skill_progress={},
        )
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id="action-focused-completion",
            input_git_commit=self.head,
        )
        ids = {item["id"] for item in bundle["artifacts"]}
        self.assertIn("review-demo-current-synthesis", ids)
        self.assertIn("plan-demo-main", ids)
        self.assertIn("claim-demo-recoverable-state", ids)
        self.assertFalse(any(item.startswith("review-demo-stale-") for item in ids))
        self.assertLessEqual(len(ids), 64)

    def test_targeted_followup_context_uses_completion_failure_frontier(self) -> None:
        synthesis = copy.deepcopy(
            yaml.safe_load(
                (self.root / "projects/proj-demo/reviews/review-demo-claim-001.yaml").read_text()
            )
        )
        synthesis.update(
            id="review-demo-synthesis-for-followup",
            tags=["synthesis"],
            reviewer={
                "reviewer_type": "COMPLETION",
                "actor": {"actor_type": "agent", "actor_id": "research-synthesis"},
            },
            assessment={"scheme": "COMPLETION", "outcome": "INCOMPLETE", "items": []},
            target={
                "artifact_ref": {
                    "id": "claim-demo-recoverable-state",
                    "kind": "ScientificClaim",
                    "revision": 1,
                },
                "git_commit": self.head,
            },
            issues=[],
            evidence_resources=[],
            recommendation="MAJOR_REVISION",
            summary="Synthesis is incomplete.",
            updated_at="2026-08-21T10:00:00Z",
        )
        synthesis["provenance"] = {
            "created_by": {"actor_type": "agent", "actor_id": "research-synthesis"},
            "updated_by": {"actor_type": "agent", "actor_id": "research-synthesis"},
        }
        synth_path = self.root / "projects/proj-demo/reviews/review-demo-synthesis-for-followup.yaml"
        synth_path.write_text(yaml.safe_dump(synthesis, sort_keys=False), encoding="utf-8")

        gate = copy.deepcopy(synthesis)
        gate.update(
            id="review-demo-completion-gate",
            tags=["completion-gate"],
            reviewer={
                "reviewer_type": "COMPLETION",
                "actor": {"actor_type": "agent", "actor_id": "completion-review"},
            },
            target={
                "artifact_ref": {
                    "id": "review-demo-synthesis-for-followup",
                    "kind": "Review",
                    "revision": 1,
                },
                "git_commit": self.head,
            },
            updated_at="2026-08-21T11:00:00Z",
        )
        gate_path = self.root / "projects/proj-demo/reviews/review-demo-completion-gate.yaml"
        gate_path.write_text(yaml.safe_dump(gate, sort_keys=False), encoding="utf-8")
        for position in range(70):
            stale = copy.deepcopy(gate)
            stale["id"] = f"review-demo-followup-stale-{position:02d}"
            stale["assessment"]["scheme"] = "THEORY"
            stale["assessment"]["outcome"] = "PASS"
            (self.root / f"projects/proj-demo/reviews/{stale['id']}.yaml").write_text(
                yaml.safe_dump(stale, sort_keys=False), encoding="utf-8"
            )
        self._write_checkpoint(
            state="FOLLOWUP_TARGETED",
            branch_states={"theory": "COMPLETED", "experiment": "COMPLETED"},
            skill_progress={},
        )
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id="action-focused-followup",
            input_git_commit=self.head,
        )
        ids = {item["id"] for item in bundle["artifacts"]}
        self.assertIn("review-demo-completion-gate", ids)
        self.assertIn("review-demo-synthesis-for-followup", ids)
        self.assertIn("claim-demo-recoverable-state", ids)
        self.assertFalse(any(item.startswith("review-demo-followup-stale-") for item in ids))
        self.assertLessEqual(len(ids), 64)

    def test_writer_context_cannot_propose_scientific_claims(self) -> None:
        self._write_checkpoint(state="WRITING_DRAFT", skill_progress={})
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id="action-writer-boundary",
            input_git_commit=self.head,
        )
        self.assertEqual(bundle["role"], "scientific-writing")
        self.assertEqual(bundle["allowed_outputs"]["artifact_kinds"], [])
        claim = copy.deepcopy(
            yaml.safe_load(
                (self.root / "projects/proj-demo/claims/claim-demo-recoverable-state.yaml").read_text()
            )
        )
        claim["revision"] += 1
        claim["updated_at"] = "2026-08-21T09:00:00Z"
        service = ArtifactService(self.root)
        proposal = service.stage_proposal(
                proposal_id="proposal-writer-claim",
                project_id="proj-demo",
                action_id="action-writer-boundary",
                operation="REVISE",
                artifact_id=claim["id"],
                kind="ScientificClaim",
                base_revision=1,
                bundle_sha256=bundle["bundle_sha256"],
                proposer_session_id="writer-session",
                candidate=claim,
        )
        with self.assertRaises(RuntimeValidationError):
            service.validate_action(
                "proj-demo", "action-writer-boundary", [proposal.proposal_id]
            )

    def test_ready_experiment_protocol_change_is_rejected(self) -> None:
        progress = {
            "experiment-state-recovery": {
                "track": "experiment",
                "verification_profile": None,
                "artifact_ref": {"id": "exp-demo-state-recovery", "kind": "Experiment", "revision": 1},
                "required_skills": ["experiment-design", "experiment-execution", "experiment-verification"],
                "completed_skills": ["experiment-design"],
                "active_skill": None,
                "status": "IN_PROGRESS",
            }
        }
        self._write_checkpoint(
            state="EXECUTION_TRACKS",
            branch_states={"theory": "NOT_SELECTED", "experiment": "RUN"},
            skill_progress=progress,
        )
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id="action-protocol-lock",
            input_git_commit=self.head,
            track="experiment",
        )
        execution_records = {
            item["id"]: item for item in bundle["artifacts"]
        }
        self.assertFalse(any(
            item["kind"] == "Decision" for item in execution_records.values()
        ))
        self.assertEqual(
            [
                item["id"] for item in execution_records.values()
                if item["kind"] == "ResearchPlan"
            ],
            ["plan-demo-main"],
        )
        experiment = copy.deepcopy(
            yaml.safe_load(
                (self.root / "projects/proj-demo/experiments/exp-demo-state-recovery.yaml").read_text()
            )
        )
        experiment["revision"] += 1
        experiment["updated_at"] = "2026-08-21T09:00:00Z"
        experiment["method"] = "Changed after protocol lock"
        proposal = ArtifactService(self.root).stage_proposal(
            proposal_id="proposal-protocol-change",
            project_id="proj-demo",
            action_id="action-protocol-lock",
            operation="REVISE",
            artifact_id=experiment["id"],
            kind="Experiment",
            base_revision=1,
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="experiment-session",
            candidate=experiment,
        )
        with self.assertRaises(RuntimeValidationError):
            ArtifactService(self.root).validate_action(
                "proj-demo", "action-protocol-lock", [proposal.proposal_id]
            )

    def test_theory_developer_cannot_self_mark_claim_verified(self) -> None:
        progress = {
            "theory-recoverable-state": {
                "track": "theory",
                "verification_profile": "ADVERSARIAL",
                "artifact_ref": {
                    "id": "claim-demo-recoverable-state",
                    "kind": "ScientificClaim",
                    "revision": 1,
                },
                "required_skills": ["theory-development", "theory-verification"],
                "completed_skills": [],
                "active_skill": None,
                "status": "PENDING",
            }
        }
        self._write_checkpoint(
            state="EXECUTION_TRACKS",
            branch_states={"theory": "DEVELOP", "experiment": "NOT_SELECTED"},
            skill_progress=progress,
        )
        claim_path = self.root / "projects/proj-demo/claims/claim-demo-recoverable-state.yaml"
        claim = yaml.safe_load(claim_path.read_text(encoding="utf-8"))
        claim["status"] = "SUPPORTED"
        claim_path.write_text(yaml.safe_dump(claim, sort_keys=False), encoding="utf-8")
        self._git(
            "add",
            "projects/proj-demo/orchestrator/state.yaml",
            "projects/proj-demo/claims/claim-demo-recoverable-state.yaml",
        )
        self._git("commit", "-m", "prepare theory developer boundary")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        action_id = "action-theory-no-self-verify"
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id=action_id,
            input_git_commit=self.head,
            track="theory",
        )
        candidate = copy.deepcopy(claim)
        candidate["status"] = "VERIFIED"
        candidate["revision"] = 2
        candidate["updated_at"] = "2026-08-21T09:00:00Z"
        candidate["provenance"]["updated_by"] = {
            "actor_type": "agent",
            "actor_id": "theory-development",
            "session_id": "theory-developer-session",
        }
        proposal = ArtifactService(self.root).stage_proposal(
            proposal_id="proposal-theory-self-verify",
            project_id="proj-demo",
            action_id=action_id,
            operation="REVISE",
            artifact_id=candidate["id"],
            kind="ScientificClaim",
            base_revision=1,
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="theory-developer-session",
            candidate=candidate,
        )
        with self.assertRaises(RuntimeValidationError):
            ArtifactService(self.root).validate_action(
                "proj-demo", action_id, [proposal.proposal_id]
            )

    def test_writer_resource_action_commits_paper_and_checkpoint(self) -> None:
        self._write_checkpoint(state="WRITING_DRAFT", skill_progress={})
        self._git("add", "projects/proj-demo/orchestrator/state.yaml")
        self._git("commit", "-m", "prepare writing fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        action_id = "action-writing-resource"
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id=action_id,
            input_git_commit=self.head,
        )
        draft = self.root / "projects/proj-demo/paper/manuscript/draft.md"
        draft.write_text(
            draft.read_text(encoding="utf-8") + "\nTraceable theorem statement.\n",
            encoding="utf-8",
        )
        trace = self.root / "projects/proj-demo/paper/traceability.yaml"
        trace.write_text(
            yaml.safe_dump(
                {
                    "traceability_version": "research-traceability/v0.1",
                    "project_id": "proj-demo",
                    "manuscript_paths": ["paper/manuscript/draft.md"],
                    "entries": [
                        {
                            "anchor": "theorem:recoverable-state",
                            "statement_type": "THEOREM",
                            "source_refs": [
                                {
                                    "id": "claim-demo-recoverable-state",
                                    "kind": "ScientificClaim",
                                    "revision": 1,
                                }
                            ],
                            "resource_refs": [],
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        ArtifactService(self.root).submit_action(
            project_id="proj-demo",
            action_id=action_id,
            bundle_sha256=bundle["bundle_sha256"],
            proposal_ids=[],
            outcome="SUBMITTED",
        )
        result = ScientificTransaction(self.root).promote_action(
            project_id="proj-demo",
            action_id=action_id,
            expected_git_commit=self.head,
            expected_checkpoint_seq=12,
            event={"type": "WRITING_COMPLETED", "payload": {"action_id": action_id}},
            commit=True,
            push=False,
        )
        self.assertEqual(result["artifact_ids"], [])
        changed = self._git("show", "--pretty=", "--name-only", "HEAD").stdout
        self.assertIn("projects/proj-demo/paper/traceability.yaml", changed)
        self.assertIn("projects/proj-demo/paper/manuscript/draft.md", changed)
        self.assertIn("projects/proj-demo/orchestrator/state.yaml", changed)
        written_claim = yaml.safe_load(
            (self.root / "projects/proj-demo/claims/claim-demo-recoverable-state.yaml").read_text()
        )
        self.assertEqual(written_claim["status"], "IN_PAPER")
        self.assertEqual(
            written_claim["paper_locations"][0]["anchor"],
            "theorem:recoverable-state",
        )

    def test_passing_verifier_promotes_claim_host_side(self) -> None:
        progress = {
            "theory-recoverable-state": {
                "track": "theory",
                "verification_profile": "ADVERSARIAL",
                "artifact_ref": {
                    "id": "claim-demo-recoverable-state",
                    "kind": "ScientificClaim",
                    "revision": 1,
                },
                "required_skills": ["theory-development", "theory-verification"],
                "completed_skills": ["theory-development"],
                "active_skill": None,
                "status": "IN_PROGRESS",
            }
        }
        self._write_checkpoint(
            state="EXECUTION_TRACKS",
            branch_states={"theory": "VERIFY", "experiment": "NOT_SELECTED"},
            skill_progress=progress,
        )
        claim_path = self.root / "projects/proj-demo/claims/claim-demo-recoverable-state.yaml"
        claim_fixture = yaml.safe_load(claim_path.read_text(encoding="utf-8"))
        # The theory-development contract completes at FORMALIZED or
        # SUPPORTED; an independent PASS Review is what authorizes VERIFIED.
        claim_fixture["status"] = "FORMALIZED"
        claim_path.write_text(
            yaml.safe_dump(claim_fixture, sort_keys=False), encoding="utf-8"
        )
        plan_path = self.root / "projects/proj-demo/plans/plan-demo-main.yaml"
        plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
        plan["status"] = "IN_PROGRESS"
        for package in plan["work_packages"]:
            if package["track"] == "THEORY":
                package["status"] = "IN_PROGRESS"
        plan_path.write_text(
            yaml.safe_dump(plan, sort_keys=False), encoding="utf-8"
        )
        self._git(
            "add",
            "projects/proj-demo/orchestrator/state.yaml",
            "projects/proj-demo/claims/claim-demo-recoverable-state.yaml",
            "projects/proj-demo/plans/plan-demo-main.yaml",
        )
        self._git("commit", "-m", "prepare verifier fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        action_id = "action-theory-verifier"
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo",
            action_id=action_id,
            input_git_commit=self.head,
            track="theory",
        )
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review_candidate(revision=1)
        review["id"] = review_id
        review["title"] = "Independent passing theory review"
        review["created_at"] = review["updated_at"]
        service = ArtifactService(self.root)
        proposal = service.stage_proposal(
            proposal_id="proposal-theory-pass",
            project_id="proj-demo",
            action_id=action_id,
            operation="CREATE",
            artifact_id=review_id,
            kind="Review",
            base_revision=None,
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="independent-theory-session",
            candidate=review,
        )
        service.submit_action(
            project_id="proj-demo",
            action_id=action_id,
            bundle_sha256=bundle["bundle_sha256"],
            proposal_ids=[proposal.proposal_id],
            outcome="SUBMITTED",
        )
        ScientificTransaction(self.root).promote_action(
            project_id="proj-demo",
            action_id=action_id,
            expected_git_commit=self.head,
            expected_checkpoint_seq=12,
            event={
                "type": "SKILL_COMPLETED",
                "payload": {
                    "action_id": action_id,
                    "output_id": "theory-recoverable-state",
                    "skill_id": "theory-verification",
                    "artifact_ids": [review_id],
                },
            },
            commit=True,
            push=False,
        )
        claim = yaml.safe_load(
            (self.root / "projects/proj-demo/claims/claim-demo-recoverable-state.yaml").read_text()
        )
        self.assertEqual(claim["status"], "VERIFIED")
        self.assertEqual(claim["revision"], 2)
        self.assertTrue(claim["verification"]["review_refs"])

    def test_controller_materializes_planned_outputs_without_agent(self) -> None:
        plan_path = self.root / "projects/proj-demo/plans/plan-demo-main.yaml"
        plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
        plan["status"] = "IN_PROGRESS"
        for package in plan["work_packages"]:
            if package["track"] in {"THEORY", "EXPERIMENT"}:
                package["materialized_outputs"] = []
                package["status"] = "IN_PROGRESS"
        theory_package = next(
            package for package in plan["work_packages"] if package["track"] == "THEORY"
        )
        theory_package["planned_outputs"].append(
            {
                "local_id": "independent-review-obligation",
                "kind": "Review",
                "description": "Review generated by the verification pipeline.",
                "required": True,
                "depends_on_outputs": ["theory-recoverable-state"],
            }
        )
        plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        self._write_checkpoint(
            state="EXECUTION_MATERIALIZE",
            route="MIXED",
            branch_states={"theory": "NOT_SELECTED", "experiment": "NOT_SELECTED"},
            skill_progress={},
        )
        self._git(
            "add",
            "projects/proj-demo/plans/plan-demo-main.yaml",
            "projects/proj-demo/orchestrator/state.yaml",
        )
        self._git("commit", "-m", "prepare materialization fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        result = ScientificTransaction(self.root).materialize_outputs(
            project_id="proj-demo",
            action_id="action-materialize",
            expected_git_commit=self.head,
            expected_checkpoint_seq=12,
            commit=True,
            push=False,
        )
        repeated = ScientificTransaction(self.root).materialize_outputs(
            project_id="proj-demo",
            action_id="action-materialize",
            expected_git_commit=self.head,
            expected_checkpoint_seq=12,
            commit=True,
            push=False,
        )
        self.assertEqual(result, repeated)
        self.assertEqual(
            result["artifact_ids"],
            [
                "claim-demo-theory-recoverable-state",
                "exp-demo-experiment-state-recovery",
            ],
        )
        self.assertEqual(result["checkpoint"]["state"], "EXECUTION_TRACKS")
        self.assertEqual(
            set(result["checkpoint"]["skill_progress"]),
            {"theory-recoverable-state", "experiment-state-recovery"},
        )
        self.assertTrue(
            (self.root / "projects/proj-demo/claims/claim-demo-theory-recoverable-state.yaml").is_file()
        )

    def test_materialization_rejects_existing_artifact_id(self) -> None:
        plan_path = self.root / "projects/proj-demo/plans/plan-demo-main.yaml"
        plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
        plan["status"] = "IN_PROGRESS"
        for package in plan["work_packages"]:
            package["materialized_outputs"] = []
            if package["track"] in {"THEORY", "EXPERIMENT"}:
                package["status"] = "IN_PROGRESS"
        theory_package = next(
            package for package in plan["work_packages"]
            if package["track"] == "THEORY"
        )
        theory_package["planned_outputs"][0]["local_id"] = "recoverable-state"
        plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
        self._write_checkpoint(
            state="EXECUTION_MATERIALIZE",
            route="MIXED",
            branch_states={"theory": "NOT_SELECTED", "experiment": "NOT_SELECTED"},
            skill_progress={},
        )
        self._git(
            "add",
            "projects/proj-demo/plans/plan-demo-main.yaml",
            "projects/proj-demo/orchestrator/state.yaml",
        )
        self._git("commit", "-m", "prepare duplicate materialization fixture")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        with self.assertRaisesRegex(
            RuntimeValidationError, "materialized artifact ID already exists"
        ):
            ScientificTransaction(self.root).materialize_outputs(
                project_id="proj-demo",
                action_id="action-materialize-duplicate",
                expected_git_commit=head,
                expected_checkpoint_seq=12,
                commit=True,
                push=False,
            )
    def test_revision_conflict_is_detected_against_canonical_state(self) -> None:
        bundle = self._build_context("action-conflict")
        service = ArtifactService(self.root)
        service.stage_proposal(
                proposal_id="proposal-conflict",
                project_id="proj-demo",
                action_id="action-conflict",
                operation="REVISE",
                artifact_id="review-demo-claim-001",
                kind="Review",
                base_revision=99,
                bundle_sha256=bundle["bundle_sha256"],
                proposer_session_id="session-conflict",
                candidate=self._review_candidate(),
        )
        with self.assertRaises(RevisionConflict):
            service.validate_action(
                "proj-demo", "action-conflict", ["proposal-conflict"]
            )

    def test_final_pass_review_must_be_resolved(self) -> None:
        self._write_checkpoint(state="WRITING_FINAL_REVIEW")
        self._git("add", "projects/proj-demo/orchestrator/state.yaml")
        self._git("commit", "-m", "prepare final review fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.strip()
        action_id = "action-final-review-status"
        bundle = ResearchContextBuilder(self.root).build(
            project_id="proj-demo", action_id=action_id,
            input_git_commit=self.head,
        )
        self.assertEqual(bundle["role"], "final-review")
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        candidate = self._review_candidate(revision=1)
        candidate.update({
            "id": review_id,
            "status": "OPEN",
            "recommendation": "ACCEPT",
            "issues": [],
            "assessment": {"scheme": "FINAL", "outcome": "PASS", "items": []},
        })
        candidate["reviewer"]["reviewer_type"] = "FINAL"
        service = ArtifactService(self.root)
        open_proposal = service.stage_proposal(
            proposal_id="proposal-final-pass-open",
            project_id="proj-demo", action_id=action_id, operation="CREATE",
            artifact_id=review_id, kind="Review", base_revision=None,
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="independent-final-session", candidate=candidate,
        )
        with self.assertRaisesRegex(
            RuntimeValidationError, "FINAL PASS Review must have top-level status RESOLVED"
        ):
            service.validate_action(
                "proj-demo", action_id, [open_proposal.proposal_id]
            )

        candidate["status"] = "RESOLVED"
        resolved_proposal = service.stage_proposal(
            proposal_id="proposal-final-pass-resolved",
            project_id="proj-demo", action_id=action_id, operation="CREATE",
            artifact_id=review_id, kind="Review", base_revision=None,
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="independent-final-session-2", candidate=candidate,
        )
        self.assertTrue(service.validate_action(
            "proj-demo", action_id, [resolved_proposal.proposal_id]
        )["valid"])

    def test_staged_revision_promotes_once_and_preserves_unrelated_changes(self) -> None:
        action_id = "action-promote"
        bundle = self._build_context(action_id)
        service = ArtifactService(self.root)
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        candidate = self._review_candidate(revision=1)
        candidate["id"] = review_id
        candidate["title"] = "Completion review for transaction test"
        candidate["created_at"] = candidate["updated_at"]
        candidate["reviewer"]["reviewer_type"] = "COMPLETION"
        candidate["assessment"] = {
            "scheme": "COMPLETION",
            "outcome": "COMPLETE",
            "items": [],
        }
        proposal = service.stage_proposal(
            proposal_id="proposal-completion-review",
            project_id="proj-demo",
            action_id=action_id,
            operation="CREATE",
            artifact_id=review_id,
            kind="Review",
            base_revision=None,
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="session-promote",
            candidate=candidate,
        )
        validation = service.validate_action(
            "proj-demo", action_id, [proposal.proposal_id]
        )
        self.assertEqual(
            validation["artifact_refs"],
            [{"id": review_id, "kind": "Review", "revision": 1}],
        )
        service.submit_action(
            project_id="proj-demo",
            action_id=action_id,
            bundle_sha256=bundle["bundle_sha256"],
            proposal_ids=[proposal.proposal_id],
            outcome="SUBMITTED",
        )
        unrelated = self.root / "notes-from-user.txt"
        unrelated.write_text("leave me alone\n", encoding="utf-8")

        transaction = ScientificTransaction(self.root)
        first = transaction.promote_action(
            project_id="proj-demo",
            action_id=action_id,
            expected_git_commit=self.head,
            commit=True,
            push=False,
        )
        second = transaction.promote_action(
            project_id="proj-demo",
            action_id=action_id,
            expected_git_commit=self.head,
            commit=True,
            push=False,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["artifact_ids"], [review_id])
        self.assertEqual(
            yaml.safe_load(
                (self.root / f"projects/proj-demo/reviews/{review_id}.yaml").read_text(
                    encoding="utf-8"
                )
            )["revision"],
            1,
        )
        changed = self._git("show", "--pretty=", "--name-only", "HEAD").stdout.splitlines()
        self.assertEqual(changed, [f"projects/proj-demo/reviews/{review_id}.yaml"])
        self.assertTrue(unrelated.is_file())
        self.assertIn("notes-from-user.txt", self._git("status", "--short").stdout)

    def test_sidecar_protocol_has_structured_success_and_error(self) -> None:
        server = SidecarServer(self.root)
        health = server.dispatch(
            {
                "protocol": "research-sidecar/v0.1",
                "request_id": "request-health",
                "method": "health",
                "params": {},
            }
        )
        self.assertTrue(health["ok"])
        self.assertEqual(health["result"]["protocol"], "research-sidecar/v0.1")

        error = server.dispatch(
            {
                "protocol": "research-sidecar/v0.1",
                "request_id": "request-error",
                "method": "artifact.promote_from_model",
                "params": {},
            }
        )
        self.assertFalse(error["ok"])
        self.assertEqual(error["error"]["code"], "INTERNAL")

    def test_sidecar_jsonl_process_round_trip_and_shutdown(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-m", "research_artifacts.sidecar", str(self.root)],
            cwd=REPOSITORY_ROOT,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIsNotNone(process.stdin)
        self.assertIsNotNone(process.stdout)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps({
            "protocol": "research-sidecar/v0.1",
            "request_id": "process-health",
            "method": "health",
            "params": {},
        }) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        self.assertTrue(response["ok"])
        process.stdin.write(json.dumps({
            "protocol": "research-sidecar/v0.1",
            "request_id": "process-shutdown",
            "method": "shutdown",
            "params": {},
        }) + "\n")
        process.stdin.flush()
        shutdown = json.loads(process.stdout.readline())
        self.assertTrue(shutdown["ok"])
        process.stdin.close()
        self.assertEqual(process.wait(timeout=5), 0)
        process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    def test_agent_cannot_create_an_artifact_outside_context_allowlist(self) -> None:
        bundle = self._build_context("action-create-denied")
        source = yaml.safe_load(
            (self.root / "projects/proj-demo/reviews/review-demo-claim-001.yaml").read_text(
                encoding="utf-8"
            )
        )
        source["id"] = "review-demo-not-authorized"
        source["title"] = "Unauthorized candidate"
        source["revision"] = 1
        service = ArtifactService(self.root)
        service.stage_proposal(
            proposal_id="proposal-create-denied",
            project_id="proj-demo",
            action_id="action-create-denied",
            operation="CREATE",
            artifact_id=source["id"],
            kind="Review",
            base_revision=None,
            bundle_sha256=bundle["bundle_sha256"],
            proposer_session_id="session-create-denied",
            candidate=source,
        )
        with self.assertRaises(RuntimeValidationError):
            service.validate_action(
                "proj-demo", "action-create-denied", ["proposal-create-denied"]
            )

    def test_control_events_commit_project_status_and_cancellation_decision(self) -> None:
        transaction = ScientificTransaction(self.root)
        paused = transaction.apply_control_event(
            project_id="proj-demo",
            event={"type": "USER_PAUSE", "payload": {"reason": "pause test"}},
            expected_git_commit=self.head,
            expected_seq=12,
        )
        self.assertEqual(paused["checkpoint"]["state"], "PAUSED_BY_USER")
        project = yaml.safe_load(
            (self.root / "projects/proj-demo/project.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(project["status"], "PAUSED")

        resumed = transaction.apply_control_event(
            project_id="proj-demo",
            event={"type": "RESUME", "payload": {}},
            expected_git_commit=paused["commit_hash"],
            expected_seq=13,
        )
        cancelled = transaction.apply_control_event(
            project_id="proj-demo",
            event={"type": "USER_CANCEL", "payload": {"reason": "cancel test"}},
            expected_git_commit=resumed["commit_hash"],
            expected_seq=14,
        )
        self.assertEqual(cancelled["checkpoint"]["state"], "CANCELLED_BY_USER")
        project = yaml.safe_load(
            (self.root / "projects/proj-demo/project.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(project["status"], "ARCHIVED")
        decisions = list((self.root / "projects/proj-demo/decisions").glob("*user-cancel*.yaml"))
        self.assertEqual(len(decisions), 1)
        self.assertEqual(yaml.safe_load(decisions[0].read_text(encoding="utf-8"))["status"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
