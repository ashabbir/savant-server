# Server Architecture

## Purpose

`savant-server` is the shared backend for workspaces, tasks, notes, knowledge, reminders, abilities, tools, and MCP access.

## Main layers

- `app.py` creates the Flask app and registers feature blueprints.
- `db/` contains the data access layer.
- `abilities/`, `context/`, `knowledge/`, and `reminders/` expose domain routes.
- `code_intelligence/` exposes CodeGraph-backed structural source analysis through the private `codegraph_bridge/`.
- `mcp/` contains the MCP server and related bridge code.
- `server_paths.py` resolves host/container data paths.

## Runtime behavior

- The app enables CORS for local/API consumers and explicitly allows `X-API-Key`.
- `before_request` resolves the API key to `g.user_id` for authenticated requests.
- Health and system paths are exempt from normal auth where needed.
- Default users are seeded on startup in an idempotent way.

## Data model

- The current production path uses PostgreSQL through `postgres_client.py`.
- Persistent files still exist under `data/` for assets and compatibility data.
- Workspace/session linkage is handled separately from the primary auth model.

## Important contracts

- `GET /api/auth/validate` is the canonical login validation route.
- MCP traffic is exposed through `/api/mcp/*` style routes and related servers.
- Tooling and knowledge writes should preserve the authenticated user context.
