"""Read-only environment diagnostics; never dispatch a research action."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from .action_compiler import ActionCompiler
from .cost_control import BudgetStore
from .operator_client import CordisClient, operator_socket


def diagnose(workspace: str | Path, *, runtime: str = "synthetic",
             socket_path: str | None = None, timeout: float = 5) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    checks: list[dict[str, Any]] = []

    def check(name: str, operation: Callable[[], str]) -> None:
        try:
            checks.append({"name": name, "ok": True, "detail": operation()})
        except Exception as exc:
            checks.append({"name": name, "ok": False, "detail": str(exc)})

    def python() -> str:
        if sys.version_info < (3, 11):
            raise ValueError("Python 3.11+ is required")
        return f"{sys.executable} (Python {sys.version.split()[0]})"

    def git() -> str:
        if not root.is_dir():
            raise ValueError("workspace does not exist; run workspace-init first")
        def run(*args: str) -> str:
            result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                    text=True, timeout=5)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "Git check failed")
            return result.stdout.strip()
        if Path(run("rev-parse", "--show-toplevel")).resolve() != root:
            raise ValueError("workspace must be its own Git repository, not a directory inside another repo")
        head = run("rev-parse", "--verify", "HEAD")
        run("var", "GIT_AUTHOR_IDENT")
        run("var", "GIT_COMMITTER_IDENT")
        return f"independent Git workspace with commit identity; HEAD={head}"

    def contracts() -> str:
        compiler = ActionCompiler(root)
        contracts = list(compiler.skills_root.glob("*/skill.yaml"))
        if not contracts:
            raise ValueError("no packaged skill contracts found")
        BudgetStore(root)
        return f"schema validators and routing configuration loaded; {len(contracts)} skill contracts available"

    check("python", python)
    check("workspace_git", git)
    check("framework_resources", contracts)
    if runtime == "cordis":
        def host() -> str:
            client = CordisClient(root, socket_path=socket_path, timeout=timeout)
            client.handshake()
            return f"compatible host at {client.socket_path}; model authentication and inference not tested"
        check("cordis_operator", host)
    elif runtime == "synthetic":
        def synthetic() -> str:
            if operator_socket(root, socket_path).exists():
                raise ValueError("Cordis socket path exists; select --runtime cordis or stop the host first")
            return "synthetic test runtime; no model-backed scientific execution"
        check("runtime", synthetic)
    else:
        checks.append({"name": "runtime", "ok": False, "detail": f"unknown runtime: {runtime}"})
    return {"ok": all(item["ok"] for item in checks), "runtime": runtime,
            "workspace": str(root), "checks": checks}
