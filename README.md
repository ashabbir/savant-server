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

### Savant Context: `analyze_code` guide

Use `savant-context.analyze_code` for a read-only structural review before or after a refactor. It never executes submitted source and never writes to a repository. The result includes complexity, line count, findings, before/after deltas, and a safe refactor workflow.

| Goal | Required arguments | What is analyzed |
|------|--------------------|------------------|
| Review a pasted file | `code` | The complete submitted file; no repository lookup is required. |
| Review a proposed replacement | `repo`, `path`, `code` | Submitted complete file compared with the indexed file. |
| Review only one declaration | Add `symbol` / `name` / `class_name`, plus `node_type` | The matching function or class in the baseline and submitted source. |
| Review a patch | `repo`, `path`, `diff` | Indexed file with the unified diff applied in memory. |
| Review after editing | `repo`, `path` | Current indexed file. Re-index first if the file changed on disk. |

Examples:

```text
# Find refactor targets in a file supplied by the caller
analyze_code(
  code="""def normalize(value):
    if value:
        return value.strip()
    return None
    print('unreachable')
"""
)

# Validate a proposed complete replacement before changing it on disk
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

When narrowing to a function or class, `code` must include its declaration (for example, `def function_name(...)`), not only its inner body. The initial standalone call reports a `new` baseline; a repository-backed proposal reports `updated` with its delta when it differs from indexed source.

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
