---
name: savant-knowledge-commit
description: Record workspace-scoped outcomes in the Savant knowledge graph through MCP.
---

# Savant knowledge commit

After completing meaningful work that belongs to an attached workspace, capture the durable outcome in Savant Knowledge using only `savant-knowledge` MCP tools.

1. Search first for an equivalent node with `savant-knowledge.search`. Reuse or update an equivalent node instead of creating a duplicate.
2. Find a matching domain node. If none exists, create a `domain` node first; every outcome must be under a domain.
3. Choose the narrowest valid node type: use `insight` for a learning or decision, `issue` for a defect, `service`/`library`/`technology` for concrete technical entities, `operation` for operational work, and `session` for session context.
4. Store the outcome with the attached `workspace_id`, source, repository, and touched files. Link it to the domain and at least one other relevant existing node (such as project, repository, service, or related insight) using typed edges. If necessary, create the supporting node first so the outcome is never orphaned.
5. Commit the staged node with `savant-knowledge.commit_nodes` or commit the workspace with `savant-knowledge.commit_workspace` after verifying the graph links.

Never write directly to the database or call the Savant API/graph endpoints outside MCP. Do not create duplicate nodes, domainless nodes, orphaned nodes, or nodes with an incorrect type.
