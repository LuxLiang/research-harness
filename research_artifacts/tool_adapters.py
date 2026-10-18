"""Deterministic scientific tool adapters used inside bounded Skill actions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .runtime_types import RuntimeValidationError, atomic_write_json, sha256_json


Runner = Callable[..., subprocess.CompletedProcess[str]]
_LEAN_RAW_STRING = re.compile(r'r(#+)?"')


@dataclass(frozen=True)
class LeanResult:
    outcome: str
    build_passed: bool
    axiom_audit_passed: bool
    findings: tuple[str, ...]
    report_path: str
    report_sha256: str


def _lean_code(source: str) -> str:
    """Mask comments and literals, preserving newlines for conservative scans.

    Lean block comments nest. Strings (including raw strings) and quoted names
    must not turn their contents into apparent comments or forbidden tokens.
    This is only a supplemental scan; compiled target axiom reports are required.
    """
    result = list(source)
    index = 0
    while index < len(source):
        start = index
        if source.startswith('--', index):
            end = source.find('\n', index)
            index = len(source) if end < 0 else end
        elif source.startswith('/-', index):
            depth = 1
            index += 2
            while index < len(source) and depth:
                if source.startswith('/-', index):
                    depth += 1
                    index += 2
                elif source.startswith('-/', index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
        elif (raw := _LEAN_RAW_STRING.match(source, index)) is not None:
            closing = '"' + (raw.group(1) or '')
            end = source.find(closing, raw.end())
            index = len(source) if end < 0 else end + len(closing)
        elif source[index] == '"':
            index += 1
            while index < len(source):
                if source[index] == '\\':
                    index += 2
                elif source[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
        elif source[index] == '«':
            end = source.find('»', index + 1)
            index = len(source) if end < 0 else end + 1
        else:
            index += 1
            continue
        for position in range(start, min(index, len(source))):
            if result[position] != '\n':
                result[position] = ' '
    return ''.join(result)


class LeanToolAdapter:
    """Compile exact source snapshots and audit explicitly named targets.

    The runner/compiler is trusted execution infrastructure. Version parameters
    remain provenance labels; this adapter does not attest the toolchain itself.
    """

    def __init__(self, runner: Runner = subprocess.run):
        self.runner = runner

    def verify(
        self,
        *,
        project_dir: str | Path,
        source_paths: Sequence[str | Path],
        target_theorems: Mapping[str, Sequence[str]],
        lean_version: str,
        mathlib_commit: str,
        allowed_axioms: Sequence[str] = ("propext", "Classical.choice", "Quot.sound"),
        report_path: str | Path,
        build_command: Sequence[str] | None = ("lake", "build"),
        lean_command: Sequence[str] = ("lake", "env", "lean"),
        stronger_checker: Sequence[str] | None = None,
        timeout: int = 300,
    ) -> LeanResult:
        root = Path(project_dir).resolve()
        if not source_paths:
            raise RuntimeValidationError("Lean verification requires nonempty source_paths")
        # Only compiler invocations are accepted here, never arbitrary shell
        # commands. A custom build is preparatory and cannot establish PASS.
        compiler = list(lean_command)
        if not (
            (len(compiler) == 1 and Path(compiler[0]).name == 'lean')
            or (len(compiler) == 3 and Path(compiler[0]).name == 'lake'
                and compiler[1:] == ['env', 'lean'])
        ):
            raise RuntimeValidationError("lean_command must invoke lean or lake env lean")
        sources = [(root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve() for path in source_paths]
        if len(set(sources)) != len(sources):
            raise RuntimeValidationError("Duplicate Lean source paths")
        targets: dict[Path, list[str]] = {}
        for path, names in target_theorems.items():
            source = (root / path).resolve()
            if source in targets or isinstance(names, str) or not names:
                raise RuntimeValidationError("Each Lean source requires a nonempty target list")
            names = list(names)
            if len(set(names)) != len(names) or any(
                not isinstance(name, str) or not re.fullmatch(r"[^\W\d][\w']*(?:\.[^\W\d][\w']*)*", name)
                for name in names
            ):
                raise RuntimeValidationError("Lean targets must be distinct qualified identifiers")
            targets[source] = names
        if set(targets) != set(sources):
            raise RuntimeValidationError("target_theorems must cover exactly the supplied Lean sources")
        if Path(report_path).resolve() in sources:
            raise RuntimeValidationError("Lean report must not overwrite a supplied source")
        for source in sources:
            if root not in source.parents and source != root:
                raise RuntimeValidationError("Lean source escapes the pinned project directory")
            if not source.is_file():
                raise RuntimeValidationError(f"Lean source not found: {source}")
        findings: list[str] = []
        declared_axioms: set[str] = set()
        source_hashes: dict[str, str] = {}
        snapshots: dict[Path, str] = {}
        for source in sources:
            raw = source.read_bytes()
            snapshots[source] = raw.decode('utf-8')
            content = _lean_code(snapshots[source])
            source_hashes[str(source.relative_to(root))] = hashlib.sha256(raw).hexdigest()
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

        def run(command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            nonlocal build_failure
            try:
                return self.runner(list(command), cwd=root, text=True,
                                   capture_output=True, check=False, timeout=timeout, **kwargs)
            except subprocess.TimeoutExpired as exc:
                build_failure = "TOOL_TIMEOUT"
                def decoded(value: str | bytes | None) -> str:
                    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
                return subprocess.CompletedProcess(list(command), 124, decoded(exc.stdout),
                                                   decoded(exc.stderr) or "Lean check timed out")
            except OSError as exc:
                build_failure = "TOOL_UNAVAILABLE"
                return subprocess.CompletedProcess(list(command), 127, '', str(exc))

        build = run(build_command) if build_command else subprocess.CompletedProcess([], 0, '', '')
        source_checks: list[dict[str, Any]] = []
        target_axioms: dict[str, dict[str, list[str]]] = {}
        if build.returncode == 0:
            for source in sources:
                relative = str(source.relative_to(root))
                audit_commands = ''.join(f'\n#print axioms _root_.{name}\n' for name in targets[source])
                checked = run([*compiler, '--stdin'], input=snapshots[source] + '\n' + audit_commands)
                source_checks.append({
                    'source': relative, 'source_sha256': source_hashes[relative],
                    'command': [*compiler, '--stdin'], 'returncode': checked.returncode,
                    'stdout': checked.stdout, 'stderr': checked.stderr,
                })
                audits: dict[str, list[str]] = {}
                for match in re.finditer(
                    r"(?m)^'([^\n]+)' (?:depends on axioms:\s*\[([^\]]*)\]|does not depend on any axioms)\s*$",
                    checked.stdout + '\n' + checked.stderr,
                ):
                    name = match.group(1)
                    axioms = [item.strip() for item in (match.group(2) or '').split(',') if item.strip()]
                    # Union duplicate reports so a later empty report cannot hide
                    # forbidden dependencies printed by the actual compiler.
                    audits[name] = sorted(set(audits.get(name, [])) | set(axioms))
                target_axioms[relative] = {name: audits[name] for name in targets[source] if name in audits}
                if checked.returncode == 0:
                    findings.extend(f'AXIOM_AUDIT_MISSING:{name}' for name in targets[source] if name not in audits)
        build_passed = build.returncode == 0 and len(source_checks) == len(sources) and all(check['returncode'] == 0 for check in source_checks)
        if not build_passed:
            findings.append(build_failure or "LEAN_BUILD_FAILED")
        combined_output = '\n'.join([build.stdout, build.stderr, *(
            check['stdout'] + '\n' + check['stderr'] for check in source_checks
        )])
        reported_axioms: set[str] = set()
        # Failed elaboration can introduce recovery sorryAx terms. Preserve its
        # diagnostics, but classify it as a build failure rather than an axiom
        # violation unless a source scan or successful compilation establishes one.
        audit_output = '\n'.join(
            check['stdout'] + '\n' + check['stderr']
            for check in source_checks if check['returncode'] == 0
        )
        for match in re.finditer(
            r"depends on axioms:\s*\[([^\]]*)\]", audit_output
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
        if 'sorryAx' in reported_axioms:
            findings.append('SORRY_AX_FOUND')
        stronger = None
        if stronger_checker and build_passed and not findings:
            stronger = run(stronger_checker)
            if stronger.returncode != 0:
                findings.append("STRONGER_CHECK_FAILED")
        for source in sources:
            try:
                unchanged = hashlib.sha256(source.read_bytes()).hexdigest() == source_hashes[str(source.relative_to(root))]
            except OSError:
                unchanged = False
            if not unchanged:
                findings.append(f'SOURCE_CHANGED:{source.relative_to(root)}')
        audit_passed = build_passed and not findings
        if any(item.startswith("SORRY") for item in findings):
            outcome = "SORRY_FOUND"
        elif unapproved or unapproved_reported:
            outcome = "UNAPPROVED_AXIOM"
        elif build_failure is not None:
            outcome = build_failure
        elif not build_passed:
            combined = combined_output.lower()
            if any(word in combined for word in ("unknown module", "package", "dependency", "no such file")):
                outcome = "LIBRARY_GAP"
            elif any(word in combined for word in ("unexpected token", "parser", "invalid syntax")):
                outcome = "FORMALIZATION_GAP"
            else:
                outcome = "LEAN_PROOF_GAP"
        elif any(item.startswith('AXIOM_AUDIT_MISSING:') for item in findings):
            outcome = 'AXIOM_AUDIT_MISSING'
        elif any(item.startswith('SOURCE_CHANGED:') for item in findings):
            outcome = 'SOURCE_CHANGED'
        elif stronger is not None and stronger.returncode != 0:
            outcome = "LEAN_PROOF_GAP"
        else:
            outcome = "PASS"
        report = {
            "adapter_version": "research-lean-tool/v0.2",
            "outcome": outcome,
            "lean_version": lean_version,
            "mathlib_commit": mathlib_commit,
            "source_hashes": source_hashes,
            "build_command": list(build_command) if build_command else None,
            "lean_command": compiler,
            "target_theorems": {str(source.relative_to(root)): targets[source] for source in sources},
            "target_axioms": target_axioms,
            "source_checks": source_checks,
            "build_returncode": build.returncode or next((check['returncode'] for check in source_checks if check['returncode']), 0),
            "build_stdout": '\n'.join([build.stdout, *(check['stdout'] for check in source_checks)]),
            "build_stderr": '\n'.join([build.stderr, *(check['stderr'] for check in source_checks)]),
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
        existing = next((run for run in experiment.get('runs', []) if run.get('run_id') == run_id), None)
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
                self._validate_reused_run(persisted['run'], run_id, actual_hash, commit)
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
        if existing is not None:
            raise RuntimeValidationError('Existing experiment run lacks a matching host execution journal')
        repository = self._repository(experiment)
        self._validate_datasets(experiment, repository=repository)
        with self._pinned_repository(repository, commit, output_dir) as snapshot:
            original_root, snapshot_root, pinned_repository, actual_commit = snapshot
            self._validate_snapshot_datasets(experiment, repository, original_root, snapshot_root)
            return self._dispatch(
                experiment=experiment, seed=seed, actual_hash=actual_hash,
                output_dir=output_dir, run_state=run_state, run_id=run_id,
                entrypoint=entrypoint, repository=pinned_repository,
                original_root=original_root, snapshot_root=snapshot_root,
                code_commit=actual_commit, timeout=timeout, extra_env=extra_env,
            )

    def _dispatch(self, *, experiment, seed, actual_hash, output_dir, run_state,
                  run_id, entrypoint, repository, original_root, snapshot_root,
                  code_commit, timeout, extra_env):
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
        command = [item if Path(item).is_absolute() and Path(item).resolve().is_relative_to(output_dir)
                   else self._snapshot_token(item, original_root, snapshot_root) for item in command]
        interpreter = Path(command[0]).name
        if interpreter.startswith('python') or interpreter in {'bash', 'sh', 'node', 'Rscript', 'julia'}:
            position = 1
            while position < len(command) and command[position].startswith('-'):
                option = command[position]
                if option == '-c':  # Literal code is itself part of the locked protocol.
                    position = len(command)
                    break
                if option == '-m' and interpreter.startswith('python'):
                    module = command[position + 1] if position + 1 < len(command) else ''
                    module_path = repository / module.replace('.', '/')
                    if not module or not (module_path.with_suffix('.py').is_file() or (module_path / '__main__.py').is_file()):
                        raise RuntimeValidationError('Experiment module must exist in the pinned code snapshot')
                    position = len(command)
                    break
                position += 2 if option in {'-X', '-W', '--check-hash-based-pycs'} else 1
            if position < len(command):
                path = Path(command[position])
                path = (repository / path).resolve() if not path.is_absolute() else path.resolve()
                if not path.is_relative_to(snapshot_root) or not path.is_file():
                    raise RuntimeValidationError('Experiment script must exist in the pinned code snapshot')
                command[position] = str(path)
        manifest = output_dir / 'execution-manifest.json'
        atomic_write_json(manifest, {
            'manifest_version': 'research-experiment-execution/v0.1',
            'code_commit': code_commit, 'repository': str(original_root),
            'snapshot_root': str(snapshot_root), 'command': command,
        })
        env = os.environ.copy()
        env.update(extra_env or {})
        import_paths = [repository]
        if (repository / 'src').is_dir():
            import_paths.append(repository / 'src')
        env["PYTHONPATH"] = os.pathsep.join(map(str, import_paths))
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
        run['outputs'].append({
            'uri': str(manifest), 'sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
            'media_type': 'application/json', 'description': 'Pinned execution manifest',
        })
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

    @staticmethod
    def _snapshot_token(token: str, original_root: Path, snapshot_root: Path) -> str:
        path = Path(token)
        if path.is_absolute():
            if path.resolve().is_relative_to(original_root):
                return str(snapshot_root / path.resolve().relative_to(original_root))
            return token
        # Embedded absolute paths cannot be safely rewritten as shell text.
        if str(original_root) in token:
            raise RuntimeValidationError('Repository paths in experiment commands must be standalone tokens')
        return token

    @contextmanager
    def _pinned_repository(self, repository: Path, commit: str, output_dir: Path):
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise RuntimeValidationError('Experiment requires a full pinned Git commit')

        def git(*args: str, check: bool = True):
            try:
                result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repository), *args],
                                        text=True, capture_output=True, timeout=60, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeValidationError('Pinned experiment Git checkout unavailable') from exc
            if check and result.returncode:
                raise RuntimeValidationError('Pinned experiment Git checkout failed: ' + result.stderr.strip())
            return result

        original_root = Path(git('rev-parse', '--show-toplevel').stdout.strip()).resolve()
        actual = git('rev-parse', '--verify', commit + '^{commit}').stdout.strip()
        if actual != commit:
            raise RuntimeValidationError('Experiment commit identity mismatch')
        if git('ls-tree', '-r', commit).stdout.find('160000 commit ') >= 0:
            raise RuntimeValidationError('Pinned experiment submodules must be materialized explicitly')
        checkout = output_dir / 'code-checkout'
        git('worktree', 'add', '--detach', str(checkout), commit)
        try:
            for path in checkout.rglob('*'):
                if path.is_symlink() and not path.resolve().is_relative_to(checkout):
                    raise RuntimeValidationError('Pinned code snapshot contains an escaping symlink')
            pinned_repository = checkout / repository.relative_to(original_root)
            if not pinned_repository.is_dir():
                raise RuntimeValidationError('Experiment repository directory is absent from the pinned commit')
            yield original_root, checkout, pinned_repository, actual
        finally:
            git('worktree', 'remove', '--force', str(checkout))

    @staticmethod
    def _validate_reused_run(run, run_id, protocol_hash, commit):
        if (run.get('run_id') != run_id or run.get('protocol_sha256') != protocol_hash
                or run.get('code_commit') != commit):
            raise RuntimeValidationError('Persisted experiment run identity mismatch')
        if run.get('status') != 'SUCCEEDED':
            return
        manifests = []
        for resource in run.get('outputs', []):
            path = Path(resource.get('uri', ''))
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != resource.get('sha256'):
                raise RuntimeValidationError('Persisted experiment output hash mismatch')
            if path.name == 'execution-manifest.json':
                manifests.append(json.loads(path.read_text()))
        if len(manifests) != 1 or manifests[0].get('code_commit') != commit:
            raise RuntimeValidationError('Persisted run lacks a pinned execution manifest')

    def _validate_snapshot_datasets(self, experiment, repository, original_root, snapshot_root):
        for dataset in experiment.get('datasets', []):
            uri = dataset.get('uri')
            if not isinstance(uri, str) or '://' in uri:
                continue
            original = Path(uri)
            if not original.is_absolute():
                original = (self.workspace_root or repository) / original
            original = original.resolve()
            if original.is_relative_to(original_root):
                pinned = snapshot_root / original.relative_to(original_root)
                if (not pinned.is_file() or
                        hashlib.sha256(pinned.read_bytes()).hexdigest() != dataset.get('sha256')):
                    raise RuntimeValidationError('Pinned snapshot dataset differs from the locked protocol: ' + uri)

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
