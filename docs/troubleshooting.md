# Troubleshooting

## Using `doctor` to diagnose the bridge

`uv run bonsai-mcp doctor` (from inside your clone) is the fastest way to
find out whether the problem is the bridge, the client config, or the
network. It does not speak MCP, so you can run it from a normal terminal:

```bash
uv run bonsai-mcp doctor
# or override the target:
uv run bonsai-mcp doctor --host 127.0.0.1 --port 9878 --timeout 5
```

It prints:

- the `bonsai-mcp` version and your Python interpreter,
- the list of MCP tools this build registers,
- whether the bridge is reachable,
- the Blender version, the running add-on version (with a warning on
  version skew vs the installed package), whether IfcOpenShell is
  importable, whether an IFC project is loaded,
- an example client config snippet (use the `uv run --directory` form from
  [Clients](clients.md)).

Exit code `0` means the bridge is healthy, `1` means it is unreachable.
A version mismatch is a warning, not a failure: `doctor` still exits
`0` so it works in scripts.

For scripting and CI, `doctor --json` prints a machine-readable report
(`reachable`, `bridge` info, `version_skew`, `error`) with the same exit
codes.

## "Cannot reach Blender bridge at 127.0.0.1:9878"

The MCP server keeps one connection open to the bridge (reconnecting
transparently when it goes stale) and this connection attempt was
refused. Check, in order:

1. Blender is running.
2. The **Bonsai MCP Bridge** add-on is enabled.
3. You clicked **Start Bridge** in the 3D Viewport sidebar
   (`N`, then the Bonsai MCP tab).
4. The port matches (`BONSAI_MCP_PORT` and the add-on preference are
   both `9878` by default).

## Port conflicts with other Blender MCP bridges

Several Blender MCP projects (blender-mcp and its forks) default to port
`9876`. Bonsai MCP defaults to `9878` to stay out of their way, so both
kinds of bridge can run in the same Blender at the same time.

Signs that something else is using the port:

- `doctor` warns that the responding service did not identify itself as
  the Bonsai MCP bridge.
- Tool calls report a "non-JSON reply", a reply that "is not a Bonsai MCP
  bridge response", or time out on every call even though something is
  listening.
- **Start Bridge** fails with "port may be in use".

Fix it either way:

- Stop the other application, or
- Move Bonsai MCP to a free port: change the port in the add-on
  preferences (Edit > Preferences > Add-ons > Bonsai MCP Bridge), restart
  the bridge, and set `BONSAI_MCP_PORT` to the same value in your MCP
  client config.

## The MCP server starts but no tools appear in the client

- Restart the client after editing its MCP config.
- Confirm `uv` is on `PATH`. Many clients launch the command from a
  non-shell environment, so using its absolute path as the `command`
  (e.g. `/Users/you/.local/bin/uv`, or `C:\Users\you\.local\bin\uv.exe`
  on Windows) often helps.
- Watch the client's MCP log for stderr from the server.

## The add-on does not appear in Blender's Add-ons list

You probably installed the wrong file. Install
`blender_addon/bonsai_bridge.py`, not `scripts/package_addon.py`, which is
a build helper with no `bl_info` (Blender warns "add-on missing 'bl_info'"
and hides it). Remove the wrong entry, then install the correct file via
Edit > Preferences > Add-ons > (**▾** menu) **Install from Disk...**.

## Viewport screenshot fails

`bpy.ops.render.opengl` requires an active 3D viewport. If Blender is
in `--background` mode or no `VIEW_3D` area is visible, the operator
errors out. Open a normal Blender window with a 3D viewport visible
and retry.

## The screenshot runs but the model says it cannot see the image

Some MCP clients (notably claude.ai / Claude Desktop in some
configurations) do not deliver tool-result image blocks to the model,
even though the block is valid; the model sees only a placeholder. The
tool's text output reports how many base64 characters were attached, so
you can confirm the server sent the image. Work around it with
`include_objects=true`: the tool then returns screen-space bounding boxes
keyed by GlobalId as text, which lets the model verify framing and reason
spatially without the pixels. See the
[tools reference](tools.md#get_viewport_screenshot-query).

## `doctor` shows "ifcopenshell   missing"

Bonsai (BlenderBIM) / IfcOpenShell is not installed in the Blender the
bridge is running in. The scene, screenshot, and `execute_blender_code`
tools still work, but every IFC tool (`get_psets`, `get_ifc_project_info`,
`execute_ifc_code`, `save_ifc_file`) will fail. Install the Bonsai
extension from [bonsaibim.org](https://bonsaibim.org), restart Blender,
and start the bridge again.

## `get_ifc_project_info` returns "No IFC project appears to be loaded"

You probably haven't opened an IFC file in Bonsai yet. Use Bonsai's
normal load flow (File > Open IFC project) and try again.

## `execute_blender_code` returns `success: false`

The `error` and `traceback` fields contain the actual Python exception.
Treat them like any other Python traceback.

## The bridge hangs

The bridge dispatches work on Blender's main thread via a timer. If
Blender's UI itself is frozen (e.g. a long modal dialog, a slow
operator), the timer won't tick and requests will queue. Bring
Blender's UI back to a responsive state and the queue will drain.

If a request waits too long, the bridge cancels it and replies with a
timeout error; a request whose client disconnected before it ran is
cancelled and never executed.

## "Unknown command" errors after updating

The server and the add-on ship together but are deployed separately. If a
tool fails with an "Unknown command" error (the message includes a
redeploy hint), the add-on inside Blender predates the server: copy the
current `blender_addon/bonsai_bridge.py` over the installed one (or
reinstall the add-on zip), then restart the bridge. `doctor` warns about
version skew explicitly.

## "Invalid or missing bridge token"

The add-on has a token configured in its preferences and the client did
not send the matching secret. Set `BONSAI_MCP_TOKEN` in the MCP server's
environment (see [Clients](clients.md)) to the same value. Token changes
in the add-on preferences apply on the next bridge start.

## Writing your own client for the bridge port

The port speaks length-prefixed JSON (4-byte big-endian length + UTF-8
JSON object). A request may carry an optional integer `id`, which the
bridge echoes back in the response so pipelined clients can correlate
replies (and an optional `token` when the add-on requires one). Two rules
for custom clients:

- Keep the connection open until you have read the reply. The bridge
  treats end-of-stream from the client as "the client is gone" and
  cancels the request if it has not started yet (a half-close via
  `shutdown(SHUT_WR)` after sending is tolerated: an in-flight result is
  still delivered, but a request that is still queued when the
  half-close is noticed gets a cancellation error instead of a result).
- Malformed input (an oversized frame, a non-JSON body) gets one framed
  error reply, then the bridge closes the connection.

## Modifying behaviour

The handlers live in `blender_addon/bonsai_bridge.py`. Add your own
command and a matching entry in `_HANDLERS`; re-enable the add-on to
pick up the change.
