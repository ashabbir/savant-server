---
name: savant-code-analysis
description: Analyze and clean up code after every completed implementation change.
---

# Savant code analysis

Whenever implementation changes are complete, before declaring the work done:

1. Run `savant-context.analyze_code` through MCP against every changed production file or its unified diff.
2. Review the findings for correctness, complexity, dead code, duplication, unsafe error handling, performance regressions, and maintainability.
3. Make the focused cleanup changes that are justified by the analysis, then run `savant-context.analyze_code` again on the final code or diff.
4. Run the relevant test and validation commands; report remaining findings explicitly if they cannot be safely resolved in scope.

Use `savant-context` MCP for all code analysis. Do not substitute direct database/API access for MCP tools.
