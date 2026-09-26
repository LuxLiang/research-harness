"""Minimal command-line surface for the wired Research Harness MVP."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

from .mvp import MVPController, SyntheticSkillRuntime
from .bootstrap import initialize_workspace
from .doctor import diagnose
from .operator_client import CordisClient, OperatorError, operator_socket
from .workspace import ArtifactError


def _project(value: str) -> str:
    return value if value.startswith("proj-") else f"proj-{value}"


def _positive(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return result


def _percentage(value: str) -> float:
    result = _positive(value)
    if result > 100:
        raise argparse.ArgumentTypeError("must not exceed 100")
    return result


def _action_limit(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _runtime_options(parser: argparse.ArgumentParser, *, child: bool = False) -> None:
    parser.add_argument("--runtime", choices=["synthetic", "cordis"],
                        default=argparse.SUPPRESS if child else "synthetic",
                        help="synthetic test execution (default), or the model-backed Cordis host")
    parser.add_argument("--socket", default=argparse.SUPPRESS if child else None,
                        help="Cordis Unix socket; defaults to the workspace's .harness/research/cordis.sock")
    parser.add_argument("--timeout", type=_positive,
                        default=argparse.SUPPRESS if child else 3600,
                        help="operator response timeout in seconds; timeout does not cancel the host")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="research")
    root.add_argument("--workspace", default=".")
    _runtime_options(root)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("workspace-init", help="Initialize a separate, empty research workspace")
    commands.add_parser("doctor", help="Check local resources and runtime connectivity without executing research")
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
    run.add_argument("--budget-percent", type=_percentage)
    run.add_argument("--max-actions", type=_action_limit)
    budget = commands.add_parser("budget")
    budget.add_argument("project")
    group = budget.add_mutually_exclusive_group(required=True)
    group.add_argument("--minimum", action="store_true")
    group.add_argument("--recommended", action="store_true")
    group.add_argument("--additional-percent", type=_percentage)
    group.add_argument("--keep-paused", action="store_true")
    unblock = commands.add_parser("unblock"); unblock.add_argument("project"); unblock.add_argument("--reason", required=True)
    pause = commands.add_parser("pause"); pause.add_argument("project"); pause.add_argument("--reason", default="User requested pause")
    cancel = commands.add_parser("cancel"); cancel.add_argument("project"); cancel.add_argument("--reason", default="User cancelled the project")
    revision = commands.add_parser("plan-revision", help="Request a plan revision through the Cordis controller")
    revision.add_argument("project"); revision.add_argument("--reason", required=True)
    decisions = commands.add_parser("proposal-decisions")
    decisions.add_argument("project"); decisions.add_argument("--file", required=True)
    convert = commands.add_parser("convert")
    convert.add_argument("project"); convert.add_argument("--to", choices=["full-research"], required=True)
    convert.add_argument("--project", dest="derived_project", required=True)
    pilot = commands.add_parser("pilot")
    pilot.add_argument("pilot", choices=["theory", "experiment", "mixed", "all"])
    pilot.add_argument("--lean-project")
    pilot.add_argument("--no-agent", action="store_true", help="Use deterministic test auditor instead of Codex sessions")
    pilot.add_argument("--budget-percent", type=_percentage)
    for command in commands.choices.values():
        _runtime_options(command, child=True)
    return root


def _ask_budget() -> float:
    if not sys.stdin.isatty():
        raise ValueError("provide --budget-percent for noninteractive pilot execution")
    print("Select an estimated harness budget (not a provider quota or billing limit).", file=sys.stderr)
    print("1. ~5%   — very economical\n2. ~10%  — standard\n3. ~20%  — thorough\n4. ~30%+ — intensive\n5. Custom %", file=sys.stderr)
    choice = input("Select 1-5: ").strip()
    presets = {"1": 5.0, "2": 10.0, "3": 20.0, "4": 30.0}
    if choice in presets:
        return presets[choice]
    if choice == "5":
        return _percentage(input("Custom percentage: ").strip())
    raise ValueError("a budget selection is required before research run")


def _cordis_command(args: argparse.Namespace, workspace: Path) -> object:
    client = CordisClient(workspace, socket_path=args.socket, timeout=args.timeout)
    client.handshake()
    project_id = _project(args.project)
    base = {"project_id": project_id}

    def call(method: str, **params: object) -> object:
        return client.call(method, {**base, **params})

    command = args.command
    if command == "init":
        options: dict[str, object] = {"workflowMode": args.mode.replace("-", "_").upper()}
        for field, value in (("proposalPath", args.proposal), ("rubricPath", args.rubric)):
            if value:
                options[field] = str(Path(value).expanduser().resolve())
        if args.source_material:
            options["sourceMaterials"] = [str(Path(path).expanduser().resolve()) for path in args.source_material]
        return call("init", objective=args.objective, options=options)
    if command == "run":
        params = {key: getattr(args, key) for key in ("budget_percent", "max_actions")
                  if getattr(args, key) is not None}
        return call("run", **params)
    if command == "status":
        return call("status")
    if command == "budget":
        status = call("status")
        budget = status.get("budget", {}) if isinstance(status, dict) else {}
        if not isinstance(budget, dict):
            raise OperatorError("operator returned an invalid budget status")
        if args.keep_paused:
            return budget
        request = budget.get("pending_request") or {}
        if not isinstance(request, dict):
            raise OperatorError("operator returned an invalid pending budget request")
        amount = (request.get("minimum_additional_percent") if args.minimum else
                  request.get("recommended_additional_percent") if args.recommended else args.additional_percent)
        if amount is None:
            raise ValueError("no pending budget request supplies that amount")
        call("budget", additional_percent=_percentage(str(amount)))
        return call("run")
    if command in {"pause", "cancel", "unblock", "plan-revision"}:
        call(command, reason=args.reason)
        return call("run" if command == "unblock" else "status")
    if command == "resume":
        call("resume")
        return call("run")
    if command == "approve":
        call("approve")
        return call("status")
    if command == "proposal-decisions":
        return call(command, decisions_path=str(Path(args.file).expanduser().resolve()))
    if command == "convert":
        return call(command, derived_project_id=_project(args.derived_project))
    raise ValueError(f"unsupported Cordis command: {command}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()
    if args.command == "doctor":
        report = diagnose(workspace, runtime=args.runtime, socket_path=args.socket,
                          timeout=min(args.timeout, 5))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["ok"] else 2
    if args.command == "workspace-init":
        try:
            print(json.dumps({"workspace": str(initialize_workspace(workspace))}))
            return 0
        except (ArtifactError, OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.runtime == "cordis":
        try:
            if args.command == "pilot":
                raise ValueError("pilot has its own auditor; use --runtime synthetic, or use init/run for Cordis")
            value = _cordis_command(args, workspace)
            print(json.dumps(value, indent=2, ensure_ascii=False))
            return 0
        except (OperatorError, OSError, ValueError, argparse.ArgumentTypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command not in {"status"}:
        try:
            if operator_socket(workspace, args.socket).exists():
                raise ValueError("a Cordis socket path exists in this workspace; use --runtime cordis, "
                                 "or stop the host and resolve its stale socket before synthetic execution")
        except (OperatorError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if args.command == "pilot":
        from .golden_pilots import run_pilot

        selected = ["theory", "experiment", "mixed"] if args.pilot == "all" else [args.pilot]
        results = []
        try:
            budget_percent = args.budget_percent if args.budget_percent is not None else _ask_budget()
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
        except (ArtifactError, OSError, ValueError, argparse.ArgumentTypeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    project_id = _project(args.project)
    try:
        if args.command == "plan-revision":
            raise ValueError("plan-revision requires --runtime cordis")
        if args.command in {"run", "resume", "unblock", "budget"}:
            print("runtime=synthetic: test execution only; no model-backed research", file=sys.stderr)
        controller = MVPController(workspace, SyntheticSkillRuntime(workspace))
        if args.command == "init":
            value = controller.init(
                project_id, args.objective, mode=args.mode,
                proposal=args.proposal, rubric=args.rubric,
                source_materials=args.source_material,
            )
        elif args.command == "run":
            budget_percent = args.budget_percent
            if controller.budgets.load(project_id) is None and budget_percent is None and sys.stdin.isatty():
                budget_percent = _ask_budget()
            options = {} if args.max_actions is None else {"max_actions": args.max_actions}
            value = controller.run(project_id, budget_percent=budget_percent, **options)
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
    except (ArtifactError, OSError, ValueError, argparse.ArgumentTypeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
