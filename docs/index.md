# Bonsai MCP

A local Model Context Protocol server that bridges AI clients (Claude
Desktop, Claude Code, Cursor, VS Code, and any MCP-compatible tool) to
a running **Blender + Bonsai** (BlenderBIM) session.

This documentation applies to **v1.2.x**. The changelog can be found on the
[GitHub Releases page](https://github.com/Show2Instruct/bonsai-mcp/releases).

Bonsai MCP exposes a focused, well-defined tool surface for inspecting
the active Blender scene, querying the loaded IFC project, and running
Python inside Blender. It is **local-first**: nothing listens on a
non-loopback interface.

## At a glance

- 14 tools, split into 8 QUERY (read-only) and 6 EDIT (mutating)
  operations. The category appears as a `[QUERY]` or `[EDIT]` prefix
  in every tool description and via MCP's `Tool.annotations` hints.
- BIM-native queries with no code execution needed: spatial hierarchy,
  quantity takeoff, inheritance-aware element listing, psets.
- Structured tool output, MCP resources, and workflow prompts on top of
  the plain-text results.
- Two moving parts: a Python MCP server (this package) plus a Blender
  add-on that runs inside Blender.
- Talks over `127.0.0.1` only, with a length-prefixed JSON protocol on a
  persistent connection; optional shared-secret token for shared machines.
- Built directly on the official `mcp` Python SDK and Pydantic, with no
  bespoke framework dependency.

## Where to next

- [Installation](installation.md)
- [Client setup](clients.md)
- [Tools reference](tools.md)
- [Example prompts](examples.md)
- [CLI reference](cli.md)
- [Safety](safety.md)
- [Troubleshooting](troubleshooting.md)
