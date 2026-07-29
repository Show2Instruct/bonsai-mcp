# Client setup

Every MCP client launches the server as a stdio child process. This
project is not published to PyPI, so the launch command runs it straight
from your local clone:

```
uv run --directory /path/to/bonsai-mcp bonsai-mcp
```

Replace `/path/to/bonsai-mcp` everywhere below with the folder you cloned
(`git clone https://github.com/show2instruct/bonsai-mcp.git`). `uv` builds
the project into that repo's `.venv` on first launch (editable, so local
edits take effect on the next restart) and caches it after that. On Windows
in JSON, use forward slashes or double every backslash
(`C:\\Users\\you\\bonsai-mcp`).

All `BONSAI_MCP_*` environment variables are optional; the defaults are
`127.0.0.1`, `9878`, and `30` (seconds). The examples below show them only for
clarity, and you can drop the `env` block entirely.

## Claude Desktop

Add to your Claude Desktop config (path depends on OS; see the Claude
Desktop docs):

```json
{
  "mcpServers": {
    "bonsai-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bonsai-mcp", "bonsai-mcp"],
      "env": {
        "BONSAI_MCP_HOST": "127.0.0.1",
        "BONSAI_MCP_PORT": "9878",
        "BONSAI_MCP_TIMEOUT": "30"
      }
    }
  }
}
```

Restart Claude Desktop. The tools should appear in the tools panel.

## Cursor

```json
{
  "mcpServers": {
    "bonsai-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bonsai-mcp", "bonsai-mcp"]
    }
  }
}
```

**Windows note:** if the client cannot find `uv`, use its full path as the
`command` (e.g. `C:\\Users\\you\\.local\\bin\\uv.exe`).

## Claude Code

```bash
claude mcp add bonsai-mcp -- uv run --directory /path/to/bonsai-mcp bonsai-mcp
```

Or edit `~/.claude/mcp.json` with the Claude Desktop shape above.

## VS Code

VS Code MCP extensions take the same shape: `command` plus optional
`args` and `env`. Use the Claude Desktop example as a template.

## OpenAI clients

Any OpenAI tool that can launch stdio MCP servers works the same way.

**Codex CLI** (add to `~/.codex/config.toml`):

```toml
[mcp_servers.bonsai-mcp]
command = "uv"
args = ["run", "--directory", "/path/to/bonsai-mcp", "bonsai-mcp"]
```

**OpenAI Agents SDK** (Python):

```python
from agents.mcp import MCPServerStdio

params = {"command": "uv", "args": ["run", "--directory", "/path/to/bonsai-mcp", "bonsai-mcp"]}
async with MCPServerStdio(params=params) as bonsai:
    # pass mcp_servers=[bonsai] when constructing your Agent
    ...
```

## Other MCP clients

Bonsai MCP is a standard stdio MCP server, so any client that can spawn
`uv run --directory /path/to/bonsai-mcp bonsai-mcp` as a child process can
use it. There is no HTTP/SSE mode; the server is local by design.

## Run from an editable install instead

If you would rather have a plain `bonsai-mcp` command on your PATH, install
the clone into a virtual environment once:

```bash
cd /path/to/bonsai-mcp
uv venv
uv pip install -e .
```

Then point your client's `command` at the `bonsai-mcp` executable in that
venv (`.venv/bin/bonsai-mcp`, or `.venv\Scripts\bonsai-mcp.exe` on Windows)
with empty `args`.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BONSAI_MCP_HOST` | `127.0.0.1` | Bridge host the MCP server connects to. |
| `BONSAI_MCP_PORT` | `9878` | Bridge TCP port. |
| `BONSAI_MCP_TIMEOUT` | `30` | Seconds to wait for a single tool call. |
| `BONSAI_MCP_TOKEN` | (unset) | Shared secret, only needed when the add-on preferences have a token configured (see [Safety](safety.md)). |
