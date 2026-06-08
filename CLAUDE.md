# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Canonical reference

`.github/copilot-instructions.md` contains the full architecture, conventions, and rules. When in doubt, follow that file.

## Quick start

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python app.py              # Flask on http://127.0.0.1:8090
pytest tests/ -v                     # run tests
docker compose up -d --build         # Docker deployment
```

## Key rules

- **Pydantic v2:** Use `ConfigDict` not `class Config`. Use `model_dump()` not `.dict()`.
- **DB layer:** Static-method classes in `db/`. Timestamps are ISO 8601 UTC strings. Use `get_connection()` from `sqlite_client.py`.
- **MCP servers:** Thin SSE bridges only — proxy to Flask REST endpoints, never touch DB directly. Ports 8091–8095.
- **No client imports:** Server communicates with savant-client over HTTP/SSE only.
- **TDD:** `pytest` with `pytest-cov`. Write failing test first, then implement, then refactor.
- **Testing:** `pytest tests/ -v`. File naming: `tests/test_<module>.py`.
