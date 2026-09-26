from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "integrations" / "deepseek-harness" / "skills"
EXPECTED = {
    "question-framing", "literature-novelty", "research-planning",
    "theory-development", "theory-verification", "lean-formalization",
    "lean-verification", "semantic-alignment-review", "experiment-design",
    "experiment-execution", "experiment-verification", "research-synthesis",
    "completion-review", "consistency-review", "targeted-followup",
    "scientific-writing", "final-review",
    "proposal-correctness-review", "proposal-revision",
}
REQUIRED_SECTIONS = {
    "## Purpose", "## Inputs", "## Outputs", "## Tools", "## Procedure",
    "## Completion Conditions", "## Failure Classes", "## Non-goals",
    "## Permissions", "## Provenance", "## Forbidden Behaviors",
}


class SkillContractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        schema = json.loads(
            (SKILLS / "_shared" / "skill-contract.schema.json").read_text()
        )
        self.validator = Draft202012Validator(schema)

    def test_expected_unique_skill_contracts_validate(self) -> None:
        found = {}
        for path in SKILLS.glob("*/skill.yaml"):
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            errors = list(self.validator.iter_errors(value))
            self.assertEqual(errors, [], f"{path}: {errors}")
            self.assertNotIn(value["id"], found)
            self.assertEqual(path.parent.name, value["id"])
            found[value["id"]] = value
        self.assertEqual(set(found), EXPECTED)

    def test_writer_and_reviewer_boundaries_are_contractual(self) -> None:
        writer = yaml.safe_load(
            (SKILLS / "scientific-writing" / "skill.yaml").read_text()
        )
        self.assertNotIn("ScientificClaim", writer["permissions"]["artifact_kinds"])
        self.assertNotIn("CREATE", writer["permissions"]["operations"])
        for skill_id in {
            "theory-verification", "lean-verification",
            "semantic-alignment-review", "experiment-verification",
            "completion-review", "consistency-review", "final-review",
        }:
            value = yaml.safe_load((SKILLS / skill_id / "skill.yaml").read_text())
            self.assertEqual(value["permissions"]["artifact_kinds"], ["Review"])
            self.assertFalse(value["permissions"]["canonical_promote"])

    def test_theory_rework_revises_the_canonical_target(self) -> None:
        value = yaml.safe_load(
            (SKILLS / "theory-development" / "skill.yaml").read_text()
        )
        procedure = "\n".join(value["procedure"])
        self.assertIn("exact canonical target Claim ID", procedure)
        self.assertIn("base_revision", procedure)
        self.assertIn("never create an action-local replacement", procedure)
        self.assertIn(
            "Creating a new Claim ID for a targeted revision of an existing canonical Claim",
            value["forbidden_behaviors"],
        )

    def test_completion_skill_understands_controller_claim_promotion(self) -> None:
        procedure = (SKILLS / "completion-review" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("VERIFIED revision r+1", procedure)
        self.assertIn("never demand a Review of the already-promoted revision", procedure)
        self.assertIn("arbitrary stale Review", procedure)
        self.assertIn("research-artifact/v0.1.2", procedure)
        self.assertIn("never assessment.outcome", procedure)
        contract = yaml.safe_load(
            (SKILLS / "completion-review" / "skill.yaml").read_text()
        )
        contract_procedure = "\n".join(contract["procedure"])
        self.assertIn("VERIFIED revision r+1", contract_procedure)
        self.assertIn("outcome COMPLETE or INCOMPLETE", contract_procedure)

    def test_synthesis_preserves_resolved_negative_provenance(self) -> None:
        procedure = (SKILLS / "research-synthesis" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("negative provenance", procedure)
        self.assertIn("mark its synthesis issue RESOLVED", procedure)
        contract = yaml.safe_load(
            (SKILLS / "research-synthesis" / "skill.yaml").read_text()
        )
        self.assertIn("mark its synthesis issue RESOLVED", "\n".join(contract["procedure"]))

    def test_synthesis_understands_controller_verification_promotion(self) -> None:
        procedure = (SKILLS / "research-synthesis" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("VERIFIED revision r+1", procedure)
        self.assertIn("never emit REVISION_MISMATCH", procedure)
        contract = yaml.safe_load(
            (SKILLS / "research-synthesis" / "skill.yaml").read_text()
        )
        contract_procedure = "\n".join(contract["procedure"])
        self.assertIn("VERIFIED revision r+1", contract_procedure)
        self.assertIn("never emit REVISION_MISMATCH", contract_procedure)

    def test_consistency_understands_controller_verification_promotion(self) -> None:
        contract = yaml.safe_load(
            (SKILLS / "consistency-review" / "skill.yaml").read_text()
        )
        procedure = "\n".join(contract["procedure"])
        self.assertIn("VERIFIED revision r+1", procedure)
        self.assertIn("current lifecycle evidence", procedure)
        self.assertIn("arbitrary edits, skipped revisions", procedure)

    def test_writer_uses_resource_only_submission_contract(self) -> None:
        contract = yaml.safe_load(
            (SKILLS / "scientific-writing" / "skill.yaml").read_text()
        )
        procedure = "\n".join(contract["procedure"])
        self.assertIn("exact research-traceability/v0.1 contract", procedure)
        self.assertIn("paper/provenance.yaml", procedure)
        self.assertIn("proposal_ids_json set to []", procedure)
        self.assertIn("outcome SUBMITTED", procedure)
        self.assertIn("self-contained submission-grade manuscript", procedure)
        self.assertIn("complete proofs in appendices", procedure)
        self.assertIn("two proposed algorithms", procedure)

    def test_final_review_reads_frozen_paper_and_accepts_only_lifecycle_promotion(self) -> None:
        contract = yaml.safe_load(
            (SKILLS / "final-review" / "skill.yaml").read_text()
        )
        procedure = "\n".join(contract["procedure"])
        self.assertIn("manuscript_read", procedure)
        self.assertIn("paper/traceability.yaml", procedure)
        self.assertIn("IN_PAPER ScientificClaim revision r+1", procedure)
        self.assertIn("Never demand a new theory Review", procedure)
        self.assertIn("Arbitrary edits, skipped revisions, or stale Reviews", procedure)
        self.assertIn("self-contained submission-grade paper", procedure)
        self.assertIn("complete Base and Optimistic pseudocode", procedure)
        self.assertIn("verified finite witness", procedure)
        self.assertIn("top-level status to RESOLVED", procedure)
        self.assertIn("PASS Review with top-level status OPEN is invalid", procedure)

    def test_targeted_followup_creates_one_successor_plan(self) -> None:
        value = yaml.safe_load(
            (SKILLS / "targeted-followup" / "skill.yaml").read_text()
        )
        self.assertIn("CREATE", value["permissions"]["operations"])
        self.assertNotIn("REVISE", value["permissions"]["operations"])
        procedure = "\n".join(value["procedure"])
        self.assertIn("exactly one CREATE proposal", procedure)
        self.assertIn("successor ResearchPlan with status IN_PROGRESS", procedure)
        self.assertIn("never revise it in place", procedure)

    def test_reused_proposal_skills_have_real_resource_contracts(self) -> None:
        expected_states = {
            "question-framing": "PROPOSAL_STRUCTURING",
            "literature-novelty": "PROPOSAL_NOVELTY_REVIEW",
            "research-planning": "PROPOSAL_PLAN_RECONSTRUCTION",
        }
        for skill_id, state in expected_states.items():
            value = yaml.safe_load((SKILLS / skill_id / "skill.yaml").read_text())
            self.assertIn(state, value["trigger_states"])
        question = yaml.safe_load((SKILLS / "question-framing" / "skill.yaml").read_text())
        self.assertIn("resource_read", question["tools"])
        self.assertIn("research_proposal_write", question["tools"])
        self.assertIn("ResearchProposal", question["permissions"]["artifact_kinds"])
        self.assertIn("RESOURCE_WRITE", question["permissions"]["operations"])
        question_procedure = "\n".join(question["procedure"])
        self.assertIn("provenance schema-exact", question_procedure)
        self.assertIn("only created_by and updated_by", question_procedure)
        self.assertIn("never as extra provenance keys", question_procedure)
        self.assertIn("two or more FEASIBILITY RETHINK_QUESTION verdicts", question_procedure)
        self.assertIn("one central falsifiable novelty hypothesis", question_procedure)
        self.assertIn("rather than separate novelty hypotheses", question_procedure)
        self.assertIn("do not silently drop required deliverables", question_procedure)
        self.assertIn("REVISE the most recent active ResearchQuestion in place", question_procedure)
        self.assertIn("instead of creating a new artifact ID", question_procedure)
        self.assertIn("LiteratureEvidence relations accumulate on one scientific target", question_procedure)
        self.assertIn("increment revision exactly once", question_procedure)
        planning = yaml.safe_load((SKILLS / "research-planning" / "skill.yaml").read_text())
        self.assertIn("resource_read", planning["tools"])
        planning_procedure = "\n".join(planning["procedure"])
        self.assertIn("CORE_FORMAL only when", planning_procedure)
        self.assertIn("Otherwise use ADVERSARIAL", planning_procedure)
        self.assertIn("each theorem family its own stable THEORY-track", planning_procedure)
        self.assertIn("Do not create a required REVIEW-track output", planning_procedure)
        self.assertIn("must never point superseded_by to itself", planning_procedure)
        self.assertIn("exact ResearchPlan v0.1.2 contract", planning_procedure)
        planning_doc = (SKILLS / "research-planning" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("candidate id equals research_artifact_propose.artifact_id", planning_doc)
        self.assertIn("Every work package contains exactly", planning_doc)
        self.assertIn("start materialized_outputs empty", planning_doc)
        self.assertIn("Every milestone contains exactly", planning_doc)
        self.assertIn("Provenance contains only created_by and updated_by", planning_doc)
        novelty = yaml.safe_load((SKILLS / "literature-novelty" / "skill.yaml").read_text())
        self.assertIn("academic_source_read", novelty["tools"])

    def test_proposal_novelty_contract_is_directly_constructible(self) -> None:
        value = yaml.safe_load(
            (SKILLS / "literature-novelty" / "skill.yaml").read_text()
        )
        procedure = "\n".join(value["procedure"])
        self.assertIn('"schema_version": "research-artifact/v0.1.2"', procedure)
        self.assertIn('"schema_version": "research-artifact/v0.1.3"', procedure)
        self.assertIn('"artifact_ref": {"id": "<ResearchProposal id>"', procedure)
        self.assertIn('"reviewer_type": "LITERATURE"', procedure)
        self.assertIn('"severity": "MAJOR"', procedure)
        self.assertIn("SOURCE_UNVERIFIED is a scientific limitation", procedure)
        self.assertIn(
            "In FULL_RESEARCH DISCOVERY_LITERATURE submit one or more LiteratureEvidence candidates",
            procedure,
        )
        self.assertIn("do not fail because ResearchProposal is absent", procedure)
        self.assertIn('"schema_version": "research-artifact/v0.1.2"', procedure)
        self.assertIn('"gate_type": "FEASIBILITY"', procedure)
        self.assertIn('"review_refs": [{"id": "<NOVELTY Review create id>"', procedure)
        self.assertIn("Do not use a two-field gate object", procedure)
        self.assertIn("UNCERTAIN to RETHINK_QUESTION", procedure)
        self.assertIn("never force incomplete evidence to PARTIAL", procedure)
        self.assertIn("one LiteratureEvidence candidate for every materially relied-on external work", procedure)
        self.assertIn("do not collapse several papers into one search-log artifact", procedure)
        self.assertIn("while a core hypothesis still lacks a search record", procedure)
        self.assertIn("REVISE that LiteratureEvidence to append a relation", procedure)
        self.assertIn("preserving every older relation", procedure)
        self.assertIn("Reuse verified evidence instead of rerunning the same search", procedure)
        self.assertIn("never retarget evidence whose scientific relationship changed", procedure)
        self.assertIn("pass its exact recorded uri to resource_read", procedure)
        self.assertIn("remaining uncertainty is the research obligation itself", procedure)
        self.assertIn("target-to-baseline difference cannot yet be established", procedure)
        self.assertIn("does not weaken PROPOSAL_REVIEW's final same-revision novelty gate", procedure)
        self.assertIn("assess feasibility novelty on the central claimed difference", procedure)
        self.assertIn("theorem truth belongs to theory development and verification", procedure)
        self.assertIn("may be KNOWN secondary mechanisms without erasing a distinct central target", procedure)
        self.assertIn("cross-check its outcome against canonical LiteratureEvidence", procedure)
        self.assertIn("must not claim that no verified falsifiable difference exists", procedure)
        self.assertIn("absence of a paper proving the proposed theorem as the sole reason for UNCERTAIN", procedure)
        self.assertIn("Preserve unresolved correctness as a theory obligation", procedure)
        self.assertIn("cannot accompany an OPEN/PARTIAL PASS", procedure)
        self.assertIn("discarded broad-firstness claim as an unresolved MAJOR", procedure)
        self.assertIn("supporting or conditional extension as non-blocking", procedure)
        self.assertIn('"severity": "MINOR"', procedure)
        self.assertIn("every assessed LiteratureEvidence materially relied on", procedure)
        self.assertNotIn(
            "Known or equivalent findings have verified sources",
            value["completion_conditions"],
        )

    def test_proposal_correctness_contract_is_directly_constructible(self) -> None:
        value = yaml.safe_load(
            (SKILLS / "proposal-correctness-review" / "skill.yaml").read_text()
        )
        procedure = "\n".join(value["procedure"])
        self.assertIn("research_artifact_validate", value["tools"])
        self.assertIn('"schema_version": "research-artifact/v0.1.3"', procedure)
        self.assertIn('"reviewer_type": "PROPOSAL_CORRECTNESS"', procedure)
        self.assertIn('"scheme": "PROPOSAL_CORRECTNESS"', procedure)
        self.assertIn('"severity": "BLOCKER"', procedure)
        self.assertIn("submitted with outcome SUBMITTED", procedure)

    def test_old_combined_roles_are_removed(self) -> None:
        self.assertFalse((SKILLS / "experiment-research" / "SKILL.md").exists())
        self.assertFalse((SKILLS / "synthesis-completion" / "SKILL.md").exists())

    def test_operational_skill_docs_have_all_required_sections(self) -> None:
        for skill_id in EXPECTED:
            text = (SKILLS / skill_id / "SKILL.md").read_text(encoding="utf-8")
            headings = {line for line in text.splitlines() if line.startswith("## ")}
            self.assertTrue(REQUIRED_SECTIONS <= headings, skill_id)
            self.assertIn("research_action_submit", text)
            self.assertIn("no transition authority", text)

    def test_skill_docs_are_deterministically_rendered(self) -> None:
        import subprocess

        result = subprocess.run(
            [sys.executable, "scripts/render_skill_docs.py", str(SKILLS), "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
