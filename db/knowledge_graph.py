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


VALID_NODE_TYPES = {"insight", "project", "session", "concept", "repo",
                   "client", "domain", "service", "library", "technology", "issue", "person"}
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
            status_filter = "" if include_staged else " AND status = 'committed'"
            with conn.cursor() as cur:
                if node_type:
                    cur.execute(
                        f"SELECT {node_cols} FROM kg_nodes WHERE node_type = %s{status_filter} ORDER BY created_at DESC LIMIT %s",
                        (node_type, limit)
                    )
                    nodes = cur.fetchall()
                    node_ids = {dict(n)["node_id"] for n in nodes}
                    if node_ids:
                        cur.execute(
                            "SELECT * FROM kg_edges WHERE source_id = ANY(%s) OR target_id = ANY(%s)",
                            (list(node_ids), list(node_ids))
                        )
                        extra_edges = cur.fetchall()
                        extra_ids = set()
                        for e in extra_edges:
                            ed = dict(e)
                            extra_ids.add(ed["source_id"])
                            extra_ids.add(ed["target_id"])
                        missing = extra_ids - node_ids
                        if missing:
                            status_clause = "" if include_staged else " AND status = 'committed'"
                            cur.execute(
                                f"SELECT {node_cols} FROM kg_nodes WHERE node_id = ANY(%s){status_clause}",
                                (list(missing),)
                            )
                            extra_nodes = cur.fetchall()
                            nodes = list(nodes) + list(extra_nodes)
                            node_ids.update(dict(n)["node_id"] for n in extra_nodes)
                        edges = [e for e in extra_edges if dict(e)["source_id"] in node_ids and dict(e)["target_id"] in node_ids]
                    else:
                        edges = []
                else:
                    status_where = " WHERE status = 'committed'" if not include_staged else ""
                    cur.execute(
                        f"SELECT {node_cols} FROM kg_nodes{status_where} ORDER BY created_at DESC LIMIT %s", (limit,)
                    )
                    nodes = cur.fetchall()
                    node_ids = {dict(n)["node_id"] for n in nodes}
                    if node_ids:
                        cur.execute(
                            "SELECT * FROM kg_edges WHERE source_id = ANY(%s) AND target_id = ANY(%s)",
                            (list(node_ids), list(node_ids))
                        )
                        edges = cur.fetchall()
                    else:
                        edges = []

            if slim:
                node_list = [dict(n) for n in nodes]
            else:
                node_list = [_row_to_dict(n) for n in nodes]

            return {
                "nodes": node_list,
                "edges": [_row_to_dict(e) for e in edges]
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

            absorbed_nodes = []
            for nid in absorbed_ids:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM kg_nodes WHERE node_id = %s", (nid,))
                    row = cur.fetchone()
                if row:
                    absorbed_nodes.append(_row_to_dict(row))

            if not absorbed_nodes:
                return survivor

            merged_content = survivor.get("content", "") or ""
            for an in absorbed_nodes:
                ac = (an.get("content") or "").strip()
                if ac and ac != merged_content:
                    merged_content = merged_content.rstrip() + "\n\n---\n\n" + ac if merged_content.strip() else ac

            merged_meta = dict(survivor.get("metadata") or {})
            for an in absorbed_nodes:
                am = an.get("metadata") or {}
                if am.get("files"):
                    existing = set(merged_meta.get("files") or [])
                    for f in am["files"]:
                        existing.add(f)
                    merged_meta["files"] = sorted(existing)
                if am.get("repo") and not merged_meta.get("repo"):
                    merged_meta["repo"] = am["repo"]
                if am.get("source") and not merged_meta.get("source"):
                    merged_meta["source"] = am["source"]
                if am.get("workspace_id") and not merged_meta.get("workspace_id"):
                    merged_meta["workspace_id"] = am["workspace_id"]

            all_absorbed = [n["node_id"] for n in absorbed_nodes]
            absorbed_set = set(all_absorbed)

            with conn.cursor() as cur:
                for nid in all_absorbed:
                    cur.execute("SELECT edge_id, source_id, target_id, edge_type, weight FROM kg_edges WHERE source_id = %s", (nid,))
                    src_edges = cur.fetchall()
                    for e in src_edges:
                        new_tgt = e["target_id"]
                        if new_tgt in absorbed_set or new_tgt == survivor_id:
                            cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (e["edge_id"],))
                            continue
                        cur.execute(
                            "SELECT edge_id, weight FROM kg_edges WHERE source_id = %s AND target_id = %s AND edge_type = %s",
                            (survivor_id, new_tgt, e["edge_type"])
                        )
                        existing = cur.fetchone()
                        if existing:
                            if e["weight"] > existing["weight"]:
                                cur.execute("UPDATE kg_edges SET weight = %s WHERE edge_id = %s", (e["weight"], existing["edge_id"]))
                            cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (e["edge_id"],))
                        else:
                            cur.execute("UPDATE kg_edges SET source_id = %s WHERE edge_id = %s", (survivor_id, e["edge_id"]))

                    cur.execute("SELECT edge_id, source_id, target_id, edge_type, weight FROM kg_edges WHERE target_id = %s", (nid,))
                    tgt_edges = cur.fetchall()
                    for e in tgt_edges:
                        new_src = e["source_id"]
                        if new_src in absorbed_set or new_src == survivor_id:
                            cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (e["edge_id"],))
                            continue
                        cur.execute(
                            "SELECT edge_id, weight FROM kg_edges WHERE source_id = %s AND target_id = %s AND edge_type = %s",
                            (new_src, survivor_id, e["edge_type"])
                        )
                        existing = cur.fetchone()
                        if existing:
                            if e["weight"] > existing["weight"]:
                                cur.execute("UPDATE kg_edges SET weight = %s WHERE edge_id = %s", (e["weight"], existing["edge_id"]))
                            cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (e["edge_id"],))
                        else:
                            cur.execute("UPDATE kg_edges SET target_id = %s WHERE edge_id = %s", (survivor_id, e["edge_id"]))

                final_type = node_type if node_type else survivor.get("node_type", "insight")
                cur.execute(
                    "UPDATE kg_nodes SET content = %s, metadata = %s, node_type = %s, updated_at = %s WHERE node_id = %s",
                    (merged_content, json.dumps(merged_meta), final_type, _now(), survivor_id)
                )
                for nid in all_absorbed:
                    cur.execute("DELETE FROM kg_nodes WHERE node_id = %s", (nid,))

            conn.commit()
            return KnowledgeGraphDB._get_node_with_conn(survivor_id, conn)
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
