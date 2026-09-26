"""Shared contracts for the Research Harness runtime integration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping


RUNTIME_PROTOCOL = "research-sidecar/v0.1"
CONTEXT_VERSION = "research-context/v0.1"
PROPOSAL_VERSION = "research-proposal/v0.1"
SUBMISSION_VERSION = "research-submission/v0.1"


class RuntimeIntegrationError(RuntimeError):
    """Base error with a stable sidecar-facing code and retry classification."""

    code = "INTERNAL"
    retryable = False

    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.details = details


class RuntimeValidationError(RuntimeIntegrationError):
    code = "VALIDATION_FAILED"


class ContextLimitError(RuntimeIntegrationError):
    code = "CONTEXT_LIMIT"


class GitConflict(RuntimeIntegrationError):
    code = "GIT_CONFLICT"
    retryable = True


class SubmissionConflict(RuntimeIntegrationError):
    code = "SUBMISSION_CONFLICT"


def json_value(value: Any) -> Any:
    """Convert supported runtime values to lossless JSON-compatible values."""

    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(child) for child in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Return stable UTF-8 JSON bytes used for hashes and protocol records."""

    return json.dumps(
        json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace one text file and fsync its contents."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(json_value(value), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
    )
