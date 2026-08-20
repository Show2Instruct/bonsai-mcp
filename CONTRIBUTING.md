# Contributing

Thanks for helping improve Bonsai MCP.

## Setup

Python 3.10+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/Show2Instruct/bonsai-mcp.git
cd bonsai-mcp
uv venv
uv pip install -e ".[dev]"
```

## Checks

Both must pass before opening a pull request:

```bash
uv run python -m ruff check src tests blender_addon scripts
uv run python -m pytest
```

(The `python -m` form matters: a bare `uv run pytest` can silently pick
up a system-wide pytest.)

The test suite runs without Blender: the add-on is imported against a
faked `bpy` (see `tests/test_addon_bridge.py`). Changes to the add-on's
Blender-facing behavior (panel, operators, viewport capture) should also
be smoke-tested inside a real Blender with Bonsai; note in the PR what
you tested live.

## Expectations

- Add tests and docs for behavior changes.
- Use clear pull request titles and descriptions; GitHub uses them to generate
  release notes automatically.
- The wire protocol between `src/bonsai_mcp/blender_client.py` and
  `blender_addon/bonsai_bridge.py` is deliberately duplicated (the add-on
  is a standalone file inside Blender's Python); parity tests keep the
  two in sync, so change both sides together.
- Do not add new dependencies without discussion; the server deliberately
  uses only the official `mcp` SDK and Pydantic.

## Security issues

Report privately via
[GitHub Security Advisories](https://github.com/Show2Instruct/bonsai-mcp/security/advisories/new),
not as a public issue. See [SECURITY.md](SECURITY.md).
