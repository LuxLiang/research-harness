"""Minimal command-line surface for the wired Research Harness MVP."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .mvp import MVPController, SyntheticSkillRuntime
from .bootstrap import initialize_workspace
from .workspace import ArtifactError


def _project(value: str) -> str:
    return value if value.startswith("proj-") else f"proj-{value}"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="research")
    root.add_argument("--workspace", default=".")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("workspace-init", help="Initialize a separate, empty research workspace")
    init = commands.add_parser("init")
    init.add_argument("project")
    init.add_argument("--mode", choices=["full-research", "proposal-review"], default="full-research")
    init.add_argument("--proposal")
    init.add_argument("--rubric")
    init.add_argument(
        "--source-material", action="append", default=[],
        help="Repeatable read-only input for FULL_RESEARCH; copied and hash-pinned",
    )
    init.add_argument("--objective", default="Evaluate a traceable scientific workflow.")
    for name in ("status", "resume", "approve"):
        commands.add_parser(name).add_argument("project")
    run = commands.add_parser("run")
    run.add_argument("project")
    run.add_argument("--budget-percent", type=float)
    budget = commands.add_parser("budget")
    budget.add_argument("project")
    group = budget.add_mutually_exclusive_group(required=True)
    group.add_argument("--minimum", action="store_true")
    group.add_argument("--recommended", action="store_true")
    group.add_argument("--additional-percent", type=float)
    group.add_argument("--keep-paused", action="store_true")
    unblock = commands.add_parser("unblock"); unblock.add_argument("project"); unblock.add_argument("--reason", required=True)
    pause = commands.add_parser("pause"); pause.add_argument("project"); pause.add_argument("--reason", default="User requested pause")
    cancel = commands.add_parser("cancel"); cancel.add_argument("project"); cancel.add_argument("--reason", default="User cancelled the project")
    decisions = commands.add_parser("proposal-decisions")
    decisions.add_argument("project"); decisions.add_argument("--file", required=True)
    convert = commands.add_parser("convert")
    convert.add_argument("project"); convert.add_argument("--to", choices=["full-research"], required=True)
    convert.add_argument("--project", dest="derived_project", required=True)
    pilot = commands.add_parser("pilot")
    pilot.add_argument("pilot", choices=["theory", "experiment", "mixed", "all"])
    pilot.add_argument("--lean-project")
    pilot.add_argument("--no-agent", action="store_true", help="Use deterministic test auditor instead of Codex sessions")
    pilot.add_argument("--budget-percent", type=float)
    return root


def _ask_budget() -> float:
    print("How much of your current Codex allowance may this research run use?", file=sys.stderr)
    print("1. ~5%   — very economical\n2. ~10%  — standard\n3. ~20%  — thorough\n4. ~30%+ — intensive\n5. Custom %", file=sys.stderr)
    choice = input("Select 1-5: ").strip()
    presets = {"1": 5.0, "2": 10.0, "3": 20.0, "4": 30.0}
    if choice in presets:
        return presets[choice]
    if choice == "5":
        return float(input("Custom percentage: ").strip())
    raise ValueError("a budget selection is required before research run")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    workspace = Path(args.workspace).resolve()
    if args.command == "workspace-init":
        try:
            print(json.dumps({"workspace": str(initialize_workspace(workspace))}))
            return 0
        except (ArtifactError, OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "pilot":
        from .golden_pilots import run_pilot

        selected = ["theory", "experiment", "mixed"] if args.pilot == "all" else [args.pilot]
        budget_percent = args.budget_percent if args.budget_percent is not None else _ask_budget()
        results = []
        try:
            for name in selected:
                results.append(
                    run_pilot(
                        workspace, name, use_codex=not args.no_agent,
                        lean_project=args.lean_project,
                        exercise_pause=name == "mixed",
                        budget_percent=budget_percent,
                    )
                )
            print(json.dumps(results, indent=2, ensure_ascii=False))
            return 0
        except (ArtifactError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    project_id = _project(args.project)
    try:
        controller = MVPController(workspace, SyntheticSkillRuntime(workspace))
        if args.command == "init":
            value = controller.init(
                project_id, args.objective, mode=args.mode,
                proposal=args.proposal, rubric=args.rubric,
                source_materials=args.source_material,
            )
        elif args.command == "run":
            budget_percent = args.budget_percent
            if controller.budgets.load(project_id) is None and budget_percent is None:
                budget_percent = _ask_budget()
            value = controller.run(project_id, budget_percent=budget_percent)
        elif args.command == "budget":
            status = controller.budgets.status(project_id)
            if args.keep_paused:
                value = status
            else:
                request = status.get("pending_request") or {}
                amount = (
                    request.get("minimum_additional_percent") if args.minimum else
                    request.get("recommended_additional_percent") if args.recommended else
                    args.additional_percent
                )
                if amount is None:
                    raise ValueError("no pending budget request supplies that amount")
                controller.approve_budget(project_id, float(amount))
                value = controller.run(project_id)
        elif args.command == "status":
            value = controller.status(project_id)
        elif args.command == "pause":
            controller.pause(project_id, args.reason); value = controller.status(project_id)
        elif args.command == "resume":
            controller.resume(project_id); value = controller.run(project_id)
        elif args.command == "unblock":
            controller.unblock(project_id, args.reason); value = controller.run(project_id)
        elif args.command == "approve":
            controller.approve(project_id); value = controller.status(project_id)
        elif args.command == "proposal-decisions":
            value = controller.proposal_decisions(project_id, args.file)
        elif args.command == "convert":
            value = controller.convert(project_id, args.derived_project)
        elif args.command == "cancel":
            controller.cancel(project_id, args.reason); value = controller.status(project_id)
        else:  # pragma: no cover
            return 2
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0
    except (ArtifactError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
