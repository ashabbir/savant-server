# Copilot Instructions — Savant Server

## What This Is

Savant Server is a **Flask API + MCP backend** that provides centralized persistence, knowledge graph, context ingestion, and AI tool servers for the Savant ecosystem. It communicates with `savant-client` over HTTP/SSE — never import client code.

## Build & Run

```bash
# Local development
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python app.py                      # starts on http://127.0.0.1:8090

# Docker
docker compose up -d --build

# Tests
pytest tests/ -v
pytest tests/ -v --cov                       # with coverage
./run-tests.sh                               # creates venv if missing, then pytest
```

Entry point: `app.py` (Flask application factory + route registration).

## Architecture

### Core stack

- **Python 3.11+**, Flask, Gunicorn
- **SQLite** with WAL mode — singleton `SQLiteClient` in `sqlite_client.py`
- **Pydantic v2** for validation (`models.py`)
- **sentence-transformers** for semantic search embeddings
- **tree-sitter** for AST-based code analysis

### Module layout

```
app.py                    ← Flask entry point, blueprint registration
sqlite_client.py          ← SQLiteClient singleton (WAL mode)
hardening.py              ← rate_limit, validate_request, safe_limit, retry_with_backoff
models.py                 ← Pydantic v2 models

db/                       ← Data access layer (static-method classes)
├── workspace_db.py
├── task_db.py
├── note_db.py
├── merge_request_db.py
├── jira_ticket_db.py
├── notification_db.py
└── ...

abilities/                ← Abilities feature module + routes
context/                  ← Context/code-indexing feature module + routes
knowledge/                ← Knowledge graph feature module + routes
reminders/                ← Reminders feature module + routes

mcp/                      ← MCP server implementations (SSE bridges)
├── server.py             ← savant-workspace (port 8091)
├── abilities_server.py   ← savant-abilities (port 8092)
├── context_server.py     ← savant-context (port 8093)
├── knowledge_server.py   ← savant-knowledge (port 8094)
├── reminders_server.py   ← savant-reminders (port 8095)
└── session_detect.py     ← PID-based session detection

templates/                ← Jinja2 templates (server-rendered dashboard)
static/                   ← Static assets
```

### DB layer pattern

Each entity has a `db/<entity>.py` file with a class using `@staticmethod` methods. All methods call `get_connection()` from `sqlite_client.py`.

```python
from sqlite_client import get_connection

class ExampleDB:
    @staticmethod
    def get_by_id(item_id: str) -> dict | None:
        conn = get_connection()
        row = conn.execute("SELECT * FROM examples WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def create(item_id: str, name: str) -> dict:
        conn = get_connection()
        conn.execute("INSERT INTO examples (id, name) VALUES (?, ?)", (item_id, name))
        conn.commit()
        return {"id": item_id, "name": name}
```

**Rules:**
- Timestamps are **ISO 8601 UTC strings** (not unix epochs, not naive datetimes).
- Use `get_connection()` — never create your own sqlite3 connections.
- Follow the existing `@staticmethod` class pattern exactly.

### Flask blueprints

Each feature module exposes a Blueprint registered in `app.py`:

```python
from abilities.routes import abilities_bp
app.register_blueprint(abilities_bp)
```

Routes live under `/api/<feature>/*`. Every feature gets a `/api/<feature>/health` endpoint.

### MCP server pattern

MCP servers are **thin SSE bridges** that proxy tool calls to Flask REST endpoints. They never touch the DB or filesystem directly.

| MCP name | Port | Server file |
|----------|------|-------------|
| savant-workspace | 8091 | `mcp/server.py` |
| savant-abilities | 8092 | `mcp/abilities_server.py` |
| savant-context | 8093 | `mcp/context_server.py` |
| savant-knowledge | 8094 | `mcp/knowledge_server.py` |
| savant-reminders | 8095 | `mcp/reminders_server.py` |

Ports are **fixed** so AI tool configs never go stale. Configured via `SAVANT_*_MCP_PORT` env vars.

**MCP tool rules:**
- Every tool is a thin proxy — call the Flask API via `_api()` helper and return the result.
- Use `@mcp.tool()` decorator. The docstring becomes the tool description.
- Type hints on parameters are **required** — MCP uses them for JSON schema generation.
- Return `dict` or `list`, never raw strings.

### Adding a new MCP server

1. Create Flask Blueprint with REST routes under `/api/<feature>/*`
2. Register blueprint in `app.py`
3. Add health probe port to `api_mcp_health` in `app.py`
4. Create `mcp/<name>_server.py` following the existing template exactly
5. Pick the next sequential port

## Pydantic v2 — Critical Rules

This codebase uses **Pydantic v2**. Common mistakes to avoid:

```python
# ✅ CORRECT (Pydantic v2)
from pydantic import BaseModel, ConfigDict

class MyModel(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str
    created_at: str  # ISO 8601 UTC

# ❌ WRONG (Pydantic v1 style — will break)
class MyModel(BaseModel):
    class Config:
        str_strip_whitespace = True
```

- Use `ConfigDict`, not `class Config`.
- Use `model_dump()`, not `.dict()`.
- Use `model_validate()`, not `.parse_obj()`.

## Testing — Hard Rules

- **Framework:** `pytest` with `pytest-cov`. Config in `pytest.ini`.
- **File naming:** `tests/test_<module>.py`
- **Run single test:** `pytest tests/test_<module>.py::<TestClass>::<test_name> -v`
- **TDD required:** Write failing test first (RED), implement (GREEN), refactor (REFACTOR).
- **Minimum per module:** happy path + edge cases + error handling.

```python
import pytest
from db.example_db import ExampleDB

class TestExampleDB:
    def test_create_and_get(self):
        result = ExampleDB.create("ex-1", "Test")
        assert result["id"] == "ex-1"
        fetched = ExampleDB.get_by_id("ex-1")
        assert fetched["name"] == "Test"

    def test_get_nonexistent_returns_none(self):
        assert ExampleDB.get_by_id("nope") is None
```

## Coding Standards

- 4-space indentation, PEP 8 naming/style.
- Keep changes scoped — avoid unrelated refactors.
- Comments only where logic is non-obvious.
- No cross-boundary imports — server must not import client modules.
- Server must not own renderer HTML/CSS/JS or terminal UI concerns.

## Hardening

`hardening.py` provides decorators for Flask routes:

- `rate_limit` — rate limiting
- `validate_request` — request validation
- `safe_limit` — pagination safety
- `retry_with_backoff` — retry logic for external calls

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SAVANT_DB` | `~/.savant/savant.db` | SQLite database path |
| `SAVANT_API_BASE` | `http://localhost:8090` | Flask API base URL (used by MCP servers) |
| `BASE_CODE_DIR` | — | Root directory for code project ingestion |
| `SAVANT_WORKSPACE_MCP_PORT` | `8091` | Workspace MCP port |
| `SAVANT_ABILITIES_MCP_PORT` | `8092` | Abilities MCP port |
| `SAVANT_CONTEXT_MCP_PORT` | `8093` | Context MCP port |
| `SAVANT_KNOWLEDGE_MCP_PORT` | `8094` | Knowledge MCP port |
| `SAVANT_REMINDERS_MCP_PORT` | `8095` | Reminders MCP port |

## Docker

```bash
# Build and run
docker compose up -d --build

# With code directory mounted
BASE_CODE_HOST_DIR=~/code docker compose up -d --build
```

`docker-compose.yml` and `Dockerfile` are at repo root. Entry point is `docker-entrypoint.sh`.
