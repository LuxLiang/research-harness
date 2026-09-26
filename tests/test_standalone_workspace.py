"""Regression coverage for installed-library use outside the checkout."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from research_artifacts import (
    ArtifactError, ArtifactWorkspace, MVPController, SyntheticSkillRuntime,
    initialize_workspace,
)
from research_artifacts.cost_control import BudgetStore
from research_artifacts.resources import resource_path
from research_artifacts.runtime_types import RuntimeValidationError
from research_evals.runner import ScientificEvalRunner


class StandaloneWorkspaceTests(unittest.TestCase):
    def test_external_workspace_can_initialize_compile_and_validate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = initialize_workspace(Path(temporary) / "workspace")
            self.assertFalse((root / "schemas").exists())
            self.assertFalse((root / "integrations").exists())
            controller = MVPController(root, SyntheticSkillRuntime(root))
            controller.init("example", "Exercise framework contracts")
            checkpoint = controller.status("proj-example")
            self.assertEqual(checkpoint["state"], "DISCOVERY_QUESTION")
            action = controller.compiler.preview("proj-example")
            self.assertEqual(action["skill"], "question-framing")
            self.assertEqual(ArtifactWorkspace(root).validate(), [])
            status = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True,
            )
            self.assertEqual(status, "")

    def test_nonempty_workspace_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "important.txt"
            original.write_text("keep me")
            with self.assertRaises(ArtifactError):
                initialize_workspace(root)
            self.assertEqual(original.read_text(), "keep me")
            self.assertFalse((root / ".git").exists())

    def test_workspace_configuration_overrides_packaged_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = initialize_workspace(Path(temporary) / "workspace")
            path = root / "config/model-routing.v0.1.yaml"
            self.assertEqual(resource_path(root, "config/model-routing.v0.1.yaml"), path)
            path.write_text("invalid: configuration\n")
            with self.assertRaises(RuntimeValidationError) as error:
                BudgetStore(root)
            self.assertIn("model routing configuration is invalid", str(error.exception))

    def test_default_evals_work_without_checkout_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = ScientificEvalRunner(temporary).run()
            self.assertGreater(len(result.results), 0)
            self.assertTrue(result.passed)

    def test_cli_workspace_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "workspace"
            result = subprocess.run(
                [sys.executable, "-m", "research_artifacts.mvp_cli",
                 "--workspace", str(root), "workspace-init"],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(result.stdout)["workspace"], str(root))


if __name__ == "__main__":
    unittest.main()
