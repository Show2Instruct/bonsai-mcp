"""MCP stdio server."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
from typing import Any

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    ImageContent,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import AnyUrl, BaseModel, ConfigDict

from bonsai_mcp import __version__
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
    input_schema_for,
)
from bonsai_mcp.tools import (
    ALL_TOOL_NAMES,
    EDIT_TOOL_NAMES,
    QUERY_TOOL_NAMES,
    TOOL_EXECUTE_BLENDER_CODE,
    TOOL_EXECUTE_IFC_CODE,
    TOOL_GET_IFC_PROJECT_INFO,
    TOOL_GET_PSETS,
    TOOL_GET_QUANTITIES,
    TOOL_GET_SCENE_INFO,
    TOOL_GET_SELECTED_OBJECTS,
    TOOL_GET_SPATIAL_STRUCTURE,
    TOOL_GET_VIEWPORT_SCREENSHOT,
    TOOL_LIST_ELEMENTS,
    TOOL_REFRESH_GEOMETRY,
    TOOL_REFRESH_VIEW,
    TOOL_RELOAD_PROJECT,
    TOOL_SAVE_IFC_FILE,
    ToolError,
    _as_json,
    tool_execute_blender_code,
    tool_execute_ifc_code,
    tool_get_ifc_project_info,
    tool_get_psets,
    tool_get_quantities,
    tool_get_scene_info,
    tool_get_selected_objects,
    tool_get_spatial_structure,
    tool_get_viewport_screenshot,
    tool_list_elements,
    tool_refresh_geometry,
    tool_refresh_view,
    tool_reload_project,
    tool_save_ifc_file,
)

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[bonsai-mcp] %(message)s")
log = logging.getLogger("bonsai-mcp")


class _EmptyInput(BaseModel):
    """No parameters."""

    model_config = ConfigDict(extra="forbid")


def _output_schema(description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    """A deliberately loose result schema.

    Bridge payloads evolve between add-on versions; the properties listed
    here are hints for clients, not a validation contract, so nothing is
    required and unknown keys are allowed.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "description": description,
        "additionalProperties": True,
    }
    if properties:
        schema["properties"] = properties
    return schema


_PAGED_LIST_PROPS: dict[str, Any] = {
    "total": {"type": "integer"},
    "truncated": {"type": "boolean"},
}

_EXEC_OUTPUT_PROPS: dict[str, Any] = {
    "success": {"type": "boolean"},
    "stdout": {"type": "string"},
    "stderr": {"type": "string"},
    "error": {"type": ["string", "null"]},
    "traceback": {"type": ["string", "null"]},
}

# (name, title, kind, input model, description, output schema, idempotent)
_TOOL_SPECS: tuple[tuple[str, str, str, type[BaseModel], str, dict[str, Any], bool], ...] = (
    (
        TOOL_GET_SCENE_INFO,
        "Get Scene Info",
        "QUERY",
        GetSceneInfoInput,
        (
            "Return a summary of the current Blender scene (scene name, "
            "object count, selection, collections, IFC availability). When "
            "`query` is supplied, the response also includes an `objects` "
            "list filtered by the query, paged via limit/offset. Supported "
            "queries: 'all', 'selected', 'walls', 'doors', 'windows', "
            "'spaces', 'slabs', 'columns', 'beams', 'roofs', 'stairs', "
            "'by_class', 'by_name', 'by_global_id'. For structured element "
            "listings prefer the dedicated list_elements tool."
        ),
        _output_schema(
            "Scene summary, plus objects/objects_total/objects_truncated when a query is given.",
            {
                "scene_name": {"type": "string"},
                "object_count": {"type": "integer"},
                "objects": {"type": "array"},
                "objects_total": {"type": "integer"},
                "objects_truncated": {"type": "boolean"},
            },
        ),
        True,
    ),
    (
        TOOL_GET_SELECTED_OBJECTS,
        "Get Selected Objects",
        "QUERY",
        GetSelectedObjectsInput,
        (
            "Return name, type, location, dimensions, and (if available) "
            "IFC class and GlobalId for each currently selected object. "
            "Results are capped by `limit` with total/truncated reported."
        ),
        _output_schema(
            "Selected object summaries.",
            {"objects": {"type": "array"}, **_PAGED_LIST_PROPS},
        ),
        True,
    ),
    (
        TOOL_LIST_ELEMENTS,
        "List Elements",
        "QUERY",
        ListElementsInput,
        (
            "List IFC-backed elements in the scene with structured filters: "
            "`ifc_class` (inheritance-aware, so 'IfcWall' matches "
            "IfcWallStandardCase), `name_contains`, `storey` (Name or "
            "GlobalId of an IfcBuildingStorey), and `selector` (an "
            "IfcOpenShell selector query, e.g. "
            "'IfcWall, Pset_WallCommon.FireRating=F30', for property/"
            "material/attribute filters without code execution). Paged via "
            "limit/offset with total/truncated reported. Prefer this over "
            "get_scene_info queries for element listings."
        ),
        _output_schema(
            "Matching element summaries.",
            {"elements": {"type": "array"}, **_PAGED_LIST_PROPS},
        ),
        True,
    ),
    (
        TOOL_GET_PSETS,
        "Get Property Sets",
        "QUERY",
        GetPsetsInput,
        (
            "Return IFC property sets (psets) and quantity sets (qtos) for "
            "one or more objects. Accepts any mix of `global_ids` and "
            "`names` lists (Blender object names). Large batches are paged "
            "(limit/offset, with targets_total/truncated/next_offset in the "
            "response). The response preserves input order in a `results` "
            "list."
        ),
        _output_schema(
            "Per-target pset/qto results with paging metadata.",
            {
                "results": {"type": "array"},
                "targets_total": {"type": "integer"},
                "truncated": {"type": "boolean"},
                "next_offset": {"type": "integer"},
            },
        ),
        True,
    ),
    (
        TOOL_GET_VIEWPORT_SCREENSHOT,
        "Get Viewport Screenshot",
        "QUERY",
        ViewportScreenshotInput,
        (
            "Capture the Blender 3D viewport and return the image inline. "
            "Optionally aim the viewport first: `view` sets the direction "
            "(top/bottom/front/back/left/right = orthographic axis views, "
            "'iso' = perspective isometric, 'camera' = scene camera), or use "
            "`azimuth`/`elevation` degrees for arbitrary directions. `fit` "
            "frames the content ('all' or 'selected') with direction-aware "
            "tightening. `storey` isolates one building storey for the shot "
            "(floor plans: storey + view='top' + fit='all'). `shading` "
            "overrides viewport shading; 'class_colors' colors objects by "
            "IFC class and returns a legend. Overlays are hidden by default. "
            "The render is downscaled so the longest edge fits `max_size` "
            "(default 800 px, also capped by the native viewport resolution) "
            "and encoded as JPEG by default; an oversized PNG request is "
            "automatically downgraded to JPEG rather than failing after the "
            "render. Scene render settings, shading, overlays, and "
            "visibility are restored afterwards; the viewport orientation "
            "persists."
        ),
        _output_schema(
            "Screenshot metadata and viewport state (pixels arrive as an image content block).",
            {
                "format": {"type": "string"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "viewport": {"type": "object"},
                "class_legend": {"type": "object"},
                "note": {"type": "string"},
            },
        ),
        True,
    ),
    (
        TOOL_GET_IFC_PROJECT_INFO,
        "Get IFC Project Info",
        "QUERY",
        _EmptyInput,
        (
            "Return IFC schema, project name, counts of sites/buildings/"
            "storeys, counts of common IFC entities, and material and "
            "classification summaries. Returns a clear error if no IFC "
            "project is loaded."
        ),
        _output_schema(
            "Project-level IFC summary.",
            {
                "schema": {"type": "string"},
                "project_name": {"type": ["string", "null"]},
                "entity_counts": {"type": "object"},
            },
        ),
        True,
    ),
    (
        TOOL_GET_SPATIAL_STRUCTURE,
        "Get Spatial Structure",
        "QUERY",
        GetSpatialStructureInput,
        (
            "Return the project's spatial hierarchy as a tree: IfcProject -> "
            "IfcSite -> IfcBuilding -> IfcBuildingStorey -> IfcSpace, with "
            "names, GlobalIds, storey elevations, and (optionally) counts of "
            "contained elements by IFC class. Answers 'what is in this "
            "building, storey by storey' without any code execution."
        ),
        _output_schema(
            "Spatial hierarchy tree.",
            {"schema": {"type": "string"}, "tree": {"type": "object"}},
        ),
        True,
    ),
    (
        TOOL_GET_QUANTITIES,
        "Get Quantities",
        "QUERY",
        GetQuantitiesInput,
        (
            "Quantity takeoff: aggregate IFC base quantities (areas, "
            "volumes, lengths, counts) grouped by IFC class, optionally "
            "broken down per building storey. Sums every numeric quantity "
            "found in each element's quantity sets (e.g. walls' NetSideArea, "
            "slabs' GrossVolume) and reports how many elements carried each "
            "quantity. Answers 'how much wall area / slab volume is in this "
            "model' without any code execution."
        ),
        _output_schema(
            "Aggregated quantities per IFC class.",
            {
                "classes": {"type": "object"},
                "by_storey": {"type": "object"},
                "units": {"type": "object"},
            },
        ),
        True,
    ),
    (
        TOOL_EXECUTE_IFC_CODE,
        "Execute IFC Code",
        "EDIT",
        ExecuteIfcCodeInput,
        (
            "PREFERRED code execution tool. Runs IfcOpenShell / Bonsai API "
            "code with a pre-injected IFC namespace. bpy access is BLOCKED; "
            "use execute_blender_code only when you genuinely need Blender "
            "operations (viewport, rendering, object transforms). "
            "Pre-injected variables: `ifc` (the loaded IFC file or None), "
            "`ifcopenshell`, `ifc_api` (ifcopenshell.api), "
            "`element_util` (ifcopenshell.util.element), "
            "`tool` (bonsai.tool). Pre-injected helper functions: "
            "`get_ifc_file()` (loaded IFC file or raises), "
            "`get_default_container()` (active spatial container). "
            "Edits are not visible in the viewport until refreshed: after "
            "attribute/pset/classification edits call refresh_view with the "
            "affected GlobalIds; after moving elements or changing "
            "representations call refresh_geometry; after creating or "
            "deleting elements call reload_project. Do NOT call "
            "save_ifc_file just to make edits visible. "
            "Use this for: querying IFC entities, reading/writing "
            "properties, traversing the IFC model, calling "
            "ifcopenshell.api operations, and any BIM data work."
        ),
        _output_schema("Execution outcome with captured output.", _EXEC_OUTPUT_PROPS),
        False,
    ),
    (
        TOOL_EXECUTE_BLENDER_CODE,
        "Execute Blender Code",
        "EDIT",
        ExecuteCodeInput,
        (
            "Run arbitrary Python code inside Blender with full bpy access. "
            "Use ONLY when you need Blender-specific operations (viewport "
            "manipulation, rendering, object transforms, modifiers). "
            "For IFC/BIM data operations, ALWAYS prefer execute_ifc_code. "
            "LOCAL TRUSTED USE ONLY."
        ),
        _output_schema("Execution outcome with captured output.", _EXEC_OUTPUT_PROPS),
        False,
    ),
    (
        TOOL_REFRESH_VIEW,
        "Refresh View",
        "EDIT",
        RefreshInput,
        (
            "Sync the Blender scene after DATA-ONLY IFC edits (names, "
            "descriptions, psets, quantities, classifications). Pass the "
            "GlobalIds of the edited elements. Milliseconds even on large "
            "models. Does not write to disk. Does not rebuild geometry. "
            "This is the right tool after execute_ifc_code attribute or "
            "pset edits; never save just to refresh."
        ),
        _output_schema(
            "Per-element sync outcome.",
            {"refreshed": {"type": "integer"}, "results": {"type": "array"}},
        ),
        True,
    ),
    (
        TOOL_REFRESH_GEOMETRY,
        "Refresh Geometry",
        "EDIT",
        RefreshInput,
        (
            "Rebuild Blender geometry and placement for SPECIFIC elements "
            "after geometric IFC edits (moved elements, changed "
            "representations). Pass the affected GlobalIds. Fast and "
            "targeted; does not write to disk; does not touch the rest of "
            "the scene. Newly created or deleted elements are NOT covered: "
            "use reload_project for those."
        ),
        _output_schema(
            "Per-element rebuild outcome.",
            {"refreshed": {"type": "integer"}, "results": {"type": "array"}},
        ),
        True,
    ),
    (
        TOOL_RELOAD_PROJECT,
        "Reload Project",
        "EDIT",
        _EmptyInput,
        (
            "Full scene rebuild from the in-memory IFC model (via a "
            "temporary file; the project file on disk is not modified and "
            "the project path is preserved). SLOW: seconds to minutes on "
            "large models, and it resets selection, visibility, and camera. "
            "Use only when targeted refresh is insufficient: after creating "
            "or deleting elements, or when the scene has genuinely "
            "diverged. Prefer refresh_view / refresh_geometry otherwise."
        ),
        _output_schema(
            "Reload outcome.",
            {
                "reloaded": {"type": "boolean"},
                "project_path": {"type": "string"},
                "path_restored": {"type": "boolean"},
            },
        ),
        True,
    ),
    (
        TOOL_SAVE_IFC_FILE,
        "Save IFC File",
        "EDIT",
        SaveIfcInput,
        (
            "Write the IFC model to disk. Call only when the user "
            "explicitly asks to save; it does NOT refresh the viewport "
            "(use refresh_view / refresh_geometry / reload_project for "
            "that). With no arguments it saves the project back to its own "
            "file (in-place, like File > Save IFC). Pass `output_path` for "
            "a save-as; that refuses to overwrite existing files unless "
            "`overwrite=true`. The legacy `reload=true` flag reloads the "
            "scene from the saved file afterwards; prefer reload_project, "
            "which does not require saving first."
        ),
        _output_schema(
            "Save outcome.",
            {
                "saved": {"type": "boolean"},
                "output_path": {"type": "string"},
                "reloaded": {"type": "boolean"},
            },
        ),
        True,
    ),
)


def _tool_definitions() -> list[Tool]:
    """MCP tool definitions; input schemas are generated from the Pydantic models."""
    tools: list[Tool] = []
    for name, title, kind, model, description, output, idempotent in _TOOL_SPECS:
        is_query = kind == "QUERY"
        tools.append(
            Tool(
                name=name,
                title=title,
                description=f"[{kind}] {description}",
                inputSchema=input_schema_for(model),
                outputSchema=output,
                annotations=ToolAnnotations(
                    readOnlyHint=is_query,
                    destructiveHint=not is_query,
                    idempotentHint=idempotent,
                    openWorldHint=False,
                ),
            )
        )
    return tools


_SERVER_INSTRUCTIONS = """\
You are connected to a Blender + Bonsai (BlenderBIM) session via Bonsai MCP.

## Tool categories

- [QUERY] tools (read-only): `get_scene_info`, `get_selected_objects`,
  `list_elements`, `get_psets`, `get_viewport_screenshot`,
  `get_ifc_project_info`, `get_spatial_structure`, `get_quantities`.
- [EDIT] tools (modify state): `execute_ifc_code`, `execute_blender_code`,
  `save_ifc_file`, `refresh_view`, `refresh_geometry`, `reload_project`.

Reach for QUERY tools first; only use EDIT tools when the user has asked
for a change. Typical BIM questions are answerable without code:
`get_spatial_structure` for "what is in this building, storey by storey",
`get_quantities` for areas/volumes takeoffs, `list_elements` for filtered
element lists (including property/material filters via its `selector`
parameter, e.g. 'IfcWall, Pset_WallCommon.FireRating=F30'), `get_psets`
for properties.

Large result sets are paged: pass `limit`/`offset` and watch the
`total`/`truncated` flags instead of requesting everything at once.

The add-on has an "Allow edits" toggle. When it is off, EDIT tools fail
with a clear error while QUERY tools keep working; tell the user to enable
it in the Blender sidebar (N-panel > Bonsai MCP) if they want edits.

If a tool fails with "Unknown command", the add-on inside Blender is older
than this server: the user should redeploy blender_addon/bonsai_bridge.py
and restart the bridge.

## IFC-first principle for EDIT work

1. **Use `execute_ifc_code`** for ALL IFC/BIM data work: querying entities,
   reading/writing properties, traversing the model, calling ifcopenshell.api.
   This tool pre-injects `ifc`, `ifcopenshell`, `ifc_api`, `element_util`,
   and `tool` (bonsai.tool). No imports needed.

2. **Use `execute_blender_code`** ONLY when you genuinely need Blender-specific
   operations: viewport manipulation, rendering, object transforms, modifiers,
   or anything that requires `bpy`.

3. Use the dedicated query tools before writing code. They handle common
   lookups without custom scripts.

## Available namespace in execute_ifc_code

- `ifc`: the currently loaded IfcOpenShell file object (or None)
- `ifcopenshell`: the ifcopenshell module
- `ifc_api`: ifcopenshell.api (high-level operations)
- `element_util`: ifcopenshell.util.element (psets, qtos, traversal)
- `tool`: bonsai.tool module (Bonsai's internal API, or None)
- `get_ifc_file()`: return the loaded IFC file, raising if none is open
- `get_default_container()`: return the active spatial container
- `save_and_load_ifc(path=None)`: legacy helper (save then full reload);
  prefer the refresh tools below

The same three helper functions are also injected into execute_blender_code.

bpy is blocked in execute_ifc_code. If code needs bpy, it belongs in
execute_blender_code.

## Viewport sync after IFC edits: pick the cheapest tier

Edits made through `execute_ifc_code` modify the in-memory IFC model but do
NOT appear in the Blender viewport until refreshed. Route by what the edit
touched, cheapest first:

1. `refresh_view` (milliseconds): after data-only edits - names,
   descriptions, psets, quantities, classifications. Pass the affected
   GlobalIds.
2. `refresh_geometry` (fast, targeted): after moving elements or changing
   representations. Pass the affected GlobalIds.
3. `reload_project` (SLOW: seconds to minutes on large models; resets
   selection, visibility, and camera): after creating or deleting elements,
   or when the scene has genuinely diverged. Never call it implicitly.

NEVER call save_ifc_file just to make edits visible: refreshing and saving
are decoupled. MCP edits are outside Blender's undo stack (Ctrl+Z will not
revert them); warn the user before large destructive edits.

## Saving (durability only)

- Saving is for durability, not visibility. Call `save_ifc_file` when the
  user asks to save; the user can also just press Ctrl+S in Blender.
- `save_ifc_file` with no arguments: save the project to its own file.
- `save_ifc_file` with `output_path`: save-as to a new location (refuses to
  overwrite unless `overwrite=true`).

## Screenshots

`get_viewport_screenshot` downscales to `max_size` (default 800 px, JPEG).
Request a larger `max_size` or `format='png'` only when you need detail;
an oversized PNG is automatically downgraded to JPEG with a note.

Aim before you shoot: pass `view` ('top', 'front', 'right', ..., 'iso',
'camera') or `azimuth`/`elevation` degrees, and/or `fit` ('all' or
'selected') to orient and frame the viewport in the same call. Framing is
direction-aware, so elevations fill the frame. For a floor plan of one
storey, pass `storey` (Name or GlobalId) with `view='top'`, `fit='all'`.
`shading='class_colors'` colors objects by IFC class and returns a legend,
which makes walls/slabs/doors trivially distinguishable in the image.

A typical visual-verification loop: select or edit objects, `save_ifc_file`
with `reload=true` if you made IFC edits, then
`get_viewport_screenshot(view='iso', fit='all')`.

Every screenshot response also includes structured viewport state as text
(rotation quaternion, perspective mode, view distance, pivot), so the
applied view is verifiable without vision. If your client does not deliver
tool-result images to you (some MCP clients drop them; the text line
reports how many base64 chars were attached), pass `include_objects=true`
to get screen-space 2D bounding boxes and view depths keyed by GlobalId
(`box` = [x_min, y_min, x_max, y_max], normalized 0-1, origin top-left,
smaller y is higher on screen; `depth` = distance from the viewpoint).
That enables spatial reasoning entirely from text: containment,
left/right/above/below relations, relative sizes, and near/far ordering.
"""


_RESOURCES: tuple[tuple[str, str, str], ...] = (
    (
        "bonsai://project",
        "IFC project info",
        "IFC schema, project name, entity counts, materials, classifications.",
    ),
    (
        "bonsai://scene",
        "Blender scene summary",
        "Scene name, object counts, selection, collections, IFC availability.",
    ),
)

_ELEMENT_PSETS_TEMPLATE = "bonsai://element/{global_id}/psets"
_ELEMENT_PSETS_RE = re.compile(r"^bonsai://element/([^/]+)/psets$")


def _read_resource_payload(client: BlenderBridgeClient, uri: str) -> dict[str, Any]:
    """Resolve a bonsai:// resource URI to its JSON payload."""
    normalized = uri.rstrip("/")
    if normalized == "bonsai://project":
        return tool_get_ifc_project_info(client)
    if normalized == "bonsai://scene":
        return tool_get_scene_info(client, {})
    match = _ELEMENT_PSETS_RE.match(normalized)
    if match:
        return tool_get_psets(client, {"global_ids": [match.group(1)]})
    raise ValueError(f"Unknown resource URI: {uri!r}")


_PROMPT_MODEL_AUDIT = """\
Audit the IFC model currently open in Blender and produce a structured report.

Work through these steps with the Bonsai MCP query tools (no code execution
should be needed):

1. `get_ifc_project_info`: schema, project name, entity counts, materials,
   classifications. Note anything unusual (zero storeys, missing project name).
2. `get_spatial_structure`: walk the site -> building -> storey -> space
   tree. Report the storey list with elevations and the element counts per
   storey.
3. `get_quantities` (optionally `by_storey=true`): report wall areas, slab
   volumes, and other available base quantities. Flag classes where many
   elements carry no quantities (poor model quality signal).
4. Spot-check `get_psets` on a handful of representative elements from
   different classes and storeys{focus_clause}. Flag missing or suspicious
   property sets (e.g. no Pset_WallCommon on walls, missing FireRating).
5. `get_viewport_screenshot(view='iso', fit='all')` for an overview image,
   and one `storey` floor plan per storey if the model is small enough.

Finish with a concise report: model facts, quality findings ordered by
severity, and recommended fixes. Do not modify the model.
"""

_PROMPT_VISUAL_VERIFY = """\
Visually verify the most recent model edit{change_clause}.

1. If the edit was made through `execute_ifc_code` and not yet reloaded,
   call `save_ifc_file` with `reload=true` once (this rebuilds the scene so
   IFC edits become visible). Skip if nothing was edited.
2. Capture `get_viewport_screenshot(view='iso', fit='all')` for context.
3. Capture a tighter shot of the affected area: `fit='selected'` if the
   relevant objects are selected, a `storey` floor plan for layout changes,
   or an elevation (`view='front'`/`'right'`) for height changes. Use
   `shading='class_colors'` when class distinctions matter.
4. Compare what the screenshots show against what the edit was supposed to
   do; use `include_objects=true` boxes when the image channel is
   unreliable. Confirm both the intended change and the absence of obvious
   collateral damage (missing elements, displaced geometry).
5. Report: what changed, what the screenshots confirm, and any mismatch
   that needs a follow-up edit.
"""

_PROMPTS: tuple[Prompt, ...] = (
    Prompt(
        name="model-audit",
        title="Model audit",
        description=(
            "Walk the loaded IFC model with the query tools (project info, "
            "spatial tree, quantities, pset spot-checks, screenshots) and "
            "produce a quality report."
        ),
        arguments=[
            PromptArgument(
                name="focus",
                description="Optional focus area, e.g. 'walls', 'fire safety', 'storey 2'.",
                required=False,
            )
        ],
    ),
    Prompt(
        name="visual-verify",
        title="Visual verify",
        description=(
            "Verify the latest edit visually: reload if needed, take "
            "overview and detail screenshots, compare against the intent."
        ),
        arguments=[
            PromptArgument(
                name="what_changed",
                description="Optional description of the edit to verify.",
                required=False,
            )
        ],
    ),
)


def _prompt_text(name: str, arguments: dict[str, str] | None) -> str:
    args = arguments or {}
    if name == "model-audit":
        focus = (args.get("focus") or "").strip()
        focus_clause = f", prioritising {focus}" if focus else ""
        return _PROMPT_MODEL_AUDIT.format(focus_clause=focus_clause)
    if name == "visual-verify":
        change = (args.get("what_changed") or "").strip()
        change_clause = f": {change}" if change else ""
        return _PROMPT_VISUAL_VERIFY.format(change_clause=change_clause)
    raise ValueError(f"Unknown prompt: {name!r}")


def build_server(client: BlenderBridgeClient | None = None) -> Server:
    """Create the MCP server."""
    client = client or BlenderBridgeClient()
    server: Server = Server("bonsai-mcp", instructions=_SERVER_INSTRUCTIONS)

    async def _send_progress(progress: float, message: str) -> None:
        """Best-effort coarse progress; a no-op unless the client sent a token."""
        try:
            ctx = server.request_context
            token = ctx.meta.progressToken if ctx.meta is not None else None
            if token is None:
                return
            await ctx.session.send_progress_notification(
                token, progress, total=1.0, message=message
            )
        except Exception:
            pass

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return _tool_definitions()

    @server.call_tool()
    async def _call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> tuple[list[TextContent | ImageContent], dict[str, Any]]:
        # the bridge client blocks on socket I/O; a worker thread keeps the
        # asyncio loop responsive while a tool call is in flight
        await _send_progress(0.0, f"Running {name} in Blender")
        try:
            result = await asyncio.to_thread(_dispatch_tool, client, name, arguments or {})
        finally:
            await _send_progress(1.0, f"{name} finished")
        return result

    @server.list_resources()
    async def _list_resources() -> list[Resource]:
        return [
            Resource(
                uri=AnyUrl(uri),
                name=name,
                description=description,
                mimeType="application/json",
            )
            for uri, name, description in _RESOURCES
        ]

    @server.list_resource_templates()
    async def _list_resource_templates() -> list[ResourceTemplate]:
        return [
            ResourceTemplate(
                uriTemplate=_ELEMENT_PSETS_TEMPLATE,
                name="Element property sets",
                description=(
                    "Property and quantity sets for one element, addressed "
                    "by its IFC GlobalId."
                ),
                mimeType="application/json",
            )
        ]

    @server.read_resource()
    async def _read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
        payload = await asyncio.to_thread(_read_resource_payload, client, str(uri))
        return [ReadResourceContents(content=_as_json(payload), mime_type="application/json")]

    @server.list_prompts()
    async def _list_prompts() -> list[Prompt]:
        return list(_PROMPTS)

    @server.get_prompt()
    async def _get_prompt(name: str, arguments: dict[str, str] | None) -> GetPromptResult:
        text = _prompt_text(name, arguments)
        prompt = next((p for p in _PROMPTS if p.name == name), None)
        return GetPromptResult(
            description=prompt.description if prompt else None,
            messages=[
                PromptMessage(role="user", content=TextContent(type="text", text=text))
            ],
        )

    return server


# stripped from structured screenshot output: the pixels already ship as an
# image block, and the bridge-side temp path is meaningless to clients
_SCREENSHOT_STRUCTURED_EXCLUDES = ("image_base64", "path")


def _dispatch_tool(
    client: BlenderBridgeClient, name: str, arguments: dict[str, Any]
) -> tuple[list[TextContent | ImageContent], dict[str, Any]]:
    """Route a tool call to its handler; return (content blocks, structured result)."""
    if name == TOOL_GET_VIEWPORT_SCREENSHOT:
        payload = tool_get_viewport_screenshot(client, arguments)
        structured = {
            k: v for k, v in payload.items() if k not in _SCREENSHOT_STRUCTURED_EXCLUDES
        }
        return _screenshot_to_mcp_content(payload), structured

    handlers = {
        TOOL_GET_SCENE_INFO: lambda: tool_get_scene_info(client, arguments),
        TOOL_GET_SELECTED_OBJECTS: lambda: tool_get_selected_objects(client, arguments),
        TOOL_LIST_ELEMENTS: lambda: tool_list_elements(client, arguments),
        TOOL_GET_PSETS: lambda: tool_get_psets(client, arguments),
        TOOL_GET_IFC_PROJECT_INFO: lambda: tool_get_ifc_project_info(client),
        TOOL_GET_SPATIAL_STRUCTURE: lambda: tool_get_spatial_structure(client, arguments),
        TOOL_GET_QUANTITIES: lambda: tool_get_quantities(client, arguments),
        TOOL_EXECUTE_IFC_CODE: lambda: tool_execute_ifc_code(client, arguments),
        TOOL_EXECUTE_BLENDER_CODE: lambda: tool_execute_blender_code(client, arguments),
        TOOL_SAVE_IFC_FILE: lambda: tool_save_ifc_file(client, arguments),
        TOOL_REFRESH_VIEW: lambda: tool_refresh_view(client, arguments),
        TOOL_REFRESH_GEOMETRY: lambda: tool_refresh_geometry(client, arguments),
        TOOL_RELOAD_PROJECT: lambda: tool_reload_project(client),
    }
    handler = handlers.get(name)
    if handler is None:
        # raising lets the MCP layer mark the result isError=true
        raise ValueError(f"Unknown tool: {name!r}")
    payload = handler()
    return [TextContent(type="text", text=_as_json(payload))], payload


def _screenshot_to_mcp_content(
    payload: dict[str, Any],
) -> list[TextContent | ImageContent]:
    """Translate the bridge's screenshot payload into MCP content blocks."""
    if "error" in payload:
        raise ToolError(str(payload["error"]))

    image_b64 = payload.get("image_base64")
    image_format = (payload.get("format") or "png").lower()

    out: list[TextContent | ImageContent] = []
    if image_b64:
        try:
            base64.b64decode(image_b64, validate=True)
        except Exception as exc:
            raise ToolError(f"Screenshot decode failed: {exc}") from exc
        # image first: some MCP clients mishandle mixed content
        out.append(
            ImageContent(
                type="image",
                data=image_b64,
                mimeType=f"image/{image_format}",
            )
        )
    notes: list[str] = []
    if image_b64:
        width = payload.get("width")
        height = payload.get("height")
        size_note = f", {width}x{height} px" if width and height else ""
        # the length makes a client-side image drop diagnosable from text;
        # the temp-file path is deliberately omitted (dead tokens client-side)
        notes.append(
            f"Image block attached: {len(image_b64)} base64 chars of "
            f"image/{image_format}{size_note}. If no image is visible above, "
            "the MCP client dropped it; use the viewport state below (retry "
            "with include_objects=true for per-object boxes) instead of the "
            "pixels."
        )
    note = payload.get("note")
    if note:
        notes.append(str(note))
    legend = payload.get("class_legend")
    if legend:
        notes.append("Class colors (RGB 0-1):\n" + json.dumps(legend, indent=2, default=str))
    viewport = payload.get("viewport")
    if viewport is not None:
        notes.append("Viewport state:\n" + json.dumps(viewport, indent=2, default=str))
    if notes:
        out.append(TextContent(type="text", text="\n".join(notes)))
    if not out:
        raise ToolError("Screenshot returned no payload.")
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
