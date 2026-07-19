import fs from 'node:fs';
import path from 'node:path';
import { Worker } from 'node:worker_threads';
import codegraphPackage from '@colbymchenry/codegraph';
import packageInfo from '@colbymchenry/codegraph/package.json' with { type: 'json' };
import { BridgeError } from './protocol.js';

const { CodeGraph } = codegraphPackage;

const DEFAULT_TIMEOUT_MS = 15000;
const DEFAULT_CANCEL_GRACE_MS = 500;
const PROTOCOL_VERSION = 1;

const namespaced = id => `codegraph:${id}`;
const rawId = id => String(id || '').replace(/^codegraph:/, '');

export class Operations {
  constructor({ baseRoots, maxRepositories = 8, timeoutMs = DEFAULT_TIMEOUT_MS, writeTimeoutMs = null, cancelGraceMs = DEFAULT_CANCEL_GRACE_MS }) {
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

  formatLocation(repoId, node) {
    return {
      repo_id: repoId,
      file_path: node.filePath,
      start_line: node.startLine,
      end_line: node.endLine,
      start_column: node.startColumn ?? null,
      end_column: node.endColumn ?? null
    };
  }

  formatFlags(node) {
    return {
      exported: Boolean(node.isExported),
      async: Boolean(node.isAsync),
      static: Boolean(node.isStatic),
      abstract: Boolean(node.isAbstract)
    };
  }

  node(repoId, node) {
    return {
      id: namespaced(node.id),
      name: node.name,
      qualified_name: node.qualifiedName || null,
      kind: node.kind,
      language: node.language || null,
      location: this.formatLocation(repoId, node),
      signature: node.signature || null,
      docstring: node.docstring || null,
      flags: this.formatFlags(node),
      metadata: { provider_kind: node.kind, visibility: node.visibility || null }
    };
  }

  formatEdgeLocation(repoId, edge) {
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

  edge(repoId, edge) {
    const provenance = edge.provenance === 'heuristic' ? 'heuristic' : 'static';
    return {
      source_id: namespaced(edge.source),
      target_id: namespaced(edge.target),
      kind: edge.kind,
      location: this.formatEdgeLocation(repoId, edge),
      provenance,
      confidence: null,
      metadata: edge.metadata || {}
    };
  }

  createWorkerState(request, write) {
    const worker = new Worker(new URL('./task-worker.js', import.meta.url), {
      workerData: {
        operation: request.op,
        root: this.root(request.repo_id),
        repoId: request.repo_id,
        params: request.params || {}
      }
    });
    const state = { worker, write, repoId: request.repo_id, settled: false };
    this.active.set(request.id, state);
    return state;
  }

  finishTask(requestId, state, callback, value) {
    if (state.settled) return;
    state.settled = true;
    clearTimeout(state.timer);
    this.active.delete(requestId);
    if (state.write) this.writes.delete(state.repoId);
    callback(value);
  }

  markWriteRecoveryRequired(repoId) {
    this.recoveryRequired.add(repoId);
  }

  async terminateWorkerWithGrace(state) {
    state.terminating = true;
    state.worker.postMessage('cancel');
    await new Promise(done => setTimeout(done, this.cancelGraceMs));
    await state.worker.terminate();
  }

  async handleTaskTimeout(request, state) {
    await this.terminateWorkerWithGrace(state);
    if (state.write) this.markWriteRecoveryRequired(state.repoId);
    state.finish(state.reject, new BridgeError('TIMEOUT', 'operation timed out and worker was replaced', true));
  }

  async startWatching(repoId) {
    try {
      const watcher = await this.graph(repoId);
      return watcher.watch();
    } catch {
      return false;
    }
  }

  async completeSuccessfulTask(request, state, result) {
    if (!state.write) return result;
    this.recoveryRequired.delete(state.repoId);
    this.successfullySynced.add(state.repoId);
    if (request.params?.watch !== false) result.watching = await this.startWatching(state.repoId);
    return result;
  }

  async handleWorkerMessage(request, state, message) {
    state.completing = true;
    state.worker.terminate();
    if (message.ok) {
      const result = await this.completeSuccessfulTask(request, state, message.result);
      state.finish(state.resolve, result);
      return;
    }
    state.finish(state.reject, new BridgeError(message.error?.code || 'INTERNAL', message.error?.message || 'worker error'));
  }

  runTask(request, write = false) {
    const state = this.createWorkerState(request, write);
    const timeout = write ? (this.writeTimeoutMs ?? this.timeoutMs) : this.timeoutMs;
    return new Promise((resolve, reject) => {
      state.resolve = resolve;
      state.reject = reject;
      state.finish = (callback, value) => this.finishTask(request.id, state, callback, value);

      state.timer = setTimeout(() => this.handleTaskTimeout(request, state), timeout);

      state.worker.once('message', message => this.handleWorkerMessage(request, state, message));
      state.worker.once('error', error => {
        if (write) this.markWriteRecoveryRequired(request.repo_id);
        state.finish(reject, new BridgeError('INTERNAL', error.message));
      });
      state.worker.once('exit', code => {
        if (!state.settled && !state.terminating && !state.completing && code !== 0) {
          if (write) this.markWriteRecoveryRequired(request.repo_id);
          state.finish(reject, new BridgeError('ENGINE_UNAVAILABLE', 'operation worker exited', true));
        }
      });
    });
  }

  async cancel(requestId) {
    const state = this.active.get(requestId);
    if (!state) return { cancelled: false };
    await this.terminateWorkerWithGrace(state);
    if (state.write) this.markWriteRecoveryRequired(state.repoId);
    state.finish(state.reject, new BridgeError('TIMEOUT', 'operation cancelled and worker was replaced', true));
    return { cancelled: true };
  }

  ping() {
    return { engine_version: packageInfo.version, protocol_version: PROTOCOL_VERSION };
  }

  assertWriterAvailable(repoId) {
    if (this.writes.has(repoId)) throw new BridgeError('BUSY', 'repository writer is busy', true);
  }

  closeCachedGraph(repoId) {
    const cached = this.instances.get(repoId);
    if (cached) {
      cached.graph.unwatch();
      cached.graph.close();
      this.instances.delete(repoId);
    }
  }

  async ensureIndex(request) {
    const repoId = request.repo_id;
    this.assertWriterAvailable(repoId);
    this.closeCachedGraph(repoId);
    this.writes.add(repoId);
    return this.runTask(request, true);
  }

  ensureWatcherIfPossible(graph) {
    if (!graph.isWatching() && !graph.isWatcherDegraded()) {
      try { graph.watch(); } catch {}
    }
  }

  buildHealthResponse(repoId, graph) {
    const stats = graph.getStats();
    const build = graph.getIndexBuildInfo();
    const pending = graph.getPendingFiles();
    const degraded = graph.isWatcherDegraded();
    const inactive = !graph.isWatching();

    const warnings = degraded
      ? [graph.getWatcherDegradedReason() || 'watcher degraded']
      : inactive
      ? ['watcher disabled; sync before relying on freshness']
      : [];

    if (this.recoveryRequired.has(repoId)) {
      warnings.unshift('interrupted write requires a successful recovery sync');
    }

    const staleBuild = graph.isIndexStale() && !this.successfullySynced.has(repoId);
    const freshness = this.recoveryRequired.has(repoId)
      ? 'stale'
      : degraded
      ? 'degraded'
      : pending.length
      ? 'pending_sync'
      : staleBuild || inactive
      ? 'stale'
      : 'fresh';

    return {
      provider: 'codegraph',
      indexed: stats.nodeCount > 0,
      freshness,
      indexed_at: graph.getLastIndexedAt() ? new Date(graph.getLastIndexedAt()).toISOString() : null,
      graph_version: `${build.version || packageInfo.version}:${build.extractionVersion ?? 'unknown'}`,
      files: stats.fileCount,
      nodes: stats.nodeCount,
      edges: stats.edgeCount,
      warnings,
      watcher_active: graph.isWatching(),
      engine_version: packageInfo.version
    };
  }

  async health(repoId) {
    const graph = await this.graph(repoId);
    this.ensureWatcherIfPossible(graph);
    return this.buildHealthResponse(repoId, graph);
  }

  async runReadTask(request) {
    await this.graph(request.repo_id);
    return this.runTask(request, false);
  }

  async execute(request) {
    switch (request.op) {
      case 'ping': return this.ping();
      case 'cancel': return this.cancel(request.params?.request_id);
      case 'register': return this.register(request.repo_id, request.params?.root);
      case 'ensure_index': return this.ensureIndex(request);
      case 'health': return this.health(request.repo_id);
      default: return this.runReadTask(request);
    }
  }

  close() {
    for (const state of this.active.values()) state.worker.terminate();
    for (const { graph } of this.instances.values()) {
      graph.unwatch();
      graph.close();
    }
  }
}
