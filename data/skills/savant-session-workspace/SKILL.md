---
name: savant-session-workspace
description: Attach every active session to a Savant workspace using only savant-workspace MCP tools.
---

# Savant session workspace

Run this workflow immediately after the first user-authored message in every session, before doing workspace-scoped work.

1. Ask which Savant workspace to use when the user has not already named one.
2. If the user gives a workspace name, call `savant-workspace.list_workspaces` through MCP and match it by name.
3. If it exists, call `savant-workspace.assign_session_to_workspace` through MCP with that workspace ID and the current session ID.
4. If it does not exist, tell the user that a Savant workspace will be created. Call `savant-workspace.create_workspace` through MCP, then call `savant-workspace.assign_session_to_workspace` through MCP with the returned workspace ID.

Never use direct database access, HTTP/API calls, shell database clients, or guessed workspace IDs for this workflow. All workspace lookup, creation, and attachment calls must go through `savant-workspace` MCP. Do not perform workspace-scoped work until the session is attached.
