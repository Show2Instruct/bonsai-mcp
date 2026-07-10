# Bonsai MCP

A local Model Context Protocol server that bridges AI clients (Claude
Desktop, Claude Code, Cursor, VS Code, and any MCP-compatible tool) to
a running **Blender + Bonsai** (BlenderBIM) session.

Bonsai MCP exposes a focused, well-defined tool surface for inspecting
the active Blender scene, querying the loaded IFC project, and running
Python inside Blender. It is **local-first**: nothing listens on a
non-loopback interface.

## At a glance

- 8 tools, split into 5 QUERY (read-only) and 3 EDIT (mutating)
  operations. The category appears as a `[QUERY]` or `[EDIT]` prefix
  in every tool description and via MCP's `Tool.annotations.readOnlyHint`.
- Two moving parts: a Python MCP server (this package) plus a Blender
  add-on that runs inside Blender.
- Talks over `127.0.0.1` only, with a length-prefixed JSON protocol.
- Built directly on the official `mcp` Python SDK and Pydantic, with no
  bespoke framework dependency.

## Where to next

- [Installation](installation.md)
- [Client setup](clients.md)
- [Tools reference](tools.md)
- [Safety](safety.md)
- [Troubleshooting](troubleshooting.md)
