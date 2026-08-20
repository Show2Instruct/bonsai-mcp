# Tools reference

Bonsai MCP exposes fourteen tools, split into two categories:

| Category | Behaviour | Tools |
| --- | --- | --- |
| **QUERY** | Read-only. Safe to call without confirmation. | `get_scene_info`, `get_selected_objects`, `list_elements`, `get_psets`, `get_viewport_screenshot`, `get_ifc_project_info`, `get_spatial_structure`, `get_quantities` |
| **EDIT** | Mutates Blender state, the IFC model, or the filesystem. | `execute_ifc_code`, `execute_blender_code`, `save_ifc_file`, `refresh_view`, `refresh_geometry`, `reload_project` |

The category is encoded in two places:

- A `[QUERY]` or `[EDIT]` prefix at the start of every tool description.
- The standard MCP `Tool.annotations` hints (`readOnlyHint`,
  `destructiveHint`, `idempotentHint`, `openWorldHint`), which MCP clients
  can read to render the distinction natively.

Every tool also declares an `outputSchema` and returns
`structuredContent` alongside the human-readable JSON text, so clients
that understand structured tool output get typed results for free. Input
schemas are generated from the same Pydantic models the server validates
with, so schema and behaviour cannot drift apart.

List-shaped results are paged: pass `limit`/`offset` where offered and
watch the `total`/`truncated` flags instead of requesting everything at
once.

Errors follow one convention: every failure states what went wrong and
what to do next (for example, the read-only refusal names the exact
Blender panel toggle to flip, and an invalid `selector` query returns a
syntax cheat sheet). Clients and models should follow the instruction in
the error instead of retrying the same call.

Two code execution tools live in the EDIT category: `execute_ifc_code`
(preferred, IFC-only, `bpy` blocked) and `execute_blender_code` (full `bpy`
access). See [Safety](safety.md).

## `get_scene_info` (QUERY)

Returns a scene-level snapshot. When `query` is supplied, the response also
includes an `objects` array filtered by the query.

For structured element listings, prefer the dedicated
[`list_elements`](#list_elements-query) tool; the `query` parameter here
remains for compatibility.

Inputs (all optional):

```json
{
  "query": "walls",
  "ifc_class": null,
  "name": null,
  "global_id": null,
  "limit": 200,
  "offset": 0
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
  ],
  "objects_total": 28,
  "objects_truncated": false,
  "objects_offset": 0,
  "objects_limit": 200
}
```

The `objects` field (and its paging metadata) is omitted entirely when no
`query` is supplied. On huge scenes, page with `limit`/`offset` instead of
raising the limit.

## `get_selected_objects` (QUERY)

Inputs (optional): `limit` (1-1000, default 200), because a box-select can
grab thousands of objects.

Returns:

```json
{
  "objects": [
    {
      "name": "IfcWall/MyWall",
      "type": "MESH",
      "location": [0.0, 0.0, 0.0],
      "dimensions": [5.0, 0.2, 3.0],
      "ifc_class": "IfcWall",
      "global_id": "2O2Fr$t4X7Zf8NOew3FK6X"
    }
  ],
  "total": 1,
  "truncated": false
}
```

`ifc_class` and `global_id` are `null` if no IFC data is associated.

## `list_elements` (QUERY)

Lists IFC-backed elements (objects without an IFC entity are skipped) with
structured filters, replacing most uses of `get_scene_info` queries.

Inputs (all optional):

```json
{
  "ifc_class": "IfcWall",
  "name_contains": "kitchen",
  "storey": "Level 1",
  "selector": "IfcWall, Pset_WallCommon.FireRating=F30",
  "limit": 200,
  "offset": 0
}
```

| Input | Notes |
| --- | --- |
| `ifc_class` | Inheritance-aware: `IfcWall` also matches `IfcWallStandardCase`. |
| `name_contains` | Case-insensitive substring match on the Blender object name. |
| `storey` | Name or GlobalId of an `IfcBuildingStorey`. Elements inside the storey's spaces count as in the storey. |
| `selector` | Optional IfcOpenShell selector query, applied on top of the other filters. Examples: `IfcWall, material=concrete`, `IfcWall, Pset_WallCommon.FireRating=F30`, `IfcElement, Name=/W.*1/` (regex). Uses `ifcopenshell.util.selector` syntax; an invalid query returns an error with a short cheat sheet. |
| `limit` / `offset` | Paging, 1-1000 per page (default 200). |

Returns:

```json
{
  "elements": [
    {
      "name": "IfcWall/MyWall",
      "type": "MESH",
      "location": [0.0, 0.0, 0.0],
      "dimensions": [5.0, 0.2, 3.0],
      "ifc_class": "IfcWall",
      "global_id": "2O2Fr$t4X7Zf8NOew3FK6X"
    }
  ],
  "total": 28,
  "truncated": false,
  "offset": 0,
  "limit": 200
}
```

## `get_spatial_structure` (QUERY)

Returns the project's spatial hierarchy as a tree: `IfcProject` ->
`IfcSite` -> `IfcBuilding` -> `IfcBuildingStorey` -> `IfcSpace`, with
storey elevations and (by default) counts of contained elements grouped by
IFC class. Answers "what is in this building, storey by storey" without
any code execution.

Inputs (optional): `include_element_counts` (default `true`).

Returns:

```json
{
  "schema": "IFC4",
  "tree": {
    "name": "My Project",
    "ifc_class": "IfcProject",
    "global_id": "0YvctVUKr0kugbFTf53O9L",
    "children": [
      {
        "name": "Site",
        "ifc_class": "IfcSite",
        "global_id": "...",
        "children": [
          {
            "name": "Building",
            "ifc_class": "IfcBuilding",
            "global_id": "...",
            "children": [
              {
                "name": "Level 1",
                "ifc_class": "IfcBuildingStorey",
                "global_id": "...",
                "elevation": 0.0,
                "element_counts": {"IfcDoor": 9, "IfcWall": 28},
                "element_total": 37,
                "children": [
                  {"name": "Kitchen", "ifc_class": "IfcSpace", "global_id": "..."}
                ]
              }
            ]
          }
        ]
      }
    ]
  }
}
```

## `get_quantities` (QUERY)

Quantity takeoff without code execution: aggregates every numeric value
found in elements' IFC quantity sets (base quantities such as walls'
`NetSideArea` or slabs' `GrossVolume`), grouped by IFC class and
optionally per building storey.

Inputs (all optional):

```json
{
  "ifc_classes": ["IfcWall", "IfcSlab"],
  "by_storey": false
}
```

`ifc_classes` defaults to the common building element classes (walls,
slabs, columns, beams, doors, windows, roofs, stairs, coverings, spaces);
matching is inheritance-aware.

Returns:

```json
{
  "classes": {
    "IfcWall": {
      "count": 28,
      "elements_without_quantities": 2,
      "quantities": {
        "NetSideArea": {"sum": 412.6, "elements": 26},
        "Length": {"sum": 148.2, "elements": 26}
      }
    }
  },
  "units": {"LENGTHUNIT": "millimetre", "AREAUNIT": "square metre"},
  "by_storey": {
    "Level 1": {
      "IfcWall": {"count": 12, "quantities": {"NetSideArea": {"sum": 180.1, "elements": 12}}}
    }
  }
}
```

`elements_without_quantities` is a model-quality signal: elements of that
class carrying no numeric quantities at all. `units` is a best-effort read
of the project's unit assignment. `by_storey` appears only when requested.

## `get_psets` (QUERY)

Returns IFC property sets and quantity sets for one or more objects.
Accepts any mix of GlobalIds and Blender object names. Large batches are
paged: `limit` targets (default and maximum 100) are processed per call,
starting at `offset` (GlobalIds first, then names); the response reports
`targets_total`, `truncated`, and `next_offset` so clients can continue.

Inputs (at least one entry required between the two lists):

```json
{
  "global_ids": ["2O2Fr$t4X7Zf8NOew3FK6X"],
  "names": ["IfcWall/MyWall"],
  "limit": 100,
  "offset": 0
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
  "azimuth": null,
  "elevation": null,
  "storey": null,
  "shading": null,
  "show_overlays": false,
  "include_objects": false,
  "max_objects": 50
}
```

| Input | Values | Notes |
| --- | --- | --- |
| `max_size` | 64-2048, default 800 | Longest edge in px. Downscales only, never upscales, and is additionally capped by the native viewport resolution. |
| `format` | `jpeg` (default), `png` | JPEG is much smaller; PNG is lossless. A PNG that is estimated to exceed the response size cap is auto-downgraded to JPEG *before* rendering, with a note in the response. |
| `quality` | 1-100, default 85 | JPEG only. |
| `view` | `top`, `bottom`, `front`, `back`, `left`, `right`, `iso`, `camera` | Aims the viewport first. Axis names give orthographic views, `iso` a perspective isometric, `camera` the scene camera. Omit to keep the current orientation. |
| `azimuth` / `elevation` | degrees | Arbitrary view direction instead of `view` (mutually exclusive with it). Azimuth 0 = front, counter-clockwise seen from above; elevation 0 = horizontal, 90 = bird's eye (defaults to 30 when only azimuth is given). `iso` equals azimuth 45, elevation 30. |
| `fit` | `all`, `selected` | Frames everything or the current selection. Framing is direction-aware: after the initial fit, the zoom is tightened to the content's projected 2D extent, so elevations fill the frame instead of the bounding sphere. |
| `storey` | storey Name or GlobalId | Isolates one `IfcBuildingStorey` for the shot: everything else is hidden and restored afterwards. `storey` + `view='top'` + `fit='all'` is a floor plan. |
| `shading` | `wireframe`, `solid`, `material`, `rendered`, `class_colors` | Viewport shading for the capture (restored afterwards). `class_colors` renders solid with one flat color per IFC class and returns a legend. |
| `show_overlays` | boolean, default false | Overlays (grid, axes, gizmos) are hidden by default; they are noise for image analysis. Set true to keep them. |
| `include_objects` | boolean, default false | Adds screen-space 2D bounding boxes and view depth keyed by GlobalId to the text output. |
| `max_objects` | 1-200, default 50 | Cap for `include_objects`. Selection is stratified across IFC classes (a few walls, doors, windows, ...) so ground slabs cannot crowd out everything else; truncation is flagged. |

Returns, in order:

1. An MCP **image content block** (`image/jpeg` or `image/png`). The image
   comes first because some MCP clients mishandle mixed content.
2. A **text block** with the image dimensions and attached base64 length
   (so a client-side image drop is diagnosable from text), any
   auto-downgrade note, the `class_colors` legend when requested, and
   structured **viewport state**: rotation quaternion, perspective mode
   (`PERSP`/`ORTHO`/`CAMERA`), `is_orthographic_side_view`, view distance,
   pivot location, and (when used) the applied `azimuth`/`elevation` and
   `storey`.

With `include_objects=true`, the viewport state also lists in-frame objects:

```json
{
  "objects_in_view": [
    {
      "name": "IfcWall/MyWall",
      "ifc_class": "IfcWall",
      "global_id": "2O2Fr$t4X7Zf8NOew3FK6X",
      "box": [0.329, 0.55, 0.712, 0.563],
      "depth": 24.6
    }
  ],
  "objects_in_view_total": 1250,
  "objects_truncated": true
}
```

`box` is `[x_min, y_min, x_max, y_max]`, normalized 0-1 with the origin at
the image's top-left (smaller `y` is higher on screen). `depth` is the
view-space distance to the object's bounding-box centre in model units, so
near/far ordering is available from text alone. Boxes are approximate
(full object extent, ignoring occlusion) but preserve relative spatial
layout, which lets a text-only agent reason about containment,
left/right/above/below relations, relative sizes, and depth ordering even
when its client does not deliver tool-result images.

If Blender is running in `--background` mode or has no open 3D viewport,
the tool returns a clear error (a visible `VIEW_3D` area is required).
When several 3D viewports are open, the largest one is used.

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
| `save_and_load_ifc(path=None)` | Legacy helper (save then full reload); prefer the refresh tools |

**Viewport sync:** edits made here change the in-memory IFC model but do
*not* appear in the Blender viewport until refreshed. Pick the cheapest
tier for what the edit touched: [`refresh_view`](#refresh_view-edit) after
data-only edits (names, psets, classifications),
[`refresh_geometry`](#refresh_geometry-edit) after moving elements or
changing representations, [`reload_project`](#reload_project-edit) after
creating or deleting elements. Do not save just to make edits visible;
saving is a separate, durability-only step.

MCP edits are outside Blender's undo stack: Ctrl+Z will not revert them.

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

## `refresh_view` (EDIT)

Syncs the Blender scene after **data-only** IFC edits: names, descriptions,
psets, quantities, classifications. Sub-millisecond per element even on
million-entity models. Does not write to disk. Does not rebuild geometry.

Inputs:

```json
{ "global_ids": ["2O2Fr$t4X7Zf8NOew3FK6X"] }
```

Returns per-element results (`refreshed` counts successes; elements that
were deleted or have no Blender object yet get an `error` entry pointing at
`reload_project`):

```json
{
  "refreshed": 1,
  "results": [
    { "global_id": "2O2Fr$t4X7Zf8NOew3FK6X", "object": "IfcWall/NewName", "renamed": true }
  ],
  "note": "Data-only sync (names). Nothing was written to disk; ..."
}
```

## `refresh_geometry` (EDIT)

Rebuilds Blender geometry and placement for **specific** elements after
geometric IFC edits: moved elements (changed `ObjectPlacement`) or changed
representations. Fast and targeted; does not write to disk; the rest of
the scene (selection, visibility, camera) is untouched.

Not covered: newly created and deleted elements; use
[`reload_project`](#reload_project-edit) for those.

Inputs:

```json
{ "global_ids": ["2O2Fr$t4X7Zf8NOew3FK6X"] }
```

Returns per-element results with `placement_synced` flags and a
`representations_reloaded` summary flag.

## `reload_project` (EDIT)

Full scene rebuild from the **in-memory** IFC model. The model is written
to a temporary file and reloaded from it; the project file on disk is not
modified, and the project path is restored afterwards so saving (Ctrl+S or
`save_ifc_file`) still targets the user's file.

**Slow**: seconds to minutes on large models, and it resets selection,
visibility, and camera. Use only when targeted refresh is insufficient:
after creating or deleting elements, or when the scene has genuinely
diverged.

Takes no inputs. Returns:

```json
{ "reloaded": true, "project_path": "C:/models/house.ifc", "path_restored": true }
```

## `save_ifc_file` (EDIT)

Writes the IFC model to disk. **Durability only**: call it when the user
asks to save. It does not refresh the viewport; use the refresh tools for
visibility. The user pressing Ctrl+S in Blender is equivalent to the
in-place mode.

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

`reload=true` is a legacy flag that reloads the project from the saved file
afterwards; prefer `reload_project`, which does not require saving first.

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

## Resources and prompts

Beyond tools, the server exposes read-only state as MCP **resources**
(clients can pin them into context without a tool round trip):

| URI | Content |
| --- | --- |
| `bonsai://project` | The `get_ifc_project_info` payload. |
| `bonsai://scene` | The scene summary (no object query). |
| `bonsai://element/{global_id}/psets` | Resource template: psets/qtos for one element. |

All resources return `application/json`.

Two MCP **prompts** encode the workflows the server instructions describe
in prose:

| Prompt | Arguments | What it does |
| --- | --- | --- |
| `model-audit` | `focus` (optional) | Walk the model with the query tools (project info, spatial tree, quantities, pset spot-checks, screenshots) and produce a quality report. |
| `visual-verify` | `what_changed` (optional) | Reload if needed, take overview and detail screenshots, compare against the intended edit. |

Long operations also emit coarse MCP progress notifications when the
client supplies a `progressToken`.
