"""Wire protocol and tool schemas."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def input_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for a tool input, generated from its Pydantic model.

    Single source of truth: the hand-written schemas this replaces had
    already drifted from the models. Titles are dropped as noise; $defs and
    $ref pairs are inlined because some MCP clients reject non-local
    references; the root description is dropped because the Tool description
    carries the prose.
    """
    schema = deepcopy(model.model_json_schema())
    defs = schema.pop("$defs", {})

    def _clean(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                inlined = deepcopy(defs.get(ref.rsplit("/", 1)[-1], {}))
                inlined.update({k: v for k, v in node.items() if k != "$ref"})
                node = inlined
            node.pop("title", None)
            return {k: _clean(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_clean(v) for v in node]
        return node

    cleaned = _clean(schema)
    cleaned.pop("description", None)
    return cleaned


class BridgeRequest(BaseModel):
    """One request sent to the Blender bridge."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., description="Command name routed inside the Blender add-on.")
    params: dict[str, Any] = Field(default_factory=dict, description="Arbitrary JSON params.")
    id: int | None = Field(
        None,
        description=(
            "Optional request id echoed back in the response, enabling "
            "correlation on a persistent connection. Older add-ons ignore it."
        ),
    )
    token: str | None = Field(
        None,
        description=(
            "Optional shared secret. Required only when the add-on has a "
            "token configured in its preferences."
        ),
    )


class BridgeResponse(BaseModel):
    """One response from the Blender bridge."""

    model_config = ConfigDict(extra="allow")

    success: bool
    result: Any | None = None
    error: str | None = None
    traceback: str | None = None


class ExecuteCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        ...,
        description=(
            "Python source to execute inside Blender's interpreter. "
            "Pre-injected: `bpy`, plus the helpers `get_ifc_file()`, "
            "`get_default_container()`, and `save_and_load_ifc()`. Other "
            "modules (bonsai, ifcopenshell) are importable but not pre-bound. "
            "Use this ONLY when you need bpy/Blender operations. "
            "For IFC data queries and manipulation, prefer execute_ifc_code."
        ),
    )


class ExecuteIfcCodeInput(BaseModel):
    """Input for execute_ifc_code. IFC-only code execution with bpy blocked."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        ...,
        description=(
            "Python source to execute using IfcOpenShell and Bonsai APIs. "
            "bpy access is BLOCKED. Use execute_blender_code if you need it. "
            "Pre-injected namespace: `ifc` (loaded IFC file), `ifcopenshell`, "
            "`ifc_api` (ifcopenshell.api), `element_util` (ifcopenshell.util.element), "
            "`tool` (bonsai.tool)."
        ),
    )


class GetSceneInfoInput(BaseModel):
    """Scene query options."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(
        None,
        description=(
            "Optional object filter. Supported values: "
            "'all', 'selected', 'walls', 'doors', 'windows', 'spaces', "
            "'slabs', 'columns', 'beams', 'roofs', 'stairs', "
            "'by_class', 'by_name', 'by_global_id'. "
            "When omitted, the response is just the scene summary."
        ),
    )
    ifc_class: str | None = Field(
        None, description="Used when query='by_class', e.g. 'IfcWall'."
    )
    name: str | None = Field(
        None, description="Used when query='by_name'. Exact object name match."
    )
    global_id: str | None = Field(
        None, description="Used when query='by_global_id'. IFC GlobalId string."
    )
    limit: int = Field(
        200,
        ge=1,
        le=1000,
        description=(
            "Maximum number of objects returned by a query (the summary "
            "itself is unaffected). The response reports objects_total and "
            "objects_truncated; page with offset."
        ),
    )
    offset: int = Field(
        0, ge=0, description="Number of matching objects to skip (for paging)."
    )


class ListElementsInput(BaseModel):
    """Filters for the list_elements query."""

    model_config = ConfigDict(extra="forbid")

    ifc_class: str | None = Field(
        None,
        description=(
            "Filter by IFC class, inheritance-aware: 'IfcWall' also matches "
            "IfcWallStandardCase. Omit to list all IFC-backed objects."
        ),
    )
    name_contains: str | None = Field(
        None,
        description="Case-insensitive substring match on the Blender object name.",
    )
    storey: str | None = Field(
        None,
        description=(
            "Only elements contained in this IfcBuildingStorey, matched by "
            "storey Name or GlobalId."
        ),
    )
    limit: int = Field(
        200, ge=1, le=1000, description="Maximum elements returned; page with offset."
    )
    offset: int = Field(
        0, ge=0, description="Number of matching elements to skip (for paging)."
    )


class GetSpatialStructureInput(BaseModel):
    """Options for the spatial hierarchy query."""

    model_config = ConfigDict(extra="forbid")

    include_element_counts: bool = Field(
        True,
        description=(
            "Include per-storey/space counts of contained elements grouped "
            "by IFC class. Disable for a faster, smaller tree."
        ),
    )


DEFAULT_QUANTITY_CLASSES = (
    "IfcWall",
    "IfcSlab",
    "IfcColumn",
    "IfcBeam",
    "IfcDoor",
    "IfcWindow",
    "IfcRoof",
    "IfcStair",
    "IfcCovering",
    "IfcSpace",
)


class GetQuantitiesInput(BaseModel):
    """Options for the quantity takeoff query."""

    model_config = ConfigDict(extra="forbid")

    ifc_classes: list[str] | None = Field(
        None,
        description=(
            "IFC classes to aggregate (inheritance-aware). Defaults to common "
            "building element classes: " + ", ".join(DEFAULT_QUANTITY_CLASSES) + "."
        ),
    )
    by_storey: bool = Field(
        False,
        description=(
            "Break totals down per IfcBuildingStorey (containment-based) in "
            "addition to the per-class totals."
        ),
    )


class GetSelectedObjectsInput(BaseModel):
    """Options for the selection query."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(
        200,
        ge=1,
        le=1000,
        description=(
            "Maximum objects returned (a box-select can grab thousands). The "
            "response reports total and truncated."
        ),
    )


class SaveIfcInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_path: str | None = Field(
        None,
        description=(
            "Optional absolute path for a save-as. When omitted, the project "
            "is saved back to its own file (in-place, no overwrite guard). "
            "When provided, the bridge refuses to overwrite an existing file "
            "unless `overwrite=True` is passed."
        ),
    )
    overwrite: bool = Field(
        False, description="Allow overwriting an existing file at output_path."
    )
    reload: bool = Field(
        False,
        description=(
            "Reload the project from the saved file afterwards. This clears "
            "and rebuilds the Blender scene so the viewport reflects IFC-level "
            "edits made via execute_ifc_code."
        ),
    )


SCREENSHOT_MIN_SIZE = 64
SCREENSHOT_MAX_SIZE = 2048


class ViewportScreenshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_size: int = Field(
        800,
        ge=SCREENSHOT_MIN_SIZE,
        le=SCREENSHOT_MAX_SIZE,
        description=(
            "Longest edge of the returned image in pixels (the render is "
            "downscaled, never upscaled, and is additionally capped by the "
            "native viewport resolution). Keep small; large values risk "
            "exceeding the MCP response size cap."
        ),
    )
    format: str = Field(
        "jpeg",
        pattern="^(jpeg|png)$",
        description="Image format. 'jpeg' (default) is much smaller; 'png' is lossless.",
    )
    quality: int = Field(
        85, ge=1, le=100, description="JPEG quality (ignored for png)."
    )
    view: str | None = Field(
        None,
        pattern="^(top|bottom|front|back|left|right|iso|camera)$",
        description=(
            "Aim the viewport before capturing: an axis view (orthographic), "
            "'iso' for a perspective isometric, or 'camera' for the scene "
            "camera. Omit to capture the viewport as it currently points."
        ),
    )
    fit: str | None = Field(
        None,
        pattern="^(all|selected)$",
        description=(
            "Frame content before capturing: 'all' zooms to everything, "
            "'selected' zooms to the current selection. Combines with `view`. "
            "Framing is direction-aware: after the initial fit the zoom is "
            "tightened to the content's 2D extent in the chosen view, so "
            "elevations fill the frame instead of the bounding sphere."
        ),
    )
    azimuth: float | None = Field(
        None,
        ge=-360.0,
        le=360.0,
        description=(
            "Aim the viewport at an arbitrary heading, in degrees (0 = front, "
            "90 = right, counter-clockwise seen from above). Use with "
            "`elevation` instead of `view`."
        ),
    )
    elevation: float | None = Field(
        None,
        ge=-90.0,
        le=90.0,
        description=(
            "Camera height angle in degrees (0 = horizontal, 90 = straight "
            "down bird's eye). Defaults to 30 when only azimuth is given."
        ),
    )
    storey: str | None = Field(
        None,
        description=(
            "Isolate one IfcBuildingStorey (matched by Name or GlobalId) "
            "during the capture: everything else is hidden and restored "
            "afterwards. Combine with view='top', fit='all' for a floor plan."
        ),
    )
    shading: str | None = Field(
        None,
        pattern="^(wireframe|solid|material|rendered|class_colors)$",
        description=(
            "Viewport shading for the capture (restored afterwards). "
            "'class_colors' renders solid shading with one flat color per "
            "IFC class and returns a color legend in the response."
        ),
    )
    show_overlays: bool = Field(
        False,
        description=(
            "Keep viewport overlays (grid, axes, gizmos) visible in the "
            "capture. Off by default: overlays are noise for image analysis."
        ),
    )
    include_objects: bool = Field(
        False,
        description=(
            "Also return screen-space 2D bounding boxes (normalized 0-1, "
            "origin top-left) and view depth keyed by GlobalId for objects "
            "in frame, as text. Enables spatial reasoning without relying "
            "on the image."
        ),
    )
    max_objects: int = Field(
        50,
        ge=1,
        le=200,
        description=(
            "Cap for include_objects. Selection is stratified across IFC "
            "classes (a few walls, doors, windows, ...) rather than just the "
            "largest boxes, so ground slabs do not crowd everything out."
        ),
    )

    @model_validator(mode="after")
    def _validate_aim(self) -> ViewportScreenshotInput:
        if self.view is not None and (self.azimuth is not None or self.elevation is not None):
            raise ValueError("Pass either 'view' or 'azimuth'/'elevation', not both.")
        return self


PSETS_BATCH_MAX = 100


class GetPsetsInput(BaseModel):
    """Property-set lookup targets."""

    model_config = ConfigDict(extra="forbid")

    global_ids: list[str] = Field(
        default_factory=list,
        description="IFC GlobalIds to look up. Order is preserved in the response.",
    )
    names: list[str] = Field(
        default_factory=list,
        description="Blender object names to look up. Order is preserved in the response.",
    )
    limit: int = Field(
        PSETS_BATCH_MAX,
        ge=1,
        le=PSETS_BATCH_MAX,
        description=(
            "Maximum targets processed per call (global_ids first, then "
            "names). Larger requests are no longer an error: the response "
            "reports total/truncated so you can page with offset."
        ),
    )
    offset: int = Field(
        0, ge=0, description="Number of targets to skip (for paging large batches)."
    )

    @model_validator(mode="after")
    def _validate_targets(self) -> GetPsetsInput:
        total = len(self.global_ids) + len(self.names)
        if total == 0:
            raise ValueError("Provide at least one entry in 'global_ids' or 'names'.")
        for gid in self.global_ids:
            if not gid or not gid.strip():
                raise ValueError("'global_ids' must not contain empty strings.")
        for name in self.names:
            if not name or not name.strip():
                raise ValueError("'names' must not contain empty strings.")
        return self


class ObjectSummary(BaseModel):
    """Summary information about one Blender object."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: str | None = None
    location: list[float] | None = None
    dimensions: list[float] | None = None
    ifc_class: str | None = None
    global_id: str | None = None
