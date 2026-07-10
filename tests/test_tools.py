"""Tool-layer tests. Verify each MCP tool wires through to the right bridge command."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from bonsai_mcp.blender_client import BlenderBridgeError
from bonsai_mcp.tools import (
    ALL_TOOL_NAMES,
    EDIT_TOOL_NAMES,
    QUERY_TOOL_NAMES,
    tool_execute_blender_code,
    tool_execute_ifc_code,
    tool_get_ifc_project_info,
    tool_get_psets,
    tool_get_scene_info,
    tool_get_selected_objects,
    tool_get_viewport_screenshot,
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
        assert json.loads(out) == {"scene_name": "Scene"}
        assert client.calls == [("get_scene_info", {})]

    def test_simple_query_passes_through(self):
        client = FakeBlenderBridgeClient(
            responses={"get_scene_info": {"scene_name": "S", "objects": []}}
        )
        tool_get_scene_info(client, {"query": "walls"})
        cmd, params = client.calls[-1]
        assert cmd == "get_scene_info"
        assert params == {"query": "walls"}

    def test_by_class_includes_ifc_class(self):
        client = FakeBlenderBridgeClient(responses={"get_scene_info": {"objects": []}})
        tool_get_scene_info(
            client, {"query": "by_class", "ifc_class": "IfcCovering"}
        )
        params = client.calls[-1][1]
        assert params == {"query": "by_class", "ifc_class": "IfcCovering"}

    @pytest.mark.parametrize("keyword", ["roofs", "stairs"])
    def test_extended_keywords_pass_through(self, keyword):
        client = FakeBlenderBridgeClient(responses={"get_scene_info": {}})
        tool_get_scene_info(client, {"query": keyword})
        params = client.calls[-1][1]
        assert params["query"] == keyword

    def test_bridge_unreachable_returns_error_string(self):
        client = FakeBlenderBridgeClient(
            responses={"get_scene_info": BlenderBridgeError("nope")}
        )
        out = tool_get_scene_info(client)
        assert "Blender bridge error: nope" in out


class TestSelectedObjects:
    def test_happy_path(self):
        client = FakeBlenderBridgeClient(
            responses={
                "get_selected_objects": [
                    {"name": "Cube", "ifc_class": None, "global_id": None}
                ]
            }
        )
        out = tool_get_selected_objects(client)
        assert json.loads(out)[0]["name"] == "Cube"


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
        assert "ok" in out

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
        assert "ok" in out

    def test_missing_code_fails_validation(self):
        client = FakeBlenderBridgeClient()
        with pytest.raises(ValidationError):
            tool_execute_ifc_code(client, {})

    def test_bridge_error_returned_as_text(self):
        client = FakeBlenderBridgeClient(
            responses={"execute_ifc_code": BlenderBridgeError("IfcOpenShell not available")}
        )
        out = tool_execute_ifc_code(client, {"code": "print(ifc)"})
        assert "IfcOpenShell not available" in out


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

    def test_error_path(self):
        client = FakeBlenderBridgeClient(
            responses={"get_viewport_screenshot": BlenderBridgeError("no viewport")}
        )
        out = tool_get_viewport_screenshot(client)
        assert "no viewport" in out["error"]

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

    def test_invalid_view_rejected(self):
        client = FakeBlenderBridgeClient(responses={})
        out = tool_get_viewport_screenshot(client, {"view": "sideways"})
        assert "error" in out
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
        out = tool_get_viewport_screenshot(client, {"max_objects": 0})
        assert "error" in out
        assert client.calls == []

    def test_invalid_params_reported_as_error(self):
        client = FakeBlenderBridgeClient(responses={})
        out = tool_get_viewport_screenshot(client, {"max_size": 5})
        assert "error" in out
        assert client.calls == []


class TestIfcProjectInfo:
    def test_happy_path(self):
        client = FakeBlenderBridgeClient(
            responses={"get_ifc_project_info": {"schema": "IFC4", "entity_counts": {}}}
        )
        out = tool_get_ifc_project_info(client)
        assert json.loads(out)["schema"] == "IFC4"

    def test_no_ifc_loaded(self):
        client = FakeBlenderBridgeClient(
            responses={"get_ifc_project_info": BlenderBridgeError("No IFC project")}
        )
        out = tool_get_ifc_project_info(client)
        assert "No IFC project" in out

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
        decoded = json.loads(tool_get_ifc_project_info(client))
        assert decoded["materials"]["names"] == ["Concrete", "Steel"]
        assert decoded["classifications"]["systems"][0]["name"] == "Uniclass 2015"
        assert decoded["classifications"]["count"] == 1
        assert decoded["materials"]["truncated"] is False


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
        payload = json.loads(out)
        assert payload["results"][0]["object"]["ifc_class"] == "IfcWall"
        assert payload["results"][1] == {
            "request": {"global_id": "BBBB"},
            "error": "not found",
        }
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
        tool_get_psets(
            client, {"global_ids": ["AAAA"], "names": ["IfcDoor/D1"]}
        )
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

    def test_over_combined_limit_rejected(self):
        client = FakeBlenderBridgeClient()
        with pytest.raises(ValidationError):
            tool_get_psets(
                client,
                {
                    "global_ids": [f"G{i}" for i in range(60)],
                    "names": [f"N{i}" for i in range(60)],
                },
            )

    def test_bridge_error_returned_as_text(self):
        client = FakeBlenderBridgeClient(
            responses={"get_psets": BlenderBridgeError("no IFC loaded")}
        )
        out = tool_get_psets(client, {"global_ids": ["AAAA"]})
        assert "no IFC loaded" in out


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
