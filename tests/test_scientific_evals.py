from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from research_evals import ScientificEvalRunner


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = {
    "question-framing", "literature-novelty", "research-planning",
    "theory-development", "theory-verification", "lean-formalization",
    "lean-verification", "semantic-alignment-review", "experiment-design",
    "experiment-execution", "experiment-verification", "research-synthesis",
    "completion-review", "consistency-review", "targeted-followup",
    "scientific-writing", "final-review",
}
REQUIRED_CASES = {
    "literature-fabricated-citation", "literature-existing-result-open",
    "literature-equivalent-prior-open", "literature-uncertainty-overstated",
    "theory-correct-proof", "theory-incomplete-proof", "theory-hidden-assumption",
    "theory-circular-proof", "theory-false-counterexample", "theory-silent-weakening",
    "lean-sorry", "lean-sorryax", "lean-custom-axiom", "lean-stronger-assumptions",
    "lean-weaker-conclusion", "lean-library-gap",
    "experiment-protocol-change-after-ready", "experiment-metric-switching",
    "experiment-cherry-picked-seed", "experiment-omitted-failed-run",
    "experiment-unfair-baseline", "experiment-data-leakage",
    "experiment-contradiction-not-failure", "experiment-unsupported-improvement",
    "synthesis-hidden-contradiction", "writing-unsupported-novelty",
    "writing-supported-as-proved", "writing-inconclusive-as-positive",
    "writing-untraced-number", "writing-theorem-mismatch",
    "workflow-final-text-no-transition", "workflow-reviewer-cannot-mutate-target",
    "workflow-writer-cannot-create-claim", "workflow-incomplete-skill-blocks-track",
    "workflow-targeted-followup-minimal", "workflow-resume-preserves-state",
    "workflow-theory-route", "workflow-experiment-route", "workflow-mixed-route",
}


class ScientificEvaluationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = ScientificEvalRunner(ROOT)

    def test_all_adversarial_cases_pass_with_zero_s4_failures(self) -> None:
        run = self.runner.run()
        self.assertTrue(run.passed)
        self.assertEqual(run.s4_failures, 0)
        self.assertGreaterEqual(len(run.results), 40)

    def test_required_cases_and_all_seventeen_skills_are_covered(self) -> None:
        cases = self.runner.load_cases()
        self.assertTrue(REQUIRED_CASES <= {case.id for case in cases})
        covered = {skill for case in cases for skill in case.skills}
        self.assertEqual(covered, EXPECTED_SKILLS)

    def test_committed_regression_baseline_has_no_regression(self) -> None:
        baseline = ROOT / "evals/baselines/step5-v0.1.json"
        run = self.runner.run(baseline=baseline)
        self.assertTrue(run.passed)
        self.assertEqual(run.regression_errors, ())

    def test_regression_gate_rejects_a_stricter_previous_result(self) -> None:
        run = self.runner.run()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "baseline.json"
            path.write_text(
                json.dumps({
                    "suite_version": run.suite_version,
                    "cases": {
                        "literature-fabricated-citation": {
                            "passed": True,
                            "maximum_severity": "S0",
                            "finding_codes": [],
                        }
                    },
                }),
                encoding="utf-8",
            )
            compared = self.runner.run(baseline=path)
            self.assertFalse(compared.passed)
            self.assertTrue(compared.regression_errors)


if __name__ == "__main__":
    unittest.main()
