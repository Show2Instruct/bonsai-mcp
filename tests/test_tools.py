"""Tool-layer tests. Verify each MCP tool wires through to the right bridge command."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bonsai_mcp.blender_client import BlenderBridgeError
from bonsai_mcp.tools import (
    ALL_TOOL_NAMES,
    EDIT_TOOL_NAMES,
    QUERY_TOOL_NAMES,
    ToolError,
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
    tool_save_ifc_file,
)
from tests.conftest import FakeBlenderBridgeClient


def test_all_tool_names_unique():
    assert len(set(ALL_TOOL_NAMES)) == len(ALL_TOOL_NAMES)


def test_query_and_edit_partitions_cover_all_tools():
    assert set(QUERY_TOOL_NAMES) | set(EDIT_TOOL_NAMES) == set(ALL_TOOL_NAMES)
    assert set(QUERY_TOOL_NAMES).isdisjoint(EDIT_TOOL_NAMES)


class TestSceneInfo:
    def test_no_query_sends_empty_params(self):
        client = FakeBlenderBridgeClient(
            responses={"get_scene_info": {"scene_name": "Scene"}}
        )
        out = tool_get_scene_info(client)
        assert out == {"scene_name": "Scene"}
        assert client.calls == [("get_scene_info", {})]

    def test_simple_query_passes_through_with_paging(self):
        client = FakeBlenderBridgeClient(
            responses={
                "get_scene_info": {"scene_name": "S", "objects": [], "objects_total": 0}
            }
        )
        tool_get_scene_info(client, {"query": "walls"})
        cmd, params = client.calls[-1]
        assert cmd == "get_scene_info"
        assert params == {"query": "walls", "limit": 200, "offset": 0}

    def test_custom_paging_passes_through(self):
        client = FakeBlenderBridgeClient(
            responses={"get_scene_info": {"objects": [], "objects_total": 0}}
        )
        tool_get_scene_info(client, {"query": "all", "limit": 10, "offset": 30})
        params = client.calls[-1][1]
        assert params["limit"] == 10
        assert params["offset"] == 30

    def test_by_class_includes_ifc_class(self):
        client = FakeBlenderBridgeClient(
            responses={"get_scene_info": {"objects": [], "objects_total": 0}}
        )
        tool_get_scene_info(client, {"query": "by_class", "ifc_class": "IfcCovering"})
        params = client.calls[-1][1]
        assert params["query"] == "by_class"
        assert params["ifc_class"] == "IfcCovering"

    @pytest.mark.parametrize("keyword", ["roofs", "stairs"])
    def test_extended_keywords_pass_through(self, keyword):
        client = FakeBlenderBridgeClient(responses={"get_scene_info": {}})
        tool_get_scene_info(client, {"query": keyword})
        params = client.calls[-1][1]
        assert params["query"] == keyword

    def test_old_addon_objects_are_paged_server_side(self):
        # an add-on without bridge-side paging returns the full list; the
        # adapter must apply the page and report total/truncated itself
        objects = [{"name": f"O{i}"} for i in range(10)]
        client = FakeBlenderBridgeClient(
            responses={"get_scene_info": {"scene_name": "S", "objects": list(objects)}}
        )
        out = tool_get_scene_info(client, {"query": "all", "limit": 3, "offset": 4})
        assert [o["name"] for o in out["objects"]] == ["O4", "O5", "O6"]
        assert out["objects_total"] == 10
        assert out["objects_truncated"] is True

    def test_bridge_unreachable_raises_tool_error(self):
        client = FakeBlenderBridgeClient(
            responses={"get_scene_info": BlenderBridgeError("nope")}
        )
        with pytest.raises(ToolError, match="Blender bridge error: nope"):
            tool_get_scene_info(client)


class TestSelectedObjects:
    def test_happy_path_new_addon_shape(self):
        client = FakeBlenderBridgeClient(
            responses={
                "get_selected_objects": {
                    "objects": [{"name": "Cube", "ifc_class": None, "global_id": None}],
                    "total": 1,
                    "truncated": False,
                }
            }
        )
        out = tool_get_selected_objects(client)
        assert out["objects"][0]["name"] == "Cube"
        assert client.calls[-1] == ("get_selected_objects", {"limit": 200})

    def test_old_addon_bare_list_is_wrapped_and_capped(self):
        client = FakeBlenderBridgeClient(
            responses={"get_selected_objects": [{"name": f"O{i}"} for i in range(5)]}
        )
        out = tool_get_selected_objects(client, {"limit": 2})
        assert [o["name"] for o in out["objects"]] == ["O0", "O1"]
        assert out["total"] == 5
        assert out["truncated"] is True


class TestListElements:
    def test_filters_passed_through(self):
        client = FakeBlenderBridgeClient(
            responses={
                "list_elements": {"elements": [], "total": 0, "truncated": False}
            }
        )
        tool_list_elements(
            client,
            {"ifc_class": "IfcWall", "name_contains": "w1", "storey": "Level 1"},
        )
        cmd, params = client.calls[-1]
        assert cmd == "list_elements"
        assert params == {
            "ifc_class": "IfcWall",
            "name_contains": "w1",
            "storey": "Level 1",
            "limit": 200,
            "offset": 0,
        }

    def test_defaults(self):
        client = FakeBlenderBridgeClient(
            responses={"list_elements": {"elements": [], "total": 0, "truncated": False}}
        )
        out = tool_list_elements(client)
        assert out["total"] == 0
        params = client.calls[-1][1]
        assert params["ifc_class"] is None
        assert params["limit"] == 200

    def test_unexpected_payload_raises(self):
        client = FakeBlenderBridgeClient(responses={"list_elements": "weird"})
        with pytest.raises(ToolError, match="unexpected list_elements payload"):
            tool_list_elements(client)


class TestSpatialStructure:
    def test_happy_path(self):
        tree = {"ifc_class": "IfcProject", "name": "P", "children": []}
        client = FakeBlenderBridgeClient(
            responses={"get_spatial_structure": {"schema": "IFC4", "tree": tree}}
        )
        out = tool_get_spatial_structure(client)
        assert out["tree"]["ifc_class"] == "IfcProject"
        assert client.calls[-1] == (
            "get_spatial_structure",
            {"include_element_counts": True},
        )

    def test_counts_can_be_disabled(self):
        client = FakeBlenderBridgeClient(
            responses={"get_spatial_structure": {"schema": "IFC4", "tree": {}}}
        )
        tool_get_spatial_structure(client, {"include_element_counts": False})
        assert client.calls[-1][1] == {"include_element_counts": False}


class TestQuantities:
    def test_defaults(self):
        client = FakeBlenderBridgeClient(
            responses={"get_quantities": {"classes": {}, "units": {}}}
        )
        out = tool_get_quantities(client)
        assert out == {"classes": {}, "units": {}}
        assert client.calls[-1] == (
            "get_quantities",
            {"ifc_classes": None, "by_storey": False},
        )

    def test_classes_and_by_storey_pass_through(self):
        client = FakeBlenderBridgeClient(
            responses={"get_quantities": {"classes": {}, "units": {}, "by_storey": {}}}
        )
        tool_get_quantities(client, {"ifc_classes": ["IfcWall"], "by_storey": True})
        assert client.calls[-1][1] == {"ifc_classes": ["IfcWall"], "by_storey": True}


class TestExecuteCode:
    def test_passes_code_through(self):
        captured: dict = {}

        def on_send(cmd, params):
            captured["cmd"] = cmd
            captured["params"] = params
            return {"success": True, "stdout": "ok\n", "stderr": ""}

        client = FakeBlenderBridgeClient(on_send=on_send)
        out = tool_execute_blender_code(client, {"code": "print('ok')"})
        assert captured == {"cmd": "execute_code", "params": {"code": "print('ok')"}}
        assert out["stdout"] == "ok\n"

    def test_missing_code_fails_validation(self):
        client = FakeBlenderBridgeClient()
        with pytest.raises(ValidationError):
            tool_execute_blender_code(client, {})


class TestExecuteIfcCode:
    def test_passes_code_through(self):
        captured: dict = {}

        def on_send(cmd, params):
            captured["cmd"] = cmd
            captured["params"] = params
            return {
                "success": True,
                "stdout": "ok\n",
                "stderr": "",
                "ifc_available": True,
                "namespace_keys": ["ifc", "ifcopenshell"],
            }

        client = FakeBlenderBridgeClient(on_send=on_send)
        out = tool_execute_ifc_code(client, {"code": "print(ifc)"})
        assert captured == {
            "cmd": "execute_ifc_code",
            "params": {"code": "print(ifc)"},
        }
        assert out["stdout"] == "ok\n"

    def test_missing_code_fails_validation(self):
        client = FakeBlenderBridgeClient()
        with pytest.raises(ValidationError):
            tool_execute_ifc_code(client, {})

    def test_bridge_error_raises_tool_error(self):
        client = FakeBlenderBridgeClient(
            responses={"execute_ifc_code": BlenderBridgeError("IfcOpenShell not available")}
        )
        with pytest.raises(ToolError, match="IfcOpenShell not available"):
            tool_execute_ifc_code(client, {"code": "print(ifc)"})


class TestViewportScreenshot:
    def test_returns_payload(self):
        client = FakeBlenderBridgeClient(
            responses={
                "get_viewport_screenshot": {
                    "path": "/tmp/x.png",
                    "image_base64": "AAAA",
                    "format": "png",
                }
            }
        )
        out = tool_get_viewport_screenshot(client)
        assert out["image_base64"] == "AAAA"
        assert out["path"].endswith("x.png")

    def test_error_path_raises_tool_error(self):
        client = FakeBlenderBridgeClient(
            responses={"get_viewport_screenshot": BlenderBridgeError("no viewport")}
        )
        with pytest.raises(ToolError, match="no viewport"):
            tool_get_viewport_screenshot(client)

    def test_default_params_sent_to_bridge(self):
        client = FakeBlenderBridgeClient(
            responses={"get_viewport_screenshot": {"image_base64": "AAAA", "format": "jpeg"}}
        )
        tool_get_viewport_screenshot(client)
        params = client.calls[-1][1]
        assert params == {
            "max_size": 800,
            "format": "jpeg",
            "quality": 85,
            "view": None,
            "fit": None,
            "azimuth": None,
            "elevation": None,
            "storey": None,
            "shading": None,
            "show_overlays": False,
            "include_objects": False,
            "max_objects": 50,
        }

    def test_custom_params_sent_to_bridge(self):
        client = FakeBlenderBridgeClient(
            responses={"get_viewport_screenshot": {"image_base64": "AAAA", "format": "png"}}
        )
        tool_get_viewport_screenshot(client, {"max_size": 1200, "format": "png"})
        params = client.calls[-1][1]
        assert params["max_size"] == 1200
        assert params["format"] == "png"

    def test_view_and_fit_sent_to_bridge(self):
        client = FakeBlenderBridgeClient(
            responses={"get_viewport_screenshot": {"image_base64": "AAAA", "format": "jpeg"}}
        )
        tool_get_viewport_screenshot(client, {"view": "iso", "fit": "all"})
        params = client.calls[-1][1]
        assert params["view"] == "iso"
        assert params["fit"] == "all"

    def test_azimuth_elevation_sent_to_bridge(self):
        client = FakeBlenderBridgeClient(
            responses={"get_viewport_screenshot": {"image_base64": "AAAA", "format": "jpeg"}}
        )
        tool_get_viewport_screenshot(client, {"azimuth": 120.0, "elevation": 15.0})
        params = client.calls[-1][1]
        assert params["azimuth"] == pytest.approx(120.0)
        assert params["elevation"] == pytest.approx(15.0)

    def test_view_and_azimuth_are_mutually_exclusive(self):
        client = FakeBlenderBridgeClient(responses={})
        with pytest.raises(ValidationError):
            tool_get_viewport_screenshot(client, {"view": "iso", "azimuth": 45.0})
        assert client.calls == []

    def test_storey_shading_overlays_sent_to_bridge(self):
        client = FakeBlenderBridgeClient(
            responses={"get_viewport_screenshot": {"image_base64": "AAAA", "format": "jpeg"}}
        )
        tool_get_viewport_screenshot(
            client,
            {"storey": "Level 1", "shading": "class_colors", "show_overlays": True},
        )
        params = client.calls[-1][1]
        assert params["storey"] == "Level 1"
        assert params["shading"] == "class_colors"
        assert params["show_overlays"] is True

    def test_invalid_shading_rejected(self):
        client = FakeBlenderBridgeClient(responses={})
        with pytest.raises(ValidationError):
            tool_get_viewport_screenshot(client, {"shading": "cartoon"})
        assert client.calls == []

    def test_invalid_view_rejected(self):
        client = FakeBlenderBridgeClient(responses={})
        with pytest.raises(ValidationError):
            tool_get_viewport_screenshot(client, {"view": "sideways"})
        assert client.calls == []

    def test_include_objects_sent_to_bridge(self):
        client = FakeBlenderBridgeClient(
            responses={"get_viewport_screenshot": {"image_base64": "AAAA", "format": "jpeg"}}
        )
        tool_get_viewport_screenshot(client, {"include_objects": True, "max_objects": 10})
        params = client.calls[-1][1]
        assert params["include_objects"] is True
        assert params["max_objects"] == 10

    def test_max_objects_bounds_rejected(self):
        client = FakeBlenderBridgeClient(responses={})
        with pytest.raises(ValidationError):
            tool_get_viewport_screenshot(client, {"max_objects": 0})
        assert client.calls == []

    def test_invalid_params_rejected(self):
        client = FakeBlenderBridgeClient(responses={})
        with pytest.raises(ValidationError):
            tool_get_viewport_screenshot(client, {"max_size": 5})
        assert client.calls == []


class TestIfcProjectInfo:
    def test_happy_path(self):
        client = FakeBlenderBridgeClient(
            responses={"get_ifc_project_info": {"schema": "IFC4", "entity_counts": {}}}
        )
        out = tool_get_ifc_project_info(client)
        assert out["schema"] == "IFC4"

    def test_no_ifc_loaded_raises_tool_error(self):
        client = FakeBlenderBridgeClient(
            responses={"get_ifc_project_info": BlenderBridgeError("No IFC project")}
        )
        with pytest.raises(ToolError, match="No IFC project"):
            tool_get_ifc_project_info(client)

    def test_materials_and_classifications_passthrough(self):
        payload = {
            "schema": "IFC4",
            "entity_counts": {"IfcWall": 3},
            "materials": {
                "count": 2,
                "names": ["Concrete", "Steel"],
                "truncated": False,
            },
            "classifications": {
                "count": 1,
                "systems": [
                    {"name": "Uniclass 2015", "source": "NBS", "edition": "v1.20"}
                ],
                "truncated": False,
            },
        }
        client = FakeBlenderBridgeClient(responses={"get_ifc_project_info": payload})
        decoded = tool_get_ifc_project_info(client)
        assert decoded["materials"]["names"] == ["Concrete", "Steel"]
        assert decoded["classifications"]["systems"][0]["name"] == "Uniclass 2015"


class TestGetPsets:
    def test_global_ids_only(self):
        client = FakeBlenderBridgeClient(
            responses={
                "get_psets": {
                    "results": [
                        {
                            "request": {"global_id": "AAAA"},
                            "object": {
                                "name": "IfcWall/W1",
                                "global_id": "AAAA",
                                "ifc_class": "IfcWall",
                            },
                            "property_sets": {"Pset_WallCommon": {"IsExternal": True}},
                            "quantity_sets": {},
                        },
                        {"request": {"global_id": "BBBB"}, "error": "not found"},
                    ]
                }
            }
        )
        out = tool_get_psets(client, {"global_ids": ["AAAA", "BBBB"]})
        assert out["results"][0]["object"]["ifc_class"] == "IfcWall"
        assert out["targets_total"] == 2
        assert out["truncated"] is False
        cmd, params = client.calls[-1]
        assert cmd == "get_psets"
        assert params == {"global_ids": ["AAAA", "BBBB"], "names": []}

    def test_names_only(self):
        client = FakeBlenderBridgeClient(responses={"get_psets": {"results": []}})
        tool_get_psets(client, {"names": ["IfcWall/MyWall"]})
        params = client.calls[-1][1]
        assert params == {"global_ids": [], "names": ["IfcWall/MyWall"]}

    def test_mixed_inputs(self):
        client = FakeBlenderBridgeClient(responses={"get_psets": {"results": []}})
        tool_get_psets(client, {"global_ids": ["AAAA"], "names": ["IfcDoor/D1"]})
        params = client.calls[-1][1]
        assert params == {"global_ids": ["AAAA"], "names": ["IfcDoor/D1"]}

    def test_requires_at_least_one_target(self):
        client = FakeBlenderBridgeClient()
        with pytest.raises(ValidationError):
            tool_get_psets(client, {})

    def test_rejects_blank_entries(self):
        client = FakeBlenderBridgeClient()
        with pytest.raises(ValidationError):
            tool_get_psets(client, {"global_ids": ["AAAA", "   "]})

    def test_large_batches_are_paged_not_rejected(self):
        # 120 targets used to be a hard error; now the first page is sent
        # and the response reports how to continue
        client = FakeBlenderBridgeClient(responses={"get_psets": {"results": []}})
        out = tool_get_psets(
            client,
            {
                "global_ids": [f"G{i}" for i in range(60)],
                "names": [f"N{i}" for i in range(60)],
            },
        )
        params = client.calls[-1][1]
        assert len(params["global_ids"]) + len(params["names"]) == 100
        assert params["global_ids"] == [f"G{i}" for i in range(60)]
        assert params["names"] == [f"N{i}" for i in range(40)]
        assert out["targets_total"] == 120
        assert out["truncated"] is True
        assert out["next_offset"] == 100

    def test_offset_pages_across_the_target_list(self):
        client = FakeBlenderBridgeClient(responses={"get_psets": {"results": []}})
        out = tool_get_psets(
            client,
            {
                "global_ids": [f"G{i}" for i in range(4)],
                "names": [f"N{i}" for i in range(4)],
                "limit": 3,
                "offset": 3,
            },
        )
        params = client.calls[-1][1]
        assert params["global_ids"] == ["G3"]
        assert params["names"] == ["N0", "N1"]
        assert out["truncated"] is True
        assert out["next_offset"] == 6

    def test_bridge_error_raises_tool_error(self):
        client = FakeBlenderBridgeClient(
            responses={"get_psets": BlenderBridgeError("no IFC loaded")}
        )
        with pytest.raises(ToolError, match="no IFC loaded"):
            tool_get_psets(client, {"global_ids": ["AAAA"]})


class TestSaveIfcFile:
    def test_passes_overwrite_flag(self):
        client = FakeBlenderBridgeClient(responses={"save_ifc_file": {"saved": True}})
        tool_save_ifc_file(client, {"output_path": "/tmp/out.ifc", "overwrite": True})
        params = client.calls[-1][1]
        assert params == {"output_path": "/tmp/out.ifc", "overwrite": True, "reload": False}

    def test_default_overwrite_false(self):
        client = FakeBlenderBridgeClient(responses={"save_ifc_file": {"saved": True}})
        tool_save_ifc_file(client, {"output_path": "/tmp/out.ifc"})
        params = client.calls[-1][1]
        assert params["overwrite"] is False

    def test_in_place_save_without_path(self):
        client = FakeBlenderBridgeClient(responses={"save_ifc_file": {"saved": True}})
        tool_save_ifc_file(client, {})
        params = client.calls[-1][1]
        assert params == {"output_path": None, "overwrite": False, "reload": False}

    def test_passes_reload_flag(self):
        client = FakeBlenderBridgeClient(responses={"save_ifc_file": {"saved": True}})
        tool_save_ifc_file(client, {"reload": True})
        params = client.calls[-1][1]
        assert params["reload"] is True
