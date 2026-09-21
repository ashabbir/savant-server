# Copilot Instructions — Savant Server

## What This Is

Savant Server is a **Flask API + MCP backend** that provides centralized persistence, knowledge graph, context ingestion, and AI tool servers for the Savant ecosystem. It communicates with `savant-client` over HTTP, SSE, and Streamable HTTP — never import client code.

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

mcp/                      ← MCP server implementations (SSE + Streamable HTTP bridges)
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

MCP servers are **thin transport bridges** that proxy tool calls to Flask REST endpoints. SSE (`8091–8095`) and Streamable HTTP (`8191–8195`, `/mcp`) run side by side; they never touch the DB or filesystem directly.

| MCP name | Port | Server file |
|----------|------|-------------|
| savant-workspace | 8091 | `mcp/server.py` |
| savant-abilities | 8092 | `mcp/abilities_server.py` |
| savant-context | 8093 | `mcp/context_server.py` |
| savant-knowledge | 8094 | `mcp/knowledge_server.py` |
| savant-reminders | 8095 | `mcp/reminders_server.py` |

Ports are **fixed** so AI tool configs never go stale. Configured via `SAVANT_*_MCP_PORT` env vars.

### Tool Guide: What Tool to Use for What Purpose

AI agents must select tools based on domain boundaries, abstraction layers, and task stage:

#### 1. Code Intelligence & Context (`savant-context` - Port 8093)
Use for physical codebase exploration, syntax trees, dependency graphs, and static quality checks:
- **`research(q, repo, type='all'|'code'|'memory', limit)`**:
  - **Purpose**: Single first-pass entry point for exploring codebases, dependencies, and memory banks.
  - **When to use**: Starting an unfamiliar task, discovering feature implementations across repositories, or investigating bug reports.
  - **Efficiency**: Use `type='all'` (default, limit 5) for broad exploration; `type='code'` (auto-enables CodeGraph) for source code & call chains; `type='memory'` for architecture docs only.
- **`structure_search(q, repo)`**:
  - **Purpose**: AST symbol definition pinpointing.
  - **When to use**: When the symbol name or shape is known (`SessionManager`, `auth_middleware`) and you need its exact declaration location without semantic vector fuzziness.
  - **Workflow**: Follow up with `get_lossless_tree` on that specific file and narrow line range before editing.
- **`get_lossless_tree(repo, path, start_line, end_line, max_nodes=200)`**:
  - **Purpose**: Concrete syntax tree (LST) inspection immediately before surgical edits.
  - **When to use**: Before modifying code where exact indentation, whitespace, delimiters, comments, and line ranges matter.
  - **LST vs AST**: Conventional ASTs discard comments and formatting; LST preserves 100% concrete syntax fidelity.
  - **Efficiency**: **Always provide narrow `start_line` and `end_line` ranges** on large files to minimize token cost.
- **`search_lossless_tree(q, repo, limit=10)`**:
  - **Purpose**: Exact multi-repository syntax pattern matching.
  - **When to use**: Locating identical syntax patterns, literal usages, or configurations across multiple repositories bounded by concrete syntax nodes.
- **`analyze_code(repo, path, code, diff, symbol, node_type)`**:
  - **Purpose**: Deep static code quality, complexity, and blast radius analysis.
  - **When to use**: Before/after refactoring or editing, evaluating a standalone snippet (`code='...'`), reviewing a proposed replacement (`repo` + `path` + `code`), or checking a unified diff (`diff='...'`).
  - **Metrics**: Reports cyclomatic/cognitive complexity, code health findings (dead code, unsafe error handling, tight coupling), and maintainability index without writing to disk or executing code.
- **CodeGraph Relationships**:
  - **Purpose**: Code-level call graphs, imports, and impact surfaces.
  - **When to use**: Evaluating blast radius (`upstream_caller`: who calls this?; downstream: what does this call?) before changing function signatures or APIs. Surfaced in `research(include_graph=True)` and `analyze_code`.

#### 2. Business & Architecture Knowledge Graph (`savant-knowledge` - Port 8094)
Use for domain capability models, partner client rules, service catalogs, tech stacks, and developer insights:
- **`project_context(workspace_id)`**:
  - **Purpose**: Workspace onboarding at session start. Traverses the graph from the workspace project node (depth 2) to return connected domains, services, tasks, and notes.
- **`search(query, node_type, limit)`**:
  - **Purpose**: Discovering existing domain concepts, client partner requirements, architectural decisions, and known bugs.
  - **Node types**: `client` (Fidelity, UBS…), `domain` (Auth/SSO, Holdings…), `service` (icn, simonapp…), `library`, `technology`, `insight` (curated knowledge/decisions), `issue` (known bugs), `project`, `concept`, `repo`, `session`.
- **`neighbors(node_id, depth, edge_type)`**:
  - **Purpose**: Traversing relationships outward from any entity to understand cross-service or client impacts.
- **`store(content, workspace_id, node_type, repo, files, connections)`**:
  - **Purpose**: Recording durable outcomes, design decisions (`insight`), or known bugs (`issue`). Created as `staged`; publish via `commit_workspace(workspace_id)` or `commit_nodes(...)`.
- **`list_concepts()`**:
  - **Purpose**: Listing abstract architectural concepts and design patterns.

#### 3. Cross-Server Workflow Bridge
- **From Knowledge to Code**: When a knowledge node references a `service`, `repo`, or `files`, transition to `savant-context.research` and `structure_search` to investigate the actual implementation, then `get_lossless_tree` before editing.
- **From Code to Knowledge**: When code analysis or bug fixing reveals non-obvious architecture constraints, client workarounds, or incident root causes, record them in `savant-knowledge.store` and commit them for future agents.

#### 4. Workspace & Task Management (`savant-workspace` - Port 8091)
- `list_workspaces`, `get_workspace`, `create_workspace`, `close_workspace`
- `list_tasks`, `create_task`, `update_task`, `complete_task`, `add_task_dependency`: Task dependency graphs and execution tracking.
- `list_session_notes`, `create_session_note`: Ephemeral scratchpad notes for session context.
- `list_jira_tickets`, `create_jira_ticket`, `list_merge_requests`, `create_merge_request`: Tracking integration work.

#### 5. Abilities & Prompts (`savant-abilities` - Port 8092)
- `find_assets(query, type)`: Locate backend coding rules, repo overlays, or policies without building full persona prompts.
- `resolve_abilities(persona_id, repo_id)`: Compile personas, applicable rules, and policies into a deterministic prompt.

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
