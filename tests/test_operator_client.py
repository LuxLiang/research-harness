"""Public CLI routing and real local-socket transport, without model calls."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from research_artifacts import initialize_workspace
from research_artifacts.doctor import diagnose
from research_artifacts.mvp_cli import main, parser
from research_artifacts.operator_client import CordisClient, OperatorError, PROTOCOL


class LocalOperator:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "operator.sock"
        self.calls = []
        self.health = {"protocol": PROTOCOL, "workspace": str(root), "runtime": "cordis"}
        self.error = None
        self.wrong_id = False
        self.done = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.done.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(2)
                with connection.makefile("rb") as reader:
                    request = json.loads(reader.readline())
                self.calls.append(request)
                result = self.health if request["method"] == "health" else {
                    "checkpoint": {"state": "DISCOVERY_QUESTION"},
                    "budget": {"pending_request": {"minimum_additional_percent": 2.5}},
                }
                response = {"id": "wrong" if self.wrong_id else request["id"],
                            "ok": self.error is None, "result": result, "error": self.error}
                raw = json.dumps(response).encode() + b"\n"
                connection.sendall(raw[:8])
                connection.sendall(raw[8:])

    def close(self):
        self.done.set()
        self.thread.join(3)
        self.listener.close()
        self.path.unlink()


class OperatorClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rh-")
        self.root = Path(self.temporary.name)
        self.host = LocalOperator(self.root)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.host.close)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["--workspace", str(self.root), "--socket", str(self.host.path), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_status_handshake_and_response_across_socket_chunks(self):
        code, output, error = self.invoke("status", "example", "--runtime", "cordis")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["checkpoint"]["state"], "DISCOVERY_QUESTION")
        self.assertEqual([r["method"] for r in self.host.calls], ["health", "status"])

    def test_run_never_constructs_synthetic_controller(self):
        with patch("research_artifacts.mvp_cli.MVPController", side_effect=AssertionError("synthetic fallback")):
            code, _, error = self.invoke("--runtime", "cordis", "run", "example",
                                         "--budget-percent", "10", "--max-actions", "2")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.host.calls[-1]["params"], {
            "project_id": "proj-example", "budget_percent": 10.0, "max_actions": 2,
        })

    def test_wrong_workspace_prevents_mutation(self):
        self.host.health["workspace"] = str(self.root / "other")
        code, _, error = self.invoke("--runtime", "cordis", "run", "example")
        self.assertEqual(code, 2)
        self.assertIn("different workspace", error)
        self.assertEqual([r["method"] for r in self.host.calls], ["health"])

    def test_incompatible_protocol_prevents_mutation(self):
        self.host.health["protocol"] = "unknown/v9"
        code, _, error = self.invoke("--runtime", "cordis", "init", "example")
        self.assertEqual(code, 2)
        self.assertIn("rebuild and restart", error)
        self.assertEqual(len(self.host.calls), 1)

    def test_host_error_and_mismatched_response_are_not_accepted(self):
        client = CordisClient(self.root, socket_path=str(self.host.path))
        self.host.error = "model provider unavailable"
        with self.assertRaisesRegex(OperatorError, "model provider unavailable"):
            client.call("run", {"project_id": "proj-example"})
        self.host.error = None
        self.host.wrong_id = True
        with self.assertRaisesRegex(OperatorError, "does not match"):
            client.handshake()

    def test_budget_and_resume_preserve_existing_cli_continuation(self):
        code, _, error = self.invoke("--runtime", "cordis", "budget", "example", "--minimum")
        self.assertEqual(code, 0, error)
        self.assertEqual([r["method"] for r in self.host.calls], ["health", "status", "budget", "run"])
        self.assertEqual(self.host.calls[-2]["params"]["additional_percent"], 2.5)
        self.host.calls.clear()
        code, _, error = self.invoke("--runtime", "cordis", "resume", "example")
        self.assertEqual(code, 0, error)
        self.assertEqual([r["method"] for r in self.host.calls], ["health", "resume", "run"])

    def test_input_paths_are_absolute_before_host_dispatch(self):
        code, _, error = self.invoke("--runtime", "cordis", "init", "example",
                                     "--source-material", "input.md")
        self.assertEqual(code, 0, error)
        self.assertEqual(self.host.calls[-1]["params"]["options"]["sourceMaterials"],
                         [str(Path("input.md").resolve())])

    def test_synthetic_mutation_refuses_existing_host_socket(self):
        code, _, error = self.invoke("run", "example", "--budget-percent", "10")
        self.assertEqual(code, 2)
        self.assertIn("use --runtime cordis", error)
        self.assertEqual(self.host.calls, [])

    def test_doctor_live_host_is_read_only_and_does_not_claim_model_readiness(self):
        report = diagnose(self.root, runtime="cordis", socket_path=str(self.host.path))
        connection = report["checks"][-1]
        self.assertTrue(connection["ok"])
        self.assertIn("inference not tested", connection["detail"])
        self.assertEqual([r["method"] for r in self.host.calls], ["health"])


class TransportFailureTests(unittest.TestCase):
    def test_timeout_after_send_reports_unknown_outcome_without_retry(self):
        connection = MagicMock()
        connection.recv.side_effect = TimeoutError("deadline")
        with patch("research_artifacts.operator_client.socket.socket") as factory:
            factory.return_value.__enter__.return_value = connection
            with self.assertRaisesRegex(OperatorError, "outcome is unknown"):
                CordisClient("/tmp").call("run", {"project_id": "proj-example"})
            self.assertEqual(factory.call_count, 1)
            connection.sendall.assert_called_once()

    def test_response_size_limit_is_enforced(self):
        connection = MagicMock()
        connection.recv.return_value = b"x" * 16
        with patch("research_artifacts.operator_client.socket.socket") as factory, \
             patch("research_artifacts.operator_client.MAX_RESPONSE_BYTES", 8):
            factory.return_value.__enter__.return_value = connection
            with self.assertRaisesRegex(OperatorError, "response exceeds"):
                CordisClient("/tmp").call("status")

    def test_missing_host_does_not_invoke_synthetic_runtime(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("research_artifacts.mvp_cli.MVPController", side_effect=AssertionError("fallback")), \
             redirect_stderr(io.StringIO()) as error:
            code = main(["--workspace", temporary, "--runtime", "cordis", "run", "example"])
        self.assertEqual(code, 2)
        self.assertIn("Cordis operator unavailable", error.getvalue())


class DoctorTests(unittest.TestCase):
    def test_noninteractive_synthetic_run_waits_for_budget_without_reading_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = initialize_workspace(Path(temporary) / "data")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--workspace", str(root), "init", "example"]), 0)
            output, error = io.StringIO(), io.StringIO()
            with patch("sys.stdin", io.StringIO()), redirect_stdout(output), redirect_stderr(error):
                code = main(["--workspace", str(root), "run", "example", "--max-actions", "1"])
            self.assertEqual(code, 0, error.getvalue())
            self.assertEqual(json.loads(output.getvalue())["reason"], "WAITING_HUMAN:BUDGET")
            self.assertIn("runtime=synthetic", error.getvalue())

    def test_missing_workspace_is_reported_without_creating_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "missing"
            report = diagnose(root)
            self.assertFalse(report["ok"])
            self.assertFalse(root.exists())

    def test_standalone_workspace_passes_and_diagnostics_are_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = initialize_workspace(Path(temporary) / "data")
            before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            self.assertTrue(diagnose(root)["ok"])
            after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            self.assertEqual(before, after)
            report = diagnose(root, runtime="cordis")
            self.assertFalse(report["ok"])
            self.assertIn("Start the configured dsh host", report["checks"][-1]["detail"])

    def test_nested_directory_is_not_an_independent_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = initialize_workspace(Path(temporary) / "data")
            child = root / "nested"
            child.mkdir()
            report = diagnose(child)
            self.assertFalse(report["ok"])
            self.assertIn("own Git repository", report["checks"][1]["detail"])

    def test_invalid_cli_limits_are_rejected(self):
        for args in (("--timeout", "nan"), ("--budget-percent", "inf"),
                     ("--budget-percent", "101"), ("--max-actions", "0")):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["run", "example", *args])

    def test_socket_cannot_target_another_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(OperatorError, "inside"):
                CordisClient(Path(temporary) / "data", socket_path=str(Path(temporary) / "wrong.sock"))


if __name__ == "__main__":
    unittest.main()
