# CLI reference

The `bonsai-mcp` command is normally launched by your MCP client, but it
can also be run directly from the cloned repo:

```bash
uv run bonsai-mcp [subcommand] [options]
```

## `serve` (the default)

Starts the MCP stdio server. Running `bonsai-mcp` with no subcommand does
the same thing; MCP clients use this form.

The server reads its bridge connection settings from the environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `BONSAI_MCP_HOST` | `127.0.0.1` | Bridge host. |
| `BONSAI_MCP_PORT` | `9878` | Bridge port. |
| `BONSAI_MCP_TIMEOUT` | `30` | Per-request timeout in seconds. |
| `BONSAI_MCP_TOKEN` | (unset) | Shared secret, only if the add-on has a token configured. |

## `doctor`

Diagnoses the link to the Blender bridge: pings it, reports Blender and
IfcOpenShell status, warns on add-on/server version skew, and prints an
example client config.

```bash
uv run bonsai-mcp doctor
uv run bonsai-mcp doctor --json
```

| Option | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Bridge host to ping. |
| `--port` | `9878` | Bridge port to ping. |
| `--timeout` | `5.0` | Per-request timeout in seconds. |
| `--json` | off | Machine-readable output for scripting and CI. |

Exit codes: `0` when the bridge is reachable, `1` when it is not. The
`--json` report includes `reachable`, the full bridge `ping` payload, and
a `version_skew` flag.

## `--version`

```bash
uv run bonsai-mcp --version
```

Prints the server version. It should match the add-on version shown in
the Blender panel; `doctor` warns when they differ.
