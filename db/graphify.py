"""GraphifyDB — PostgreSQL backend for the codebase-level Graphify integration."""

import json
import logging
from db.base import _now, _row_to_dict as _base_row, _rows_to_dicts
from postgres_client import get_connection, release_connection

logger = logging.getLogger(__name__)

def _row_to_dict(row):
    return _base_row(row, json_fields={"metadata": {}})

class GraphifyDB:
    """Operations on graphify_nodes and graphify_edges tables."""

    @staticmethod
    def import_graph(workspace_id: str, graph_data: dict, meta_data: dict = None) -> dict:
        """Imports Graphify nodes and edges into the database.

        Clears existing graphify data for the workspace first to ensure a clean state.
        """
        if not workspace_id:
            raise ValueError("workspace_id is required")

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges") or graph_data.get("links") or []

        # Index metadata by path/key if provided
        file_meta_map = {}
        if isinstance(meta_data, dict):
            file_meta_map = meta_data

        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                # 1. Delete existing graphify edges and nodes for this workspace
                cur.execute("DELETE FROM graphify_edges WHERE workspace_id = %s", (workspace_id,))
                cur.execute("DELETE FROM graphify_nodes WHERE workspace_id = %s", (workspace_id,))

                # 2. Insert nodes
                node_ids = set()
                for node in nodes:
                    nid = str(node.get("id") or node.get("node_id") or "")
                    if not nid:
                        continue
                    title = str(node.get("title") or node.get("label") or node.get("name") or nid)
                    ntype = str(node.get("type") or node.get("node_type") or "")
                    if not ntype:
                        if any(title.endswith(ext) for ext in [".js", ".jsx", ".ts", ".tsx", ".py", ".json", ".css", ".md", ".html", ".sh", ".yml", ".yaml"]):
                            ntype = "file"
                        elif title.endswith("()"):
                            ntype = "function"
                        elif title.isupper() and "_" in title:
                            ntype = "constant"
                        elif title[0].isupper() if title else False:
                            ntype = "class"
                        else:
                            ntype = "variable"
                    content = str(node.get("content") or node.get("description") or "")
                    
                    # Store any extra fields in metadata
                    metadata = node.get("metadata")
                    if not isinstance(metadata, dict):
                        metadata = {}
                    for k, v in node.items():
                        if k not in ["id", "node_id", "type", "node_type", "title", "label", "name", "content", "description", "metadata"]:
                            metadata[k] = v

                    # Merge manifest meta if present
                    if nid in file_meta_map:
                        metadata.update(file_meta_map[nid])
                    elif title in file_meta_map:
                        metadata.update(file_meta_map[title])

                    cur.execute(
                        """INSERT INTO graphify_nodes
                           (node_id, workspace_id, node_type, title, content, metadata, created_at, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (workspace_id, node_id) DO UPDATE SET
                           node_type = EXCLUDED.node_type,
                           title = EXCLUDED.title,
                           content = EXCLUDED.content,
                           metadata = EXCLUDED.metadata,
                           updated_at = EXCLUDED.updated_at""",
                        (nid, workspace_id, ntype, title, content, json.dumps(metadata), now, now),
                    )
                    node_ids.add(nid)

                # 3. Insert edges
                edge_counter = 0
                for edge in edges:
                    eid = str(edge.get("id") or edge.get("edge_id") or "")
                    source = str(edge.get("source") or edge.get("source_id") or "")
                    target = str(edge.get("target") or edge.get("target_id") or "")
                    
                    # Ensure both source and target nodes exist (or were imported) to avoid FK violations
                    if not source or not target or source not in node_ids or target not in node_ids:
                        continue

                    etype = str(edge.get("type") or edge.get("edge_type") or edge.get("relation") or "depends_on")
                    weight = float(edge.get("weight") or 1.0)
                    label = str(edge.get("label") or etype)

                    if not eid:
                        edge_counter += 1
                        eid = f"gfe_{workspace_id}_{edge_counter}_{now}"

                    cur.execute(
                        """INSERT INTO graphify_edges
                           (edge_id, workspace_id, source_id, target_id, edge_type, weight, label, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (workspace_id, source_id, target_id, edge_type) DO UPDATE SET
                           weight = EXCLUDED.weight,
                           label = EXCLUDED.label""",
                        (eid, workspace_id, source, target, etype, weight, label, now),
                    )

                # 4. Save raw manifest/metadata to general metadata store if present
                if file_meta_map:
                    cur.execute(
                        """INSERT INTO meta (key, value)
                           VALUES (%s, %s)
                           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                        (f"graphify_meta_{workspace_id}", json.dumps(file_meta_map))
                    )

            conn.commit()
            return {"status": "success", "nodes_imported": len(node_ids), "edges_imported": edge_counter, "has_metadata": len(file_meta_map) > 0}
        except Exception as e:
            conn.rollback()
            logger.error(f"Error importing Graphify graph: {e}")
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def get_stats(workspace_id: str) -> dict:
        """Returns node and edge counts grouped by node_type and edge_type for the workspace."""
        if not workspace_id:
            return {"nodes": {}, "edges": {}, "total_nodes": 0, "total_edges": 0}

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # Group nodes by type
                cur.execute(
                    "SELECT node_type, COUNT(*) as cnt FROM graphify_nodes WHERE workspace_id = %s GROUP BY node_type",
                    (workspace_id,),
                )
                node_rows = cur.fetchall()
                node_stats = {row["node_type"]: row["cnt"] for row in node_rows}
                total_nodes = sum(node_stats.values())

                # Group edges by type
                cur.execute(
                    "SELECT edge_type, COUNT(*) as cnt FROM graphify_edges WHERE workspace_id = %s GROUP BY edge_type",
                    (workspace_id,),
                )
                edge_rows = cur.fetchall()
                edge_stats = {row["edge_type"]: row["cnt"] for row in edge_rows}
                total_edges = sum(edge_stats.values())

                return {
                    "nodes": node_stats,
                    "edges": edge_stats,
                    "total_nodes": total_nodes,
                    "total_edges": total_edges,
                }
        finally:
            release_connection(conn)

    @staticmethod
    def search(query: str, workspace_id: str = None, limit: int = 20) -> list[dict]:
        """Performs a text search on Graphify nodes, optionally scoped to a workspace."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if workspace_id:
                    cur.execute(
                        """SELECT * FROM graphify_nodes
                           WHERE workspace_id = %s AND (title ILIKE %s OR content ILIKE %s)
                           ORDER BY created_at DESC LIMIT %s""",
                        (workspace_id, f"%{query}%", f"%{query}%", limit),
                    )
                else:
                    cur.execute(
                        """SELECT * FROM graphify_nodes
                           WHERE title ILIKE %s OR content ILIKE %s
                           ORDER BY created_at DESC LIMIT %s""",
                        (f"%{query}%", f"%{query}%", limit),
                    )
                rows = cur.fetchall()
                nodes = [_row_to_dict(r) for r in rows]

                # Fetch connected edges for each node to provide graph context
                for node in nodes:
                    nid = node["node_id"]
                    cur.execute(
                        "SELECT * FROM graphify_edges WHERE source_id = %s OR target_id = %s",
                        (nid, nid)
                    )
                    edge_rows = cur.fetchall()
                    node["edges"] = [_row_to_dict(e) for e in edge_rows]
                return nodes
        finally:
            release_connection(conn)

    @staticmethod
    def get_neighbors(workspace_id: str, node_id: str) -> dict:
        """Fetch a node, all its direct neighbors, and the connecting edges."""
        if not workspace_id or not node_id:
            return {"nodes": [], "edges": []}

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM graphify_edges
                       WHERE workspace_id = %s AND (source_id = %s OR target_id = %s)""",
                    (workspace_id, node_id, node_id)
                )
                edges = [_row_to_dict(r) for r in cur.fetchall()]

                neighbor_ids = {node_id}
                for edge in edges:
                    neighbor_ids.add(edge["source_id"])
                    neighbor_ids.add(edge["target_id"])

                nodes = []
                if neighbor_ids:
                    placeholders = ", ".join(["%s"] * len(neighbor_ids))
                    cur.execute(
                        f"SELECT * FROM graphify_nodes WHERE workspace_id = %s AND node_id IN ({placeholders})",
                        [workspace_id] + list(neighbor_ids)
                    )
                    nodes = [_row_to_dict(r) for r in cur.fetchall()]

                return {"nodes": nodes, "edges": edges}
        finally:
            release_connection(conn)

    @staticmethod
    def get_main_entities(workspace_id: str, limit: int = 30) -> dict:
        """Fetch the most connected nodes (main entities) and their edges to initialize the graph."""
        if not workspace_id:
            return {"nodes": [], "edges": []}

        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # Find nodes with highest degree
                cur.execute(
                    """SELECT node_id, COUNT(*) as degree FROM (
                           SELECT source_id as node_id FROM graphify_edges WHERE workspace_id = %s
                           UNION ALL
                           SELECT target_id as node_id FROM graphify_edges WHERE workspace_id = %s
                       ) t GROUP BY node_id ORDER BY degree DESC LIMIT %s""",
                    (workspace_id, workspace_id, limit)
                )
                rows = cur.fetchall()
                node_ids = [r["node_id"] for r in rows]

                if not node_ids:
                    cur.execute(
                        "SELECT node_id FROM graphify_nodes WHERE workspace_id = %s LIMIT %s",
                        (workspace_id, limit)
                    )
                    node_ids = [r["node_id"] for r in cur.fetchall()]

                nodes = []
                edges = []
                if node_ids:
                    placeholders = ", ".join(["%s"] * len(node_ids))
                    cur.execute(
                        f"SELECT * FROM graphify_nodes WHERE workspace_id = %s AND node_id IN ({placeholders})",
                        [workspace_id] + node_ids
                    )
                    nodes = [_row_to_dict(r) for r in cur.fetchall()]

                    cur.execute(
                        f"""SELECT * FROM graphify_edges
                           WHERE workspace_id = %s AND source_id IN ({placeholders}) AND target_id IN ({placeholders})""",
                        [workspace_id] + node_ids + node_ids
                    )
                    edges = [_row_to_dict(r) for r in cur.fetchall()]

                return {"nodes": nodes, "edges": edges}
        finally:
            release_connection(conn)
