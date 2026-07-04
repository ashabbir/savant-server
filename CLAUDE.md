# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Canonical reference

`.github/copilot-instructions.md` contains the full architecture, conventions, and rules. When in doubt, follow that file.

## What this app is

**Savant Server** is a Flask REST API + MCP backend for the Savant developer AI assistant system. It manages workspaces, tasks, code context (semantic search), a knowledge graph, reminders, and prompt abilities — all exposed via both REST and five MCP servers (SSE bridges on ports 8091–8095).

## Quick start

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python app.py              # Flask on http://127.0.0.1:8090
pytest tests/ -v                     # run tests
docker compose up -d --build         # Docker deployment (Postgres + server)
```

Default users seeded on first startup:
| user_id | role  | api_key               |
|---------|-------|-----------------------|
| ahmed   | admin | sk-ahmed-savant-001   |
| lex     | user  | sk-lex-savant-001     |

Auth header: `X-API-Key: sk-ahmed-savant-001`

## Architecture

```
app.py                  Flask app + all routes (main entry point)
models.py               Pydantic v2 data models
postgres_client.py      PostgreSQL connection pool
sqlite_client.py        Legacy SQLite fallback (local dev)
server_paths.py         Path resolution (Docker ↔ host)
hardening.py            Rate limiting, sanitization, validation

db/                     Database layer — static-method classes only
  workspaces.py         WorkspaceDB
  tasks.py              TaskDB (with dependency graph)
  notes.py              NoteDB
  merge_requests.py     MergeRequestDB
  jira_tickets.py       JiraTicketDB
  reminders.py          ReminderDB
  knowledge_graph.py    KnowledgeGraphDB (nodes + edges)
  users.py              UserDB (API key auth)
  workspace_session_links.py  Session ↔ workspace mapping

mcp/                    MCP servers (SSE, never touch DB directly)
  server.py             savant-workspace  port 8091
  abilities_server.py   savant-abilities  port 8092
  context_server.py     savant-context    port 8093
  knowledge_server.py   savant-knowledge  port 8094
  reminders_server.py   savant-reminders  port 8095

abilities/              Prompt asset system (personas, rules, policies)
context/                Code indexing + semantic search (pgvector, tree-sitter)
knowledge/              Knowledge graph routes
reminders/              Reminder routes
tools/                  Tool registry + KG integration
graphify/               Graph visualization
utils/                  auth.py (admin_required decorator)
```

## MCP servers

| Server             | Port | File                      | Tool count | Manages                                      |
|--------------------|------|---------------------------|------------|----------------------------------------------|
| savant-workspace   | 8091 | mcp/server.py             | 32         | workspaces, tasks, notes, MRs, Jira tickets  |
| savant-abilities   | 8092 | mcp/abilities_server.py   | 12         | prompt assets (personas, rules, policies)    |
| savant-context     | 8093 | mcp/context_server.py     | 11         | code search, AST, memory bank, repo index    |
| savant-knowledge   | 8094 | mcp/knowledge_server.py   | 16         | KG nodes, edges, staging, search             |
| savant-reminders   | 8095 | mcp/reminders_server.py   | 9          | personal reminders                           |

Config: `mcp_servers.toml` / `mcp-config.json`

## Key rules

- **Pydantic v2:** Use `ConfigDict` not `class Config`. Use `model_dump()` not `.dict()`.
- **DB layer:** All DB access through static-method classes in `db/`. Timestamps are ISO 8601 UTC strings. Use `get_connection()` from `postgres_client.py` (not sqlite_client for new code).
- **MCP servers:** Thin SSE bridges only — proxy to Flask REST endpoints, never touch DB directly.
- **No client imports:** Server communicates with savant-client over HTTP/SSE only.
- **TDD:** `pytest` with `pytest-cov`. Write failing test first, then implement, then refactor.
- **Testing:** `pytest tests/ -v`. File naming: `tests/test_<module>.py`.
- **Blueprints:** Each feature module (`abilities/`, `context/`, `knowledge/`, `reminders/`, `tools/`, `graphify/`) has its own `routes.py` registered as a Flask blueprint.
- **Knowledge graph staging:** Nodes created via `store()` are staged; call `commit_workspace(workspace_id)` to publish.
- **Abilities assets:** Markdown files with YAML frontmatter in `<data>/abilities/{personas,rules,policies,styles,repos}/`. Use `resolver.py` to compose prompts.
- **Semantic search:** `context/embeddings.py` wraps stsb-distilbert (768-dim). Embeddings stored in pgvector. Use `ContextDB` for search.

## Adding a new feature

1. Create `db/<feature>.py` with a static-method class.
2. Create `<feature>/routes.py` with a Flask blueprint.
3. Register the blueprint in `app.py`.
4. Add MCP tool in the relevant `mcp/*.py` if the feature should be exposed via MCP.
5. Write tests in `tests/test_<feature>.py` — failing test first.

## Docker environment variables

| Variable              | Default                        | Purpose                          |
|-----------------------|--------------------------------|----------------------------------|
| SAVANT_DATABASE_URL   | (required)                     | PostgreSQL connection string     |
| SAVANT_API_ONLY       | 1                              | Disable non-API routes           |
| SAVANT_SERVER_DATA_DIR| /data/savant                   | Persistent data root             |
| BASE_CODE_DIR         | /base-code                     | Source code root for indexing    |
| RUNNING_IN_DOCKER     | 1                              | Triggers Docker path mapping     |
| GUNICORN_WORKERS      | 2                              | Gunicorn worker count            |
| GUNICORN_THREADS      | 4                              | Gunicorn thread count            |
