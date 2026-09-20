---
name: savant-code-analysis
description: Analyze, inspect, and clean up code using AST, LST, CodeGraph, and static analysis tools.
---

# Savant Code Analysis

When implementing, modifying, or cleaning up code, follow this code intelligence and verification workflow:

## 1. Tool Selection: What to Use When Best

- **AST Structure Pinpointing (`savant-context.structure_search`)**:
  Use when locating declared classes, functions, and methods across repositories by symbol name without semantic fuzziness.
- **Concrete Syntax Inspection (`savant-context.get_lossless_tree`)**:
  Use immediately before making surgical edits. The Lossless Syntax Tree (LST) preserves exact delimiters, comments, formatting, and line ranges. Always pass narrow `start_line` and `end_line` ranges on large files to minimize token usage.
- **CodeGraph Blast Radius Analysis**:
  Review upstream callers (1 level up) and downstream dependencies (1 level down) via `savant-context.research(include_graph=True)` or the `impact_surface` emitted by `savant-context.analyze_code` to evaluate ripples across the codebase.
- **Deep Static Analysis (`savant-context.analyze_code`)**:
  Run against every changed production file, proposed complete file, or unified diff. Evaluates cyclomatic and cognitive complexity, dead code, duplication, unsafe error handling, performance regressions, and maintainability deltas.

## 2. Implementation & Cleanup Loop

1. **Pre-Change Inspection**: Run `savant-context.get_lossless_tree` on target line ranges to verify concrete syntax.
2. **Pre-Change Review (Optional)**: If modifying a complex file, run `savant-context.analyze_code` with proposed `code` or `diff` to preview findings before disk write.
3. **Execution**: Apply the focused code changes.
4. **Post-Change Verification**: Run `savant-context.analyze_code` against every modified production file. Resolve justified findings (dead code, complexity spikes, unhandled exceptions).
5. **Validation**: Run the relevant test suite to ensure regressions were not introduced.

## 3. Knowledge Graph Bridge (`savant-knowledge`)

When code analysis or refactoring reveals significant architectural learnings, critical edge cases, or bug root causes:
1. Capture them in `savant-knowledge.store`:
   - `workspace_id`: Active workspace ID.
   - `node_type`: `'insight'` for architectural patterns/learnings, or `'issue'` for identified defects/gotchas.
   - `repo`: Repository name.
   - `files`: Touched file paths verified through `savant-context`.
   - `connections`: Link to matching domain or service nodes.
2. Publish with `savant-knowledge.commit_workspace(workspace_id)` or `commit_nodes(...)`.

Use `savant-context` and `savant-knowledge` MCP tools exclusively. Do not substitute direct database or REST API access.
