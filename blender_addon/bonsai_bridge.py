"""Local Blender bridge for Bonsai MCP."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import queue
import re as _re
import socket
import socketserver
import struct
import threading
import traceback

import bpy
from bpy.props import IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

bl_info = {
    "name": "Bonsai MCP Bridge",
    "author": "Show2Instruct",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Bonsai MCP",
    "description": "Localhost TCP bridge for the bonsai-mcp server.",
    "category": "Development",
}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9878
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
_STATE: dict[str, object] = {
    "server": None,
    "thread": None,
    "request_queue": queue.Queue(),
    "timer_registered": False,
}

def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _read_message(sock: socket.socket) -> dict | None:
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length > MAX_MESSAGE_BYTES:
        return None
    body = _recv_exact(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


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


def _get_loaded_ifc():
    """Return the loaded IFC file, if available."""
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
    tool_mod.IfcGit.load_project(path)


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


def _ifc_class_for_object(obj) -> str | None:
    """Best-effort IFC class lookup for a Blender object."""
    ifc = _get_loaded_ifc()
    ifc_def_id = None
    try:
        ifc_def_id = obj.BIMObjectProperties.ifc_definition_id  # type: ignore[attr-defined]
    except Exception:
        ifc_def_id = None

    if ifc is not None and ifc_def_id:
        try:
            element = ifc.by_id(ifc_def_id)
            return element.is_a()
        except Exception:
            pass

    name = getattr(obj, "name", "") or ""
    if "/" in name:
        prefix = name.split("/", 1)[0]
        if prefix.startswith("Ifc"):
            return prefix
    return None


def _global_id_for_object(obj) -> str | None:
    ifc = _get_loaded_ifc()
    ifc_def_id = None
    try:
        ifc_def_id = obj.BIMObjectProperties.ifc_definition_id  # type: ignore[attr-defined]
    except Exception:
        return None

    if ifc is not None and ifc_def_id:
        try:
            element = ifc.by_id(ifc_def_id)
            return getattr(element, "GlobalId", None)
        except Exception:
            return None
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
    }


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


def _filter_objects(objects, params) -> list[dict]:
    """Apply the optional `query` filter and return a list of object summaries."""
    query = (params.get("query") or "").strip().lower()
    if not query:
        return []

    if query == "all":
        return [_object_summary(o) for o in objects]

    if query == "selected":
        return [_object_summary(o) for o in bpy.context.selected_objects]

    if query in _QUERY_KEYWORD_TO_CLASS:
        target = _QUERY_KEYWORD_TO_CLASS[query]
        return [_object_summary(o) for o in objects if _ifc_class_for_object(o) == target]

    if query == "by_class":
        target = params.get("ifc_class")
        if not target:
            raise ValueError("query='by_class' requires 'ifc_class'")
        return [_object_summary(o) for o in objects if _ifc_class_for_object(o) == target]

    if query == "by_name":
        target = params.get("name")
        if not target:
            raise ValueError("query='by_name' requires 'name'")
        return [_object_summary(o) for o in objects if o.name == target]

    if query == "by_global_id":
        target = params.get("global_id")
        if not target:
            raise ValueError("query='by_global_id' requires 'global_id'")
        return [_object_summary(o) for o in objects if _global_id_for_object(o) == target]

    raise ValueError(f"Unknown query: {query!r}")


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
        "selected_objects": selected,
        "collections": collections,
        "object_type_counts": object_type_counts,
        "ifc_available": _get_loaded_ifc() is not None,
        "blender_version": bpy.app.version_string,
    }

    if params and (params.get("query") or "").strip():
        payload["objects"] = _filter_objects(objects, params)

    return payload


def _h_get_selected_objects(_params):
    return [_object_summary(o) for o in bpy.context.selected_objects]


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
    except Exception as exc:
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
    except Exception as exc:
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


def _find_view3d():
    """Return (window, area, region) for the first open 3D viewport, or Nones."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    return window, area, region
    return None, None, None


def _orient_viewport(view: str | None, fit: str | None) -> None:
    """Aim the first 3D viewport before capturing (persists after the call)."""
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

            if fit == "all":
                bpy.ops.view3d.view_all()
            elif fit == "selected":
                if not bpy.context.selected_objects:
                    raise ValueError("fit='selected' requires at least one selected object.")
                bpy.ops.view3d.view_selected()
            elif fit:
                raise ValueError(f"'fit' must be one of {_VALID_FITS}, got {fit!r}")
    finally:
        prefs_view.smooth_view = original_smooth


def _viewport_snapshot(include_objects: bool, max_objects: int) -> dict | None:
    """Structured viewport state, optionally with screen-space object boxes.

    Box format: [x_min, y_min, x_max, y_max], normalized 0-1, origin at the
    image's top-left. Boxes are approximate (region aspect may differ slightly
    from the rendered image) but preserve relative spatial layout.
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

    boxes: list[tuple[float, dict]] = []
    for obj in bpy.context.view_layer.objects:
        if obj.type != "MESH" or not obj.visible_get():
            continue
        pts = []
        for corner in obj.bound_box:
            point = location_3d_to_region_2d(region, rv3d, obj.matrix_world @ Vector(corner))
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
        boxes.append(
            (
                (x1 - x0) * (y1 - y0),
                {
                    "name": obj.name,
                    "ifc_class": _ifc_class_for_object(obj),
                    "global_id": _global_id_for_object(obj),
                    "box": [round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)],
                },
            )
        )
    boxes.sort(key=lambda item: item[0], reverse=True)
    state["objects_in_view"] = [entry for _, entry in boxes[:max_objects]]
    state["objects_in_view_total"] = len(boxes)
    state["objects_truncated"] = len(boxes) > max_objects
    return state


def _screenshot_output_path(ext: str) -> str:
    """Return the viewport screenshot path for the given extension."""
    tmp_dir = _STATE.get("screenshot_dir")
    if not isinstance(tmp_dir, str):
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="bonsai_mcp_")
        _STATE["screenshot_dir"] = tmp_dir
    return os.path.join(tmp_dir, f"viewport.{ext}")


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
    if view or fit:
        _orient_viewport(view, fit)

    out_path = _screenshot_output_path("jpg" if is_jpeg else "png")

    scene = bpy.context.scene
    render = scene.render
    settings = render.image_settings
    original_filepath = render.filepath
    original_format = settings.file_format
    original_color_mode = settings.color_mode
    original_quality = settings.quality
    original_res = (render.resolution_x, render.resolution_y, render.resolution_percentage)

    # render.opengl uses the scene render resolution; downscale only, never upscale
    effective_x = max(1, int(render.resolution_x * render.resolution_percentage / 100))
    effective_y = max(1, int(render.resolution_y * render.resolution_percentage / 100))
    scale = min(1.0, max_size / max(effective_x, effective_y))
    width = max(1, int(effective_x * scale))
    height = max(1, int(effective_y * scale))

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
        window, area, region = _find_view3d()
        if window is not None:
            with bpy.context.temp_override(window=window, area=area, region=region):
                bpy.ops.render.opengl(write_still=True)
        else:
            bpy.ops.render.opengl(write_still=True)
        with open(out_path, "rb") as fh:
            image_bytes = fh.read()
    finally:
        render.filepath = original_filepath
        settings.file_format = original_format
        settings.color_mode = original_color_mode
        settings.quality = original_quality
        render.resolution_x, render.resolution_y, render.resolution_percentage = original_res

    encoded = base64.b64encode(image_bytes).decode("ascii")
    if len(encoded) > _SCREENSHOT_MAX_B64_CHARS:
        raise RuntimeError(
            f"Screenshot is still {len(image_bytes)} bytes at {width}x{height}, "
            "too large for an MCP response. Retry with a smaller max_size "
            "and/or format='jpeg'."
        )
    return {
        "path": out_path,
        "image_base64": encoded,
        "base64_chars": len(encoded),
        "format": "jpeg" if is_jpeg else "png",
        "width": width,
        "height": height,
        "bytes": len(image_bytes),
        "view": view,
        "fit": fit,
        "viewport": _viewport_snapshot(include_objects, max_objects),
    }

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


def _find_ifc_element(ifc, name: str | None, global_id: str | None):
    """Resolve an IFC element and its Blender object name."""
    if global_id:
        try:
            element = ifc.by_guid(global_id)
        except Exception:
            element = None
        if element is not None:
            blender_name = None
            for obj in bpy.context.scene.objects:
                try:
                    if obj.BIMObjectProperties.ifc_definition_id and (  # type: ignore[attr-defined]
                        ifc.by_id(obj.BIMObjectProperties.ifc_definition_id)  # type: ignore[attr-defined]
                        is element
                    ):
                        blender_name = obj.name
                        break
                except Exception:
                    continue
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

    results: list[dict] = []
    for request, lookup_name, lookup_gid in requests:
        element, blender_name = _find_ifc_element(ifc, lookup_name, lookup_gid)
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


_HANDLERS = {
    "ping": _h_ping,
    "get_scene_info": _h_get_scene_info,
    "get_selected_objects": _h_get_selected_objects,
    "execute_code": _h_execute_code,
    "execute_ifc_code": _h_execute_ifc_code,
    "get_viewport_screenshot": _h_get_viewport_screenshot,
    "get_ifc_project_info": _h_get_ifc_project_info,
    "get_psets": _h_get_psets,
    "save_ifc_file": _h_save_ifc_file,
}

class _PendingRequest:
    """Bundle a request with an Event that is set when the result is ready."""

    __slots__ = ("command", "params", "result", "error", "traceback", "done")

    def __init__(self, command: str, params: dict) -> None:
        self.command = command
        self.params = params
        self.result = None
        self.error: str | None = None
        self.traceback: str | None = None
        self.done = threading.Event()


def _drain_queue() -> float:
    """Timer callback. Runs on the main thread, executes queued commands."""
    q: queue.Queue = _STATE["request_queue"]  # type: ignore[assignment]
    drained = 0
    while drained < 8:
        try:
            pending: _PendingRequest = q.get_nowait()
        except queue.Empty:
            break
        handler = _HANDLERS.get(pending.command)
        try:
            if handler is None:
                raise ValueError(f"Unknown command: {pending.command!r}")
            pending.result = handler(pending.params)
        except Exception as exc:
            pending.error = f"{type(exc).__name__}: {exc}"
            pending.traceback = traceback.format_exc()
        finally:
            pending.done.set()
            drained += 1
    return 0.05


def _ensure_timer_running() -> None:
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


class _BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        """Read framed requests until the client disconnects (socketserver hook)."""
        sock: socket.socket = self.request
        sock.settimeout(60.0)
        try:
            while True:
                message = _read_message(sock)
                if message is None:
                    return
                command = message.get("command", "")
                params = message.get("params") or {}

                pending = _PendingRequest(command, params)
                _STATE["request_queue"].put(pending)  # type: ignore[union-attr]

                # re-register the drain timer if something killed it;
                # bpy.app.timers is safe to call from this thread
                with contextlib.suppress(Exception):
                    _ensure_timer_running()

                pending.done.wait(timeout=120.0)

                if not pending.done.is_set():
                    _send_message(sock, {"success": False, "error": "timeout waiting for Blender main thread"})
                    continue

                if pending.error is not None:
                    _send_message(
                        sock,
                        {
                            "success": False,
                            "error": pending.error,
                            "traceback": pending.traceback,
                        },
                    )
                else:
                    _send_message(sock, {"success": True, "result": pending.result})
        except OSError:
            # normal client disconnect (WinError 10053 on Windows), not an error
            return


def _start_server(host: str, port: int) -> None:
    if _STATE.get("server") is not None:
        return
    server = _ThreadedTCPServer((host, port), _BridgeHandler)
    thread = threading.Thread(
        target=server.serve_forever, name="bonsai-mcp-bridge", daemon=True
    )
    _STATE["server"] = server
    _STATE["thread"] = thread
    thread.start()
    _ensure_timer_running()
    print(f"[bonsai-mcp-bridge] listening on {host}:{port}")


def _stop_server() -> None:
    server = _STATE.get("server")
    if server is None:
        _ensure_timer_stopped()
        return
    try:
        server.shutdown()  # type: ignore[attr-defined]
        server.server_close()  # type: ignore[attr-defined]
    finally:
        _STATE["server"] = None
        _STATE["thread"] = None
        _ensure_timer_stopped()
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

    def draw(self, _context) -> None:
        layout = self.layout
        layout.label(text="Bridge bind address (local only):")
        layout.prop(self, "host")
        layout.prop(self, "port")
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
            _start_server(prefs.host, prefs.port)
        except OSError as exc:
            self.report(
                {"ERROR"},
                f"Could not start bridge on {prefs.host}:{prefs.port}: {exc}. "
                "The port may be in use by another application (for example a "
                "different Blender MCP bridge). Pick another port here and set "
                "BONSAI_MCP_PORT to match on the client side.",
            )
            return {"CANCELLED"}
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

        col = layout.column(align=True)
        col.label(text=f"Status: {'running' if running else 'stopped'}")
        col.label(text=f"Bind: {prefs.host}:{prefs.port}")
        col.separator()
        if running:
            col.operator(BONSAI_MCP_OT_StopBridge.bl_idname, icon="PAUSE")
        else:
            col.operator(BONSAI_MCP_OT_StartBridge.bl_idname, icon="PLAY")
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
