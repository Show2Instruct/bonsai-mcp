"""Local Blender bridge for Bonsai MCP."""

from __future__ import annotations

import base64
import collections
import contextlib
import errno
import hmac
import io
import json
import os
import queue
import re as _re
import select
import shutil
import socket
import socketserver
import struct
import threading
import time
import traceback

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

bl_info = {
    "name": "Bonsai MCP Bridge",
    "author": "Show2Instruct",
    "version": (1, 2, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Bonsai MCP",
    "description": "Localhost TCP bridge for the bonsai-mcp server.",
    "category": "Development",
}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9878
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
# How long a connection handler waits for the main thread before cancelling
# the queued request and reporting a timeout to the client.
MAIN_THREAD_WAIT_SECONDS = 120.0
# Commands that modify state; rejected when the "Allow edits" preference is off.
_EDIT_COMMANDS = frozenset(
    {
        "execute_code",
        "execute_ifc_code",
        "save_ifc_file",
        "refresh_view",
        "refresh_geometry",
        "reload_project",
    }
)
_ACTIVITY_LOG_SIZE = 5
_STATE: dict[str, object] = {
    "server": None,
    "thread": None,
    "request_queue": queue.Queue(),
    "timer_registered": False,
    "bound": None,
    "last_error": None,
    "requests_served": 0,
    "last_command": None,
    "activity": collections.deque(maxlen=_ACTIVITY_LOG_SIZE),
    # snapshot of the token preference, taken on the main thread when the
    # bridge starts so handler threads never touch bpy preferences
    "token": "",
}


class _ProtocolError(ValueError):
    """A malformed inbound frame; the peer gets an error reply, then we close."""

def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _read_message(sock: socket.socket) -> dict | None:
    """Read one framed request. None means the peer closed the connection.

    Raises _ProtocolError for malformed input (oversized frame, non-JSON
    body) so the handler can send an error reply instead of silently
    dropping the connection.
    """
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length > MAX_MESSAGE_BYTES:
        raise _ProtocolError(
            f"frame of {length} bytes exceeds the {MAX_MESSAGE_BYTES} byte cap"
        )
    body = _recv_exact(sock, length)
    if body is None:
        return None
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ProtocolError(f"frame body is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise _ProtocolError(
            f"frame body must be a JSON object, got {type(message).__name__}"
        )
    return message


def _send_message(sock: socket.socket, payload: dict) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    sock.sendall(struct.pack(">I", len(body)) + body)

def _try_import_ifcopenshell():
    try:
        import ifcopenshell  # type: ignore
        return ifcopenshell
    except ImportError:
        return None


def _get_bonsai_tool():
    """Return bonsai.tool (or the legacy blenderbim.tool), or None."""
    with contextlib.suppress(ImportError):
        import bonsai.tool as tool_mod  # type: ignore

        return tool_mod
    with contextlib.suppress(ImportError):
        import blenderbim.tool as tool_mod  # type: ignore

        return tool_mod
    return None


# Memoizes _get_loaded_ifc for the duration of one bridge request. Without
# this, per-object lookups while filtering can re-resolve (and on the
# fallback path re-open from disk!) the IFC file thousands of times.
_IFC_CACHE: dict[str, object] = {"valid": False, "ifc": None}


def _invalidate_ifc_cache() -> None:
    _IFC_CACHE["valid"] = False
    _IFC_CACHE["ifc"] = None


def _get_loaded_ifc():
    """Return the loaded IFC file, if available. Memoized per bridge request."""
    if _IFC_CACHE["valid"]:
        return _IFC_CACHE["ifc"]
    ifc = _resolve_loaded_ifc()
    _IFC_CACHE["valid"] = True
    _IFC_CACHE["ifc"] = ifc
    return ifc


def _resolve_loaded_ifc():
    """Resolve the loaded IFC file from Bonsai, or best-effort from disk."""
    tool_mod = _get_bonsai_tool()
    if tool_mod is not None:
        try:
            ifc = tool_mod.Ifc.get()
            if ifc is not None:
                return ifc
        except Exception:
            pass

    try:
        ifc_path = getattr(bpy.context.scene.BIMProperties, "ifc_file", "")  # type: ignore[attr-defined]
        if ifc_path and os.path.isfile(ifc_path):
            ifcopenshell = _try_import_ifcopenshell()
            if ifcopenshell is not None:
                return ifcopenshell.open(ifc_path)
    except Exception:
        pass

    return None


def _bonsai_project_path() -> str | None:
    """Return the file path of the currently loaded IFC project, if known."""
    import importlib

    for mod_name in ("bonsai.bim.ifc", "blenderbim.bim.ifc"):
        with contextlib.suppress(Exception):
            path = importlib.import_module(mod_name).IfcStore.path
            if path:
                return path
    return None


def _save_ifc_project(path: str) -> str:
    """Write the in-memory IFC model to `path`; return the method used.

    Prefers Bonsai's IfcExporter, which first syncs pending Blender-side
    edits into the IFC model. Falls back to a plain ifcopenshell write.
    """
    import importlib
    import logging

    for pkg in ("bonsai", "blenderbim"):
        try:
            export_ifc = importlib.import_module(f"{pkg}.bim.export_ifc")
        except ImportError:
            continue
        settings = export_ifc.IfcExportSettings.factory(
            bpy.context, path, logging.getLogger("BonsaiExport")
        )
        export_ifc.IfcExporter(settings).export()
        return f"{pkg}.bim.export_ifc.IfcExporter"

    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError("No IFC project is loaded; cannot save.")
    ifc.write(path)
    return "ifcopenshell.write"


def _reload_ifc_project(path: str) -> None:
    """Clear the scene and reload the project so the viewport matches the IFC."""
    tool_mod = _get_bonsai_tool()
    if tool_mod is None:
        raise RuntimeError("Bonsai is not available; cannot reload the project.")
    # Bonsai's loader purges IfcStore and can lazily re-resolve the project
    # from the scene's ifc_file property; unless that points at the target
    # first, the reload silently re-imports the previous file.
    with contextlib.suppress(Exception):
        bpy.context.scene.BIMProperties.ifc_file = path  # type: ignore[attr-defined]
    tool_mod.IfcGit.load_project(path)
    _invalidate_ifc_cache()


def _bridge_get_ifc_file():
    """Return the loaded IFC file or raise. Injected as get_ifc_file()."""
    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError("No IFC file open. Load a project in Bonsai first.")
    return ifc


def _bridge_get_default_container():
    """Return the active spatial container. Injected as get_default_container()."""
    tool_mod = _get_bonsai_tool()
    if tool_mod is None:
        raise RuntimeError("Bonsai (bonsai.tool) is not available.")
    container = tool_mod.Root.get_default_container()
    if not container:
        raise RuntimeError("No active spatial container.")
    return container


def _bridge_save_and_load_ifc(path: str | None = None) -> str:
    """Save the project (to its own file by default) and reload it.

    Reloading is what makes IFC-level edits visible in the Blender viewport.
    Returns the saved path. Injected as save_and_load_ifc().
    """
    target = path or _bonsai_project_path()
    if not target:
        raise RuntimeError(
            "The project has no file path yet. Pass an explicit path, e.g. "
            "save_and_load_ifc(r'C:/path/model.ifc')."
        )
    _save_ifc_project(target)
    _reload_ifc_project(target)
    return target


def _element_for_object(obj):
    """Return the IFC entity behind a Blender object, or None."""
    ifc = _get_loaded_ifc()
    if ifc is None:
        return None
    try:
        ifc_def_id = obj.BIMObjectProperties.ifc_definition_id  # type: ignore[attr-defined]
    except Exception:
        return None
    if not ifc_def_id:
        return None
    try:
        return ifc.by_id(ifc_def_id)
    except Exception:
        return None


def _name_prefix_class(obj) -> str | None:
    """Fallback class guess from the 'IfcClass/Name' object naming convention."""
    name = getattr(obj, "name", "") or ""
    if "/" in name:
        prefix = name.split("/", 1)[0]
        if prefix.startswith("Ifc"):
            return prefix
    return None


def _ifc_class_for_object(obj) -> str | None:
    """Best-effort IFC class lookup for a Blender object."""
    element = _element_for_object(obj)
    if element is not None:
        try:
            return element.is_a()
        except Exception:
            pass
    return _name_prefix_class(obj)


def _object_matches_class(obj, target: str) -> bool:
    """Inheritance-aware class test: IfcWallStandardCase matches IfcWall.

    Uses the boolean form element.is_a(target), which walks the schema
    inheritance chain (string equality on is_a() misses subtypes such as
    IfcWallStandardCase in IFC2X3 models). Falls back to an exact name-prefix
    match when no IFC entity is resolvable.
    """
    element = _element_for_object(obj)
    if element is not None:
        try:
            return bool(element.is_a(target))
        except Exception:
            return False
    return _name_prefix_class(obj) == target


def _global_id_for_object(obj) -> str | None:
    element = _element_for_object(obj)
    if element is None:
        return None
    try:
        return getattr(element, "GlobalId", None)
    except Exception:
        return None


def _object_summary(obj) -> dict:
    loc = getattr(obj, "location", None)
    dims = getattr(obj, "dimensions", None)
    return {
        "name": obj.name,
        "type": getattr(obj, "type", None),
        "location": [loc.x, loc.y, loc.z] if loc is not None else None,
        "dimensions": [dims.x, dims.y, dims.z] if dims is not None else None,
        "ifc_class": _ifc_class_for_object(obj),
        "global_id": _global_id_for_object(obj),
    }

def _h_ping(_params):
    return {
        "status": "ok",
        "service": "bonsai-mcp-bridge",
        "blender_version": bpy.app.version_string,
        "addon_version": ".".join(str(x) for x in bl_info["version"]),
        "ifcopenshell_available": _try_import_ifcopenshell() is not None,
        "ifc_loaded": _get_loaded_ifc() is not None,
        "edits_allowed": _edits_allowed(),
        "token_required": bool(_STATE.get("token")),
        "requests_served": _STATE.get("requests_served", 0),
    }


def _paginate(items: list, params: dict, default_limit: int = 200, max_limit: int = 1000):
    """Slice `items` by the request's limit/offset; return page plus metadata."""
    try:
        limit = int(params.get("limit") or default_limit)
    except (TypeError, ValueError):
        limit = default_limit
    limit = max(1, min(limit, max_limit))
    try:
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    page = items[offset : offset + limit]
    truncated = offset + len(page) < len(items)
    return page, len(items), truncated, limit, offset


_QUERY_KEYWORD_TO_CLASS = {
    "walls": "IfcWall",
    "doors": "IfcDoor",
    "windows": "IfcWindow",
    "spaces": "IfcSpace",
    "slabs": "IfcSlab",
    "columns": "IfcColumn",
    "beams": "IfcBeam",
    "roofs": "IfcRoof",
    "stairs": "IfcStair",
}


def _matching_objects(objects, params) -> list:
    """Apply the optional `query` filter and return the matching objects.

    Summaries are built later, for the requested page only, so a paged
    query on a huge scene does not pay per-object IFC lookups for every
    match.
    """
    query = (params.get("query") or "").strip().lower()
    if not query:
        return []

    if query == "all":
        return list(objects)

    if query == "selected":
        return list(bpy.context.selected_objects)

    if query in _QUERY_KEYWORD_TO_CLASS:
        target = _QUERY_KEYWORD_TO_CLASS[query]
        return [o for o in objects if _object_matches_class(o, target)]

    if query == "by_class":
        target = params.get("ifc_class")
        if not target:
            raise ValueError("query='by_class' requires 'ifc_class'")
        return [o for o in objects if _object_matches_class(o, target)]

    if query == "by_name":
        target = params.get("name")
        if not target:
            raise ValueError("query='by_name' requires 'name'")
        return [o for o in objects if o.name == target]

    if query == "by_global_id":
        target = params.get("global_id")
        if not target:
            raise ValueError("query='by_global_id' requires 'global_id'")
        return [o for o in objects if _global_id_for_object(o) == target]

    raise ValueError(f"Unknown query: {query!r}")


# Cap on the selected-object name list in the scene summary; a box-select can
# grab thousands of objects and the full list would bloat every summary frame.
_SCENE_SELECTED_NAMES_CAP = 50


def _h_get_scene_info(params):
    scene = bpy.context.scene
    objects = list(scene.objects)
    selected = [o.name for o in bpy.context.selected_objects]
    collections = [c.name for c in scene.collection.children_recursive] if hasattr(
        scene.collection, "children_recursive"
    ) else [c.name for c in scene.collection.children]
    object_type_counts: dict[str, int] = {}
    for obj in objects:
        object_type_counts[obj.type] = object_type_counts.get(obj.type, 0) + 1

    payload: dict = {
        "scene_name": scene.name,
        "object_count": len(objects),
        "selected_count": len(selected),
        "selected_objects": selected[:_SCENE_SELECTED_NAMES_CAP],
        "selected_objects_truncated": len(selected) > _SCENE_SELECTED_NAMES_CAP,
        "collections": collections,
        "object_type_counts": object_type_counts,
        "ifc_available": _get_loaded_ifc() is not None,
        "blender_version": bpy.app.version_string,
    }

    if params and (params.get("query") or "").strip():
        matches = _matching_objects(objects, params)
        page, total, truncated, limit, offset = _paginate(matches, params)
        payload["objects"] = [_object_summary(o) for o in page]
        payload["objects_total"] = total
        payload["objects_truncated"] = truncated
        payload["objects_offset"] = offset
        payload["objects_limit"] = limit

    return payload


def _h_get_selected_objects(params):
    selected = list(bpy.context.selected_objects)
    page, total, truncated, _limit, _offset = _paginate(selected, params or {})
    return {
        "objects": [_object_summary(o) for o in page],
        "total": total,
        "truncated": truncated,
    }


_EXEC_OUTPUT_CAP_BYTES = 256 * 1024


def _trim_output(text: str) -> tuple[str, bool, int]:
    """Trim captured execution output."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _EXEC_OUTPUT_CAP_BYTES:
        return text, False, len(encoded)
    cut = encoded[:_EXEC_OUTPUT_CAP_BYTES].decode("utf-8", errors="replace")
    return cut, True, len(encoded)


def _h_execute_code(params):
    code = params.get("code", "")
    if not code:
        raise ValueError("'code' is required")

    stdout = io.StringIO()
    stderr = io.StringIO()
    namespace = {
        "bpy": bpy,
        "__name__": "__bonsai_mcp_exec__",
        "get_ifc_file": _bridge_get_ifc_file,
        "get_default_container": _bridge_get_default_container,
        "save_and_load_ifc": _bridge_save_and_load_ifc,
    }
    success = True
    error: str | None = None
    tb: str | None = None
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compile(code, "<bonsai-mcp>", "exec"), namespace)  # noqa: S102
    except (Exception, SystemExit) as exc:
        # SystemExit: generated code calling sys.exit() must not kill Blender
        success = False
        error = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc()

    stdout_text, stdout_truncated, stdout_bytes = _trim_output(stdout.getvalue())
    stderr_text, stderr_truncated, stderr_bytes = _trim_output(stderr.getvalue())

    return {
        "success": success,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "error": error,
        "traceback": tb,
    }


_BPY_PATTERN = _re.compile(
    r"(^|\s)(import\s+bpy|from\s+bpy\s|bpy\.)", _re.MULTILINE
)


def _h_execute_ifc_code(params):
    """Execute IFC code without direct bpy access."""
    code = params.get("code", "")
    if not code:
        raise ValueError("'code' is required")

    if _BPY_PATTERN.search(code):
        raise ValueError(
            "execute_ifc_code does not allow bpy access. "
            "Use execute_blender_code instead for Blender-specific operations. "
            "This tool is restricted to IfcOpenShell and Bonsai API operations."
        )

    ifcopenshell = _try_import_ifcopenshell()
    if ifcopenshell is None:
        raise RuntimeError(
            "IfcOpenShell is not available in this Blender environment. "
            "Install it via Bonsai or manually to use execute_ifc_code."
        )

    ifc = _get_loaded_ifc()

    ifc_util_element = None
    ifc_api = None
    with contextlib.suppress(ImportError):
        import ifcopenshell.util.element as ifc_util_element  # type: ignore
    with contextlib.suppress(ImportError):
        import ifcopenshell.api as ifc_api  # type: ignore

    bonsai_tool = None
    with contextlib.suppress(ImportError):
        import bonsai.tool as bonsai_tool  # type: ignore
    if bonsai_tool is None:
        with contextlib.suppress(ImportError):
            import blenderbim.tool as bonsai_tool  # type: ignore

    namespace = {
        "__name__": "__bonsai_mcp_ifc_exec__",
        "ifcopenshell": ifcopenshell,
        "ifc": ifc,
        "ifc_api": ifc_api,
        "element_util": ifc_util_element,
        "tool": bonsai_tool,
        "get_ifc_file": _bridge_get_ifc_file,
        "get_default_container": _bridge_get_default_container,
        "save_and_load_ifc": _bridge_save_and_load_ifc,
    }

    stdout = io.StringIO()
    stderr = io.StringIO()
    success = True
    error: str | None = None
    tb: str | None = None
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compile(code, "<bonsai-mcp-ifc>", "exec"), namespace)  # noqa: S102
    except (Exception, SystemExit) as exc:
        # SystemExit: generated code calling sys.exit() must not kill Blender
        success = False
        error = f"{type(exc).__name__}: {exc}"
        tb = traceback.format_exc()

    stdout_text, stdout_truncated, stdout_bytes = _trim_output(stdout.getvalue())
    stderr_text, stderr_truncated, stderr_bytes = _trim_output(stderr.getvalue())

    return {
        "success": success,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "error": error,
        "traceback": tb,
        "ifc_available": ifc is not None,
        "namespace_keys": [
            k for k in namespace if not k.startswith("_") and namespace[k] is not None
        ],
    }


_SCREENSHOT_MAX_B64_CHARS = 700 * 1024  # keep the MCP payload well under the 1 MB cap
# Rough png bytes-per-pixel estimate used to downgrade to jpeg BEFORE
# rendering; calibrated against measured viewport output, with headroom.
_PNG_BYTES_PER_PIXEL_ESTIMATE = 1.0
_SCREENSHOT_MIN_SIZE = 64
_SCREENSHOT_MAX_SIZE = 2048
_SCREENSHOT_DEFAULT_SIZE = 800

_VIEW_AXIS_MAP = {
    "top": "TOP",
    "bottom": "BOTTOM",
    "front": "FRONT",
    "back": "BACK",
    "left": "LEFT",
    "right": "RIGHT",
}
_VALID_VIEWS = (*_VIEW_AXIS_MAP, "iso", "camera")
_VALID_FITS = ("all", "selected")
_SHADING_MAP = {
    "wireframe": "WIREFRAME",
    "solid": "SOLID",
    "material": "MATERIAL",
    "rendered": "RENDERED",
    "class_colors": "SOLID",
}

# distinct flat colors for shading='class_colors' (one per IFC class)
_CLASS_COLOR_PALETTE = (
    (0.12, 0.47, 0.71),
    (1.00, 0.50, 0.05),
    (0.17, 0.63, 0.17),
    (0.84, 0.15, 0.16),
    (0.58, 0.40, 0.74),
    (0.55, 0.34, 0.29),
    (0.89, 0.47, 0.76),
    (0.50, 0.50, 0.50),
    (0.74, 0.74, 0.13),
    (0.09, 0.75, 0.81),
    (0.70, 0.87, 0.54),
    (1.00, 0.73, 0.47),
)


def _find_view3d():
    """Return (window, area, region) for the largest open 3D viewport, or Nones."""
    best = (None, None, None)
    best_size = -1
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            size = area.width * area.height
            if size > best_size:
                best = (window, area, region)
                best_size = size
    return best


def _visible_mesh_objects(selected_only: bool = False):
    objects = bpy.context.selected_objects if selected_only else bpy.context.view_layer.objects
    return [o for o in objects if o.type == "MESH" and o.visible_get()]


def _tighten_framing(area, region, selected_only: bool) -> None:
    """Direction-aware zoom: fit the content's projected 2D extent.

    view_all/view_selected frame the bounding sphere; on flat-ish buildings
    an elevation view then fills only ~30% of the frame. Ortho zoom scales
    linearly with view_distance, so one multiplicative correction against
    the measured screen coverage is exact there and a safe approximation in
    perspective (clamped to avoid near-clipping surprises).
    """
    rv3d = area.spaces.active.region_3d
    if rv3d.view_perspective == "CAMERA":
        return
    from bpy_extras.view3d_utils import location_3d_to_region_2d
    from mathutils import Vector

    xs: list[float] = []
    ys: list[float] = []
    for obj in _visible_mesh_objects(selected_only):
        matrix = obj.matrix_world
        for corner in obj.bound_box:
            point = location_3d_to_region_2d(region, rv3d, matrix @ Vector(corner))
            if point is not None:
                xs.append(point.x)
                ys.append(point.y)
    if not xs or region.width <= 0 or region.height <= 0:
        return
    # The capture renders at the scene render aspect, not the region's;
    # measure against the letterboxed intersection of both frames so
    # tightening can never crop content out of the render.
    render = bpy.context.scene.render
    render_x = max(1, int(render.resolution_x * render.resolution_percentage / 100))
    render_y = max(1, int(render.resolution_y * render.resolution_percentage / 100))
    render_aspect = render_x / render_y
    frame_w = min(float(region.width), region.height * render_aspect)
    frame_h = min(float(region.height), region.width / render_aspect)
    center_x = region.width / 2.0
    center_y = region.height / 2.0
    coverage_x = 2.0 * max(abs(x - center_x) for x in xs) / frame_w
    coverage_y = 2.0 * max(abs(y - center_y) for y in ys) / frame_h
    coverage = max(coverage_x, coverage_y)
    if coverage <= 0.0:
        return
    scale = coverage / 0.9  # leave a 10% margin
    if scale >= 1.0:
        return  # never zoom out: the fit operator already shows everything
    if rv3d.view_perspective != "ORTHO":
        scale = max(scale, 0.2)
    rv3d.view_distance *= max(scale, 0.05)


def _orient_viewport(
    view: str | None,
    fit: str | None,
    azimuth: float | None = None,
    elevation: float | None = None,
) -> None:
    """Aim the largest 3D viewport before capturing (persists after the call)."""
    window, area, region = _find_view3d()
    if window is None:
        raise RuntimeError(
            "No 3D viewport is open; 'view'/'fit' need a visible VIEW_3D area."
        )
    r3d = area.spaces.active.region_3d

    prefs_view = bpy.context.preferences.view
    original_smooth = prefs_view.smooth_view
    prefs_view.smooth_view = 0  # animated transitions would smear the capture
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            if view in _VIEW_AXIS_MAP:
                bpy.ops.view3d.view_axis(type=_VIEW_AXIS_MAP[view])
            elif view == "iso":
                import math

                from mathutils import Euler

                r3d.view_perspective = "PERSP"
                r3d.view_rotation = Euler(
                    (math.radians(60.0), 0.0, math.radians(45.0)), "XYZ"
                ).to_quaternion()
            elif view == "camera":
                if bpy.context.scene.camera is None:
                    raise RuntimeError("view='camera' requires a scene camera.")
                r3d.view_perspective = "CAMERA"
            elif view:
                raise ValueError(f"'view' must be one of {_VALID_VIEWS}, got {view!r}")
            elif azimuth is not None or elevation is not None:
                import math

                from mathutils import Euler

                # same convention as 'iso' (which is azimuth=45, elevation=30):
                # azimuth 0 = front, counter-clockwise seen from above;
                # elevation 0 = horizontal, 90 = bird's eye
                az = float(azimuth) if azimuth is not None else 0.0
                el = float(elevation) if elevation is not None else 30.0
                el = max(-90.0, min(90.0, el))
                r3d.view_perspective = "PERSP"
                r3d.view_rotation = Euler(
                    (math.radians(90.0 - el), 0.0, math.radians(az)), "XYZ"
                ).to_quaternion()

            if fit == "all":
                bpy.ops.view3d.view_all()
            elif fit == "selected":
                if not bpy.context.selected_objects:
                    raise ValueError("fit='selected' requires at least one selected object.")
                bpy.ops.view3d.view_selected()
            elif fit:
                raise ValueError(f"'fit' must be one of {_VALID_FITS}, got {fit!r}")
            if fit:
                with contextlib.suppress(Exception):
                    _tighten_framing(area, region, fit == "selected")
    finally:
        prefs_view.smooth_view = original_smooth


def _viewport_snapshot(include_objects: bool, max_objects: int) -> dict | None:
    """Structured viewport state, optionally with screen-space object boxes.

    Box format: [x_min, y_min, x_max, y_max], normalized 0-1, origin at the
    image's top-left. `depth` is the view-space distance to the object's
    bounding-box centre (model units), so near/far ordering is available
    without pixels. Boxes are approximate (full object extent, ignoring
    occlusion) but preserve relative spatial layout.
    """
    _window, area, region = _find_view3d()
    if area is None:
        return None
    rv3d = area.spaces.active.region_3d
    state: dict = {
        "view_rotation": [round(v, 4) for v in rv3d.view_rotation],
        "view_perspective": rv3d.view_perspective,
        "is_orthographic_side_view": rv3d.is_orthographic_side_view,
        "view_distance": round(rv3d.view_distance, 3),
        "view_location": [round(v, 3) for v in rv3d.view_location],
    }
    if not include_objects:
        return state

    from bpy_extras.view3d_utils import location_3d_to_region_2d
    from mathutils import Vector

    view_matrix = rv3d.view_matrix
    # (box area, ifc class, entry) per candidate object
    candidates: list[tuple[float, str, dict]] = []
    for obj in bpy.context.view_layer.objects:
        if obj.type != "MESH" or not obj.visible_get():
            continue
        matrix = obj.matrix_world
        corners = [matrix @ Vector(c) for c in obj.bound_box]
        pts = []
        for corner in corners:
            point = location_3d_to_region_2d(region, rv3d, corner)
            if point is not None:
                pts.append(point)
        if not pts:
            continue
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
        if max(xs) < 0 or min(xs) > region.width or max(ys) < 0 or min(ys) > region.height:
            continue
        x0 = max(0.0, min(xs)) / region.width
        x1 = min(float(region.width), max(xs)) / region.width
        y0 = 1.0 - min(float(region.height), max(ys)) / region.height
        y1 = 1.0 - max(0.0, min(ys)) / region.height
        centre = sum(corners, Vector((0.0, 0.0, 0.0))) / 8.0
        depth = -(view_matrix @ centre).z  # positive = in front of the viewpoint
        ifc_class = _ifc_class_for_object(obj)
        candidates.append(
            (
                (x1 - x0) * (y1 - y0),
                ifc_class or "(unclassified)",
                {
                    "name": obj.name,
                    "ifc_class": ifc_class,
                    "global_id": _global_id_for_object(obj),
                    "box": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
                    "depth": round(depth, 2),
                },
            )
        )

    # round-robin across IFC classes (largest box first within each class)
    # so a few huge slabs cannot crowd out doors and windows
    by_class: dict[str, list[tuple[float, str, dict]]] = {}
    for item in candidates:
        by_class.setdefault(item[1], []).append(item)
    for items in by_class.values():
        items.sort(key=lambda item: item[0], reverse=True)
    class_order = sorted(by_class, key=lambda cls: by_class[cls][0][0], reverse=True)
    picked: list[tuple[float, str, dict]] = []
    rank = 0
    while len(picked) < max_objects:
        advanced = False
        for cls in class_order:
            items = by_class[cls]
            if rank < len(items):
                picked.append(items[rank])
                advanced = True
                if len(picked) >= max_objects:
                    break
        if not advanced:
            break
        rank += 1
    picked.sort(key=lambda item: item[0], reverse=True)
    state["objects_in_view"] = [entry for _, _, entry in picked]
    state["objects_in_view_total"] = len(candidates)
    state["objects_truncated"] = len(candidates) > len(picked)
    return state


def _screenshot_output_path(ext: str) -> str:
    """Return the viewport screenshot path for the given extension."""
    tmp_dir = _STATE.get("screenshot_dir")
    if not isinstance(tmp_dir, str):
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="bonsai_mcp_")
        _STATE["screenshot_dir"] = tmp_dir
    return os.path.join(tmp_dir, f"viewport.{ext}")


def _apply_class_colors():
    """Assign one flat color per IFC class via object color.

    Returns (legend, restore) where legend maps class -> [r, g, b] and
    restore is a list of (object, original_color) pairs. Colors are
    assigned to classes in sorted order, so the same model always gets the
    same legend.
    """
    pairs = [
        (obj, _ifc_class_for_object(obj) or "(unclassified)")
        for obj in _visible_mesh_objects()
    ]
    classes = sorted({cls for _, cls in pairs})
    class_to_color = {
        cls: _CLASS_COLOR_PALETTE[i % len(_CLASS_COLOR_PALETTE)]
        for i, cls in enumerate(classes)
    }
    restore = []
    for obj, cls in pairs:
        color = class_to_color[cls]
        restore.append((obj, tuple(obj.color)))
        obj.color = (color[0], color[1], color[2], 1.0)
    legend = {
        cls: [round(c, 3) for c in color] for cls, color in class_to_color.items()
    }
    return legend, restore


def _isolate_storey(storey_key: str) -> list:
    """Hide everything not contained in the storey; return objects to unhide."""
    ids = _element_ids_in_storey(storey_key)
    hidden = []
    for obj in bpy.context.view_layer.objects:
        if obj.hide_get():
            continue
        element = _element_for_object(obj)
        keep = False
        if element is not None:
            with contextlib.suppress(Exception):
                keep = element.id() in ids
        if not keep:
            hidden.append(obj)
            obj.hide_set(True)
    return hidden


def _h_get_viewport_screenshot(params):
    params = params or {}
    try:
        max_size = int(params.get("max_size") or _SCREENSHOT_DEFAULT_SIZE)
        quality = int(params.get("quality") or 85)
    except (TypeError, ValueError):
        raise ValueError("'max_size' and 'quality' must be integers") from None
    max_size = max(_SCREENSHOT_MIN_SIZE, min(max_size, _SCREENSHOT_MAX_SIZE))
    quality = max(1, min(quality, 100))
    fmt = str(params.get("format") or "jpeg").lower()
    if fmt not in ("jpeg", "jpg", "png"):
        raise ValueError("'format' must be 'jpeg' or 'png'")
    is_jpeg = fmt != "png"

    include_objects = bool(params.get("include_objects", False))
    try:
        max_objects = int(params.get("max_objects") or 50)
    except (TypeError, ValueError):
        raise ValueError("'max_objects' must be an integer") from None
    max_objects = max(1, min(max_objects, 200))

    view = params.get("view") or None
    fit = params.get("fit") or None
    azimuth = params.get("azimuth")
    elevation = params.get("elevation")
    if azimuth is not None or elevation is not None:
        if view:
            raise ValueError("Pass either 'view' or 'azimuth'/'elevation', not both.")
        try:
            azimuth = float(azimuth) if azimuth is not None else None
            elevation = float(elevation) if elevation is not None else None
        except (TypeError, ValueError):
            raise ValueError("'azimuth' and 'elevation' must be numbers") from None
    storey_key = params.get("storey") or None
    shading = params.get("shading") or None
    if shading is not None and shading not in _SHADING_MAP:
        raise ValueError(
            f"'shading' must be one of {tuple(_SHADING_MAP)}, got {shading!r}"
        )
    show_overlays = bool(params.get("show_overlays", False))

    window, area, region = _find_view3d()
    if window is None:
        raise RuntimeError(
            "Screenshot requires an open 3D viewport, and this Blender "
            "session has no VIEW_3D area (background/headless mode, or all "
            "3D viewports are closed)."
        )
    space = area.spaces.active

    note_parts: list[str] = []
    class_legend = None
    color_restore: list = []
    hidden_for_storey: list = []
    original_shading_type = space.shading.type
    original_color_type = space.shading.color_type
    original_overlays = space.overlay.show_overlays

    try:
        if storey_key:
            hidden_for_storey = _isolate_storey(storey_key)

        if view or fit or azimuth is not None or elevation is not None:
            _orient_viewport(view, fit, azimuth, elevation)

        if shading is not None:
            space.shading.type = _SHADING_MAP[shading]
            if shading == "class_colors":
                space.shading.color_type = "OBJECT"
                class_legend, color_restore = _apply_class_colors()
        space.overlay.show_overlays = show_overlays

        scene = bpy.context.scene
        render = scene.render
        settings = render.image_settings
        original_filepath = render.filepath
        original_format = settings.file_format
        original_color_mode = settings.color_mode
        original_quality = settings.quality
        original_res = (
            render.resolution_x,
            render.resolution_y,
            render.resolution_percentage,
        )

        # render.opengl uses the scene render resolution; downscale only, never upscale
        effective_x = max(1, int(render.resolution_x * render.resolution_percentage / 100))
        effective_y = max(1, int(render.resolution_y * render.resolution_percentage / 100))
        scale = min(1.0, max_size / max(effective_x, effective_y))
        width = max(1, int(effective_x * scale))
        height = max(1, int(effective_y * scale))

        # pre-render size estimate: degrade before paying for the render
        # round trip instead of erroring after it
        if not is_jpeg:
            estimated_b64 = width * height * _PNG_BYTES_PER_PIXEL_ESTIMATE * 4 / 3
            if estimated_b64 > _SCREENSHOT_MAX_B64_CHARS:
                is_jpeg = True
                note_parts.append(
                    f"png at {width}x{height} was estimated to exceed the "
                    f"response size cap; auto-downgraded to jpeg (quality "
                    f"{quality}). Request a smaller max_size to get png."
                )

        out_path = _screenshot_output_path("jpg" if is_jpeg else "png")
        try:
            render.filepath = out_path
            render.resolution_x = width
            render.resolution_y = height
            render.resolution_percentage = 100
            if is_jpeg:
                settings.file_format = "JPEG"
                settings.color_mode = "RGB"
                settings.quality = quality
            else:
                settings.file_format = "PNG"
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.render.opengl(write_still=True)
            with open(out_path, "rb") as fh:
                image_bytes = fh.read()
        finally:
            render.filepath = original_filepath
            settings.file_format = original_format
            settings.color_mode = original_color_mode
            settings.quality = original_quality
            (
                render.resolution_x,
                render.resolution_y,
                render.resolution_percentage,
            ) = original_res

        encoded = base64.b64encode(image_bytes).decode("ascii")
        if len(encoded) > _SCREENSHOT_MAX_B64_CHARS:
            raise RuntimeError(
                f"Screenshot is still {len(image_bytes)} bytes at {width}x{height}, "
                "too large for an MCP response. Retry with a smaller max_size "
                "and/or format='jpeg'."
            )

        viewport = _viewport_snapshot(include_objects, max_objects)
        if viewport is not None:
            if azimuth is not None:
                viewport["azimuth"] = azimuth
            if elevation is not None:
                viewport["elevation"] = elevation
            if storey_key:
                viewport["storey"] = storey_key

        payload = {
            "path": out_path,
            "image_base64": encoded,
            "base64_chars": len(encoded),
            "format": "jpeg" if is_jpeg else "png",
            "width": width,
            "height": height,
            "bytes": len(image_bytes),
            "view": view,
            "fit": fit,
            "viewport": viewport,
        }
        if class_legend:
            payload["class_legend"] = class_legend
        if note_parts:
            payload["note"] = " ".join(note_parts)
        return payload
    finally:
        with contextlib.suppress(Exception):
            space.shading.type = original_shading_type
        with contextlib.suppress(Exception):
            space.shading.color_type = original_color_type
        with contextlib.suppress(Exception):
            space.overlay.show_overlays = original_overlays
        for obj, color in color_restore:
            with contextlib.suppress(Exception):
                obj.color = color
        for obj in hidden_for_storey:
            with contextlib.suppress(Exception):
                obj.hide_set(False)

_IFC_SUMMARY_NAME_LIMIT = 100


def _summarise_materials(ifc) -> dict:
    """Return a small summary of IfcMaterial entities in the file."""
    try:
        materials = list(ifc.by_type("IfcMaterial"))
    except Exception:
        return {"count": 0, "names": [], "truncated": False}

    names: list[str] = []
    for mat in materials:
        name = getattr(mat, "Name", None)
        if name:
            names.append(name)
    unique_sorted = sorted(set(names))
    truncated = len(unique_sorted) > _IFC_SUMMARY_NAME_LIMIT
    return {
        "count": len(materials),
        "names": unique_sorted[:_IFC_SUMMARY_NAME_LIMIT],
        "truncated": truncated,
    }


def _summarise_classifications(ifc) -> dict:
    """Return a small summary of IfcClassification systems in the file."""
    try:
        classifications = list(ifc.by_type("IfcClassification"))
    except Exception:
        return {"count": 0, "systems": [], "truncated": False}

    systems: list[dict] = []
    for cls in classifications:
        systems.append(
            {
                "name": getattr(cls, "Name", None),
                "source": getattr(cls, "Source", None),
                "edition": getattr(cls, "Edition", None),
            }
        )
    truncated = len(systems) > _IFC_SUMMARY_NAME_LIMIT
    return {
        "count": len(classifications),
        "systems": systems[:_IFC_SUMMARY_NAME_LIMIT],
        "truncated": truncated,
    }


def _h_get_ifc_project_info(_params):
    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError(
            "No IFC project appears to be loaded. Open an IFC file in Bonsai first."
        )

    project = next(iter(ifc.by_type("IfcProject")), None)
    entity_counts: dict[str, int] = {}
    for ifc_class in (
        "IfcSite",
        "IfcBuilding",
        "IfcBuildingStorey",
        "IfcWall",
        "IfcDoor",
        "IfcWindow",
        "IfcSlab",
        "IfcSpace",
        "IfcColumn",
        "IfcBeam",
        "IfcRoof",
        "IfcStair",
    ):
        try:
            entity_counts[ifc_class] = len(ifc.by_type(ifc_class))
        except Exception:
            entity_counts[ifc_class] = 0

    return {
        "schema": getattr(ifc, "schema", None),
        "project_name": getattr(project, "Name", None) if project else None,
        "project_global_id": getattr(project, "GlobalId", None) if project else None,
        "entity_counts": entity_counts,
        "materials": _summarise_materials(ifc),
        "classifications": _summarise_classifications(ifc),
    }


def _object_names_by_definition_id() -> dict[int, str]:
    """Map ifc_definition_id -> Blender object name, built in one scene pass."""
    mapping: dict[int, str] = {}
    for obj in bpy.context.scene.objects:
        try:
            def_id = obj.BIMObjectProperties.ifc_definition_id  # type: ignore[attr-defined]
        except Exception:
            continue
        if def_id and def_id not in mapping:
            mapping[def_id] = obj.name
    return mapping


def _find_ifc_element(ifc, name: str | None, global_id: str | None, id_to_name=None):
    """Resolve an IFC element and its Blender object name."""
    if global_id:
        try:
            element = ifc.by_guid(global_id)
        except Exception:
            element = None
        if element is not None:
            if id_to_name is None:
                id_to_name = _object_names_by_definition_id()
            try:
                blender_name = id_to_name.get(element.id())
            except Exception:
                blender_name = None
            return element, blender_name

    if name:
        obj = bpy.context.scene.objects.get(name)
        if obj is None:
            return None, None
        ifc_def_id = None
        try:
            ifc_def_id = obj.BIMObjectProperties.ifc_definition_id  # type: ignore[attr-defined]
        except Exception:
            ifc_def_id = None
        if not ifc_def_id:
            return None, obj.name
        try:
            return ifc.by_id(ifc_def_id), obj.name
        except Exception:
            return None, obj.name

    return None, None


def _h_get_psets(params):
    """Return property and quantity sets for IFC objects."""
    global_ids = list(params.get("global_ids") or [])
    names = list(params.get("names") or [])
    if not global_ids and not names:
        raise ValueError("Provide at least one entry in 'global_ids' or 'names'.")

    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError(
            "No IFC project appears to be loaded. Open an IFC file in Bonsai first."
        )

    try:
        from ifcopenshell.util.element import (  # type: ignore
            get_psets as _get_psets,
        )
    except Exception as exc:
        raise RuntimeError(
            f"IfcOpenShell util.element is unavailable: {exc}"
        ) from exc

    requests: list[tuple[dict, str | None, str | None]] = []
    for gid in global_ids:
        requests.append(({"global_id": gid}, None, gid))
    for name in names:
        requests.append(({"name": name}, name, None))

    # one scene pass shared by every GlobalId lookup in this batch
    id_to_name = _object_names_by_definition_id() if global_ids else None

    results: list[dict] = []
    for request, lookup_name, lookup_gid in requests:
        element, blender_name = _find_ifc_element(ifc, lookup_name, lookup_gid, id_to_name)
        if element is None:
            results.append({"request": request, "error": "not found"})
            continue
        try:
            psets = _get_psets(element, psets_only=True) or {}
            qtos = _get_psets(element, qtos_only=True) or {}
            results.append(
                {
                    "request": request,
                    "object": {
                        "name": blender_name,
                        "global_id": getattr(element, "GlobalId", None),
                        "ifc_class": element.is_a(),
                    },
                    "property_sets": psets,
                    "quantity_sets": qtos,
                }
            )
        except Exception as exc:  # pragma: no cover
            results.append(
                {"request": request, "error": f"{type(exc).__name__}: {exc}"}
            )

    return {"results": results}


def _find_storey(ifc, key: str):
    """Find an IfcBuildingStorey by GlobalId or (exact) Name."""
    try:
        storeys = list(ifc.by_type("IfcBuildingStorey"))
    except Exception:
        return None
    for storey in storeys:
        if getattr(storey, "GlobalId", None) == key:
            return storey
    for storey in storeys:
        if (getattr(storey, "Name", None) or "") == key:
            return storey
    return None


def _contained_element_ids(spatial) -> set:
    """Ids of elements contained in a spatial element, recursing into
    aggregated children (so elements inside a storey's spaces count too)."""
    ids: set = set()
    seen: set = set()
    stack = [spatial]
    while stack:
        node = stack.pop()
        try:
            node_id = node.id()
        except Exception:
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        for rel in getattr(node, "ContainsElements", None) or []:
            for element in getattr(rel, "RelatedElements", None) or []:
                with contextlib.suppress(Exception):
                    ids.add(element.id())
        for rel in getattr(node, "IsDecomposedBy", None) or []:
            for child in getattr(rel, "RelatedObjects", None) or []:
                stack.append(child)
    return ids


def _element_ids_in_storey(key: str) -> set:
    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError(
            "No IFC project appears to be loaded. Open an IFC file in Bonsai first."
        )
    storey = _find_storey(ifc, key)
    if storey is None:
        raise ValueError(
            f"No IfcBuildingStorey with Name or GlobalId {key!r}. "
            "Use get_spatial_structure to list the storeys."
        )
    return _contained_element_ids(storey)


_SELECTOR_CHEAT_SHEET = (
    "Selector examples: 'IfcWall' (one class), 'IfcWall, IfcSlab' (union), "
    "'IfcWall, material=concrete', 'IfcWall, Pset_WallCommon.FireRating=F30' "
    "(property value), 'IfcElement, Name=/W.*1/' (regex on an attribute). "
    "This is ifcopenshell.util.selector syntax."
)


def _selector_element_ids(selector: str) -> set:
    """Resolve an IfcOpenShell selector query to a set of element ids."""
    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError(
            "No IFC project appears to be loaded. Open an IFC file in Bonsai first."
        )
    try:
        import ifcopenshell.util.selector as selector_util  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "ifcopenshell.util.selector is not available in this Blender "
            "environment; update Bonsai/IfcOpenShell to use the 'selector' "
            "filter, or filter by ifc_class/name_contains instead."
        ) from exc
    try:
        elements = selector_util.filter_elements(ifc, selector)
    except Exception as exc:
        raise ValueError(
            f"Invalid selector {selector!r}: {exc}. {_SELECTOR_CHEAT_SHEET}"
        ) from exc
    ids: set = set()
    for element in elements:
        with contextlib.suppress(Exception):
            ids.add(element.id())
    return ids


def _h_list_elements(params):
    """List IFC-backed objects with class/name/storey/selector filters and paging."""
    params = params or {}
    ifc_class = params.get("ifc_class") or None
    name_contains = (params.get("name_contains") or "").strip().lower() or None
    storey_key = params.get("storey") or None
    selector = (params.get("selector") or "").strip() or None

    storey_ids = _element_ids_in_storey(storey_key) if storey_key else None
    selector_ids = _selector_element_ids(selector) if selector else None

    matches = []
    for obj in bpy.context.scene.objects:
        element = _element_for_object(obj)
        if element is None:
            continue
        if ifc_class:
            try:
                if not element.is_a(ifc_class):
                    continue
            except Exception:
                continue
        if name_contains and name_contains not in (obj.name or "").lower():
            continue
        if storey_ids is not None:
            try:
                if element.id() not in storey_ids:
                    continue
            except Exception:
                continue
        if selector_ids is not None:
            try:
                if element.id() not in selector_ids:
                    continue
            except Exception:
                continue
        matches.append(obj)

    page, total, truncated, limit, offset = _paginate(matches, params)
    return {
        "elements": [_object_summary(o) for o in page],
        "total": total,
        "truncated": truncated,
        "offset": offset,
        "limit": limit,
    }


_SPATIAL_TREE_MAX_DEPTH = 10


def _spatial_node(entity, include_counts: bool, depth: int = 0) -> dict:
    node: dict = {"name": getattr(entity, "Name", None)}
    try:
        node["ifc_class"] = entity.is_a()
    except Exception:
        node["ifc_class"] = None
    node["global_id"] = getattr(entity, "GlobalId", None)
    with contextlib.suppress(Exception):
        if entity.is_a("IfcBuildingStorey"):
            node["elevation"] = getattr(entity, "Elevation", None)

    if include_counts:
        counts: dict[str, int] = {}
        for rel in getattr(entity, "ContainsElements", None) or []:
            for element in getattr(rel, "RelatedElements", None) or []:
                with contextlib.suppress(Exception):
                    cls = element.is_a()
                    counts[cls] = counts.get(cls, 0) + 1
        if counts:
            node["element_counts"] = dict(sorted(counts.items()))
            node["element_total"] = sum(counts.values())

    children: list[dict] = []
    if depth < _SPATIAL_TREE_MAX_DEPTH:
        for rel in getattr(entity, "IsDecomposedBy", None) or []:
            for child in getattr(rel, "RelatedObjects", None) or []:
                with contextlib.suppress(Exception):
                    children.append(_spatial_node(child, include_counts, depth + 1))
    if children:
        node["children"] = children
    return node


def _h_get_spatial_structure(params):
    """Site -> building -> storey -> space tree with optional element counts."""
    params = params or {}
    include_counts = bool(params.get("include_element_counts", True))
    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError(
            "No IFC project appears to be loaded. Open an IFC file in Bonsai first."
        )
    project = next(iter(ifc.by_type("IfcProject")), None)
    if project is None:
        raise RuntimeError("The IFC file contains no IfcProject entity.")
    return {
        "schema": getattr(ifc, "schema", None),
        "tree": _spatial_node(project, include_counts),
    }


def _storey_of(element):
    """Climb containment/aggregation up to the IfcBuildingStorey, or None."""
    seen: set = set()
    current = element
    while current is not None:
        try:
            current_id = current.id()
        except Exception:
            return None
        if current_id in seen:
            return None
        seen.add(current_id)
        with contextlib.suppress(Exception):
            if current.is_a("IfcBuildingStorey"):
                return current
        parent = None
        for rel in getattr(current, "ContainedInStructure", None) or []:
            parent = getattr(rel, "RelatingStructure", None)
            if parent is not None:
                break
        if parent is None:
            for rel in getattr(current, "Decomposes", None) or []:
                parent = getattr(rel, "RelatingObject", None)
                if parent is not None:
                    break
        current = parent
    return None


def _add_quantities(bucket: dict, qtos: dict) -> bool:
    """Accumulate numeric quantity values into bucket; True if any were found."""
    found = False
    for qset in (qtos or {}).values():
        if not isinstance(qset, dict):
            continue
        for qname, qval in qset.items():
            if qname == "id" or isinstance(qval, bool):
                continue
            if isinstance(qval, (int, float)):
                entry = bucket.setdefault(qname, {"sum": 0.0, "elements": 0})
                entry["sum"] += float(qval)
                entry["elements"] += 1
                found = True
    return found


def _round_quantity_sums(bucket: dict) -> dict:
    return {
        qname: {"sum": round(entry["sum"], 4), "elements": entry["elements"]}
        for qname, entry in sorted(bucket.items())
    }


def _project_units(ifc) -> dict:
    """Best-effort map of unit type -> unit name (e.g. LENGTHUNIT -> millimetre)."""
    units: dict[str, str] = {}
    try:
        project = next(iter(ifc.by_type("IfcProject")), None)
        assignment = getattr(project, "UnitsInContext", None)
        for unit in getattr(assignment, "Units", None) or []:
            unit_type = getattr(unit, "UnitType", None)
            name = getattr(unit, "Name", None)
            if not unit_type or not name:
                continue
            prefix = getattr(unit, "Prefix", None)
            label = f"{prefix}{name}".lower() if prefix else str(name).lower()
            units[str(unit_type)] = label
    except Exception:
        pass
    return units


_DEFAULT_QUANTITY_CLASSES = (
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


def _h_get_quantities(params):
    """Aggregate IFC base quantities by class, optionally per storey."""
    params = params or {}
    classes = params.get("ifc_classes") or list(_DEFAULT_QUANTITY_CLASSES)
    if not isinstance(classes, (list, tuple)) or not all(
        isinstance(c, str) and c for c in classes
    ):
        raise ValueError("'ifc_classes' must be a list of IFC class names")
    by_storey = bool(params.get("by_storey", False))

    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError(
            "No IFC project appears to be loaded. Open an IFC file in Bonsai first."
        )
    try:
        from ifcopenshell.util.element import get_psets as _get_psets_util  # type: ignore
    except Exception as exc:
        raise RuntimeError(f"IfcOpenShell util.element is unavailable: {exc}") from exc

    classes_out: dict[str, dict] = {}
    storeys_out: dict[str, dict] = {}
    for ifc_class in classes:
        try:
            elements = list(ifc.by_type(ifc_class))
        except Exception:
            classes_out[ifc_class] = {
                "count": 0,
                "quantities": {},
                "note": "class not found in this IFC schema",
            }
            continue

        bucket: dict = {}
        without_quantities = 0
        for element in elements:
            qtos: dict = {}
            with contextlib.suppress(Exception):
                qtos = _get_psets_util(element, qtos_only=True) or {}
            if not _add_quantities(bucket, qtos):
                without_quantities += 1

            if by_storey:
                storey = _storey_of(element)
                storey_key = (
                    getattr(storey, "Name", None)
                    or getattr(storey, "GlobalId", None)
                    or "(no storey)"
                ) if storey is not None else "(no storey)"
                storey_entry = storeys_out.setdefault(storey_key, {})
                class_entry = storey_entry.setdefault(
                    ifc_class, {"count": 0, "quantities": {}}
                )
                class_entry["count"] += 1
                _add_quantities(class_entry["quantities"], qtos)

        classes_out[ifc_class] = {
            "count": len(elements),
            "elements_without_quantities": without_quantities,
            "quantities": _round_quantity_sums(bucket),
        }

    out: dict = {"classes": classes_out, "units": _project_units(ifc)}
    if by_storey:
        for storey_entry in storeys_out.values():
            for class_entry in storey_entry.values():
                class_entry["quantities"] = _round_quantity_sums(class_entry["quantities"])
        out["by_storey"] = storeys_out
    return out


def _h_save_ifc_file(params):
    output_path = params.get("output_path")
    overwrite = bool(params.get("overwrite", False))
    reload_after = bool(params.get("reload", False))

    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError("No IFC project is loaded; cannot save.")

    in_place = not output_path
    if in_place:
        output_path = _bonsai_project_path()
        if not output_path:
            raise RuntimeError(
                "The project has no file path yet (it was never saved). "
                "Pass 'output_path' to choose where to save it."
            )
    else:
        if os.path.exists(output_path) and not overwrite:
            raise FileExistsError(
                f"Refusing to overwrite existing file at {output_path}. "
                "Pass overwrite=true to force, or omit output_path entirely to "
                "save the project back to its own file."
            )
        parent = os.path.dirname(output_path)
        if parent and not os.path.isdir(parent):
            raise FileNotFoundError(f"Output directory does not exist: {parent}")

    method = _save_ifc_project(output_path)

    if not os.path.isfile(output_path):
        raise RuntimeError(
            f"Save reported no error but no file was written at {output_path}."
        )

    reloaded = False
    if reload_after:
        _reload_ifc_project(output_path)
        reloaded = True

    return {
        "saved": True,
        "output_path": output_path,
        "in_place": in_place,
        "method": method,
        "reloaded": reloaded,
    }


_REFRESH_MAX_TARGETS = 200


def _blender_name_for_element(tool_mod, element) -> str:
    """Bonsai's object-name convention for an element ('IfcClass/Name')."""
    with contextlib.suppress(Exception):
        return tool_mod.Loader.get_name(element)
    name = getattr(element, "Name", None) or "Unnamed"
    return f"{element.is_a()}/{name}"


def _refresh_targets(params) -> list[str]:
    global_ids = list((params or {}).get("global_ids") or [])
    if not global_ids:
        raise ValueError("'global_ids' is required: pass the GlobalIds of the edited elements.")
    if len(global_ids) > _REFRESH_MAX_TARGETS:
        raise ValueError(
            f"Too many targets ({len(global_ids)} > {_REFRESH_MAX_TARGETS}). "
            "Refresh in batches, or use reload_project if most of the model changed."
        )
    return global_ids


def _require_bonsai_and_ifc():
    """Return (tool module, ifc file); refresh needs both."""
    tool_mod = _get_bonsai_tool()
    if tool_mod is None:
        raise RuntimeError("Bonsai (bonsai.tool) is not available; cannot refresh the scene.")
    ifc = _get_loaded_ifc()
    if ifc is None:
        raise RuntimeError(
            "No IFC project appears to be loaded. Open an IFC file in Bonsai first."
        )
    return tool_mod, ifc


def _sync_object_placement(tool_mod, ifc, element, obj) -> bool:
    """Set obj.matrix_world from the element's IFC placement. True on success."""
    try:
        import ifcopenshell.util.placement as placement_util  # type: ignore
        import ifcopenshell.util.unit as unit_util  # type: ignore

        placement = getattr(element, "ObjectPlacement", None)
        if placement is None:
            return False
        matrix = placement_util.get_local_placement(placement).copy()
        matrix[:3, 3] *= unit_util.calculate_unit_scale(ifc)
        obj.matrix_world = tool_mod.Loader.apply_blender_offset_to_matrix_world(obj, matrix)
        return True
    except Exception:
        return False


def _h_refresh_view(params):
    """Tier 1: sync names after data-only IFC edits. No geometry, no disk I/O."""
    global_ids = _refresh_targets(params)
    tool_mod, ifc = _require_bonsai_and_ifc()

    results: list[dict] = []
    for gid in global_ids:
        entry: dict = {"global_id": gid}
        try:
            element = ifc.by_guid(gid)
        except Exception:
            entry["error"] = (
                "No element with this GlobalId in the loaded IFC. If you deleted "
                "it, use reload_project to remove its object from the scene."
            )
            results.append(entry)
            continue
        obj = None
        with contextlib.suppress(Exception):
            obj = tool_mod.Ifc.get_object(element)
        if obj is None:
            entry["error"] = (
                "The element has no Blender object yet (newly created?). "
                "reload_project materializes new elements."
            )
            results.append(entry)
            continue
        new_name = _blender_name_for_element(tool_mod, element)
        entry["object"] = obj.name
        if obj.name != new_name:
            obj.name = new_name
            entry["object"] = new_name
            entry["renamed"] = True
        results.append(entry)

    return {
        "refreshed": sum(1 for r in results if "error" not in r),
        "results": results,
        "note": (
            "Data-only sync (names). Nothing was written to disk; edits stay "
            "in memory until the project is saved."
        ),
    }


def _h_refresh_geometry(params):
    """Tier 2: rebuild geometry and placement for specific elements. No disk I/O."""
    global_ids = _refresh_targets(params)
    tool_mod, ifc = _require_bonsai_and_ifc()

    results: list[dict] = []
    to_reload = []
    for gid in global_ids:
        entry: dict = {"global_id": gid}
        try:
            element = ifc.by_guid(gid)
        except Exception:
            entry["error"] = (
                "No element with this GlobalId in the loaded IFC. If you deleted "
                "it, use reload_project to remove its object from the scene."
            )
            results.append(entry)
            continue
        obj = None
        with contextlib.suppress(Exception):
            obj = tool_mod.Ifc.get_object(element)
        if obj is None:
            entry["error"] = (
                "The element has no Blender object yet (newly created?). "
                "reload_project materializes new elements."
            )
            results.append(entry)
            continue
        entry["object"] = obj.name
        entry["placement_synced"] = _sync_object_placement(tool_mod, ifc, element, obj)
        with contextlib.suppress(Exception):
            new_name = _blender_name_for_element(tool_mod, element)
            if obj.name != new_name:
                obj.name = new_name
                entry["object"] = new_name
        to_reload.append(obj)
        results.append(entry)

    representations_reloaded = False
    if to_reload:
        try:
            tool_mod.Geometry.reload_representation(to_reload)
            representations_reloaded = True
        except Exception as exc:
            for entry in results:
                if "error" not in entry:
                    entry["representation_error"] = f"{type(exc).__name__}: {exc}"

    return {
        "refreshed": sum(1 for r in results if "error" not in r),
        "representations_reloaded": representations_reloaded,
        "results": results,
        "note": (
            "Targeted geometry rebuild. Nothing was written to disk; edits stay "
            "in memory until the project is saved."
        ),
    }


def _restore_project_path(path: str) -> bool:
    """Point Bonsai's project path back at `path` (after a temp-file reload)."""
    import importlib

    restored = False
    for mod_name in ("bonsai.bim.ifc", "blenderbim.bim.ifc"):
        with contextlib.suppress(Exception):
            importlib.import_module(mod_name).IfcStore.path = path
            restored = True
            break
    with contextlib.suppress(Exception):
        bpy.context.scene.BIMProperties.ifc_file = path  # type: ignore[attr-defined]
    return restored


def _reload_temp_path(original: str) -> str:
    """A temp path for reload_project, reused across calls, cleaned on stop."""
    tmp_dir = _STATE.get("reload_dir")
    if not isinstance(tmp_dir, str):
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="bonsai_mcp_reload_")
        _STATE["reload_dir"] = tmp_dir
    return os.path.join(tmp_dir, os.path.basename(original) or "project.ifc")


def _h_reload_project(_params):
    """Tier 3: rebuild the whole scene from the in-memory model.

    Saves to a temp file and reloads from it, then restores the project path,
    so the user's file on disk is never touched and Ctrl+S still saves to it.
    """
    tool_mod = _get_bonsai_tool()
    if tool_mod is None:
        raise RuntimeError("Bonsai is not available; cannot reload the project.")
    original = _bonsai_project_path()
    if not original:
        raise RuntimeError(
            "The project has no file path yet (it was never saved). Save it "
            "once in Blender (or via save_ifc_file with output_path) first."
        )
    tmp_path = _reload_temp_path(original)
    _save_ifc_project(tmp_path)
    _reload_ifc_project(tmp_path)
    path_restored = _restore_project_path(original)
    return {
        "reloaded": True,
        "project_path": original,
        "path_restored": path_restored,
        "note": (
            "Scene rebuilt from the in-memory model via a temporary file. The "
            "project file on disk was not modified; saving still targets "
            f"{original}."
        ),
    }


def _cleanup_reload_dir() -> None:
    tmp = _STATE.pop("reload_dir", None)
    if isinstance(tmp, str):
        shutil.rmtree(tmp, ignore_errors=True)


_HANDLERS = {
    "ping": _h_ping,
    "get_scene_info": _h_get_scene_info,
    "get_selected_objects": _h_get_selected_objects,
    "list_elements": _h_list_elements,
    "execute_code": _h_execute_code,
    "execute_ifc_code": _h_execute_ifc_code,
    "get_viewport_screenshot": _h_get_viewport_screenshot,
    "get_ifc_project_info": _h_get_ifc_project_info,
    "get_spatial_structure": _h_get_spatial_structure,
    "get_quantities": _h_get_quantities,
    "get_psets": _h_get_psets,
    "save_ifc_file": _h_save_ifc_file,
    "refresh_view": _h_refresh_view,
    "refresh_geometry": _h_refresh_geometry,
    "reload_project": _h_reload_project,
}

class _PendingRequest:
    """Bundle a request with an Event that is set when the result is ready.

    `cancelled` is set by the connection handler when the client is gone or
    timed out: the drain loop then skips execution. `finished` is set by the
    bridge only when it produced a definitive outcome (result or error), so
    the handler can distinguish "ran" from "was skipped".
    """

    __slots__ = (
        "command",
        "params",
        "result",
        "error",
        "traceback",
        "done",
        "cancelled",
        "cancel_reason",
        "finished",
    )

    def __init__(self, command: str, params: dict) -> None:
        self.command = command
        self.params = params
        self.result = None
        self.error: str | None = None
        self.traceback: str | None = None
        self.done = threading.Event()
        self.cancelled = False
        self.cancel_reason: str | None = None
        self.finished = False


def _edits_allowed() -> bool:
    """Whether EDIT commands may run (the add-on's 'Allow edits' preference)."""
    try:
        return bool(_prefs().allow_edits)
    except Exception:
        return True


def _record_activity(pending: _PendingRequest, duration_s: float) -> None:
    _STATE["requests_served"] = int(_STATE.get("requests_served", 0)) + 1  # type: ignore[arg-type]
    _STATE["last_command"] = pending.command
    log = _STATE.get("activity")
    if isinstance(log, collections.deque):
        stamp = time.strftime("%H:%M:%S")
        outcome = "err" if pending.error else "ok"
        log.appendleft(f"{stamp} {outcome} {pending.command} {duration_s * 1000.0:.0f}ms")


def _redraw_view3d_areas() -> None:
    """Repaint 3D viewports so the panel's counters update without mouse-over."""
    with contextlib.suppress(Exception):
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()


def _drain_queue() -> float | None:
    """Timer callback. Runs on the main thread, executes queued commands."""
    if _STATE.get("server") is None:
        # bridge stopped: fail anything still queued and let Blender
        # unregister this timer (handler threads may have re-registered it
        # in the window between shutdown and their own exit)
        _flush_queue("bridge stopped before the request could run")
        _STATE["timer_registered"] = False
        return None
    q: queue.Queue = _STATE["request_queue"]  # type: ignore[assignment]
    drained = 0
    while drained < 8:
        try:
            pending: _PendingRequest = q.get_nowait()
        except queue.Empty:
            break
        drained += 1
        if pending.cancelled:
            # the client already gave up on this request; never execute it
            pending.done.set()
            continue
        _invalidate_ifc_cache()
        started = time.perf_counter()
        handler = _HANDLERS.get(pending.command)
        try:
            if handler is None:
                raise ValueError(f"Unknown command: {pending.command!r}")
            if pending.command in _EDIT_COMMANDS and not _edits_allowed():
                raise PermissionError(
                    "Editing is disabled in the Bonsai MCP add-on settings. "
                    "Enable it in the 3D viewport sidebar (N) > Bonsai MCP > "
                    "Allow edits. Query tools keep working in read-only mode."
                )
            pending.result = handler(pending.params)
        except (Exception, SystemExit) as exc:
            # SystemExit too: exec()'d code calling sys.exit() must not kill
            # this timer (that would strand every future request)
            pending.error = f"{type(exc).__name__}: {exc}"
            pending.traceback = traceback.format_exc()
        finally:
            pending.finished = True
            pending.done.set()
            _record_activity(pending, time.perf_counter() - started)
    if drained:
        _redraw_view3d_areas()
    return 0.05


def _flush_queue(reason: str) -> None:
    """Fail every queued request; called when the bridge stops."""
    q: queue.Queue = _STATE["request_queue"]  # type: ignore[assignment]
    while True:
        try:
            pending: _PendingRequest = q.get_nowait()
        except queue.Empty:
            return
        pending.error = reason
        pending.finished = True
        pending.done.set()


def _ensure_timer_running() -> None:
    if _STATE.get("server") is None:
        # never (re)register the drain timer for a stopped bridge; handler
        # threads that outlive _stop_server call this on every request
        return
    if bpy.app.timers.is_registered(_drain_queue):
        _STATE["timer_registered"] = True
        return
    # persistent=True: file loads silently unregister non-persistent timers
    bpy.app.timers.register(_drain_queue, persistent=True)
    _STATE["timer_registered"] = True


def _ensure_timer_stopped() -> None:
    if bpy.app.timers.is_registered(_drain_queue):
        bpy.app.timers.unregister(_drain_queue)
    _STATE["timer_registered"] = False

class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


# Idle timeout for an open client connection; the persistent client
# reconnects transparently when it expires.
_IDLE_TIMEOUT_SECONDS = 300.0


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        """Read framed requests until the client disconnects (socketserver hook)."""
        sock: socket.socket = self.request
        sock.settimeout(_IDLE_TIMEOUT_SECONDS)
        try:
            while True:
                try:
                    message = _read_message(sock)
                except _ProtocolError as exc:
                    with contextlib.suppress(OSError):
                        _send_message(
                            sock, {"success": False, "error": f"Protocol error: {exc}"}
                        )
                    self._drain_and_close(sock)
                    return
                if message is None:
                    return

                request_id = message.get("id")

                def _reply(payload: dict, _rid=request_id) -> None:
                    # echo the client's request id so a persistent connection
                    # can detect out-of-sync replies
                    if _rid is not None:
                        payload["id"] = _rid
                    _send_message(sock, payload)

                if _STATE.get("server") is not self.server:
                    # the bridge was stopped while this connection stayed open;
                    # refuse instead of executing commands on a "stopped" bridge
                    with contextlib.suppress(OSError):
                        _reply({"success": False, "error": "bridge stopped"})
                    return

                expected_token = str(_STATE.get("token") or "")
                if expected_token:
                    provided = message.get("token")
                    if not isinstance(provided, str) or not hmac.compare_digest(
                        provided, expected_token
                    ):
                        with contextlib.suppress(OSError):
                            _reply(
                                {
                                    "success": False,
                                    "error": (
                                        "Invalid or missing bridge token. This "
                                        "bridge requires a shared secret: set "
                                        "BONSAI_MCP_TOKEN on the client to the "
                                        "token configured in the add-on "
                                        "preferences."
                                    ),
                                }
                            )
                        self._drain_and_close(sock)
                        return

                command = str(message.get("command", ""))
                params = message.get("params") or {}

                pending = _PendingRequest(command, params)
                _STATE["request_queue"].put(pending)  # type: ignore[union-attr]

                # re-register the drain timer if something killed it;
                # bpy.app.timers is safe to call from this thread
                with contextlib.suppress(Exception):
                    _ensure_timer_running()

                if not self._await_result(sock, pending):
                    # client is gone; the request was cancelled, send nothing
                    return

                if pending.finished:
                    if pending.error is not None:
                        _reply(
                            {
                                "success": False,
                                "error": pending.error,
                                "traceback": pending.traceback,
                            }
                        )
                    else:
                        _reply({"success": True, "result": pending.result})
                elif pending.cancel_reason == "client_closed":
                    _reply(
                        {
                            "success": False,
                            "error": (
                                "The request was cancelled because the client "
                                "closed the connection before a result was ready; "
                                "it will not run."
                            ),
                        }
                    )
                else:
                    _reply(
                        {
                            "success": False,
                            "error": (
                                f"Timed out after {MAIN_THREAD_WAIT_SECONDS:.0f}s waiting "
                                "for Blender's main thread; the queued request was "
                                "cancelled and will not run (unless it had already "
                                "started). Blender may be busy with a modal operation "
                                "or a long-running script."
                            ),
                        }
                    )
        except OSError:
            # normal client disconnect (WinError 10053 on Windows), not an error
            return

    @staticmethod
    def _drain_and_close(sock: socket.socket) -> None:
        """Half-close and drain unread input so the error reply is delivered.

        Closing a socket with unread received data (e.g. the body of an
        oversized frame) sends an RST that can destroy the just-sent reply.
        Draining (bounded) lets the close end with an orderly FIN instead.
        """
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(2.0)
            drained = 0
            while drained < 1024 * 1024:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                drained += len(chunk)

    @staticmethod
    def _await_result(sock: socket.socket, pending: _PendingRequest) -> bool:
        """Wait for the main thread, watching for client disconnect and timeout.

        Returns False when the client is gone and nothing should be sent.
        On timeout or bridge stop, sets `cancelled` so the drain loop skips
        the request, gives the main thread a short grace period in case it
        finished concurrently, and returns True so the caller reports the
        outcome.
        """
        deadline = time.monotonic() + MAIN_THREAD_WAIT_SECONDS
        poll_socket = True
        while not pending.done.wait(timeout=0.5):
            if _STATE.get("server") is None:
                # bridge stopped while this request waited; nothing will run it
                pending.cancelled = True
                if not pending.done.is_set():
                    pending.error = "bridge stopped before the request could run"
                    pending.finished = True
                    pending.done.set()
                return True
            if time.monotonic() >= deadline:
                pending.cancelled = True
                pending.cancel_reason = pending.cancel_reason or "timeout"
                pending.done.wait(timeout=0.05)
                return True
            if not poll_socket:
                continue
            try:
                readable, _, _ = select.select([sock], [], [], 0)
                if readable and sock.recv(1, socket.MSG_PEEK) == b"":
                    # EOF: the client closed its side - either a full close
                    # (gone for good) or a half-close (legal for a one-shot
                    # send/shutdown(SHUT_WR)/recv exchange, still reading).
                    # TCP cannot distinguish the two, so: cancel the request
                    # if it has not started (safety: no ghost edits), but
                    # keep waiting for an in-flight result and attempt to
                    # deliver the reply; a fully-closed peer surfaces as
                    # OSError on send, which the caller swallows.
                    pending.cancelled = True
                    pending.cancel_reason = "client_closed"
                    poll_socket = False
            except OSError:
                pending.cancelled = True
                return False
        return True


def _start_server(host: str, port: int, token: str = "") -> bool:
    """Start the bridge. Returns False when it was already running.

    The token is snapshotted here (main thread) so handler threads never
    read bpy preferences; changing the preference therefore requires a
    bridge restart to take effect.
    """
    if _STATE.get("server") is not None:
        return False
    server = _ThreadedTCPServer((host, port), _BridgeHandler)
    thread = threading.Thread(
        target=server.serve_forever, name="bonsai-mcp-bridge", daemon=True
    )
    _STATE["server"] = server
    _STATE["thread"] = thread
    _STATE["bound"] = tuple(server.server_address[:2])
    _STATE["last_error"] = None
    _STATE["token"] = str(token or "")
    thread.start()
    _ensure_timer_running()
    bound = _STATE["bound"]
    print(f"[bonsai-mcp-bridge] listening on {bound[0]}:{bound[1]}")  # type: ignore[index]
    return True


def _bound_address() -> tuple[str, int] | None:
    bound = _STATE.get("bound")
    if isinstance(bound, tuple) and len(bound) == 2:
        return bound  # type: ignore[return-value]
    return None


def _cleanup_screenshot_dir() -> None:
    tmp = _STATE.pop("screenshot_dir", None)
    if isinstance(tmp, str):
        shutil.rmtree(tmp, ignore_errors=True)


def _stop_server() -> None:
    server = _STATE.get("server")
    if server is None:
        _ensure_timer_stopped()
        _flush_queue("bridge stopped before the request could run")
        _cleanup_screenshot_dir()
        return
    try:
        server.shutdown()  # type: ignore[attr-defined]
        server.server_close()  # type: ignore[attr-defined]
    finally:
        _STATE["server"] = None
        _STATE["thread"] = None
        _STATE["bound"] = None
        _STATE["token"] = ""
        _ensure_timer_stopped()
        # requests queued but never run must not leave clients waiting
        _flush_queue("bridge stopped before the request could run")
        _cleanup_screenshot_dir()
        _cleanup_reload_dir()
        print("[bonsai-mcp-bridge] stopped")

class BONSAI_MCP_AddonPrefs(AddonPreferences):
    bl_idname = __name__

    host: StringProperty(  # type: ignore[valid-type]
        name="Host",
        default=DEFAULT_HOST,
        description="Bind address. Leave as 127.0.0.1 unless you know what you're doing.",
    )
    port: IntProperty(  # type: ignore[valid-type]
        name="Port",
        default=DEFAULT_PORT,
        min=1024,
        max=65535,
    )
    allow_edits: BoolProperty(  # type: ignore[valid-type]
        name="Allow edits",
        default=True,
        description=(
            "Allow the MCP EDIT tools (execute_ifc_code, execute_blender_code, "
            "save_ifc_file) to run. Disable for a read-only session: queries and "
            "screenshots keep working, code execution and saving are blocked"
        ),
    )
    token: StringProperty(  # type: ignore[valid-type]
        name="Token",
        default="",
        subtype="PASSWORD",
        description=(
            "Optional shared secret. When set, every bridge request must "
            "carry the same token (client side: BONSAI_MCP_TOKEN). Useful on "
            "shared machines where loopback is not a user boundary. Applied "
            "on bridge start; restart the bridge after changing it"
        ),
    )

    def draw(self, _context) -> None:
        layout = self.layout
        layout.label(text="Bridge bind address (local only):")
        layout.prop(self, "host")
        layout.prop(self, "port")
        layout.prop(self, "allow_edits")
        layout.prop(self, "token")
        layout.label(text="Token changes apply on the next bridge start.")
        layout.label(text="Warning: do not bind to a non-loopback address.", icon="ERROR")


def _prefs():
    return bpy.context.preferences.addons[__name__].preferences  # type: ignore[index]


class BONSAI_MCP_OT_StartBridge(Operator):
    bl_idname = "bonsai_mcp.start_bridge"
    bl_label = "Start Bridge"
    bl_description = "Start the local TCP bridge for the bonsai-mcp server."

    def execute(self, _context):
        prefs = _prefs()
        try:
            started = _start_server(prefs.host, prefs.port, getattr(prefs, "token", ""))
        except OSError as exc:
            message = (
                f"Could not start bridge on {prefs.host}:{prefs.port}: {exc}. "
                "The port may be in use by another application (for example a "
                "different Blender MCP bridge). Pick another port here and set "
                "BONSAI_MCP_PORT to match on the client side."
            )
            if getattr(exc, "errno", None) == errno.EADDRINUSE:
                message += (
                    " If you just stopped the bridge, the port can stay reserved "
                    "for a short time (TIME_WAIT); wait a few seconds and press "
                    "Start again."
                )
            _STATE["last_error"] = f"Start failed: port {prefs.port} unavailable"
            self.report({"ERROR"}, message)
            return {"CANCELLED"}
        bound = _bound_address()
        if not started and bound is not None:
            self.report(
                {"INFO"},
                f"Bridge already running on {bound[0]}:{bound[1]} "
                "(stop it first to rebind with new preferences)",
            )
            return {"FINISHED"}
        if bound is not None:
            self.report({"INFO"}, f"Bridge listening on {bound[0]}:{bound[1]}")
        else:
            self.report({"INFO"}, f"Bridge listening on {prefs.host}:{prefs.port}")
        return {"FINISHED"}


class BONSAI_MCP_OT_StopBridge(Operator):
    bl_idname = "bonsai_mcp.stop_bridge"
    bl_label = "Stop Bridge"
    bl_description = "Stop the local TCP bridge."

    def execute(self, _context):
        _stop_server()
        self.report({"INFO"}, "Bridge stopped")
        return {"FINISHED"}


class BONSAI_MCP_PT_Panel(Panel):
    bl_label = "Bonsai MCP"
    bl_idname = "BONSAI_MCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Bonsai MCP"

    def draw(self, _context) -> None:
        layout = self.layout
        prefs = _prefs()
        running = _STATE.get("server") is not None
        bound = _bound_address()

        col = layout.column(align=True)
        col.label(
            text=f"Status: {'running' if running else 'stopped'}",
            icon="CHECKMARK" if running else "X",
        )
        if running and bound is not None:
            # the address actually bound, not the (editable) preference
            col.label(text=f"Listening on {bound[0]}:{bound[1]}")
        else:
            col.label(text=f"Bind: {prefs.host}:{prefs.port}")
        if running and _STATE.get("token"):
            col.label(text="Token required by clients", icon="LOCKED")
        last_error = _STATE.get("last_error")
        if last_error:
            col.label(text=str(last_error), icon="ERROR")
        col.separator()

        row = col.row(align=True)
        start = row.row(align=True)
        start.enabled = not running
        start.operator(BONSAI_MCP_OT_StartBridge.bl_idname, icon="PLAY")
        stop = row.row(align=True)
        stop.enabled = running
        stop.operator(BONSAI_MCP_OT_StopBridge.bl_idname, icon="PAUSE")
        col.separator()

        col.prop(prefs, "allow_edits")
        if prefs.allow_edits:
            warn = col.row()
            warn.alert = True
            warn.label(text="Edits on: AI can modify the model.", icon="UNLOCKED")
        else:
            col.label(text="Read-only: EDIT tools are blocked.", icon="LOCKED")

        if running:
            col.separator()
            activity = _STATE.get("activity")
            if isinstance(activity, collections.deque) and activity:
                col.label(text="Recent:")
                for line in list(activity):
                    col.label(text=f"  {line}")

        col.separator()
        col.label(text="Local trusted use only.", icon="ERROR")


_CLASSES = (
    BONSAI_MCP_AddonPrefs,
    BONSAI_MCP_OT_StartBridge,
    BONSAI_MCP_OT_StopBridge,
    BONSAI_MCP_PT_Panel,
)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    _stop_server()
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
