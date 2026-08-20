# Security policy

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/Show2Instruct/bonsai-mcp/security/advisories/new),
not as a public issue.

## Scope and threat model

Bonsai MCP is a local development tool. The bridge binds to `127.0.0.1`
only, and `execute_blender_code` runs arbitrary Python inside Blender by
design; treat the bridge like an open Python REPL on your machine. The
documented trust model, the read-only mode, and the optional shared-secret
token are described in [docs/safety.md](docs/safety.md). Reports that the
bridge executes code when asked to are expected behavior; reports that it
can be reached from outside the machine, bypasses the read-only mode, or
leaks the token are security bugs.
