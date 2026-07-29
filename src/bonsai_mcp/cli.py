"""Command-line interface for Bonsai MCP."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import TextIO

from bonsai_mcp import __version__
from bonsai_mcp.blender_client import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    BlenderBridgeClient,
    BlenderBridgeError,
)
from bonsai_mcp.tools import ALL_TOOL_NAMES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bonsai-mcp",
        description=(
            "MCP server bridging AI clients to a running Blender + Bonsai session. "
            "With no subcommand, starts the MCP stdio server."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"bonsai-mcp {__version__}",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser(
        "serve",
        help="Start the MCP stdio server (the default).",
    )
    serve.set_defaults(func=_cmd_serve)

    doctor = sub.add_parser(
        "doctor",
        help="Diagnose the link to the Blender bridge.",
    )
    doctor.add_argument("--host", default=None, help=f"Bridge host (default: {DEFAULT_HOST}).")
    doctor.add_argument(
        "--port", type=int, default=None, help=f"Bridge port (default: {DEFAULT_PORT})."
    )
    doctor.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-request timeout in seconds (default: 5.0).",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help=(
            "Machine-readable output for scripting/CI. Exit codes: 0 = bridge "
            "reachable, 1 = unreachable."
        ),
    )
    doctor.set_defaults(func=_cmd_doctor)

    return parser


def _cmd_serve(_args: argparse.Namespace) -> int:
    from bonsai_mcp.server import run_stdio_server

    run_stdio_server()
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    client = BlenderBridgeClient(host=args.host, port=args.port, timeout=args.timeout)
    return run_doctor(client, stream=sys.stdout, as_json=getattr(args, "json", False))


def run_doctor(client: BlenderBridgeClient, stream: TextIO, as_json: bool = False) -> int:
    """Run diagnostics. Returns 0 on success, 1 if the bridge is unreachable."""
    if as_json:
        return _run_doctor_json(client, stream)

    def write(line: str) -> None:
        stream.write(line + "\n")

    write(f"bonsai-mcp {__version__}")
    write(f"python           {platform.python_version()} ({platform.platform()})")
    write(f"bridge target    {client.host}:{client.port}  (timeout {client.timeout}s)")
    write(f"mcp tools ({len(ALL_TOOL_NAMES)})  {', '.join(ALL_TOOL_NAMES)}")
    write("")

    try:
        info = client.ping()
    except BlenderBridgeError as exc:
        write("bridge           UNREACHABLE")
        write(f"  reason: {exc}")
        write("")
        write("Fixes, in order:")
        write("  1. Start Blender.")
        write("  2. Enable the 'Bonsai MCP Bridge' add-on.")
        write("  3. Open the 3D viewport sidebar (N) -> 'Bonsai MCP' tab -> Start Bridge.")
        write("  4. Confirm host/port match (BONSAI_MCP_HOST / BONSAI_MCP_PORT).")
        return 1

    write("bridge           OK")
    service = info.get("service")
    if service is not None and service != "bonsai-mcp-bridge":
        write(
            f"  WARNING: the service on {client.host}:{client.port} identifies as "
            f"{service!r}, not the Bonsai MCP bridge. Another Blender MCP bridge "
            "may be using this port."
        )
    blender_version = info.get("blender_version", "?")
    write(f"  blender        {blender_version}")
    addon_version = info.get("addon_version")
    write(f"  addon version  {addon_version if addon_version else 'unknown'}")
    if addon_version and addon_version != __version__:
        write(
            f"  WARNING: addon version {addon_version} != server version {__version__}. "
            "Re-install the matching blender_addon/bonsai_bridge.py."
        )
    write(f"  ifcopenshell   {'available' if info.get('ifcopenshell_available') else 'missing'}")
    write(f"  ifc loaded     {'yes' if info.get('ifc_loaded') else 'no'}")
    edits_allowed = info.get("edits_allowed")
    if edits_allowed is not None:
        write(f"  edits allowed  {'yes' if edits_allowed else 'no (read-only mode)'}")
    requests_served = info.get("requests_served")
    if requests_served is not None:
        write(f"  requests served {requests_served}")
    token_required = info.get("token_required")
    if token_required:
        write("  token          required (client must set BONSAI_MCP_TOKEN)")

    known = {
        "blender_version",
        "addon_version",
        "ifcopenshell_available",
        "ifc_loaded",
        "status",
        "service",
        "edits_allowed",
        "requests_served",
        "token_required",
    }
    extras = {k: v for k, v in info.items() if k not in known}
    if extras:
        write(f"  extras         {json.dumps(extras, default=str)}")

    write("")
    write("Example MCP client config:")
    write(_example_config_snippet(client))
    return 0


def _run_doctor_json(client: BlenderBridgeClient, stream: TextIO) -> int:
    """Machine-readable doctor output; same exit codes as the human form."""
    report: dict = {
        "server_version": __version__,
        "bridge_target": f"{client.host}:{client.port}",
        "timeout_seconds": client.timeout,
        "tools": list(ALL_TOOL_NAMES),
        "reachable": False,
        "bridge": None,
        "error": None,
        "version_skew": False,
    }
    exit_code = 1
    try:
        info = client.ping()
    except BlenderBridgeError as exc:
        report["error"] = str(exc)
    else:
        report["reachable"] = True
        report["bridge"] = info
        addon_version = info.get("addon_version")
        report["version_skew"] = bool(addon_version and addon_version != __version__)
        exit_code = 0
    stream.write(json.dumps(report, indent=2, default=str) + "\n")
    return exit_code


def _example_config_snippet(client: BlenderBridgeClient) -> str:
    project_dir = Path(__file__).resolve().parents[2]
    snippet = {
        "mcpServers": {
            "bonsai-mcp": {
                "command": "uv",
                "args": ["run", "--directory", str(project_dir), "bonsai-mcp"],
                "env": {
                    "BONSAI_MCP_HOST": client.host,
                    "BONSAI_MCP_PORT": str(client.port),
                    "BONSAI_MCP_TIMEOUT": f"{int(client.timeout)}",
                },
            }
        }
    }
    return json.dumps(snippet, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        return _cmd_serve(args)
    return func(args)
