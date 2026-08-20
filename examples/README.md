# Example MCP client configs

Copy the relevant file, replace `/path/to/bonsai-mcp` with the full path
of your cloned repo, and place the content where your client expects it.
On Windows, either use forward slashes or double every backslash
(`C:\\Users\\you\\bonsai-mcp`), and if the client cannot find `uv`, use
the full path to `uv.exe` as the `command` (see `cursor_config.json`).

| Client | Where the config goes |
| --- | --- |
| Claude Desktop | `claude_desktop_config.json`: Windows `%APPDATA%\Claude\`, macOS `~/Library/Application Support/Claude/` |
| Claude Code | no file needed: `claude mcp add bonsai-mcp -- uv run --directory /path/to/bonsai-mcp bonsai-mcp` |
| Cursor | `.cursor/mcp.json` in your project, or `~/.cursor/mcp.json` globally |
| VS Code | `.vscode/mcp.json` in your workspace (use the `servers` key per VS Code docs) |

Merge the `mcpServers` entry into an existing config file rather than
replacing it; other server entries you already have should stay.

The `env` block is optional when you use the defaults
(`127.0.0.1:9878`, 30 s timeout). Full client walkthroughs:
[docs/clients.md](../docs/clients.md).
