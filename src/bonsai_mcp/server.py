"""MCP stdio server."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool, ToolAnnotations

from bonsai_mcp import __version__
from bonsai_mcp.blender_client import BlenderBridgeClient, BlenderBridgeError
from bonsai_mcp.tools import (
    ALL_TOOL_NAMES,
    EDIT_TOOL_NAMES,
    QUERY_TOOL_NAMES,
    TOOL_EXECUTE_BLENDER_CODE,
    TOOL_EXECUTE_IFC_CODE,
    TOOL_GET_IFC_PROJECT_INFO,
    TOOL_GET_PSETS,
    TOOL_GET_SCENE_INFO,
    TOOL_GET_SELECTED_OBJECTS,
    TOOL_GET_VIEWPORT_SCREENSHOT,
    TOOL_SAVE_IFC_FILE,
    tool_execute_blender_code,
    tool_execute_ifc_code,
    tool_get_ifc_project_info,
    tool_get_psets,
    tool_get_scene_info,
    tool_get_selected_objects,
    tool_get_viewport_screenshot,
    tool_save_ifc_file,
)

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[bonsai-mcp] %(message)s")
log = logging.getLogger("bonsai-mcp")


def _query_tool(name: str, description: str, input_schema: dict) -> Tool:
    """Build a Tool definition flagged as read-only (QUERY)."""
    return Tool(
        name=name,
        description=f"[QUERY] {description}",
        inputSchema=input_schema,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )


def _edit_tool(
    name: str,
    description: str,
    input_schema: dict,
    *,
    destructive: bool = True,
) -> Tool:
    """Build a Tool definition flagged as a write/edit operation (EDIT)."""
    return Tool(
        name=name,
        description=f"[EDIT] {description}",
        inputSchema=input_schema,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=destructive),
    )


def _tool_definitions() -> list[Tool]:
    """Static MCP tool definitions. Schemas use plain JSON Schema (no Pydantic refs)."""
    return [
        _query_tool(
            TOOL_GET_SCENE_INFO,
            description=(
                "Return a summary of the current Blender scene (scene name, "
                "object count, selection, collections, IFC availability). When "
                "`query` is supplied, the response also includes an `objects` "
                "list filtered by the query. Supported queries: 'all', "
                "'selected', 'walls', 'doors', 'windows', 'spaces', 'slabs', "
                "'columns', 'beams', 'roofs', 'stairs', 'by_class', 'by_name', "
                "'by_global_id'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional object filter. Omit for scene summary only.",
                    },
                    "ifc_class": {"type": "string"},
                    "name": {"type": "string"},
                    "global_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        ),
        _query_tool(
            TOOL_GET_SELECTED_OBJECTS,
            description=(
                "Return name, type, location, dimensions, and (if available) "
                "IFC class and GlobalId for each currently selected object."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _query_tool(
            TOOL_GET_PSETS,
            description=(
                "Return IFC property sets (psets) and quantity sets (qtos) for "
                "one or more objects. Accepts any mix of `global_ids` and "
                "`names` lists (Blender object names). Up to 100 targets per "
                "call. The response preserves input order in a `results` list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "global_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "names": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "additionalProperties": False,
            },
        ),
        _query_tool(
            TOOL_GET_VIEWPORT_SCREENSHOT,
            description=(
                "Capture the Blender 3D viewport and return the image inline. "
                "Optionally aim the viewport first: `view` sets the direction "
                "(top/bottom/front/back/left/right = orthographic axis views, "
                "'iso' = perspective isometric, 'camera' = scene camera) and "
                "`fit` frames the content ('all' or 'selected'). The render is "
                "downscaled so the longest edge fits `max_size` (default 800 "
                "px) and encoded as JPEG by default, keeping the response "
                "safely under MCP size limits. Scene render settings are "
                "restored afterwards; the viewport orientation persists."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "max_size": {
                        "type": "integer",
                        "default": 800,
                        "minimum": 64,
                        "maximum": 2048,
                        "description": (
                            "Longest image edge in pixels. The render is only "
                            "downscaled, never upscaled. Values above ~1200 with "
                            "format='png' may exceed the response size cap."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": ["jpeg", "png"],
                        "default": "jpeg",
                        "description": "'jpeg' (default, small) or 'png' (lossless, large).",
                    },
                    "quality": {
                        "type": "integer",
                        "default": 85,
                        "minimum": 1,
                        "maximum": 100,
                        "description": "JPEG quality. Ignored for png.",
                    },
                    "view": {
                        "type": "string",
                        "enum": [
                            "top",
                            "bottom",
                            "front",
                            "back",
                            "left",
                            "right",
                            "iso",
                            "camera",
                        ],
                        "description": (
                            "Aim the viewport before capturing. Axis names give "
                            "orthographic views; 'iso' a perspective isometric; "
                            "'camera' the scene camera. Omit to keep the current "
                            "orientation."
                        ),
                    },
                    "fit": {
                        "type": "string",
                        "enum": ["all", "selected"],
                        "description": (
                            "Frame content before capturing: 'all' = everything, "
                            "'selected' = current selection. Combines with view."
                        ),
                    },
                    "include_objects": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Also return, as text, screen-space 2D bounding "
                            "boxes keyed by GlobalId for objects in frame "
                            "(normalized 0-1, origin top-left). Use this for "
                            "spatial reasoning when the image channel is "
                            "unavailable or for grounding what the image shows."
                        ),
                    },
                    "max_objects": {
                        "type": "integer",
                        "default": 50,
                        "minimum": 1,
                        "maximum": 200,
                        "description": (
                            "Cap for include_objects; largest boxes are kept."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
        _query_tool(
            TOOL_GET_IFC_PROJECT_INFO,
            description=(
                "Return IFC schema, project name, counts of sites/buildings/"
                "storeys, counts of common IFC entities, and material and "
                "classification summaries. Returns a clear error if no IFC "
                "project is loaded."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        _edit_tool(
            TOOL_EXECUTE_IFC_CODE,
            description=(
                "PREFERRED code execution tool. Runs IfcOpenShell / Bonsai API "
                "code with a pre-injected IFC namespace. bpy access is BLOCKED; "
                "use execute_blender_code only when you genuinely need Blender "
                "operations (viewport, rendering, object transforms). "
                "Pre-injected variables: `ifc` (the loaded IFC file or None), "
                "`ifcopenshell`, `ifc_api` (ifcopenshell.api), "
                "`element_util` (ifcopenshell.util.element), "
                "`tool` (bonsai.tool). Pre-injected helper functions: "
                "`get_ifc_file()` (loaded IFC file or raises), "
                "`get_default_container()` (active spatial container), "
                "`save_and_load_ifc(path=None)` (save the project and reload "
                "it; call this after IFC edits to make them visible in the "
                "viewport, since edits do NOT appear until the project is reloaded). "
                "Use this for: querying IFC entities, reading/writing "
                "properties, traversing the IFC model, calling "
                "ifcopenshell.api operations, and any BIM data work."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "Python source using IfcOpenShell/Bonsai APIs. "
                            "bpy imports are rejected."
                        ),
                    }
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        ),
        _edit_tool(
            TOOL_EXECUTE_BLENDER_CODE,
            description=(
                "Run arbitrary Python code inside Blender with full bpy access. "
                "Use ONLY when you need Blender-specific operations (viewport "
                "manipulation, rendering, object transforms, modifiers). "
                "For IFC/BIM data operations, ALWAYS prefer execute_ifc_code. "
                "LOCAL TRUSTED USE ONLY."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to execute."}
                },
                "required": ["code"],
                "additionalProperties": False,
            },
        ),
        _edit_tool(
            TOOL_SAVE_IFC_FILE,
            description=(
                "Save the loaded IFC model. With no arguments it saves the "
                "project back to its own file (in-place, like File > Save IFC). "
                "Pass `output_path` for a save-as; that refuses to overwrite "
                "existing files unless `overwrite=true`. Pass `reload=true` to "
                "reload the project from the saved file afterwards, which "
                "rebuilds the Blender scene so IFC-level edits become visible "
                "in the viewport."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": (
                            "Optional save-as path. Omit to save the project to "
                            "its own file."
                        ),
                    },
                    "overwrite": {"type": "boolean", "default": False},
                    "reload": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Reload the project after saving so the viewport "
                            "reflects IFC edits."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
    ]


_SERVER_INSTRUCTIONS = """\
You are connected to a Blender + Bonsai (BlenderBIM) session via Bonsai MCP.

## Tool categories

- [QUERY] tools (read-only): `get_scene_info`, `get_selected_objects`,
  `get_psets`, `get_viewport_screenshot`, `get_ifc_project_info`.
- [EDIT] tools (modify state): `execute_ifc_code`, `execute_blender_code`,
  `save_ifc_file`.

Reach for QUERY tools first; only use EDIT tools when the user has asked
for a change.

## IFC-first principle for EDIT work

1. **Use `execute_ifc_code`** for ALL IFC/BIM data work: querying entities,
   reading/writing properties, traversing the model, calling ifcopenshell.api.
   This tool pre-injects `ifc`, `ifcopenshell`, `ifc_api`, `element_util`,
   and `tool` (bonsai.tool). No imports needed.

2. **Use `execute_blender_code`** ONLY when you genuinely need Blender-specific
   operations: viewport manipulation, rendering, object transforms, modifiers,
   or anything that requires `bpy`.

3. Use the dedicated query tools (`get_scene_info`, `get_psets`,
   `get_ifc_project_info`) before writing code. They handle common lookups
   without custom scripts.

## Available namespace in execute_ifc_code

- `ifc`: the currently loaded IfcOpenShell file object (or None)
- `ifcopenshell`: the ifcopenshell module
- `ifc_api`: ifcopenshell.api (high-level operations)
- `element_util`: ifcopenshell.util.element (psets, qtos, traversal)
- `tool`: bonsai.tool module (Bonsai's internal API, or None)
- `get_ifc_file()`: return the loaded IFC file, raising if none is open
- `get_default_container()`: return the active spatial container
- `save_and_load_ifc(path=None)`: save the project (to its own file by
  default) and reload it, rebuilding the Blender scene

The same three helper functions are also injected into execute_blender_code.

bpy is blocked in execute_ifc_code. If code needs bpy, it belongs in
execute_blender_code.

## Viewport sync after IFC edits

Edits made through `execute_ifc_code` (ifcopenshell.api calls, attribute
changes, new entities) modify the in-memory IFC model but do NOT appear in
the Blender viewport until the project is reloaded. After completing a batch
of IFC edits, call `save_and_load_ifc()` inside execute_ifc_code, or use
`save_ifc_file` with `reload=true`. Do this once per batch, not per edit,
because reloading clears and rebuilds the whole scene.

## Saving

- `save_ifc_file` with no arguments: save the project to its own file.
- `save_ifc_file` with `output_path`: save-as to a new location (refuses to
  overwrite unless `overwrite=true`).
- Add `reload=true` to also refresh the viewport from the saved file.

## Screenshots

`get_viewport_screenshot` downscales to `max_size` (default 800 px, JPEG).
Request a larger `max_size` or `format='png'` only when you need detail;
oversized results are rejected to protect the MCP response size cap.

Aim before you shoot: pass `view` ('top', 'front', 'right', ..., 'iso',
'camera') and/or `fit` ('all' or 'selected') to orient and frame the
viewport in the same call, with no execute_blender_code needed. A typical
visual-verification loop: select or edit objects, `save_ifc_file` with
`reload=true` if you made IFC edits, then
`get_viewport_screenshot(view='iso', fit='all')`.

Every screenshot response also includes structured viewport state as text
(rotation quaternion, perspective mode, view distance, pivot), so the
applied view is verifiable without vision. If your client does not deliver
tool-result images to you (some MCP clients drop them; the text line
reports how many base64 chars were attached), pass `include_objects=true`
to get screen-space 2D bounding boxes keyed by GlobalId (`box` =
[x_min, y_min, x_max, y_max], normalized 0-1, origin top-left, smaller y
is higher on screen). That enables spatial reasoning entirely from text:
containment, left/right/above/below relations, and relative sizes.
"""


def build_server(client: BlenderBridgeClient | None = None) -> Server:
    """Create the MCP server."""
    client = client or BlenderBridgeClient()
    server: Server = Server("bonsai-mcp", instructions=_SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return _tool_definitions()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[TextContent | ImageContent]:
        return _dispatch_tool(client, name, arguments or {})

    return server


def _dispatch_tool(
    client: BlenderBridgeClient, name: str, arguments: dict[str, Any]
) -> list[TextContent | ImageContent]:
    """Route a tool call to its handler."""
    if name == TOOL_GET_SCENE_INFO:
        return [TextContent(type="text", text=tool_get_scene_info(client, arguments))]

    if name == TOOL_GET_SELECTED_OBJECTS:
        return [TextContent(type="text", text=tool_get_selected_objects(client))]

    if name == TOOL_GET_PSETS:
        return [TextContent(type="text", text=tool_get_psets(client, arguments))]

    if name == TOOL_GET_VIEWPORT_SCREENSHOT:
        payload = tool_get_viewport_screenshot(client, arguments)
        return _screenshot_to_mcp_content(payload)

    if name == TOOL_GET_IFC_PROJECT_INFO:
        return [TextContent(type="text", text=tool_get_ifc_project_info(client))]

    if name == TOOL_EXECUTE_IFC_CODE:
        return [TextContent(type="text", text=tool_execute_ifc_code(client, arguments))]

    if name == TOOL_EXECUTE_BLENDER_CODE:
        return [TextContent(type="text", text=tool_execute_blender_code(client, arguments))]

    if name == TOOL_SAVE_IFC_FILE:
        return [TextContent(type="text", text=tool_save_ifc_file(client, arguments))]

    return [TextContent(type="text", text=f"Unknown tool: {name!r}")]


def _screenshot_to_mcp_content(
    payload: dict[str, Any],
) -> list[TextContent | ImageContent]:
    """Translate the bridge's screenshot payload into MCP content blocks."""
    if "error" in payload:
        return [TextContent(type="text", text=str(payload["error"]))]

    image_b64 = payload.get("image_base64")
    image_format = (payload.get("format") or "png").lower()
    path = payload.get("path")

    out: list[TextContent | ImageContent] = []
    if image_b64:
        try:
            base64.b64decode(image_b64, validate=True)
        except Exception as exc:
            out.append(TextContent(type="text", text=f"Screenshot decode failed: {exc}"))
            return out
        # image first: some MCP clients mishandle mixed content
        out.append(
            ImageContent(
                type="image",
                data=image_b64,
                mimeType=f"image/{image_format}",
            )
        )
    notes: list[str] = []
    if path:
        width = payload.get("width")
        height = payload.get("height")
        size_note = f" ({width}x{height}, {payload.get('bytes')} bytes)" if width and height else ""
        notes.append(f"Viewport saved to: {path}{size_note}")
    if image_b64:
        # the length makes a client-side image drop diagnosable from text
        notes.append(
            f"Image block attached: {len(image_b64)} base64 chars of "
            f"image/{image_format}. If no image is visible above, the MCP "
            "client dropped it; use the viewport state below (retry with "
            "include_objects=true for per-object boxes) instead of the pixels."
        )
    viewport = payload.get("viewport")
    if viewport is not None:
        notes.append("Viewport state:\n" + json.dumps(viewport, indent=2, default=str))
    if notes:
        out.append(TextContent(type="text", text="\n".join(notes)))
    if not out:
        out.append(TextContent(type="text", text="Screenshot returned no payload."))
    return out


async def _run() -> None:
    log.info("bonsai-mcp %s starting", __version__)
    log.info("query tools: %s", ", ".join(QUERY_TOOL_NAMES))
    log.info("edit tools:  %s", ", ".join(EDIT_TOOL_NAMES))
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio_server() -> None:
    """Blocking entry point that starts the MCP stdio server."""
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    except BlenderBridgeError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from exc


def main() -> None:
    """Console entry point for `bonsai-mcp`."""
    from bonsai_mcp.cli import main as cli_main

    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()


__all__ = [
    "ALL_TOOL_NAMES",
    "EDIT_TOOL_NAMES",
    "QUERY_TOOL_NAMES",
    "build_server",
    "main",
    "run_stdio_server",
]
