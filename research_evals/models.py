"""Typed, serializable evaluation results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Mapping


class Severity(IntEnum):
    S0 = 0
    S1 = 1
    S2 = 2
    S3 = 3
    S4 = 4

    @classmethod
    def parse(cls, value: str | int) -> "Severity":
        if isinstance(value, int):
            return cls(value)
        return cls[str(value)]


@dataclass(frozen=True)
class EvalFinding:
    code: str
    severity: Severity
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.name,
            "message": self.message,
        }


@dataclass(frozen=True)
class EvalCase:
    id: str
    title: str
    domain: str
    skills: tuple[str, ...]
    evaluator: str
    input: Mapping[str, Any]
    expected_findings: Mapping[str, Severity]


@dataclass(frozen=True)
class EvalResult:
    case_id: str
    title: str
    domain: str
    skills: tuple[str, ...]
    passed: bool
    findings: tuple[EvalFinding, ...]
    missing_findings: tuple[str, ...] = ()
    unexpected_findings: tuple[str, ...] = ()

    @property
    def maximum_severity(self) -> Severity:
        return max((finding.severity for finding in self.findings), default=Severity.S0)

    @property
    def acceptance_severity(self) -> Severity:
        return max(
            (finding.severity for finding in self.findings if not self.passed),
            default=(Severity.S4 if self.missing_findings else Severity.S0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "domain": self.domain,
            "skills": list(self.skills),
            "passed": self.passed,
            "findings": [finding.to_dict() for finding in self.findings],
            "missing_findings": list(self.missing_findings),
            "unexpected_findings": list(self.unexpected_findings),
            "maximum_severity": self.maximum_severity.name,
        }


@dataclass(frozen=True)
class EvalRun:
    suite_version: str
    results: tuple[EvalResult, ...]
    regression_errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results) and not self.regression_errors

    @property
    def s4_failures(self) -> int:
        return sum(
            1 for result in self.results
            if not result.passed and result.acceptance_severity == Severity.S4
        )

    def by_skill(self) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for result in self.results:
            for skill in result.skills:
                row = summary.setdefault(skill, {"passed": 0, "failed": 0})
                row["passed" if result.passed else "failed"] += 1
        return dict(sorted(summary.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_version": self.suite_version,
            "passed": self.passed,
            "s4_failures": self.s4_failures,
            "case_count": len(self.results),
            "results": [result.to_dict() for result in self.results],
            "by_skill": self.by_skill(),
            "regression_errors": list(self.regression_errors),
        }
