"""Shared one-level code-graph impact-surface extraction."""


def _graph_nodes(results: dict):
    graph_results = results.get("code_graph_search", {})
    if not isinstance(graph_results, dict):
        return
    for node_list in graph_results.values():
        if isinstance(node_list, list):
            yield from (node for node in node_list if isinstance(node, dict))


def _endpoint_matches(endpoint: str, node_id: str, symbols: list[str]) -> bool:
    if node_id and (endpoint == node_id or node_id in endpoint):
        return True
    endpoint_lower = endpoint.lower()
    return any(symbol.lower() in endpoint_lower for symbol in symbols if symbol)


def _edge_entries(node: dict, edge: dict, symbols: list[str]) -> tuple[dict | None, dict | None]:
    source = str(edge.get("source_id") or "")
    target = str(edge.get("target_id") or "")
    relationship = str(edge.get("label") or edge.get("edge_type") or "relates_to")
    node_id = str(node.get("node_id") or "")
    title = str(node.get("title") or node.get("norm_label") or node_id)
    upstream = None
    downstream = None
    if _endpoint_matches(target, node_id, symbols):
        upstream = {
            "upstream_caller": source,
            "relationship": relationship,
            "target_symbol": title,
        }
    if _endpoint_matches(source, node_id, symbols):
        downstream = {
            "source_symbol": title,
            "relationship": relationship,
            "downstream_dependency": target,
        }
    return upstream, downstream


def _append_unique(entries: list[dict], seen: set[tuple], key: tuple, entry: dict | None) -> None:
    if entry is not None and key not in seen:
        seen.add(key)
        entries.append(entry)


def _collect_entries(results: dict, symbols: list[str]) -> tuple[list[dict], list[dict]]:
    upstream = []
    downstream = []
    seen_up = set()
    seen_down = set()
    for node in _graph_nodes(results):
        edges = node.get("edges")
        if not isinstance(edges, list):
            continue
        for edge in (item for item in edges if isinstance(item, dict)):
            upstream_entry, downstream_entry = _edge_entries(node, edge, symbols)
            edge_key = (
                str(edge.get("source_id") or ""),
                str(edge.get("target_id") or ""),
                str(edge.get("label") or edge.get("edge_type") or "relates_to"),
            )
            _append_unique(upstream, seen_up, edge_key, upstream_entry)
            _append_unique(downstream, seen_down, edge_key, downstream_entry)
    return upstream, downstream


def build_impact_surface(results: dict, top_symbols: list[str], top_files: set[str]) -> dict:
    """Extract one graph level above and below the matched symbols."""
    upstream, downstream = _collect_entries(results, top_symbols)
    return {
        "summary": (
            f"Impact Surface Analysis: Identified {len(upstream)} upstream callers/importers (1 level up) "
            f"and {len(downstream)} downstream dependencies/callees (1 level down). "
            "AI AGENTS MUST evaluate these impact surfaces to prevent breaking changes when modifying code."
        ),
        "upstream_dependencies_1_level_up": upstream[:10],
        "downstream_impacts_1_level_down": downstream[:10],
        "affected_files": sorted(top_files)[:8],
    }
