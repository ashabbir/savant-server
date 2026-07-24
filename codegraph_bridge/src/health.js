import packageInfo from '@colbymchenry/codegraph/package.json' with { type: 'json' };

export function ensureWatcherIfPossible(graph) {
  if (!graph.isWatching() && !graph.isWatcherDegraded()) {
    try { graph.watch(); } catch {}
  }
}

export function buildHealthResponse(repoId, graph, recoveryRequired, successfullySynced) {
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

  if (recoveryRequired.has(repoId)) {
    warnings.unshift('interrupted write requires a successful recovery sync');
  }

  const staleBuild = graph.isIndexStale() && !successfullySynced.has(repoId);
  const freshness = recoveryRequired.has(repoId)
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
