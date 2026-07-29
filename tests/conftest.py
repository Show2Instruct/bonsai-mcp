"""Shared pytest helpers. Provides a fake bridge so tests can run without Blender."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from bonsai_mcp.blender_client import BlenderBridgeClient, BlenderBridgeError


class FakeBlenderBridgeClient(BlenderBridgeClient):
    """Drop-in client that returns scripted responses instead of opening sockets."""

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        on_send: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        super().__init__(host="127.0.0.1", port=0, timeout=1.0, token="")
        self._responses = responses or {}
        self._on_send = on_send
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def send(self, command: str, params: dict[str, Any] | None = None) -> Any:  # type: ignore[override]
        params = params or {}
        self.calls.append((command, params))
        if self._on_send is not None:
            return self._on_send(command, params)
        if command not in self._responses:
            raise BlenderBridgeError(f"no canned response for {command!r}")
        value = self._responses[command]
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def fake_client() -> FakeBlenderBridgeClient:
    return FakeBlenderBridgeClient()
