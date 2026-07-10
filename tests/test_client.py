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
