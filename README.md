# Bonsai MCP

A local **Model Context Protocol** server that connects any MCP client
(Claude Desktop, Claude Code, Cursor, VS Code, OpenAI Codex) to a running
**Blender + Bonsai** (BlenderBIM) session. Inspect the scene, query the
loaded IFC project, capture the viewport, and run Python inside Blender.

```
MCP client  --stdio-->  bonsai-mcp  --127.0.0.1:9878-->  Blender add-on (bpy + Bonsai + IfcOpenShell)
```

## Requirements

- **Blender 3.6+** (4.x recommended).
- **Bonsai (BlenderBIM)**, only for the IFC tools. Install it from
  [bonsaibim.org](https://bonsaibim.org). 
- **uv**, one-time install below. Runs the server and handles Python 3.10+ for you.
- **git**, to clone this repo. The server runs from your local checkout;
  it is not published to PyPI.

## Setup (one-time)

Do these once. `uv` installs everything into the repo's own `.venv` the
first time you run it; nothing is installed globally, and nothing is
fetched from PyPI.

**1. Install uv:**

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Clone this repo** somewhere permanent, and note its full path; you
point your MCP client at it below:

```bash
git clone https://github.com/show2instruct/bonsai-mcp.git
```

## Quick start

### 1. Install the Blender add-on

1. In Blender: **Edit > Preferences > Add-ons**. On Blender 4.2+/5.x, open
   the **▾** menu (top-right) > **Install from Disk...**; on older versions
   use the **Install...** button.
2. Pick **`blender_addon/bonsai_bridge.py`** from the repo you cloned: that
   exact file, not `scripts/package_addon.py` or the `.zip`. Then tick
   **Bonsai MCP Bridge** to enable it.
3. Open the sidebar (press **N**) > **Bonsai MCP** tab > **Start Bridge**.
   It now listens on `127.0.0.1:9878`.

### 2. Connect your client

Point your client at the folder you cloned. Everywhere below, replace
`/path/to/bonsai-mcp` with that path. On Windows in JSON, either use forward
slashes or double every backslash (`C:\\Users\\you\\bonsai-mcp`).

- **Claude Code:**

  ```bash
  claude mcp add bonsai-mcp -- uv run --directory /path/to/bonsai-mcp bonsai-mcp
  ```
- **Claude Desktop, Cursor, VS Code**, and other clients: add this to the
  client's MCP config, then restart the client:

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

  On Windows, if the client can't find `uv`, use its full path as the
  `command` (e.g. `C:\\Users\\you\\.local\\bin\\uv.exe`).

`uv run` builds the project into the repo's `.venv` on first launch
(editable, so your local edits take effect on the next restart) and caches
it after that.

### 3. Verify

With Blender running and the bridge started, run this from inside the
cloned folder:

```bash
uv run bonsai-mcp doctor
```

`doctor` pings the bridge and reports Blender / IfcOpenShell status and the
tools it exposes.

Per-client config paths, the Windows `uv.exe` path note, and optional
`BONSAI_MCP_*` settings are in [`docs/clients.md`](docs/clients.md).
Example configs live in [`examples/`](./examples); set the `--directory`
path to your clone.

## Tools

Eight tools, each tagged `[QUERY]` (read-only) or `[EDIT]` (mutates state).

| Category | Tool                      | Purpose                                                     |
| -------- | ------------------------- | ----------------------------------------------------------- |
| QUERY    | `get_scene_info`          | Scene summary plus an optional filtered object list.        |
| QUERY    | `get_selected_objects`    | Per-object info for the current selection.                  |
| QUERY    | `get_psets`               | IFC property and quantity sets for one or many objects.     |
| QUERY    | `get_viewport_screenshot` | Capture the viewport (aim with `view`/`fit`), plus structured viewport state. |
| QUERY    | `get_ifc_project_info`    | Schema, counts, materials, classifications.                 |
| EDIT     | `execute_ifc_code`        | Run IfcOpenShell / Bonsai API code. `bpy` blocked.          |
| EDIT     | `execute_blender_code`    | Run arbitrary Python with full `bpy` access.                |
| EDIT     | `save_ifc_file`           | Save in place, save-as (guarded), and optional viewport reload. |

Full reference with inputs, outputs, and examples: [`docs/tools.md`](docs/tools.md).

Example prompt, with an IFC project open in Bonsai:

> "List every wall in the model with its fire rating."

## Safety

The bridge binds to `127.0.0.1` only and has no authentication, and
`execute_blender_code` runs arbitrary Python. Treat it like an open Python
REPL on your machine and never expose it to a network. See
[`docs/safety.md`](docs/safety.md).

## Documentation

- [Installation](docs/installation.md)
- [Client setup](docs/clients.md) (Claude, Cursor, VS Code, OpenAI)
- [Tools reference](docs/tools.md)
- [Safety](docs/safety.md)
- [Troubleshooting](docs/troubleshooting.md)

## Contributing

Python 3.10+ and `uv`:

```bash
uv venv
uv pip install -e ".[dev]"
uv run ruff check src tests blender_addon scripts
uv run pytest
```

Add tests and docs for behavior changes. Report security issues privately
through [GitHub Security Advisories](https://github.com/show2instruct/bonsai-mcp/security/advisories/new).

## License

MIT. See [LICENSE](LICENSE).
