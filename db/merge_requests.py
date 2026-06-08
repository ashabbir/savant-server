"""MergeRequestDB — PostgreSQL backend."""

from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection


class MergeRequestDB:

    @staticmethod
    def _enrich_with_notes(mr: dict, conn=None) -> dict:
        """Attach notes list from mr_notes table."""
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT text, session_id, created_at FROM mr_notes WHERE mr_id = %s ORDER BY created_at",
                    (mr["mr_id"],),
                )
                rows = cur.fetchall()
            mr["notes"] = [dict(r) for r in rows]
            return mr
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def _enrich_list(mrs: list[dict], conn=None) -> list[dict]:
        """Batch-enrich a list of MRs with their notes."""
        if not mrs:
            return mrs
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            mr_ids = [m["mr_id"] for m in mrs]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT mr_id, text, session_id, created_at FROM mr_notes WHERE mr_id = ANY(%s) ORDER BY created_at",
                    (mr_ids,),
                )
                rows = cur.fetchall()
            notes_map: dict[str, list] = {}
            for r in rows:
                notes_map.setdefault(r["mr_id"], []).append(
                    {"text": r["text"], "session_id": r["session_id"], "created_at": r["created_at"]}
                )
            for m in mrs:
                m["notes"] = notes_map.get(m["mr_id"], [])
            return mrs
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def _get_by_id_with_conn(mr_id: str, conn, user_id: str = "") -> dict | None:
        with conn.cursor() as cur:
            if user_id:
                cur.execute("SELECT * FROM merge_requests WHERE mr_id = %s AND user_id = %s", (mr_id, user_id))
            else:
                cur.execute("SELECT * FROM merge_requests WHERE mr_id = %s", (mr_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return MergeRequestDB._enrich_with_notes(_row_to_dict(row), conn=conn)

    @staticmethod
    def create(mr: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO merge_requests
                       (mr_id, workspace_id, url, project_id, mr_iid, title, status,
                        priority, author, jira, created_at, updated_at, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        mr["mr_id"], mr["workspace_id"], mr["url"],
                        mr.get("project_id", ""), mr.get("mr_iid", 0),
                        mr.get("title", ""), mr.get("status", "open"),
                        mr.get("priority", "medium"), mr.get("author", ""),
                        mr.get("jira", ""), mr.get("created_at", now), mr.get("updated_at", now),
                        mr.get("user_id", ""),
                    ),
                )
                # Insert embedded notes
                for note in mr.get("notes", []):
                    cur.execute(
                        "INSERT INTO mr_notes (mr_id, text, session_id, created_at) VALUES (%s, %s, %s, %s)",
                        (mr["mr_id"], note.get("text", ""), note.get("session_id", ""), note.get("created_at", now)),
                    )
            conn.commit()
            return MergeRequestDB._get_by_id_with_conn(mr["mr_id"], conn, user_id=mr.get("user_id", ""))
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(mr_id: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            return MergeRequestDB._get_by_id_with_conn(mr_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_url(url: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("SELECT * FROM merge_requests WHERE url = %s AND user_id = %s", (url, user_id))
                else:
                    cur.execute("SELECT * FROM merge_requests WHERE url = %s", (url,))
                row = cur.fetchone()
            if row is None:
                return None
            return MergeRequestDB._enrich_with_notes(_row_to_dict(row), conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_workspace(workspace_id: str, status: str | None = None, limit: int = 1000, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            clauses = ["workspace_id = %s"]
            params: list = [workspace_id]
            if status:
                clauses.append("status = %s")
                params.append(status)
            if user_id:
                clauses.append("user_id = %s")
                params.append(user_id)
            where = "WHERE " + " AND ".join(clauses)
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM merge_requests {where} ORDER BY created_at DESC LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
            return MergeRequestDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_status(status: str, limit: int = 1000, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM merge_requests WHERE status = %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (status, user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM merge_requests WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                        (status, limit),
                    )
                rows = cur.fetchall()
            return MergeRequestDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_all(limit: int = 1000, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM merge_requests WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM merge_requests ORDER BY created_at DESC LIMIT %s",
                        (limit,),
                    )
                rows = cur.fetchall()
            return MergeRequestDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def update(mr_id: str, updates: dict, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            updates["updated_at"] = _now()
            valid_cols = {
                "workspace_id", "title", "status", "priority",
                "author", "jira", "updated_at", "project_id", "mr_iid",
            }
            filtered = {k: v for k, v in updates.items() if k in valid_cols}
            if not filtered:
                return MergeRequestDB._get_by_id_with_conn(mr_id, conn, user_id=user_id)

            set_clause = ", ".join(f"{k} = %s" for k in filtered)
            values = list(filtered.values()) + [mr_id]
            where = "WHERE mr_id = %s"
            if user_id:
                where += " AND user_id = %s"
                values.append(user_id)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE merge_requests SET {set_clause} {where}", values)
            conn.commit()
            return MergeRequestDB._get_by_id_with_conn(mr_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def delete(mr_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("DELETE FROM merge_requests WHERE mr_id = %s AND user_id = %s", (mr_id, user_id))
                else:
                    cur.execute("DELETE FROM merge_requests WHERE mr_id = %s", (mr_id,))
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def add_note(mr_id: str, text: str, session_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO mr_notes (mr_id, text, session_id, created_at) VALUES (%s, %s, %s, %s)",
                    (mr_id, text, session_id, _now()),
                )
            conn.commit()
            return MergeRequestDB._get_by_id_with_conn(mr_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def update_status(mr_id: str, status: str, user_id: str = "") -> dict | None:
        return MergeRequestDB.update(mr_id, {"status": status}, user_id=user_id)

    # -- Session assignment --------------------------------------------------

    @staticmethod
    def assign_session(mr_id: str, session_id: str, role: str = "author") -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO mr_sessions (mr_id, session_id, role, assigned_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (mr_id, session_id) DO UPDATE SET role = EXCLUDED.role""",
                    (mr_id, session_id, role or "author", _now()),
                )
            conn.commit()
            return {"mr_id": mr_id, "session_id": session_id, "role": role}
        finally:
            release_connection(conn)

    @staticmethod
    def unassign_session(mr_id: str, session_id: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM mr_sessions WHERE mr_id = %s AND session_id = %s",
                    (mr_id, session_id),
                )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def list_sessions(mr_id: str) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT session_id, role, assigned_at FROM mr_sessions WHERE mr_id = %s ORDER BY assigned_at",
                    (mr_id,),
                )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_session(session_id: str) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ms.mr_id, ms.role, ms.assigned_at FROM mr_sessions ms WHERE ms.session_id = %s",
                    (session_id,),
                )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_full_by_session(session_id: str) -> list[dict]:
        """Return full MR records linked to a session (JOIN with merge_requests)."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT m.*, ms.role, ms.assigned_at AS session_assigned_at
                       FROM mr_sessions ms
                       JOIN merge_requests m ON m.mr_id = ms.mr_id
                       WHERE ms.session_id = %s
                       ORDER BY ms.assigned_at DESC""",
                    (session_id,),
                )
                rows = cur.fetchall()
            return MergeRequestDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)
