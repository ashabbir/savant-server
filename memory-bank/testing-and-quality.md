# Server Testing and Quality

## Verification commands

- Set `SAVANT_TEST_DATABASE_URL` to a dedicated PostgreSQL database whose name
  contains `test`, then set `SAVANT_DATABASE_URL` to the same URL for test
  runs. The pytest fixture fails closed before `TRUNCATE` if this is absent or
  unsafe.
- `./run-tests.sh`
- `.venv/bin/python -m pytest tests/<path>::<test_name> -v`
- `.venv/bin/python -m pytest --cov=. --cov-report=term`

## What to verify

- Auth rejects missing or invalid `X-API-Key` values on protected routes.
- Workspace/task/note CRUD flows still persist correctly.
- Knowledge and ability routes remain reachable after startup.
- MCP endpoints still proxy through the Flask app cleanly.
- Seed users and schema initialization remain idempotent.

## Common regressions

- Breakage in the auth middleware causing all API calls to fail.
- Path resolution differences between Docker and host runs.
- Schema drift in the persistence layer.
- Mismatched assumptions between REST and MCP wrappers.

## Quality bar

- Prefer source-faithful fixes over local placeholders.
- Update tests near the changed layer and add integration coverage when a contract crosses modules.
