"""Compatibility adapter for Context AST and Graphify data."""

from pathlib import PurePosixPath

from .contracts import (
    CodeEdge,
    CodeSymbol,
    ExploreResult,
    Freshness,
    ProviderHealth,
    Provenance,
    SymbolLocation,
)
from .provider import CodeIntelligenceError, ErrorCategory, ProviderCapabilities


class LegacyCodeIntelligenceProvider:
    name = "legacy"
    capabilities = ProviderCapabilities(
        indexing=False,
        symbol_search=True,
        explore=True,
        symbol_lookup=False,
        callers=False,
        callees=False,
        impact=False,
        neighbors=False,
    )

    def __init__(self, *, context_db, graphify_db):
        self.context_db = context_db
        self.graphify_db = graphify_db

    @staticmethod
    def _repo_id(repo):
        return str(repo["repo_id"] if isinstance(repo, dict) else repo.repo_id)

    @staticmethod
    def _safe_path(path):
        normalized = str(path or "unknown").replace("\\", "/")
        parsed = PurePosixPath(normalized)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise CodeIntelligenceError(ErrorCategory.PATH_REFUSED, "legacy result path escaped repository root")
        return normalized

    def search_symbols(self, repo, query, filters, limit):
        repo_id = self._repo_id(repo)
        rows = self.context_db.search_ast_nodes(query, repo_filter=repo_id)
        symbols = []
        for row in rows[:limit]:
            kind = str(row.get("node_type") or "unknown")
            symbols.append(CodeSymbol(
                id=f"legacy:ast:{row['id']}",
                name=str(row.get("name") or ""),
                qualified_name=row.get("qualified_name"),
                kind=kind,
                language=row.get("language"),
                location=SymbolLocation(
                    repo_id=repo_id,
                    file_path=self._safe_path(row.get("rel_path") or row.get("file_path")),
                    start_line=max(1, int(row.get("start_line") or 1)),
                    end_line=max(1, int(row.get("end_line") or row.get("start_line") or 1)),
                    start_column=row.get("start_column"),
                    end_column=row.get("end_column"),
                ),
                signature=row.get("signature"),
                docstring=row.get("docstring"),
                flags=row.get("flags") or {},
                metadata={"provider_kind": kind},
            ))
        return symbols

    def explore(self, repo, query, max_files, include_source=True):
        repo_id = self._repo_id(repo)
        rows = self.graphify_db.search(query, workspace_id=repo_id, limit=max_files)
        symbols, edges = [], []
        for row in rows:
            metadata = dict(row.get("metadata") or {})
            node_id = str(row.get("node_id") or row.get("id"))
            kind = str(row.get("node_type") or "unknown")
            symbols.append(CodeSymbol(
                id=f"legacy:graphify:{node_id}",
                name=str(row.get("title") or row.get("name") or node_id),
                qualified_name=metadata.get("qualified_name"),
                kind=kind,
                language=metadata.get("language"),
                location=SymbolLocation(
                    repo_id=repo_id,
                    file_path=self._safe_path(metadata.get("path") or metadata.get("file_path")),
                    start_line=max(1, int(metadata.get("start_line") or 1)),
                    end_line=max(1, int(metadata.get("end_line") or metadata.get("start_line") or 1)),
                    start_column=metadata.get("start_column"),
                    end_column=metadata.get("end_column"),
                ),
                signature=metadata.get("signature"),
                docstring=None,
                flags=metadata.get("flags") or {},
                metadata={**metadata, "provider_kind": kind, **({"source": row.get("content")} if include_source else {})},
            ))
            for edge in row.get("edges") or []:
                edges.append(CodeEdge(
                    source_id=f"legacy:graphify:{edge['source_id']}",
                    target_id=f"legacy:graphify:{edge['target_id']}",
                    kind=str(edge.get("edge_type") or edge.get("kind") or "unknown"),
                    provenance=Provenance.LEGACY,
                    confidence=None,
                    metadata={key: value for key, value in edge.items() if key not in {"source_id", "target_id", "edge_type", "kind"}},
                ))
        return ExploreResult(symbols=symbols, edges=edges, provider=self.name)

    def health(self, repo):
        """Project legacy index state into the mandatory provider health contract."""
        repo_id = self._repo_id(repo)
        repo_record = self.context_db.get_repo(repo_id)
        ast_nodes = self.context_db.list_ast_nodes(repo_filter=repo_id)
        files = self.context_db.list_code_files(repo_filter=repo_id)
        graph_stats = self.graphify_db.get_stats(workspace_id=repo_id)
        ast_count = len(ast_nodes)
        graph_node_count = int(graph_stats.get("node_count") or 0)
        indexed = ast_count > 0 or graph_node_count > 0
        warnings = []
        if not indexed:
            warnings.append("legacy structural index is not available")
        elif graph_node_count == 0:
            warnings.append("legacy Graphify graph is unavailable; AST symbols only")
        return ProviderHealth(
            provider=self.name,
            indexed=indexed,
            freshness=Freshness.FRESH if indexed else Freshness.UNAVAILABLE,
            indexed_at=repo_record.get("indexed_at") if repo_record else None,
            graph_version=None,
            files=len(files),
            # Graphify and AST can describe the same symbols, so do not add the
            # counts. Prefer the richer graph count and expose AST count when it
            # is the only structural index available.
            nodes=graph_node_count if graph_node_count else ast_count,
            edges=int(graph_stats.get("edge_count") or 0),
            warnings=warnings,
        )

    def _unsupported(self, *_args, **_kwargs):
        raise CodeIntelligenceError(ErrorCategory.UNSUPPORTED, "operation unsupported by legacy provider")

    ensure_index = get_symbol = get_callers = get_callees = get_impact = get_neighbors = _unsupported
