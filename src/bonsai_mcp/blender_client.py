"""Local TCP client for the Blender bridge."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import struct
import threading
from typing import Any

from pydantic import ValidationError

from bonsai_mcp.schemas import BridgeRequest, BridgeResponse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9878
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

_VERSION_SKEW_HINT = (
    " The add-on inside Blender is likely older than this server: redeploy "
    "blender_addon/bonsai_bridge.py (or reinstall the add-on zip) and "
    "restart the bridge."
)


class BlenderBridgeError(RuntimeError):
    """Raised when the bridge cannot be reached or returns an error response."""


class _TransportFailure(Exception):
    """Internal: a transport-level failure during one exchange.

    `retryable` means no reply byte arrived and the request cannot have
    outlived the connection (the add-on cancels queued requests on client
    disconnect), so resending on a fresh connection is safe. A reply
    timeout is NOT retryable: the request may still be executing.
    """

    def __init__(self, retryable: bool, error: BlenderBridgeError) -> None:
        super().__init__(str(error))
        self.retryable = retryable
        self.error = error


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
    """Synchronous client for the Blender bridge.

    Keeps one TCP connection open across calls (the add-on handler reads
    frames until disconnect). A send failure or an unreplied frame on a
    reused connection triggers one transparent reconnect, which covers the
    add-on closing idle connections between calls. Calls are serialized
    with a lock so concurrent tool calls cannot interleave frames on the
    shared connection.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: float | None = None,
        token: str | None = None,
    ) -> None:
        self.host = host if host is not None else os.environ.get("BONSAI_MCP_HOST", DEFAULT_HOST)
        self.port = port if port is not None else _parse_port(os.environ.get("BONSAI_MCP_PORT"))
        self.timeout = (
            timeout if timeout is not None else _parse_timeout(os.environ.get("BONSAI_MCP_TIMEOUT"))
        )
        self.token = token if token is not None else (os.environ.get("BONSAI_MCP_TOKEN") or None)
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._request_id = 0

    def send(self, command: str, params: dict[str, Any] | None = None) -> Any:
        """Send a command and return its result."""
        request = BridgeRequest(command=command, params=params or {})
        with self._lock:
            self._request_id += 1
            payload: dict[str, Any] = {
                "command": request.command,
                "params": request.params,
                "id": self._request_id,
            }
            if self.token:
                payload["token"] = self.token
            response = self._roundtrip(payload, self._request_id)
        if not response.success:
            error = response.error or "bridge returned success=False"
            if "Unknown command" in error:
                error += _VERSION_SKEW_HINT
            raise BlenderBridgeError(error)
        return response.result

    def ping(self) -> dict[str, Any]:
        """Return bridge status metadata."""
        result = self.send("ping")
        return result if isinstance(result, dict) else {"result": result}

    def close(self) -> None:
        """Close the persistent connection (safe to call at any time)."""
        with self._lock:
            self._drop_connection()

    def _drop_connection(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def _connect(self) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise BlenderBridgeError(
                f"Cannot reach Blender bridge at {self.host}:{self.port}. "
                "Is Blender running with the Bonsai MCP bridge add-on started? "
                f"(underlying error: {exc})"
            ) from exc
        sock.settimeout(self.timeout)
        return sock

    def _roundtrip(self, payload: dict[str, Any], request_id: int) -> BridgeResponse:
        # caller holds self._lock
        reused = self._sock is not None
        if self._sock is None:
            self._sock = self._connect()
        sock = self._sock
        sock.settimeout(self.timeout)

        try:
            reply = self._exchange(sock, payload)
        except _TransportFailure as failure:
            self._drop_connection()
            if not (failure.retryable and reused):
                raise failure.error from failure
            # the add-on closed the idle connection between calls; one
            # fresh-connection retry, then give up with the original error
            self._sock = self._connect()
            try:
                reply = self._exchange(self._sock, payload)
            except _TransportFailure as second:
                self._drop_connection()
                raise second.error from second

        reply_id = reply.get("id")
        if reply_id is not None and reply_id != request_id:
            # a stale reply from a previous exchange means the stream is
            # desynchronized; drop the connection rather than mis-matching
            self._drop_connection()
            raise BlenderBridgeError(
                "Out-of-sync reply from the Blender bridge (got response id "
                f"{reply_id!r}, expected {request_id}). The connection was "
                "reset; retry the call."
            )

        try:
            return BridgeResponse.model_validate(reply)
        except ValidationError as exc:
            self._drop_connection()
            raise BlenderBridgeError(
                f"The reply from {self.host}:{self.port} is not a Bonsai MCP bridge "
                "response. Another application may be listening on this port; "
                "expected the Bonsai MCP Bridge add-on."
            ) from exc

    def _exchange(self, sock: socket.socket, payload: dict[str, Any]) -> dict[str, Any]:
        """One framed write + read. Raises _TransportFailure on transport errors."""
        try:
            _write_message(sock, payload)
        except OSError as exc:
            raise _TransportFailure(
                True,
                BlenderBridgeError(
                    f"Lost the connection to the Blender bridge at {self.host}:{self.port} "
                    f"(underlying error: {exc})"
                ),
            ) from exc

        try:
            reply = _read_message(sock)
        except TimeoutError as exc:
            # the request may still be executing inside Blender: never resend
            raise _TransportFailure(
                False,
                BlenderBridgeError(
                    f"Timed out after {self.timeout}s waiting for the Blender bridge. "
                    "Long-running operations may need a larger BONSAI_MCP_TIMEOUT. "
                    "If this happens on every call, something other than the Bonsai MCP "
                    f"Bridge add-on may be listening on {self.host}:{self.port}."
                ),
            ) from exc
        except json.JSONDecodeError as exc:
            raise _TransportFailure(
                False,
                BlenderBridgeError(
                    f"Received a non-JSON reply from {self.host}:{self.port}. "
                    "Another application (for example a different Blender MCP bridge) "
                    "may be listening on this port; expected the Bonsai MCP Bridge add-on."
                ),
            ) from exc
        except BlenderBridgeError as exc:
            # oversized frame: a protocol-level red flag, not a stale socket
            raise _TransportFailure(False, exc) from exc
        except OSError as exc:
            raise _TransportFailure(
                True,
                BlenderBridgeError(
                    f"Lost the connection to the Blender bridge at {self.host}:{self.port} "
                    f"(underlying error: {exc})"
                ),
            ) from exc

        if reply is None:
            raise _TransportFailure(
                True,
                BlenderBridgeError("Blender bridge closed the connection without responding."),
            )
        return reply


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
