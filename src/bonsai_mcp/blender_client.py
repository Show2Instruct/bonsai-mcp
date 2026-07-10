"""Local TCP client for the Blender bridge."""

from __future__ import annotations

import json
import os
import socket
import struct
from typing import Any

from pydantic import ValidationError

from bonsai_mcp.schemas import BridgeRequest, BridgeResponse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9878
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class BlenderBridgeError(RuntimeError):
    """Raised when the bridge cannot be reached or returns an error response."""


def _parse_port(raw: str | None) -> int:
    """Parse the bridge port."""
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        raise BlenderBridgeError(f"BONSAI_MCP_PORT must be an integer, got {raw!r}.") from None


def _parse_timeout(raw: str | None) -> float:
    """Parse the bridge timeout."""
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        raise BlenderBridgeError(
            f"BONSAI_MCP_TIMEOUT must be a number of seconds, got {raw!r}."
        ) from None


class BlenderBridgeClient:
    """Synchronous client for the Blender bridge."""

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.host = host or os.environ.get("BONSAI_MCP_HOST", DEFAULT_HOST)
        self.port = port or _parse_port(os.environ.get("BONSAI_MCP_PORT"))
        self.timeout = (
            timeout if timeout is not None else _parse_timeout(os.environ.get("BONSAI_MCP_TIMEOUT"))
        )

    def send(self, command: str, params: dict[str, Any] | None = None) -> Any:
        """Send a command and return its result."""
        request = BridgeRequest(command=command, params=params or {})
        response = self._roundtrip(request)
        if not response.success:
            raise BlenderBridgeError(response.error or "bridge returned success=False")
        return response.result

    def ping(self) -> dict[str, Any]:
        """Return bridge status metadata."""
        result = self.send("ping")
        return result if isinstance(result, dict) else {"result": result}

    def _roundtrip(self, request: BridgeRequest) -> BridgeResponse:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise BlenderBridgeError(
                f"Cannot reach Blender bridge at {self.host}:{self.port}. "
                "Is Blender running with the Bonsai MCP bridge add-on started? "
                f"(underlying error: {exc})"
            ) from exc

        try:
            with sock:
                sock.settimeout(self.timeout)
                _write_message(sock, request.model_dump())
                payload = _read_message(sock)
        except TimeoutError as exc:
            raise BlenderBridgeError(
                f"Timed out after {self.timeout}s waiting for the Blender bridge. "
                "Long-running operations may need a larger BONSAI_MCP_TIMEOUT. "
                "If this happens on every call, something other than the Bonsai MCP "
                f"Bridge add-on may be listening on {self.host}:{self.port}."
            ) from exc
        except json.JSONDecodeError as exc:
            raise BlenderBridgeError(
                f"Received a non-JSON reply from {self.host}:{self.port}. "
                "Another application (for example a different Blender MCP bridge) "
                "may be listening on this port; expected the Bonsai MCP Bridge add-on."
            ) from exc
        except OSError as exc:
            raise BlenderBridgeError(
                f"Lost the connection to the Blender bridge at {self.host}:{self.port} "
                f"(underlying error: {exc})"
            ) from exc

        if payload is None:
            raise BlenderBridgeError("Blender bridge closed the connection without responding.")

        try:
            return BridgeResponse.model_validate(payload)
        except ValidationError as exc:
            raise BlenderBridgeError(
                f"The reply from {self.host}:{self.port} is not a Bonsai MCP bridge "
                "response. Another application may be listening on this port; "
                "expected the Bonsai MCP Bridge add-on."
            ) from exc

def _write_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)


def _read_message(sock: socket.socket) -> dict[str, Any] | None:
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length > MAX_MESSAGE_BYTES:
        raise BlenderBridgeError(
            f"Bridge frame too large: {length} bytes exceeds the {MAX_MESSAGE_BYTES} byte cap. "
            "This usually means the service on this port is not the Bonsai MCP Bridge add-on."
        )
    body = _recv_exact(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
