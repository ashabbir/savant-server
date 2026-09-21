# Parallel MCP Streamable HTTP Transport

## Goal

Keep the five existing SSE MCP endpoints stable while adding Streamable HTTP
endpoints for the same tool surfaces. Ship the change as Savant Server 16.0.0.

## Compatibility contract

| Server | SSE (unchanged) | Streamable HTTP (new) |
| --- | --- | --- |
| workspace | `:8091/sse` | `:8191/mcp` |
| abilities | `:8092/sse` | `:8192/mcp` |
| context | `:8093/sse` | `:8193/mcp` |
| knowledge | `:8094/sse` | `:8194/mcp` |
| reminders | `:8095/sse` | `:8195/mcp` |

Both listeners run the same server code and use the same MCP authentication
capture. SSE remains enabled by default for existing long-running clients.

## Tasks

### Task 1: Add failing transport contract tests

Files: `tests/test_runtime_configuration.py`, `tests/test_runtime_health.py`,
`tests/test_mcp_sse_integration.py`, `tests/test_server_version.py`

What to do: Define the dual-port API contract, require all five server CLIs to
accept Streamable HTTP, and exercise Streamable HTTP tool discovery.

Verify: The new tests fail against the pre-change runtime.

### Task 2: Enable Streamable HTTP in all MCP servers

Files: `mcp/context_server.py`, `mcp/knowledge_server.py`

What to do: Add `streamable-http` to the allowed transports and keep the
current FastMCP invocation/auth behavior shared by both transports.

Verify: Each server can initialize and list tools over Streamable HTTP.

### Task 3: Launch and expose both listener sets

Files: `docker-entrypoint.sh`, `docker-compose.yml`, `routes/jobs_system.py`

What to do: Launch a supervised SSE process and a supervised Streamable HTTP
process for each server. Add independently overrideable Streamable ports,
publish them in Compose, and return both transport endpoints in MCP discovery
and health diagnostics.

Verify: SSE health stays green and Streamable HTTP health/tool discovery works
for all five services.

### Task 4: Update shipped client configs and documentation

Files: `mcp-config.json`, `mcp_servers.toml`, `README.md`, `CLAUDE.md`,
`.github/copilot-instructions.md`

What to do: Keep generated legacy SSE configs intact, document the modern
Streamable URLs and migration path, and remove statements that SSE is the only
server transport.

Verify: Documentation and config tests agree on all ten endpoints.

### Task 5: Release and verify production artifact

Files: `build-info.json`, `tests/test_server_version.py`

What to do: Bump 15.0.0 to major version 16.0.0, run focused and full tests,
build and publish `ashabbir/savant-server:16.0.0` and `:latest`, restart the
local Compose service from that image, verify health and both transports, then
commit and push `main` to GitHub.

Verify: Published image digest is reported, local `/health/ready` reports
16.0.0, all ten MCP listeners are reachable, and `git status` is clean after
the push.
