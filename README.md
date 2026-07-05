# Savant Server (Version 13.0.1 - Patch Release)

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

```bash
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

- SQLite with WAL mode (`sqlite_client.py`)
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

### Abilities Bootstrap

Seed data is embedded in `abilities/bootstrap.py`. On first startup, abilities are materialized to `SAVANT_SERVER_DATA_DIR/abilities/`.

### Health Probes

- `GET /health/live` — process alive
- `GET /health/ready` — DB initialized, abilities bootstrapped

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
