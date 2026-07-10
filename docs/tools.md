# Tools reference

Bonsai MCP exposes eight tools, split into two categories:

| Category | Behaviour | Tools |
| --- | --- | --- |
| **QUERY** | Read-only. Safe to call without confirmation. | `get_scene_info`, `get_selected_objects`, `get_psets`, `get_viewport_screenshot`, `get_ifc_project_info` |
| **EDIT** | Mutates Blender state, the IFC model, or the filesystem. | `execute_ifc_code`, `execute_blender_code`, `save_ifc_file` |

The category is encoded in two places:

- A `[QUERY]` or `[EDIT]` prefix at the start of every tool description.
- The standard MCP `Tool.annotations.readOnlyHint` field, which MCP clients
  can read to render the distinction natively.

Two code execution tools live in the EDIT category: `execute_ifc_code`
(preferred, IFC-only, `bpy` blocked) and `execute_blender_code` (full `bpy`
access). See [Safety](safety.md).

## `get_scene_info` (QUERY)

Returns a scene-level snapshot. When `query` is supplied, the response also
includes an `objects` array filtered by the query.

Inputs (all optional):

```json
{
  "query": "walls",
  "ifc_class": null,
  "name": null,
  "global_id": null
}
```

Supported `query` values:

| Query | Notes |
| --- | --- |
| (omitted) | Scene summary only; no `objects` field. |
| `all` | All objects in the scene. |
| `selected` | Currently selected objects. |
| `walls`, `doors`, `windows`, `spaces`, `slabs`, `columns`, `beams`, `roofs`, `stairs` | Filtered by IFC class. |
| `by_class` | Requires `ifc_class`, e.g. `"IfcCovering"`. |
| `by_name` | Requires `name` (exact Blender object name). |
| `by_global_id` | Requires `global_id` (IFC GlobalId). |

Returns:

```json
{
  "scene_name": "Scene",
  "object_count": 42,
  "selected_count": 1,
  "selected_objects": ["IfcWall/MyWall"],
  "collections": ["IfcSite", "IfcBuilding"],
  "object_type_counts": {"MESH": 40, "EMPTY": 2},
  "ifc_available": true,
  "blender_version": "4.2.0",
  "objects": [
    {
      "name": "IfcWall/MyWall",
      "type": "MESH",
      "location": [0.0, 0.0, 0.0],
      "dimensions": [5.0, 0.2, 3.0],
      "ifc_class": "IfcWall",
      "global_id": "2O2Fr$t4X7Zf8NOew3FK6X"
    }
  ]
}
```

The `objects` field is omitted entirely when no `query` is supplied.

## `get_selected_objects` (QUERY)

Inputs: none.

Returns a list of:

```json
{
  "name": "IfcWall/MyWall",
  "type": "MESH",
  "location": [0.0, 0.0, 0.0],
  "dimensions": [5.0, 0.2, 3.0],
  "ifc_class": "IfcWall",
  "global_id": "2O2Fr$t4X7Zf8NOew3FK6X"
}
```

`ifc_class` and `global_id` are `null` if no IFC data is associated.

## `get_psets` (QUERY)

Returns IFC property sets and quantity sets for one or more objects.
Accepts any mix of GlobalIds and Blender object names. Up to 100 targets
per call (combined across both lists).

Inputs (at least one entry required between the two lists):

```json
{
  "global_ids": ["2O2Fr$t4X7Zf8NOew3FK6X"],
  "names": ["IfcWall/MyWall"]
}
```

Returns an ordered `results` list that mirrors the input order
(GlobalIds first, then names). Each entry records the original
`request`, plus either the pset payload or an `error` field:

```json
{
  "results": [
    {
      "request": {"global_id": "2O2Fr$t4X7Zf8NOew3FK6X"},
      "object": {
        "name": "IfcWall/MyWall",
        "global_id": "2O2Fr$t4X7Zf8NOew3FK6X",
        "ifc_class": "IfcWall"
      },
      "property_sets": {
        "Pset_WallCommon": {
          "IsExternal": true,
          "LoadBearing": false,
          "FireRating": "F30"
        }
      },
      "quantity_sets": {
        "Qto_WallBaseQuantities": {
          "Length": 5.0,
          "Height": 3.0,
          "NetArea": 14.5
        }
      }
    },
    {
      "request": {"name": "IfcDoor/Missing"},
      "error": "not found"
    }
  ]
}
```

Each `property_sets` / `quantity_sets` map passes through IfcOpenShell's native
`ifcopenshell.util.element.get_psets` shape, so besides the real property names
each set also carries an integer `id` key (the STEP id of the underlying
`IfcPropertySet` / `IfcElementQuantity`); clients can ignore it.

Returns a clear error if no IFC project is loaded. Per-target lookup
failures (missing GlobalId, missing object name, object without IFC link)
appear as `error` entries in the results list rather than aborting the
batch.

## `get_viewport_screenshot` (QUERY)

Captures the 3D viewport, optionally aiming and framing it first. The
render is downscaled so its longest edge fits `max_size` and encoded as
JPEG by default, keeping the response safely inside MCP size caps (some
clients enforce 1 MB per tool result). Scene render settings are restored
after the capture; the viewport orientation persists.

Inputs (all optional):

```json
{
  "max_size": 800,
  "format": "jpeg",
  "quality": 85,
  "view": "iso",
  "fit": "all",
  "include_objects": false,
  "max_objects": 50
}
```

| Input | Values | Notes |
| --- | --- | --- |
| `max_size` | 64-2048, default 800 | Longest edge in px. Downscales only, never upscales. |
| `format` | `jpeg` (default), `png` | JPEG is much smaller; PNG is lossless. |
| `quality` | 1-100, default 85 | JPEG only. |
| `view` | `top`, `bottom`, `front`, `back`, `left`, `right`, `iso`, `camera` | Aims the viewport first. Axis names give orthographic views, `iso` a perspective isometric, `camera` the scene camera. Omit to keep the current orientation. |
| `fit` | `all`, `selected` | Frames everything or the current selection. Combines with `view`. |
| `include_objects` | boolean, default false | Adds screen-space 2D bounding boxes keyed by GlobalId to the text output. |
| `max_objects` | 1-200, default 50 | Cap for `include_objects`; largest boxes kept, truncation flagged. |

Returns, in order:

1. An MCP **image content block** (`image/jpeg` or `image/png`). The image
   comes first because some MCP clients mishandle mixed content.
2. A **text block** with the saved file path, dimensions and byte size, the
   attached base64 length (so a client-side image drop is diagnosable from
   text), and structured **viewport state**: rotation quaternion,
   perspective mode (`PERSP`/`ORTHO`/`CAMERA`), `is_orthographic_side_view`,
   view distance, and pivot location.

With `include_objects=true`, the viewport state also lists in-frame objects:

```json
{
  "objects_in_view": [
    {
      "name": "IfcWall/MyWall",
      "ifc_class": "IfcWall",
      "global_id": "2O2Fr$t4X7Zf8NOew3FK6X",
      "box": [0.329, 0.55, 0.712, 0.563]
    }
  ],
  "objects_in_view_total": 1250,
  "objects_truncated": true
}
```

`box` is `[x_min, y_min, x_max, y_max]`, normalized 0-1 with the origin at
the image's top-left (smaller `y` is higher on screen). Boxes are
approximate but preserve relative spatial layout, which lets a text-only
agent reason about containment, left/right/above/below relations, and
relative sizes even when its client does not deliver tool-result images.

If Blender is running in `--background` mode or has no usable 3D viewport,
the tool returns an error message instead (and `view`/`fit` require an open
3D viewport).

## `get_ifc_project_info` (QUERY)

Inputs: none.

Returns:

```json
{
  "schema": "IFC4",
  "project_name": "My Project",
  "project_global_id": "0YvctVUKr0kugbFTf53O9L",
  "entity_counts": {
    "IfcSite": 1,
    "IfcBuilding": 1,
    "IfcBuildingStorey": 3,
    "IfcWall": 28,
    "IfcDoor": 9,
    "IfcWindow": 14,
    "IfcSlab": 6,
    "IfcSpace": 22,
    "IfcColumn": 0,
    "IfcBeam": 0,
    "IfcRoof": 0,
    "IfcStair": 0
  },
  "materials": {
    "count": 14,
    "names": ["Concrete", "Glass", "Steel", "Timber"],
    "truncated": false
  },
  "classifications": {
    "count": 1,
    "systems": [
      {"name": "Uniclass 2015", "source": "NBS", "edition": "v1.20"}
    ],
    "truncated": false
  }
}
```

`materials.names` is sorted and de-duplicated; `materials.count` is the raw
`IfcMaterial` count. Both lists are capped at 100 entries; `truncated: true`
means more entities exist than were returned.

If no IFC project is loaded, returns a clear error.

## `execute_ifc_code` (EDIT)

**Preferred code execution tool.** Runs IfcOpenShell / Bonsai API code with
`bpy` access blocked. Use this for all IFC/BIM data operations.

Pre-injected namespace (no imports needed):

| Variable | What it is |
| --- | --- |
| `ifc` | The currently loaded IFC file (`ifcopenshell.file` or `None`) |
| `ifcopenshell` | The `ifcopenshell` module |
| `ifc_api` | `ifcopenshell.api` (high-level create/edit operations) |
| `element_util` | `ifcopenshell.util.element` (psets, qtos, traversal) |
| `tool` | `bonsai.tool` module (or `None` if unavailable) |
| `get_ifc_file()` | Returns the loaded IFC file, raising a clear error if none is open |
| `get_default_container()` | Returns the active spatial container (e.g. the active storey) |
| `save_and_load_ifc(path=None)` | Saves the project (to its own file by default) and reloads it |

**Viewport sync:** edits made here change the in-memory IFC model but do
*not* appear in the Blender viewport until the project is reloaded. After a
batch of edits, call `save_and_load_ifc()` (or use `save_ifc_file` with
`reload=true`). Reload once per batch, not per edit; it rebuilds the whole
scene.

Inputs:

```json
{ "code": "walls = ifc.by_type('IfcWall')\nfor w in walls:\n    print(w.Name, w.GlobalId)" }
```

Returns:

```json
{
  "success": true,
  "stdout": "MyWall 2O2Fr$t4X7Zf8NOew3FK6X\n",
  "stderr": "",
  "stdout_truncated": false,
  "stderr_truncated": false,
  "stdout_bytes": 38,
  "stderr_bytes": 0,
  "error": null,
  "traceback": null,
  "ifc_available": true,
  "namespace_keys": ["ifc", "ifcopenshell", "ifc_api", "element_util", "tool",
                     "get_ifc_file", "get_default_container", "save_and_load_ifc"]
}
```

If the code contains `bpy` imports or references, the tool returns an error
directing you to use `execute_blender_code` instead.

## `execute_blender_code` (EDIT)

Fallback for operations that genuinely require `bpy` (viewport manipulation,
rendering, object transforms, modifiers). **For IFC/BIM data work, always
prefer `execute_ifc_code`.**

The same three helper functions as `execute_ifc_code` are pre-injected:
`get_ifc_file()`, `get_default_container()`, and `save_and_load_ifc(path=None)`.

Inputs:

```json
{ "code": "import bpy; print(len(bpy.context.scene.objects))" }
```

Returns:

```json
{
  "success": true,
  "stdout": "42\n",
  "stderr": "",
  "stdout_truncated": false,
  "stderr_truncated": false,
  "stdout_bytes": 3,
  "stderr_bytes": 0,
  "error": null,
  "traceback": null
}
```

Errors are reported with `success: false`, a stringified `error`, and the full
traceback.

`stdout`/`stderr` are each capped at 256 KB to keep one MCP message bounded;
when truncation occurs, the corresponding `_truncated` flag is `true` and
`_bytes` reports the original size before trimming.

## `save_ifc_file` (EDIT)

Inputs (all optional):

```json
{ "output_path": "/abs/path/to/output.ifc", "overwrite": false, "reload": false }
```

Two modes:

- **In-place save** (`output_path` omitted): saves the project back to its
  own file, like Bonsai's File > Save IFC. There is no overwrite guard here,
  since writing the project's own file is the point. Fails with a clear error if the project
  has never been saved (no path yet).
- **Save-as** (`output_path` given): writes to the new path. Refuses to
  overwrite an existing file unless `overwrite=true`; the parent directory
  must already exist. The project keeps pointing at its original file.

With `reload=true`, the project is reloaded from the saved file afterwards,
clearing and rebuilding the Blender scene so IFC-level edits made via
`execute_ifc_code` become visible in the viewport.

Returns:

```json
{
  "saved": true,
  "output_path": "/abs/path/to/output.ifc",
  "in_place": false,
  "method": "bonsai.bim.export_ifc.IfcExporter",
  "reloaded": false
}
```

`method` reports which writer ran: `"bonsai.bim.export_ifc.IfcExporter"`
(preferred; Bonsai's exporter first syncs pending Blender-side edits into
the IFC model) or `"ifcopenshell.write"` (fallback when Bonsai is not
available).
