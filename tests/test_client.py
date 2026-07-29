"""Blender bridge client tests."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time

import pytest

from bonsai_mcp.blender_client import (
    BlenderBridgeClient,
    BlenderBridgeError,
)


def _serve_once(host: str, port: int, response: dict, hold_seconds: float = 0.0) -> threading.Thread:
    """Serve one framed response."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    srv.settimeout(5.0)

    def run() -> None:
        try:
            conn, _ = srv.accept()
            with conn:
                header = b""
                while len(header) < 4:
                    chunk = conn.recv(4 - len(header))
                    if not chunk:
                        return
                    header += chunk
                (length,) = struct.unpack(">I", header)
                body = b""
                while len(body) < length:
                    chunk = conn.recv(length - len(body))
                    if not chunk:
                        return
                    body += chunk
                json.loads(body.decode("utf-8"))

                if hold_seconds:
                    time.sleep(hold_seconds)

                payload = json.dumps(response).encode("utf-8")
                conn.sendall(struct.pack(">I", len(payload)) + payload)
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _serve_raw_once(host: str, port: int, raw: bytes) -> threading.Thread:
    """Serve one raw response."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(1)
    srv.settimeout(5.0)

    def run() -> None:
        try:
            conn, _ = srv.accept()
            with conn:
                header = b""
                while len(header) < 4:
                    chunk = conn.recv(4 - len(header))
                    if not chunk:
                        return
                    header += chunk
                (length,) = struct.unpack(">I", header)
                body = b""
                while len(body) < length:
                    chunk = conn.recv(length - len(body))
                    if not chunk:
                        return
                    body += chunk
                conn.sendall(raw)
        finally:
            srv.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestClientFraming:
    def test_send_and_receive(self):
        port = _free_port()
        _serve_once("127.0.0.1", port, {"success": True, "result": {"hello": "world"}})

        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        result = client.send("ping")
        assert result == {"hello": "world"}

    def test_ping_helper(self):
        port = _free_port()
        _serve_once("127.0.0.1", port, {"success": True, "result": {"status": "ok"}})

        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        assert client.ping() == {"status": "ok"}

    def test_failure_response_raises(self):
        port = _free_port()
        _serve_once("127.0.0.1", port, {"success": False, "error": "kaboom"})

        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        with pytest.raises(BlenderBridgeError, match="kaboom"):
            client.send("ping")


class TestClientErrors:
    def test_connection_refused_message(self):
        port = _free_port()
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=0.5)
        with pytest.raises(BlenderBridgeError, match="Cannot reach Blender bridge"):
            client.send("ping")

    def test_timeout(self):
        port = _free_port()
        _serve_once(
            "127.0.0.1",
            port,
            {"success": True, "result": "late"},
            hold_seconds=1.0,
        )

        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=0.2)
        with pytest.raises(BlenderBridgeError, match="Timed out"):
            client.send("ping")


class TestClientEnvDefaults:
    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("BONSAI_MCP_HOST", "127.0.0.1")
        monkeypatch.setenv("BONSAI_MCP_PORT", "12345")
        monkeypatch.setenv("BONSAI_MCP_TIMEOUT", "7.5")

        client = BlenderBridgeClient()
        assert client.host == "127.0.0.1"
        assert client.port == 12345
        assert client.timeout == pytest.approx(7.5)

    def test_malformed_port_raises_clear_error(self, monkeypatch):
        monkeypatch.setenv("BONSAI_MCP_PORT", "not-a-number")
        with pytest.raises(BlenderBridgeError, match="BONSAI_MCP_PORT must be an integer"):
            BlenderBridgeClient()

    def test_malformed_timeout_raises_clear_error(self, monkeypatch):
        monkeypatch.setenv("BONSAI_MCP_TIMEOUT", "soon")
        with pytest.raises(BlenderBridgeError, match="BONSAI_MCP_TIMEOUT must be a number"):
            BlenderBridgeClient()

    def test_explicit_falsy_host_and_port_are_honored(self, monkeypatch):
        # an explicit host="" or port=0 must not be silently replaced by env/defaults
        monkeypatch.setenv("BONSAI_MCP_HOST", "envhost")
        monkeypatch.setenv("BONSAI_MCP_PORT", "12345")
        client = BlenderBridgeClient(host="", port=0, timeout=1.0)
        assert client.host == ""
        assert client.port == 0


class TestForeignService:
    """A different service on the bridge port must produce a clear error, not a traceback."""

    def test_non_json_reply_hints_at_port_conflict(self):
        port = _free_port()
        garbage = b"\x00\x00\x00\x0bhello world"
        _serve_raw_once("127.0.0.1", port, garbage)

        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        with pytest.raises(BlenderBridgeError, match="non-JSON reply"):
            client.send("ping")

    def test_unframed_reply_hints_at_port_conflict(self):
        port = _free_port()
        _serve_raw_once("127.0.0.1", port, b"HTTP/1.1 400 Bad Request\r\n\r\n")

        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        with pytest.raises(BlenderBridgeError, match="not the Bonsai MCP Bridge"):
            client.send("ping")

    def test_wrong_envelope_hints_at_port_conflict(self):
        port = _free_port()
        _serve_once("127.0.0.1", port, {"jsonrpc": "2.0", "id": 1})

        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        with pytest.raises(BlenderBridgeError, match="not a Bonsai MCP bridge response"):
            client.send("ping")


def _read_frame(conn: socket.socket) -> dict | None:
    header = b""
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    (length,) = struct.unpack(">I", header)
    body = b""
    while len(body) < length:
        chunk = conn.recv(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def _send_frame(conn: socket.socket, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    conn.sendall(struct.pack(">I", len(body)) + body)


def _listener(port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(2)
    srv.settimeout(5.0)
    return srv


class TestPersistentConnection:
    def test_two_requests_reuse_one_connection(self):
        port = _free_port()
        srv = _listener(port)
        stats = {"accepts": 0, "requests": []}

        def run() -> None:
            try:
                conn, _ = srv.accept()
                stats["accepts"] += 1
                with conn:
                    for _ in range(2):
                        request = _read_frame(conn)
                        if request is None:
                            return
                        stats["requests"].append(request)
                        _send_frame(
                            conn,
                            {"success": True, "result": {"ok": True}, "id": request.get("id")},
                        )
            finally:
                srv.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        assert client.send("ping") == {"ok": True}
        assert client.send("ping") == {"ok": True}
        t.join(timeout=5)
        assert stats["accepts"] == 1, "second call must reuse the open connection"
        assert [r["id"] for r in stats["requests"]] == [1, 2]

    def test_reconnects_after_bridge_closed_idle_connection(self):
        port = _free_port()
        srv = _listener(port)

        def run() -> None:
            try:
                # first connection: serve one request, then close (idle timeout)
                conn, _ = srv.accept()
                with conn:
                    request = _read_frame(conn)
                    _send_frame(conn, {"success": True, "result": 1, "id": request.get("id")})
                # second connection: the client's transparent reconnect
                conn2, _ = srv.accept()
                with conn2:
                    request = _read_frame(conn2)
                    _send_frame(conn2, {"success": True, "result": 2, "id": request.get("id")})
            finally:
                srv.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        assert client.send("ping") == 1
        assert client.send("ping") == 2, "stale connection must trigger one reconnect"
        t.join(timeout=5)

    def test_fresh_connection_eof_is_not_retried(self):
        port = _free_port()
        srv = _listener(port)

        def run() -> None:
            try:
                conn, _ = srv.accept()
                with conn:
                    _read_frame(conn)
                    # close without replying
            finally:
                srv.close()

        threading.Thread(target=run, daemon=True).start()
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        with pytest.raises(BlenderBridgeError, match="without responding"):
            client.send("ping")

    def test_out_of_sync_reply_id_raises(self):
        port = _free_port()
        _serve_once("127.0.0.1", port, {"success": True, "result": "stale", "id": 999})
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        with pytest.raises(BlenderBridgeError, match="Out-of-sync reply"):
            client.send("ping")

    def test_request_carries_id_and_token(self, monkeypatch):
        monkeypatch.setenv("BONSAI_MCP_TOKEN", "sekret")
        port = _free_port()
        srv = _listener(port)
        captured: dict = {}

        def run() -> None:
            try:
                conn, _ = srv.accept()
                with conn:
                    request = _read_frame(conn)
                    captured.update(request or {})
                    _send_frame(conn, {"success": True, "result": {}, "id": request.get("id")})
            finally:
                srv.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        client.send("ping")
        t.join(timeout=5)
        assert captured["command"] == "ping"
        assert captured["id"] == 1
        assert captured["token"] == "sekret"

    def test_no_token_field_when_unset(self, monkeypatch):
        monkeypatch.delenv("BONSAI_MCP_TOKEN", raising=False)
        port = _free_port()
        srv = _listener(port)
        captured: dict = {}

        def run() -> None:
            try:
                conn, _ = srv.accept()
                with conn:
                    request = _read_frame(conn)
                    captured.update(request or {})
                    _send_frame(conn, {"success": True, "result": {}})
            finally:
                srv.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        client.send("ping")
        t.join(timeout=5)
        assert "token" not in captured


class TestVersionSkew:
    def test_unknown_command_error_appends_redeploy_hint(self):
        port = _free_port()
        _serve_once(
            "127.0.0.1",
            port,
            {"success": False, "error": "ValueError: Unknown command: 'list_elements'"},
        )
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        with pytest.raises(BlenderBridgeError, match="redeploy"):
            client.send("list_elements")


class TestClientFrameCap:
    def test_oversized_response_frame_rejected(self):
        from bonsai_mcp.blender_client import MAX_MESSAGE_BYTES

        port = _free_port()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(5.0)

        def run() -> None:
            try:
                conn, _ = srv.accept()
                with conn:
                    header = b""
                    while len(header) < 4:
                        chunk = conn.recv(4 - len(header))
                        if not chunk:
                            return
                        header += chunk
                    (length,) = struct.unpack(">I", header)
                    body = b""
                    while len(body) < length:
                        chunk = conn.recv(length - len(body))
                        if not chunk:
                            return
                        body += chunk
                    conn.sendall(struct.pack(">I", MAX_MESSAGE_BYTES + 1))
            finally:
                srv.close()

        threading.Thread(target=run, daemon=True).start()
        client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
        with pytest.raises(BlenderBridgeError, match="too large"):
            client.send("ping")
