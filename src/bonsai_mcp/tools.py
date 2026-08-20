"""MCP tool adapters.

Each adapter validates input, calls the bridge, and returns the JSON-ready
payload. The server layer renders it as text and structured content.
"""

from __future__ import annotations

import json
from typing import Any

from bonsai_mcp.blender_client import BlenderBridgeClient, BlenderBridgeError
from bonsai_mcp.schemas import (
    ExecuteCodeInput,
    ExecuteIfcCodeInput,
    GetPsetsInput,
    GetQuantitiesInput,
    GetSceneInfoInput,
    GetSelectedObjectsInput,
    GetSpatialStructureInput,
    ListElementsInput,
    RefreshInput,
    SaveIfcInput,
    ViewportScreenshotInput,
)

TOOL_GET_SCENE_INFO = "get_scene_info"
TOOL_GET_SELECTED_OBJECTS = "get_selected_objects"
TOOL_LIST_ELEMENTS = "list_elements"
TOOL_GET_PSETS = "get_psets"
TOOL_GET_VIEWPORT_SCREENSHOT = "get_viewport_screenshot"
TOOL_GET_IFC_PROJECT_INFO = "get_ifc_project_info"
TOOL_GET_SPATIAL_STRUCTURE = "get_spatial_structure"
TOOL_GET_QUANTITIES = "get_quantities"
TOOL_EXECUTE_IFC_CODE = "execute_ifc_code"
TOOL_EXECUTE_BLENDER_CODE = "execute_blender_code"
TOOL_SAVE_IFC_FILE = "save_ifc_file"
TOOL_REFRESH_VIEW = "refresh_view"
TOOL_REFRESH_GEOMETRY = "refresh_geometry"
TOOL_RELOAD_PROJECT = "reload_project"

QUERY_TOOL_NAMES = (
    TOOL_GET_SCENE_INFO,
    TOOL_GET_SELECTED_OBJECTS,
    TOOL_LIST_ELEMENTS,
    TOOL_GET_PSETS,
    TOOL_GET_VIEWPORT_SCREENSHOT,
    TOOL_GET_IFC_PROJECT_INFO,
    TOOL_GET_SPATIAL_STRUCTURE,
    TOOL_GET_QUANTITIES,
)
EDIT_TOOL_NAMES = (
    TOOL_EXECUTE_IFC_CODE,
    TOOL_EXECUTE_BLENDER_CODE,
    TOOL_SAVE_IFC_FILE,
    TOOL_REFRESH_VIEW,
    TOOL_REFRESH_GEOMETRY,
    TOOL_RELOAD_PROJECT,
)
ALL_TOOL_NAMES = QUERY_TOOL_NAMES + EDIT_TOOL_NAMES


class ToolError(RuntimeError):
    """A tool could not complete. The MCP layer reports it with isError=true."""


def _tool_error(exc: Exception) -> ToolError:
    """Wrap a failure in a ToolError with a friendly, single-line prefix."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, BlenderBridgeError):
        return ToolError(f"Blender bridge error: {exc}")
    return ToolError(f"{type(exc).__name__}: {exc}")


def _as_json(payload: Any) -> str:
    """Pretty-print payloads. MCP clients render this as text content."""
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def tool_get_scene_info(
    client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a scene snapshot, optionally filtered by a query keyword."""
    args = GetSceneInfoInput.model_validate(raw_input or {})
    params: dict[str, Any] = {}
    if args.query:
        params["query"] = args.query
        params["limit"] = args.limit
        params["offset"] = args.offset
    if args.ifc_class is not None:
        params["ifc_class"] = args.ifc_class
    if args.name is not None:
        params["name"] = args.name
    if args.global_id is not None:
        params["global_id"] = args.global_id
    try:
        result = client.send("get_scene_info", params)
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected scene info payload: {result!r}")
    objects = result.get("objects")
    if isinstance(objects, list) and "objects_total" not in result:
        # older add-on without bridge-side paging: apply the page here
        result["objects_total"] = len(objects)
        result["objects"] = objects[args.offset : args.offset + args.limit]
        result["objects_truncated"] = len(objects) > args.offset + args.limit
    return result


def tool_get_selected_objects(
    client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return basic information about currently selected objects."""
    args = GetSelectedObjectsInput.model_validate(raw_input or {})
    try:
        result = client.send("get_selected_objects", {"limit": args.limit})
    except Exception as exc:
        raise _tool_error(exc) from exc
    if isinstance(result, list):
        # older add-on returns a bare, uncapped list: apply the cap here
        return {
            "objects": result[: args.limit],
            "total": len(result),
            "truncated": len(result) > args.limit,
        }
    if not isinstance(result, dict):
        raise ToolError(f"unexpected selection payload: {result!r}")
    return result


def tool_list_elements(
    client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """List IFC-backed elements with class/name/storey filters and paging."""
    args = ListElementsInput.model_validate(raw_input or {})
    try:
        result = client.send(
            "list_elements",
            {
                "ifc_class": args.ifc_class,
                "name_contains": args.name_contains,
                "storey": args.storey,
                "selector": args.selector,
                "limit": args.limit,
                "offset": args.offset,
            },
        )
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected list_elements payload: {result!r}")
    return result


def tool_execute_ifc_code(client: BlenderBridgeClient, raw_input: dict[str, Any]) -> dict[str, Any]:
    """Execute IFC code without bpy access."""
    args = ExecuteIfcCodeInput.model_validate(raw_input)
    try:
        result = client.send("execute_ifc_code", {"code": args.code})
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected execution payload: {result!r}")
    return result


def tool_execute_blender_code(
    client: BlenderBridgeClient, raw_input: dict[str, Any]
) -> dict[str, Any]:
    """Execute Python with full Blender access."""
    args = ExecuteCodeInput.model_validate(raw_input)
    try:
        result = client.send("execute_code", {"code": args.code})
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected execution payload: {result!r}")
    return result


def tool_get_viewport_screenshot(
    client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Capture the active 3D viewport, downscaled to fit MCP response limits."""
    args = ViewportScreenshotInput.model_validate(raw_input or {})
    try:
        result = client.send(
            "get_viewport_screenshot",
            {
                "max_size": args.max_size,
                "format": args.format,
                "quality": args.quality,
                "view": args.view,
                "fit": args.fit,
                "azimuth": args.azimuth,
                "elevation": args.elevation,
                "storey": args.storey,
                "shading": args.shading,
                "show_overlays": args.show_overlays,
                "include_objects": args.include_objects,
                "max_objects": args.max_objects,
            },
        )
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected screenshot payload: {result!r}")
    return result


def tool_get_ifc_project_info(client: BlenderBridgeClient) -> dict[str, Any]:
    """Return IFC project summary or a friendly error if no IFC is loaded."""
    try:
        result = client.send("get_ifc_project_info")
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected project info payload: {result!r}")
    return result


def tool_get_spatial_structure(
    client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return the site -> building -> storey -> space hierarchy as a tree."""
    args = GetSpatialStructureInput.model_validate(raw_input or {})
    try:
        result = client.send(
            "get_spatial_structure",
            {"include_element_counts": args.include_element_counts},
        )
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected spatial structure payload: {result!r}")
    return result


def tool_get_quantities(
    client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return a quantity takeoff aggregated by IFC class."""
    args = GetQuantitiesInput.model_validate(raw_input or {})
    try:
        result = client.send(
            "get_quantities",
            {"ifc_classes": args.ifc_classes, "by_storey": args.by_storey},
        )
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected quantities payload: {result!r}")
    return result


def tool_get_psets(client: BlenderBridgeClient, raw_input: dict[str, Any]) -> dict[str, Any]:
    """Return IFC property and quantity sets, paging large target lists."""
    args = GetPsetsInput.model_validate(raw_input)
    targets = [("global_id", gid) for gid in args.global_ids]
    targets += [("name", name) for name in args.names]
    total = len(targets)
    page = targets[args.offset : args.offset + args.limit]
    page_gids = [value for kind, value in page if kind == "global_id"]
    page_names = [value for kind, value in page if kind == "name"]
    try:
        result = client.send(
            "get_psets",
            {"global_ids": page_gids, "names": page_names},
        )
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected psets payload: {result!r}")
    result["targets_total"] = total
    result["offset"] = args.offset
    result["limit"] = args.limit
    truncated = args.offset + len(page) < total
    result["truncated"] = truncated
    if truncated:
        result["next_offset"] = args.offset + len(page)
    return result


def tool_refresh_view(client: BlenderBridgeClient, raw_input: dict[str, Any]) -> dict[str, Any]:
    """Sync the Blender scene after data-only IFC edits. No disk I/O."""
    args = RefreshInput.model_validate(raw_input)
    try:
        result = client.send("refresh_view", {"global_ids": args.global_ids})
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected refresh_view payload: {result!r}")
    return result


def tool_refresh_geometry(
    client: BlenderBridgeClient, raw_input: dict[str, Any]
) -> dict[str, Any]:
    """Rebuild Blender geometry for specific elements. No disk I/O."""
    args = RefreshInput.model_validate(raw_input)
    try:
        result = client.send("refresh_geometry", {"global_ids": args.global_ids})
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected refresh_geometry payload: {result!r}")
    return result


def tool_reload_project(client: BlenderBridgeClient) -> dict[str, Any]:
    """Rebuild the whole scene from the in-memory model. Slow; explicit only."""
    try:
        result = client.send("reload_project", {})
    except Exception as exc:
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected reload_project payload: {result!r}")
    return result


def tool_save_ifc_file(
    client: BlenderBridgeClient, raw_input: dict[str, Any] | None = None
) -> dict[str, Any]:
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
        raise _tool_error(exc) from exc
    if not isinstance(result, dict):
        raise ToolError(f"unexpected save payload: {result!r}")
    return result
