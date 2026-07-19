import fs from 'node:fs';
import path from 'node:path';
import { Worker } from 'node:worker_threads';
import codegraphPackage from '@colbymchenry/codegraph';
import packageInfo from '@colbymchenry/codegraph/package.json' with { type: 'json' };
import { BridgeError } from './protocol.js';

const { CodeGraph } = codegraphPackage;

const namespaced = id => `codegraph:${id}`;
const rawId = id => String(id || '').replace(/^codegraph:/, '');

export class Operations {
  constructor({ baseRoots, maxRepositories = 8, timeoutMs = 15000, writeTimeoutMs = null, cancelGraceMs = 500 }) {
    this.baseRoots = baseRoots.map(root => fs.realpathSync(root));
    this.maxRepositories = maxRepositories;
    this.registrations = new Map();
    this.instances = new Map();
    this.writes = new Set();
    this.active = new Map();
    this.recoveryRequired = new Set();
    this.successfullySynced = new Set();
    this.timeoutMs = timeoutMs;
    this.writeTimeoutMs = writeTimeoutMs;
    this.cancelGraceMs = cancelGraceMs;
  }

  register(repoId, root) {
    if (!repoId || !root) throw new BridgeError('PATH_REFUSED', 'repo_id and root are required');
    let real;
    try { real = fs.realpathSync(root); } catch { throw new BridgeError('PATH_REFUSED', 'repository root does not exist'); }
    const allowed = this.baseRoots.some(base => real === base || real.startsWith(`${base}${path.sep}`));
    if (!allowed) throw new BridgeError('PATH_REFUSED', 'repository root is outside configured base roots');
    this.registrations.set(repoId, real);
    return { repo_id: repoId };
  }

  root(repoId) {
    const root = this.registrations.get(repoId);
    if (!root) throw new BridgeError('PATH_REFUSED', 'repository is not registered');
    return root;
  }

  async graph(repoId, create = false) {
    if (this.instances.has(repoId)) return this.instances.get(repoId).graph;
    const root = this.root(repoId);
    if (!create && !CodeGraph.isInitialized(root)) throw new BridgeError('NOT_INDEXED', 'repository is not indexed');
    const graph = create && !CodeGraph.isInitialized(root) ? await CodeGraph.init(root) : await CodeGraph.open(root, { sync: false });
    this.instances.set(repoId, { graph, used: Date.now() });
    this.evict();
    return graph;
  }

  evict() {
    if (this.instances.size <= this.maxRepositories) return;
    const candidates = [...this.instances].filter(([id]) => !this.writes.has(id)).sort((a, b) => a[1].used - b[1].used);
    const victim = candidates[0];
    if (victim) { victim[1].graph.unwatch(); victim[1].graph.close(); this.instances.delete(victim[0]); }
  }

  node(repoId, node) {
    return {
      id: namespaced(node.id), name: node.name, qualified_name: node.qualifiedName || null,
      kind: node.kind, language: node.language || null,
      location: { repo_id: repoId, file_path: node.filePath, start_line: node.startLine, end_line: node.endLine, start_column: node.startColumn ?? null, end_column: node.endColumn ?? null },
      signature: node.signature || null, docstring: node.docstring || null,
      flags: { exported: Boolean(node.isExported), async: Boolean(node.isAsync), static: Boolean(node.isStatic), abstract: Boolean(node.isAbstract) },
      metadata: { provider_kind: node.kind, visibility: node.visibility || null }
    };
  }

  edge(repoId, edge) {
    const provenance = edge.provenance === 'heuristic' ? 'heuristic' : 'static';
    return { source_id: namespaced(edge.source), target_id: namespaced(edge.target), kind: edge.kind, location: edge.line ? { repo_id: repoId, file_path: edge.metadata?.filePath || '.', start_line: edge.line, end_line: edge.line, start_column: edge.column ?? null, end_column: edge.column ?? null } : null, provenance, confidence: null, metadata: edge.metadata || {} };
  }

  runTask(request, write = false) {
    const worker = new Worker(new URL('./task-worker.js', import.meta.url), { workerData: { operation: request.op, root: this.root(request.repo_id), repoId: request.repo_id, params: request.params || {} } });
    const state = { worker, write, repoId: request.repo_id, settled: false };
    this.active.set(request.id, state);
    return new Promise((resolve, reject) => {
      const finish = (callback, value) => {
        if (state.settled) return;
        state.settled = true; clearTimeout(state.timer); this.active.delete(request.id);
        if (write) this.writes.delete(request.repo_id);
        callback(value);
      };
      state.finish = finish; state.reject = reject;
      state.timer = setTimeout(async () => {
        state.terminating = true;
        worker.postMessage('cancel');
        await new Promise(done => setTimeout(done, this.cancelGraceMs));
        await worker.terminate();
        if (write) this.recoveryRequired.add(request.repo_id);
        finish(reject, new BridgeError('TIMEOUT', 'operation timed out and worker was replaced', true));
      }, write ? (this.writeTimeoutMs ?? this.timeoutMs) : this.timeoutMs);
      worker.once('message', async message => {
        state.completing = true;
        worker.terminate();
        if (message.ok) {
          if (write) {
            this.recoveryRequired.delete(request.repo_id);
            this.successfullySynced.add(request.repo_id);
            if (request.params?.watch !== false) {
              try { const watcher = await this.graph(request.repo_id); message.result.watching = watcher.watch(); }
              catch { message.result.watching = false; }
            }
          }
          finish(resolve, message.result);
        }
        else finish(reject, new BridgeError(message.error?.code || 'INTERNAL', message.error?.message || 'worker error'));
      });
      worker.once('error', error => { if (write) this.recoveryRequired.add(request.repo_id); finish(reject, new BridgeError('INTERNAL', error.message)); });
      worker.once('exit', code => { if (!state.settled && !state.terminating && !state.completing && code !== 0) { if (write) this.recoveryRequired.add(request.repo_id); finish(reject, new BridgeError('ENGINE_UNAVAILABLE', 'operation worker exited', true)); } });
    });
  }

  async cancel(requestId) {
    const state = this.active.get(requestId);
    if (!state) return { cancelled: false };
    state.terminating = true;
    state.worker.postMessage('cancel');
    await new Promise(done => setTimeout(done, this.cancelGraceMs));
    await state.worker.terminate();
    if (state.write) this.recoveryRequired.add(state.repoId);
    state.finish(state.reject, new BridgeError('TIMEOUT', 'operation cancelled and worker was replaced', true));
    return { cancelled: true };
  }

  async execute(request) {
    const { op, repo_id: repoId, params = {} } = request;
    if (op === 'ping') return { engine_version: packageInfo.version, protocol_version: 1 };
    if (op === 'cancel') return this.cancel(params.request_id);
    if (op === 'register') return this.register(repoId, params.root);
    if (op === 'ensure_index') {
      if (this.writes.has(repoId)) throw new BridgeError('BUSY', 'repository writer is busy', true);
      const cached = this.instances.get(repoId);
      if (cached) { cached.graph.unwatch(); cached.graph.close(); this.instances.delete(repoId); }
      this.writes.add(repoId);
      return this.runTask(request, true);
    }
    const graph = await this.graph(repoId);
    if (op === 'health') {
      const stats = graph.getStats(); const build = graph.getIndexBuildInfo(); const pending = graph.getPendingFiles();
      const degraded = graph.isWatcherDegraded(); const inactive = !graph.isWatching();
      const warnings = degraded ? [graph.getWatcherDegradedReason() || 'watcher degraded'] : inactive ? ['watcher disabled; sync before relying on freshness'] : [];
      if (this.recoveryRequired.has(repoId)) warnings.unshift('interrupted write requires a successful recovery sync');
      const staleBuild = graph.isIndexStale() && !this.successfullySynced.has(repoId);
      return { provider: 'codegraph', indexed: stats.nodeCount > 0, freshness: this.recoveryRequired.has(repoId) ? 'stale' : degraded ? 'degraded' : pending.length ? 'pending_sync' : staleBuild || inactive ? 'stale' : 'fresh', indexed_at: graph.getLastIndexedAt() ? new Date(graph.getLastIndexedAt()).toISOString() : null, graph_version: `${build.version || packageInfo.version}:${build.extractionVersion ?? 'unknown'}`, files: stats.fileCount, nodes: stats.nodeCount, edges: stats.edgeCount, warnings, watcher_active: graph.isWatching(), engine_version: packageInfo.version };
    }
    return this.runTask(request, false);
  }

  close() { for (const state of this.active.values()) state.worker.terminate(); for (const { graph } of this.instances.values()) { graph.unwatch(); graph.close(); } }
}
