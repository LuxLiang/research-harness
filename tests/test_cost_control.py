from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from research_artifacts.cost_control import BudgetApprovalRequired, BudgetStore, ModelRouter
from research_artifacts.mvp import MVPController, SyntheticSkillRuntime


ROOT = Path(__file__).resolve().parents[1]


class CountingRuntime(SyntheticSkillRuntime):
    def __init__(self, root: Path):
        super().__init__(root)
        self.calls: list[str] = []

    def execute(self, action, bundle):
        self.calls.append(action["action_id"])
        return super().execute(action, bundle)


class CostControlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        shutil.copytree(ROOT / "config", self.root / "config")
        skills = self.root / "integrations/deepseek-harness/skills"
        skills.parent.mkdir(parents=True)
        shutil.copytree(ROOT / "integrations/deepseek-harness/skills", skills)
        (self.root / "projects").mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Cost Guard Test")
        self._git("config", "user.email", "cost@example.invalid")
        self._git("add", "schemas", "config", "integrations", "projects")
        self._git("commit", "-m", "cost fixture baseline")
        self.runtime = CountingRuntime(self.root)
        self.controller = MVPController(self.root, self.runtime)
        self.controller.init("proj-cost", "Cost-aware research")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *args: str):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)

    @staticmethod
    def _action(skill: str, action_id: str = "action-1", **factors):
        return {
            "action_id": action_id, "state": "EXECUTION_TRACKS", "skill": skill,
            "target_output_id": "core", "target_ref": {"id": "claim-cost-core"},
            "model_routing": {
                "scientific_risk": factors.get("risk", "MEDIUM"),
                "difficulty": factors.get("difficulty", "MEDIUM"),
                "uncertainty": factors.get("uncertainty", "MEDIUM"),
            },
            "attempt": factors.get("attempt", 0),
        }

    def test_new_run_requires_explicit_budget(self) -> None:
        stop = self.controller.run("proj-cost", max_actions=1)
        self.assertEqual(stop["reason"], "WAITING_HUMAN:BUDGET")
        self.assertEqual(self.runtime.calls, [])
        self.assertFalse(stop["budget"]["approved"])

    def test_configurable_tiers_escalate_only_upward_and_preserve_floor(self) -> None:
        store = BudgetStore(self.root)
        economy = store.router.route(self._action("experiment-execution"))
        terra = store.router.route(self._action("experiment-execution", risk="HIGH"))
        sol = store.router.route(self._action("experiment-execution", risk="HIGH", difficulty="HIGH"))
        self.assertEqual([economy["model_tier"], terra["model_tier"], sol["model_tier"]], ["economy", "balanced", "frontier"])
        self.assertEqual(economy["model_alias"], store.config["tier_aliases"]["economy"])
        critical = store.router.route(self._action("theory-verification"))
        self.assertEqual(critical["model_tier"], "frontier")
        self.assertEqual(critical["quality_floor"], "frontier")

    def test_budget_exhaustion_waits_then_resumes_exact_pending_action(self) -> None:
        first = self.controller.run("proj-cost", max_actions=10, budget_percent=1.0)
        self.assertEqual(first["reason"], "WAITING_HUMAN:BUDGET")
        self.assertEqual(len(self.runtime.calls), 1)
        pending = first["checkpoint"]["pending_action"]["action_id"]
        request = first["request"]
        self.assertEqual(request["action_id"], pending)
        self.assertIn(request["required_quality_floor"], {"economy", "balanced", "frontier"})
        self.assertGreater(request["minimum_additional_percent"], 0)
        decision_count = len(list((self.root / "projects/proj-cost/decisions").glob("decision-*-budget-*.yaml")))
        self.controller.approve_budget("proj-cost", request["recommended_additional_percent"])
        second = self.controller.run("proj-cost", max_actions=1)
        self.assertNotEqual(second["reason"], "WAITING_HUMAN:BUDGET")
        self.assertEqual(self.runtime.calls.count(pending), 1)
        self.assertEqual(len(list((self.root / "projects/proj-cost/decisions").glob("decision-*-budget-*.yaml"))), decision_count + 1)

    def test_verification_reserve_blocks_routine_but_allows_critical(self) -> None:
        checkpoint = self.controller.transactions.orchestrator.store.load("proj-cost")
        store = self.controller.budgets
        store.approve_initial("proj-cost", checkpoint["run_id"], 1.0)
        store.reserve("proj-cost", self._action("question-framing", "routine-1"))
        store.consume("proj-cost", "routine-1")
        with self.assertRaises(BudgetApprovalRequired):
            store.reserve("proj-cost", self._action("experiment-execution", "routine-2"))
        store.increase("proj-cost", 1.0)
        critical = store.reserve("proj-cost", self._action("theory-verification", "critical-1"))
        self.assertTrue(critical["critical_verification"])

    def test_feasibility_gate_uses_verification_reserve_even_with_reused_literature_skill(self) -> None:
        action = self._action("literature-novelty", "feasibility-gate-1")
        action["state"] = "DISCOVERY_FEASIBILITY_GATE"
        routed = ModelRouter(self.controller.budgets.config).route(action)
        self.assertTrue(routed["critical_verification"])

    def test_recorded_feasibility_rethink_makes_corrective_action_critical(self) -> None:
        action = self._action("question-framing", "corrective-question-1")
        action["model_routing"]["reason"] = (
            "feasibility re-evaluation after a recorded RETHINK_QUESTION"
        )
        routed = ModelRouter(self.controller.budgets.config).route(action)
        self.assertTrue(routed["critical_verification"])


if __name__ == "__main__":
    unittest.main()
