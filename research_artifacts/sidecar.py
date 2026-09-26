"""JSONL/stdin-stdout bridge between DeepSeek Harness and Python semantics."""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Mapping

from .artifact_service import ArtifactService
from .academic_sources import AcademicSourceTools
from .action_compiler import ActionCompiler
from .context_builder import ResearchContextBuilder
from .cost_control import BudgetStore
from .orchestrator import (
    CheckpointConflict,
    InvalidEvent,
    OrchestratorEvent,
    OrchestratorValidationError,
    ResearchOrchestrator,
    utc_now,
)
from .runtime_types import RUNTIME_PROTOCOL, RuntimeIntegrationError, json_value
from .transactions import ScientificTransaction
from .tool_adapters import ExperimentExecutionAdapter, LeanToolAdapter
from .workspace import ArtifactError, ArtifactValidationError, RevisionConflict


class SidecarServer:
    """Sequential request dispatcher; no network listener or background scheduler."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.artifacts = ArtifactService(self.root)
        self.actions = ActionCompiler(self.root)
        self.contexts = ResearchContextBuilder(self.root)
        self.orchestrator = ResearchOrchestrator(self.root)
        self.transactions = ScientificTransaction(self.root)
        self.lean = LeanToolAdapter()
        self.experiments = ExperimentExecutionAdapter(workspace_root=self.root)
        self.budgets = BudgetStore(self.root)
        self.academic = AcademicSourceTools(self.root)
        self.running = True
        self.methods: dict[str, Callable[[Mapping[str, Any]], Any]] = {
            "health": self._health,
            "shutdown": self._shutdown,
            "workspace.git_head": self._git_head,
            "workspace.push": lambda p: self.transactions.push_head(
                int(p.get("attempts", 3))
            ),
            "artifact.read": lambda p: self.artifacts.read(str(p["artifact_id"])),
            "artifact.query": lambda p: self.artifacts.query(
                kind=p.get("kind"), status=p.get("status"), project_id=p.get("project_id")
            ),
            "artifact.resolve": lambda p: self.artifacts.resolve(p.get("refs", [])),
            "artifact.list_proposals": lambda p: [
                proposal.to_dict() for proposal in self.artifacts.list_proposals(
                    str(p["project_id"]), str(p["action_id"])
                )
            ],
            "artifact.stage_create": lambda p: self._stage(p, "CREATE"),
            "artifact.stage_revision": lambda p: self._stage(p, "REVISE"),
            "artifact.validate_action": lambda p: self.artifacts.validate_action(
                str(p["project_id"]), str(p["action_id"]), p.get("proposal_ids")
            ),
            "artifact.validate_resource_action": lambda p: self.artifacts.validate_resource_action(
                str(p["project_id"]), str(p["action_id"])
            ),
            "artifact.submit_action": lambda p: self.artifacts.submit_action(**dict(p)),
            "artifact.load_submission": lambda p: self.artifacts.load_submission(
                str(p["project_id"]), str(p["action_id"])
            ),
            "artifact.promote_action": lambda p: self.transactions.promote_action(**dict(p)),
            "artifact.approve_plan": lambda p: self.transactions.approve_plan(**dict(p)),
            "project.init": lambda p: self.transactions.initialize_project(**dict(p)),
            "artifact.materialize_outputs": lambda p: self.transactions.materialize_outputs(**dict(p)),
            "artifact.record_recovery_decision": lambda p: self.transactions.record_recovery_decision(**dict(p)),
            "proposal.evaluate_gate": lambda p: self.transactions.evaluate_proposal_gate(
                project_id=str(p["project_id"])
            ),
            "proposal.write_decision_template": lambda p: self.transactions.write_proposal_decision_template(
                project_id=str(p["project_id"])
            ),
            "proposal.record_decisions": lambda p: self.transactions.record_proposal_decisions(**dict(p)),
            "proposal.convert": lambda p: self.transactions.convert_approved_proposal(**dict(p)),
            "resource.read": lambda p: self.academic.resource_read(**dict(p)),
            "resource.write": lambda p: self.academic.resource_write(**dict(p)),
            "paper.manuscript_read": lambda p: self.academic.manuscript_read(**dict(p)),
            "paper.filesystem": lambda p: self.academic.paper_filesystem(**dict(p)),
            "academic.search": lambda p: self.academic.academic_search(**dict(p)),
            "academic.citation_neighborhood": lambda p: self.academic.citation_neighborhood(**dict(p)),
            "academic.source_read": lambda p: self.academic.source_read(**dict(p)),
            "proposal.write": lambda p: self.academic.proposal_write(**dict(p)),
            "action.preview": lambda p: self.actions.preview(str(p["project_id"])),
            "action.compile": lambda p: self.actions.compile(**dict(p)),
            "tool.lean_verify": lambda p: self.lean.verify(**dict(p)).__dict__,
            "tool.experiment_run": self._experiment_run,
            "context.build": lambda p: self.contexts.build(**dict(p)),
            "orchestrator.inspect": lambda p: self.orchestrator.store.load(str(p["project_id"])),
            "orchestrator.fail_closed_recover": lambda p: self.transactions.fail_closed_checkpoint(
                project_id=str(p["project_id"]), reason=str(p["reason"])
            ),
            "orchestrator.begin_action": self._begin_action,
            "orchestrator.apply_event": self._apply_event,
            "orchestrator.commit_event": lambda p: self.transactions.apply_control_event(**dict(p)),
            "orchestrator.prepare_route": self._prepare_route,
            "gate.evaluate": self._gate_evaluate,
            "transaction.recover": lambda p: self.transactions.recover(p.get("project_id")),
            "budget.inspect": lambda p: self.budgets.status(str(p["project_id"])),
            "budget.approve_initial": lambda p: self.budgets.approve_initial(
                str(p["project_id"]), str(p["run_id"]), float(p["budget_percent"])
            ),
            "budget.increase": lambda p: self.budgets.increase(
                str(p["project_id"]), float(p["additional_percent"]),
                actor_id=str(p.get("actor_id", "researcher")),
            ),
            "budget.reserve": lambda p: self.budgets.reserve(
                str(p["project_id"]), p["action"]
            ),
            "budget.consume": lambda p: self.budgets.consume(
                str(p["project_id"]), str(p["action_id"]), p.get("usage")
            ),
        }

    def dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request_id = request.get("request_id")
        try:
            if request.get("protocol") != RUNTIME_PROTOCOL:
                raise RuntimeIntegrationError(
                    f"unsupported protocol: {request.get('protocol')}"
                )
            if not isinstance(request_id, str) or not request_id:
                raise RuntimeIntegrationError("request_id must be a non-empty string")
            method = request.get("method")
            handler = self.methods.get(str(method))
            if handler is None:
                raise RuntimeIntegrationError(f"unknown sidecar method: {method}")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise RuntimeIntegrationError("params must be a JSON object")
            return {
                "protocol": RUNTIME_PROTOCOL,
                "request_id": request_id,
                "ok": True,
                "result": json_value(handler(params)),
            }
        except Exception as exc:  # one malformed request must not kill the sidecar
            return {
                "protocol": RUNTIME_PROTOCOL,
                "request_id": request_id,
                "ok": False,
                "error": self._error(exc),
            }

    def serve(self) -> int:
        for line in sys.stdin:
            if not self.running:
                break
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("request root must be an object")
                response = self.dispatch(value)
            except Exception as exc:
                response = {
                    "protocol": RUNTIME_PROTOCOL,
                    "request_id": None,
                    "ok": False,
                    "error": self._error(exc),
                }
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        return 0

    def _stage(self, params: Mapping[str, Any], operation: str) -> dict[str, Any]:
        value = dict(params)
        value["operation"] = operation
        return self.artifacts.stage_proposal(**value).to_dict()

    def _experiment_run(self, params: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(params)
        experiment_id = str(value.pop("experiment_id"))
        experiment = self.artifacts.workspace.get(experiment_id)
        if experiment.kind != "Experiment":
            raise RuntimeIntegrationError(f"{experiment_id} is not an Experiment")
        return self.experiments.execute(experiment=experiment.data, **value)

    def _begin_action(self, params: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(params)
        value.setdefault("at", utc_now())
        value.setdefault("commit", True)
        return self.transactions.begin_action(**value)

    def _git_head(self, _params: Mapping[str, Any]) -> dict[str, Any]:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeIntegrationError("workspace is not a Git repository")
        return {"git_commit": result.stdout.strip().lower()}

    def _apply_event(self, params: Mapping[str, Any]) -> dict[str, Any]:
        event_value = params.get("event")
        if not isinstance(event_value, dict):
            raise InvalidEvent("event must be a JSON object")
        reduction = self.orchestrator.apply(
            str(params["project_id"]),
            OrchestratorEvent(
                str(event_value["type"]),
                str(event_value.get("at") or utc_now()),
                dict(event_value.get("payload", {})),
            ),
            expected_seq=int(params["expected_seq"]),
        )
        return {
            "checkpoint": reduction.checkpoint,
            "commands": [
                {"type": command.type, "payload": dict(command.payload)}
                for command in reduction.commands
            ],
        }

    def _prepare_route(self, params: Mapping[str, Any]) -> dict[str, Any]:
        project_id = str(params["project_id"])
        checkpoint = self.orchestrator.store.load(project_id)
        plan = self.orchestrator.active_plan(checkpoint)
        if plan is None:
            raise InvalidEvent("routing requires an active plan")
        refs = []
        for record in self.artifacts.workspace.query(project_id=project_id):
            refs.append(
                {
                    "id": record.id,
                    "kind": record.kind,
                    "revision": record.data["revision"],
                }
            )
        return {
            "snapshot": {
                "git_commit": str(params["git_commit"]),
                "artifacts": sorted(refs, key=lambda ref: ref["id"]),
            }
        }

    def _gate_evaluate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        gate = params.get("gate")
        if not isinstance(gate, dict):
            raise InvalidEvent("gate must be a JSON object")
        checkpoint = self.orchestrator.store.load(str(params["project_id"]))
        active_plan = self.orchestrator.active_plan(checkpoint)
        self.orchestrator.gates.require_valid(
            gate, active_plan.data if active_plan is not None else None
        )
        return {"valid": True, "verdict": gate["verdict"], "gate": gate}

    def _health(self, _params: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": "ok",
            "protocol": RUNTIME_PROTOCOL,
            "workspace": str(self.root),
            "artifact_schema": "research-artifact/v0.1.4",
            "orchestrator": "research-orchestrator/v0.1",
        }

    def _shutdown(self, _params: Mapping[str, Any]) -> dict[str, Any]:
        self.running = False
        return {"status": "shutting-down"}

    @staticmethod
    def _error(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, RuntimeIntegrationError):
            code = exc.code
            retryable = exc.retryable
            details = exc.details
        elif isinstance(exc, RevisionConflict):
            code, retryable, details = "REVISION_CONFLICT", True, None
        elif isinstance(exc, CheckpointConflict):
            code, retryable, details = "CHECKPOINT_CONFLICT", True, None
        elif isinstance(exc, InvalidEvent):
            code, retryable, details = "INVALID_EVENT", False, None
        elif isinstance(exc, ArtifactValidationError):
            code, retryable = "VALIDATION_FAILED", False
            details = [issue.__dict__ for issue in exc.issues]
        elif isinstance(exc, OrchestratorValidationError):
            code, retryable, details = "VALIDATION_FAILED", False, exc.issues
        elif isinstance(exc, ArtifactError):
            code, retryable, details = "ARTIFACT_ERROR", False, None
        else:
            code, retryable, details = "INTERNAL", False, None
            traceback.print_exc(file=sys.stderr)
        return {
            "code": code,
            "message": str(exc),
            "details": json_value(details),
            "retryable": retryable,
        }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    workspace = Path(arguments[0] if arguments else ".")
    return SidecarServer(workspace).serve()


if __name__ == "__main__":
    raise SystemExit(main())
