from __future__ import annotations

import copy
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from research_artifacts import ArtifactWorkspace
from research_artifacts.action_compiler import ActionCompiler
from research_artifacts.golden_pilots import GoldenPilotRuntime
from research_artifacts.mvp import MVPController, SyntheticSkillRuntime
from research_artifacts.orchestrator import required_skills_for_output
from research_artifacts.runtime_types import RuntimeValidationError
from research_artifacts.tool_adapters import ExperimentExecutionAdapter, LeanToolAdapter


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class FullMVPWiringTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "schemas", self.root / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "config", self.root / "config")
        skills = self.root / "integrations/deepseek-harness/skills"
        skills.parent.mkdir(parents=True)
        shutil.copytree(REPOSITORY_ROOT / "integrations/deepseek-harness/skills", skills)
        (self.root / "projects").mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Research Harness MVP Test")
        self._git("config", "user.email", "mvp@example.invalid")
        self._git("add", "schemas", "integrations", "projects")
        self._git("commit", "-m", "MVP fixture baseline")
        self.controller = MVPController(self.root, SyntheticSkillRuntime(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.root, text=True, capture_output=True, check=True)

    def test_one_command_drives_mixed_core_formal_project_to_final_approval(self) -> None:
        self.controller.init("proj-wire", "Synthetic full MVP wiring")
        result = self.controller.run("proj-wire", budget_percent=100)
        self.assertEqual(result["reason"], "WAITING_FINAL_APPROVAL")
        checkpoint = result["checkpoint"]
        self.assertEqual(checkpoint["route"], "MIXED")
        self.assertEqual(checkpoint["branch_states"], {"theory": "COMPLETED", "experiment": "COMPLETED"})
        theory = checkpoint["skill_progress"]["core-correctness"]
        self.assertEqual(theory["completed_skills"], theory["required_skills"])
        experiment_progress = checkpoint["skill_progress"]["protocol-integrity"]
        self.assertEqual(experiment_progress["completed_skills"], experiment_progress["required_skills"])

        workspace = ArtifactWorkspace(self.root)
        workspace.require_valid()
        claim = workspace.get(theory["artifact_ref"]["id"])
        self.assertEqual(claim.data["status"], "IN_PAPER")
        schemes = {
            item.data.get("assessment", {}).get("scheme")
            for item in workspace.query(kind="Review", project_id="proj-wire")
        }
        self.assertTrue({"THEORY", "LEAN", "AXIOM_AUDIT", "SEMANTIC_ALIGNMENT"}.issubset(schemes))
        experiment = workspace.get(experiment_progress["artifact_ref"]["id"])
        self.assertEqual(experiment.data["status"], "COMPLETED")
        self.assertEqual(len(experiment.data["runs"]), 2)
        self.assertIn(-0.1, [run["metric_values"]["integrity"] for run in experiment.data["runs"]])

        self.controller.pause("proj-wire", "test interruption")
        self.assertEqual(self.controller.status("proj-wire")["state"], "PAUSED_BY_USER")
        self.controller.resume("proj-wire")
        self.assertEqual(self.controller.run("proj-wire")["reason"], "WAITING_FINAL_APPROVAL")
        self.controller.approve("proj-wire")
        self.assertEqual(self.controller.status("proj-wire")["state"], "DONE")
        ArtifactWorkspace(self.root).require_valid()
        self.assertGreater(int(self._git("rev-list", "--count", "HEAD").stdout.strip()), 20)


class DeterministicToolAdapterTestCase(unittest.TestCase):
    def test_writer_never_labels_empirical_hypothesis_as_formal_theorem(self) -> None:
        self.assertFalse(GoldenPilotRuntime._is_formal_claim({"claim_type": "HYPOTHESIS"}))
        self.assertFalse(GoldenPilotRuntime._is_formal_claim({"claim_type": "EMPIRICAL_CLAIM"}))
        self.assertTrue(GoldenPilotRuntime._is_formal_claim({"claim_type": "THEOREM"}))
        self.assertTrue(GoldenPilotRuntime._is_manuscript_claim({
            "claim_type": "THEOREM", "status": "IN_PAPER",
        }))
        self.assertFalse(GoldenPilotRuntime._is_manuscript_claim({
            "claim_type": "HYPOTHESIS", "status": "VERIFIED",
        }))
        empirical = {"id": "claim-e", "claim_type": "HYPOTHESIS", "status": "VERIFIED", "statement": "H"}
        experiment = {"id": "exp-e", "kind": "Experiment", "revision": 4, "status": "COMPLETED", "result": {"hypothesis_outcome": "SUPPORTED"}}
        self.assertTrue(GoldenPilotRuntime._manuscript_blockers(
            "## Verified theorem\nH\nLean theorem establishes H\n**SUPPORTED**", [empirical], [experiment]
        ))
        formal = {
            "id": "claim-t", "kind": "ScientificClaim", "revision": 5,
            "claim_type": "THEOREM", "status": "VERIFIED",
            "statement": "For every real p, p * (1-p) = p * (1-p).",
        }
        mixed_manuscript = (
            "<a id=\"theorem-core\"></a>\n## Verified theorem\nFor every real p, p * (1-p) = p * (1-p).\n"
            "Lean theorem establishes this exact statement.\n"
            "<a id=\"result-primary\"></a>\n## Preregistered experiment\nThe outcome was **SUPPORTED**."
        )
        traceability = {"entries": [
            {"anchor": "theorem-core", "statement_type": "THEOREM", "source_refs": [
                {"id": "claim-t", "kind": "ScientificClaim", "revision": 5},
            ]},
            {"anchor": "result-primary", "statement_type": "EMPIRICAL_RESULT", "source_refs": [
                {"id": "exp-e", "kind": "Experiment", "revision": 4},
            ]},
        ]}
        self.assertEqual(
            GoldenPilotRuntime._manuscript_blockers(
                mixed_manuscript, [formal, empirical], [experiment],
                traceability=traceability,
            ),
            [],
        )
        self.assertEqual(GoldenPilotRuntime._manuscript_blockers(
            '<a id="result-primary"></a>\n## Result\nThe outcome was **SUPPORTED**.',
            [empirical], [experiment], traceability={"entries": [
                {"anchor": "result-primary", "statement_type": "EMPIRICAL_RESULT", "source_refs": [
                    {"id": "exp-e", "kind": "Experiment", "revision": 4},
                ]},
            ]},
        ), [])

    def test_action_compiler_preserves_skill_specific_read_tools(self) -> None:
        tools = ActionCompiler._allowed_tools("literature-novelty", {
            "tools": ["research_artifact_read", "academic_search", "citation_neighborhood", "academic_source_read", "resource_read"]
        })
        self.assertIn("academic_search", tools)
        self.assertIn("citation_neighborhood", tools)
        self.assertIn("academic_source_read", tools)
        self.assertIn("resource_read", tools)

    def test_action_preview_tolerates_track_advance_checkpoint_window(self) -> None:
        class Checkpoints:
            @staticmethod
            def load(_project_id):
                return {
                    "state": "EXECUTION_TRACKS",
                    "branch_states": {"theory": "DEVELOP", "experiment": "NOT_SELECTED"},
                }

        class Contexts:
            @staticmethod
            def _planned_output_dependencies(_checkpoint, _index):
                return {}

            @staticmethod
            def _role_for(_state, _track, _checkpoint, _output_dependencies):
                raise RuntimeValidationError("no pending Skill matches theory branch phase DEVELOP")

        compiler = object.__new__(ActionCompiler)
        compiler.checkpoints = Checkpoints()
        compiler.contexts = Contexts()
        compiler.workspace = mock.Mock()
        compiler.workspace.index.return_value = {}
        preview = compiler.preview("proj-transient")
        self.assertTrue(preview["controller_only"])
        self.assertEqual(preview["controller_transition"], "TRACK_ADVANCED")

    def test_action_preview_uses_runnable_experiment_when_theory_is_dependency_blocked(
        self,
    ) -> None:
        class Checkpoints:
            @staticmethod
            def load(_project_id):
                return {
                    "state": "EXECUTION_TRACKS",
                    "branch_states": {"theory": "DEVELOP", "experiment": "PREPARE"},
                    "skill_progress": {
                        "protocol": {
                            "artifact_ref": {
                                "id": "exp-protocol",
                                "kind": "Experiment",
                                "revision": 1,
                            },
                        },
                    },
                }

        class Contexts:
            @staticmethod
            def _planned_output_dependencies(_checkpoint, _index):
                return {}

            @staticmethod
            def _role_for(_state, track, _checkpoint, _output_dependencies):
                if track == "theory":
                    raise RuntimeValidationError(
                        "no pending Skill matches theory branch phase DEVELOP"
                    )
                return "experiment-design", "research-experiment", "protocol"

        compiler = object.__new__(ActionCompiler)
        compiler.checkpoints = Checkpoints()
        compiler.contexts = Contexts()
        compiler.workspace = mock.Mock()
        compiler.workspace.index.return_value = {}
        compiler.workspace.get.return_value = mock.Mock(
            id="exp-protocol",
            kind="Experiment",
            data={"revision": 1},
        )
        preview = compiler.preview("proj-cross-track")
        self.assertFalse(preview["controller_only"])
        self.assertEqual(preview["track"], "experiment")
        self.assertEqual(preview["skill"], "experiment-design")
        self.assertEqual(preview["target_output_id"], "protocol")

    def test_controller_resumes_a_persisted_pending_action(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            shutil.copytree(REPOSITORY_ROOT / "schemas", root / "schemas")
            shutil.copytree(REPOSITORY_ROOT / "config", root / "config")
            skills = root / "integrations/deepseek-harness/skills"
            skills.parent.mkdir(parents=True)
            shutil.copytree(REPOSITORY_ROOT / "integrations/deepseek-harness/skills", skills)
            (root / "projects").mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "schemas", "config", "integrations", "projects"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "test baseline"], cwd=root, check=True, capture_output=True)
            # The full runtime test suite covers execution; this focused check
            # guards the controller branch that must not call begin_action twice.
            controller = MVPController(root, SyntheticSkillRuntime(root))
            controller.init("resume-pending", "test pending action recovery")
            checkpoint = controller.transactions.orchestrator.store.load("proj-resume-pending")
            controller.budgets.approve_initial(
                "proj-resume-pending", checkpoint["run_id"], 100
            )
            checkpoint = controller.transactions.orchestrator.store.load("proj-resume-pending")
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                text=True, capture_output=True,
            ).stdout.strip()
            begun = controller.transactions.begin_action(
                project_id="proj-resume-pending", action_id="stable-action",
                input_git_commit=head, expected_seq=checkpoint["checkpoint_seq"],
                commit=True,
            )["checkpoint"]
            # Re-entering execution consumes the existing pending action; it
            # must not fail with "cannot begin an action".
            controller._execute_action(begun)  # noqa: SLF001
            resumed = controller.transactions.orchestrator.store.load("proj-resume-pending")
            self.assertEqual(resumed["last_completed_action"], "stable-action")
            self.assertIsNone(resumed["pending_action"])

    def test_empirical_claim_is_owned_by_experiment_pipeline(self) -> None:
        self.assertEqual(
            required_skills_for_output({
                "kind": "ScientificClaim",
                "verification_profile": "EMPIRICAL",
            }),
            [],
        )

    def test_lean_adapter_rejects_sorry_and_unapproved_axiom(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = root / "Bad.lean"
            source.write_text("axiom secret : Prop\ntheorem bad : True := by sorry\n", encoding="utf-8")

            class SuccessRunner:
                def __call__(self, *args, **kwargs):
                    return subprocess.CompletedProcess(args[0], 0, "ok", "")

            result = LeanToolAdapter(SuccessRunner()).verify(
                project_dir=root, source_paths=[source], lean_version="4.19.0",
                mathlib_commit="a" * 40, allowed_axioms=[], report_path=root / "report.json",
            )
            self.assertNotEqual(result.outcome, "PASS")
            self.assertIn("SORRY_FOUND", result.findings)
            self.assertIn("UNAPPROVED_AXIOM:secret", result.findings)

    def test_lean_adapter_audits_axioms_reported_by_lean(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            source = root / "Reported.lean"
            source.write_text("theorem reported : True := True.intro\n", encoding="utf-8")

            class ReportingRunner:
                def __call__(self, *args, **kwargs):
                    return subprocess.CompletedProcess(
                        args[0], 0,
                        "reported depends on axioms: [Classical.choice, forbidden.extra]\n",
                        "",
                    )

            result = LeanToolAdapter(ReportingRunner()).verify(
                project_dir=root, source_paths=[source], lean_version="4.19.0",
                mathlib_commit="a" * 40,
                allowed_axioms=["Classical.choice"], report_path=root / "report.json",
            )
            self.assertEqual(result.outcome, "UNAPPROVED_AXIOM")
            self.assertIn("UNAPPROVED_AXIOM:forbidden.extra", result.findings)

    def test_experiment_adapter_is_idempotent_and_rejects_protocol_change(self) -> None:
        experiment = {
            "id": "exp-tool-test", "revision": 2,
            "hypothesis": {"claim_ref": {"id": "claim-tool", "kind": "ScientificClaim", "revision": 1}, "operationalization": "fixed"},
            "method": "fixed", "baselines": [],
            "datasets": [{"name": "d", "sha256": "1" * 64}],
            "metrics": [], "configuration": {"parameters": {}, "environment": "test"},
            "interpretation_plan": {"seeds": [0]},
            "code_location": {"git_commit": "a" * 40, "entrypoint": "python3 -c print(1)"},
            "runs": [],
        }
        adapter = ExperimentExecutionAdapter()
        experiment["protocol_lock"] = {
            "revision": 2, "sha256": adapter.protocol_hash(experiment),
            "locked_at": "2026-08-21T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as value:
            first = adapter.execute(experiment=experiment, seed=0, repetition=0, run_root=value)
            experiment["runs"] = [first["run"]]
            second = adapter.execute(experiment=experiment, seed=0, repetition=0, run_root=value)
            self.assertTrue(second["reused"])
            changed = copy.deepcopy(experiment)
            changed["method"] = "post-hoc change"
            with self.assertRaises(RuntimeValidationError):
                adapter.execute(experiment=changed, seed=0, repetition=0, run_root=value)

    def test_experiment_adapter_resolves_workspace_resource_uris_once(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            repository = root / "projects" / "p" / "resources" / "input"
            repository.mkdir(parents=True)
            dataset = repository / "data.bin"
            dataset.write_bytes(b"pinned")
            script = repository / "run.py"
            script.write_text(
                "import pathlib, sys\n"
                "assert sys.argv[1] == '--output-dir'\n"
                "output = pathlib.Path(sys.argv[2])\n"
                "assert not any(output.iterdir())\n"
                "pathlib.Path(output, 'marker').write_text('ok')\n",
                encoding="utf-8",
            )
            experiment = {
                "id": "exp-workspace-uri", "revision": 2,
                "hypothesis": {"claim_ref": {"id": "claim-tool", "kind": "ScientificClaim", "revision": 1}, "operationalization": "fixed"},
                "method": "fixed", "baselines": [],
                "datasets": [{
                    "name": "d", "sha256": hashlib.sha256(b"pinned").hexdigest(),
                    "uri": "projects/p/resources/input/data.bin",
                }],
                "metrics": [], "configuration": {
                    "parameters": {
                        "execution_argv": ["--output-dir", "{run_output_dir}"],
                    },
                    "environment": "test",
                },
                "interpretation_plan": {"seeds": [0]},
                "code_location": {
                    "git_commit": "a" * 40,
                    "entrypoint": "run.py",
                    "repository": "projects/p/resources/input",
                },
                "runs": [],
            }
            adapter = ExperimentExecutionAdapter(workspace_root=root)
            experiment["protocol_lock"] = {
                "revision": 2, "sha256": adapter.protocol_hash(experiment),
                "locked_at": "2026-08-21T00:00:00Z",
            }
            result = adapter.execute(
                experiment=experiment, seed=0, repetition=0, run_root=root / "runs",
            )
            self.assertEqual(result["run"]["status"], "SUCCEEDED")
            output_dir = Path(result["run"]["outputs"][0]["uri"]).parent
            self.assertEqual((output_dir / "payload" / "marker").read_text(), "ok")

    def test_experiment_adapter_rejects_repeated_interpreter_in_execution_argv(self) -> None:
        adapter = ExperimentExecutionAdapter()
        experiment = {
            "configuration": {
                "parameters": {"execution_argv": ["python3", "audit.py"]},
            },
        }
        with self.assertRaisesRegex(
            RuntimeValidationError,
            "must not repeat an interpreter",
        ):
            adapter._entrypoint_command(
                "audit.py",
                repository=Path("."),
                experiment=experiment,
                output_dir=Path("run"),
            )

    def test_experiment_adapter_rejects_embedded_execution_placeholder(self) -> None:
        adapter = ExperimentExecutionAdapter()
        experiment = {
            "configuration": {
                "parameters": {
                    "execution_argv": ["--output={run_output_dir}"],
                },
            },
        }
        with self.assertRaisesRegex(
            RuntimeValidationError,
            "placeholders must be standalone tokens",
        ):
            adapter._entrypoint_command(
                "audit.py",
                repository=Path("."),
                experiment=experiment,
                output_dir=Path("run"),
            )


if __name__ == "__main__":
    unittest.main()
