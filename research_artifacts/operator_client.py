"""Bounded local transport to the model-backed Cordis controller."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import socket
import time
import uuid
from typing import Any


PROTOCOL = "research-operator/v0.1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class OperatorError(RuntimeError):
    """The host is unavailable, incompatible, or rejected a request."""


def operator_socket(workspace: Path, explicit: str | None = None) -> Path:
    value = explicit or os.environ.get("RESEARCH_HARNESS_SOCKET")
    path = Path(value).expanduser() if value else Path(".harness/research/cordis.sock")
    path = (workspace / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(workspace.resolve()):
        raise OperatorError("operator socket must be inside the selected workspace")
    return path


class CordisClient:
    """Never retries a dispatched request or falls back to synthetic execution."""

    def __init__(self, workspace: str | Path, *, socket_path: str | None = None,
                 timeout: float = 3600):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("operator timeout must be finite and greater than zero")
        self.workspace = Path(workspace).expanduser().resolve()
        self.socket_path = operator_socket(self.workspace, socket_path)
        self.timeout = timeout

    def call(self, method: str, params: dict[str, Any] | None = None,
             *, timeout: float | None = None) -> Any:
        request_id = uuid.uuid4().hex
        request = json.dumps({"id": request_id, "method": method, "params": params or {}},
                             allow_nan=False).encode() + b"\n"
        if len(request) > 1_000_000:
            raise OperatorError("operator request exceeds 1 MB")
        if not hasattr(socket, "AF_UNIX"):
            raise OperatorError("the Cordis operator requires Unix-domain sockets")
        dispatched = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(min(self.timeout, 5))
                connection.connect(str(self.socket_path))
                response_timeout = self.timeout if timeout is None else timeout
                deadline = time.monotonic() + response_timeout
                connection.settimeout(response_timeout)
                dispatched = True
                connection.sendall(request)
                response = bytearray()
                while b"\n" not in response:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("operator response deadline exceeded")
                    connection.settimeout(remaining)
                    chunk = connection.recv(65536)
                    if not chunk:
                        raise OperatorError("operator closed before sending a complete response")
                    response.extend(chunk)
                    if len(response) > MAX_RESPONSE_BYTES:
                        raise OperatorError("operator response exceeds 8 MB")
            value = json.loads(bytes(response).split(b"\n", 1)[0])
        except (OSError, ValueError, OperatorError) as exc:
            detail = (
                "Request outcome is unknown; inspect status before retrying. "
                "Disconnecting does not cancel host execution."
                if dispatched else
                "Start the configured dsh host for this workspace and run research doctor."
            )
            raise OperatorError(f"Cordis operator unavailable at {self.socket_path}: {exc}. {detail}") from exc
        if not isinstance(value, dict) or value.get("id") != request_id:
            raise OperatorError("operator response does not match the request; inspect status before retrying")
        if value.get("ok") is not True:
            raise OperatorError(str(value.get("error", "operator rejected the request")))
        return value.get("result")

    def handshake(self) -> dict[str, Any]:
        value = self.call("health", timeout=min(self.timeout, 5))
        if not isinstance(value, dict) or value.get("protocol") != PROTOCOL:
            raise OperatorError("incompatible operator protocol; rebuild and restart the Cordis integration")
        if value.get("runtime") != "cordis":
            raise OperatorError("operator is not a Cordis research runtime")
        workspace = value.get("workspace")
        if not isinstance(workspace, str) or Path(workspace).resolve() != self.workspace:
            raise OperatorError("operator serves a different workspace; request was not dispatched")
        return value
