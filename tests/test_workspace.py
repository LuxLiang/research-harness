from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from research_artifacts import ArtifactWorkspace, RevisionConflict
from research_artifacts.tool_adapters import ExperimentExecutionAdapter
from research_artifacts.workspace import ArtifactValidationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(REPOSITORY_ROOT / "schemas", self.root / "schemas")
        shutil.copytree(REPOSITORY_ROOT / "tests/fixtures/projects", self.root / "projects")
        self.workspace = ArtifactWorkspace(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_example_is_valid_and_has_all_kinds(self) -> None:
        self.assertEqual(self.workspace.validate(), [])
        self.assertEqual(
            {record.kind for record in self.workspace.discover()},
            {
                "Project",
                "ResearchQuestion",
                "ResearchPlan",
                "LiteratureEvidence",
                "ScientificClaim",
                "Experiment",
                "Review",
                "Decision",
            },
        )

    def test_unknown_field_is_rejected(self) -> None:
        record = self.workspace.get("claim-demo-recoverable-state")
        candidate = copy.deepcopy(record.data)
        candidate["uncontrolled_note"] = "not allowed"
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "schema" for issue in issues))

    def test_dangling_reference_is_rejected(self) -> None:
        record = self.workspace.get("plan-demo-main")
        candidate = copy.deepcopy(record.data)
        candidate["question_refs"][0]["id"] = "rq-demo-missing"
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "dangling-ref" for issue in issues))

    def test_claim_dependency_cycle_is_rejected(self) -> None:
        record = self.workspace.get("claim-demo-recoverable-state")
        candidate = copy.deepcopy(record.data)
        candidate["dependencies"] = [
            {
                "claim_ref": {
                    "id": "claim-demo-recoverable-state",
                    "kind": "ScientificClaim",
                },
                "relation": "USES",
            }
        ]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "claim-cycle" for issue in issues))

    def test_verified_claim_requires_evidence(self) -> None:
        record = self.workspace.get("claim-demo-recoverable-state")
        candidate = copy.deepcopy(record.data)
        candidate["evidence"] = []
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "schema" for issue in issues))

    def test_review_evidence_resource_must_exist_and_match_hash(self) -> None:
        review = self.workspace.get("review-demo-claim-001")
        candidate = copy.deepcopy(review.data)
        uri = "projects/proj-demo/resources/review-evidence.md"
        candidate["evidence_resources"] = [{
            "uri": uri,
            "sha256": "0" * 64,
            "media_type": "text/markdown",
            "description": "pinned review evidence",
        }]

        issues = self.workspace.validate({review.id: candidate})
        self.assertTrue(any(issue.code == "missing-review-resource" for issue in issues))

        resource = self.root / uri
        resource.write_text("verified evidence\n", encoding="utf-8")
        issues = self.workspace.validate({review.id: candidate})
        self.assertTrue(any(issue.code == "review-resource-hash" for issue in issues))

        candidate["evidence_resources"][0]["sha256"] = hashlib.sha256(
            resource.read_bytes()
        ).hexdigest()
        issues = self.workspace.validate({review.id: candidate})
        self.assertFalse(any(issue.code.startswith("review-resource") for issue in issues))

    def test_completed_experiment_requires_code_location(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        del candidate["code_location"]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "schema" for issue in issues))

    def test_ready_experiment_must_pin_claim_revision(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        del candidate["hypothesis"]["claim_ref"]["revision"]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(
            any(issue.code == "unpinned-experiment-claim" for issue in issues)
        )

    def test_protocol_lock_hash_detects_scientific_field_tampering(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        candidate["method"] = "Post-lock method change"
        issues = self.workspace.validate({record.id: candidate})
        mismatch = next(
            issue for issue in issues if issue.code == "protocol-lock-mismatch"
        )
        expected = hashlib.sha256(
            json.dumps(
                {
                    key: candidate[key]
                    for key in (
                        "hypothesis", "method", "baselines", "datasets", "metrics",
                        "configuration", "interpretation_plan", "code_location",
                    )
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertIn(f"expected {expected}", mismatch.message)

    def test_ready_experiment_rejects_repeated_entrypoint_in_argv(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        candidate["configuration"]["parameters"]["execution_argv"] = [
            "python3", "research-artifacts", "validate"
        ]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(
            any(issue.code == "duplicated-experiment-entrypoint" for issue in issues)
        )

    def test_ready_experiment_rejects_embedded_argv_placeholder(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        candidate["configuration"]["parameters"]["execution_argv"] = [
            "--output", "{run_output_dir}/result.json"
        ]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(
            any(issue.code == "embedded-execution-placeholder" for issue in issues)
        )

    def test_unrelated_overlay_can_retire_a_legacy_invalid_protocol(self) -> None:
        experiment = self.workspace.get("exp-demo-state-recovery")
        legacy = copy.deepcopy(experiment.data)
        legacy["configuration"]["parameters"]["execution_argv"] = [
            "python3", "{workspace_root}/legacy-driver.py"
        ]
        experiment_path = self.root / experiment.path
        experiment_path.write_text(
            yaml.safe_dump(legacy, sort_keys=False), encoding="utf-8"
        )

        full_audit = self.workspace.validate()
        self.assertTrue(
            any(issue.code == "duplicated-experiment-entrypoint" for issue in full_audit)
        )
        project = self.workspace.get("proj-demo")
        project_overlay = copy.deepcopy(project.data)
        issues = self.workspace.validate({project.id: project_overlay})
        self.assertFalse(
            any(
                issue.artifact_id == experiment.id
                and issue.code in {
                    "duplicated-experiment-entrypoint",
                    "embedded-execution-placeholder",
                }
                for issue in issues
            )
        )

    def test_experiment_requires_preregistered_integrity_fields(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        del candidate["interpretation_plan"]["statistical_procedure"]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "schema" for issue in issues))

    def test_run_requires_dataset_provenance(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        del candidate["runs"][0]["dataset_hashes"]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "schema" for issue in issues))

    def test_run_seed_must_match_preregistration(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        candidate["runs"][0]["seed"] = 999
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "unregistered-run-seed" for issue in issues))

    def test_single_matrix_driver_may_use_legacy_wrapper_seed_zero(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        scientific_seeds = [11, 12, 13]
        candidate["interpretation_plan"]["seeds"] = scientific_seeds
        candidate["configuration"]["parameters"]["matrix"] = {
            "seeds": scientific_seeds,
        }
        candidate["configuration"]["parameters"]["execution_argv"] = [
            "--output-dir", "{run_output_dir}",
        ]
        candidate["code_location"]["entrypoint"] = "recovery-matrix-driver.py"
        candidate["runs"][0]["seed"] = 0
        candidate["protocol_lock"]["sha256"] = (
            ExperimentExecutionAdapter.protocol_hash(candidate)
        )
        candidate["runs"][0]["protocol_sha256"] = candidate["protocol_lock"]["sha256"]
        issues = self.workspace.validate({record.id: candidate})
        self.assertFalse(any(issue.code == "unregistered-run-seed" for issue in issues))

        candidate["runs"][0]["seed"] = 1
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "unregistered-run-seed" for issue in issues))

    def test_completed_matrix_driver_counts_wrapper_as_seed_census(self) -> None:
        record = self.workspace.get("exp-demo-state-recovery")
        candidate = copy.deepcopy(record.data)
        scientific_seeds = [11, 12, 13]
        candidate["interpretation_plan"]["seeds"] = scientific_seeds
        candidate["configuration"]["parameters"]["matrix"] = {
            "seeds": scientific_seeds,
        }
        candidate["configuration"]["parameters"]["execution_argv"] = [
            "--output-dir", "{run_output_dir}",
        ]
        candidate["code_location"]["entrypoint"] = "recovery-matrix-driver.py"
        candidate["runs"][0]["seed"] = 0
        candidate["status"] = "COMPLETED"
        candidate["protocol_lock"]["sha256"] = (
            ExperimentExecutionAdapter.protocol_hash(candidate)
        )
        candidate["runs"][0]["protocol_sha256"] = candidate["protocol_lock"]["sha256"]
        issues = self.workspace.validate({record.id: candidate})
        self.assertFalse(any(issue.code == "missing-preregistered-runs" for issue in issues))

        candidate["runs"][0]["seed"] = scientific_seeds[0]
        issues = self.workspace.validate({record.id: candidate})
        self.assertFalse(any(issue.code == "missing-preregistered-runs" for issue in issues))

        candidate["configuration"]["parameters"]["execution_argv"].append("{seed}")
        candidate["protocol_lock"]["sha256"] = (
            ExperimentExecutionAdapter.protocol_hash(candidate)
        )
        candidate["runs"][0]["protocol_sha256"] = candidate["protocol_lock"]["sha256"]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "missing-preregistered-runs" for issue in issues))

    def test_core_formal_claim_requires_all_four_accepting_reviews(self) -> None:
        plan = copy.deepcopy(self.workspace.get("plan-demo-main").data)
        claim = copy.deepcopy(self.workspace.get("claim-demo-recoverable-state").data)
        for package in plan["work_packages"]:
            for output in package.get("planned_outputs", []):
                if output["local_id"] == "theory-recoverable-state":
                    output["verification_profile"] = "CORE_FORMAL"
        claim["status"] = "VERIFIED"
        issues = self.workspace.validate({plan["id"]: plan, claim["id"]: claim})
        missing = [issue for issue in issues if issue.code == "verification-profile"]
        self.assertTrue(any("LEAN" in issue.message for issue in missing))
        self.assertTrue(any("AXIOM_AUDIT" in issue.message for issue in missing))
        self.assertTrue(any("SEMANTIC_ALIGNMENT" in issue.message for issue in missing))

    def test_non_claim_planned_output_rejects_verification_profile(self) -> None:
        plan = copy.deepcopy(self.workspace.get("plan-demo-main").data)
        for package in plan["work_packages"]:
            for output in package.get("planned_outputs", []):
                if output["kind"] == "Experiment":
                    output["verification_profile"] = "ADVERSARIAL"
        issues = self.workspace.validate({plan["id"]: plan})
        self.assertTrue(any(issue.code == "schema" for issue in issues))

    def test_draft_plan_does_not_require_placeholder_artifacts(self) -> None:
        record = self.workspace.get("plan-demo-main")
        candidate = copy.deepcopy(record.data)
        candidate["status"] = "DRAFT"
        for package in candidate["work_packages"]:
            package["status"] = "TODO"
            package["materialized_outputs"] = []
        project = self.workspace.get("proj-demo")
        project_candidate = copy.deepcopy(project.data)
        project_candidate["stage"] = "PLANNING"
        issues = self.workspace.validate(
            {record.id: candidate, project.id: project_candidate}
        )
        self.assertEqual(issues, [])

    def test_done_work_package_requires_materialized_required_outputs(self) -> None:
        record = self.workspace.get("plan-demo-main")
        candidate = copy.deepcopy(record.data)
        candidate["work_packages"][0]["materialized_outputs"] = []
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(
            any(issue.code == "unfinished-required-output" for issue in issues)
        )

    def test_planned_output_dependencies_must_form_a_dag(self) -> None:
        record = self.workspace.get("plan-demo-main")
        candidate = copy.deepcopy(record.data)
        candidate["work_packages"][0]["planned_outputs"][0][
            "depends_on_outputs"
        ] = ["review-recoverable-state"]
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "planned-output-cycle" for issue in issues))

    def test_materialized_mapping_must_stay_with_its_work_package(self) -> None:
        record = self.workspace.get("plan-demo-main")
        candidate = copy.deepcopy(record.data)
        mapping = candidate["work_packages"][0]["materialized_outputs"].pop()
        candidate["work_packages"][1]["materialized_outputs"].append(mapping)
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(
            any(issue.code == "misplaced-materialized-output" for issue in issues)
        )

    def test_materialized_experiment_must_test_its_planned_claim(self) -> None:
        plan = self.workspace.get("plan-demo-main")
        candidate = copy.deepcopy(plan.data)
        experiment_output = candidate["work_packages"][2]["planned_outputs"][0]
        experiment_output["depends_on_outputs"] = ["review-recoverable-state"]
        issues = self.workspace.validate({plan.id: candidate})
        self.assertTrue(
            any(issue.code == "experiment-without-planned-claim" for issue in issues)
        )

    def test_review_cannot_pin_a_future_revision(self) -> None:
        record = self.workspace.get("review-demo-claim-001")
        candidate = copy.deepcopy(record.data)
        candidate["target"]["artifact_ref"]["revision"] = 99
        issues = self.workspace.validate({record.id: candidate})
        self.assertTrue(any(issue.code == "future-revision" for issue in issues))

    def test_revision_conflict_prevents_transition(self) -> None:
        with self.assertRaises(RevisionConflict):
            self.workspace.transition(
                "rq-demo-correction",
                expected_revision=99,
                new_status="ANSWERED",
                actor_type="human",
                actor_id="tester",
            )

    def test_valid_transition_is_atomic_and_increments_revision(self) -> None:
        result = self.workspace.transition(
            "plan-demo-main",
            expected_revision=1,
            new_status="IN_PROGRESS",
            actor_type="human",
            actor_id="tester",
        )
        self.assertEqual(result.data["revision"], 2)
        reloaded = ArtifactWorkspace(self.root).get("plan-demo-main")
        self.assertEqual(reloaded.data["status"], "IN_PROGRESS")
        self.assertEqual(reloaded.data["revision"], 2)

    def test_project_stage_gate_allows_writing_but_blocks_complete(self) -> None:
        result = self.workspace.transition(
            "proj-demo",
            expected_revision=1,
            new_stage="WRITING",
            actor_type="agent",
            actor_id="orchestrator",
            session_id="test-session",
        )
        self.assertEqual(result.data["stage"], "WRITING")
        with self.assertRaises(ArtifactValidationError):
            self.workspace.transition(
                "proj-demo",
                expected_revision=2,
                new_stage="COMPLETE",
                actor_type="human",
                actor_id="tester",
            )

    def test_graph_is_computed_without_an_index_file(self) -> None:
        edges = self.workspace.graph_edges()
        self.assertTrue(
            any(
                edge["source"] == "exp-demo-state-recovery"
                and edge["target"] == "claim-demo-recoverable-state"
                for edge in edges
            )
        )
        self.assertFalse((self.root / "projects" / "index.json").exists())


if __name__ == "__main__":
    unittest.main()
