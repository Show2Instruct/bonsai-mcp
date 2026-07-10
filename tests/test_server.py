"""MCP server tests."""

from __future__ import annotations

import pytest

from bonsai_mcp.server import (
    _dispatch_tool,
    _screenshot_to_mcp_content,
    _tool_definitions,
    build_server,
)
from bonsai_mcp.tools import ALL_TOOL_NAMES, EDIT_TOOL_NAMES, QUERY_TOOL_NAMES
from tests.conftest import FakeBlenderBridgeClient


def test_all_tools_have_definitions():
    defined = {t.name for t in _tool_definitions()}
    assert defined == set(ALL_TOOL_NAMES)


def test_definitions_have_required_fields():
    for tool in _tool_definitions():
        assert tool.name
        assert tool.description, f"{tool.name} is missing a description"
        assert isinstance(tool.inputSchema, dict)


def test_descriptions_have_category_prefix():
    for tool in _tool_definitions():
        if tool.name in QUERY_TOOL_NAMES:
            assert tool.description.startswith("[QUERY] "), tool.name
        else:
            assert tool.description.startswith("[EDIT] "), tool.name


def test_annotations_match_category():
    by_name = {t.name: t for t in _tool_definitions()}
    for name in QUERY_TOOL_NAMES:
        ann = by_name[name].annotations
        assert ann is not None and ann.readOnlyHint is True, name
    for name in EDIT_TOOL_NAMES:
        ann = by_name[name].annotations
        assert ann is not None and ann.readOnlyHint is False, name


def test_build_server_does_not_touch_bridge():
    client = FakeBlenderBridgeClient()
    server = build_server(client)
    assert server is not None
    assert client.calls == []


def test_tool_adapter_hits_bridge():
    client = FakeBlenderBridgeClient(responses={"get_scene_info": {"ok": True}})
    build_server(client)
    assert client.calls == []

    from bonsai_mcp.tools import tool_get_scene_info

    tool_get_scene_info(client)
    assert client.calls == [("get_scene_info", {})]


_DISPATCH_CASES = {
    "get_scene_info": ({}, "get_scene_info"),
    "get_selected_objects": ({}, "get_selected_objects"),
    "get_psets": ({"global_ids": ["AAAA"]}, "get_psets"),
    "get_viewport_screenshot": ({}, "get_viewport_screenshot"),
    "get_ifc_project_info": ({}, "get_ifc_project_info"),
    "execute_ifc_code": ({"code": "print(1)"}, "execute_ifc_code"),
    "execute_blender_code": ({"code": "print(1)"}, "execute_code"),
    "save_ifc_file": ({"output_path": "/tmp/out.ifc"}, "save_ifc_file"),
}


def test_dispatch_cases_cover_every_tool():
    assert set(_DISPATCH_CASES) == set(ALL_TOOL_NAMES)


@pytest.mark.parametrize("tool_name", list(_DISPATCH_CASES))
def test_dispatch_routes_tool_to_its_bridge_command(tool_name):
    def on_send(cmd, _params):
        if cmd == "get_viewport_screenshot":
            return {"path": "/tmp/v.png", "image_base64": "AAAA", "format": "png"}
        return {"ok": True}

    arguments, expected_cmd = _DISPATCH_CASES[tool_name]
    client = FakeBlenderBridgeClient(on_send=on_send)

    out = _dispatch_tool(client, tool_name, arguments)
    assert out, tool_name
    assert client.calls[-1][0] == expected_cmd


def test_dispatch_unknown_tool_returns_text_error():
    client = FakeBlenderBridgeClient()
    out = _dispatch_tool(client, "does_not_exist", {})
    assert len(out) == 1
    assert out[0].type == "text"
    assert out[0].text.startswith("Unknown tool:")
    assert client.calls == []


def test_screenshot_content_success_returns_image_and_path():
    import base64

    b64 = base64.b64encode(b"\x89PNG not-a-real-image").decode("ascii")
    out = _screenshot_to_mcp_content(
        {"path": "/tmp/v.png", "image_base64": b64, "format": "png"}
    )
    image = next(c for c in out if c.type == "image")
    text = next(c for c in out if c.type == "text")
    assert image.mimeType == "image/png"
    assert image.data == b64
    assert "/tmp/v.png" in text.text
    assert out[0].type == "image", "image block must come first for client compatibility"
    assert f"{len(b64)} base64 chars" in text.text


def test_screenshot_content_includes_viewport_state_as_text():
    import base64

    b64 = base64.b64encode(b"fake").decode("ascii")
    viewport = {
        "view_rotation": [0.7071, 0.7071, 0.0, 0.0],
        "view_perspective": "ORTHO",
        "objects_in_view": [
            {"name": "Wall", "ifc_class": "IfcWall", "global_id": "XYZ", "box": [0.1, 0.2, 0.5, 0.9]}
        ],
    }
    out = _screenshot_to_mcp_content(
        {"path": "/tmp/v.jpg", "image_base64": b64, "format": "jpeg", "viewport": viewport}
    )
    text = next(c for c in out if c.type == "text")
    assert "Viewport state:" in text.text
    assert '"view_perspective": "ORTHO"' in text.text
    assert '"global_id": "XYZ"' in text.text


def test_screenshot_content_error_returns_single_text():
    out = _screenshot_to_mcp_content({"error": "no viewport"})
    assert len(out) == 1
    assert out[0].type == "text"
    assert "no viewport" in out[0].text


def test_screenshot_content_invalid_base64_reports_decode_failure():
    out = _screenshot_to_mcp_content({"image_base64": "!!! not base64 !!!"})
    assert any(c.type == "text" and "decode failed" in c.text.lower() for c in out)
    assert all(c.type != "image" for c in out)


def test_screenshot_content_empty_payload():
    out = _screenshot_to_mcp_content({})
    assert len(out) == 1
    assert out[0].type == "text"
    assert "no payload" in out[0].text.lower()


def test_server_instructions_describe_categories():
    from bonsai_mcp.server import _SERVER_INSTRUCTIONS

    assert "[QUERY]" in _SERVER_INSTRUCTIONS
    assert "[EDIT]" in _SERVER_INSTRUCTIONS
    assert "execute_ifc_code" in _SERVER_INSTRUCTIONS
