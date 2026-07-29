"""Tests for the `bonsai-mcp` CLI (especially `doctor`)."""

from __future__ import annotations

import io

import pytest

from bonsai_mcp.blender_client import BlenderBridgeError
from bonsai_mcp.cli import build_parser, run_doctor
from tests.conftest import FakeBlenderBridgeClient


def test_parser_has_doctor_and_serve_subcommands():
    parser = build_parser()
    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"

    args = parser.parse_args(["serve"])
    assert args.command == "serve"


def test_parser_defaults_to_no_subcommand():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.command is None


def test_doctor_reports_bridge_ok():
    from bonsai_mcp import __version__

    client = FakeBlenderBridgeClient(
        responses={
            "ping": {
                "status": "ok",
                "service": "bonsai-mcp-bridge",
                "blender_version": "4.2.1",
                "addon_version": __version__,
                "ifcopenshell_available": True,
                "ifc_loaded": False,
            }
        }
    )
    buf = io.StringIO()
    rc = run_doctor(client, stream=buf)
    out = buf.getvalue()

    assert rc == 0
    assert "bridge           OK" in out
    assert "4.2.1" in out
    assert f"addon version  {__version__}" in out
    assert "WARNING" not in out
    assert "ifcopenshell   available" in out
    assert "ifc loaded     no" in out
    assert '"BONSAI_MCP_HOST": "127.0.0.1"' in out


def test_doctor_warns_on_addon_version_mismatch():
    """A pip-installed server talking to a stale add-on should warn, but not fail."""
    from bonsai_mcp import __version__

    stale = "0.0.1-stale"
    assert stale != __version__
    client = FakeBlenderBridgeClient(
        responses={
            "ping": {
                "status": "ok",
                "blender_version": "4.2.1",
                "addon_version": stale,
                "ifcopenshell_available": True,
                "ifc_loaded": False,
            }
        }
    )
    buf = io.StringIO()
    rc = run_doctor(client, stream=buf)
    out = buf.getvalue()

    assert rc == 0
    assert f"addon version  {stale}" in out
    assert "WARNING" in out
    assert stale in out
    assert __version__ in out


def test_doctor_handles_missing_addon_version():
    """Old add-ons predate `addon_version`. Doctor prints 'unknown' and skips the warning."""
    client = FakeBlenderBridgeClient(
        responses={
            "ping": {
                "status": "ok",
                "blender_version": "4.2.1",
                "ifcopenshell_available": True,
                "ifc_loaded": False,
            }
        }
    )
    buf = io.StringIO()
    rc = run_doctor(client, stream=buf)
    out = buf.getvalue()

    assert rc == 0
    assert "addon version  unknown" in out
    assert "WARNING" not in out


def test_doctor_warns_when_service_is_not_the_bridge():
    """Another bridge answering on the port should be called out, not silently accepted."""
    client = FakeBlenderBridgeClient(
        responses={
            "ping": {
                "status": "ok",
                "service": "some-other-bridge",
                "blender_version": "4.2.1",
                "ifcopenshell_available": True,
                "ifc_loaded": False,
            }
        }
    )
    buf = io.StringIO()
    rc = run_doctor(client, stream=buf)
    out = buf.getvalue()

    assert rc == 0
    assert "WARNING" in out
    assert "some-other-bridge" in out


def test_doctor_lists_registered_tools():
    """Report registered MCP tools."""
    from bonsai_mcp.tools import ALL_TOOL_NAMES

    client = FakeBlenderBridgeClient(
        responses={
            "ping": {
                "status": "ok",
                "blender_version": "4.2.1",
                "ifcopenshell_available": True,
                "ifc_loaded": False,
            }
        }
    )
    buf = io.StringIO()
    run_doctor(client, stream=buf)
    out = buf.getvalue()

    assert f"mcp tools ({len(ALL_TOOL_NAMES)})" in out
    for tool_name in ALL_TOOL_NAMES:
        assert tool_name in out


def test_doctor_lists_tools_even_when_unreachable():
    """Report tools when the bridge is unavailable."""
    from bonsai_mcp.tools import ALL_TOOL_NAMES

    client = FakeBlenderBridgeClient(
        responses={"ping": BlenderBridgeError("Cannot reach Blender bridge at 127.0.0.1:9878")}
    )
    buf = io.StringIO()
    rc = run_doctor(client, stream=buf)
    out = buf.getvalue()

    assert rc == 1
    for tool_name in ALL_TOOL_NAMES:
        assert tool_name in out


def test_doctor_surfaces_bridge_extras_verbatim():
    client = FakeBlenderBridgeClient(
        responses={
            "ping": {
                "blender_version": "4.2.1",
                "ifcopenshell_available": True,
                "ifc_loaded": True,
                "ifc_schema": "IFC4",
            }
        }
    )
    buf = io.StringIO()
    rc = run_doctor(client, stream=buf)
    out = buf.getvalue()

    assert rc == 0
    assert "ifc loaded     yes" in out
    assert "ifc_schema" in out
    assert "IFC4" in out


def test_doctor_reports_unreachable_bridge():
    client = FakeBlenderBridgeClient(
        responses={"ping": BlenderBridgeError("Cannot reach Blender bridge at 127.0.0.1:9878")}
    )
    buf = io.StringIO()
    rc = run_doctor(client, stream=buf)
    out = buf.getvalue()

    assert rc == 1
    assert "UNREACHABLE" in out
    assert "Start Blender" in out
    assert "Bonsai MCP" in out


def test_doctor_reports_token_requirement():
    client = FakeBlenderBridgeClient(
        responses={
            "ping": {
                "status": "ok",
                "blender_version": "4.2.1",
                "ifcopenshell_available": True,
                "ifc_loaded": True,
                "token_required": True,
            }
        }
    )
    buf = io.StringIO()
    run_doctor(client, stream=buf)
    out = buf.getvalue()
    assert "BONSAI_MCP_TOKEN" in out
    assert "extras" not in out  # token_required has a first-party line, not an extra


class TestDoctorJson:
    _PING = {
        "status": "ok",
        "service": "bonsai-mcp-bridge",
        "blender_version": "4.2.1",
        "ifcopenshell_available": True,
        "ifc_loaded": True,
        "requests_served": 12,
    }

    def test_reachable(self):
        import json

        from bonsai_mcp import __version__

        info = dict(self._PING)
        info["addon_version"] = __version__
        client = FakeBlenderBridgeClient(responses={"ping": info})
        buf = io.StringIO()
        assert run_doctor(client, stream=buf, as_json=True) == 0
        report = json.loads(buf.getvalue())
        assert report["reachable"] is True
        assert report["version_skew"] is False
        assert report["bridge"]["requests_served"] == 12
        assert report["server_version"] == __version__

    def test_unreachable(self):
        import json

        client = FakeBlenderBridgeClient(
            responses={"ping": BlenderBridgeError("Cannot reach Blender bridge")}
        )
        buf = io.StringIO()
        assert run_doctor(client, stream=buf, as_json=True) == 1
        report = json.loads(buf.getvalue())
        assert report["reachable"] is False
        assert "Cannot reach" in report["error"]

    def test_version_skew_flagged(self):
        import json

        info = dict(self._PING)
        info["addon_version"] = "0.0.9"
        client = FakeBlenderBridgeClient(responses={"ping": info})
        buf = io.StringIO()
        run_doctor(client, stream=buf, as_json=True)
        report = json.loads(buf.getvalue())
        assert report["version_skew"] is True

    def test_json_flag_parses(self):
        parser = build_parser()
        args = parser.parse_args(["doctor", "--json"])
        assert args.json is True


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_cli_help_does_not_exit_with_error(flag, capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args([flag])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "bonsai-mcp" in captured.out


def test_cli_version_flag(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "bonsai-mcp" in captured.out
