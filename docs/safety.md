# Safety

This server is designed for **local trusted use** on a single workstation.
Read this page before exposing the bridge to anything outside your own
machine. Short version: **don't**.

## The bridge is a Python REPL on your computer

`execute_blender_code` runs arbitrary Python inside Blender's interpreter,
with full `bpy` access. That means anything that connects to the bridge can:

- read and write any file your Blender process can read and write,
- run shell commands via `subprocess` or `os.system`,
- modify or destroy your Blender scene and any loaded IFC project.

By default the bridge has **no authentication**. The primary protection is
that it binds to `127.0.0.1`, so only processes on your local machine can
connect. An optional shared-secret token (below) hardens shared machines,
but does not change the loopback-only stance.

### `execute_ifc_code` is not a safe subset

`execute_ifc_code` rejects obvious `bpy` use with a best-effort regex, but that
check is a convenience rail, not a security boundary. It is bypassable (for
example via `__import__`, or through the injected `tool` / `ifc_api` objects,
which use `bpy` internally), so treat `execute_ifc_code` as carrying the same
trust level as `execute_blender_code`.

## Rules

1. **Do not change the bind address.** Leave it as `127.0.0.1`. Do not bind
   to `0.0.0.0`, do not put the port behind a reverse proxy, do not expose
   it via Docker port mapping to the host's external interface.
2. **Stop the bridge when you're done.** The add-on panel has a Stop button.
3. **Do not run the bridge on a shared machine.** Anything that can run
   Python on your machine can also issue commands to the bridge.
4. **Treat `save_ifc_file` carefully.** Called without `output_path` it
   overwrites the project's own file (that is its purpose, like File >
   Save). For save-as targets it refuses to overwrite by default;
   keep it that way. `output_path` is an unconfined absolute path: the file is
   written wherever the Blender process can write, and `overwrite=true` will
   clobber whatever is already there, including non-IFC files. Use a separate
   output path during automated work.
5. **Inspect generated code before running it** if your MCP client is acting
   on prompts from untrusted sources (web content, third-party documents,
   and so on).

## Read-only mode

The add-on has an **Allow edits** toggle (sidebar panel and add-on
preferences, on by default). When it is off, the bridge rejects the three
EDIT commands (`execute_ifc_code`, `execute_blender_code`, `save_ifc_file`)
with a clear error before they run, while QUERY tools and screenshots keep
working.

This is a guard against *unwanted tool use*, not a sandbox: it blocks code
execution entirely rather than trying to prove that a given script is
read-only. It is enforced in one place (the command dispatcher on Blender's
main thread), so a client cannot bypass it by crafting requests.

## Optional shared-secret token

On a single-user machine, loopback-only is the documented trust model. On a
shared machine, loopback is not a user boundary: any local process of any
user can connect to `127.0.0.1:9878`.

For that case the add-on preferences have a **Token** field. When set,
every bridge request must carry the same token or it is rejected before
anything runs (constant-time comparison). On the client side, set
`BONSAI_MCP_TOKEN` to the same value in the MCP server's environment.

The token is snapshotted when the bridge starts, so changing the
preference requires a bridge restart to take effect. `bonsai-mcp doctor`
reports whether the bridge requires a token. Off by default.

## What the server tries to do safely

- Binds to loopback only by default.
- Refuses to overwrite IFC files unless `overwrite=true`.
- Catches exceptions in handlers and returns them as JSON rather than letting
  Blender crash.
- Caps the per-tick command processing inside Blender so the UI stays
  responsive even if many requests queue up.
- Cancels queued requests when the client disconnects or times out, so a
  command the client was told failed does not silently run later and mutate
  the scene anyway.
- Optional read-only mode (see above).

## What it does **not** do

- No sandboxing of `execute_blender_code` (an out-of-process worker for
  `execute_ifc_code` is planned for a future release).
- No rate limiting.
- No authentication unless the optional token is configured.
