from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from research_artifacts import ArtifactService, ArtifactWorkspace
from research_artifacts.context_builder import ResearchContextBuilder
from research_artifacts.golden_pilots import GoldenPilotRuntime
from research_artifacts.mvp import MVPController, SyntheticSkillRuntime
from research_artifacts.orchestrator import InvalidEvent, OrchestratorEvent, StateReducer, new_checkpoint, utc_now
from research_artifacts.runtime_types import GitConflict, RuntimeValidationError, atomic_write_json
from research_artifacts.sidecar import SidecarServer
from research_artifacts.tool_adapters import ExperimentExecutionAdapter, LeanToolAdapter
from research_artifacts.transactions import ScientificTransaction
from research_evals import ScientificEvalRunner


ROOT = Path(__file__).resolve().parents[1]


class RaisingRuntime:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def execute(self, _action, _bundle):
        self.calls += 1
        raise self.error


class FailedRuntime:
    def execute(self, action, bundle):
        return {
            "action_id": action["action_id"],
            "bundle_sha256": bundle["bundle_sha256"],
            "proposal_ids": [],
            "outcome": "FAILED",
            "failure_classification": "LOCAL_RETRY",
        }


class HardeningTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        shutil.copytree(ROOT / "config", self.root / "config")
        skills = self.root / "integrations/deepseek-harness/skills"
        skills.parent.mkdir(parents=True)
        shutil.copytree(ROOT / "integrations/deepseek-harness/skills", skills)
        (self.root / "projects").mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Failure Injection")
        self._git("config", "user.email", "failure@example.invalid")
        self._git("add", "schemas", "integrations", "projects")
        self._git("commit", "-m", "failure fixture baseline")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str, check: bool = True):
        return subprocess.run(
            ["git", *args], cwd=self.root, text=True,
            capture_output=True, check=check,
        )

    def _controller(self, runtime=None, slug="hardening") -> tuple[MVPController, str]:
        controller = MVPController(
            self.root, runtime if runtime is not None else SyntheticSkillRuntime(self.root)
        )
        project_id = f"proj-{slug}"
        controller.init(project_id, "Failure recovery test")
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        controller.budgets.approve_initial(project_id, checkpoint["run_id"], 100)
        return controller, project_id

    def _experiment(self, directory: Path) -> dict:
        dataset = directory / "dataset.json"
        dataset.write_text("{\"values\":[1,2,3]}\n", encoding="utf-8")
        experiment = {
            "id": "exp-hardening",
            "revision": 2,
            "hypothesis": {"claim_ref": {"id": "claim-hardening", "kind": "ScientificClaim", "revision": 1}, "operationalization": "fixed"},
            "method": "fixed",
            "baselines": [{"name": "baseline", "version": "1", "configuration": {}}],
            "datasets": [{"name": "data", "version": "1", "uri": str(dataset), "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(), "split_definition": "all"}],
            "metrics": [{"name": "m", "definition": "fixed", "direction": "HIGHER_BETTER", "aggregation": "mean"}],
            "configuration": {"parameters": {}, "environment": "test-env"},
            "interpretation_plan": {"supported_when": ["m>0"], "contradicted_when": ["m<0"], "inconclusive_when": ["m=0"], "analysis_plan": "fixed", "seeds": [7], "repetitions": 1, "hyperparameter_budget": "none", "ablations": [], "statistical_procedure": "fixed", "exclusions": []},
            "code_location": {"repository": str(directory), "git_commit": "a" * 40, "entrypoint": "/bin/true"},
            "runs": [],
        }
        experiment["protocol_lock"] = {
            "revision": 2, "sha256": ExperimentExecutionAdapter.protocol_hash(experiment),
            "locked_at": "2026-08-21T00:00:00Z",
        }
        # protocol_lock is intentionally excluded by protocol_hash.
        experiment["protocol_lock"]["sha256"] = ExperimentExecutionAdapter.protocol_hash(experiment)
        return experiment

    def test_model_timeout_retries_then_blocks(self) -> None:
        runtime = RaisingRuntime(TimeoutError("model timeout"))
        controller, project_id = self._controller(runtime, "model-timeout")
        stop = controller.run(project_id, max_actions=10)
        self.assertEqual(stop["reason"], "BLOCKED")
        self.assertEqual(stop["checkpoint"]["retry_counters"]["action"], 3)
        self.assertFalse(ArtifactWorkspace(self.root).query(kind="ResearchQuestion", project_id=project_id))

    def test_dependency_unavailable_retries_then_blocks(self) -> None:
        controller, project_id = self._controller(
            RaisingRuntime(FileNotFoundError("tool unavailable")), "dependency"
        )
        self.assertEqual(controller.run(project_id, max_actions=10)["reason"], "BLOCKED")

    def test_agent_cancellation_does_not_advance(self) -> None:
        controller, project_id = self._controller(FailedRuntime(), "cancelled-agent")
        controller.run(project_id, max_actions=1)
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        self.assertEqual(checkpoint["state"], "DISCOVERY_QUESTION")
        self.assertEqual(checkpoint["retry_counters"]["action"], 1)

    def test_agent_crash_resumes_same_pending_action(self) -> None:
        controller, project_id = self._controller(
            RaisingRuntime(KeyboardInterrupt()), "agent-crash"
        )
        with self.assertRaises(KeyboardInterrupt):
            controller.run(project_id, max_actions=1)
        pending = controller.transactions.orchestrator.store.load(project_id)["pending_action"]
        self.assertIsNotNone(pending)
        controller.runtime = SyntheticSkillRuntime(self.root)
        controller.run(project_id, max_actions=1)
        resumed = controller.transactions.orchestrator.store.load(project_id)
        self.assertEqual(resumed["last_completed_action"], pending["action_id"])
        self.assertIsNone(resumed["pending_action"])

    def test_crash_before_dispatch_preserves_pending_action(self) -> None:
        controller, project_id = self._controller(slug="before-dispatch")
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        head = self._git("rev-parse", "HEAD").stdout.strip()
        begun = controller.transactions.begin_action(
            project_id=project_id, action_id="before-dispatch-action",
            input_git_commit=head, expected_seq=checkpoint["checkpoint_seq"], commit=True,
        )["checkpoint"]
        self.assertEqual(begun["state"], checkpoint["state"])
        self.assertEqual(begun["pending_action"]["action_id"], "before-dispatch-action")

    def test_pause_resume_preserves_pending_scientific_state(self) -> None:
        controller, project_id = self._controller(slug="pause-resume")
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        head = self._git("rev-parse", "HEAD").stdout.strip()
        checkpoint = controller.transactions.begin_action(
            project_id=project_id, action_id="pending-pause",
            input_git_commit=head, expected_seq=checkpoint["checkpoint_seq"], commit=True,
        )["checkpoint"]
        controller.pause(project_id, "user pause")
        paused = controller.transactions.orchestrator.store.load(project_id)
        self.assertEqual(paused["pending_action"], checkpoint["pending_action"])
        controller.resume(project_id)
        resumed = controller.transactions.orchestrator.store.load(project_id)
        self.assertEqual(resumed["pending_action"], checkpoint["pending_action"])
        self.assertEqual(resumed["state"], "DISCOVERY_QUESTION")

    def test_cancel_is_terminal_and_distinct(self) -> None:
        controller, project_id = self._controller(slug="human-cancel")
        controller.cancel(project_id, "permanent stop")
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        self.assertEqual(checkpoint["state"], "CANCELLED_BY_USER")
        with self.assertRaises(InvalidEvent):
            controller.resume(project_id)

    def test_corrupted_checkpoint_recovers_from_git_into_blocked(self) -> None:
        controller, project_id = self._controller(slug="corrupt-state")
        path = controller.transactions.orchestrator.store.path_for(project_id)
        path.write_text("not: [valid", encoding="utf-8")
        status = controller.status(project_id)
        self.assertEqual(status["state"], "BLOCKED")
        self.assertTrue(list((self.root / ".harness/research/quarantine" / project_id).glob("state-*.yaml")))
        project = ArtifactWorkspace(self.root).get(project_id)
        self.assertEqual(project.data["status"], "BLOCKED")

    def test_unblock_requires_explicit_recovery_decision(self) -> None:
        controller, project_id = self._controller(
            RaisingRuntime(TimeoutError("timeout")), "explicit-unblock"
        )
        controller.run(project_id, max_actions=10)
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        with self.assertRaises(InvalidEvent):
            controller._event(project_id, "UNBLOCK", {"reason": "no decision"})  # noqa: SLF001
        controller.unblock(project_id, "dependency restored and verified")
        resumed = controller.transactions.orchestrator.store.load(project_id)
        self.assertEqual(resumed["state"], "DISCOVERY_QUESTION")
        self.assertNotIn("action", resumed["retry_counters"])
        decisions = ArtifactWorkspace(self.root).query(kind="Decision", project_id=project_id)
        self.assertTrue(any("recovery" in item.data.get("tags", []) for item in decisions))

    def test_scope_transition_resets_transient_action_retry_budget(self) -> None:
        checkpoint = new_checkpoint(
            "proj-retry-scope", "run-retry-scope", "0" * 40,
        )
        checkpoint["state"] = "WRITING_FINAL_APPROVAL"
        checkpoint["retry_counters"]["action"] = 3
        reduction = StateReducer().reduce(
            checkpoint,
            OrchestratorEvent(
                "FINAL_REVISION_REQUIRED", utc_now(), {"reason": "scientific revision"},
            ),
        )
        updated = reduction.checkpoint
        self.assertEqual(updated["state"], "WRITING_TARGETED_REVISION")
        self.assertNotIn("action", updated["retry_counters"])

    def test_dirty_transaction_target_fails_closed(self) -> None:
        controller, project_id = self._controller(slug="dirty-target")
        path = controller.transactions.orchestrator.store.path_for(project_id)
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        with self.assertRaises(GitConflict):
            controller.transactions.apply_control_event(
                project_id=project_id,
                event={"type": "USER_PAUSE", "payload": {"reason": "test"}},
                expected_git_commit=self._git("rev-parse", "HEAD").stdout.strip(),
                expected_seq=checkpoint["checkpoint_seq"], commit=True,
            )

    def test_unrelated_staged_change_is_preserved(self) -> None:
        controller, project_id = self._controller(slug="dirty-unrelated")
        unrelated = self.root / "notes.txt"
        unrelated.write_text("user work\n", encoding="utf-8")
        self._git("add", "notes.txt")
        controller.pause(project_id, "test")
        status = self._git("status", "--short").stdout
        self.assertIn("A  notes.txt", status)

    def test_git_commit_failure_rolls_back_control_transaction(self) -> None:
        controller, project_id = self._controller(slug="commit-failure")
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        before = copy.deepcopy(checkpoint)
        with patch.object(
            controller.transactions, "_commit", side_effect=GitConflict("injected commit failure")
        ):
            with self.assertRaises(GitConflict):
                controller.transactions.apply_control_event(
                    project_id=project_id,
                    event={"type": "USER_PAUSE", "payload": {"reason": "test"}},
                    expected_git_commit=self._git("rev-parse", "HEAD").stdout.strip(),
                    expected_seq=checkpoint["checkpoint_seq"], commit=True,
                )
        self.assertEqual(controller.transactions.orchestrator.store.load(project_id), before)

    def test_prepared_partial_transaction_rolls_back(self) -> None:
        controller, project_id = self._controller(slug="partial")
        tx = controller.transactions
        project_path = self.root / ArtifactWorkspace(self.root).get(project_id).path
        backup = tx._backup_values([project_path])  # noqa: SLF001
        project_path.write_text("corrupted partial write\n", encoding="utf-8")
        action_id = "injected-partial-promotion"
        journal = {
            "transaction_version": "research-transaction/v0.1",
            "transaction_id": "partial-test",
            "project_id": project_id,
            "action_id": action_id,
            "status": "PREPARED",
            "paths": [str(project_path.relative_to(self.root))],
            "backups": backup,
            "commit_hash": None,
            "prepared_at": utc_now(),
        }
        journal_path = self.root / ".harness/research/journals/partial-test.json"
        atomic_write_json(journal_path, journal)
        self.assertEqual(tx.recover(project_id)[0]["outcome"], "rolled_back")
        ArtifactWorkspace(self.root).require_valid()

    def test_committed_journal_recovers_after_later_commit(self) -> None:
        controller, project_id = self._controller(slug="journal-finish")
        path = self.root / "projects" / project_id / "resources" / "accepted.txt"
        path.parent.mkdir(parents=True)
        path.write_text("accepted\n", encoding="utf-8")
        action_id = "committed-before-journal-finalization"
        self._git("add", str(path.relative_to(self.root)))
        self._git("commit", "-m", f"research({project_id}): accept {action_id}")
        accepted_commit = self._git("rev-parse", "HEAD").stdout.strip()
        other = self.root / "later.txt"; other.write_text("later\n", encoding="utf-8")
        self._git("add", "later.txt"); self._git("commit", "-m", "later unrelated commit")
        journal_path = self.root / ".harness/research/journals/finish-test.json"
        atomic_write_json(journal_path, {
            "transaction_version": "research-transaction/v0.1",
            "transaction_id": "finish-test", "project_id": project_id,
            "action_id": action_id, "status": "PREPARED",
            "paths": [str(path.relative_to(self.root))],
            "backups": {str(path.relative_to(self.root)): {"exists": False, "content_base64": None}},
            "commit_hash": None, "prepared_at": utc_now(),
        })
        self.assertEqual(controller.transactions.recover(project_id)[0]["outcome"], "finished")
        recovered = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(recovered["commit_hash"], accepted_commit)
        self.assertTrue(path.is_file())

    def test_remote_push_failure_keeps_local_commit(self) -> None:
        controller, _project_id = self._controller(slug="push-failure")
        head = self._git("rev-parse", "HEAD").stdout.strip()
        with patch.object(controller.transactions, "_push", return_value=(False, "offline")):
            result = controller.transactions.push_head(attempts=1)
        self.assertEqual(result["commit_hash"], head)
        self.assertTrue(result["push_pending"])

    def test_sidecar_crash_restart_preserves_state(self) -> None:
        controller, project_id = self._controller(slug="sidecar-crash")
        expected = controller.transactions.orchestrator.store.load(project_id)
        first = SidecarServer(self.root).dispatch({
            "protocol": "research-sidecar/v0.1", "request_id": "before",
            "method": "orchestrator.inspect", "params": {"project_id": project_id},
        })
        self.assertTrue(first["ok"])
        # A fresh process/server reconstructs only from canonical Git/artifacts.
        second = SidecarServer(self.root).dispatch({
            "protocol": "research-sidecar/v0.1", "request_id": "after",
            "method": "orchestrator.inspect", "params": {"project_id": project_id},
        })
        self.assertEqual(second["result"], expected)

    def test_staged_proposal_is_never_canonical_without_promotion(self) -> None:
        controller, project_id = self._controller(slug="staged-only")
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        action_id = "staged-crash"
        head = self._git("rev-parse", "HEAD").stdout.strip()
        controller.transactions.begin_action(
            project_id=project_id, action_id=action_id,
            input_git_commit=head, expected_seq=checkpoint["checkpoint_seq"], commit=True,
        )
        bundle = ResearchContextBuilder(self.root).build(
            project_id=project_id, action_id=action_id, input_git_commit=head,
        )
        question_id = bundle["allowed_outputs"]["create_ids"][0]
        candidate = SyntheticSkillRuntime(self.root)._envelope(  # noqa: SLF001
            {"project_id": project_id, "action_id": action_id, "skill": "question-framing"},
            "ResearchQuestion", question_id, "staged", "DRAFT",
        )
        candidate.update({
            "status": "DRAFT", "problem_definition": "staged only",
            "background": "test", "scope": {"included": ["test"], "excluded": []},
            "known_results": [], "research_gap": "test",
            "hypotheses": [{"statement": "test"}],
            "expected_contributions": ["test"], "risks": [],
        })
        ArtifactService(self.root).stage_proposal(
            proposal_id="proposal-staged", project_id=project_id,
            action_id=action_id, operation="CREATE", artifact_id=question_id,
            kind="ResearchQuestion", base_revision=None,
            bundle_sha256=bundle["bundle_sha256"], proposer_session_id="crashed",
            candidate=candidate,
        )
        with self.assertRaises(Exception):
            ArtifactWorkspace(self.root).get(question_id)

    def test_validation_failure_never_promotes_candidate(self) -> None:
        controller, project_id = self._controller(slug="invalid-candidate")
        checkpoint = controller.transactions.orchestrator.store.load(project_id)
        action_id = "invalid-validation"
        head = self._git("rev-parse", "HEAD").stdout.strip()
        controller.transactions.begin_action(
            project_id=project_id, action_id=action_id,
            input_git_commit=head, expected_seq=checkpoint["checkpoint_seq"], commit=True,
        )
        bundle = ResearchContextBuilder(self.root).build(
            project_id=project_id, action_id=action_id, input_git_commit=head,
        )
        question_id = bundle["allowed_outputs"]["create_ids"][0]
        invalid = SyntheticSkillRuntime(self.root)._envelope(  # noqa: SLF001
            {"project_id": project_id, "action_id": action_id, "skill": "question-framing"},
            "ResearchQuestion", question_id, "invalid", "DRAFT",
        )
        proposal = ArtifactService(self.root).stage_proposal(
            proposal_id="proposal-invalid", project_id=project_id,
            action_id=action_id, operation="CREATE", artifact_id=question_id,
            kind="ResearchQuestion", base_revision=None,
            bundle_sha256=bundle["bundle_sha256"], proposer_session_id="invalid",
            candidate=invalid,
        )
        with self.assertRaises(Exception):
            ArtifactService(self.root).validate_action(project_id, action_id, [proposal.proposal_id])
        with self.assertRaises(Exception):
            ArtifactWorkspace(self.root).get(question_id)

    def test_lean_timeout_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); source = root / "Main.lean"; source.write_text("theorem x : True := by trivial\n")
            adapter = LeanToolAdapter(lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(a[0], 1)))
            result = adapter.verify(project_dir=root, source_paths=[source], lean_version="4.19.0", mathlib_commit="a" * 40, report_path=root / "report.json")
            self.assertEqual(result.outcome, "TOOL_TIMEOUT")
            self.assertFalse(result.build_passed)

    def test_lean_failure_classification(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); source = root / "Main.lean"; source.write_text("theorem x : True := by trivial\n")
            def result(stderr):
                return subprocess.CompletedProcess(["lake", "build"], 1, stdout="", stderr=stderr)
            syntax = LeanToolAdapter(lambda *a, **k: result("unexpected token"))
            library = LeanToolAdapter(lambda *a, **k: result("unknown module Mathlib.X"))
            self.assertEqual(syntax.verify(project_dir=root, source_paths=[source], lean_version="4", mathlib_commit="a" * 40, report_path=root / "syntax.json").outcome, "FORMALIZATION_GAP")
            self.assertEqual(library.verify(project_dir=root, source_paths=[source], lean_version="4", mathlib_commit="a" * 40, report_path=root / "library.json").outcome, "LIBRARY_GAP")

    def test_experiment_timeout_records_failed_raw_run(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); experiment = self._experiment(root)
            adapter = ExperimentExecutionAdapter(lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(a[0], 1)))
            first = adapter.execute(experiment=experiment, seed=7, repetition=0, run_root=root / "runs")
            second = adapter.execute(experiment=experiment, seed=7, repetition=0, run_root=root / "runs")
            self.assertEqual(first["run"]["status"], "FAILED")
            self.assertEqual(first["run"]["failure_reason"], "TIMEOUT")
            self.assertTrue(second["reused"])
            self.assertEqual(first["run"]["run_id"], second["run"]["run_id"])

    def test_experiment_crash_journal_prevents_duplicate_run(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); experiment = self._experiment(root)
            crashed = ExperimentExecutionAdapter(lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
            with self.assertRaises(KeyboardInterrupt):
                crashed.execute(experiment=experiment, seed=7, repetition=0, run_root=root / "runs")
            calls = []
            def should_not_run(*args, **kwargs):
                calls.append(1)
                return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")
            recovered = ExperimentExecutionAdapter(should_not_run).execute(
                experiment=experiment, seed=7, repetition=0, run_root=root / "runs"
            )
            self.assertEqual(calls, [])
            self.assertTrue(recovered["reused"])
            self.assertEqual(recovered["run"]["failure_reason"], "INTERRUPTED_RUN")

    def test_corrupted_dataset_is_rejected_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value); experiment = self._experiment(root)
            Path(experiment["datasets"][0]["uri"]).write_text("corrupted\n", encoding="utf-8")
            calls = []
            adapter = ExperimentExecutionAdapter(lambda *a, **k: calls.append(1))
            with self.assertRaisesRegex(RuntimeValidationError, "DATA_CORRUPTION"):
                adapter.execute(experiment=experiment, seed=7, repetition=0, run_root=root / "runs")
            self.assertEqual(calls, [])


class FailureMatrixTestCase(unittest.TestCase):
    def test_final_review_blocks_missing_or_stale_traceability(self) -> None:
        claim = {
            "id": "claim-test", "kind": "ScientificClaim", "revision": 3,
            "claim_type": "THEOREM", "status": "IN_PAPER", "statement": "P",
        }
        manuscript = '<a id="theorem-core"></a>\n## Verified theorem\n\nP\n'
        self.assertTrue(GoldenPilotRuntime._manuscript_blockers(
            manuscript, [claim], [], traceability={"entries": []},
        ))
        self.assertTrue(GoldenPilotRuntime._manuscript_blockers(
            manuscript, [claim], [],
            traceability={"entries": [{
                "anchor": "theorem-core", "statement_type": "THEOREM",
                "source_refs": [{"id": "claim-test", "kind": "ScientificClaim", "revision": 2}],
            }]},
        ))
        self.assertEqual(GoldenPilotRuntime._manuscript_blockers(
            manuscript, [claim], [],
            traceability={"entries": [{
                "anchor": "theorem-core", "statement_type": "THEOREM",
                "source_refs": [{"id": "claim-test", "kind": "ScientificClaim", "revision": 3}],
            }]},
        ), [])

    def test_failure_matrix_covers_all_required_categories_and_eval_cases(self) -> None:
        matrix = yaml.safe_load((ROOT / "evals/failure-injection-v0.1.yaml").read_text())
        cases = matrix["cases"]
        categories = {item["category"] for item in cases}
        self.assertEqual(categories, {
            "runtime", "artifact_git", "theory_lean", "experiment",
            "scientific_integrity", "human_control", "crash_point",
        })
        self.assertEqual(len(cases), len({item["id"] for item in cases}))
        eval_ids = {case.id for case in ScientificEvalRunner(ROOT).load_cases()}
        for item in cases:
            target = item["test"]
            if target.startswith("eval:"):
                self.assertIn(target.removeprefix("eval:"), eval_ids)
            self.assertIn(item["severity"], {"S0", "S1", "S2", "S3", "S4"})

    def test_library_checkout_contains_no_live_projects(self) -> None:
        workspace = ArtifactWorkspace(ROOT)
        workspace.require_valid()
        project_dirs = {
            path.name
            for path in (ROOT / "projects").glob("*")
            if path.is_dir()
        }
        self.assertEqual(project_dirs, set())
        fixture = yaml.safe_load(
            (ROOT / "tests/fixtures/projects/proj-demo/project.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["kind"], "Project")


if __name__ == "__main__":
    unittest.main()
