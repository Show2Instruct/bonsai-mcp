"""Wire protocol and tool schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BridgeRequest(BaseModel):
    """One request sent to the Blender bridge."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(..., description="Command name routed inside the Blender add-on.")
    params: dict[str, Any] = Field(default_factory=dict, description="Arbitrary JSON params.")


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
            "Runs with full access to `bpy`, `bonsai`, and `ifcopenshell`. "
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
            "downscaled, never upscaled). Keep small; large values risk "
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
            "'selected' zooms to the current selection. Combines with `view`."
        ),
    )
    include_objects: bool = Field(
        False,
        description=(
            "Also return screen-space 2D bounding boxes (normalized 0-1, "
            "origin top-left) keyed by GlobalId for objects in frame, as "
            "text. Enables spatial reasoning without relying on the image."
        ),
    )
    max_objects: int = Field(
        50,
        ge=1,
        le=200,
        description="Cap for include_objects, largest on-screen boxes first.",
    )


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

    @model_validator(mode="after")
    def _validate_targets(self) -> GetPsetsInput:
        total = len(self.global_ids) + len(self.names)
        if total == 0:
            raise ValueError("Provide at least one entry in 'global_ids' or 'names'.")
        if total > PSETS_BATCH_MAX:
            raise ValueError(
                f"Total targets ({total}) exceeds the per-call cap of {PSETS_BATCH_MAX}."
            )
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
