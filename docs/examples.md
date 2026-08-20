# Example prompts

Real prompts to try once your client is connected and an IFC project is
open in Bonsai. Each example notes which tools the assistant typically
uses, so you can follow along in the Blender panel's activity log.

## Explore a model

> What is in this building, storey by storey?

Uses `get_spatial_structure`. Returns the site, building, storey, and
space tree with element counts per storey; no code execution involved.

> Give me an overview picture of the model.

Uses `get_viewport_screenshot` with `view='iso'`, `fit='all'`.

> Show me a floor plan of the second storey, colored by element type.

Uses `get_viewport_screenshot` with `storey`, `view='top'`, `fit='all'`,
`shading='class_colors'`. The response includes a color legend.

## Query BIM data

> How much wall area and slab volume is in the model, per storey?

Uses `get_quantities` with `by_storey=true`. Sums IFC base quantities and
reports the project units.

> List every wall with its fire rating.

Uses `list_elements` (class filter), then `get_psets` for the wall
property sets.

> Which walls have fire rating F30?

Uses `list_elements` with
`selector='IfcWall, Pset_WallCommon.FireRating=F30'`; property filtering
without any code.

> What material are the columns made of?

Uses `list_elements` plus `get_psets`, or a short `execute_ifc_code`
snippet via `element_util`.

## Audit

> Audit this model and report quality issues.

Use the built-in `model-audit` MCP prompt if your client exposes prompts;
it walks project info, the spatial tree, quantities, pset spot-checks,
and screenshots, then reports findings by severity.

## Edit (requires "Allow edits" in the Blender panel)

> Set the FireRating of all doors on the ground floor to F30 and show me
> proof.

Uses `execute_ifc_code` (ifcopenshell.api pset edits), then `refresh_view`
with the door GlobalIds (milliseconds, no disk write), then `get_psets` to
verify. Nothing is saved until you ask (or press Ctrl+S in Blender).

> Add a Pset_WallCommon with IsExternal=true to every wall touching the
> facade, then show me a plan view.

Uses `execute_ifc_code`, `refresh_view`, and a `get_viewport_screenshot`
floor plan.

After IFC edits the assistant refreshes only what changed: `refresh_view`
for data edits, `refresh_geometry` for moved elements, and the slow
`reload_project` only after creating or deleting elements. Saving is a
separate step that only happens when you ask.

## Tips

- Read-only by choice: untick "Allow edits" in the Blender sidebar and
  the assistant can still answer every query above, but cannot change
  anything.
- Large models: results are paged; the assistant follows `total` and
  `truncated` flags automatically.
- If a screenshot does not reach your client, the response text says so
  and the assistant can fall back to `include_objects=true` boxes.
