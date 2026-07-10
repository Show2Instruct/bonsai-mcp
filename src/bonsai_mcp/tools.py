"""MCP tool adapters."""

from __future__ import annotations

import json
from typing import Any

from bonsai_mcp.blender_client import BlenderBridgeClient, BlenderBridgeError
from bonsai_mcp.schemas import (
    ExecuteCodeInput,
    ExecuteIfcCodeInput,
    GetPsetsInput,
    GetSceneInfoInput,
    SaveIfcInput,
    ViewportScreenshotInput,
)

TOOL_GET_SCENE_INFO = "get_scene_info"
TOOL_GET_SELECTED_OBJECTS = "get_selected_objects"
TOOL_GET_PSETS = "get_psets"
TOOL_GET_VIEWPORT_SCREENSHOT = "get_viewport_screenshot"
TOOL_GET_IFC_PROJECT_INFO = "get_ifc_project_info"
TOOL_EXECUTE_IFC_CODE = "execute_ifc_code"
TOOL_EXECUTE_BLENDER_CODE = "execute_blender_code"
TOOL_SAVE_IFC_FILE = "save_ifc_file"

QUERY_TOOL_NAMES = (
    TOOL_GET_SCENE_INFO,
    TOOL_GET_SELECTED_OBJECTS,
    TOOL_GET_PSETS,
    TOOL_GET_VIEWPORT_SCREENSHOT,
    TOOL_GET_IFC_PROJECT_INFO,
)
EDIT_TOOL_NAMES = (
    TOOL_EXECUTE_IFC_CODE,
    TOOL_EXECUTE_BLENDER_CODE,
    TOOL_SAVE_IFC_FILE,
)
ALL_TOOL_NAMES = QUERY_TOOL_NAMES + EDIT_TOOL_NAMES


def _format_error(exc: Exception) -> str:
    """Render bridge errors as friendly multi-line text for MCP clients."""
    if isinstance(exc, BlenderBridgeError):
        return f"Blender bridge error: {exc}"
    return f"{type(exc).__name__}: {exc}"


def _as_json(payload: Any) -> str:
    """Pretty-print payloads. MCP clients render this as text content."""
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def tool_get_scene_info(client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None) -> str:
    """Return a scene snapshot, optionally filtered by a query keyword."""
    args = GetSceneInfoInput.model_validate(raw_input or {})
    params: dict[str, Any] = {}
    if args.query:
        params["query"] = args.query
    if args.ifc_class is not None:
        params["ifc_class"] = args.ifc_class
    if args.name is not None:
        params["name"] = args.name
    if args.global_id is not None:
        params["global_id"] = args.global_id
    try:
        result = client.send("get_scene_info", params)
    except Exception as exc:
        return _format_error(exc)
    return _as_json(result)


def tool_get_selected_objects(client: BlenderBridgeClient) -> str:
    """Return basic information about currently selected objects."""
    try:
        result = client.send("get_selected_objects")
    except Exception as exc:
        return _format_error(exc)
    return _as_json(result)


def tool_execute_ifc_code(client: BlenderBridgeClient, raw_input: dict[str, Any]) -> str:
    """Execute IFC code without bpy access."""
    args = ExecuteIfcCodeInput.model_validate(raw_input)
    try:
        result = client.send("execute_ifc_code", {"code": args.code})
    except Exception as exc:
        return _format_error(exc)
    return _as_json(result)


def tool_execute_blender_code(client: BlenderBridgeClient, raw_input: dict[str, Any]) -> str:
    """Execute Python with full Blender access."""
    args = ExecuteCodeInput.model_validate(raw_input)
    try:
        result = client.send("execute_code", {"code": args.code})
    except Exception as exc:
        return _format_error(exc)
    return _as_json(result)


def tool_get_viewport_screenshot(
    client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Capture the active 3D viewport, downscaled to fit MCP response limits."""
    try:
        args = ViewportScreenshotInput.model_validate(raw_input or {})
    except Exception as exc:
        return {"error": _format_error(exc)}
    try:
        result = client.send(
            "get_viewport_screenshot",
            {
                "max_size": args.max_size,
                "format": args.format,
                "quality": args.quality,
                "view": args.view,
                "fit": args.fit,
                "include_objects": args.include_objects,
                "max_objects": args.max_objects,
            },
        )
    except Exception as exc:
        return {"error": _format_error(exc)}
    if not isinstance(result, dict):
        return {"error": f"unexpected screenshot payload: {result!r}"}
    return result


def tool_get_ifc_project_info(client: BlenderBridgeClient) -> str:
    """Return IFC project summary or a friendly error if no IFC is loaded."""
    try:
        result = client.send("get_ifc_project_info")
    except Exception as exc:
        return _format_error(exc)
    return _as_json(result)


def tool_get_psets(client: BlenderBridgeClient, raw_input: dict[str, Any]) -> str:
    """Return IFC property and quantity sets."""
    args = GetPsetsInput.model_validate(raw_input)
    try:
        result = client.send(
            "get_psets",
            {"global_ids": args.global_ids, "names": args.names},
        )
    except Exception as exc:
        return _format_error(exc)
    return _as_json(result)


def tool_save_ifc_file(client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None) -> str:
    """Save the loaded IFC model, in place by default or to output_path (save-as)."""
    args = SaveIfcInput.model_validate(raw_input or {})
    try:
        result = client.send(
            "save_ifc_file",
            {
                "output_path": args.output_path,
                "overwrite": args.overwrite,
                "reload": args.reload,
            },
        )
    except Exception as exc:
        return _format_error(exc)
    return _as_json(result)
