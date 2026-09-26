from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import hashlib
from pathlib import Path
from unittest import mock

import yaml

from research_artifacts.academic_sources import AcademicSourceTools
from research_artifacts.action_compiler import ActionCompiler
from research_artifacts.artifact_service import ArtifactService
from research_artifacts.mvp import MVPController, SyntheticSkillRuntime
from research_artifacts.proposal_review import _pdf_markdown, import_proposal_resources
from research_artifacts.runtime_types import RuntimeValidationError
from research_artifacts.sidecar import SidecarServer
from research_artifacts.workspace import ArtifactWorkspace


ROOT = Path(__file__).resolve().parents[1]


class OneRevisionRuntime(SyntheticSkillRuntime):
    """Inject one material correctness issue, then pass after revision."""

    def _skill_proposal_correctness_review(self, action, bundle):
        proposal = self._artifact(bundle, "ResearchProposal")
        if self._passes(proposal):
            return super()._skill_proposal_correctness_review(action, bundle)
        review_id = next(
            item for item in bundle["allowed_outputs"]["create_ids"]
            if item.startswith("review-")
        )
        review = self._review(
            action, bundle, review_id=review_id, target=proposal,
            scheme="PROPOSAL_CORRECTNESS", outcome="REVISION_REQUIRED",
            reviewer_type="PROPOSAL_CORRECTNESS",
        )
        review.update({
            "status": "OPEN", "recommendation": "MAJOR_REVISION",
            "summary": "The identification assumption must be made explicit.",
            "issues": [{
                "id": "identification-assumption", "title": "Identification assumption",
                "description": "The proposed conclusion depends on an unstated identification assumption.",
                "severity": "MAJOR", "status": "OPEN",
                "evidence_refs": [{"id": proposal["id"], "kind": "ResearchProposal", "revision": proposal["revision"]}],
                "locator": "Method section", "suggested_resolution": "State and justify the identification assumption.",
            }],
        })
        return [self._create(action, bundle, review, "proposal-correctness-major")], None

    @staticmethod
    def _passes(proposal):
        return int(proposal.get("revision_cycle", 0)) > 0


class AlwaysRevisionRuntime(OneRevisionRuntime):
    @staticmethod
    def _passes(_proposal):
        return False


class ProposalReviewWorkflowTest(unittest.TestCase):
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
        self._git("config", "user.name", "Proposal Review Test")
        self._git("config", "user.email", "proposal@example.invalid")
        self._git("add", ".")
        self._git("commit", "-m", "fixture baseline")
        self.controller = MVPController(self.root, SyntheticSkillRuntime(self.root))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    def _proposal(self) -> Path:
        path = self.root / "proposal.md"
        path.write_text(
            "# Evidence-pinned research\n\n## Question\nCan immutable revisions improve "
            "recoverability in long-running scientific workflows?\n\n## Method\nWe will "
            "preregister deterministic interruption, conflict, and recovery tests. Each core "
            "claim will retain its evidence and review revision.\n\n## Limits\nA missing search "
            "result will be treated as uncertain rather than evidence of novelty.\n",
            encoding="utf-8",
        )
        return path

    def test_full_research_source_materials_are_copied_pinned_and_readable(self) -> None:
        first = self.root / "theory-input.md"
        second = self.root / "review.yaml"
        first.write_text("# Immutable theorem input\n", encoding="utf-8")
        second.write_text("outcome: revision-required\n", encoding="utf-8")

        self.controller.init(
            "proj-sources", "Develop a theory from pinned inputs",
            source_materials=[first, second],
        )
        project = ArtifactWorkspace(self.root).get("proj-sources")
        self.assertEqual(project.data["schema_version"], "research-artifact/v0.1.4")
        self.assertEqual(project.data["workflow_mode"], "FULL_RESEARCH")
        self.assertEqual(len(project.data["input_resources"]), 3)
        copied = project.data["input_resources"][0]
        copied_path = self.root / copied["uri"]
        self.assertEqual(copied_path.read_text(encoding="utf-8"), first.read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(copied_path.read_bytes()).hexdigest(), copied["sha256"])

        first.write_text("# Mutated original\n", encoding="utf-8")
        self.assertEqual(copied_path.read_text(encoding="utf-8"), "# Immutable theorem input\n")
        result = AcademicSourceTools(self.root).resource_read(
            project_id="proj-sources", uri=copied["uri"]
        )
        self.assertEqual(result["content"], "# Immutable theorem input\n")
        ArtifactWorkspace(self.root).require_valid()

        with self.assertRaisesRegex(RuntimeValidationError, "FULL_RESEARCH"):
            self.controller.init(
                "proj-invalid-sources", "Invalid mixed mode", mode="proposal-review",
                proposal=self._proposal(), source_materials=[second],
            )

    def test_markdown_review_approval_and_conversion(self) -> None:
        self.controller.init(
            "proj-review", "Review the proposal", mode="proposal-review",
            proposal=self._proposal(),
        )
        result = self.controller.run("proj-review", budget_percent=100)
        self.assertEqual(result["reason"], "WAITING_FINAL_APPROVAL", result)
        self.assertEqual(result["checkpoint"]["state"], "PROPOSAL_FINAL_APPROVAL")
        gate = result["checkpoint"]["gate_results"][-1]
        self.assertEqual(gate["verdict"], "PASS")
        self.assertEqual({ref["kind"] for ref in gate["review_refs"]}, {"Review"})
        self.controller.approve("proj-review")
        proposal = ArtifactWorkspace(self.root).get("proposal-review")
        self.assertEqual(proposal.data["status"], "APPROVED")
        converted = self.controller.convert("proj-review", "proj-derived")
        self.assertEqual(converted["source_proposal_ref"]["revision"], proposal.data["revision"])
        derived = ArtifactWorkspace(self.root).get("proj-derived")
        self.assertEqual(derived.data["workflow_mode"], "FULL_RESEARCH")
        self.assertEqual(derived.data["source_proposal_ref"], converted["source_proposal_ref"])
        ArtifactWorkspace(self.root).require_valid()

    def test_material_issue_requires_decision_and_one_re_review_cycle(self) -> None:
        controller = MVPController(self.root, OneRevisionRuntime(self.root))
        controller.init(
            "proj-revise", "Review and revise", mode="proposal-review",
            proposal=self._proposal(),
        )
        waiting = controller.run("proj-revise", budget_percent=100)
        self.assertEqual(waiting["reason"], "WAITING_HUMAN:PROPOSAL_DECISIONS", waiting)
        decision_file = Path(waiting["decision_file"])
        template = yaml.safe_load(decision_file.read_text(encoding="utf-8"))
        self.assertEqual(template["decisions"][0]["issue_id"], "identification-assumption")

        template["decisions"][0].update({
            "action": "REJECT", "reason": "The proposed wording is not acceptable yet.",
        })
        decision_file.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
        rejected = controller.proposal_decisions("proj-revise", decision_file)
        self.assertFalse(rejected["all_authorized"])
        self.assertEqual(controller.status("proj-revise")["state"], "PROPOSAL_WAITING_DECISIONS")

        template["decisions"][0].update({
            "action": "EDIT", "instruction": "State the exact identifying assumption and narrow the conclusion.",
        })
        decision_file.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
        accepted = controller.proposal_decisions("proj-revise", decision_file)
        self.assertTrue(accepted["all_authorized"])
        completed_review = controller.run("proj-revise")
        self.assertEqual(completed_review["reason"], "WAITING_FINAL_APPROVAL")
        proposal = ArtifactWorkspace(self.root).get("proposal-revise")
        self.assertEqual(proposal.data["revision_cycle"], 1)
        self.assertTrue((self.root / "projects/proj-revise/resources/proposal/revisions/rev-002.md").is_file())
        self.assertTrue((self.root / "projects/proj-revise/resources/proposal/changes/rev-002.yaml").is_file())
        decisions = ArtifactWorkspace(self.root).query(kind="Decision", project_id="proj-revise")
        self.assertEqual({
            item.data["proposal_issue"]["action"] for item in decisions
            if "proposal_issue" in item.data
        }, {"REJECT", "EDIT"})
        sessions = {
            item.data["reviewer"]["actor"]["session_id"]
            for item in ArtifactWorkspace(self.root).query(kind="Review", project_id="proj-revise")
        }
        self.assertEqual(len(sessions), len(ArtifactWorkspace(self.root).query(kind="Review", project_id="proj-revise")))
        ArtifactWorkspace(self.root).require_valid()

    def test_default_mode_remains_full_research(self) -> None:
        checkpoint = self.controller.init("proj-legacy", "Legacy full workflow")
        self.assertEqual(checkpoint["workflow_mode"], "FULL_RESEARCH")
        project = ArtifactWorkspace(self.root).get("proj-legacy")
        self.assertEqual(project.data["schema_version"], "research-artifact/v0.1.2")

    def test_production_structuring_action_can_read_and_write_only_source_map_fields(self) -> None:
        checkpoint = self.controller.init(
            "proj-action-contract", "Compile proposal action",
            mode="proposal-review", proposal=self._proposal(),
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        action_id = "run-action-contract-1-proposal-structuring"
        begun = self.controller.transactions.begin_action(
            project_id="proj-action-contract", action_id=action_id,
            input_git_commit=head, expected_seq=checkpoint["checkpoint_seq"],
        )
        pending = begun["checkpoint"]["pending_action"]
        compiled = ActionCompiler(self.root).compile(
            project_id="proj-action-contract", action_id=action_id,
            input_git_commit=pending["input_git_commit"],
        )["action"]
        self.assertIn("resource_read", compiled["allowed_tools"])
        self.assertIn("research_proposal_write", compiled["allowed_tools"])
        proposal_rule = next(
            rule for rule in compiled["field_permissions"]
            if rule["kind"] == "ResearchProposal"
        )
        self.assertEqual(proposal_rule["operations"], ["REVISE"])
        self.assertEqual(
            set(proposal_rule["fields"]),
            {"revision", "updated_at", "provenance.updated_by", "status", "question_refs", "source_map"},
        )

    def test_sidecar_exposes_production_proposal_gate_contract(self) -> None:
        self.controller.init(
            "proj-sidecar-review", "Review through sidecar",
            mode="proposal-review", proposal=self._proposal(),
        )
        stopped = self.controller.run(
            "proj-sidecar-review", max_actions=4, budget_percent=100,
        )
        self.assertEqual(stopped["checkpoint"]["state"], "PROPOSAL_REVIEW_GATE")
        response = SidecarServer(self.root).dispatch({
            "protocol": "research-sidecar/v0.1",
            "request_id": "proposal-gate",
            "method": "proposal.evaluate_gate",
            "params": {"project_id": "proj-sidecar-review"},
        })
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["result"]["gate_type"], "PROPOSAL_REVIEW")
        self.assertEqual(response["result"]["verdict"], "PASS")

    def test_three_revision_limit_blocks_without_a_fourth_revision(self) -> None:
        controller = MVPController(self.root, AlwaysRevisionRuntime(self.root))
        controller.init(
            "proj-limit", "Exercise revision limit", mode="proposal-review",
            proposal=self._proposal(),
        )
        result = controller.run("proj-limit", budget_percent=100)
        for cycle in range(3):
            self.assertEqual(result["reason"], "WAITING_HUMAN:PROPOSAL_DECISIONS", result)
            path = Path(result["decision_file"])
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            value["decisions"][0]["action"] = "ACCEPT"
            path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
            controller.proposal_decisions("proj-limit", path)
            result = controller.run("proj-limit")
        self.assertEqual(result["reason"], "BLOCKED")
        self.assertEqual(result["checkpoint"]["proposal_revision_cycles"], 3)
        proposal = ArtifactWorkspace(self.root).get("proposal-limit")
        self.assertEqual(proposal.data["revision_cycle"], 3)
        self.assertFalse(
            (self.root / "projects/proj-limit/resources/proposal/revisions/rev-005.md").exists()
        )

    def test_academic_network_failure_is_source_unverified(self) -> None:
        self.controller.init(
            "proj-sources", "Review sources", mode="proposal-review",
            proposal=self._proposal(),
        )
        tools = AcademicSourceTools(self.root)
        with mock.patch(
            "research_artifacts.academic_sources.AcademicSourceTools._open_public",
            side_effect=urllib.error.URLError("offline"),
        ):
            result = tools.academic_search(
                project_id="proj-sources", action_id="action-search",
                query="immutable scientific artifacts",
            )
        self.assertEqual(result["status"], "SOURCE_UNVERIFIED")

    def test_markdown_and_rubric_import_are_immutable_and_hash_pinned(self) -> None:
        proposal = self._proposal()
        rubric = self.root / "rubric.md"
        rubric.write_text(
            "# Review rubric\n\nCheck novelty, factual support, identification, feasibility, "
            "and whether each conclusion is appropriately qualified.\n",
            encoding="utf-8",
        )
        imported = import_proposal_resources(
            self.root, "proj-import", proposal, rubric,
        )
        for key in ("source_document", "current_document", "rubric_source_document", "rubric_document"):
            resource = imported[key]
            copied = self.root / resource["uri"]
            self.assertEqual(hashlib.sha256(copied.read_bytes()).hexdigest(), resource["sha256"])
        copied_source = self.root / imported["source_document"]["uri"]
        before = copied_source.read_bytes()
        proposal.write_text("# Replaced original\n" + "x" * 100, encoding="utf-8")
        self.assertEqual(copied_source.read_bytes(), before)
        self.assertEqual(imported["language"], "en")
        self.assertTrue(imported["section_index"])
        with self.assertRaises(RuntimeValidationError):
            import_proposal_resources(self.root, "../escape", proposal)

    def test_pdf_extractor_preserves_pages_and_rejects_unreliable_metadata(self) -> None:
        path = self.root / "proposal.pdf"
        path.write_bytes(b"%PDF fixture")
        info = subprocess.CompletedProcess(
            ["pdfinfo"], 0, stdout="Pages: 2\nEncrypted: no\n", stderr="",
        )
        page = "Reliable proposal text " * 8
        extracted = subprocess.CompletedProcess(
            ["pdftotext"], 0, stdout=f"{page}\f{page}\f", stderr="",
        )
        with mock.patch(
            "research_artifacts.proposal_review.shutil.which", side_effect=["/bin/pdftext", "/bin/pdfinfo"],
        ), mock.patch(
            "research_artifacts.proposal_review.subprocess.run", side_effect=[info, extracted],
        ):
            markdown = _pdf_markdown(path)
        self.assertIn("<!-- proposal-page: 1 -->", markdown)
        self.assertIn("<!-- proposal-page: 2 -->", markdown)

        encrypted = subprocess.CompletedProcess(
            ["pdfinfo"], 0, stdout="Pages: 2\nEncrypted: yes\n", stderr="",
        )
        with mock.patch(
            "research_artifacts.proposal_review.shutil.which", side_effect=["/bin/pdftext", "/bin/pdfinfo"],
        ), mock.patch(
            "research_artifacts.proposal_review.subprocess.run", return_value=encrypted,
        ), self.assertRaises(RuntimeValidationError):
            _pdf_markdown(path)

        with mock.patch(
            "research_artifacts.proposal_review.shutil.which", side_effect=["/bin/pdftext", "/bin/pdfinfo"],
        ), mock.patch(
            "research_artifacts.proposal_review.subprocess.run",
            side_effect=subprocess.TimeoutExpired("pdfinfo", 30),
        ), self.assertRaises(RuntimeValidationError):
            _pdf_markdown(path)

    def test_material_revision_authority_requires_an_accepted_issue_decision(self) -> None:
        immutable = {"uri": "projects/proj-review/resources/proposal/source/original.md"}
        current = {
            "id": "proposal-review", "revision": 4, "revision_cycle": 0,
            "source_document": immutable, "rubric_source_document": None,
            "rubric_document": None, "proposal_type": "ACADEMIC_RESEARCH", "language": "en",
            "current_document": {"uri": "projects/proj-review/resources/proposal/revisions/rev-001.md"},
            "change_log_refs": [],
        }
        candidate = dict(current)
        candidate.update({
            "revision_cycle": 1,
            "current_document": {"uri": "projects/proj-review/resources/proposal/revisions/rev-002.md"},
            "change_log_refs": [{"uri": "projects/proj-review/resources/proposal/changes/rev-002.yaml"}],
        })
        review = {
            "kind": "Review",
            "target": {"artifact_ref": {"id": "proposal-review", "revision": 4}},
            "issues": [{"id": "material-method", "severity": "MAJOR", "status": "OPEN"}],
        }
        context = {"artifacts": [{"content": review}]}
        with self.assertRaisesRegex(RuntimeValidationError, "lack accepted Decisions"):
            ArtifactService._validate_proposal_revision_authority(current, candidate, context)
        context["artifacts"].append({"content": {
            "kind": "Decision", "status": "ACCEPTED",
            "proposal_issue": {"issue_id": "material-method", "action": "EDIT"},
        }})
        ArtifactService._validate_proposal_revision_authority(current, candidate, context)

        # Failed actions may have occupied the next create-only resource names.
        # A retry can skip those names, but its document and change log must keep
        # one matching ordinal so no prior resource is overwritten.
        collision_safe = dict(candidate)
        collision_safe.update({
            "current_document": {
                "uri": "projects/proj-review/resources/proposal/revisions/rev-006.md"
            },
            "change_log_refs": [{
                "uri": "projects/proj-review/resources/proposal/changes/rev-006.yaml"
            }],
        })
        ArtifactService._validate_proposal_revision_authority(
            current, collision_safe, context
        )
        mismatched = dict(collision_safe)
        mismatched["change_log_refs"] = [{
            "uri": "projects/proj-review/resources/proposal/changes/rev-005.yaml"
        }]
        with self.assertRaisesRegex(RuntimeValidationError, "same immutable"):
            ArtifactService._validate_proposal_revision_authority(
                current, mismatched, context
            )
        stale_ordinal = dict(collision_safe)
        stale_ordinal["current_document"] = {
            "uri": "projects/proj-review/resources/proposal/revisions/rev-000.md"
        }
        stale_ordinal["change_log_refs"] = [{
            "uri": "projects/proj-review/resources/proposal/changes/rev-000.yaml"
        }]
        with self.assertRaisesRegex(RuntimeValidationError, "N >= 002"):
            ArtifactService._validate_proposal_revision_authority(
                current, stale_ordinal, context
            )
        changed_source = dict(candidate)
        changed_source["source_document"] = {"uri": "replacement.md"}
        with self.assertRaisesRegex(RuntimeValidationError, "immutable field source_document"):
            ArtifactService._validate_proposal_revision_authority(current, changed_source, context)

    @unittest.skipUnless(shutil.which("pdftotext"), "Poppler is required")
    def test_pdf_without_reliable_text_layer_fails_closed(self) -> None:
        path = self.root / "scan.pdf"
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
        with self.assertRaises(RuntimeValidationError):
            _pdf_markdown(path)

    @unittest.skipUnless(
        shutil.which("pdftotext") and shutil.which("libreoffice"),
        "Poppler and LibreOffice are required for the PDF golden flow",
    )
    def test_text_layer_pdf_golden_review_flow(self) -> None:
        source = self.root / "pdf-proposal.txt"
        source.write_text(
            "Evidence-pinned proposal review\n\nResearch question\nCan immutable revision pins "
            "improve recoverability and auditability in long-running scientific workflows?\n\n"
            "Hypothesis and contribution\nExplicit question, plan, evidence, review, and decision "
            "artifacts should reduce scientific state loss after interruption. The contribution is "
            "a deterministic control protocol whose gates operate on exact revisions.\n\nMethod\n"
            "We preregister interruption, recovery, conflict, and stale-review tests. Successful, "
            "negative, and failed outcomes are retained. Missing literature remains uncertain.\n",
            encoding="utf-8",
        )
        try:
            subprocess.run(
                [shutil.which("libreoffice") or "libreoffice", "--headless", "--convert-to", "pdf", "--outdir", str(self.root), str(source)],
                check=True, capture_output=True, text=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            self.skipTest(f"LibreOffice PDF conversion is unavailable: {exc}")
        pdf = self.root / "pdf-proposal.pdf"
        self.controller.init(
            "proj-pdf", "Review PDF proposal", mode="proposal-review", proposal=pdf,
        )
        result = self.controller.run("proj-pdf", budget_percent=100)
        self.assertEqual(result["reason"], "WAITING_FINAL_APPROVAL")
        normalized = self.root / "projects/proj-pdf/resources/proposal/revisions/rev-001.md"
        self.assertIn("<!-- proposal-page: 1 -->", normalized.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
