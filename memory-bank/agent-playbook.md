# Server Agent Playbook

- Read `app.py`, `server_paths.py`, and the relevant `db/` or feature module before editing.
- Preserve the auth middleware contract unless the request explicitly changes it.
- Keep API and MCP behavior aligned when changing a route.
- Treat startup/bootstrap code as production code; verify idempotence.
- Avoid hardcoding host paths or ports when the repo already exposes environment variables.
- If you touch database behavior, verify both schema creation and read/write paths.

## Safe change order

1. Locate the request path and auth boundary.
2. Update the feature module and backing DB code together.
3. Confirm startup/bootstrap still succeeds.
4. Run the targeted tests and the broader suite when behavior crosses modules.
