"""Cost-aware model routing and version-controlled run budget policy.

Percentages are relative user constraints.  They are deliberately not
presented as measured Codex allowance when the runtime cannot observe that
allowance.  Scientific state remains in artifacts/checkpoints; this module
only owns operational budget control.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

from .resources import resource_path
from typing import Any, Iterator, Mapping

import yaml
from jsonschema import Draft202012Validator

from .orchestrator import utc_now
from .runtime_types import RuntimeValidationError, atomic_write_text

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class BudgetRequired(RuntimeValidationError):
    code = "BUDGET_REQUIRED"


class BudgetApprovalRequired(RuntimeValidationError):
    code = "BUDGET_APPROVAL_REQUIRED"

    def __init__(self, request: Mapping[str, Any]):
        super().__init__("approved run budget is insufficient", details=dict(request))


class ModelRouter:
    """Deterministic tier selection with immutable per-Skill quality floors."""

    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        self.order = list(config["tier_order"])

    def route(self, action: Mapping[str, Any]) -> dict[str, Any]:
        skill = str(action["skill"])
        policy = self.config["skills"].get(skill)
        if not isinstance(policy, Mapping):
            raise RuntimeValidationError(f"missing model policy for Skill {skill}")
        floor = str(policy["quality_floor"])
        selected = str(policy.get("default_tier", floor))
        if self.order.index(selected) < self.order.index(floor):
            selected = floor
        factors = action.get("model_routing", {})
        reasons = [f"{skill} default={selected}", f"quality floor={floor}"]
        for key in ("scientific_risk", "difficulty", "uncertainty"):
            value = str(factors.get(key, "MEDIUM")).upper()
            if value == "HIGH" and self.order.index(selected) < self.order.index("frontier"):
                selected = self.order[self.order.index(selected) + 1]
                reasons.append(f"upward escalation: {key}=HIGH")
        if int(action.get("attempt", 0)) > 0 and self.order.index(selected) < self.order.index("frontier"):
            selected = self.order[self.order.index(selected) + 1]
            reasons.append("upward escalation: prior attempt failed")
        selected = self.order[max(self.order.index(selected), self.order.index(floor))]
        effort = str(policy.get("reasoning_effort", self.config["tier_reasoning"][selected]))
        alias = str(self.config["tier_aliases"][selected])
        estimate = float(self.config["cost_estimates_percent"][selected][effort])
        state = str(action.get("state", ""))
        critical_verification = (
            bool(policy.get("critical_verification", False))
            or "feasibility re-evaluation" in str(factors.get("reason", ""))
            or state in {
            "DISCOVERY_FEASIBILITY_GATE",
            "SYNTHESIS_COMPLETION_GATE",
            "CONSISTENCY_GATE",
            "WRITING_FINAL_REVIEW",
            "PROPOSAL_REVIEW_GATE",
            }
        )
        return {
            "model_tier": selected,
            "model_alias": alias,
            "reasoning_effort": effort,
            "quality_floor": floor,
            "estimated_cost_percent": estimate,
            "critical_verification": critical_verification,
            "routing_reason": "; ".join(reasons),
        }


class CostLedger:
    """Pure aggregation over persisted action charges."""

    @staticmethod
    def group(entries: list[Mapping[str, Any]]) -> dict[str, float]:
        grouped: dict[str, float] = defaultdict(float)
        for item in entries:
            grouped[f"{item['stage']}/{item['skill']}/{item['model_tier']}"] += float(item["charged_percent"])
        return dict(sorted(grouped.items()))


class BudgetGuard:
    """Protect quality floors and verification reserve before dispatch."""

    def __init__(self, config: Mapping[str, Any]):
        self.config = config

    def request_if_needed(
        self, project_id: str, policy: Mapping[str, Any],
        action: Mapping[str, Any], route: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        available = float(policy["approved_budget_percent"]) - float(policy["used_percent"]) - float(policy["reserved_percent"])
        protected = 0.0 if route["critical_verification"] else float(policy["verification_reserve_remaining_percent"])
        spendable = max(0.0, available - protected)
        estimate = float(route["estimated_cost_percent"])
        if estimate <= spendable + 1e-9:
            return None
        shortage = max(0.01, estimate - spendable)
        reserve_factor = (
            1.0 if route["critical_verification"]
            else 1.0 - float(self.config["verification_reserve_fraction"])
        )
        return {
            "project": project_id, "current_state": action["state"], "current_skill": action["skill"],
            "current_target": action.get("target_ref") or action.get("target_output_id"),
            "approved_budget_percent": policy["approved_budget_percent"],
            "estimated_used_percent": policy["used_percent"],
            "estimated_remaining_percent": max(0.0, available),
            "why_stopped": "next required action would exceed the spendable approved budget or verification reserve",
            "required_quality_floor": route["quality_floor"], "required_model": route["model_alias"],
            "minimum_additional_percent": round(shortage / reserve_factor, 2),
            "recommended_additional_percent": round(max(shortage / reserve_factor, estimate / reserve_factor), 2),
            "action_id": action["action_id"],
        }


class BudgetApprovalGate:
    """Validate explicit human approval; never infer it from session text."""

    @staticmethod
    def require_percentage(percent: float, current: float = 0.0) -> None:
        if not (0 < float(percent) <= 100) or current + float(percent) > 100:
            raise RuntimeValidationError(
                "budget percentage must be greater than 0 and total at most 100"
            )


class BudgetStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.config = yaml.safe_load(
            (resource_path(self.root, "config/model-routing.v0.1.yaml")).read_text(encoding="utf-8")
        )
        routing_schema = json.loads(
            (resource_path(self.root, "schemas/runtime/v0.1/model-routing.schema.json")).read_text(encoding="utf-8")
        )
        routing_errors = list(Draft202012Validator(routing_schema).iter_errors(self.config))
        if routing_errors:
            raise RuntimeValidationError(
                "model routing configuration is invalid",
                details=[error.message for error in routing_errors],
            )
        schema = json.loads(
            (resource_path(self.root, "schemas/runtime/v0.1/budget.schema.json")).read_text(encoding="utf-8")
        )
        self.validator = Draft202012Validator(schema)
        self.router = ModelRouter(self.config)
        self.guard = BudgetGuard(self.config)
        self.ledger = CostLedger()
        self.approval_gate = BudgetApprovalGate()

    def path_for(self, project_id: str) -> Path:
        return self.root / "projects" / project_id / "orchestrator" / "budget.yaml"

    def load(self, project_id: str, *, required: bool = False) -> dict[str, Any] | None:
        path = self.path_for(project_id)
        if not path.is_file():
            if required:
                raise BudgetRequired({"project_id": project_id})
            return None
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        errors = list(self.validator.iter_errors(value))
        if errors:
            raise RuntimeValidationError(
                "budget policy is invalid",
                details=[{"path": ".".join(map(str, e.absolute_path)), "message": e.message} for e in errors],
            )
        return value

    def approve_initial(self, project_id: str, run_id: str, percent: float) -> dict[str, Any]:
        self.approval_gate.require_percentage(percent)
        if self.load(project_id) is not None:
            return self.load(project_id, required=True)  # idempotent
        reserve = round(percent * float(self.config["verification_reserve_fraction"]), 4)
        now = utc_now()
        decision = self._decision(project_id, run_id, percent, initial=True)
        value = {
            "budget_version": "research-budget/v0.1", "project_id": project_id,
            "run_id": run_id, "allowance_basis": "USER_RELATIVE_PERCENT",
            "approved_budget_percent": float(percent), "used_percent": 0.0,
            "reserved_percent": 0.0, "estimated_remaining_percent": float(percent),
            "verification_reserve_percent": reserve,
            "verification_reserve_remaining_percent": reserve, "status": "ACTIVE",
            "approvals": [{"decision_ref": self._ref(decision), "additional_percent": float(percent), "approved_at": now}],
            "entries": [], "pending_request": None, "warnings": [],
            "created_at": now, "updated_at": now,
        }
        self._commit_budget_and_decision(value, decision, "approve-initial-budget")
        return value

    def reserve(self, project_id: str, action: Mapping[str, Any]) -> dict[str, Any]:
        value = self.load(project_id, required=True)
        assert value is not None
        action_id = str(action["action_id"])
        existing = next((item for item in value["entries"] if item["action_id"] == action_id), None)
        if existing is not None:
            if existing["status"] in {"RESERVED", "CONSUMED"}:
                return copy.deepcopy(existing)
        route = self.router.route(action)
        estimate = float(route["estimated_cost_percent"])
        request = self.guard.request_if_needed(project_id, value, action, route)
        if request is not None:
            value["status"] = "WAITING_HUMAN_BUDGET"
            value["pending_request"] = request
            value["updated_at"] = utc_now()
            self._write_and_commit(value, "budget-exhausted")
            raise BudgetApprovalRequired(request)
        now = utc_now()
        entry = {
            "action_id": action_id, "state": action["state"], "stage": str(action["state"]).split("_")[0],
            "skill": action["skill"], **route, "estimate_percent": estimate,
            "charged_percent": 0.0, "actual_usage": None, "status": "RESERVED",
            "created_at": now, "updated_at": now,
        }
        value["entries"].append(entry)
        value["reserved_percent"] = round(float(value["reserved_percent"]) + estimate, 6)
        value["estimated_remaining_percent"] = round(
            float(value["approved_budget_percent"]) - float(value["used_percent"])
            - float(value["reserved_percent"]), 6
        )
        value["updated_at"] = now
        self._write_and_commit(value, f"reserve-budget-{action_id}")
        return copy.deepcopy(entry)

    def consume(self, project_id: str, action_id: str, usage: Mapping[str, Any] | None = None) -> dict[str, Any]:
        value = self.load(project_id, required=True)
        assert value is not None
        entry = next((item for item in value["entries"] if item["action_id"] == action_id), None)
        if entry is None:
            raise RuntimeValidationError(f"no budget reservation for {action_id}")
        if entry["status"] == "CONSUMED":
            return copy.deepcopy(entry)
        charge = float(entry["estimate_percent"])
        entry.update(status="CONSUMED", charged_percent=charge, actual_usage=dict(usage) if usage else None, updated_at=utc_now())
        value["reserved_percent"] = round(max(0.0, float(value["reserved_percent"]) - charge), 6)
        value["used_percent"] = round(float(value["used_percent"]) + charge, 6)
        if entry["critical_verification"]:
            value["verification_reserve_remaining_percent"] = round(max(0.0, float(value["verification_reserve_remaining_percent"]) - charge), 6)
        value["estimated_remaining_percent"] = round(max(0.0, float(value["approved_budget_percent"]) - float(value["used_percent"]) - float(value["reserved_percent"])), 6)
        value["updated_at"] = utc_now()
        self._write_and_commit(value, f"consume-budget-{action_id}")
        return copy.deepcopy(entry)

    def increase(self, project_id: str, additional_percent: float, *, actor_id: str = "researcher") -> dict[str, Any]:
        value = self.load(project_id, required=True)
        assert value is not None
        self.approval_gate.require_percentage(
            additional_percent, float(value["approved_budget_percent"])
        )
        decision = self._decision(project_id, value["run_id"], additional_percent, initial=False, actor_id=actor_id)
        value["approved_budget_percent"] = round(float(value["approved_budget_percent"]) + additional_percent, 6)
        reserve_add = round(additional_percent * float(self.config["verification_reserve_fraction"]), 6)
        value["verification_reserve_percent"] = round(float(value["verification_reserve_percent"]) + reserve_add, 6)
        value["verification_reserve_remaining_percent"] = round(float(value["verification_reserve_remaining_percent"]) + reserve_add, 6)
        value["approvals"].append({"decision_ref": self._ref(decision), "additional_percent": additional_percent, "approved_at": utc_now()})
        value["status"] = "ACTIVE"; value["pending_request"] = None
        value["estimated_remaining_percent"] = round(float(value["approved_budget_percent"]) - float(value["used_percent"]) - float(value["reserved_percent"]), 6)
        value["updated_at"] = utc_now()
        self._commit_budget_and_decision(value, decision, "approve-budget-increase")
        return value

    def status(self, project_id: str) -> dict[str, Any]:
        value = self.load(project_id)
        if value is None:
            return {
                "approved": False, "status": "BUDGET_REQUIRED",
                "prompt_options_percent": [5, 10, 20, 30, "CUSTOM"],
            }
        grouped = self.ledger.group(value["entries"])
        current = next((item for item in reversed(value["entries"]) if item["status"] == "RESERVED"), None)
        pending = value["pending_request"]
        return {
            "approved": True, "status": value["status"],
            "approved_run_budget_percent": value["approved_budget_percent"],
            "estimated_used_percent": value["used_percent"],
            "estimated_remaining_percent": value["estimated_remaining_percent"],
            "verification_reserve_percent": value["verification_reserve_remaining_percent"],
            "current_model": None if current is None else current["model_alias"],
            "reasoning_effort": None if current is None else current["reasoning_effort"],
            "cost_by_skill_model_stage": grouped,
            "pending_estimated_cost": None if pending is None else pending["minimum_additional_percent"],
            "budget_warnings": value["warnings"], "pending_request": pending,
            "available_actions": [] if pending is None else [
                "APPROVE_MINIMUM_INCREASE", "APPROVE_RECOMMENDED_INCREASE",
                "SET_CUSTOM_ADDITIONAL_PERCENT", "KEEP_PAUSED",
                "REVISE_RESEARCH_PLAN", "CANCEL",
            ],
        }

    def _decision(self, project_id: str, run_id: str, percent: float, *, initial: bool, actor_id: str = "researcher") -> dict[str, Any]:
        now = utc_now(); slug = project_id.removeprefix("proj-")
        digest = hashlib.sha256(f"{run_id}:{now}:{percent}:{initial}".encode()).hexdigest()[:10]
        decision_id = f"decision-{slug}-budget-{digest}"
        actor = {"actor_type": "human", "actor_id": actor_id}
        return {
            "schema_version": "research-artifact/v0.1.2", "kind": "Decision", "id": decision_id,
            "project_id": project_id, "title": "Approve research run budget", "status": "ACCEPTED", "revision": 1,
            "created_at": now, "updated_at": now, "provenance": {"created_by": actor, "updated_by": actor}, "tags": ["budget"],
            "decision": f"Approve {'initial' if initial else 'additional'} research budget of {percent}% of current Codex allowance.",
            "context": f"Cost policy for research run {run_id}.",
            "reason": "User explicitly selected a relative Codex allowance budget.",
            "alternatives_considered": [{"option": "Do not start or remain paused", "rejected_because": "The user authorized this budget."}],
            "evidence_refs": [], "impact": [{"affected_ref": {"id": project_id, "kind": "Project"}, "description": "Authorizes bounded model execution without changing scientific conclusions."}],
            "approved_by": [actor], "effective_at": now,
        }

    @staticmethod
    def _ref(decision: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": decision["id"], "kind": "Decision", "revision": 1}

    def _decision_path(self, decision: Mapping[str, Any]) -> Path:
        return self.root / "projects" / str(decision["project_id"]) / "decisions" / f"{decision['id']}.yaml"

    def _commit_budget_and_decision(self, value: Mapping[str, Any], decision: Mapping[str, Any], action: str) -> None:
        decision_path = self._decision_path(decision)
        atomic_write_text(decision_path, yaml.safe_dump(dict(decision), sort_keys=False, allow_unicode=True))
        self._write(value)
        self._git_commit([self.path_for(str(value["project_id"])), decision_path], str(value["project_id"]), action)

    def _write_and_commit(self, value: Mapping[str, Any], action: str) -> None:
        self._write(value)
        self._git_commit([self.path_for(str(value["project_id"]))], str(value["project_id"]), action)

    def _write(self, value: Mapping[str, Any]) -> None:
        errors = list(self.validator.iter_errors(value))
        if errors:
            raise RuntimeValidationError("budget policy is invalid", details=[e.message for e in errors])
        atomic_write_text(self.path_for(str(value["project_id"])), yaml.safe_dump(dict(value), sort_keys=False, allow_unicode=True))

    def _git_commit(self, paths: list[Path], project_id: str, action: str) -> None:
        rels = [str(path.relative_to(self.root)) for path in paths]
        subprocess.run(["git", "add", "--", *rels], cwd=self.root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", f"research({project_id}): {action}", "--", *rels], cwd=self.root, check=True, capture_output=True, text=True)
