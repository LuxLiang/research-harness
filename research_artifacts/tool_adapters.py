"""Deterministic scientific tool adapters used inside bounded Skill actions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .runtime_types import RuntimeValidationError, atomic_write_json, sha256_json


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class LeanResult:
    outcome: str
    build_passed: bool
    axiom_audit_passed: bool
    findings: tuple[str, ...]
    report_path: str
    report_sha256: str


class LeanToolAdapter:
    """Run pinned Lean checks; an agent's prose can never manufacture PASS."""

    def __init__(self, runner: Runner = subprocess.run):
        self.runner = runner

    def verify(
        self,
        *,
        project_dir: str | Path,
        source_paths: Sequence[str | Path],
        lean_version: str,
        mathlib_commit: str,
        allowed_axioms: Sequence[str] = ("propext", "Classical.choice", "Quot.sound"),
        report_path: str | Path,
        build_command: Sequence[str] = ("lake", "build"),
        stronger_checker: Sequence[str] | None = None,
        timeout: int = 300,
    ) -> LeanResult:
        root = Path(project_dir).resolve()
        sources = [(root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve() for path in source_paths]
        for source in sources:
            if root not in source.parents and source != root:
                raise RuntimeValidationError("Lean source escapes the pinned project directory")
            if not source.is_file():
                raise RuntimeValidationError(f"Lean source not found: {source}")
        findings: list[str] = []
        declared_axioms: set[str] = set()
        source_hashes: dict[str, str] = {}
        for source in sources:
            content = source.read_text(encoding="utf-8")
            source_hashes[str(source.relative_to(root))] = hashlib.sha256(content.encode()).hexdigest()
            if re.search(r"\bsorryAx\b", content):
                findings.append("SORRY_AX_FOUND")
            if re.search(r"\bsorry\b", content):
                findings.append("SORRY_FOUND")
            declared_axioms.update(
                match.group(1)
                for match in re.finditer(r"(?m)^\s*(?:axiom|constant)\s+([A-Za-z0-9_'.]+)", content)
            )
        unapproved = sorted(declared_axioms - set(allowed_axioms))
        findings.extend(f"UNAPPROVED_AXIOM:{item}" for item in unapproved)
        build_failure: str | None = None
        try:
            build = self.runner(
                list(build_command), cwd=root, text=True, capture_output=True,
                check=False, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            build_failure = "TOOL_TIMEOUT"
            build = subprocess.CompletedProcess(
                list(build_command), 124,
                stdout=str(exc.stdout or ""), stderr=str(exc.stderr or "Lean build timed out"),
            )
        except OSError as exc:
            build_failure = "TOOL_UNAVAILABLE"
            build = subprocess.CompletedProcess(
                list(build_command), 127, stdout="", stderr=str(exc),
            )
        build_passed = build.returncode == 0
        if not build_passed:
            findings.append(build_failure or "LEAN_BUILD_FAILED")
        combined_output = f"{build.stdout}\n{build.stderr}"
        reported_axioms: set[str] = set()
        for match in re.finditer(
            r"depends on axioms:\s*\[([^\]]*)\]", combined_output
        ):
            reported_axioms.update(
                item.strip()
                for item in match.group(1).split(",")
                if item.strip()
            )
        unapproved_reported = sorted(reported_axioms - set(allowed_axioms))
        findings.extend(
            f"UNAPPROVED_AXIOM:{item}" for item in unapproved_reported
        )
        stronger = None
        if stronger_checker and build_passed and not findings:
            stronger = self.runner(
                list(stronger_checker), cwd=root, text=True, capture_output=True,
                check=False, timeout=timeout,
            )
            if stronger.returncode != 0:
                findings.append("STRONGER_CHECK_FAILED")
        audit_passed = not any(
            item.startswith(("SORRY", "UNAPPROVED_AXIOM")) for item in findings
        )
        if any(item.startswith("SORRY") for item in findings):
            outcome = "SORRY_FOUND"
        elif unapproved or unapproved_reported:
            outcome = "UNAPPROVED_AXIOM"
        elif build_failure is not None:
            outcome = build_failure
        elif not build_passed:
            combined = f"{build.stdout}\n{build.stderr}".lower()
            if any(word in combined for word in ("unknown module", "package", "dependency", "no such file")):
                outcome = "LIBRARY_GAP"
            elif any(word in combined for word in ("unexpected token", "parser", "invalid syntax")):
                outcome = "FORMALIZATION_GAP"
            else:
                outcome = "LEAN_PROOF_GAP"
        elif stronger is not None and stronger.returncode != 0:
            outcome = "LEAN_PROOF_GAP"
        else:
            outcome = "PASS"
        report = {
            "adapter_version": "research-lean-tool/v0.1",
            "outcome": outcome,
            "lean_version": lean_version,
            "mathlib_commit": mathlib_commit,
            "source_hashes": source_hashes,
            "build_command": list(build_command),
            "build_returncode": build.returncode,
            "build_stdout": build.stdout,
            "build_stderr": build.stderr,
            "declared_axioms": sorted(declared_axioms),
            "reported_axioms": sorted(reported_axioms),
            "allowed_axioms": sorted(set(allowed_axioms)),
            "findings": findings,
            "stronger_checker": None if stronger is None else {
                "command": list(stronger_checker or ()), "returncode": stronger.returncode,
                "stdout": stronger.stdout, "stderr": stronger.stderr,
            },
        }
        output = Path(report_path)
        atomic_write_json(output, report)
        digest = hashlib.sha256(output.read_bytes()).hexdigest()
        return LeanResult(outcome, build_passed, audit_passed, tuple(findings), str(output), digest)


class ExperimentExecutionAdapter:
    """Execute one frozen Experiment run without interpreting its result."""

    def __init__(
        self,
        runner: Runner = subprocess.run,
        *,
        workspace_root: str | Path | None = None,
    ):
        self.runner = runner
        self.workspace_root = (
            None if workspace_root is None else Path(workspace_root).resolve()
        )

    @staticmethod
    def protocol_hash(experiment: Mapping[str, Any]) -> str:
        fields = {
            key: experiment.get(key)
            for key in (
                "hypothesis", "method", "baselines", "datasets", "metrics",
                "configuration", "interpretation_plan", "code_location",
            )
        }
        return sha256_json(fields)

    def execute(
        self,
        *,
        experiment: Mapping[str, Any],
        seed: int,
        repetition: int,
        run_root: str | Path,
        timeout: int = 3600,
        extra_env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        lock = experiment.get("protocol_lock")
        if not isinstance(lock, Mapping):
            raise RuntimeValidationError("Experiment is not protocol-locked")
        actual_hash = self.protocol_hash(experiment)
        if lock.get("sha256") != actual_hash:
            raise RuntimeValidationError("PROTOCOL_DEVIATION: locked scientific fields changed")
        run_id = "run-" + sha256_json({
            "experiment": experiment.get("id"), "revision": experiment.get("revision"),
            "protocol": actual_hash, "seed": seed, "repetition": repetition,
        })[:16]
        for existing in experiment.get("runs", []):
            if existing.get("run_id") == run_id:
                return {"run": existing, "reused": True}
        code = experiment.get("code_location", {})
        entrypoint = code.get("entrypoint")
        commit = code.get("git_commit")
        if not isinstance(entrypoint, str) or not entrypoint or not isinstance(commit, str):
            raise RuntimeValidationError("Experiment lacks a pinned executable code location")
        output_dir = Path(run_root).resolve() / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        run_state = output_dir / "run-state.json"
        if run_state.is_file():
            persisted = json.loads(run_state.read_text(encoding="utf-8"))
            if persisted.get("status") == "COMMITTED" and isinstance(persisted.get("run"), dict):
                return {"run": persisted["run"], "reused": True}
            if persisted.get("status") == "RUNNING":
                # The previous process crossed the durable dispatch boundary but
                # never committed an outcome.  Preserve that attempt as failed;
                # never execute the same stable run_id twice.
                raw_path = output_dir / "raw-output.json"
                if not raw_path.is_file():
                    atomic_write_json(raw_path, {
                        "run_id": run_id,
                        "returncode": None,
                        "stdout": "",
                        "stderr": "process interrupted before durable completion",
                    })
                run = self._run_record(
                    experiment=experiment,
                    run_id=run_id,
                    seed=seed,
                    actual_hash=actual_hash,
                    raw_path=raw_path,
                    returncode=None,
                    failure_reason="INTERRUPTED_RUN",
                )
                atomic_write_json(run_state, {"status": "COMMITTED", "run": run})
                return {"run": run, "reused": True}
        repository = self._repository(experiment)
        self._validate_datasets(experiment, repository=repository)
        # Keep the protocol-visible output directory empty at dispatch time.
        # The adapter's durable run journal must exist before dispatch, but it
        # is host metadata and must not make `{run_output_dir}` violate a
        # protocol's fresh-output precondition.
        payload_dir = output_dir / "payload"
        payload_dir.mkdir(parents=True, exist_ok=True)
        if any(payload_dir.iterdir()):
            raise RuntimeValidationError(
                "PROTOCOL_DEVIATION: run output payload directory is not empty"
            )
        command = self._entrypoint_command(
            entrypoint,
            repository=repository,
            experiment=experiment,
            output_dir=payload_dir,
        )
        env = os.environ.copy()
        env.update(extra_env or {})
        env["RESEARCH_RUN_ID"] = run_id
        env["RESEARCH_SEED"] = str(seed)
        atomic_write_json(run_state, {
            "status": "RUNNING", "run_id": run_id,
            "protocol_sha256": actual_hash, "seed": seed,
        })
        failure_reason: str | None = None
        try:
            completed = self.runner(
                command, cwd=output_dir, env=env, text=True,
                capture_output=True, check=False, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            failure_reason = "TIMEOUT"
            completed = subprocess.CompletedProcess(
                command, 124,
                stdout=str(exc.stdout or ""), stderr=str(exc.stderr or "experiment timed out"),
            )
        except OSError as exc:
            failure_reason = "TOOL_UNAVAILABLE"
            completed = subprocess.CompletedProcess(
                command, 127, stdout="", stderr=str(exc),
            )
        raw_path = output_dir / "raw-output.json"
        atomic_write_json(raw_path, {
            "run_id": run_id, "returncode": completed.returncode,
            "stdout": completed.stdout, "stderr": completed.stderr,
        })
        run = self._run_record(
            experiment=experiment,
            run_id=run_id,
            seed=seed,
            actual_hash=actual_hash,
            raw_path=raw_path,
            returncode=completed.returncode,
            failure_reason=(
                failure_reason
                or (f"entrypoint exited {completed.returncode}" if completed.returncode != 0 else None)
            ),
        )
        atomic_write_json(run_state, {"status": "COMMITTED", "run": run})
        return {"run": run, "reused": False}

    @staticmethod
    def _run_record(
        *, experiment: Mapping[str, Any], run_id: str, seed: int,
        actual_hash: str, raw_path: Path, returncode: int | None,
        failure_reason: str | None,
    ) -> dict[str, Any]:
        raw_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        dataset_hashes = [
            str(item["sha256"])
            for item in experiment.get("datasets", []) if item.get("sha256")
        ]
        commit = str(experiment.get("code_location", {}).get("git_commit", ""))
        lock = experiment.get("protocol_lock", {})
        environment = str(experiment.get("configuration", {}).get("environment", ""))
        run: dict[str, Any] = {
            "run_id": run_id,
            "status": "SUCCEEDED" if returncode == 0 else "FAILED",
            "seed": seed,
            "config_hash": sha256_json(experiment.get("configuration", {})),
            "protocol_revision": experiment["revision"],
            "protocol_sha256": actual_hash,
            "code_commit": commit,
            "dataset_hashes": dataset_hashes,
            "environment_hash": hashlib.sha256(environment.encode()).hexdigest(),
            "environment": environment,
            "started_at": lock["locked_at"],
            "finished_at": lock["locked_at"],
            "outputs": [{
                "uri": str(raw_path), "sha256": raw_hash,
                "media_type": "application/json", "description": "raw execution output",
            }],
            "metric_values": {},
            "deviations": [],
        }
        if failure_reason is not None:
            run["failure_reason"] = failure_reason
        return run

    def _repository(self, experiment: Mapping[str, Any]) -> Path:
        value = Path(str(experiment.get("code_location", {}).get("repository", ".")))
        if value.is_absolute():
            return value.resolve()
        if self.workspace_root is not None:
            return (self.workspace_root / value).resolve()
        return value.resolve()

    def _entrypoint_command(
        self,
        entrypoint: str,
        *,
        repository: Path,
        experiment: Mapping[str, Any],
        output_dir: Path,
    ) -> list[str]:
        command = shlex.split(entrypoint)
        if not command:
            raise RuntimeValidationError("Experiment entrypoint is empty")
        first = Path(command[0])
        if len(command) == 1 and not first.is_absolute():
            candidate = (repository / first).resolve()
            if candidate.is_file():
                command = (
                    [sys.executable, str(candidate)]
                    if candidate.suffix == ".py"
                    else [str(candidate)]
                )
        if len(command) >= 2 and Path(command[0]).name.startswith("python"):
            script = Path(command[1])
            if not script.is_absolute():
                candidate = (repository / script).resolve()
                if candidate.is_file():
                    command[1] = str(candidate)
        parameters = experiment.get("configuration", {}).get("parameters", {})
        execution_argv = (
            parameters.get("execution_argv", [])
            if isinstance(parameters, Mapping)
            else []
        )
        if not isinstance(execution_argv, list) or not all(
            isinstance(item, str) for item in execution_argv
        ):
            raise RuntimeValidationError(
                "Experiment configuration.parameters.execution_argv must be a list of strings"
            )
        allowed_placeholders = {
            "{run_output_dir}", "{repository}", "{workspace_root}"
        }
        embedded = [
            item for item in execution_argv
            if ("{" in item or "}" in item) and item not in allowed_placeholders
        ]
        if embedded:
            raise RuntimeValidationError(
                "execution_argv placeholders must be standalone tokens: "
                + ", ".join(repr(item) for item in embedded)
            )
        if execution_argv:
            first_arg = Path(execution_argv[0]).name.lower()
            entry_name = Path(entrypoint.split()[-1]).name.lower()
            if first_arg.startswith("python") or first_arg == entry_name:
                raise RuntimeValidationError(
                    "execution_argv must not repeat an interpreter or code_location.entrypoint"
                )
        substitutions = {
            "{run_output_dir}": str(output_dir),
            "{repository}": str(repository),
            "{workspace_root}": (
                "" if self.workspace_root is None else str(self.workspace_root)
            ),
        }
        command.extend(substitutions.get(item, item) for item in execution_argv)
        return command

    def _validate_datasets(
        self,
        experiment: Mapping[str, Any],
        *,
        repository: Path,
    ) -> None:
        for dataset in experiment.get("datasets", []):
            uri = dataset.get("uri")
            expected = dataset.get("sha256")
            if not isinstance(uri, str) or not isinstance(expected, str) or "://" in uri:
                continue
            path = Path(uri)
            if path.is_absolute():
                path = path.resolve()
            elif self.workspace_root is not None:
                # Canonical project resource URIs are workspace-relative.  They
                # must not be appended to code_location.repository a second time.
                path = (self.workspace_root / path).resolve()
            else:
                path = (repository / path).resolve()
            if self.workspace_root is not None and not (
                path == self.workspace_root or self.workspace_root in path.parents
            ):
                raise RuntimeValidationError(f"dataset URI escapes workspace: {uri}")
            if not path.is_file():
                raise RuntimeValidationError(f"DATASET_UNAVAILABLE: {uri}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeValidationError(f"DATA_CORRUPTION: checksum mismatch for {uri}")
