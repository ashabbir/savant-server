---
name: savant-knowledge-commit
description: Record workspace-scoped outcomes in the Savant knowledge graph through MCP, referencing code context from savant-context.
---

# Savant Knowledge Commit

After completing meaningful work that belongs to an attached workspace, capture the durable outcome in Savant Knowledge using only `savant-knowledge` MCP tools, cross-referenced with `savant-context` code intelligence.

## Step-by-Step Workflow

1. **Verify Code Findings (`savant-context`)**:
   - Use `savant-context.analyze_code` to confirm touched production files, complexity metrics, and resolved findings.
   - Use `savant-context.structure_search` or `savant-context.get_lossless_tree` to verify exact symbol declarations and file paths that belong to this knowledge record.
2. **Search First (`savant-knowledge.search`)**:
   - Query existing nodes to prevent duplication. Reuse or update an existing node via `savant-knowledge.update_node` if the concept already exists.
3. **Verify Domain & Context Hierarchy**:
   - Check `savant-knowledge.project_context(workspace_id)` for the matching `domain` node. If none exists, create a `domain` node first—every outcome must be rooted in a business domain.
4. **Choose the Narrowest Valid Node Type**:
   - `insight`: Durable architectural decisions, design rationale, or lessons learned.
   - `issue`: Known defects, incident root causes, or tricky edge cases.
   - `service` / `library` / `technology`: Concrete technical components.
   - `operation`: Operational tasks or runbooks.
   - `session`: AI session summary.
5. **Store Staged Node (`savant-knowledge.store`)**:
   - Set `workspace_id` to the active workspace ID.
   - Populate `repo` and comma-separated `files` verified via `savant-context`.
   - Provide `connections` linking the new node to the `domain` and relevant existing `service`, `technology`, or `repo` nodes using typed edges (`relates_to`, `applies_to`, `uses`, `depends_on`).
6. **Publish / Commit**:
   - Commit the staged node with `savant-knowledge.commit_nodes` or commit the entire workspace with `savant-knowledge.commit_workspace(workspace_id)`.

Never write directly to the database or call REST API endpoints outside MCP. Do not create duplicate nodes, domainless nodes, orphaned nodes, or nodes with an incorrect type.
