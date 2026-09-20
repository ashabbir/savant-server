# Savant Server (Version 15.0.0 - Major Release)

License Owned by Project X. This repository is private and proprietary.

Centralized Flask API + MCP backend for Savant. Deployed in customer infrastructure (Docker/K8s/VM).

Features the new **Tool Belt** for enhanced agentic capabilities and the **Skill System** for unified capability management.

## Quick Start

### Local (Python)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python app.py
```

Server starts at `http://127.0.0.1:8090`.

### Docker

```bash
docker compose up -d --build
```

## Test

Tests require an explicitly configured, isolated PostgreSQL database. The
database name must contain `test`; the fixture refuses to run destructive
setup otherwise. Do not point either variable at a development or production
database.

```bash
export SAVANT_TEST_DATABASE_URL='postgresql://savant_test_user:password@127.0.0.1:55432/savant_test'
export SAVANT_DATABASE_URL="$SAVANT_TEST_DATABASE_URL"

# All server tests
./run-tests.sh

# Single test
.venv/bin/python -m pytest tests/<path>::<test_name> -v

# With coverage
.venv/bin/python -m pytest --cov=. --cov-report=term
```

## Build & Deploy

```bash
# Build Docker image
docker build --build-arg SAVANT_UID=$(id -u) --build-arg SAVANT_GID=$(id -g) -t savant-server:latest .

# Deploy (Docker)
docker compose up -d

# Deploy (local gunicorn)
.venv/bin/gunicorn --bind 0.0.0.0:8090 --workers 2 --threads 4 app:app
```

## Architecture

### API Surface

Flask app (`app.py`) with feature modules as Blueprints:

| Module | Routes | MCP Server | Port |
|--------|--------|------------|------|
| **Workspace** | `/api/workspaces/*`, `/api/tasks/*`, `/api/notes/*` | `mcp/server.py` | 8091 |
| **Abilities** | `/api/abilities/*` | `mcp/abilities_server.py` | 8092 |
| **Context** | `/api/context/*` | `mcp/context_server.py` | 8093 |
| **Knowledge** | `/api/knowledge/*` | `mcp/knowledge_server.py` | 8094 |
| **Reminders** | `/api/reminders/*` | `mcp/reminders_server.py` | 8095 |

### Data Layer

- PostgreSQL (with pgvector) via `postgres_client.py`; `SAVANT_DATABASE_URL`
  is required for production deployment.
- DB access: static-method classes in `db/` (`WorkspaceDB`, `TaskDB`, `NoteDB`, `MergeRequestDB`, `JiraTicketDB`, `NotificationDB`, `UserDB`)
- Pydantic v2 models in `models.py` (use `ConfigDict`, not class-based `Config`)
- Timestamps: ISO 8601 UTC strings

### MCP Pattern

Each MCP server is a thin SSE bridge that proxies tool calls to Flask REST endpoints:

```python
@mcp.tool()
def example_tool(param: str) -> dict:
    """Tool description shown to AI clients."""
    return _api("POST", "/api/feature/example", json={"param": param})
```

### Savant Context & Knowledge: What to Use When Best

Savant provides two complementary MCP servers for AI agents:
1. **`savant-context`** (Port 8093): Physical codebase intelligence — AST structure search, Lossless Syntax Trees (LST), CodeGraph dependency graphs, semantic code search, and static code analysis.
2. **`savant-knowledge`** (Port 8094): Business & architecture metadata graph — capability domains, partner clients (Fidelity, UBS…), deployable services, shared libraries, tech stack, and curated developer insights/issues.

#### Decision Matrix for AI Agents

| Capability / Tool | Subsystem | When to Use Best | Key Benefit / Token Efficiency Tip |
| :--- | :--- | :--- | :--- |
| **`research`** | Context (Unified) | **Task start & initial discovery** when exploring an unfamiliar area, bug, or feature. | Combines semantic search, AST definitions, CodeGraph, and memory bank in 1 call. Start with `type='all', limit=5`. |
| **`structure_search`** | Context (AST) | When the **symbol name or shape is known** (e.g. `SessionManager`, `authMiddleware`) and you need its exact declaration. | Direct AST index lookup. Zero semantic fuzziness. Pinpoints class and function declarations. |
| **`get_lossless_tree`** | Context (LST) | **Immediately before reading or editing code** for surgical changes or refactoring. | Preserves 100% concrete syntax (whitespace, comments, delimiters). **Always specify narrow line ranges** (`start_line`, `end_line`). |
| **`search_lossless_tree`** | Context (LST) | Multi-repository exact syntax pattern matching or variable usage. | Exact matching bounded by concrete syntax nodes across repositories. |
| **`analyze_code`** | Context (Analysis) | **Before & after code modifications**, reviewing snippets, or testing unified diffs. | Evaluates cyclomatic/cognitive complexity, code quality findings/lints, duplication, and before/after deltas. |
| **CodeGraph** | Context (Graph) | Investigating **who calls what** (`upstream_caller`), dependency chains, and ripple effects. | Built into `research(include_graph=True)` and returned in the `impact_surface` of `analyze_code`. |
| **`project_context` / `search`** | Knowledge Graph | **Before code work**: checking domain rules, client quirks, architecture decisions, and known issues. | Traverses high-level domain graph (depth 2) to onboard an agent to workspace constraints. |
| **`store` + `commit_workspace`** | Knowledge Graph | **After code work**: recording architectural decisions, bug patterns, or reusable lessons. | Links technical findings (`repo`, `files`) to business domains and services for future sessions. |

#### Context Tool Deep-Dive

##### 1. AST Structure Search (`structure_search`)
Queries the AST symbol index directly. Use when you need the exact file path, class hierarchy, and line ranges where a function or class is defined without semantic vector fuzziness. Follow up with `get_lossless_tree` on that specific line range before modifying it.

##### 2. Lossless Syntax Tree (`get_lossless_tree`, `search_lossless_tree`)
Standard ASTs strip comments, whitespace, and formatting delimiters. The Lossless Syntax Tree (LST) preserves complete concrete syntax fidelity with exact byte and line coordinates. Always provide narrow `start_line` and `end_line` parameters on large files to keep token costs minimal.

##### 3. CodeGraph & Blast Radius
CodeGraph models code-level relationships: caller/callee (`calls`), module imports (`imports`), and class inheritance. Available through `research(include_graph=True)` and within the `impact_surface` section of `analyze_code`. Use it to answer: "Who breaks if I change this function?" and "What dependencies does this class rely on?"

##### 4. Static Code Review (`analyze_code`)
Use `savant-context.analyze_code` for read-only structural analysis before or after a refactor. It never executes submitted source and never writes to disk.

| Goal | Required arguments | What is analyzed |
|------|--------------------|------------------|
| Review a pasted file | `code` | The complete submitted file; no repository lookup is required. |
| Review a proposed replacement | `repo`, `path`, `code` | Submitted complete file compared with the indexed file. |
| Review only one declaration | Add `symbol` / `name` / `class_name`, plus `node_type` | The matching function or class in the baseline and submitted source. |
| Review a patch | `repo`, `path`, `diff` | Indexed file with the unified diff applied in memory. |
| Review after editing | `repo`, `path` | Current indexed file. Re-index first if the file changed on disk. |

Examples:

```python
# Standalone review of a code snippet
analyze_code(
    code="""def normalize(value):
    if value:
        return value.strip()
    return None
"""
)

# Validate proposed replacement before writing to disk
analyze_code(
    repo="savant-server",
    path="context/routes.py",
    symbol="_execute_analysis",
    node_type="function",
    code="""def _execute_analysis(params):
    # proposed replacement, including the function declaration
    ...
"""
)
```

#### Knowledge Graph Bridge: Transitioning Between Context and Knowledge

- **From Knowledge to Code**: When `savant-knowledge.search` or `project_context` returns nodes pointing to services, repositories, or source files, transition to `savant-context.research` and `structure_search` to inspect the physical code, then `get_lossless_tree` for surgical editing.
- **From Code to Knowledge**: When deep code analysis (`savant-context.analyze_code`) or refactoring reveals non-obvious architecture rules, client-specific workarounds, or bug root causes, persist them in `savant-knowledge.store` (with `workspace_id`, `node_type='insight'|'issue'`, `repo`, and touched `files`) and publish with `commit_workspace`.

### Abilities Bootstrap

Seed data is embedded in `abilities/bootstrap.py`. On first startup, abilities are materialized to `SAVANT_SERVER_DATA_DIR/abilities/`.

### Health Probes

- `GET /health/live` — process alive
- `GET /health/ready` — PostgreSQL dependency is reachable; returns `503` with
  a non-secret dependency diagnostic when it is not. It is intentionally
  distinct from liveness.
- `GET /api/mcp/health` — probes the MCP SSE servers on ports 8091-8095 and
  returns `503` when any configured server is unreachable.

### Knowledge Graph Maintenance

The dedicated `knowledge.maintenance_runner` process runs the institutional
knowledge graph optimization job at `0 */4 * * *` UTC. Each pass takes a
PostgreSQL advisory transaction lock, promotes staged workspace knowledge in
bounded batches, resolves explicit supersession records, consolidates exact
canonical entities, applies a taxonomy cluster, expires time-bound records,
and writes an audit row. The work is isolated from Flask/MCP SSE workers.

- `GET /api/knowledge/maintenance/status` — scheduler state and recent runs (admin)
- `GET /api/knowledge/maintenance/runs` — audit history (admin)
- `POST /api/knowledge/maintenance/run` — queue an immediate run (admin)

### Version Info

- `GET /version` or `GET /api/version` — returns the server build version, branch, commit, and build timestamp
- Version is read from `build-info.json`
- `GET /health/live` and `GET /health/ready` also include the same build version in their JSON response

## Docker Isolation (API-only mode)

- `SAVANT_API_ONLY=1`: non-API routes return 404
- Read-only root filesystem
- Dropped Linux capabilities + `no-new-privileges`
- Bind mount for persistent data: `~/.savant/server-data` → `/data/savant`

## Initial Seed Users

On first startup with empty DB:

| user_id | role | api_key |
|---------|------|---------|
| `ahmed` | `admin` | `sk-ahmed-savant-001` |
| `lex` | `user` | `sk-lex-savant-001` |

Use `X-API-Key` header. Rotate for production.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `FLASK_HOST` | `0.0.0.0` | Bind address |
| `FLASK_PORT` | `8090` | API port |
| `SAVANT_SERVER_DATA_DIR` | `/data/savant` (Docker) or `./data` (local) | Persistent data root |
| `SAVANT_DB` | `<data_dir>/savant.db` | SQLite DB path override |
| `SAVANT_API_ONLY` | `0` | Enable API-only mode |
| `SAVANT_ABILITIES_SEED_DIR` | `<data_dir>/abilities` | Abilities seed location |
| `EMBEDDING_MODEL_DIR` | Bundled | Embedding model files |
| `BASE_CODE_DIR` | `/base-code` (Docker) | Root for directory source ingestion |
| `BASE_CODE_HOST_DIR` | `~/Developer/code` | Host path mounted as BASE_CODE_DIR |
| `RUNNING_IN_DOCKER` | Auto-detected | Force container-mode paths |
| `SAVANT_DISABLE_BG_CACHE` | `0` | Disable background cache worker |

## Migrations

Data migration scripts are in `migrations/`. Run sequentially:

```bash
python migrations/01-migrate-export.py
python migrations/02-migrate-import.py
# ... etc
```
