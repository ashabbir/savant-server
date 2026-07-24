import fs from 'node:fs';
import codegraphPackage from '@colbymchenry/codegraph';
import packageInfo from '@colbymchenry/codegraph/package.json' with { type: 'json' };
import { BridgeError } from './protocol.js';
import { canonicalizeBaseRoots, registerRepo, getRepoRoot } from './registry.js';
import { formatNode, formatEdge } from './formatters.js';
import { ensureWatcherIfPossible, buildHealthResponse } from './health.js';
import { runTask, cancelTask, handleWorkerMessage } from './worker.js';

const { CodeGraph } = codegraphPackage;

const DEFAULT_TIMEOUT_MS = 15000;
const DEFAULT_CANCEL_GRACE_MS = 500;
const PROTOCOL_VERSION = 1;

export class Operations {
  constructor({ baseRoots, maxRepositories = 8, timeoutMs = DEFAULT_TIMEOUT_MS, writeTimeoutMs = null, cancelGraceMs = DEFAULT_CANCEL_GRACE_MS }) {
    this.baseRoots = canonicalizeBaseRoots(baseRoots);
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
    return registerRepo(this.baseRoots, this.registrations, repoId, root);
  }

  root(repoId) {
    return getRepoRoot(this.registrations, repoId);
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
    return formatNode(repoId, node);
  }

  edge(repoId, edge) {
    return formatEdge(repoId, edge);
  }

  runTask(request, write = false) {
    return runTask(this, request, write);
  }

  handleWorkerMessage(request, state, message) {
    return handleWorkerMessage(this, request, state, message);
  }

  async cancel(requestId) {
    return cancelTask(this, requestId);
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

  async health(repoId) {
    const graph = await this.graph(repoId);
    ensureWatcherIfPossible(graph);
    return buildHealthResponse(repoId, graph, this.recoveryRequired, this.successfullySynced);
  }

  async runReadTask(request) {
    await this.graph(request.repo_id);
    return this.runTask(request, false);
  }

  async execute(request) {
    const handlers = {
      ping: req => this.ping(),
      cancel: req => this.cancel(req.params?.request_id),
      register: req => this.register(req.repo_id, req.params?.root),
      ensure_index: req => this.ensureIndex(req),
      health: req => this.health(req.repo_id)
    };
    const handler = handlers[request.op];
    if (handler) return handler(request);
    return this.runReadTask(request);
  }

  close() {
    for (const state of this.active.values()) state.worker.terminate();
    for (const { graph } of this.instances.values()) {
      graph.unwatch();
      graph.close();
    }
  }
}
