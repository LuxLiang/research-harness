"""Evaluation suite loading, execution, and hard regression gates."""

from __future__ import annotations

import json
from pathlib import Path

from research_artifacts.resources import resource_path
from typing import Any, Iterable, Mapping

import yaml
from jsonschema import Draft202012Validator

from .evaluators import EVALUATORS
from .models import EvalCase, EvalResult, EvalRun, Severity


SUITE_VERSION = "research-skill-evals/v0.1"


class ScientificEvalRunner:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        schema = json.loads(
            (resource_path(self.root, "schemas/evals/v0.1/eval-case.schema.json")).read_text(encoding="utf-8")
        )
        self.validator = Draft202012Validator(schema)

    def load_cases(self, case_paths: Iterable[Path] | None = None) -> list[EvalCase]:
        paths = list(case_paths or sorted((resource_path(self.root, "evals/cases")).glob("*.yaml")))
        cases: list[EvalCase] = []
        seen: set[str] = set()
        for path in paths:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            errors = sorted(self.validator.iter_errors(document), key=lambda error: list(error.path))
            if errors:
                raise ValueError(f"invalid eval case file {path}: {errors[0].message}")
            for item in document["cases"]:
                if item["id"] in seen:
                    raise ValueError(f"duplicate eval case ID: {item['id']}")
                seen.add(item["id"])
                cases.append(EvalCase(
                    id=item["id"], title=item["title"], domain=item["domain"],
                    skills=tuple(item["skills"]), evaluator=item["evaluator"],
                    input=item["input"],
                    expected_findings={
                        finding["code"]: Severity.parse(finding["severity"])
                        for finding in item["expected_findings"]
                    },
                ))
        return cases

    def run(
        self,
        *,
        domains: set[str] | None = None,
        baseline: str | Path | None = None,
    ) -> EvalRun:
        results: list[EvalResult] = []
        for case in self.load_cases():
            if domains and case.domain not in domains:
                continue
            evaluator = EVALUATORS.get(case.evaluator)
            if evaluator is None:
                raise ValueError(f"unknown evaluator: {case.evaluator}")
            findings = tuple(sorted(evaluator(case.input, self.root), key=lambda item: item.code))
            actual = {item.code: item.severity for item in findings}
            missing = tuple(sorted(set(case.expected_findings) - set(actual)))
            unexpected = tuple(sorted(set(actual) - set(case.expected_findings)))
            severity_mismatch = {
                code for code in set(actual) & set(case.expected_findings)
                if actual[code] != case.expected_findings[code]
            }
            results.append(EvalResult(
                case_id=case.id, title=case.title, domain=case.domain,
                skills=case.skills,
                passed=not missing and not unexpected and not severity_mismatch,
                findings=findings,
                missing_findings=tuple(sorted(set(missing) | severity_mismatch)),
                unexpected_findings=tuple(sorted(set(unexpected) | severity_mismatch)),
            ))
        regression_errors = self._compare_baseline(results, baseline) if baseline else []
        return EvalRun(SUITE_VERSION, tuple(results), tuple(regression_errors))

    def write_baseline(self, path: str | Path, run: EvalRun) -> None:
        if not run.passed:
            raise ValueError("refusing to write a baseline from a failing evaluation run")
        value = {
            "suite_version": run.suite_version,
            "cases": {
                result.case_id: {
                    "passed": result.passed,
                    "maximum_severity": result.maximum_severity.name,
                    "finding_codes": [finding.code for finding in result.findings],
                }
                for result in run.results
            },
        }
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _compare_baseline(results: list[EvalResult], baseline: str | Path) -> list[str]:
        expected = json.loads(Path(baseline).read_text(encoding="utf-8"))
        current = {result.case_id: result for result in results}
        errors: list[str] = []
        for case_id, prior in expected.get("cases", {}).items():
            result = current.get(case_id)
            if result is None:
                errors.append(f"baseline case missing from current suite: {case_id}")
                continue
            if prior.get("passed") and not result.passed:
                errors.append(f"previously passing case regressed: {case_id}")
            prior_severity = Severity.parse(prior.get("maximum_severity", "S0"))
            if result.maximum_severity > prior_severity:
                errors.append(
                    f"finding severity increased for {case_id}: {prior_severity.name} -> {result.maximum_severity.name}"
                )
        return errors
