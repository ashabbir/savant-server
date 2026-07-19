import { parentPort, workerData } from 'node:worker_threads';
import codegraphPackage from '@colbymchenry/codegraph';

const { CodeGraph, NODE_KINDS } = codegraphPackage;
const { operation, root, repoId, params } = workerData;
const ns = id => `codegraph:${id}`;
const raw = value => String(value || '').replace(/^codegraph:/, '');
const symbol = node => ({
  id: ns(node.id), name: node.name, qualified_name: node.qualifiedName || null,
  kind: node.kind, language: node.language || null,
  location: { repo_id: repoId, file_path: node.filePath, start_line: node.startLine, end_line: node.endLine, start_column: node.startColumn ?? null, end_column: node.endColumn ?? null },
  signature: node.signature || null, docstring: node.docstring || null,
  flags: { exported: Boolean(node.isExported), async: Boolean(node.isAsync), static: Boolean(node.isStatic), abstract: Boolean(node.isAbstract) },
  metadata: { provider_kind: node.kind, visibility: node.visibility || null }
});
const edge = value => ({ source_id: ns(value.source), target_id: ns(value.target), kind: value.kind, location: null, provenance: value.provenance === 'heuristic' ? 'heuristic' : 'static', confidence: null, metadata: value.metadata || {} });

let graph;
let controller;
async function run() {
  if (params.__fault_delay_ms) await new Promise(resolve => setTimeout(resolve, params.__fault_delay_ms));
  if (operation === 'ensure_index') {
    const existed = CodeGraph.isInitialized(root);
    graph = existed ? await CodeGraph.open(root, { sync: false }) : await CodeGraph.init(root, { index: false });
    controller = new AbortController();
    const result = existed ? await graph.sync() : await graph.indexAll({ signal: controller.signal });
    return { provider: 'codegraph', accepted: true, watching: false, result };
  }
  graph = await CodeGraph.open(root, { sync: false, readOnly: true });
  if (operation === 'list_symbols') {
    const limit = Math.max(1, Math.min(Number(params.limit) || 100, 1000));
    const offset = Math.max(0, Number(params.cursor) || 0);
    const filters = params.filters || {};
    const nodes = NODE_KINDS.flatMap(kind => graph.getNodesByKind(kind))
      .filter(node => !filters.kind || node.kind === filters.kind)
      .filter(node => !filters.language || node.language === filters.language)
      .filter(node => !filters.path || String(node.filePath).startsWith(String(filters.path)))
      .sort((a, b) => String(a.filePath).localeCompare(String(b.filePath)) || a.startLine - b.startLine || String(a.kind).localeCompare(String(b.kind)) || String(a.name).localeCompare(String(b.name)) || String(a.id).localeCompare(String(b.id)));
    const page = nodes.slice(offset, offset + limit);
    const next = offset + page.length;
    return { items: page.map(symbol), next_cursor: next < nodes.length ? String(next) : null, incomplete: next < nodes.length };
  }
  if (operation === 'search_symbols') return graph.searchNodes(params.query || '', { limit: params.limit || 20, ...(params.filters || {}) }).map(item => symbol(item.node));
  if (operation === 'get_symbol') { const node = graph.getNode(raw(params.symbol_ref?.id || params.symbol_ref)); if (!node) throw Object.assign(new Error('symbol not found'), { code: 'NOT_INDEXED' }); return { symbol: symbol(node), source: null }; }
  if (operation === 'explore') {
    const found = graph.searchNodes(params.query || '', { limit: params.max_files || 10 });
    const symbols = found.map(item => symbol(item.node));
    const edges = found.flatMap(item => [...graph.getCallers(item.node.id, 1), ...graph.getCallees(item.node.id, 1)].map(entry => edge(entry.edge)));
    const context = await graph.buildContext(params.query || '', { maxNodes: Math.min(50, params.max_nodes || 20), includeCode: params.include_source !== false, format: 'structured' });
    return { symbols, edges, provider: 'codegraph', incomplete: false, warnings: [], context };
  }
  const id = raw(params.symbol_ref?.id || params.symbol_ref);
  let entries;
  if (operation === 'get_callers') entries = graph.getCallers(id, params.depth || 1);
  else if (operation === 'get_callees') entries = graph.getCallees(id, params.depth || 1);
  else if (operation === 'get_impact' || operation === 'get_neighbors') {
    const sub = graph.getImpactRadius(id, params.depth || 1);
    const nodes = sub.nodes instanceof Map ? [...sub.nodes.values()] : sub.nodes;
    return { symbols: nodes.map(symbol), edges: sub.edges.map(edge), incomplete: false, warnings: [] };
  } else throw Object.assign(new Error(`unsupported operation: ${operation}`), { code: 'UNSUPPORTED' });
  const limited = entries.slice(0, params.limit || 20);
  return { symbols: limited.map(entry => symbol(entry.node)), edges: limited.map(entry => edge(entry.edge)), incomplete: entries.length > limited.length, warnings: [] };
}

parentPort.on('message', message => { if (message === 'cancel') controller?.abort(); });
run().then(result => parentPort.postMessage({ ok: true, result })).catch(error => parentPort.postMessage({ ok: false, error: { code: error.code || (error.name === 'AbortError' ? 'TIMEOUT' : 'INTERNAL'), message: error.message } })).finally(() => { graph?.close(); });
