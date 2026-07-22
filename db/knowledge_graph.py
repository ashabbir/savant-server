"""KnowledgeGraphDB — PostgreSQL backend for the brain-like knowledge graph."""

import json
import threading
from datetime import datetime, timezone
from db.base import _now, _row_to_dict as _base_row
from postgres_client import get_connection, release_connection


_counter = 0
_counter_lock = threading.Lock()


def _gen_id(prefix="kgn"):
    global _counter
    with _counter_lock:
        _counter += 1
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        return f"{prefix}_{ts}_{_counter}"


def _row_to_dict(row):
    return _base_row(row, json_fields={"metadata": {}})


def _fetch_graph_nodes(cur, columns: str, node_type: str, limit: int, include_staged: bool):
    status_clause = "" if include_staged else " AND status = 'committed'"
    if node_type:
        cur.execute(
            f"SELECT {columns} FROM kg_nodes WHERE node_type = %s{status_clause} "
            "ORDER BY created_at DESC LIMIT %s",
            (node_type, limit),
        )
    else:
        status_where = "" if include_staged else " WHERE status = 'committed'"
        cur.execute(
            f"SELECT {columns} FROM kg_nodes{status_where} ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
    return list(cur.fetchall())


def _fetch_graph_edges(cur, node_ids: set[str], include_connected: bool):
    if not node_ids:
        return []
    operator = "OR" if include_connected else "AND"
    cur.execute(
        f"SELECT * FROM kg_edges WHERE source_id = ANY(%s) {operator} target_id = ANY(%s)",
        (list(node_ids), list(node_ids)),
    )
    return list(cur.fetchall())


def _expand_connected_nodes(cur, nodes: list, edges: list, columns: str, include_staged: bool):
    node_ids = {dict(node)["node_id"] for node in nodes}
    connected_ids = {
        endpoint
        for edge in edges
        for endpoint in (dict(edge)["source_id"], dict(edge)["target_id"])
    }
    missing_ids = connected_ids - node_ids
    if not missing_ids:
        return nodes, node_ids
    status_clause = "" if include_staged else " AND status = 'committed'"
    cur.execute(
        f"SELECT {columns} FROM kg_nodes WHERE node_id = ANY(%s){status_clause}",
        (list(missing_ids),),
    )
    extra_nodes = list(cur.fetchall())
    nodes.extend(extra_nodes)
    node_ids.update(dict(node)["node_id"] for node in extra_nodes)
    return nodes, node_ids


def _edges_between_nodes(edges: list, node_ids: set[str]):
    return [
        edge for edge in edges
        if dict(edge)["source_id"] in node_ids and dict(edge)["target_id"] in node_ids
    ]


def _merge_content(survivor: dict, absorbed_nodes: list[dict]) -> str:
    parts = []
    for node in [survivor, *absorbed_nodes]:
        content = (node.get("content") or "").strip()
        if content and content not in parts:
            parts.append(content)
    return "\n\n---\n\n".join(parts)


def _merge_metadata(survivor: dict, absorbed_nodes: list[dict]) -> dict:
    merged = dict(survivor.get("metadata") or {})
    files = set(merged.get("files") or [])
    for node in absorbed_nodes:
        metadata = node.get("metadata") or {}
        files.update(metadata.get("files") or [])
        for key in ("repo", "source", "workspace_id"):
            if metadata.get(key) and not merged.get(key):
                merged[key] = metadata[key]
    if files:
        merged["files"] = sorted(files)
    return merged


def _fetch_nodes(conn, node_ids: list[str]) -> list[dict]:
    if not node_ids:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM kg_nodes WHERE node_id = ANY(%s)", (node_ids,))
        by_id = {row["node_id"]: _row_to_dict(row) for row in cur.fetchall()}
    return [by_id[node_id] for node_id in node_ids if node_id in by_id]


def _resolve_edge_conflict(cur, edge: dict, survivor_id: str, absorbed_ids: set[str], direction: str) -> None:
    external_id = edge["target_id"] if direction == "outgoing" else edge["source_id"]
    if external_id in absorbed_ids or external_id == survivor_id:
        cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (edge["edge_id"],))
        return
    source_id = survivor_id if direction == "outgoing" else external_id
    target_id = external_id if direction == "outgoing" else survivor_id
    cur.execute(
        "SELECT edge_id, weight FROM kg_edges WHERE source_id = %s AND target_id = %s AND edge_type = %s",
        (source_id, target_id, edge["edge_type"]),
    )
    existing = cur.fetchone()
    if existing:
        if edge["weight"] > existing["weight"]:
            cur.execute("UPDATE kg_edges SET weight = %s WHERE edge_id = %s", (edge["weight"], existing["edge_id"]))
        cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (edge["edge_id"],))
        return
    column = "source_id" if direction == "outgoing" else "target_id"
    cur.execute(f"UPDATE kg_edges SET {column} = %s WHERE edge_id = %s", (survivor_id, edge["edge_id"]))


def _rewire_absorbed_edges(cur, survivor_id: str, absorbed_ids: list[str]) -> None:
    absorbed_set = set(absorbed_ids)
    edge_columns = "edge_id, source_id, target_id, edge_type, weight"
    for node_id in absorbed_ids:
        cur.execute(f"SELECT {edge_columns} FROM kg_edges WHERE source_id = %s", (node_id,))
        for edge in cur.fetchall():
            _resolve_edge_conflict(cur, edge, survivor_id, absorbed_set, "outgoing")
        cur.execute(f"SELECT {edge_columns} FROM kg_edges WHERE target_id = %s", (node_id,))
        for edge in cur.fetchall():
            _resolve_edge_conflict(cur, edge, survivor_id, absorbed_set, "incoming")


VALID_NODE_TYPES = {"insight", "project", "session", "concept", "repo",
                   "client", "domain", "service", "library", "technology", "issue", "person",
                   "operation", "organization"}
VALID_EDGE_TYPES = {"relates_to", "learned_from", "applies_to", "uses",
                   "evolved_from", "contributed_to", "part_of",
                   "integrates_with", "depends_on", "built_with"}


class KnowledgeGraphDB:
    """Graph-based knowledge store with nodes and typed edges."""

    @staticmethod
    def _get_node_with_conn(node_id: str, conn) -> dict | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM kg_nodes WHERE node_id = %s", (node_id,))
            row = cur.fetchone()
            if not row:
                return None
            node = _row_to_dict(row)
            
            cur.execute("SELECT * FROM kg_edges WHERE source_id = %s OR target_id = %s", (node_id, node_id))
            edges = cur.fetchall()
            node["edges"] = [_row_to_dict(e) for e in edges]
        return node

    # -----------------------------------------------------------------------
    # Nodes
    # -----------------------------------------------------------------------

    @staticmethod
    def create_node(node: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            node_id = node.get("node_id") or _gen_id("kgn")
            node_type = node.get("node_type", "insight")
            title = node.get("title", "").strip()
            if not title:
                raise ValueError("title is required")
            if node_type not in VALID_NODE_TYPES:
                raise ValueError(f"Invalid node_type: {node_type}. Must be one of {VALID_NODE_TYPES}")
            content = node.get("content", "")
            metadata = node.get("metadata", {})
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            status = node.get("status", "staged")

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kg_nodes (node_id, node_type, title, content, metadata, status, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (node_id, node_type, title, content, json.dumps(metadata), status, now, now)
                )
            conn.commit()
            return KnowledgeGraphDB._get_node_with_conn(node_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def get_node(node_id: str) -> dict | None:
        conn = get_connection()
        try:
            return KnowledgeGraphDB._get_node_with_conn(node_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def update_node(node_id: str, updates: dict) -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM kg_nodes WHERE node_id = %s", (node_id,))
                existing = cur.fetchone()
            if not existing:
                return None
            now = _now()
            fields = []
            values = []
            for key in ("title", "content", "node_type"):
                if key in updates and updates[key]:
                    fields.append(f"{key} = %s")
                    values.append(updates[key])
            if "metadata" in updates:
                meta = updates["metadata"]
                if isinstance(meta, str):
                    meta = json.loads(meta)
                existing_raw = existing["metadata"] if existing["metadata"] else "{}"
                try:
                    existing_meta = json.loads(existing_raw) if isinstance(existing_raw, str) else (existing_raw or {})
                except (json.JSONDecodeError, TypeError):
                    existing_meta = {}
                merged = dict(existing_meta)
                merged.update(meta)
                if "workspaces" not in meta and "workspaces" in existing_meta:
                    merged["workspaces"] = existing_meta["workspaces"]
                fields.append("metadata = %s")
                values.append(json.dumps(merged))
            if not fields:
                return KnowledgeGraphDB._get_node_with_conn(node_id, conn)
            fields.append("updated_at = %s")
            values.append(now)
            values.append(node_id)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE kg_nodes SET {', '.join(fields)} WHERE node_id = %s", values)
            conn.commit()
            return KnowledgeGraphDB._get_node_with_conn(node_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def delete_node(node_id: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kg_edges WHERE source_id = %s OR target_id = %s", (node_id, node_id))
                cur.execute("DELETE FROM kg_nodes WHERE node_id = %s", (node_id,))
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def prune_graph(remove_orphan_nodes: bool = False) -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM kg_edges WHERE source_id NOT IN (SELECT node_id FROM kg_nodes) "
                    "OR target_id NOT IN (SELECT node_id FROM kg_nodes)"
                )
                edges_removed = cur.rowcount
                nodes_removed = 0
                if remove_orphan_nodes:
                    cur.execute(
                        "DELETE FROM kg_nodes WHERE node_id NOT IN "
                        "(SELECT source_id FROM kg_edges UNION SELECT target_id FROM kg_edges)"
                    )
                    nodes_removed = cur.rowcount
            conn.commit()
            return {"edges_removed": edges_removed, "nodes_removed": nodes_removed}
        finally:
            release_connection(conn)

    @staticmethod
    def commit_nodes(node_ids: list[str]) -> int:
        if not node_ids:
            return 0
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE kg_nodes SET status = 'committed', updated_at = %s WHERE node_id = ANY(%s)",
                    [now, list(node_ids)]
                )
                count = cur.rowcount
            conn.commit()
            return count
        finally:
            release_connection(conn)

    @staticmethod
    def uncommit_nodes(node_ids: list[str]) -> int:
        if not node_ids:
            return 0
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE kg_nodes SET status = 'staged', updated_at = %s WHERE node_id = ANY(%s)",
                    [now, list(node_ids)]
                )
                count = cur.rowcount
            conn.commit()
            return count
        finally:
            release_connection(conn)

    @staticmethod
    def list_nodes(node_type: str = "", limit: int = 200, status: str = "", include_staged: bool = False) -> list[dict]:
        conn = get_connection()
        try:
            conditions = []
            params: list = []
            if node_type:
                conditions.append("node_type = %s")
                params.append(node_type)
            if status:
                conditions.append("status = %s")
                params.append(status)
            elif not include_staged:
                conditions.append("status = 'committed'")
            where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM kg_nodes{where} ORDER BY created_at DESC LIMIT %s", params)
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def search_nodes(query: str, node_type: str = "", limit: int = 20, include_staged: bool = False) -> list[dict]:
        conn = get_connection()
        try:
            words = query.split()
            if not words:
                words = [query]

            grams: list[str] = []
            if len(words) > 1:
                grams.append(query)
            for n in (3, 2):
                if len(words) >= n:
                    for i in range(len(words) - n + 1):
                        grams.append(" ".join(words[i:i + n]))
            grams.extend(words)
            seen: set[str] = set()
            unique_grams: list[str] = []
            for g in grams:
                gl = g.lower()
                if gl not in seen:
                    seen.add(gl)
                    unique_grams.append(g)

            score_parts: list[str] = []
            match_parts: list[str] = []
            params: list = []
            for gram in unique_grams:
                like = f"%{gram}%"
                weight = len(gram.split())
                score_parts.append(f"(CASE WHEN title ILIKE %s OR content ILIKE %s THEN {weight} ELSE 0 END)")
                match_parts.append("(title ILIKE %s OR content ILIKE %s)")
                params.extend([like, like])

            score_sql = " + ".join(score_parts)
            any_match = " OR ".join(match_parts)
            match_params = list(params)

            conditions = [f"({any_match})"]
            if node_type:
                conditions.append("node_type = %s")
                match_params.append(node_type)
            if not include_staged:
                conditions.append("status = 'committed'")
            where = " AND ".join(conditions)

            sql = (
                f"SELECT *, ({score_sql}) AS _score FROM kg_nodes"
                f" WHERE {where}"
                f" ORDER BY _score DESC, created_at DESC LIMIT %s"
            )
            all_params = params + match_params + [limit]
            with conn.cursor() as cur:
                cur.execute(sql, all_params)
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def count_nodes(node_type: str = "") -> int:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if node_type:
                    cur.execute("SELECT COUNT(*) FROM kg_nodes WHERE node_type = %s", (node_type,))
                else:
                    cur.execute("SELECT COUNT(*) FROM kg_nodes")
                row = cur.fetchone()
            return row["count"] if row else 0
        finally:
            release_connection(conn)

    # -----------------------------------------------------------------------
    # Edges
    # -----------------------------------------------------------------------

    @staticmethod
    def create_edge(edge: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            edge_id = edge.get("edge_id") or _gen_id("kge")
            source_id = edge.get("source_id", "")
            target_id = edge.get("target_id", "")
            edge_type = edge.get("edge_type", "relates_to")

            if not source_id or not target_id:
                raise ValueError("source_id and target_id are required")
            if edge_type not in VALID_EDGE_TYPES:
                raise ValueError(f"Invalid edge_type: {edge_type}. Must be one of {VALID_EDGE_TYPES}")

            with conn.cursor() as cur:
                cur.execute("SELECT node_id FROM kg_nodes WHERE node_id = %s", (source_id,))
                if not cur.fetchone():
                    raise ValueError(f"Source node not found: {source_id}")
                cur.execute("SELECT node_id FROM kg_nodes WHERE node_id = %s", (target_id,))
                if not cur.fetchone():
                    raise ValueError(f"Target node not found: {target_id}")

                weight = edge.get("weight", 1.0)
                label = edge.get("label", "")

                cur.execute(
                    "SELECT edge_id FROM kg_edges WHERE source_id=%s AND target_id=%s AND edge_type=%s",
                    (source_id, target_id, edge_type)
                )
                existing = cur.fetchone()
                if existing:
                    return _row_to_dict(KnowledgeGraphDB._get_edge_with_conn(existing["edge_id"], conn))

                cur.execute(
                    "INSERT INTO kg_edges (edge_id, source_id, target_id, edge_type, weight, label, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (edge_id, source_id, target_id, edge_type, weight, label, now)
                )
                conn.commit()
                return _row_to_dict(KnowledgeGraphDB._get_edge_with_conn(edge_id, conn))
        finally:
            release_connection(conn)

    @staticmethod
    def _get_edge_with_conn(edge_id: str, conn) -> dict | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM kg_edges WHERE edge_id = %s", (edge_id,))
            row = cur.fetchone()
        return row

    @staticmethod
    def delete_edge(edge_id: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (edge_id,))
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def delete_edge_by_nodes(source_id: str, target_id: str, edge_type: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if edge_type:
                    cur.execute(
                        "DELETE FROM kg_edges WHERE source_id = %s AND target_id = %s AND edge_type = %s",
                        (source_id, target_id, edge_type)
                    )
                else:
                    cur.execute(
                        "DELETE FROM kg_edges WHERE source_id = %s AND target_id = %s",
                        (source_id, target_id)
                    )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def list_edges(node_id: str = "", edge_type: str = "", limit: int = 0) -> list[dict]:
        conn = get_connection()
        try:
            limit_clause = f" LIMIT {int(limit)}" if limit > 0 else ""
            with conn.cursor() as cur:
                if node_id and edge_type:
                    cur.execute(
                        f"SELECT * FROM kg_edges WHERE (source_id = %s OR target_id = %s) AND edge_type = %s{limit_clause}",
                        (node_id, node_id, edge_type)
                    )
                elif node_id:
                    cur.execute(
                        f"SELECT * FROM kg_edges WHERE source_id = %s OR target_id = %s{limit_clause}",
                        (node_id, node_id)
                    )
                elif edge_type:
                    cur.execute(
                        f"SELECT * FROM kg_edges WHERE edge_type = %s{limit_clause}", (edge_type,)
                    )
                else:
                    cur.execute(f"SELECT * FROM kg_edges{limit_clause}")
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    # -----------------------------------------------------------------------
    # Graph queries
    # -----------------------------------------------------------------------

    @staticmethod
    def get_full_graph(node_type: str = "", limit: int = 500, include_staged: bool = False, slim: bool = False) -> dict:
        conn = get_connection()
        try:
            node_cols = "node_id, node_type, title, status, created_at" if slim else "*"
            with conn.cursor() as cur:
                nodes = _fetch_graph_nodes(cur, node_cols, node_type, limit, include_staged)
                node_ids = {dict(node)["node_id"] for node in nodes}
                edges = _fetch_graph_edges(cur, node_ids, include_connected=bool(node_type))
                if node_type:
                    nodes, node_ids = _expand_connected_nodes(
                        cur, nodes, edges, node_cols, include_staged
                    )
                    edges = _edges_between_nodes(edges, node_ids)
            node_list = [dict(node) for node in nodes] if slim else [_row_to_dict(node) for node in nodes]
            return {
                "nodes": node_list,
                "edges": [_row_to_dict(edge) for edge in edges]
            }
        finally:
            release_connection(conn)

    @staticmethod
    def _get_neighbors_with_conn(node_id: str, conn, depth: int = 1, edge_type: str = "", include_staged: bool = False) -> dict:
        visited_nodes = set()
        visited_edges = []
        frontier = {node_id}

        with conn.cursor() as cur:
            if not include_staged:
                cur.execute("SELECT node_id FROM kg_nodes WHERE status = 'committed'")
                committed_ids: set | None = {r["node_id"] for r in cur.fetchall()}
            else:
                committed_ids = None

            for _ in range(depth):
                if not frontier:
                    break
                new_frontier = set()
                for nid in frontier:
                    if edge_type:
                        cur.execute(
                            "SELECT * FROM kg_edges WHERE (source_id = %s OR target_id = %s) AND edge_type = %s",
                            (nid, nid, edge_type)
                        )
                    else:
                        cur.execute(
                            "SELECT * FROM kg_edges WHERE source_id = %s OR target_id = %s",
                            (nid, nid)
                        )
                    edges = cur.fetchall()
                    for e in edges:
                        ed = _row_to_dict(e)
                        other = ed["target_id"] if ed["source_id"] == nid else ed["source_id"]
                        if committed_ids is not None and other not in committed_ids:
                            continue
                        if ed["edge_id"] not in {ve["edge_id"] for ve in visited_edges}:
                            visited_edges.append(ed)
                        if other not in visited_nodes:
                            new_frontier.add(other)
                    visited_nodes.add(nid)
                frontier = new_frontier - visited_nodes

            visited_nodes.update(frontier)
            all_ids = visited_nodes
            nodes = []
            if all_ids:
                status_clause = "" if include_staged else " AND status = 'committed'"
                cur.execute(
                    f"SELECT * FROM kg_nodes WHERE node_id = ANY(%s){status_clause}", (list(all_ids),)
                )
                nodes = cur.fetchall()

        return {
            "nodes": [_row_to_dict(n) for n in nodes],
            "edges": visited_edges
        }

    @staticmethod
    def get_neighbors(node_id: str, depth: int = 1, edge_type: str = "", include_staged: bool = False) -> dict:
        conn = get_connection()
        try:
            return KnowledgeGraphDB._get_neighbors_with_conn(node_id, conn, depth=depth, edge_type=edge_type, include_staged=include_staged)
        finally:
            release_connection(conn)

    @staticmethod
    def merge_nodes(survivor_id: str, absorbed_ids: list[str], node_type: str = "") -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM kg_nodes WHERE node_id = %s", (survivor_id,))
                survivor_row = cur.fetchone()
            if not survivor_row:
                return None
            survivor = _row_to_dict(survivor_row)
            unique_absorbed_ids = list(dict.fromkeys(
                node_id for node_id in absorbed_ids if node_id != survivor_id
            ))
            absorbed_nodes = _fetch_nodes(conn, unique_absorbed_ids)
            if not absorbed_nodes:
                return survivor
            all_absorbed = [n["node_id"] for n in absorbed_nodes]
            with conn.cursor() as cur:
                _rewire_absorbed_edges(cur, survivor_id, all_absorbed)
                final_type = node_type if node_type else survivor.get("node_type", "insight")
                cur.execute(
                    "UPDATE kg_nodes SET content = %s, metadata = %s, node_type = %s, updated_at = %s WHERE node_id = %s",
                    (_merge_content(survivor, absorbed_nodes), json.dumps(_merge_metadata(survivor, absorbed_nodes)), final_type, _now(), survivor_id)
                )
                for nid in all_absorbed:
                    cur.execute("DELETE FROM kg_nodes WHERE node_id = %s", (nid,))

            conn.commit()
            return KnowledgeGraphDB._get_node_with_conn(survivor_id, conn)
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def get_project_context(project_node_id: str, include_staged: bool = False) -> dict:
        conn = get_connection()
        try:
            node = KnowledgeGraphDB._get_node_with_conn(project_node_id, conn)
            if not node:
                return {"error": f"Node not found: {project_node_id}"}
            neighborhood = KnowledgeGraphDB._get_neighbors_with_conn(project_node_id, conn, depth=2, include_staged=include_staged)
            insights = [n for n in neighborhood["nodes"] if n["node_type"] == "insight"]
            concepts = [n for n in neighborhood["nodes"] if n["node_type"] == "concept"]
            sessions = [n for n in neighborhood["nodes"] if n["node_type"] == "session"]
            repos = [n for n in neighborhood["nodes"] if n["node_type"] == "repo"]
            ws_id = node.get("metadata", {}).get("workspace_id", "")
            tasks = []
            notes = []
            if ws_id:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT task_id, title, status, priority FROM tasks WHERE workspace_id = %s ORDER BY created_at DESC LIMIT 50",
                        (ws_id,)
                    )
                    tasks = [dict(r) for r in cur.fetchall()]
                    cur.execute(
                        "SELECT text, created_at FROM notes WHERE workspace_id = %s ORDER BY created_at DESC LIMIT 20",
                        (ws_id,)
                    )
                    notes = [dict(r) for r in cur.fetchall()]
            return {
                "project": {"node_id": node["node_id"], "title": node["title"], "content": node["content"]},
                "insights": [{"title": i["title"], "content": i["content"][:500]} for i in insights],
                "concepts": [n["title"] for n in concepts],
                "sessions": [{"title": s["title"], "node_id": s["node_id"]} for s in sessions],
                "repos": [r["title"] for r in repos],
                "tasks": tasks,
                "notes": notes,
                "stats": {
                    "insights": len(insights),
                    "concepts": len(concepts),
                    "sessions": len(sessions),
                    "repos": len(repos),
                    "edges": len(neighborhood["edges"])
                }
            }
        finally:
            release_connection(conn)

    @staticmethod
    def find_root_domains(node_id: str, max_depth: int = 5) -> set[str]:
        """Traverse graph neighborhood up to max_depth to find all connected root domain nodes."""
        conn = get_connection()
        try:
            domain_ids = set()
            with conn.cursor() as cur:
                cur.execute("SELECT node_id, node_type FROM kg_nodes WHERE node_id = %s", (node_id,))
                row = cur.fetchone()
                if row and row.get("node_type") == "domain":
                    return {node_id}

            visited = {node_id}
            frontier = [node_id]

            for _ in range(max_depth):
                if not frontier:
                    break
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT source_id, target_id FROM kg_edges WHERE source_id = ANY(%s) OR target_id = ANY(%s)",
                        (frontier, frontier)
                    )
                    edges = cur.fetchall()

                next_frontier = set()
                for edge in edges:
                    src = edge.get("source_id")
                    tgt = edge.get("target_id")
                    for n_id in (src, tgt):
                        if n_id and n_id not in visited:
                            visited.add(n_id)
                            next_frontier.add(n_id)

                if not next_frontier:
                    break

                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT node_id FROM kg_nodes WHERE node_id = ANY(%s) AND node_type = 'domain'",
                        (list(next_frontier),)
                    )
                    found_domains = cur.fetchall()
                    for d in found_domains:
                        domain_ids.add(d["node_id"])

                frontier = list(next_frontier)

            return domain_ids
        finally:
            release_connection(conn)
