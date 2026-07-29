"""Schema validation tests. No Blender required."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bonsai_mcp.schemas import (
    PSETS_BATCH_MAX,
    BridgeRequest,
    BridgeResponse,
    ExecuteCodeInput,
    ExecuteIfcCodeInput,
    GetPsetsInput,
    GetQuantitiesInput,
    GetSceneInfoInput,
    GetSelectedObjectsInput,
    GetSpatialStructureInput,
    ListElementsInput,
    ObjectSummary,
    SaveIfcInput,
    ViewportScreenshotInput,
)


class TestBridgeRequest:
    def test_minimal_request(self):
        req = BridgeRequest(command="ping")
        assert req.command == "ping"
        assert req.params == {}

    def test_request_with_params(self):
        req = BridgeRequest(command="execute_code", params={"code": "print(1)"})
        assert req.params == {"code": "print(1)"}

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            BridgeRequest(command="ping", extra="nope")  # type: ignore[call-arg]

    def test_roundtrip_dict(self):
        req = BridgeRequest(command="get_scene_info")
        assert req.model_dump(exclude_none=True) == {
            "command": "get_scene_info",
            "params": {},
        }
        assert req.id is None
        assert req.token is None

    def test_optional_id_and_token(self):
        req = BridgeRequest(command="ping", id=7, token="sekret")
        assert req.id == 7
        assert req.token == "sekret"


class TestBridgeResponse:
    def test_success(self):
        resp = BridgeResponse(success=True, result={"hello": "world"})
        assert resp.success is True
        assert resp.result == {"hello": "world"}
        assert resp.error is None

    def test_failure(self):
        resp = BridgeResponse(success=False, error="boom", traceback="...")
        assert resp.success is False
        assert resp.error == "boom"

    def test_unknown_fields_allowed(self):
        resp = BridgeResponse.model_validate(
            {"success": True, "result": 1, "future_field": "ok"}
        )
        assert resp.success is True


class TestExecuteCodeInput:
    def test_requires_code(self):
        with pytest.raises(ValidationError):
            ExecuteCodeInput()  # type: ignore[call-arg]

    def test_extra_rejected(self):
        with pytest.raises(ValidationError):
            ExecuteCodeInput(code="x", extra=1)  # type: ignore[call-arg]


class TestExecuteIfcCodeInput:
    def test_requires_code(self):
        with pytest.raises(ValidationError):
            ExecuteIfcCodeInput()  # type: ignore[call-arg]

    def test_accepts_valid_code(self):
        m = ExecuteIfcCodeInput(code="print(ifc)")
        assert m.code == "print(ifc)"

    def test_extra_rejected(self):
        with pytest.raises(ValidationError):
            ExecuteIfcCodeInput(code="x", extra=1)  # type: ignore[call-arg]


class TestGetSceneInfoInput:
    def test_no_query_is_valid(self):
        m = GetSceneInfoInput()
        assert m.query is None
        assert m.ifc_class is None

    def test_simple_query(self):
        m = GetSceneInfoInput(query="walls")
        assert m.query == "walls"

    def test_by_class(self):
        m = GetSceneInfoInput(query="by_class", ifc_class="IfcWall")
        assert m.query == "by_class"
        assert m.ifc_class == "IfcWall"

    def test_extra_rejected(self):
        with pytest.raises(ValidationError):
            GetSceneInfoInput(query="walls", garbage=1)  # type: ignore[call-arg]


class TestSaveIfcInput:
    def test_default_no_overwrite(self):
        s = SaveIfcInput(output_path="/tmp/out.ifc")
        assert s.overwrite is False
        assert s.reload is False

    def test_path_optional_for_in_place_save(self):
        s = SaveIfcInput()
        assert s.output_path is None
        assert s.overwrite is False
        assert s.reload is False

    def test_reload_flag(self):
        s = SaveIfcInput(reload=True)
        assert s.reload is True


class TestViewportScreenshotInput:
    def test_defaults(self):
        s = ViewportScreenshotInput()
        assert s.max_size == 800
        assert s.format == "jpeg"
        assert s.quality == 85

    def test_bounds_enforced(self):
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(max_size=10)
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(max_size=99999)
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(quality=0)

    def test_format_restricted(self):
        assert ViewportScreenshotInput(format="png").format == "png"
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(format="bmp")


class TestObjectSummary:
    def test_all_optional_except_name(self):
        s = ObjectSummary(name="Cube")
        assert s.name == "Cube"
        assert s.ifc_class is None


class TestGetPsetsInput:
    def test_accepts_single_global_id(self):
        m = GetPsetsInput(global_ids=["AAAA"])
        assert m.global_ids == ["AAAA"]
        assert m.names == []

    def test_accepts_single_name(self):
        m = GetPsetsInput(names=["IfcWall/MyWall"])
        assert m.names == ["IfcWall/MyWall"]
        assert m.global_ids == []

    def test_accepts_mix(self):
        m = GetPsetsInput(global_ids=["AAAA"], names=["IfcDoor/D1"])
        assert m.global_ids == ["AAAA"]
        assert m.names == ["IfcDoor/D1"]

    def test_requires_at_least_one_target(self):
        with pytest.raises(ValidationError):
            GetPsetsInput()

    def test_rejects_two_empty_lists(self):
        with pytest.raises(ValidationError):
            GetPsetsInput(global_ids=[], names=[])

    def test_rejects_blank_global_id(self):
        with pytest.raises(ValidationError):
            GetPsetsInput(global_ids=["AAAA", ""])

    def test_rejects_whitespace_only_name(self):
        with pytest.raises(ValidationError):
            GetPsetsInput(names=["   "])

    def test_large_batches_allowed_with_paging(self):
        # what used to be a hard error is now paged via limit/offset
        half = PSETS_BATCH_MAX // 2 + 1
        m = GetPsetsInput(
            global_ids=[f"G{i}" for i in range(half)],
            names=[f"N{i}" for i in range(half)],
        )
        assert m.limit == PSETS_BATCH_MAX
        assert m.offset == 0

    def test_limit_and_offset_bounds(self):
        with pytest.raises(ValidationError):
            GetPsetsInput(global_ids=["A"], limit=0)
        with pytest.raises(ValidationError):
            GetPsetsInput(global_ids=["A"], limit=PSETS_BATCH_MAX + 1)
        with pytest.raises(ValidationError):
            GetPsetsInput(global_ids=["A"], offset=-1)

    def test_extra_rejected(self):
        with pytest.raises(ValidationError):
            GetPsetsInput(global_ids=["A"], garbage=1)  # type: ignore[call-arg]


class TestViewportScreenshotV2Fields:
    def test_defaults(self):
        s = ViewportScreenshotInput()
        assert s.azimuth is None
        assert s.elevation is None
        assert s.storey is None
        assert s.shading is None
        assert s.show_overlays is False

    def test_azimuth_elevation_accepted(self):
        s = ViewportScreenshotInput(azimuth=120.5, elevation=-10)
        assert s.azimuth == pytest.approx(120.5)
        assert s.elevation == pytest.approx(-10.0)

    def test_view_conflicts_with_azimuth(self):
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(view="top", azimuth=10)
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(view="iso", elevation=45)

    def test_shading_restricted(self):
        assert ViewportScreenshotInput(shading="class_colors").shading == "class_colors"
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(shading="fancy")

    def test_elevation_bounds(self):
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(elevation=91)
        with pytest.raises(ValidationError):
            ViewportScreenshotInput(azimuth=361)


class TestListElementsInput:
    def test_defaults(self):
        m = ListElementsInput()
        assert m.ifc_class is None
        assert m.name_contains is None
        assert m.storey is None
        assert m.limit == 200
        assert m.offset == 0

    def test_limit_bounds(self):
        with pytest.raises(ValidationError):
            ListElementsInput(limit=0)
        with pytest.raises(ValidationError):
            ListElementsInput(limit=1001)

    def test_extra_rejected(self):
        with pytest.raises(ValidationError):
            ListElementsInput(garbage=1)  # type: ignore[call-arg]


class TestSpatialAndQuantitiesInputs:
    def test_spatial_defaults(self):
        assert GetSpatialStructureInput().include_element_counts is True

    def test_quantities_defaults(self):
        m = GetQuantitiesInput()
        assert m.ifc_classes is None
        assert m.by_storey is False

    def test_selected_objects_default_limit(self):
        assert GetSelectedObjectsInput().limit == 200

    def test_scene_info_paging_defaults(self):
        m = GetSceneInfoInput()
        assert m.limit == 200
        assert m.offset == 0
