"""Command-line interface for the Research Artifact Layer."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .workspace import (
    ARTIFACT_KINDS,
    ArtifactError,
    ArtifactValidationError,
    ArtifactWorkspace,
)
from .orchestrator import OrchestratorEvent, ResearchOrchestrator, utc_now


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-artifacts")
    parser.add_argument("--workspace", default=".", help="workspace root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate all artifacts")
    validate.add_argument("--json", action="store_true", help="emit machine-readable JSON")

    list_parser = subparsers.add_parser("list", help="list/query artifacts")
    list_parser.add_argument("--kind", choices=sorted(ARTIFACT_KINDS))
    list_parser.add_argument("--status")
    list_parser.add_argument("--project-id")
    list_parser.add_argument("--json", action="store_true")

    show = subparsers.add_parser("show", help="show one artifact")
    show.add_argument("artifact_id")
    show.add_argument("--json", action="store_true")

    graph = subparsers.add_parser("graph", help="render the computed dependency graph")
    graph.add_argument("--format", choices=["mermaid", "json"], default="mermaid")

    transition = subparsers.add_parser("transition", help="atomically transition status or Project stage")
    transition.add_argument("artifact_id")
    transition.add_argument("--expected-revision", required=True, type=int)
    target = transition.add_mutually_exclusive_group(required=True)
    target.add_argument("--status", dest="new_status")
    target.add_argument("--stage", dest="new_stage")
    transition.add_argument("--actor-type", choices=["human", "agent"], required=True)
    transition.add_argument("--actor-id", required=True)
    transition.add_argument("--session-id")

    orchestrator_init = subparsers.add_parser(
        "orchestrator-init", help="initialize a project's deterministic controller"
    )
    orchestrator_init.add_argument("project_id")
    orchestrator_init.add_argument("--run-id", required=True)
    orchestrator_init.add_argument("--git-commit", required=True)

    orchestrator_show = subparsers.add_parser(
        "orchestrator-show", help="show a project's persisted controller state"
    )
    orchestrator_show.add_argument("project_id")
    orchestrator_show.add_argument("--json", action="store_true")

    orchestrator_event = subparsers.add_parser(
        "orchestrator-event", help="apply one typed controller event"
    )
    orchestrator_event.add_argument("project_id")
    orchestrator_event.add_argument("event_type")
    orchestrator_event.add_argument("--expected-seq", required=True, type=int)
    orchestrator_event.add_argument("--at")
    orchestrator_event.add_argument(
        "--payload-json", default="{}", help="JSON object payload"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        workspace = ArtifactWorkspace(Path(args.workspace))
        if args.command == "validate":
            issues = workspace.validate()
            if args.json:
                print(json.dumps([issue.__dict__ for issue in issues], indent=2))
            elif issues:
                for issue in issues:
                    print(issue)
            else:
                print(f"valid: {len(workspace.discover())} artifacts")
            return 1 if issues else 0

        if args.command == "list":
            records = workspace.query(kind=args.kind, status=args.status, project_id=args.project_id)
            if args.json:
                print(json.dumps([{"id": record.id, "kind": record.kind, "status": record.data.get("status"), "revision": record.data.get("revision"), "path": str(record.path)} for record in records], indent=2))
            else:
                for record in records:
                    print(f"{record.id}\t{record.kind}\t{record.data.get('status')}\tr{record.data.get('revision')}\t{record.path}")
            return 0

        if args.command == "show":
            record = workspace.get(args.artifact_id)
            if args.json:
                print(json.dumps(record.data, indent=2, ensure_ascii=False))
            else:
                print(yaml.safe_dump(record.data, sort_keys=False, allow_unicode=True), end="")
            return 0

        if args.command == "graph":
            workspace.require_valid()
            if args.format == "json":
                print(json.dumps(workspace.graph_edges(), indent=2))
            else:
                print(workspace.mermaid_graph(), end="")
            return 0

        if args.command == "transition":
            record = workspace.transition(
                args.artifact_id,
                expected_revision=args.expected_revision,
                actor_type=args.actor_type,
                actor_id=args.actor_id,
                new_status=args.new_status,
                new_stage=args.new_stage,
                session_id=args.session_id,
            )
            print(f"updated {record.id} to revision {record.data['revision']}")
            return 0

        if args.command == "orchestrator-init":
            orchestrator = ResearchOrchestrator(Path(args.workspace))
            checkpoint = orchestrator.initialize(
                args.project_id, args.run_id, args.git_commit
            )
            print(yaml.safe_dump(checkpoint, sort_keys=False), end="")
            return 0

        if args.command == "orchestrator-show":
            orchestrator = ResearchOrchestrator(Path(args.workspace))
            checkpoint = orchestrator.store.load(args.project_id)
            if args.json:
                print(json.dumps(checkpoint, indent=2, ensure_ascii=False))
            else:
                print(yaml.safe_dump(checkpoint, sort_keys=False), end="")
            return 0

        if args.command == "orchestrator-event":
            try:
                payload = json.loads(args.payload_json)
            except json.JSONDecodeError as exc:
                raise ArtifactError(f"invalid --payload-json: {exc}") from exc
            if not isinstance(payload, dict):
                raise ArtifactError("--payload-json must be a JSON object")
            orchestrator = ResearchOrchestrator(Path(args.workspace))
            event = OrchestratorEvent(
                args.event_type,
                args.at or utc_now(),
                payload,
            )
            reduction = orchestrator.apply(
                args.project_id, event, expected_seq=args.expected_seq
            )
            print(
                json.dumps(
                    {
                        "checkpoint_seq": reduction.checkpoint["checkpoint_seq"],
                        "state": reduction.checkpoint["state"],
                        "status": reduction.checkpoint["status"],
                        "commands": [
                            {"type": command.type, "payload": command.payload}
                            for command in reduction.commands
                        ],
                    },
                    indent=2,
                )
            )
            return 0
    except ArtifactValidationError as exc:
        for issue in exc.issues:
            print(issue, file=sys.stderr)
        return 1
    except ArtifactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
