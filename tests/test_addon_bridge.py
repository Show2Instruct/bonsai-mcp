"""Add-on tests: import blender_addon/bonsai_bridge.py with a faked bpy.

The add-on is a standalone file running inside Blender's Python, so the
framing helpers are deliberately duplicated between it and
src/bonsai_mcp/blender_client.py. These tests enforce wire-format parity
between the two implementations and exercise the add-on's request lifecycle
(cancellation, read-only mode, flush-on-stop) without Blender.
"""

from __future__ import annotations

import importlib.util
import socket
import struct
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from bonsai_mcp import blender_client as bc
from bonsai_mcp.blender_client import BlenderBridgeClient, BlenderBridgeError

_ADDON_PATH = Path(__file__).resolve().parents[1] / "blender_addon" / "bonsai_bridge.py"


class _FakeTimers:
    def __init__(self) -> None:
        self.registered: list = []

    def is_registered(self, fn) -> bool:
        return fn in self.registered

    def register(self, fn, persistent=False) -> None:
        self.registered.append(fn)

    def unregister(self, fn) -> None:
        self.registered.remove(fn)


def _install_fake_bpy(monkeypatch) -> types.ModuleType:
    bpy_mod = types.ModuleType("bpy")
    props_mod = types.ModuleType("bpy.props")
    types_mod = types.ModuleType("bpy.types")

    def _prop_stub(**kwargs):
        return ("prop", kwargs)

    props_mod.BoolProperty = _prop_stub
    props_mod.IntProperty = _prop_stub
    props_mod.StringProperty = _prop_stub

    types_mod.AddonPreferences = type("AddonPreferences", (), {})
    types_mod.Operator = type("Operator", (), {})
    types_mod.Panel = type("Panel", (), {})

    bpy_mod.props = props_mod
    bpy_mod.types = types_mod
    bpy_mod.app = SimpleNamespace(timers=_FakeTimers(), version_string="4.5.0")
    bpy_mod.context = SimpleNamespace(
        scene=SimpleNamespace(objects=[], BIMProperties=SimpleNamespace(ifc_file="")),
        selected_objects=[],
        preferences=SimpleNamespace(addons={}),
    )
    bpy_mod.utils = SimpleNamespace(
        register_class=lambda cls: None, unregister_class=lambda cls: None
    )

    monkeypatch.setitem(sys.modules, "bpy", bpy_mod)
    monkeypatch.setitem(sys.modules, "bpy.props", props_mod)
    monkeypatch.setitem(sys.modules, "bpy.types", types_mod)
    return bpy_mod


@pytest.fixture
def bridge(monkeypatch):
    """Fresh import of the add-on module against a fake bpy."""
    fake_bpy = _install_fake_bpy(monkeypatch)
    spec = importlib.util.spec_from_file_location("bonsai_bridge_under_test", _ADDON_PATH)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "bonsai_bridge_under_test", mod)
    spec.loader.exec_module(mod)
    mod._fake_bpy = fake_bpy
    return mod


class FakeElement:
    """Stand-in for an ifcopenshell entity_instance."""

    def __init__(
        self, ifc_class: str, ancestors: set[str] | frozenset = frozenset(), step_id: int = 0, **attrs
    ) -> None:
        self._class = ifc_class
        self._ancestors = set(ancestors) | {ifc_class}
        self._step_id = step_id
        for key, value in attrs.items():
            setattr(self, key, value)

    def is_a(self, target: str | None = None):
        if target is None:
            return self._class
        return target in self._ancestors

    def id(self) -> int:
        return self._step_id


def _mesh_obj(name: str, def_id: int):
    return SimpleNamespace(
        name=name,
        type="MESH",
        BIMObjectProperties=SimpleNamespace(ifc_definition_id=def_id),
    )


# ---------------------------------------------------------------------------
# Framing parity between the client and the add-on (both directions)
# ---------------------------------------------------------------------------


def test_framing_client_writer_addon_reader(bridge):
    a, b = socket.socketpair()
    try:
        payload = {"command": "ping", "params": {"note": "über", "n": [1, 2, 3]}}
        bc._write_message(a, payload)
        assert bridge._read_message(b) == payload
    finally:
        a.close()
        b.close()


def test_framing_addon_writer_client_reader(bridge):
    a, b = socket.socketpair()
    try:
        payload = {"success": True, "result": {"names": ["Wand/Tür", None, 1.5]}}
        bridge._send_message(a, payload)
        assert bc._read_message(b) == payload
    finally:
        a.close()
        b.close()


def test_addon_oversized_frame_raises_protocol_error(bridge):
    a, b = socket.socketpair()
    try:
        a.sendall(struct.pack(">I", bridge.MAX_MESSAGE_BYTES + 1))
        with pytest.raises(bridge._ProtocolError, match="exceeds"):
            bridge._read_message(b)
    finally:
        a.close()
        b.close()


def test_client_oversized_frame_raises_bridge_error(bridge):
    a, b = socket.socketpair()
    try:
        a.sendall(struct.pack(">I", bc.MAX_MESSAGE_BYTES + 1))
        with pytest.raises(BlenderBridgeError, match="too large"):
            bc._read_message(b)
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize(
    "body", [b"not json at all", b"[1, 2, 3]", b'"just a string"', b"\xff\xfe\x00"]
)
def test_addon_bad_body_raises_protocol_error(bridge, body):
    a, b = socket.socketpair()
    try:
        a.sendall(struct.pack(">I", len(body)) + body)
        with pytest.raises(bridge._ProtocolError):
            bridge._read_message(b)
    finally:
        a.close()
        b.close()


def test_addon_clean_eof_returns_none(bridge):
    a, b = socket.socketpair()
    a.close()
    try:
        assert bridge._read_message(b) is None
    finally:
        b.close()


def test_trim_output_cap(bridge):
    cap = bridge._EXEC_OUTPUT_CAP_BYTES
    text, truncated, total = bridge._trim_output("a" * cap)
    assert not truncated and total == cap
    text, truncated, total = bridge._trim_output("a" * (cap + 1))
    assert truncated and total == cap + 1 and len(text.encode()) == cap


# ---------------------------------------------------------------------------
# IFC class matching (inheritance-aware) and lookup caching
# ---------------------------------------------------------------------------


def test_walls_query_matches_ifc2x3_standard_case(bridge, monkeypatch):
    wall = _mesh_obj("IfcWallStandardCase/W1", 1)
    door = _mesh_obj("IfcDoor/D1", 2)
    elements = {
        1: FakeElement("IfcWallStandardCase", {"IfcWall", "IfcBuildingElement"}),
        2: FakeElement("IfcDoor", {"IfcBuildingElement"}),
    }
    ifc = SimpleNamespace(by_id=lambda i: elements[i])
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)

    out = bridge._matching_objects([wall, door], {"query": "walls"})
    assert [o.name for o in out] == ["IfcWallStandardCase/W1"]

    out = bridge._matching_objects(
        [wall, door], {"query": "by_class", "ifc_class": "IfcWall"}
    )
    assert [o.name for o in out] == ["IfcWallStandardCase/W1"]


def test_class_fallback_without_ifc_uses_name_prefix(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: None)
    wall = SimpleNamespace(name="IfcWall/W1", type="MESH")
    other = SimpleNamespace(name="Cube", type="MESH")
    out = bridge._matching_objects([wall, other], {"query": "walls"})
    assert [o.name for o in out] == ["IfcWall/W1"]


def test_reload_points_scene_property_at_target_before_loading(bridge, monkeypatch):
    # Bonsai's loader lazily re-resolves the project from the scene's
    # ifc_file property; a reload that does not update it first re-imports
    # the OLD file (found live on Blender 5.1)
    calls = []
    fake_tool = SimpleNamespace(
        IfcGit=SimpleNamespace(load_project=lambda path: calls.append(path))
    )
    monkeypatch.setattr(bridge, "_get_bonsai_tool", lambda: fake_tool)
    bridge._reload_ifc_project("C:/models/new.ifc")
    assert calls == ["C:/models/new.ifc"]
    scene = bridge._fake_bpy.context.scene
    assert scene.BIMProperties.ifc_file == "C:/models/new.ifc"


def test_loaded_ifc_is_memoized_until_invalidated(bridge, monkeypatch):
    calls = {"n": 0}

    def fake_resolve():
        calls["n"] += 1
        return SimpleNamespace()

    monkeypatch.setattr(bridge, "_resolve_loaded_ifc", fake_resolve)
    bridge._invalidate_ifc_cache()
    bridge._get_loaded_ifc()
    bridge._get_loaded_ifc()
    assert calls["n"] == 1
    bridge._invalidate_ifc_cache()
    bridge._get_loaded_ifc()
    assert calls["n"] == 2


def test_find_ifc_element_by_guid_uses_definition_id_map(bridge):
    element = FakeElement("IfcWall", set(), step_id=7)
    ifc = SimpleNamespace(by_guid=lambda gid: element)
    got, name = bridge._find_ifc_element(ifc, None, "2O2Fr$t4X7Zf8NOew3FLKr", {7: "IfcWall/W1"})
    assert got is element
    assert name == "IfcWall/W1"


def test_object_names_by_definition_id_single_pass(bridge):
    bridge._fake_bpy.context.scene.objects = [
        _mesh_obj("IfcWall/W1", 7),
        _mesh_obj("IfcDoor/D1", 9),
        SimpleNamespace(name="NoProps"),  # no BIMObjectProperties: skipped
        _mesh_obj("IfcWall/W1.001", 7),  # duplicate id: first name wins
    ]
    assert bridge._object_names_by_definition_id() == {7: "IfcWall/W1", 9: "IfcDoor/D1"}


# ---------------------------------------------------------------------------
# Pagination and the new query handlers
# ---------------------------------------------------------------------------


def test_paginate_defaults_and_bounds(bridge):
    items = list(range(500))
    page, total, truncated, limit, offset = bridge._paginate(items, {})
    assert (len(page), total, truncated, limit, offset) == (200, 500, True, 200, 0)

    _page, _total, _truncated, limit, offset = bridge._paginate(
        items, {"limit": 99999, "offset": -5}
    )
    assert limit == 1000
    assert offset == 0

    page, *_ = bridge._paginate(items, {"limit": "not-a-number"})
    assert len(page) == 200  # bad input falls back to the default

    page, total, truncated, *_ = bridge._paginate(items, {"limit": 10, "offset": 495})
    assert len(page) == 5
    assert truncated is False


def test_scene_info_query_is_paged(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: None)
    scene = bridge._fake_bpy.context.scene
    scene.name = "Scene"
    scene.collection = SimpleNamespace(children_recursive=[])
    scene.objects = [SimpleNamespace(name=f"IfcWall/W{i}", type="MESH") for i in range(7)]
    bridge._fake_bpy.context.selected_objects = []

    out = bridge._h_get_scene_info({"query": "walls", "limit": 3, "offset": 2})
    assert [o["name"] for o in out["objects"]] == ["IfcWall/W2", "IfcWall/W3", "IfcWall/W4"]
    assert out["objects_total"] == 7
    assert out["objects_truncated"] is True
    assert out["objects_offset"] == 2
    assert out["objects_limit"] == 3


def test_selected_objects_capped_with_metadata(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: None)
    bridge._fake_bpy.context.selected_objects = [
        SimpleNamespace(name=f"O{i}", type="MESH") for i in range(5)
    ]
    out = bridge._h_get_selected_objects({"limit": 2})
    assert [o["name"] for o in out["objects"]] == ["O0", "O1"]
    assert out["total"] == 5
    assert out["truncated"] is True


def test_list_elements_filters_and_pages(bridge, monkeypatch):
    wall1 = FakeElement("IfcWallStandardCase", {"IfcWall"}, step_id=1)
    wall2 = FakeElement("IfcWall", step_id=2)
    door = FakeElement("IfcDoor", step_id=3)
    elements = {1: wall1, 2: wall2, 3: door}
    ifc = SimpleNamespace(by_id=lambda i: elements[i])
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)
    bridge._fake_bpy.context.scene.objects = [
        _mesh_obj("IfcWallStandardCase/W1", 1),
        _mesh_obj("IfcWall/W2", 2),
        _mesh_obj("IfcDoor/D1", 3),
        SimpleNamespace(name="NotIfc", type="MESH"),  # no BIMObjectProperties: skipped
    ]

    out = bridge._h_list_elements({"ifc_class": "IfcWall"})
    assert [e["name"] for e in out["elements"]] == ["IfcWallStandardCase/W1", "IfcWall/W2"]
    assert out["total"] == 2
    assert out["truncated"] is False

    out = bridge._h_list_elements({"name_contains": "W1"})
    assert [e["name"] for e in out["elements"]] == ["IfcWallStandardCase/W1"]

    out = bridge._h_list_elements({"limit": 1, "offset": 1})
    assert out["total"] == 3
    assert out["truncated"] is True
    assert len(out["elements"]) == 1


def test_list_elements_storey_filter(bridge, monkeypatch):
    wall = FakeElement("IfcWall", step_id=1)
    door = FakeElement("IfcDoor", step_id=2)
    other_wall = FakeElement("IfcWall", step_id=5)
    space = FakeElement(
        "IfcSpace",
        step_id=40,
        ContainsElements=[SimpleNamespace(RelatedElements=[door])],
    )
    storey = FakeElement(
        "IfcBuildingStorey",
        step_id=30,
        Name="Level 1",
        GlobalId="ST1",
        ContainsElements=[SimpleNamespace(RelatedElements=[wall])],
        IsDecomposedBy=[SimpleNamespace(RelatedObjects=[space])],
    )
    elements = {1: wall, 2: door, 5: other_wall, 30: storey, 40: space}
    ifc = SimpleNamespace(
        by_id=lambda i: elements[i],
        by_type=lambda cls: [storey] if cls == "IfcBuildingStorey" else [],
    )
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)
    bridge._fake_bpy.context.scene.objects = [
        _mesh_obj("IfcWall/W1", 1),
        _mesh_obj("IfcDoor/D1", 2),
        _mesh_obj("IfcWall/Other", 5),
    ]

    out = bridge._h_list_elements({"storey": "Level 1"})
    # the wall directly in the storey and the door inside the storey's space
    assert [e["name"] for e in out["elements"]] == ["IfcWall/W1", "IfcDoor/D1"]

    out = bridge._h_list_elements({"storey": "ST1"})
    assert out["total"] == 2  # GlobalId works as the storey key too

    with pytest.raises(ValueError, match="No IfcBuildingStorey"):
        bridge._h_list_elements({"storey": "Level 99"})


def _install_fake_selector(monkeypatch, filter_elements):
    """Install a fake ifcopenshell.util.selector into sys.modules."""
    ifcopenshell_mod = types.ModuleType("ifcopenshell")
    util_mod = types.ModuleType("ifcopenshell.util")
    selector_mod = types.ModuleType("ifcopenshell.util.selector")
    selector_mod.filter_elements = filter_elements
    util_mod.selector = selector_mod
    ifcopenshell_mod.util = util_mod
    monkeypatch.setitem(sys.modules, "ifcopenshell", ifcopenshell_mod)
    monkeypatch.setitem(sys.modules, "ifcopenshell.util", util_mod)
    monkeypatch.setitem(sys.modules, "ifcopenshell.util.selector", selector_mod)


def test_list_elements_selector_filter(bridge, monkeypatch):
    wall1 = FakeElement("IfcWall", step_id=1)
    wall2 = FakeElement("IfcWall", step_id=2)
    elements = {1: wall1, 2: wall2}
    ifc = SimpleNamespace(by_id=lambda i: elements[i])
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)
    bridge._fake_bpy.context.scene.objects = [
        _mesh_obj("IfcWall/W1", 1),
        _mesh_obj("IfcWall/W2", 2),
    ]
    seen_queries = []

    def fake_filter(ifc_file, query):
        seen_queries.append((ifc_file, query))
        return [wall1]  # only W1 matches the selector

    _install_fake_selector(monkeypatch, fake_filter)

    out = bridge._h_list_elements({"selector": "IfcWall, Pset_WallCommon.FireRating=F30"})
    assert [e["name"] for e in out["elements"]] == ["IfcWall/W1"]
    assert seen_queries == [(ifc, "IfcWall, Pset_WallCommon.FireRating=F30")]


def test_list_elements_selector_syntax_error_includes_cheat_sheet(bridge, monkeypatch):
    ifc = SimpleNamespace(by_id=lambda i: None)
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)

    def bad_filter(_ifc, _query):
        raise ValueError("unexpected token")

    _install_fake_selector(monkeypatch, bad_filter)

    with pytest.raises(ValueError, match="Invalid selector") as excinfo:
        bridge._h_list_elements({"selector": "IfcWall ==== nope"})
    assert "Selector examples" in str(excinfo.value)


def test_list_elements_selector_without_ifcopenshell_errors_clearly(bridge, monkeypatch):
    ifc = SimpleNamespace(by_id=lambda i: None)
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)
    # a None entry in sys.modules makes `import ifcopenshell...` raise ImportError
    monkeypatch.setitem(sys.modules, "ifcopenshell", None)

    with pytest.raises(RuntimeError, match="selector"):
        bridge._h_list_elements({"selector": "IfcWall"})


def _fake_bonsai_tool_for_refresh(objects_by_element_id):
    """Minimal bonsai.tool fake for the refresh handlers."""
    reloaded: list = []

    class Loader:
        @staticmethod
        def get_name(element):
            return f"{element.is_a()}/{element.Name}"

    class Ifc:
        @staticmethod
        def get_object(element):
            return objects_by_element_id.get(element.id())

    class Geometry:
        @staticmethod
        def reload_representation(objs):
            reloaded.extend(objs)

    return SimpleNamespace(Loader=Loader, Ifc=Ifc, Geometry=Geometry), reloaded


def test_refresh_view_syncs_names(bridge, monkeypatch):
    wall = FakeElement("IfcWall", step_id=1, Name="W1 renamed", GlobalId="G1")
    obj = SimpleNamespace(name="IfcWall/W1 old")
    elements = {"G1": wall}
    ifc = SimpleNamespace(by_guid=lambda g: elements[g])
    tool_mod, _reloaded = _fake_bonsai_tool_for_refresh({1: obj})
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)
    monkeypatch.setattr(bridge, "_get_bonsai_tool", lambda: tool_mod)

    out = bridge._h_refresh_view({"global_ids": ["G1", "MISSING"]})
    assert obj.name == "IfcWall/W1 renamed"
    assert out["refreshed"] == 1
    assert out["results"][0]["renamed"] is True
    assert "reload_project" in out["results"][1]["error"]


def test_refresh_view_element_without_object_reports_error(bridge, monkeypatch):
    wall = FakeElement("IfcWall", step_id=1, Name="W1", GlobalId="G1")
    ifc = SimpleNamespace(by_guid=lambda g: wall)
    tool_mod, _reloaded = _fake_bonsai_tool_for_refresh({})  # no Blender object
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)
    monkeypatch.setattr(bridge, "_get_bonsai_tool", lambda: tool_mod)

    out = bridge._h_refresh_view({"global_ids": ["G1"]})
    assert out["refreshed"] == 0
    assert "reload_project" in out["results"][0]["error"]


def test_refresh_targets_validation(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: SimpleNamespace())
    monkeypatch.setattr(bridge, "_get_bonsai_tool", lambda: SimpleNamespace())
    with pytest.raises(ValueError, match="global_ids"):
        bridge._h_refresh_view({})
    with pytest.raises(ValueError, match="Too many targets"):
        bridge._h_refresh_view({"global_ids": ["G"] * (bridge._REFRESH_MAX_TARGETS + 1)})


def test_refresh_geometry_reloads_representation_and_placement(bridge, monkeypatch):
    wall = FakeElement("IfcWall", step_id=1, Name="W1", GlobalId="G1")
    obj = SimpleNamespace(name="IfcWall/W1")
    ifc = SimpleNamespace(by_guid=lambda g: wall)
    tool_mod, reloaded = _fake_bonsai_tool_for_refresh({1: obj})
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)
    monkeypatch.setattr(bridge, "_get_bonsai_tool", lambda: tool_mod)
    placement_calls = []
    monkeypatch.setattr(
        bridge,
        "_sync_object_placement",
        lambda t, f, e, o: placement_calls.append(e) or True,
    )

    out = bridge._h_refresh_geometry({"global_ids": ["G1"]})
    assert out["refreshed"] == 1
    assert out["representations_reloaded"] is True
    assert reloaded == [obj]
    assert placement_calls == [wall]
    assert out["results"][0]["placement_synced"] is True


def test_reload_project_uses_temp_file_and_restores_path(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_bonsai_tool", lambda: SimpleNamespace())
    monkeypatch.setattr(bridge, "_bonsai_project_path", lambda: "C:/models/house.ifc")
    saved, loaded, restored = [], [], []
    monkeypatch.setattr(bridge, "_save_ifc_project", lambda p: saved.append(p) or "exporter")
    monkeypatch.setattr(bridge, "_reload_ifc_project", lambda p: loaded.append(p))
    monkeypatch.setattr(bridge, "_restore_project_path", lambda p: restored.append(p) or True)

    out = bridge._h_reload_project({})
    assert out["reloaded"] is True and out["path_restored"] is True
    assert out["project_path"] == "C:/models/house.ifc"
    assert saved == loaded and len(saved) == 1
    temp_path = saved[0]
    assert temp_path != "C:/models/house.ifc"
    assert temp_path.endswith("house.ifc")
    assert restored == ["C:/models/house.ifc"]
    bridge._cleanup_reload_dir()


def test_reload_project_without_path_errors(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_bonsai_tool", lambda: SimpleNamespace())
    monkeypatch.setattr(bridge, "_bonsai_project_path", lambda: None)
    with pytest.raises(RuntimeError, match="no file path"):
        bridge._h_reload_project({})


def test_scene_info_selected_names_capped(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: None)
    scene = bridge._fake_bpy.context.scene
    scene.name = "Scene"
    scene.collection = SimpleNamespace(children_recursive=[])
    over_cap = bridge._SCENE_SELECTED_NAMES_CAP + 10
    objs = [SimpleNamespace(name=f"O{i}", type="MESH") for i in range(over_cap)]
    scene.objects = objs
    bridge._fake_bpy.context.selected_objects = objs

    out = bridge._h_get_scene_info({})
    assert out["selected_count"] == over_cap
    assert len(out["selected_objects"]) == bridge._SCENE_SELECTED_NAMES_CAP
    assert out["selected_objects_truncated"] is True


def test_spatial_structure_tree(bridge, monkeypatch):
    wall = FakeElement("IfcWall", step_id=100)
    door = FakeElement("IfcDoor", step_id=101)
    space = FakeElement("IfcSpace", step_id=40, Name="Kitchen", GlobalId="SP1")
    storey = FakeElement(
        "IfcBuildingStorey",
        step_id=30,
        Name="Level 1",
        GlobalId="ST1",
        Elevation=3.0,
        ContainsElements=[SimpleNamespace(RelatedElements=[wall, door])],
        IsDecomposedBy=[SimpleNamespace(RelatedObjects=[space])],
    )
    building = FakeElement(
        "IfcBuilding",
        step_id=20,
        Name="House",
        GlobalId="B1",
        IsDecomposedBy=[SimpleNamespace(RelatedObjects=[storey])],
    )
    site = FakeElement(
        "IfcSite",
        step_id=10,
        Name="Site",
        GlobalId="S1",
        IsDecomposedBy=[SimpleNamespace(RelatedObjects=[building])],
    )
    project = FakeElement(
        "IfcProject",
        step_id=1,
        Name="P",
        GlobalId="PR1",
        IsDecomposedBy=[SimpleNamespace(RelatedObjects=[site])],
    )
    ifc = SimpleNamespace(
        by_type=lambda cls: [project] if cls == "IfcProject" else [], schema="IFC4"
    )
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)

    out = bridge._h_get_spatial_structure({})
    assert out["schema"] == "IFC4"
    tree = out["tree"]
    assert tree["ifc_class"] == "IfcProject"
    storey_node = tree["children"][0]["children"][0]["children"][0]
    assert storey_node["name"] == "Level 1"
    assert storey_node["elevation"] == 3.0
    assert storey_node["element_counts"] == {"IfcDoor": 1, "IfcWall": 1}
    assert storey_node["element_total"] == 2
    assert storey_node["children"][0]["name"] == "Kitchen"

    out = bridge._h_get_spatial_structure({"include_element_counts": False})
    storey_node = out["tree"]["children"][0]["children"][0]["children"][0]
    assert "element_counts" not in storey_node


def test_spatial_structure_requires_ifc(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: None)
    with pytest.raises(RuntimeError, match="No IFC project"):
        bridge._h_get_spatial_structure({})


def _install_fake_ifcopenshell(monkeypatch, qtos_by_id: dict) -> None:
    ifcopenshell_mod = types.ModuleType("ifcopenshell")
    util_mod = types.ModuleType("ifcopenshell.util")
    element_mod = types.ModuleType("ifcopenshell.util.element")

    def get_psets(element, psets_only=False, qtos_only=False):
        return qtos_by_id.get(element.id(), {})

    element_mod.get_psets = get_psets
    util_mod.element = element_mod
    ifcopenshell_mod.util = util_mod
    monkeypatch.setitem(sys.modules, "ifcopenshell", ifcopenshell_mod)
    monkeypatch.setitem(sys.modules, "ifcopenshell.util", util_mod)
    monkeypatch.setitem(sys.modules, "ifcopenshell.util.element", element_mod)


def test_quantities_aggregation(bridge, monkeypatch):
    storey = FakeElement("IfcBuildingStorey", step_id=30, Name="Level 1")
    w1 = FakeElement(
        "IfcWallStandardCase",
        {"IfcWall"},
        step_id=1,
        ContainedInStructure=[SimpleNamespace(RelatingStructure=storey)],
    )
    w2 = FakeElement(
        "IfcWall",
        step_id=2,
        ContainedInStructure=[SimpleNamespace(RelatingStructure=storey)],
    )
    w3 = FakeElement("IfcWall", step_id=3)  # no storey, no quantities
    project = FakeElement(
        "IfcProject",
        step_id=99,
        UnitsInContext=SimpleNamespace(
            Units=[SimpleNamespace(UnitType="LENGTHUNIT", Name="METRE", Prefix="MILLI")]
        ),
    )

    def by_type(cls):
        if cls == "IfcWall":
            return [w1, w2, w3]
        if cls == "IfcProject":
            return [project]
        raise ValueError(f"unknown class {cls!r}")

    ifc = SimpleNamespace(by_type=by_type, schema="IFC4")
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: ifc)
    _install_fake_ifcopenshell(
        monkeypatch,
        {
            1: {"Qto_WallBaseQuantities": {"id": 900, "NetSideArea": 10.5, "Width": 0.2}},
            2: {"Qto_WallBaseQuantities": {"id": 901, "NetSideArea": 4.5}},
        },
    )

    out = bridge._h_get_quantities(
        {"ifc_classes": ["IfcWall", "IfcNotAClass"], "by_storey": True}
    )
    walls = out["classes"]["IfcWall"]
    assert walls["count"] == 3
    assert walls["elements_without_quantities"] == 1
    assert walls["quantities"]["NetSideArea"] == {"sum": 15.0, "elements": 2}
    assert walls["quantities"]["Width"] == {"sum": 0.2, "elements": 1}
    # the "id" pseudo-key from get_psets must never be summed as a quantity
    assert "id" not in walls["quantities"]
    assert out["classes"]["IfcNotAClass"]["count"] == 0
    assert "note" in out["classes"]["IfcNotAClass"]
    assert out["units"]["LENGTHUNIT"] == "millimetre"
    level = out["by_storey"]["Level 1"]["IfcWall"]
    assert level["count"] == 2
    assert level["quantities"]["NetSideArea"]["sum"] == 15.0
    assert out["by_storey"]["(no storey)"]["IfcWall"]["count"] == 1


def test_quantities_rejects_non_list_classes(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: SimpleNamespace())
    with pytest.raises(ValueError, match="must be a list"):
        bridge._h_get_quantities({"ifc_classes": "IfcWall"})


def test_quantities_requires_ifc(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "_get_loaded_ifc", lambda: None)
    with pytest.raises(RuntimeError, match="No IFC project"):
        bridge._h_get_quantities({})


# ---------------------------------------------------------------------------
# Request lifecycle: drain, cancellation, read-only mode, flush on stop
# ---------------------------------------------------------------------------


def _mark_running(bridge) -> None:
    """Pretend the bridge server is up (the drain refuses to run when stopped)."""
    bridge._STATE["server"] = object()


def test_drain_executes_ping_and_records_activity(bridge):
    _mark_running(bridge)
    pending = bridge._PendingRequest("ping", {})
    bridge._STATE["request_queue"].put(pending)
    bridge._drain_queue()
    assert pending.done.is_set()
    assert pending.finished is True
    assert pending.error is None
    assert pending.result["service"] == "bonsai-mcp-bridge"
    assert pending.result["edits_allowed"] is True
    assert bridge._STATE["requests_served"] == 1
    assert bridge._STATE["last_command"] == "ping"


def test_drain_skips_cancelled_requests(bridge):
    _mark_running(bridge)
    pending = bridge._PendingRequest("ping", {})
    pending.cancelled = True
    bridge._STATE["request_queue"].put(pending)
    bridge._drain_queue()
    assert pending.done.is_set()
    assert pending.finished is False
    assert bridge._STATE["requests_served"] == 0


def test_drain_when_bridge_stopped_flushes_and_unregisters(bridge):
    # server is None (stopped): the drain must fail queued requests and
    # return None so Blender unregisters the timer (a handler thread may
    # have re-registered it after _stop_server)
    pending = bridge._PendingRequest("execute_code", {"code": "print(1)"})
    bridge._STATE["request_queue"].put(pending)
    assert bridge._drain_queue() is None
    assert pending.done.is_set()
    assert pending.finished is True
    assert "bridge stopped" in (pending.error or "")


def test_ensure_timer_not_registered_when_stopped(bridge):
    # handler threads call this on every request; it must be a no-op after stop
    bridge._ensure_timer_running()
    assert bridge._fake_bpy.app.timers.registered == []
    _mark_running(bridge)
    bridge._ensure_timer_running()
    assert bridge._fake_bpy.app.timers.registered == [bridge._drain_queue]


def test_read_only_mode_blocks_edit_commands(bridge, monkeypatch):
    _mark_running(bridge)
    monkeypatch.setattr(bridge, "_edits_allowed", lambda: False)
    for command in sorted(bridge._EDIT_COMMANDS):
        pending = bridge._PendingRequest(command, {"code": "print(1)"})
        bridge._STATE["request_queue"].put(pending)
        bridge._drain_queue()
        assert pending.finished is True
        assert pending.error is not None and "Editing is disabled" in pending.error, command

    # QUERY commands keep working
    pending = bridge._PendingRequest("ping", {})
    bridge._STATE["request_queue"].put(pending)
    bridge._drain_queue()
    assert pending.error is None
    assert pending.result["edits_allowed"] is False


def test_unknown_command_reports_error(bridge):
    _mark_running(bridge)
    pending = bridge._PendingRequest("definitely_not_a_command", {})
    bridge._STATE["request_queue"].put(pending)
    bridge._drain_queue()
    assert pending.finished is True
    assert "Unknown command" in (pending.error or "")


def test_system_exit_in_executed_code_is_contained(bridge):
    _mark_running(bridge)
    pending = bridge._PendingRequest("execute_code", {"code": "import sys; sys.exit(3)"})
    bridge._STATE["request_queue"].put(pending)
    # must not raise out of the timer callback (that would kill the timer)
    assert bridge._drain_queue() == 0.05
    assert pending.finished is True
    assert pending.error is None
    assert pending.result["success"] is False
    assert "SystemExit" in pending.result["error"]


def test_stop_server_flushes_queued_requests(bridge):
    pending = bridge._PendingRequest("ping", {})
    bridge._STATE["request_queue"].put(pending)
    bridge._stop_server()  # no server running: still flushes + stops timer
    assert pending.done.is_set()
    assert pending.finished is True
    assert "bridge stopped" in (pending.error or "")


def test_cleanup_screenshot_dir_removes_directory(bridge, tmp_path):
    shot_dir = tmp_path / "bonsai_mcp_shot"
    shot_dir.mkdir()
    (shot_dir / "viewport.png").write_bytes(b"png")
    bridge._STATE["screenshot_dir"] = str(shot_dir)
    bridge._cleanup_screenshot_dir()
    assert not shot_dir.exists()
    assert "screenshot_dir" not in bridge._STATE


def test_await_result_returns_immediately_when_done(bridge):
    _mark_running(bridge)
    a, b = socket.socketpair()
    try:
        pending = bridge._PendingRequest("ping", {})
        pending.done.set()
        assert bridge._BridgeHandler._await_result(b, pending) is True
        assert pending.cancelled is False
    finally:
        a.close()
        b.close()


def test_await_result_cancels_queued_request_on_client_close(bridge, monkeypatch):
    _mark_running(bridge)
    monkeypatch.setattr(bridge, "MAIN_THREAD_WAIT_SECONDS", 1.5)
    a, b = socket.socketpair()
    try:
        pending = bridge._PendingRequest("execute_code", {"code": "1"})
        bridge._STATE["request_queue"].put(pending)
        a.close()  # client goes away while the request is queued

        # simulate the main-thread drain arriving after the disconnect
        def drain_later():
            time.sleep(0.8)
            bridge._drain_queue()

        t = threading.Thread(target=drain_later, daemon=True)
        t.start()
        alive = bridge._BridgeHandler._await_result(b, pending)
        t.join()
        assert alive is True  # a reply is attempted; a dead peer just raises
        assert pending.cancelled is True
        assert pending.cancel_reason == "client_closed"
        assert pending.finished is False  # the drain skipped it, never ran it
    finally:
        b.close()


def test_await_result_delivers_in_flight_result_after_half_close(bridge):
    # a one-shot client may send, shutdown(SHUT_WR), then read the reply;
    # a result that is already executing must still be delivered
    _mark_running(bridge)
    a, b = socket.socketpair()
    try:
        pending = bridge._PendingRequest("ping", {})
        a.shutdown(socket.SHUT_WR)  # half-close: still reading

        def finish_later():
            time.sleep(1.2)
            pending.result = {"ok": True}
            pending.finished = True
            pending.done.set()

        t = threading.Thread(target=finish_later, daemon=True)
        t.start()
        alive = bridge._BridgeHandler._await_result(b, pending)
        t.join()
        assert alive is True
        assert pending.finished is True  # reply will be sent, not dropped
    finally:
        a.close()
        b.close()


def test_await_result_reports_bridge_stopped(bridge):
    # server is None: the wait must fail fast, not hang for 120s
    a, b = socket.socketpair()
    try:
        pending = bridge._PendingRequest("ping", {})
        started = time.monotonic()
        alive = bridge._BridgeHandler._await_result(b, pending)
        assert alive is True
        assert time.monotonic() - started < 5.0
        assert pending.finished is True
        assert "bridge stopped" in (pending.error or "")
    finally:
        a.close()
        b.close()


def test_await_result_times_out_and_cancels(bridge, monkeypatch):
    _mark_running(bridge)
    monkeypatch.setattr(bridge, "MAIN_THREAD_WAIT_SECONDS", 0.6)
    a, b = socket.socketpair()
    try:
        pending = bridge._PendingRequest("ping", {})
        started = time.monotonic()
        alive = bridge._BridgeHandler._await_result(b, pending)
        elapsed = time.monotonic() - started
        assert alive is True
        assert pending.cancelled is True
        assert pending.cancel_reason == "timeout"
        assert pending.finished is False
        assert elapsed < 5.0
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# End-to-end over real TCP: the add-on's server machinery + the real client
# ---------------------------------------------------------------------------


@pytest.fixture
def running_bridge(bridge):
    server = bridge._ThreadedTCPServer(("127.0.0.1", 0), bridge._BridgeHandler)
    bridge._STATE["server"] = server  # handlers verify the bridge is running
    port = server.server_address[1]
    serve = threading.Thread(target=server.serve_forever, daemon=True)
    serve.start()
    stop_pump = threading.Event()

    def pump():
        while not stop_pump.is_set():
            bridge._drain_queue()
            time.sleep(0.005)

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()
    try:
        yield bridge, port
    finally:
        stop_pump.set()
        pump_thread.join(timeout=2)
        bridge._STATE["server"] = None
        server.shutdown()
        server.server_close()


def test_tcp_ping_roundtrip_with_real_client(running_bridge):
    _bridge, port = running_bridge
    client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
    info = client.ping()
    assert info["service"] == "bonsai-mcp-bridge"
    assert info["status"] == "ok"
    assert info["edits_allowed"] is True


def test_tcp_unknown_command_is_a_clean_error(running_bridge):
    _bridge, port = running_bridge
    client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0)
    with pytest.raises(BlenderBridgeError, match="Unknown command"):
        client.send("definitely_not_a_command")


def test_tcp_oversized_frame_gets_error_reply(running_bridge):
    bridge_mod, port = running_bridge
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    try:
        sock.sendall(struct.pack(">I", bridge_mod.MAX_MESSAGE_BYTES + 1))
        reply = bc._read_message(sock)
        assert reply is not None
        assert reply["success"] is False
        assert "Protocol error" in reply["error"]
    finally:
        sock.close()


def test_tcp_non_json_frame_gets_error_reply(running_bridge):
    _bridge, port = running_bridge
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    try:
        body = b"this is not json"
        sock.sendall(struct.pack(">I", len(body)) + body)
        reply = bc._read_message(sock)
        assert reply is not None
        assert reply["success"] is False
        assert "Protocol error" in reply["error"]
    finally:
        sock.close()


def test_tcp_oversized_frame_with_body_still_gets_error_reply(running_bridge):
    # unread body bytes must not RST the connection before the error reply
    # is delivered (e.g. an HTTP client hitting the port)
    bridge_mod, port = running_bridge
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    try:
        sock.sendall(struct.pack(">I", bridge_mod.MAX_MESSAGE_BYTES + 1))
        sock.sendall(b"x" * 4096)  # body the add-on never reads as a frame
        reply = bc._read_message(sock)
        assert reply is not None
        assert reply["success"] is False
        assert "Protocol error" in reply["error"]
    finally:
        sock.close()


def test_tcp_request_after_stop_is_refused(running_bridge):
    bridge_mod, port = running_bridge
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    try:
        bc._write_message(sock, {"command": "ping", "params": {}})
        reply = bc._read_message(sock)
        assert reply is not None and reply["success"] is True

        # Stop Bridge while this connection stays open: further requests on
        # the still-open socket must be refused, not executed
        bridge_mod._STATE["server"] = None
        bc._write_message(sock, {"command": "ping", "params": {}})
        reply = bc._read_message(sock)
        assert reply is not None
        assert reply["success"] is False
        assert "bridge stopped" in reply["error"]
    finally:
        sock.close()


def test_tcp_persistent_client_reuses_connection(running_bridge):
    _bridge, port = running_bridge
    client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0, token="")
    assert client.ping()["status"] == "ok"
    assert client.ping()["status"] == "ok"
    assert client._sock is not None  # the connection stayed open between calls
    client.close()


def test_tcp_request_id_is_echoed(running_bridge):
    _bridge, port = running_bridge
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    try:
        bc._write_message(sock, {"command": "ping", "params": {}, "id": 42})
        reply = bc._read_message(sock)
        assert reply is not None
        assert reply["success"] is True
        assert reply["id"] == 42
    finally:
        sock.close()


def test_tcp_token_required_rejects_and_accepts(running_bridge):
    bridge_mod, port = running_bridge
    bridge_mod._STATE["token"] = "sekret"
    try:
        wrong = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0, token="nope")
        with pytest.raises(BlenderBridgeError, match="token"):
            wrong.ping()

        missing = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0, token="")
        with pytest.raises(BlenderBridgeError, match="token"):
            missing.ping()

        good = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0, token="sekret")
        info = good.ping()
        assert info["status"] == "ok"
        assert info["token_required"] is True
        good.close()
    finally:
        bridge_mod._STATE["token"] = ""


def test_tcp_list_elements_roundtrip(running_bridge):
    _bridge, port = running_bridge
    client = BlenderBridgeClient(host="127.0.0.1", port=port, timeout=5.0, token="")
    out = client.send("list_elements", {})
    assert out == {"elements": [], "total": 0, "truncated": False, "offset": 0, "limit": 200}
    client.close()


# ---------------------------------------------------------------------------
# Cross-file parity: the add-on and the server must agree on shared contracts
# ---------------------------------------------------------------------------


def test_edit_commands_match_server_edit_tools(bridge):
    from bonsai_mcp import tools

    # tool name -> bridge command (execute_blender_code is the one rename)
    tool_to_command = {"execute_blender_code": "execute_code"}
    expected = {tool_to_command.get(name, name) for name in tools.EDIT_TOOL_NAMES}
    assert expected == set(bridge._EDIT_COMMANDS), (
        "read-only mode would not block every EDIT tool: keep _EDIT_COMMANDS "
        "in the add-on in sync with EDIT_TOOL_NAMES in tools.py"
    )


def test_max_message_bytes_parity(bridge):
    assert bridge.MAX_MESSAGE_BYTES == bc.MAX_MESSAGE_BYTES
