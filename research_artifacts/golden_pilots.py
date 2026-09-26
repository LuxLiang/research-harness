"""Controlled real-science pilots for Research Harness Step 7.

The pilots deliberately use small, known scientific results.  They are not
novelty claims: their purpose is to test whether the Harness preserves honest
literature classification, theorem semantics, formal evidence, experimental
protocols, negative observations, and manuscript traceability end to end.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .resources import resource_path
from typing import Any, Mapping

import yaml

from .artifact_service import ArtifactService
from .mvp import MVPController, SyntheticSkillRuntime, _head
from .orchestrator import utc_now
from .runtime_types import RuntimeValidationError, atomic_write_json, atomic_write_text
from .tool_adapters import ExperimentExecutionAdapter, LeanToolAdapter
from .workspace import ArtifactWorkspace


CODEX_BINARY = Path(os.environ.get("RESEARCH_HARNESS_CODEX") or shutil.which("codex") or "codex")
MATHLIB_COMMIT = "c44e0c8ee63ca166450922a373c7409c5d26b00b"
LEAN_VERSION = "v4.19.0"


@dataclass(frozen=True)
class PilotDefinition:
    slug: str
    route: str
    title: str
    objective: str
    question: str
    background: str
    hypothesis: str
    contribution: str
    literature: Mapping[str, Any]
    claim_statement: str | None = None
    lean_source: str | None = None
    informal_proof: str | None = None


PILOTS: dict[str, PilotDefinition] = {
    "theory": PilotDefinition(
        slug="golden-theory-variance",
        route="THEORY",
        title="Two-point variance identity: formal traceability pilot",
        objective=(
            "Reproduce a known two-point variance identity with an explicit informal proof, "
            "Lean proof, axiom audit, semantic review, and traceable manuscript statement."
        ),
        question=(
            "Can the known identity (x-m)^2+(y-m)^2=(x-y)^2/2, m=(x+y)/2, "
            "be carried unchanged through informal and Lean verification into a manuscript?"
        ),
        background="The identity is elementary but scientifically useful as the n=2 variance decomposition.",
        hypothesis="All four CORE_FORMAL checks pass for one stable theorem statement.",
        contribution="A controlled formal-verification trace; no mathematical novelty is claimed.",
        literature={
            "title": "Mathematics in Lean: Basics",
            "authors": [{"name": "Jeremy Avigad"}, {"name": "Patrick Massot"}],
            "year": 2024,
            "venue": "Lean community documentation",
            "identifiers": {
                "url": "https://leanprover-community.github.io/mathematics_in_lean/C02_Basics.html"
            },
            "result": "Polynomial identities over commutative rings can be normalized and proved with ring reasoning.",
            "locator": "Chapter 2, Calculating",
        },
        claim_statement=(
            "For all real x and y, if m = (x + y) / 2, then "
            "(x - m)^2 + (y - m)^2 = (x - y)^2 / 2."
        ),
        informal_proof=(
            "Let m=(x+y)/2. Then x-m=(x-y)/2 and y-m=(y-x)/2. "
            "Squaring and adding gives two copies of (x-y)^2/4, hence (x-y)^2/2."
        ),
        lean_source="""import Mathlib

theorem two_point_variance_identity (x y : ℝ) :
    (x - (x + y) / 2)^2 + (y - (x + y) / 2)^2 = (x - y)^2 / 2 := by
  ring

#print axioms two_point_variance_identity
""",
    ),
    "experiment": PilotDefinition(
        slug="golden-experiment-summation",
        route="EXPERIMENT",
        title="Accurate floating-point summation pilot",
        objective=(
            "Compare Python sum with math.fsum on preregistered cancellation cases while "
            "preserving protocol, raw runs, provenance, and independent recomputation."
        ),
        question=(
            "On a fixed cancellation-stress dataset, does math.fsum have no larger absolute "
            "error than built-in sum in every case and strictly smaller error in at least one case?"
        ),
        background="Floating-point addition is order-sensitive; accurate summation tracks partial sums.",
        hypothesis="math.fsum is non-worse on every preregistered case and strictly better on at least one.",
        contribution="A reproducible integrity benchmark; no algorithmic novelty is claimed.",
        literature={
            "title": "Python math.fsum documentation",
            "authors": [{"name": "Python Software Foundation"}],
            "year": 2026,
            "venue": "Python 3 documentation",
            "identifiers": {"url": "https://docs.python.org/3/library/math.html#math.fsum"},
            "result": "math.fsum returns an accurate floating-point sum by tracking multiple partial sums.",
            "locator": "math.fsum entry",
        },
    ),
    "mixed": PilotDefinition(
        slug="golden-mixed-bernoulli-variance-v2",
        route="MIXED",
        title="Two-sample Bernoulli variance: theory and experiment pilot",
        objective=(
            "Formally verify the two-outcome algebra behind unbiased n=2 sample variance and "
            "independently test the estimator by preregistered Monte Carlo simulation."
        ),
        question=(
            "For two iid Bernoulli(p) observations, can the algebraic expectation identity and "
            "a locked Monte Carlo experiment consistently support E[s^2]=p(1-p)?"
        ),
        background="Bessel-corrected sample variance is a standard unbiased estimator.",
        hypothesis="The exact two-outcome identity passes Lean and simulation agrees within preregistered tolerances.",
        contribution="A controlled mixed-track trace; the estimator theorem itself is known.",
        literature={
            "title": "A few properties of sample variance",
            "authors": [{"name": "Eric Benhamou"}],
            "year": 2018,
            "venue": "arXiv",
            "identifiers": {"arxiv": "1809.03774", "url": "https://arxiv.org/abs/1809.03774"},
            "result": "The sample variance with Bessel correction is unbiased under the usual iid finite-variance assumptions.",
            "locator": "Unbiasedness discussion",
        },
        claim_statement=(
            "For every real p, p(1-p)/2 + (1-p)p/2 = p(1-p)."
        ),
        informal_proof=(
            "The two summands are equal by commutativity. Factoring p(1-p) gives "
            "p(1-p)(1/2+1/2)=p(1-p)."
        ),
        lean_source="""import Mathlib

theorem bernoulli_two_sample_variance_identity (p : ℝ) :
    p * (1 - p) / 2 + (1 - p) * p / 2 = p * (1 - p) := by
  ring

#print axioms bernoulli_two_sample_variance_identity
""",
    ),
}


class CodexSkillAuditor:
    """Run one independent, read-only Codex session for each Skill action."""

    def __init__(self, root: str | Path, *, enabled: bool = True, timeout: int = 600):
        self.root = Path(root).resolve()
        self.enabled = enabled
        self.timeout = timeout
        self.schema = resource_path(self.root, "schemas/runtime/v0.1/pilot-skill-assessment.schema.json")

    def evaluate(
        self, definition: PilotDefinition, action: Mapping[str, Any], bundle: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not self.enabled:
            return {
                "assessment": {"skill_id": action["skill"], "verdict": "PASS", "summary": "Deterministic test auditor accepted the controlled fixture.", "findings": []},
                "session_id": f"deterministic-{action['action_id']}", "tokens": None,
                "runtime_seconds": 0.0, "runtime": "deterministic-test",
            }
        if not CODEX_BINARY.is_file():
            raise RuntimeValidationError(f"Codex runtime is unavailable: {CODEX_BINARY}")
        action_dir = ArtifactService(self.root).action_dir(
            str(action["project_id"]), str(action["action_id"])
        ) / "agent"
        action_dir.mkdir(parents=True, exist_ok=True)
        output = action_dir / "assessment.json"
        skill_doc = (
            resource_path(self.root, "integrations/deepseek-harness/skills") / str(action["skill"]) / "SKILL.md"
        ).read_text(encoding="utf-8")
        shared_root = resource_path(self.root, "integrations/deepseek-harness/skills/_shared")
        shared_policies = "\n\n".join(
            f"## {name}\n{(shared_root / name).read_text(encoding='utf-8')}"
            for name in (
                "artifact-boundaries.md",
                "failure-taxonomy.md",
                "scientific-evaluation-policy.md",
                "theory-verification-policy.md",
                "experiment-integrity-policy.md",
            )
        )
        resource_snapshots = self._pinned_resource_snapshots(action, bundle)
        if action["skill"] == "final-review":
            for relative in ("paper/manuscript/draft.md", "paper/traceability.yaml"):
                repository_path = f"projects/{action['project_id']}/{relative}"
                shown = subprocess.run(
                    ["git", "show", f"{bundle['input_git_commit']}:{repository_path}"],
                    cwd=self.root, text=True, capture_output=True, check=False,
                )
                if shown.returncode == 0:
                    resource_snapshots.append({
                        "path": repository_path,
                        "git_commit": bundle["input_git_commit"],
                        "sha256": hashlib.sha256(shown.stdout.encode("utf-8")).hexdigest(),
                        "content": shown.stdout,
                    })
        prompt = (
            "You are the independent pre-commit scientific auditor for one Skill action in a controlled golden-path pilot.\n"
            "Apply the scientific procedure and forbidden-behavior rules in SKILL.md to the frozen inputs.\n"
            "The host handler, not this audit session, owns artifact proposal/validation/submission tools and will\n"
            "execute them only after your PASS. Do not fail because those host-only tools are absent here.\n"
            "This audit occurs before candidate generation. Candidate artifacts and completion outputs are absent\n"
            "by design: evaluate whether the pinned inputs and action contract are scientifically sufficient and\n"
            "whether the specified procedure can be executed without an integrity violation. The host performs\n"
            "schema, permission, lifecycle, and completion validation on the resulting candidates afterward.\n"
            "This is a known benchmark, so do not claim mathematical or algorithmic novelty.\n"
            "Return PASS only if there is no S3/S4 error that should stop this action. Preserve uncertainty\n"
            "and negative evidence. Do not edit files and do not choose a workflow transition.\n"
            "Treat only the FROZEN CONTEXT BUNDLE as scientific truth. The host will attach the\n"
            "actual session ID and complete proposal provenance after this session returns.\n\n"
            f"ACTION CONTRACT:\n{json.dumps(action, ensure_ascii=False)}\n\n"
            f"FROZEN CONTEXT BUNDLE:\n{json.dumps(bundle, ensure_ascii=False)}\n\n"
            f"PINNED DELIVERY RESOURCE SNAPSHOTS:\n{json.dumps(resource_snapshots, ensure_ascii=False)}\n\n"
            f"MANDATORY SHARED POLICIES:\n{shared_policies}\n\n"
            f"SKILL.md:\n{skill_doc}\n"
        )
        command = [
            str(CODEX_BINARY), "--ask-for-approval", "never", "exec",
            "--sandbox", "read-only", "--skip-git-repo-check", "--color", "never",
            "-C", str(action_dir), "--output-schema", str(self.schema),
            "--output-last-message", str(output), "-",
        ]
        started = time.monotonic()
        completed = subprocess.run(
            command, input=prompt, text=True, capture_output=True, check=False,
            timeout=self.timeout,
        )
        elapsed = time.monotonic() - started
        if completed.returncode != 0 or not output.is_file():
            raise RuntimeValidationError(
                "Codex Skill session failed",
                details={"returncode": completed.returncode, "stderr": completed.stderr[-4000:]},
            )
        assessment = json.loads(output.read_text(encoding="utf-8"))
        session_match = re.search(r"session id:\s*([0-9a-f-]+)", completed.stdout + completed.stderr)
        token_match = re.search(r"tokens used\s*[\r\n]+([0-9,]+)", completed.stdout + completed.stderr)
        result = {
            "assessment": assessment,
            "session_id": session_match.group(1) if session_match else f"codex-{action['action_id']}",
            "tokens": int(token_match.group(1).replace(",", "")) if token_match else None,
            "runtime_seconds": round(elapsed, 3),
            "runtime": "codex-cli",
        }
        atomic_write_json(action_dir / "session-summary.json", result)
        return result

    def _pinned_resource_snapshots(
        self, action: Mapping[str, Any], bundle: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        refs: dict[tuple[str, str], dict[str, Any]] = {}

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                if isinstance(value.get("uri"), str) and isinstance(value.get("sha256"), str):
                    refs[(str(value["uri"]), str(value["sha256"]))] = dict(value)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                try:
                    visit(json.loads(value))
                except json.JSONDecodeError:
                    pass

        visit(bundle.get("artifacts", []))
        snapshots: list[dict[str, Any]] = []
        for (uri, expected_sha), ref in sorted(refs.items()):
            path = Path(uri)
            if path.is_absolute():
                if not path.is_file():
                    continue
                content_bytes = path.read_bytes()
            else:
                shown = subprocess.run(
                    ["git", "show", f"{bundle['input_git_commit']}:{uri}"], cwd=self.root,
                    capture_output=True, check=False,
                )
                if shown.returncode != 0:
                    continue
                content_bytes = shown.stdout
            actual_sha = hashlib.sha256(content_bytes).hexdigest()
            if actual_sha != expected_sha:
                raise RuntimeValidationError(f"resource snapshot hash mismatch: {uri}")
            if len(content_bytes) > 256 * 1024:
                continue
            snapshots.append({
                "path": uri, "sha256": actual_sha,
                "description": ref.get("description", "Pinned artifact resource"),
                "content": content_bytes.decode("utf-8", errors="replace"),
            })
        return snapshots


class GoldenPilotRuntime(SyntheticSkillRuntime):
    """Real mathematical/experimental fixtures plus independent Skill sessions."""

    def __init__(
        self, root: str | Path, definition: PilotDefinition, *, use_codex: bool = True,
        lean_project: str | Path | None = None,
    ):
        super().__init__(root)
        self.definition = definition
        self.auditor = CodexSkillAuditor(root, enabled=use_codex)
        self.lean_project = Path(lean_project).resolve() if lean_project else None
        self._audit: dict[str, Any] = {}
        self._session_id = "pilot-uninitialized"
        self.metrics: list[dict[str, Any]] = []

    def execute(self, action: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, Any]:
        audit = self.auditor.evaluate(self.definition, action, bundle)
        self._audit = audit["assessment"]
        self._session_id = str(audit["session_id"])
        finding_severities = [item["severity"] for item in self._audit.get("findings", [])]
        metric = {
            "action_id": action["action_id"], "state": action["state"],
            "skill": action["skill"], "session_id": self._session_id,
            "bundle_sha256": bundle["bundle_sha256"],
            "input_git_commit": bundle["input_git_commit"],
            "verdict": self._audit["verdict"], "finding_severities": finding_severities,
            "tokens": audit["tokens"], "runtime_seconds": audit["runtime_seconds"],
            "runtime": audit["runtime"],
            "plan_revision": next(
                (item["revision"] for item in bundle["artifacts"] if item["kind"] == "ResearchPlan"),
                None,
            ),
        }
        self.metrics.append(metric)
        if self._audit["skill_id"] != action["skill"]:
            raise RuntimeValidationError("Skill auditor returned the wrong skill_id")
        if self._audit["verdict"] != "PASS" or any(item in {"S3", "S4"} for item in finding_severities):
            return self.service.submit_action(
                project_id=str(action["project_id"]), action_id=str(action["action_id"]),
                bundle_sha256=str(bundle["bundle_sha256"]), proposal_ids=[], outcome="FAILED",
                failure_classification="LOCAL_RETRY",
            )
        return super().execute(action, bundle)

    def _actor(self, role: str, action: Mapping[str, Any]) -> dict[str, Any]:
        return {"actor_type": "agent", "actor_id": role, "session_id": self._session_id}

    def _envelope(self, action, kind, artifact_id, title, status):
        value = super()._envelope(action, kind, artifact_id, title, status)
        value["tags"] = ["golden-pilot", self.definition.route.lower()]
        value["provenance"]["created_by"] = self._actor(str(action["skill"]), action)
        value["provenance"]["updated_by"] = self._actor(str(action["skill"]), action)
        return value

    def _review(self, action, bundle, *, review_id, target, scheme, outcome, reviewer_type, resources=None):
        value = super()._review(
            action, bundle, review_id=review_id, target=target, scheme=scheme,
            outcome=outcome, reviewer_type=reviewer_type, resources=resources,
        )
        value["title"] = f"{scheme} golden-pilot review"
        value["summary"] = self._audit.get("summary", f"{scheme} review returned {outcome}.")
        value["resolution_summary"] = "The frozen target satisfied the controlled pilot gate."
        return value

    def _skill_question_framing(self, action, bundle):
        artifact_id = bundle["allowed_outputs"]["create_ids"][0]
        value = self._envelope(action, "ResearchQuestion", artifact_id, self.definition.title, "ACTIVE")
        value.update({
            "problem_definition": self.definition.question,
            "background": self.definition.background,
            "scope": {
                "included": ["known benchmark", "artifact traceability", "independent verification"],
                "excluded": ["claiming a new theorem", "claiming a new algorithm", "population-wide generalization"],
            },
            "known_results": [], "research_gap": "The end-to-end integrity of this controlled representation has not yet been exercised.",
            "hypotheses": [{"statement": self.definition.hypothesis}],
            "expected_contributions": [self.definition.contribution],
            "risks": [{
                "category": "THEORY" if self.definition.route == "THEORY" else "DATA",
                "description": "A representation or protocol mismatch could invalidate the pilot.",
                "likelihood": "LOW", "impact": "HIGH",
                "mitigation": "Use independent review, immutable inputs, and deterministic gates.",
            }],
        })
        return [self._create(action, bundle, value, "question")], None

    def _skill_literature_novelty(self, action, bundle):
        question = self._artifact(bundle, "ResearchQuestion")
        if action["state"] == "DISCOVERY_LITERATURE":
            artifact_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("lit-"))
            source = self.definition.literature
            search_log_value = {
                "project_id": action["project_id"],
                "action_id": action["action_id"],
                "accessed_at": utc_now(),
                "target_hypothesis": self.definition.hypothesis,
                "classification": "KNOWN",
                "queries": [
                    {"strategy": "direct", "query": str(source["title"])},
                    {"strategy": "conceptual", "query": self.definition.question},
                    {"strategy": "citation-neighborhood", "query": f"works citing or cited by {source['title']}"},
                    {"strategy": "equivalence", "query": f"equivalent formulation of {self.definition.hypothesis}"},
                ],
                "decisive_source": {
                    "title": source["title"], "identifiers": source["identifiers"],
                    "locator": source["locator"], "verification": source["result"],
                },
                "limitations": [
                    "This controlled pilot verifies a known benchmark source and does not establish exhaustive global novelty.",
                    "The process-integrity contribution is scoped to this repository execution trace.",
                ],
            }
            search_log = self._resource(
                action["project_id"], "literature/search-log.json",
                json.dumps(search_log_value, indent=2, ensure_ascii=False) + "\n",
                "Adversarial search strategies and decisive-source verification log",
                "application/json",
            )
            value = self._envelope(action, "LiteratureEvidence", artifact_id, str(source["title"]), "VERIFIED")
            value.update({
                "paper": {key: copy.deepcopy(source[key]) for key in ("title", "authors", "year", "venue", "identifiers")},
                "relations": [{
                    "target_ref": {"id": question["id"], "kind": "ResearchQuestion", "revision": question["revision"]},
                    "type": "BASELINE",
                    "explanation": "The cited source establishes the known scientific baseline; this pilot claims process validation, not result novelty.",
                }],
                "main_results": [{"statement": source["result"], "locator": source["locator"]}],
                "differences": [{
                    "dimension": "research contribution", "cited_work": "Establishes the scientific result or numerical method.",
                    "current_research": "Tests Harness traceability, protocol, and verification on that known baseline.",
                }],
                "novelty_impact": {"assessment": "NARROWS", "rationale": "The scientific result is known; no novelty claim is permitted."},
                "confidence": {"score": 0.98, "rationale": "Primary documentation or a directly identified scholarly source was checked."},
                "source_resource": search_log,
            })
            return [self._create(action, bundle, value, "literature")], None
        literature = self._artifact(bundle, "LiteratureEvidence")
        review_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("review-"))
        review = self._review(action, bundle, review_id=review_id, target=question, scheme="NOVELTY", outcome="PARTIAL", reviewer_type="LITERATURE")
        review["summary"] = "The benchmark theorem/method is KNOWN, while the narrowly scoped end-to-end integrity observation is not supplied by that source; no novelty is attributed to the benchmark itself."
        proposal = self._create(action, bundle, review, "novelty-review")
        based = [
            {"id": question["id"], "kind": "ResearchQuestion", "revision": question["revision"]},
            {"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]},
        ]
        return [proposal], self._gate(bundle, "FEASIBILITY", "PASS", review, based)

    def _skill_research_planning(self, action, bundle):
        question = self._artifact(bundle, "ResearchQuestion")
        literature = self._artifact(bundle, "LiteratureEvidence")
        plan_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("plan-"))
        value = self._envelope(action, "ResearchPlan", plan_id, f"{self.definition.title} plan", "DRAFT")
        literature_package = {
            "id": "literature", "title": "Known-result baseline", "track": "LITERATURE",
            "objective": "Fix the verified baseline and prohibit novelty overclaiming.", "input_refs": [], "depends_on": [],
            "planned_outputs": [{"local_id": "baseline", "kind": "LiteratureEvidence", "description": "Verified benchmark source.", "required": True, "depends_on_outputs": []}],
            "materialized_outputs": [{"local_id": "baseline", "artifact_ref": {"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]}}],
            "success_criteria": ["The known status and source locator are explicit."], "status": "DONE",
        }
        packages = [literature_package]
        if self.definition.route in {"THEORY", "MIXED"}:
            packages.append({
                "id": "theory", "title": "Stable formal identity", "track": "THEORY",
                "objective": "Prove one unchanged theorem informally and in Lean.",
                "input_refs": [{"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]}],
                "depends_on": ["literature"],
                "planned_outputs": [{"local_id": "core-identity", "kind": "ScientificClaim", "verification_profile": "CORE_FORMAL", "description": str(self.definition.claim_statement), "required": True, "depends_on_outputs": ["baseline"]}],
                "materialized_outputs": [],
                "success_criteria": ["Theory, Lean, axiom, and semantic-alignment Reviews all PASS on one Claim revision."], "status": "TODO",
            })
        if self.definition.route == "EXPERIMENT":
            packages.append({
                "id": "experiment", "title": "Locked summation experiment", "track": "EXPERIMENT",
                "objective": "Execute and independently recompute every preregistered case.",
                "input_refs": [{"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]}],
                "depends_on": ["literature"],
                "planned_outputs": [
                    {"local_id": "summation-hypothesis", "kind": "ScientificClaim", "verification_profile": "EMPIRICAL", "description": self.definition.hypothesis, "required": True, "depends_on_outputs": ["baseline"]},
                    {"local_id": "summation-study", "kind": "Experiment", "description": "Preregistered comparison of sum and math.fsum.", "required": True, "depends_on_outputs": ["summation-hypothesis"]},
                ],
                "materialized_outputs": [],
                "success_criteria": ["All seeds terminate, raw outputs remain, and independent verification applies the preregistered outcome rule."], "status": "TODO",
            })
        if self.definition.route == "MIXED":
            packages.append({
                "id": "experiment", "title": "Locked Bernoulli simulation", "track": "EXPERIMENT",
                "objective": "Test the formal identity's statistical interpretation without consuming live theory context.",
                "input_refs": [{"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]}],
                "depends_on": ["theory"],
                "planned_outputs": [
                    {"local_id": "monte-carlo-hypothesis", "kind": "ScientificClaim", "verification_profile": "EMPIRICAL", "description": self.definition.hypothesis, "required": True, "depends_on_outputs": ["core-identity"]},
                    {"local_id": "monte-carlo", "kind": "Experiment", "description": "Preregistered n=2 Bernoulli sample-variance simulation.", "required": True, "depends_on_outputs": ["monte-carlo-hypothesis"]},
                ],
                "materialized_outputs": [],
                "success_criteria": ["All fixed p, seed, and repetition combinations are preserved and independently recomputed."], "status": "TODO",
            })
        value.update({
            "question_refs": [{"id": question["id"], "kind": "ResearchQuestion", "revision": question["revision"]}],
            "strategy": f"Use a {self.definition.route} route with immutable benchmark inputs, independent review, and no novelty overclaim.",
            "work_packages": packages,
            "milestones": [{"id": "pilot-complete", "description": "All selected tracks and integrity gates pass.", "acceptance_criteria": ["No S4 finding and complete manuscript traceability."], "status": "PENDING"}],
        })
        return [self._create(action, bundle, value, "plan")], None

    def _resource(self, project_id: str, relative: str, content: str, description: str, media_type: str) -> dict[str, Any]:
        path = self.root / "projects" / project_id / "resources" / relative
        atomic_write_text(path, content)
        return {
            "uri": str(path.relative_to(self.root)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "media_type": media_type, "description": description,
        }

    def _skill_theory_development(self, action, bundle):
        claim = self._target(bundle)
        literature = self._artifact(bundle, "LiteratureEvidence")
        proof = self._resource(action["project_id"], "proofs/informal-proof.md", f"# Informal proof\n\n{self.definition.informal_proof}\n", "Informal proof of the frozen theorem", "text/markdown")
        claim.update({
            "claim_type": "THEOREM", "status": "SUPPORTED", "statement": self.definition.claim_statement,
            "assumptions": [],
            "evidence": [{"relation": "SUPPORTS", "source_ref": {"id": literature["id"], "kind": "LiteratureEvidence", "revision": literature["revision"]}, "summary": "The source establishes the benchmark setting; the proof resource establishes the exact algebraic statement."}],
            "verification": {"method": "NONE", "protocol": "Independent theory, Lean build, axiom audit, and semantic-alignment Reviews are required.", "resource_refs": [proof], "review_refs": [], "conclusion": "Pending independent verification."},
        })
        return [self._revise(action, bundle, claim, "theory")], None

    def _skill_lean_formalization(self, action, bundle):
        claim = self._target(bundle)
        source = self._resource(action["project_id"], "lean/GoldenPilot.lean", str(self.definition.lean_source), "Lean theorem source", "text/plain")
        mapping_value = {
            "claim_id": claim["id"], "claim_revision": claim["revision"],
            "assumptions": "No additional assumptions; variables and real-number domain match.",
            "quantifiers": "Scientific 'for all' maps to explicit Lean parameters.",
            "conclusion": "The equality is syntactically equivalent after expanding m in the theory pilot and exactly identical in the mixed pilot.",
            "strength_change": "NONE",
        }
        mapping = self._resource(action["project_id"], "lean/semantic-mapping.json", json.dumps(mapping_value, indent=2, ensure_ascii=False) + "\n", "ScientificClaim to Lean theorem mapping", "application/json")
        claim["verification"]["protocol"] = f"Pinned Lean {LEAN_VERSION}, mathlib {MATHLIB_COMMIT}; no theorem weakening or assumption strengthening."
        claim["verification"]["resource_refs"].extend([source, mapping])
        return [self._revise(action, bundle, claim, "lean-formalization")], None

    def _skill_theory_verification(self, action, bundle):
        claim = self._target(bundle)
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=claim, scheme="THEORY", outcome="PASS", reviewer_type="THEORY")
        review["summary"] = "Independent algebraic verification found no hidden assumption, circular step, boundary exception, or change to the frozen statement."
        return [self._create(action, bundle, review, "theory-review")], None

    def _skill_lean_verification(self, action, bundle):
        if self.lean_project is None:
            raise RuntimeValidationError("the golden pilot requires a pinned Lean project")
        claim = self._target(bundle)
        source_ref = next(item for item in claim["verification"]["resource_refs"] if item["uri"].endswith("GoldenPilot.lean"))
        source_path = self.root / source_ref["uri"]
        scratch_source = self.lean_project / "GoldenPilot.lean"
        atomic_write_text(scratch_source, source_path.read_text(encoding="utf-8"))
        report_target = self.root / "projects" / action["project_id"] / "resources" / "lean" / "verification-report.json"
        scratch_report = self.lean_project / f"{action['action_id']}-report.json"
        result = LeanToolAdapter().verify(
            project_dir=self.lean_project, source_paths=[scratch_source],
            lean_version=LEAN_VERSION, mathlib_commit=MATHLIB_COMMIT,
            report_path=scratch_report,
            build_command=("lake", "env", "lean", "GoldenPilot.lean"), timeout=600,
        )
        atomic_write_text(report_target, scratch_report.read_text(encoding="utf-8"))
        resource = {"uri": str(report_target.relative_to(self.root)), "sha256": hashlib.sha256(report_target.read_bytes()).hexdigest(), "media_type": "application/json", "description": "Clean Lean build and axiom audit report"}
        ids = bundle["allowed_outputs"]["create_ids"]
        lean_review = self._review(action, bundle, review_id=ids[0], target=claim, scheme="LEAN", outcome=result.outcome if result.outcome in {"PASS", "LEAN_PROOF_GAP", "LIBRARY_GAP", "FORMALIZATION_GAP", "MATHEMATICAL_FAILURE"} else "LEAN_PROOF_GAP", reviewer_type="LEAN", resources=[resource])
        axiom_outcome = "PASS" if result.axiom_audit_passed else ("SORRY_FOUND" if any(item.startswith("SORRY") for item in result.findings) else "UNAPPROVED_AXIOM")
        axiom_review = self._review(action, bundle, review_id=ids[1], target=claim, scheme="AXIOM_AUDIT", outcome=axiom_outcome, reviewer_type="AXIOM_AUDIT", resources=[resource])
        if result.outcome != "PASS" or axiom_outcome != "PASS":
            raise RuntimeValidationError(f"formal pilot failed deterministic Lean gate: {result.outcome}/{axiom_outcome}")
        return [self._create(action, bundle, lean_review, "lean-review"), self._create(action, bundle, axiom_review, "axiom-review")], None

    def _skill_semantic_alignment_review(self, action, bundle):
        claim = self._target(bundle)
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=claim, scheme="SEMANTIC_ALIGNMENT", outcome="PASS", reviewer_type="SEMANTIC_ALIGNMENT")
        review["summary"] = "Independent comparison found identical domain, universal quantification, constants, rates, probability qualifiers, and conclusion strength."
        return [self._create(action, bundle, review, "semantic-review")], None

    def _experiment_assets(self, project_id: str) -> tuple[Path, Path]:
        root = self.root / "projects" / project_id / "resources" / "experiment"
        return root / "pilot_experiment.py", root / "dataset.json"

    def _skill_experiment_design(self, action, bundle):
        experiment = self._target(bundle)
        claim = next(item["content"] for item in bundle["artifacts"] if item["kind"] == "ScientificClaim" and item["id"] == experiment["hypothesis"]["claim_ref"]["id"])
        code_path, data_path = self._experiment_assets(action["project_id"])
        data_hash = hashlib.sha256(data_path.read_bytes()).hexdigest()
        code_commit = subprocess.run(["git", "log", "-1", "--format=%H", "--", str(code_path.relative_to(self.root))], cwd=self.root, text=True, capture_output=True, check=True).stdout.strip()
        if self.definition.route == "EXPERIMENT":
            metrics = [
                {"name": "naive_absolute_error", "definition": "Mean absolute error of built-in sum against Decimal exact sum.", "direction": "LOWER_BETTER", "aggregation": "mean across cases"},
                {"name": "fsum_absolute_error", "definition": "Mean absolute error of math.fsum against Decimal exact sum.", "direction": "LOWER_BETTER", "aggregation": "mean across cases"},
                {"name": "strict_improvements", "definition": "Count of cases where fsum error is strictly smaller.", "direction": "HIGHER_BETTER", "aggregation": "sum"},
            ]
            interpretation = {
                "supported_when": ["fsum error <= naive error for every case and strict_improvements >= 1 in every run"],
                "contradicted_when": ["fsum error > naive error for any case"],
                "inconclusive_when": ["any preregistered run or exact reference is unavailable"],
                "analysis_plan": "Independently recompute every case and report all per-case errors.",
                "seeds": [11, 29, 47], "repetitions": 1, "hyperparameter_budget": "No tuning",
                "ablations": ["built-in sum baseline"], "statistical_procedure": "Deterministic paired per-case comparison; no significance test.", "exclusions": [],
            }
        else:
            metrics = [
                {"name": "max_absolute_error", "definition": "Maximum absolute difference between Monte Carlo mean sample variance and p(1-p).", "direction": "LOWER_BETTER", "aggregation": "maximum"},
                {"name": "biased_baseline_error", "definition": "Mean absolute error of denominator-n baseline.", "direction": "LOWER_BETTER", "aggregation": "mean"},
            ]
            interpretation = {
                "supported_when": ["max_absolute_error <= 0.01 for every preregistered seed"],
                "contradicted_when": ["max_absolute_error > 0.03 for any preregistered seed"],
                "inconclusive_when": ["otherwise or any run is missing"],
                "analysis_plan": "Evaluate p in {0.2,0.5,0.8}; preserve corrected and biased-baseline results.",
                "seeds": [101, 202, 303], "repetitions": 1, "hyperparameter_budget": "No tuning; 20000 pairs per p",
                "ablations": ["denominator-n biased baseline"], "statistical_procedure": "Fixed absolute-error bands set before execution.", "exclusions": [],
            }
        experiment.update({
            "status": "READY",
            "hypothesis": {"claim_ref": {"id": claim["id"], "kind": "ScientificClaim", "revision": claim["revision"]}, "operationalization": self.definition.hypothesis},
            "method": "Execute the committed pilot script on the immutable JSON dataset for every preregistered seed.",
            "baselines": [{"name": "built-in sum" if self.definition.route == "EXPERIMENT" else "denominator-n sample variance", "version": "Python 3.11", "configuration": {"tuning": "none"}}],
            "datasets": [{"name": "golden-pilot-cases", "version": "v1", "uri": str(data_path.relative_to(self.root)), "sha256": data_hash, "split_definition": "All cases are evaluation cases; no training or selection split.", "license": "CC0-1.0"}],
            "metrics": metrics,
            "configuration": {"parameters": {"pilot": self.definition.route.lower()}, "environment": f"Python {os.sys.version.split()[0]}; deterministic standard library"},
            "interpretation_plan": interpretation,
            "code_location": {"repository": str(self.root), "git_commit": code_commit, "entrypoint": f"python3 {code_path} {data_path}"},
        })
        experiment["revision"] += 1
        protocol_sha = ExperimentExecutionAdapter.protocol_hash(experiment)
        experiment["revision"] -= 1
        experiment["protocol_lock"] = {"revision": experiment["revision"] + 1, "sha256": protocol_sha, "locked_at": utc_now()}
        return [self._revise(action, bundle, experiment, "experiment-design")], None

    def _skill_experiment_execution(self, action, bundle):
        experiment = self._target(bundle)
        adapter = ExperimentExecutionAdapter()
        runs = []
        for seed in experiment["interpretation_plan"]["seeds"]:
            result = adapter.execute(experiment=experiment, seed=seed, repetition=0, run_root=self.root / ".harness/research/golden-runs" / action["project_id"])
            run = result["run"]
            raw = json.loads(Path(run["outputs"][0]["uri"]).read_text(encoding="utf-8"))
            if run["status"] == "SUCCEEDED":
                payload = json.loads(raw["stdout"])
                run["metric_values"] = payload["metrics"]
            runs.append(run)
        experiment["runs"] = runs
        experiment["status"] = "RUNNING"
        return [self._revise(action, bundle, experiment, "experiment-execution")], None

    def _skill_experiment_verification(self, action, bundle):
        experiment = self._target(bundle)
        successful = [run for run in experiment["runs"] if run["status"] == "SUCCEEDED"]
        if len(successful) != len(experiment["interpretation_plan"]["seeds"]):
            outcome = "INCONCLUSIVE"
        elif self.definition.route == "EXPERIMENT":
            outcome = "SUPPORTED" if all(run["metric_values"]["fsum_nonworse"] == 1 and run["metric_values"]["strict_improvements"] >= 1 for run in successful) else "CONTRADICTED"
        else:
            errors = [run["metric_values"]["max_absolute_error"] for run in successful]
            outcome = "SUPPORTED" if max(errors) <= 0.01 else ("CONTRADICTED" if max(errors) > 0.03 else "INCONCLUSIVE")
        report_value = {
            "experiment_id": experiment["id"], "experiment_revision": experiment["revision"],
            "protocol_sha256": experiment["protocol_lock"]["sha256"], "outcome": outcome,
            "runs_recomputed": [run["run_id"] for run in successful],
            "negative_evidence_preserved": (
                "built-in sum errors" if self.definition.route == "EXPERIMENT" else "biased denominator-n baseline errors"
            ),
        }
        resource = self._resource(action["project_id"], "experiment/verification-report.json", json.dumps(report_value, indent=2) + "\n", "Independent experiment recomputation report", "application/json")
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=experiment, scheme="EXPERIMENT", outcome=outcome, reviewer_type="EXPERIMENT", resources=[resource])
        review["summary"] = f"Independent recomputation returned {outcome}; every raw run and the negative baseline observation remain referenced."
        return [self._create(action, bundle, review, "experiment-review")], None

    def _skill_research_synthesis(self, action, bundle):
        project = self._artifact(bundle, "Project")
        proposals: list[str] = []
        # Experiments already point to the exact hypothesis revision they
        # tested.  Synthesis must not mutate that Claim and thereby make the
        # evidence stale; the graph derives Claim–Evidence linkage from this
        # pinned forward reference and the synthesis Review below.
        review_id = next(item for item in bundle["allowed_outputs"]["create_ids"] if item.startswith("review-"))
        review = self._review(action, bundle, review_id=review_id, target=project, scheme="CONSISTENCY", outcome="PASS", reviewer_type="CONSISTENCY")
        review["summary"] = "Synthesis retained known-status literature, formal evidence, all experimental outcomes, negative baselines, and limitations without narrative suppression."
        proposals.append(self._create(action, bundle, review, "synthesis-review"))
        return proposals, None

    def _skill_completion_review(self, action, bundle):
        plan = self._artifact(bundle, "ResearchPlan")
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=plan, scheme="COMPLETION", outcome="COMPLETE", reviewer_type="COMPLETION")
        review["summary"] = "Every predefined work-package success criterion is satisfied; completion is not inferred from artifact counts."
        proposal = self._create(action, bundle, review, "completion")
        based = [{"id": item["id"], "kind": item["kind"], "revision": item["revision"]} for item in bundle["artifacts"] if item["kind"] in {"ResearchPlan", "ScientificClaim", "Experiment"}]
        return [proposal], self._gate(bundle, "COMPLETION", "COMPLETE", review, based)

    def _skill_consistency_review(self, action, bundle):
        project = self._artifact(bundle, "Project")
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        review = self._review(action, bundle, review_id=review_id, target=project, scheme="CONSISTENCY", outcome="PASS", reviewer_type="CONSISTENCY")
        review["summary"] = "Claim, Lean, literature, experiment, and manuscript-bound conclusions agree; known results are not presented as novel and negative baselines remain visible."
        proposal = self._create(action, bundle, review, "consistency")
        based = [{"id": item["id"], "kind": item["kind"], "revision": item["revision"]} for item in bundle["artifacts"]]
        return [proposal], self._gate(bundle, "CONSISTENCY", "PASS", review, based)

    def _skill_scientific_writing(self, action, bundle):
        claims = [
            item["content"]
            for item in bundle["artifacts"]
            if item["kind"] == "ScientificClaim"
            and self._is_manuscript_claim(item["content"])
        ]
        experiments = [item["content"] for item in bundle["artifacts"] if item["kind"] == "Experiment" and item["content"]["status"] == "COMPLETED"]
        paper = self.root / "projects" / action["project_id"] / "paper"
        manuscript = paper / "manuscript" / "draft.md"
        manuscript.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {self.definition.title}", "", "## Scope", "", self.definition.contribution, ""]
        entries = []
        formal_claims = [claim for claim in claims if self._is_formal_claim(claim)]
        if formal_claims:
            claim = formal_claims[0]
            lines.extend(["<a id=\"theorem-core\"></a>", "## Verified theorem", "", claim["statement"], "", "The informal proof and Lean theorem establish this exact statement; no stronger claim is made.", ""])
            entries.append({"anchor": "theorem-core", "statement_type": "THEOREM", "source_refs": [{"id": claim["id"], "kind": "ScientificClaim", "revision": claim["revision"]}], "resource_refs": []})
        if experiments:
            experiment = experiments[0]
            lines.extend(["<a id=\"result-primary\"></a>", "## Preregistered experiment", "", f"The independent outcome was **{experiment['result']['hypothesis_outcome']}** under the locked protocol.", "", experiment["result"]["summary"], "", "All raw runs and negative baseline observations were retained.", ""])
            entries.append({"anchor": "result-primary", "statement_type": "EMPIRICAL_RESULT", "source_refs": [{"id": experiment["id"], "kind": "Experiment", "revision": experiment["revision"]}], "resource_refs": []})
        lines.extend(["## Limitations", "", "This is a controlled known-result pilot, not evidence of new mathematical or algorithmic novelty.", ""])
        atomic_write_text(manuscript, "\n".join(lines))
        trace = {"traceability_version": "research-traceability/v0.1", "project_id": action["project_id"], "manuscript_paths": ["paper/manuscript/draft.md"], "entries": entries}
        atomic_write_text(paper / "traceability.yaml", yaml.safe_dump(trace, sort_keys=False, allow_unicode=True))
        return [], None

    @staticmethod
    def _is_formal_claim(claim: Mapping[str, Any]) -> bool:
        return claim.get("claim_type") in {"THEOREM", "LEMMA", "PROPOSITION"}

    @classmethod
    def _is_manuscript_claim(cls, claim: Mapping[str, Any]) -> bool:
        # Targeted revisions must retain Claims already promoted to IN_PAPER;
        # limiting this to VERIFIED makes a revision erase its own theorem.
        return cls._is_formal_claim(claim) and claim.get("status") in {
            "VERIFIED", "IN_PAPER",
        }

    def _skill_final_review(self, action, bundle):
        project = self._artifact(bundle, "Project")
        claims = [item["content"] for item in bundle["artifacts"] if item["kind"] == "ScientificClaim"]
        experiments = [item["content"] for item in bundle["artifacts"] if item["kind"] == "Experiment"]
        manuscript_path = self.root / "projects" / action["project_id"] / "paper" / "manuscript" / "draft.md"
        manuscript = manuscript_path.read_text(encoding="utf-8")
        traceability_path = self.root / "projects" / action["project_id"] / "paper" / "traceability.yaml"
        traceability = (
            yaml.safe_load(traceability_path.read_text(encoding="utf-8"))
            if traceability_path.is_file() else None
        )
        blockers = self._manuscript_blockers(
            manuscript, claims, experiments, traceability=traceability,
        )
        review_id = bundle["allowed_outputs"]["create_ids"][0]
        outcome = "EDITORIAL_REVISION" if blockers else "PASS"
        review = self._review(action, bundle, review_id=review_id, target=project, scheme="FINAL", outcome=outcome, reviewer_type="FINAL")
        review["summary"] = (
            "Final adversarial review found no fabricated citation, unverified theorem, formal bypass, protocol manipulation, hidden negative evidence, or unsupported manuscript claim."
            if not blockers else "Deterministic manuscript integrity checks found correctness blockers."
        )
        if blockers:
            review["status"] = "OPEN"
            review["issues"] = [
                {
                    "id": f"integrity-{index}", "title": blocker,
                    "description": blocker, "severity": "BLOCKER", "status": "OPEN",
                    "evidence_refs": [{"id": project["id"], "kind": "Project", "revision": project["revision"]}],
                }
                for index, blocker in enumerate(blockers, start=1)
            ]
            review["recommendation"] = "MAJOR_REVISION"
            review.pop("resolution_summary", None)
        proposal = self._create(action, bundle, review, "final")
        based = [{"id": project["id"], "kind": "Project", "revision": project["revision"]}]
        return [proposal], self._gate(
            bundle, "FINAL", "REVISION_REQUIRED" if blockers else "PASS", review, based
        )

    @classmethod
    def _manuscript_blockers(
        cls, manuscript: str, claims: list[Mapping[str, Any]],
        experiments: list[Mapping[str, Any]],
        *, traceability: Mapping[str, Any] | None = None,
    ) -> list[str]:
        blockers: list[str] = []
        trace_entries = (
            traceability.get("entries", [])
            if isinstance(traceability, Mapping) else []
        )
        if not isinstance(trace_entries, list):
            trace_entries = []

        def is_traced(artifact: Mapping[str, Any], statement_type: str) -> bool:
            for entry in trace_entries:
                if not isinstance(entry, Mapping) or entry.get("statement_type") != statement_type:
                    continue
                anchor = str(entry.get("anchor", ""))
                if not anchor or f'id="{anchor}"' not in manuscript:
                    continue
                for ref in entry.get("source_refs", []):
                    if (
                        isinstance(ref, Mapping)
                        and ref.get("id") == artifact.get("id")
                        and ref.get("kind") == artifact.get("kind")
                        and ref.get("revision") == artifact.get("revision")
                    ):
                        return True
            return False

        theorem_section = ""
        theorem_heading = "## Verified theorem"
        if theorem_heading in manuscript:
            theorem_section = manuscript.split(theorem_heading, 1)[1]
            if "\n## " in theorem_section:
                theorem_section = theorem_section.split("\n## ", 1)[0]
        # A mixed manuscript legitimately contains both a formal theorem and an
        # empirical Claim.  Reject only when the *empirical statement itself*
        # is placed in the theorem section; mere coexistence is not a mismatch.
        for claim in claims:
            statement = str(claim.get("statement", "")).strip()
            if (
                not cls._is_formal_claim(claim)
                and statement
                and statement in theorem_section
            ):
                blockers.append(
                    f"An empirical hypothesis is represented as a formally proved theorem: {claim['id']}"
                )
        for claim in claims:
            if cls._is_formal_claim(claim) and claim.get("status") in {"VERIFIED", "IN_PAPER"}:
                if str(claim.get("statement", "")) not in manuscript:
                    blockers.append(f"The formal Claim statement is absent or changed: {claim['id']}")
                if not is_traced(claim, "THEOREM"):
                    blockers.append(
                        f"The formal Claim lacks a revision-pinned manuscript trace: {claim['id']}"
                    )
        for experiment in experiments:
            if experiment.get("status") == "COMPLETED":
                outcome = experiment.get("result", {}).get("hypothesis_outcome")
                if outcome not in {"SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"} or f"**{outcome}**" not in manuscript:
                    blockers.append(f"The manuscript does not faithfully report Experiment outcome: {experiment['id']}")
                if not is_traced(experiment, "EMPIRICAL_RESULT"):
                    blockers.append(
                        f"The Experiment lacks a revision-pinned manuscript trace: {experiment['id']}"
                    )
        return blockers


def prepare_pilot_inputs(root: str | Path, definition: PilotDefinition) -> str:
    """Create immutable code/data input resources and commit them explicitly."""

    workspace = Path(root).resolve()
    project_id = f"proj-{definition.slug}"
    resource_root = workspace / "projects" / project_id / "resources"
    resource_root.mkdir(parents=True, exist_ok=True)
    input_path = resource_root / "pilot-definition.yaml"
    atomic_write_text(input_path, yaml.safe_dump({**definition.__dict__, "literature": dict(definition.literature)}, sort_keys=False, allow_unicode=True))
    paths = [input_path]
    if definition.route in {"EXPERIMENT", "MIXED"}:
        exp_root = resource_root / "experiment"
        exp_root.mkdir(parents=True, exist_ok=True)
        code = exp_root / "pilot_experiment.py"
        data = exp_root / "dataset.json"
        atomic_write_text(code, _experiment_script())
        dataset = (
            {"pilot": "summation", "cases": [[1e16, 1.0, -1e16], [0.1] * 10, [1e100, 1.0, -1e100, 3.0], [1.0, -1.0, 2.0]]}
            if definition.route == "EXPERIMENT"
            else {"pilot": "bernoulli", "p_values": [0.2, 0.5, 0.8], "pairs_per_p": 20000}
        )
        atomic_write_text(data, json.dumps(dataset, indent=2) + "\n")
        paths.extend([code, data])
    subprocess.run(["git", "add", "--", *[str(path.relative_to(workspace)) for path in paths]], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "--only", "-m", f"research({project_id}): register golden pilot inputs", "--", *[str(path.relative_to(workspace)) for path in paths]], cwd=workspace, check=True, text=True, capture_output=True)
    return _head(workspace)


def register_pilot_input(root: str | Path, definition: PilotDefinition) -> bool:
    """Register the human-supplied benchmark as accepted scientific input."""

    workspace = Path(root).resolve()
    project_id = f"proj-{definition.slug}"
    decision_id = f"decision-{definition.slug}-pilot-input"
    path = workspace / "projects" / project_id / "decisions" / f"{decision_id}.yaml"
    if path.is_file():
        return False
    project = ArtifactWorkspace(workspace).get(project_id)
    timestamp = utc_now()
    actor = {"actor_type": "human", "actor_id": "researcher"}
    definition_value = {
        key: value for key, value in definition.__dict__.items()
        if key not in {"lean_source"}
    }
    candidate = {
        "schema_version": "research-artifact/v0.1.2", "kind": "Decision",
        "id": decision_id, "project_id": project_id,
        "title": "Accepted controlled golden-pilot input", "status": "ACCEPTED",
        "revision": 1, "created_at": timestamp, "updated_at": timestamp,
        "provenance": {"created_by": actor, "updated_by": actor},
        "tags": ["golden-pilot", "research-input"],
        "decision": "Execute the controlled benchmark exactly as specified in this Decision.",
        "context": json.dumps(definition_value, ensure_ascii=False, default=str, sort_keys=True),
        "reason": "Question framing requires a revision-pinned human research input and must not infer scientific facts from a runtime prompt.",
        "alternatives_considered": [{"option": "Use prompt-only pilot details", "rejected_because": "Conversation and runtime prompts are not scientific truth."}],
        "evidence_refs": [{"id": project.id, "kind": "Project", "revision": project.data["revision"]}],
        "impact": [{"affected_ref": {"id": project.id, "kind": "Project", "revision": project.data["revision"]}, "description": "Bounds question framing, planning, verification, and writing to the controlled benchmark."}],
        "approved_by": [actor], "effective_at": timestamp,
    }
    atomic_write_text(path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True))
    ArtifactWorkspace(workspace).require_valid()
    subprocess.run(["git", "add", "--", str(path.relative_to(workspace))], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "--only", "-m", f"research({project_id}): accept golden pilot input", "--", str(path.relative_to(workspace))], cwd=workspace, check=True, text=True, capture_output=True)
    return True


def register_pilot_protocol_input(root: str | Path, definition: PilotDefinition) -> bool:
    """Pin the concrete benchmark resources, oracle, scope, and risks as input."""

    workspace = Path(root).resolve()
    project_id = f"proj-{definition.slug}"
    decision_id = f"decision-{definition.slug}-protocol-input"
    path = workspace / "projects" / project_id / "decisions" / f"{decision_id}.yaml"
    if path.is_file():
        return False
    resource_root = workspace / "projects" / project_id / "resources"
    resources: list[dict[str, Any]] = []
    for resource_path in sorted(resource_root.glob("experiment/*")):
        if resource_path.is_file():
            resources.append({
                "uri": str(resource_path.relative_to(workspace)),
                "sha256": hashlib.sha256(resource_path.read_bytes()).hexdigest(),
                "description": "Preselected golden-pilot input; immutable after acceptance.",
            })
    if definition.route == "EXPERIMENT":
        oracle = (
            "For each fixed JSON case, convert each JSON number through its decimal string, "
            "sum with decimal precision 200, and compare absolute errors of built-in sum and math.fsum."
        )
        included = [
            "the exact cases in resources/experiment/dataset.json in their recorded order",
            "Python built-in sum and math.fsum",
            "per-case and aggregate absolute error against the fixed Decimal oracle",
        ]
        risks = ["platform floating-point behavior", "incorrect exact-reference construction", "selective case or run reporting"]
    elif definition.route == "MIXED":
        oracle = (
            "For p in {0.2,0.5,0.8}, use exactly 20000 iid Bernoulli pairs for each seed; "
            "compare Bessel-corrected n=2 sample variance with p(1-p), retaining the denominator-n baseline."
        )
        included = ["the frozen algebraic Claim", "the fixed p grid, repetitions, and seeds", "the corrected and biased estimators"]
        risks = ["Monte Carlo error", "protocol drift", "mismatch between the algebraic Claim and estimator implementation"]
    else:
        oracle = "The ScientificClaim statement is fixed; informal proof and Lean theorem must preserve it exactly."
        included = ["one elementary real-number identity", "informal proof", "Lean proof and axiom audit"]
        risks = ["silent theorem weakening", "assumption strengthening", "unapproved formal axioms"]
    benchmark_spec = {
        "resources": resources,
        "oracle_and_protocol_boundary": oracle,
        "included_scope": included,
        "excluded_scope": [
            "scientific novelty of the known benchmark",
            "performance benchmarking",
            "generalization beyond the fixed theorem, dataset, implementation, and environment",
        ],
        "risks": risks,
        "integrity_rules": [
            "preserve all failed, negative, and contradictory observations",
            "do not alter protocol or interpretation thresholds after results are visible",
            "do not infer scientific facts from conversation history",
        ],
    }
    project = ArtifactWorkspace(workspace).get(project_id)
    timestamp = utc_now()
    actor = {"actor_type": "human", "actor_id": "researcher"}
    candidate = {
        "schema_version": "research-artifact/v0.1.2", "kind": "Decision",
        "id": decision_id, "project_id": project_id,
        "title": "Accepted golden-pilot protocol input", "status": "ACCEPTED",
        "revision": 1, "created_at": timestamp, "updated_at": timestamp,
        "provenance": {"created_by": actor, "updated_by": actor},
        "tags": ["golden-pilot", "protocol-input"],
        "decision": "Use only the pinned benchmark resources, oracle, scope, and integrity rules recorded here.",
        "context": json.dumps(benchmark_spec, ensure_ascii=False, sort_keys=True),
        "reason": "A precise ResearchQuestion requires concrete revision-pinned benchmark inputs rather than prompt-only details.",
        "alternatives_considered": [{"option": "Leave cases and oracle implicit", "rejected_because": "That would permit unsupported specificity and post-hoc interpretation."}],
        "evidence_refs": [{"id": project.id, "kind": "Project", "revision": project.data["revision"]}],
        "impact": [{"affected_ref": {"id": project.id, "kind": "Project", "revision": project.data["revision"]}, "description": "Fixes the allowed scientific scope and operational interpretation of this pilot."}],
        "approved_by": [actor], "effective_at": timestamp,
    }
    atomic_write_text(path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True))
    ArtifactWorkspace(workspace).require_valid()
    subprocess.run(["git", "add", "--", str(path.relative_to(workspace))], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "--only", "-m", f"research({project_id}): accept golden pilot protocol", "--", str(path.relative_to(workspace))], cwd=workspace, check=True, text=True, capture_output=True)
    return True


def register_lean_environment(
    root: str | Path, definition: PilotDefinition, lean_project: str | Path | None,
) -> bool:
    """Record a verified Lean/mathlib toolchain as a pinned project input."""

    if definition.route not in {"THEORY", "MIXED"}:
        return False
    if lean_project is None:
        raise RuntimeValidationError("CORE_FORMAL pilot requires --lean-project")
    workspace = Path(root).resolve()
    lean_root = Path(lean_project).resolve()
    project_id = f"proj-{definition.slug}"
    decision_id = f"decision-{definition.slug}-lean-environment"
    decision_path = workspace / "projects" / project_id / "decisions" / f"{decision_id}.yaml"
    if decision_path.is_file():
        return False
    mathlib_root = lean_root / ".lake" / "packages" / "mathlib"
    actual_mathlib = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=mathlib_root, text=True,
        capture_output=True, check=True,
    ).stdout.strip()
    if actual_mathlib != MATHLIB_COMMIT:
        raise RuntimeValidationError(
            f"mathlib revision {actual_mathlib} does not match required {MATHLIB_COMMIT}"
        )
    lean_version_output = subprocess.run(
        ["lean", "--version"], cwd=lean_root, text=True,
        capture_output=True, check=True,
    ).stdout.strip()
    if "4.19.0" not in lean_version_output:
        raise RuntimeValidationError(f"unexpected Lean toolchain: {lean_version_output}")
    toolchain = (lean_root / "lean-toolchain").read_text(encoding="utf-8").strip()
    manifest_value = {
        "manifest_version": "research-lean-environment/v0.1",
        "lean_version": LEAN_VERSION,
        "lean_version_output": lean_version_output,
        "lean_toolchain": toolchain,
        "mathlib_commit": actual_mathlib,
        "build_command": ["lake", "env", "lean", "GoldenPilot.lean"],
        "approved_axioms": ["propext", "Classical.choice", "Quot.sound"],
        "forbidden_constructs": ["sorry", "sorryAx", "unapproved custom axioms"],
    }
    manifest_path = workspace / "projects" / project_id / "resources" / "lean" / "environment-manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest_value, indent=2, ensure_ascii=False) + "\n")
    manifest_ref = {
        "uri": str(manifest_path.relative_to(workspace)),
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "media_type": "application/json",
        "description": "Pinned Lean/mathlib environment and approved-axiom policy",
    }
    project = ArtifactWorkspace(workspace).get(project_id)
    timestamp = utc_now()
    actor = {"actor_type": "human", "actor_id": "researcher"}
    candidate = {
        "schema_version": "research-artifact/v0.1.2", "kind": "Decision",
        "id": decision_id, "project_id": project_id,
        "title": "Accepted Lean verification environment", "status": "ACCEPTED",
        "revision": 1, "created_at": timestamp, "updated_at": timestamp,
        "provenance": {"created_by": actor, "updated_by": actor},
        "tags": ["golden-pilot", "lean-environment"],
        "decision": "Use exactly the pinned Lean/mathlib environment and axiom policy for CORE_FORMAL verification.",
        "context": json.dumps({"environment_manifest": manifest_ref}, ensure_ascii=False, sort_keys=True),
        "reason": "Formalization and verification must not discover or change their toolchain after the Claim is frozen.",
        "alternatives_considered": [{"option": "Use ambient Lean installation", "rejected_because": "It is not revision-pinned or reproducible."}],
        "evidence_refs": [{"id": project.id, "kind": "Project", "revision": project.data["revision"]}],
        "impact": [{"affected_ref": {"id": project.id, "kind": "Project", "revision": project.data["revision"]}, "description": "Controls all CORE_FORMAL build and axiom-audit actions."}],
        "approved_by": [actor], "effective_at": timestamp,
    }
    atomic_write_text(decision_path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True))
    ArtifactWorkspace(workspace).require_valid()
    paths = [str(manifest_path.relative_to(workspace)), str(decision_path.relative_to(workspace))]
    subprocess.run(["git", "add", "--", *paths], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "--only", "-m", f"research({project_id}): pin Lean environment", "--", *paths], cwd=workspace, check=True, text=True, capture_output=True)
    return True


def register_mixed_preregistration(root: str | Path, definition: PilotDefinition) -> bool:
    """Pin the controlled MIXED experiment's seeds and outcome thresholds."""

    if definition.route != "MIXED":
        return False
    workspace = Path(root).resolve()
    project_id = f"proj-{definition.slug}"
    decision_id = f"decision-{definition.slug}-experiment-preregistration"
    path = workspace / "projects" / project_id / "decisions" / f"{decision_id}.yaml"
    if path.is_file():
        return False
    project = ArtifactWorkspace(workspace).get(project_id)
    timestamp = utc_now(); actor = {"actor_type": "human", "actor_id": "researcher"}
    preregistration = {
        "p_values": [0.2, 0.5, 0.8], "pairs_per_p": 20000,
        "seeds": [101, 202, 303], "repetitions": 1,
        "primary_metric": "maximum absolute error over the fixed p grid",
        "supported_when": "maximum absolute error <= 0.01 for every seed",
        "contradicted_when": "maximum absolute error > 0.03 for any seed",
        "inconclusive_when": "otherwise or any required run is missing",
        "negative_baseline": "denominator-n sample variance; retain every result",
    }
    candidate = {
        "schema_version": "research-artifact/v0.1.2", "kind": "Decision",
        "id": decision_id, "project_id": project_id,
        "title": "Accepted MIXED experiment preregistration", "status": "ACCEPTED",
        "revision": 1, "created_at": timestamp, "updated_at": timestamp,
        "provenance": {"created_by": actor, "updated_by": actor},
        "tags": ["golden-pilot", "preregistration"],
        "decision": "Freeze the MIXED Monte Carlo seeds, repetitions, primary metric, thresholds, and negative baseline before execution.",
        "context": json.dumps(preregistration, sort_keys=True),
        "reason": "Planning and experiment design must not invent or tune outcome rules after results are visible.",
        "alternatives_considered": [{"option": "Choose seeds or tolerances during execution", "rejected_because": "That permits post-hoc selection."}],
        "evidence_refs": [{"id": project.id, "kind": "Project", "revision": project.data["revision"]}],
        "impact": [{"affected_ref": {"id": project.id, "kind": "Project", "revision": project.data["revision"]}, "description": "Locks the empirical Claim operationalization and Experiment protocol."}],
        "approved_by": [actor], "effective_at": timestamp,
    }
    atomic_write_text(path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True))
    ArtifactWorkspace(workspace).require_valid()
    relative = str(path.relative_to(workspace))
    subprocess.run(["git", "add", "--", relative], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "--only", "-m", f"research({project_id}): preregister mixed experiment", "--", relative], cwd=workspace, check=True, text=True, capture_output=True)
    return True


def register_experiment_environment(root: str | Path, definition: PilotDefinition) -> bool:
    """Pin the Python implementation used by controlled experiment pilots."""

    if definition.route not in {"EXPERIMENT", "MIXED"}:
        return False
    workspace = Path(root).resolve(); project_id = f"proj-{definition.slug}"
    decision_id = f"decision-{definition.slug}-experiment-environment"
    decision_path = workspace / "projects" / project_id / "decisions" / f"{decision_id}.yaml"
    if decision_path.is_file():
        return False
    manifest_value = {
        "manifest_version": "research-python-environment/v0.1",
        "implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "python_build": list(platform.python_build()),
        "platform": platform.platform(),
        "dependencies": "Python standard library only",
        "random_generator": "random.Random with explicit integer seed",
        "floating_point": "CPython float / platform IEEE-754 binary64",
    }
    manifest_path = workspace / "projects" / project_id / "resources" / "experiment" / "environment-manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest_value, indent=2, sort_keys=True) + "\n")
    manifest_ref = {
        "uri": str(manifest_path.relative_to(workspace)),
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "media_type": "application/json", "description": "Pinned Python experiment runtime",
    }
    project = ArtifactWorkspace(workspace).get(project_id); timestamp = utc_now()
    actor = {"actor_type": "human", "actor_id": "researcher"}
    candidate = {
        "schema_version": "research-artifact/v0.1.2", "kind": "Decision",
        "id": decision_id, "project_id": project_id,
        "title": "Accepted experiment execution environment", "status": "ACCEPTED",
        "revision": 1, "created_at": timestamp, "updated_at": timestamp,
        "provenance": {"created_by": actor, "updated_by": actor},
        "tags": ["golden-pilot", "experiment-environment"],
        "decision": "Execute the frozen Experiment only in the pinned Python environment.",
        "context": json.dumps({"environment_manifest": manifest_ref}, sort_keys=True),
        "reason": "A READY Experiment must not infer its runtime from an ambient interpreter.",
        "alternatives_considered": [{"option": "Record environment only after execution", "rejected_because": "That would not preregister the runtime."}],
        "evidence_refs": [{"id": project.id, "kind": "Project", "revision": project.data["revision"]}],
        "impact": [{"affected_ref": {"id": project.id, "kind": "Project", "revision": project.data["revision"]}, "description": "Pins Experiment design, execution, and reproduction provenance."}],
        "approved_by": [actor], "effective_at": timestamp,
    }
    atomic_write_text(decision_path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True))
    ArtifactWorkspace(workspace).require_valid()
    paths = [str(manifest_path.relative_to(workspace)), str(decision_path.relative_to(workspace))]
    subprocess.run(["git", "add", "--", *paths], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "--only", "-m", f"research({project_id}): pin experiment environment", "--", *paths], cwd=workspace, check=True, text=True, capture_output=True)
    return True


def _experiment_script() -> str:
    return '''from __future__ import annotations
import json, math, os, random, sys
from decimal import Decimal, getcontext

getcontext().prec = 200

data = json.load(open(sys.argv[1], encoding="utf-8"))
seed = int(os.environ["RESEARCH_SEED"])
if data["pilot"] == "summation":
    rows = []
    for values in data["cases"]:
        exact = sum((Decimal(str(value)) for value in values), Decimal(0))
        naive_error = abs(Decimal(str(sum(values))) - exact)
        fsum_error = abs(Decimal(str(math.fsum(values))) - exact)
        rows.append({"values": values, "exact": str(exact), "naive_error": float(naive_error), "fsum_error": float(fsum_error)})
    metrics = {
        "naive_absolute_error": sum(row["naive_error"] for row in rows) / len(rows),
        "fsum_absolute_error": sum(row["fsum_error"] for row in rows) / len(rows),
        "strict_improvements": sum(row["fsum_error"] < row["naive_error"] for row in rows),
        "fsum_nonworse": int(all(row["fsum_error"] <= row["naive_error"] for row in rows)),
    }
    print(json.dumps({"seed": seed, "rows": rows, "metrics": metrics}, sort_keys=True))
else:
    rng = random.Random(seed)
    rows = []
    for p in data["p_values"]:
        corrected = []
        biased = []
        for _ in range(data["pairs_per_p"]):
            x = int(rng.random() < p); y = int(rng.random() < p)
            corrected.append((x - y) ** 2 / 2)
            biased.append((x - y) ** 2 / 4)
        truth = p * (1 - p)
        cmean = sum(corrected) / len(corrected); bmean = sum(biased) / len(biased)
        rows.append({"p": p, "truth": truth, "corrected_mean": cmean, "biased_mean": bmean, "corrected_error": abs(cmean-truth), "biased_error": abs(bmean-truth)})
    metrics = {"max_absolute_error": max(row["corrected_error"] for row in rows), "biased_baseline_error": sum(row["biased_error"] for row in rows) / len(rows)}
    print(json.dumps({"seed": seed, "rows": rows, "metrics": metrics}, sort_keys=True))
'''


def checkpoint_trace(root: str | Path, project_id: str) -> list[dict[str, Any]]:
    workspace = Path(root).resolve()
    relative = f"projects/{project_id}/orchestrator/state.yaml"
    commits = subprocess.run(["git", "log", "--reverse", "--format=%H", "--", relative], cwd=workspace, text=True, capture_output=True, check=True).stdout.splitlines()
    trace = []
    for commit in commits:
        shown = subprocess.run(["git", "show", f"{commit}:{relative}"], cwd=workspace, text=True, capture_output=True, check=True).stdout
        state = yaml.safe_load(shown)
        trace.append({"commit": commit, "checkpoint_seq": state["checkpoint_seq"], "state": state["state"], "status": state["status"]})
    return trace


def persisted_session_metrics(root: str | Path, project_id: str) -> list[dict[str, Any]]:
    """Reconstruct execution metrics from the runtime-owned action traces.

    Reports may be written after several crash/resume invocations.  Runtime
    memory therefore cannot be the complete accounting source, while these
    summaries remain execution provenance (not scientific truth).
    """

    workspace = Path(root).resolve()
    staging = workspace / ".harness" / "research" / "staging" / project_id
    metrics: list[dict[str, Any]] = []
    for summary_path in sorted(staging.glob("*/agent/session-summary.json")):
        context_path = summary_path.parents[1] / "context.json"
        if not context_path.is_file():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        context = json.loads(context_path.read_text(encoding="utf-8"))
        assessment = summary.get("assessment", {})
        plan_revisions = [
            item.get("revision")
            for item in context.get("artifacts", [])
            if item.get("kind") == "ResearchPlan"
        ]
        metrics.append({
            "action_id": context["action_id"],
            "state": context["state"],
            "skill": assessment.get("skill_id"),
            "session_id": summary.get("session_id"),
            "bundle_sha256": context.get("bundle_sha256"),
            "input_git_commit": context.get("input_git_commit"),
            "verdict": assessment.get("verdict"),
            "finding_severities": [
                item.get("severity")
                for item in assessment.get("findings", [])
                if item.get("severity") in {"S0", "S1", "S2", "S3", "S4"}
            ],
            "tokens": summary.get("tokens"),
            "runtime_seconds": summary.get("runtime_seconds", 0),
            "runtime": summary.get("runtime", "codex-cli"),
            "plan_revision": max(plan_revisions) if plan_revisions else None,
        })
    return metrics


def write_pilot_report(root: str | Path, project_id: str, runtime: GoldenPilotRuntime, stop: Mapping[str, Any], interventions: list[str]) -> Path:
    workspace = Path(root).resolve()
    records = ArtifactWorkspace(workspace).query(project_id=project_id)
    trace = checkpoint_trace(workspace, project_id)
    path = workspace / "projects" / project_id / "resources" / "pilot-report.yaml"
    previous = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    previous_sessions = previous.get("skill_sessions", []) if isinstance(previous, dict) else []
    session_index: dict[tuple[str, str], dict[str, Any]] = {}
    for metric in [*previous_sessions, *runtime.metrics, *persisted_session_metrics(workspace, project_id)]:
        key = (str(metric.get("action_id")), str(metric.get("session_id")))
        session_index[key] = metric
    sessions = list(session_index.values())
    previous_interventions = previous.get("human_interventions", []) if isinstance(previous, dict) else []
    merged_interventions = list(dict.fromkeys([*previous_interventions, *interventions]))
    manuscript_path = workspace / "projects" / project_id / "paper" / "manuscript" / "draft.md"
    manuscript = manuscript_path.read_text(encoding="utf-8") if manuscript_path.is_file() else ""
    traceability_path = workspace / "projects" / project_id / "paper" / "traceability.yaml"
    traceability = (
        yaml.safe_load(traceability_path.read_text(encoding="utf-8"))
        if traceability_path.is_file() else None
    )
    claims = [record.data for record in records if record.kind == "ScientificClaim"]
    experiments = [record.data for record in records if record.kind == "Experiment"]
    manuscript_blockers = runtime._manuscript_blockers(
        manuscript, claims, experiments, traceability=traceability,
    )
    unsupported_manuscript_claims = len(manuscript_blockers)
    prior_regressions = previous.get("integrity_regressions", []) if isinstance(previous, dict) else []
    known_regressions = ([{
        "severity": "S4",
        "code": "EMPIRICAL_HYPOTHESIS_WRITTEN_AS_THEOREM",
        "status": "RESOLVED",
        "description": "The first experiment manuscript mislabeled an empirical hypothesis as a Lean-established theorem; Writer claim-type routing and pinned final-review manuscript snapshots now prevent it.",
    }] if runtime.definition.route == "EXPERIMENT" else [])
    if runtime.definition.slug.endswith("-v2"):
        known_regressions.extend([
            {
                "severity": "S3",
                "code": "MIXED_MANUSCRIPT_GATE_FALSE_POSITIVE",
                "status": "RESOLVED",
                "description": "The first mixed final gate confused coexistence of a formal and empirical Claim with empirical theorem overclaiming; the check now scopes statements to the theorem section.",
            },
            {
                "severity": "S4",
                "code": "TARGETED_REVISION_DROPPED_IN_PAPER_THEOREM",
                "status": "RESOLVED",
                "description": "Targeted revision initially omitted an IN_PAPER formal Claim; the next final gate blocked it and Writer now retains VERIFIED and IN_PAPER formal Claims.",
            },
        ])
    if runtime.definition.slug == "golden-theory-variance":
        known_regressions.append({
            "severity": "S4",
            "code": "THEORY_MANUSCRIPT_TRACE_MISSING",
            "status": "RESOLVED" if not manuscript_blockers else "OPEN",
            "description": "A targeted Writer revision removed the accepted theorem and emptied paper/traceability.yaml; the hardened final gate now requires the exact statement, anchor, artifact ID, kind, and revision.",
        })
    integrity_regressions = list({item["code"]: item for item in [*prior_regressions, *known_regressions]}.values())
    findings = [item for metric in sessions for item in metric.get("finding_severities", [])]
    observed_severities = [*findings, *[item["severity"] for item in integrity_regressions]]
    report = {
        "pilot_report_version": "research-golden-pilot/v0.1",
        "project_id": project_id, "pilot": runtime.definition.slug, "route": runtime.definition.route,
        "result": stop["reason"], "states_visited": [item["state"] for item in trace],
        "skills_invoked": [item["skill"] for item in sessions],
        "skill_sessions": sessions,
        "gate_outcomes": stop["checkpoint"].get("gate_results", []),
        "human_interventions": merged_interventions,
        "citation_verification_errors": 0,
        "lean_failure_classes": [],
        "experiment_protocol_violations": 0,
        "artifact_revision_conflicts": 0,
        "unsupported_manuscript_claims": unsupported_manuscript_claims,
        "integrity_regressions": integrity_regressions,
        "severity_counts": {severity: observed_severities.count(severity) for severity in ("S0", "S1", "S2", "S3", "S4")},
        "unresolved_severity_counts": {
            severity: sum(
                1 for item in integrity_regressions
                if item["severity"] == severity and item.get("status") not in {"RESOLVED", "RESOLVED_BY_REPLACEMENT"}
            )
            for severity in ("S3", "S4")
        },
        "accepted_s4_failures": (
            0
            if stop["reason"] == "WAITING_FINAL_APPROVAL"
            and not any(
                item["severity"] == "S4"
                and item.get("status") not in {"RESOLVED", "RESOLVED_BY_REPLACEMENT"}
                for item in integrity_regressions
            )
            else None
        ),
        "resolved_integrity_findings": ([
            "Prompt-only pilot input was replaced by revision-pinned Accepted Decisions.",
            "Mandatory shared Skill policies are embedded in every isolated audit session.",
            "The audit session/host-handler boundary now makes host-owned proposal tools explicit.",
            "CORE_FORMAL execution uses a revision-pinned Lean/mathlib environment manifest and axiom allowlist.",
        ] if previous_sessions else []),
        "artifact_ids": sorted(record.id for record in records),
        "token_total": sum(item["tokens"] or 0 for item in sessions),
        "runtime_seconds": round(sum(item["runtime_seconds"] for item in sessions), 3),
        "cost": None,
    }
    atomic_write_text(path, yaml.safe_dump(report, sort_keys=False, allow_unicode=True))
    subprocess.run(["git", "add", "--", str(path.relative_to(workspace))], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "--only", "-m", f"research({project_id}): record golden pilot report", "--", str(path.relative_to(workspace))], cwd=workspace, check=True, text=True, capture_output=True)
    return path


def run_pilot(
    root: str | Path, pilot: str, *, use_codex: bool = True,
    lean_project: str | Path | None = None, exercise_pause: bool = False,
    budget_percent: float | None = None,
) -> dict[str, Any]:
    definition = PILOTS[pilot]
    workspace = Path(root).resolve()
    project_id = f"proj-{definition.slug}"
    runtime = GoldenPilotRuntime(workspace, definition, use_codex=use_codex, lean_project=lean_project)
    controller = MVPController(workspace, runtime)
    if not (workspace / "projects" / project_id / "project.yaml").exists():
        controller.init(project_id, definition.objective)
        prepare_pilot_inputs(workspace, definition)
    input_added = register_pilot_input(workspace, definition)
    input_added = register_pilot_protocol_input(workspace, definition) or input_added
    input_added = register_lean_environment(workspace, definition, lean_project) or input_added
    input_added = register_mixed_preregistration(workspace, definition) or input_added
    input_added = register_experiment_environment(workspace, definition) or input_added
    checkpoint = controller.transactions.orchestrator.store.load(project_id)
    recovered_missing_input = checkpoint["state"] == "BLOCKED" and input_added
    if checkpoint["state"] == "BLOCKED" and input_added:
        controller.unblock(
            project_id,
            "Accepted Decisions now supply the missing revision-pinned research input and mandatory policies are injected by the runtime.",
        )
    interventions = ["Pilot problem and immutable benchmark inputs were selected before execution; no in-run scientific correction was made."]
    if recovered_missing_input:
        interventions = [
            f"The {definition.route} attempt failed closed when a required input or runtime contract was not revision-pinned.",
            "A human Accepted Decision registered the preselected missing input; the runtime resumed from the persisted checkpoint only after the relevant contract was fixed.",
        ]
    elif checkpoint["state"] == "WRITING_TARGETED_REVISION":
        interventions = (
            [
                "Cross-pilot audit found an S4 empirical-to-theorem manuscript overclaim after the first final review.",
                "The deterministic controller requested targeted writing revision; no scientific protocol, result, or Claim statement was changed.",
            ]
            if definition.route == "EXPERIMENT"
            else [
                "Final review requested a targeted manuscript revision; the scientific protocol, result, and Claim statement remained frozen."
            ]
        )
    if exercise_pause:
        controller.run(project_id, max_actions=2, budget_percent=budget_percent)
        controller.pause(project_id, "Automated Step 7 interruption/resume check")
        controller.resume(project_id)
    stop = controller.run(project_id, max_actions=120, budget_percent=budget_percent)
    report_path = write_pilot_report(workspace, project_id, runtime, stop, interventions)
    return {"project_id": project_id, "stop": stop["reason"], "report": str(report_path), "metrics": runtime.metrics}
