# Server Runtime

## Local start

- Create a Python 3.11 venv.
- Install `requirements-dev.txt`.
- Run `python app.py`.

## Docker start

- Use `docker compose up -d --build`.
- The container should expose the Flask API on the configured `FLASK_PORT`.

## Environment

- `FLASK_HOST` controls bind address.
- `FLASK_PORT` controls the API port.
- `SAVANT_SERVER_DATA_DIR` sets the persistent data root.
- `SAVANT_DB` overrides the database path.
- `SAVANT_API_ONLY` disables non-API routes.
- `BASE_CODE_DIR` and `BASE_CODE_HOST_DIR` affect code ingestion and path mapping.

## Startup behavior

- Database schema initialization happens on app startup.
- Default users are seeded automatically.
- Abilities bootstrap from the configured seed directory.
- Logging is initialized early so startup failures are visible.

## Operational checks

- Confirm `/health/live` and `/health/ready`.
- Confirm `/api/auth/validate` with a known API key.
- Confirm the main API can reach the database and bootstrap data paths.
