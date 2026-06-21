# Savant Server Memory Bank

This directory captures the server-side runtime contract for Savant's Flask API, MCP bridges, and backing data stores.

## Read order

1. `architecture.md` - service boundaries, API layers, and persistence model.
2. `runtime.md` - local startup, Docker, environment variables, and bootstrap behavior.
3. `testing-and-quality.md` - the checks that matter when changing server behavior.
4. `agent-playbook.md` - safe update rules for future agents.

## Current state

- Flask entry point is `app.py`.
- The server exposes REST APIs plus multiple MCP blueprints.
- Persistence is PostgreSQL-backed in the current setup path.
- Default users are seeded idempotently on startup.
- API auth uses `X-API-Key` and is required for normal `/api/*` routes.

Update these notes when API contracts, auth, persistence, or service boot flow changes.
