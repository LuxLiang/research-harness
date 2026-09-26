"""Command-line entry point for scientific Skill evaluations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .runner import ScientificEvalRunner


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="research-evals")
    value.add_argument("--workspace", default=".")
    value.add_argument("--domain", action="append", dest="domains")
    value.add_argument("--baseline")
    value.add_argument("--write-baseline")
    value.add_argument("--json", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.workspace).resolve()
    runner = ScientificEvalRunner(root)
    run = runner.run(
        domains=set(args.domains) if args.domains else None,
        baseline=(root / args.baseline if args.baseline else None),
    )
    if args.write_baseline:
        runner.write_baseline(root / args.write_baseline, run)
    if args.json:
        print(json.dumps(run.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Scientific evals: {len(run.results)} cases; passed={run.passed}; S4 failures={run.s4_failures}")
        for skill, summary in run.by_skill().items():
            print(f"  {skill}: {summary['passed']} passed, {summary['failed']} failed")
        for result in run.results:
            if not result.passed:
                print(f"FAIL {result.case_id}: missing={list(result.missing_findings)} unexpected={list(result.unexpected_findings)}")
        for error in run.regression_errors:
            print(f"REGRESSION {error}")
    return 0 if run.passed and run.s4_failures == 0 else 1
