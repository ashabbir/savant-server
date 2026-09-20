# Server Agent Playbook

- Read `app.py`, `server_paths.py`, and the relevant `db/` or feature module before editing.
- Preserve the auth middleware contract unless the request explicitly changes it.
- Keep API and MCP behavior aligned when changing a route.
- Treat startup/bootstrap code as production code; verify idempotence.
- Avoid hardcoding host paths or ports when the repo already exposes environment variables.
- If you touch database behavior, verify both schema creation and read/write paths.

## Safe change order

1. Locate the request path and auth boundary.
2. Check `savant-knowledge.project_context(workspace_id)` or `search()` for domain rules and architectural constraints.
3. Use `savant-context.research` for broad first-pass code & dependency exploration, and `structure_search` to pinpoint AST declarations.
4. Use `savant-context.get_lossless_tree` on narrow line ranges to inspect concrete syntax (LST) before editing.
5. Update the feature module and backing DB code together.
6. Verify changes with `savant-context.analyze_code` to review complexity, lint findings, and CodeGraph blast radius.
7. Confirm startup/bootstrap still succeeds.
8. Run the targeted tests and the broader suite when behavior crosses modules.
9. If durable architectural insights or bug discoveries were made, record them in `savant-knowledge.store` and publish with `commit_workspace`.
