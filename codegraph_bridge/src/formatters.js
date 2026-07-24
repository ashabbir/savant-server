export const namespaced = id => `codegraph:${id}`;
export const rawId = id => String(id || '').replace(/^codegraph:/, '');

export function formatLocation(repoId, node) {
  return {
    repo_id: repoId,
    file_path: node.filePath,
    start_line: node.startLine,
    end_line: node.endLine,
    start_column: node.startColumn ?? null,
    end_column: node.endColumn ?? null
  };
}

export function formatFlags(node) {
  return {
    exported: Boolean(node.isExported),
    async: Boolean(node.isAsync),
    static: Boolean(node.isStatic),
    abstract: Boolean(node.isAbstract)
  };
}

export function formatNode(repoId, node) {
  return {
    id: namespaced(node.id),
    name: node.name,
    qualified_name: node.qualifiedName || null,
    kind: node.kind,
    language: node.language || null,
    location: formatLocation(repoId, node),
    signature: node.signature || null,
    docstring: node.docstring || null,
    flags: formatFlags(node),
    metadata: { provider_kind: node.kind, visibility: node.visibility || null }
  };
}

export function formatEdgeLocation(repoId, edge) {
  if (!edge.line) return null;
  return {
    repo_id: repoId,
    file_path: edge.metadata?.filePath || '.',
    start_line: edge.line,
    end_line: edge.line,
    start_column: edge.column ?? null,
    end_column: edge.column ?? null
  };
}

export function formatEdge(repoId, edge) {
  const provenance = edge.provenance === 'heuristic' ? 'heuristic' : 'static';
  return {
    source_id: namespaced(edge.source),
    target_id: namespaced(edge.target),
    kind: edge.kind,
    location: formatEdgeLocation(repoId, edge),
    provenance,
    confidence: null,
    metadata: edge.metadata || {}
  };
}
