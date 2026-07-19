"""Context DB layer using PostgreSQL + pgvector.

All context tables use the ctx_ prefix to avoid collisions with
existing workspace/task tables.

Vector storage uses pgvector's HNSW index for fast KNN search,
replacing sqlite-vec's vec0 virtual table.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from postgres_client import get_connection, release_connection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_vec(vec: List[float]) -> list:
    """Convert a float list to a plain Python list for psycopg2 + pgvector."""
    return [float(v) for v in vec]


def vec_version() -> Optional[str]:
    """Return pgvector version string, or None."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
        return row["extversion"] if row else None
    except Exception:
        return None
    finally:
        release_connection(conn)


def init_context_schema() -> bool:
    """
    Context tables are created by postgres_client.init_schema().
    This is a no-op kept for API compatibility with import sites.
    """
    return True


# ---------------------------------------------------------------------------
# ContextDB
# ---------------------------------------------------------------------------

class ContextDB:
    """Static-method DB operations for context tables."""

    # ------------------------------------------------------------------
    # Repo CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def add_repo(name: str, path: str, conn=None) -> Dict[str, Any]:
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ctx_repos (name, path) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING",
                    (name, path),
                )
                conn.commit()
                cur.execute("SELECT * FROM ctx_repos WHERE name = %s", (name,))
                row = cur.fetchone()
            return dict(row) if row else {}
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def get_repo(name: str, conn=None) -> Optional[Dict[str, Any]]:
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM ctx_repos WHERE name = %s", (name,))
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def get_repo_by_identifier(repo_id: str, conn=None) -> Optional[Dict[str, Any]]:
        """Resolve the explicit repository ID, retaining name compatibility."""
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM ctx_repos WHERE id::text = %s OR name = %s",
                    (str(repo_id), str(repo_id)),
                )
                row = cur.fetchone()
            return dict(row) if row else None
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def list_repos() -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.*,
                           (SELECT COUNT(*) FROM ctx_files WHERE repo_id = r.id) AS file_count,
                           (SELECT COUNT(*) FROM ctx_files WHERE repo_id = r.id AND is_memory_bank = 1) AS memory_bank_count,
                           (SELECT COUNT(*) FROM ctx_chunks c
                            JOIN ctx_files f ON c.file_id = f.id
                            WHERE f.repo_id = r.id) AS chunk_count,
                           (SELECT COUNT(*) FROM ctx_ast_nodes a
                            JOIN ctx_files f ON a.file_id = f.id
                            WHERE f.repo_id = r.id) AS ast_node_count
                    FROM ctx_repos r ORDER BY r.name
                """)
                rows = cur.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                # Reuse main connection or internal cursor if possible
                with conn.cursor() as lc:
                    lc.execute(
                        "SELECT language, COUNT(*) AS count FROM ctx_files WHERE repo_id = %s AND is_memory_bank = 0 GROUP BY language ORDER BY count DESC",
                        (d["id"],),
                    )
                    langs = lc.fetchall()
                d["languages"] = {row["language"]: row["count"] for row in langs}
                result.append(d)
            return result
        finally:
            release_connection(conn)

    @staticmethod
    def delete_repo(name: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM ctx_repos WHERE name = %s", (name,))
                repo = cur.fetchone()
                if not repo:
                    return False
                repo_id = repo["id"]
                # ON DELETE CASCADE handles ctx_files → ctx_chunks → ctx_vec_chunks
                cur.execute("DELETE FROM ctx_repos WHERE id = %s", (repo_id,))
            conn.commit()
            return True
        finally:
            release_connection(conn)

    @staticmethod
    def update_repo_status(name: str, status: str, indexed_at: str = None, conn=None):
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                if indexed_at:
                    cur.execute(
                        "UPDATE ctx_repos SET status = %s, indexed_at = %s WHERE name = %s",
                        (status, indexed_at, name),
                    )
                else:
                    cur.execute("UPDATE ctx_repos SET status = %s WHERE name = %s", (status, name))
            conn.commit()
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def mark_repo_fetched(name: str, fetched_at=None) -> None:
        """Persist the last successful remote fetch/clone timestamp."""
        from db.base import _now
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ctx_repos SET last_fetched_at = %s WHERE name = %s",
                    (fetched_at or _now(), name),
                )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def clear_repo_data(repo_id: int, conn=None):
        """Delete all files and chunks for a repo (for reindex)."""
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ctx_files WHERE repo_id = %s", (repo_id,))
            conn.commit()
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def clear_index_data(repo_id: int, conn=None):
        """Delete only chunk/vector data for a repo."""
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM ctx_vec_chunks
                    WHERE chunk_id IN (
                        SELECT c.id FROM ctx_chunks c
                        JOIN ctx_files f ON c.file_id = f.id
                        WHERE f.repo_id = %s
                    )
                """, (repo_id,))
                cur.execute("""
                    DELETE FROM ctx_chunks
                    WHERE file_id IN (SELECT id FROM ctx_files WHERE repo_id = %s)
                """, (repo_id,))
            conn.commit()
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def clear_ast_data(repo_id: int, conn=None):
        """Delete only AST data for a repo."""
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM ctx_ast_nodes
                    WHERE file_id IN (SELECT id FROM ctx_files WHERE repo_id = %s)
                """, (repo_id,))
            conn.commit()
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def clear_file_generated_data(file_id: int, conn=None):
        """Remove chunks, vectors, and AST rows before replacing one indexed file."""
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM ctx_vec_chunks WHERE chunk_id IN (SELECT id FROM ctx_chunks WHERE file_id = %s)",
                    (file_id,),
                )
                cur.execute("DELETE FROM ctx_chunks WHERE file_id = %s", (file_id,))
                cur.execute("DELETE FROM ctx_ast_nodes WHERE file_id = %s", (file_id,))
            conn.commit()
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def clear_file_ast_data(file_id: int, conn=None):
        """Remove AST rows before replacing one file's structural index."""
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ctx_ast_nodes WHERE file_id = %s", (file_id,))
            conn.commit()
        finally:
            if local_conn:
                release_connection(conn)

    # ------------------------------------------------------------------
    # File & chunk operations
    # ------------------------------------------------------------------

    @staticmethod
    def insert_file(repo_id: int, rel_path: str, language: str,
                    is_memory_bank: bool, mtime_ns: int, indexed_at: str, conn=None) -> int:
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO ctx_files (repo_id, rel_path, language, is_memory_bank, mtime_ns, indexed_at)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (repo_id, rel_path) DO UPDATE SET
                         language=EXCLUDED.language,
                         is_memory_bank=EXCLUDED.is_memory_bank,
                         mtime_ns=EXCLUDED.mtime_ns,
                         indexed_at=EXCLUDED.indexed_at
                       RETURNING id""",
                    (repo_id, rel_path, language, int(is_memory_bank), mtime_ns, indexed_at),
                )
                row = cur.fetchone()
                if row:
                    file_id = row["id"]
                else:
                    cur.execute(
                        "SELECT id FROM ctx_files WHERE repo_id = %s AND rel_path = %s",
                        (repo_id, rel_path),
                    )
                    file_id = cur.fetchone()["id"]
            conn.commit()
            return file_id
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def insert_chunk(file_id: int, chunk_index: int, content: str,
                     embedding: List[float], conn=None) -> int:
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ctx_chunks (file_id, chunk_index, content) VALUES (%s, %s, %s) RETURNING id",
                    (file_id, chunk_index, content),
                )
                chunk_id = cur.fetchone()["id"]
                cur.execute(
                    "INSERT INTO ctx_vec_chunks (chunk_id, embedding) VALUES (%s, %s::vector)",
                    (chunk_id, _coerce_vec(embedding)),
                )
            conn.commit()
            return chunk_id
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def insert_ast_node(file_id: int, node_type: str, name: str, start_line: int, end_line: int, conn=None) -> int:
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO ctx_ast_nodes (file_id, node_type, name, start_line, end_line)
                       VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                    (file_id, node_type, name, start_line, end_line),
                )
                row = cur.fetchone()
            conn.commit()
            return row["id"] if row else 0
        finally:
            if local_conn:
                release_connection(conn)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @staticmethod
    def vector_search(
        query_vec: List[float],
        limit: int = 10,
        repo_filter: Optional[Union[str, List[str]]] = None,
        memory_bank_only: bool = False,
        exclude_memory_bank: bool = False,
    ) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            sql = """
                SELECT c.id AS chunk_id, c.chunk_index, c.content,
                       f.rel_path, f.language, f.is_memory_bank,
                       r.name AS repo,
                       (v.embedding <=> %s::vector) AS distance
                FROM ctx_vec_chunks v
                JOIN ctx_chunks c ON v.chunk_id = c.id
                JOIN ctx_files  f ON c.file_id  = f.id
                JOIN ctx_repos  r ON f.repo_id  = r.id
                WHERE 1=1
            """
            params: list = [_coerce_vec(query_vec)]

            if repo_filter:
                repo_list = repo_filter if isinstance(repo_filter, list) else [repo_filter]
                sql += " AND r.name = ANY(%s)"
                params.append(repo_list)

            if memory_bank_only:
                sql += " AND f.is_memory_bank = 1"
            elif exclude_memory_bank:
                sql += " AND f.is_memory_bank = 0"

            sql += " ORDER BY distance ASC LIMIT %s"
            params.append(limit)

            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

            results = []
            for row in rows:
                d = dict(row)
                d["rank"] = max(0.0, 1.0 - float(d["distance"]))
                results.append(d)
            return results
        finally:
            release_connection(conn)

    @staticmethod
    def search_ast_nodes(query: str, repo_filter: Optional[Union[str, List[str]]] = None) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            sql = """
                SELECT a.id, a.node_type, a.name, a.start_line, a.end_line,
                       f.rel_path, r.name AS repo
                FROM ctx_ast_nodes a
                JOIN ctx_files f ON a.file_id = f.id
                JOIN ctx_repos r ON f.repo_id = r.id
                WHERE a.name ILIKE %s
            """
            params: list = [f"%{query}%"]

            if repo_filter:
                repo_list = repo_filter if isinstance(repo_filter, list) else [repo_filter]
                sql += " AND r.name = ANY(%s)"
                params.append(repo_list)

            sql += " LIMIT 50"

            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    # ------------------------------------------------------------------
    # Memory bank & file listing
    # ------------------------------------------------------------------

    @staticmethod
    def list_ast_nodes(repo_filter=None) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            sql = """
                SELECT r.name AS repo, f.rel_path AS path, a.node_type, a.name, a.start_line, a.end_line
                FROM ctx_ast_nodes a
                JOIN ctx_files f ON a.file_id = f.id
                JOIN ctx_repos r ON f.repo_id = r.id
            """
            params: list = []
            if repo_filter:
                repo_list = repo_filter if isinstance(repo_filter, list) else [repo_filter]
                sql += " WHERE r.name = ANY(%s)"
                params.append(repo_list)
            sql += " ORDER BY r.name, f.rel_path, a.start_line"
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_memory_resources(repo_filter=None) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            sql = """
                SELECT r.name AS repo, f.rel_path AS path, f.language,
                       f.is_memory_bank, f.indexed_at, f.created_at,
                       (SELECT COUNT(*) FROM ctx_chunks c WHERE c.file_id = f.id) AS chunk_count
                FROM ctx_files f
                JOIN ctx_repos r ON f.repo_id = r.id
                WHERE f.is_memory_bank = 1
            """
            params: list = []
            if repo_filter:
                repo_list = repo_filter if isinstance(repo_filter, list) else [repo_filter]
                sql += " AND r.name = ANY(%s)"
                params.append(repo_list)
            sql += " ORDER BY r.name, f.rel_path"
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["uri"] = f"{d['repo']}:{d['path']}"
                results.append(d)
            return results
        finally:
            release_connection(conn)

    @staticmethod
    def list_code_files(repo_filter=None) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            sql = """
                SELECT r.name AS repo, f.rel_path AS path, f.language,
                       f.is_memory_bank, f.indexed_at, f.created_at,
                       (SELECT COUNT(*) FROM ctx_chunks c WHERE c.file_id = f.id) AS chunk_count
                FROM ctx_files f
                JOIN ctx_repos r ON f.repo_id = r.id
                WHERE f.is_memory_bank = 0
            """
            params: list = []
            if repo_filter:
                repo_list = repo_filter if isinstance(repo_filter, list) else [repo_filter]
                sql += " AND r.name = ANY(%s)"
                params.append(repo_list)
            sql += " ORDER BY r.name, f.rel_path"
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["uri"] = f"{d['repo']}:{d['path']}"
                results.append(d)
            return results
        finally:
            release_connection(conn)

    @staticmethod
    def read_code_file(uri: str) -> Optional[Dict[str, Any]]:
        conn = get_connection()
        try:
            if ":" in uri:
                repo_name, rel_path = uri.split(":", 1)
            else:
                rel_path = uri
                repo_name = None

            with conn.cursor() as cur:
                if repo_name:
                    cur.execute(
                        """SELECT f.id, f.rel_path, f.language, f.is_memory_bank, f.created_at, r.name AS repo
                           FROM ctx_files f JOIN ctx_repos r ON f.repo_id = r.id
                           WHERE f.rel_path = %s AND r.name = %s AND f.is_memory_bank = 0""",
                        (rel_path, repo_name),
                    )
                else:
                    cur.execute(
                        """SELECT f.id, f.rel_path, f.language, f.is_memory_bank, f.created_at,
                                  (SELECT name FROM ctx_repos WHERE id = f.repo_id) AS repo
                           FROM ctx_files f WHERE f.rel_path = %s AND f.is_memory_bank = 0""",
                        (rel_path,),
                    )
                file_row = cur.fetchone()

            if not file_row:
                return None

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content FROM ctx_chunks WHERE file_id = %s ORDER BY chunk_index",
                    (file_row["id"],),
                )
                chunks = cur.fetchall()

            return {
                "uri": uri,
                "repo": file_row["repo"],
                "path": file_row["rel_path"],
                "language": file_row["language"],
                "is_memory_bank": False,
                "content": "\n".join(c["content"] for c in chunks),
                "chunk_count": len(chunks),
                "created_at": str(file_row["created_at"]),
            }
        finally:
            release_connection(conn)

    @staticmethod
    def read_memory_resource(uri: str) -> Optional[Dict[str, Any]]:
        conn = get_connection()
        try:
            if ":" in uri:
                repo_name, rel_path = uri.split(":", 1)
            else:
                rel_path = uri
                repo_name = None

            with conn.cursor() as cur:
                if repo_name:
                    cur.execute(
                        """SELECT f.id, f.rel_path, f.language, f.is_memory_bank, f.created_at, r.name AS repo
                           FROM ctx_files f JOIN ctx_repos r ON f.repo_id = r.id
                           WHERE f.rel_path = %s AND r.name = %s AND f.is_memory_bank = 1""",
                        (rel_path, repo_name),
                    )
                else:
                    cur.execute(
                        """SELECT f.id, f.rel_path, f.language, f.is_memory_bank, f.created_at,
                                  (SELECT name FROM ctx_repos WHERE id = f.repo_id) AS repo
                           FROM ctx_files f WHERE f.rel_path = %s AND f.is_memory_bank = 1""",
                        (rel_path,),
                    )
                file_row = cur.fetchone()

            if not file_row:
                return None

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT content FROM ctx_chunks WHERE file_id = %s ORDER BY chunk_index",
                    (file_row["id"],),
                )
                chunks = cur.fetchall()

            return {
                "uri": uri,
                "repo": file_row["repo"],
                "path": file_row["rel_path"],
                "language": file_row["language"],
                "is_memory_bank": bool(file_row["is_memory_bank"]),
                "content": "\n".join(c["content"] for c in chunks),
                "chunk_count": len(chunks),
                "created_at": str(file_row["created_at"]),
            }
        finally:
            release_connection(conn)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @staticmethod
    def get_stats() -> Dict[str, Any]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM ctx_repos")
                repos = cur.fetchone()
                cur.execute("SELECT COUNT(*) AS n FROM ctx_files")
                files = cur.fetchone()
                cur.execute("SELECT COUNT(*) AS n FROM ctx_chunks")
                chunks = cur.fetchone()
            return {
                "repos": repos["count"] if repos else 0,
                "files": files["count"] if files else 0,
                "chunks": chunks["count"] if chunks else 0,
            }
        except Exception:
            return {"repos": 0, "files": 0, "chunks": 0}
        finally:
            release_connection(conn)

    @staticmethod
    def get_repo_stats() -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT r.id, r.name, r.path, r.status, r.indexed_at, r.created_at,
                           (SELECT COUNT(*) FROM ctx_files WHERE repo_id = r.id) AS file_count,
                           (SELECT COUNT(*) FROM ctx_chunks WHERE file_id IN (SELECT id FROM ctx_files WHERE repo_id = r.id)) AS chunk_count,
                           (SELECT COUNT(*) FROM ctx_ast_nodes WHERE file_id IN (SELECT id FROM ctx_files WHERE repo_id = r.id)) AS ast_node_count
                    FROM ctx_repos r ORDER BY r.name
                """)
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def get_repo_languages(repo_id: int) -> List[Dict[str, Any]]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT language, COUNT(*) AS count FROM ctx_files WHERE repo_id = %s GROUP BY language ORDER BY count DESC",
                    (repo_id,),
                )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)
