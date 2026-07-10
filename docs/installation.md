# Installation

Four steps: install `uv`, clone this repo, install the Blender add-on,
point your MCP client at `uv run --directory /path/to/bonsai-mcp bonsai-mcp`.
The server runs from your local clone; it is not published to PyPI.

## 1. Install `uv`

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Python 3.10+ is required; `uv` handles that for you.

## 2. Clone the repo

```bash
git clone https://github.com/show2instruct/bonsai-mcp.git
```

Note the full path to the folder. `uv run --directory <that path> bonsai-mcp`
builds the project into the repo's own `.venv` the first time your MCP
client launches it (editable, so local edits take effect on the next
restart) and caches it after that. No separate `pip install` is needed.

## 3. Blender add-on

This add-on is the bridge, not Bonsai itself. The IFC tools (`get_psets`,
`get_ifc_project_info`, `execute_ifc_code`, `save_ifc_file`) need **Bonsai
(BlenderBIM)** and IfcOpenShell installed in the same Blender; install Bonsai
from [bonsaibim.org](https://bonsaibim.org) first if you want them. The scene,
screenshot, and `execute_blender_code` tools work on stock Blender without it.

1. Open Blender 3.6+ (4.x / 5.x recommended).
2. Edit > Preferences > Add-ons. On Blender 4.2+/5.x, open the **▾** menu
   (top-right) > **Install from Disk...**; on older versions use the
   **Install...** button.
3. Pick `blender_addon/bonsai_bridge.py` from the repo you cloned: that
   exact file, not `scripts/package_addon.py` or a `.zip`.
4. Enable **Bonsai MCP Bridge**.
5. Sidebar (`N`) > **Bonsai MCP** tab > **Start Bridge**.

Default bind: `127.0.0.1:9878`. The port is deliberately different from
the `9876` used by other Blender MCP bridges so they can coexist; see
[Troubleshooting](troubleshooting.md#port-conflicts-with-other-blender-mcp-bridges).
Leave the address alone unless you have a strong reason; see
[Safety](safety.md).

## 4. Verify

From inside the cloned folder:

```bash
uv run bonsai-mcp doctor
```

Pings the bridge, reports Blender / IfcOpenShell status, and prints the
add-on version. See
[Troubleshooting](troubleshooting.md#using-doctor-to-diagnose-the-bridge).

## 5. Wire it to your MCP client

See [Clients](clients.md).
