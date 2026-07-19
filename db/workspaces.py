"""WorkspaceDB — PostgreSQL backend."""

from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection
import uuid


class WorkspaceDB:

    @staticmethod
    def _get_by_id_with_conn(workspace_id: str, conn, user_id: str = "") -> dict | None:
        with conn.cursor() as cur:
            if user_id:
                cur.execute(
                    "SELECT * FROM workspaces WHERE workspace_id = %s AND user_id = %s",
                    (workspace_id, user_id),
                )
            else:
                cur.execute(
                    "SELECT * FROM workspaces WHERE workspace_id = %s",
                    (workspace_id,),
                )
            row = cur.fetchone()
        return _row_to_dict(row)

    @staticmethod
    def create(workspace: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            ws_id = workspace.get("workspace_id") or str(uuid.uuid4().int)[:19]
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO workspaces
                       (workspace_id, name, description, priority, status,
                        created_at, updated_at, created_session_id, user_id, color)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (workspace_id) DO UPDATE SET
                         name=EXCLUDED.name,
                         description=EXCLUDED.description,
                         priority=EXCLUDED.priority,
                         status=EXCLUDED.status,
                         updated_at=EXCLUDED.updated_at,
                         user_id=EXCLUDED.user_id,
                         color=EXCLUDED.color""",
                    (
                        ws_id,
                        workspace.get("name", ""),
                        workspace.get("description", ""),
                        workspace.get("priority", "medium"),
                        workspace.get("status", "open"),
                        workspace.get("created_at", now),
                        workspace.get("updated_at", now),
                        workspace.get("created_session_id"),
                        workspace.get("user_id", ""),
                        workspace.get("color", ""),
                    ),
                )
            conn.commit()
            return WorkspaceDB._get_by_id_with_conn(ws_id, conn, user_id=workspace.get("user_id", ""))
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(workspace_id: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            return WorkspaceDB._get_by_id_with_conn(workspace_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def list_all(status: str | None = None, limit: int = 1000, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            clauses = []
            params = []
            if status:
                clauses.append("status = %s")
                params.append(status)
            if user_id:
                clauses.append("user_id = %s")
                params.append(user_id)
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM workspaces {where} ORDER BY created_at DESC LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def update(workspace_id: str, updates: dict, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            updates["updated_at"] = _now()
            # Remove None values and non-column keys
            valid_cols = {
                "name", "description", "priority", "status", "color",
                "updated_at", "created_session_id",
            }
            filtered = {k: v for k, v in updates.items() if k in valid_cols and v is not None}
            if not filtered:
                return WorkspaceDB._get_by_id_with_conn(workspace_id, conn, user_id=user_id)

            set_clause = ", ".join(f"{k} = %s" for k in filtered)
            values = list(filtered.values()) + [workspace_id]
            where = "WHERE workspace_id = %s"
            if user_id:
                where += " AND user_id = %s"
                values.append(user_id)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE workspaces SET {set_clause} {where}",
                    values,
                )
            conn.commit()
            return WorkspaceDB._get_by_id_with_conn(workspace_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def delete(workspace_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "DELETE FROM workspaces WHERE workspace_id = %s AND user_id = %s",
                        (workspace_id, user_id),
                    )
                else:
                    cur.execute(
                        "DELETE FROM workspaces WHERE workspace_id = %s",
                        (workspace_id,),
                    )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def update_task_stats(workspace_id: str, stats: dict) -> None:
        """No-op: task_stats are computed dynamically."""
        pass

    @staticmethod
    def get_task_stats(workspace_id: str) -> dict:
        """Compute task stats dynamically from tasks table."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) as cnt FROM tasks WHERE workspace_id = %s GROUP BY status",
                    (workspace_id,),
                )
                rows = cur.fetchall()
            result = {"todo": 0, "in_progress": 0, "in-progress": 0, "done": 0, "blocked": 0, "total": 0}
            for r in rows:
                result[r["status"]] = r["cnt"]
                result["total"] += r["cnt"]
            # Normalize: support both "in_progress" and "in-progress"
            result["in_progress"] = result.get("in-progress", 0) + result.get("in_progress", 0)
            return result
        finally:
            release_connection(conn)

    @staticmethod
    def close(workspace_id: str, user_id: str = "") -> dict | None:
        return WorkspaceDB.update(workspace_id, {"status": "closed"}, user_id=user_id)

    @staticmethod
    def reopen(workspace_id: str, user_id: str = "") -> dict | None:
        return WorkspaceDB.update(workspace_id, {"status": "open"}, user_id=user_id)
