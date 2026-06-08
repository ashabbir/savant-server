"""JiraTicketDB — PostgreSQL backend."""

from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection


class JiraTicketDB:

    @staticmethod
    def _enrich_with_notes(ticket: dict, conn=None) -> dict:
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT text, session_id, created_at FROM jira_notes WHERE ticket_id = %s ORDER BY created_at",
                    (ticket["ticket_id"],),
                )
                rows = cur.fetchall()
            ticket["notes"] = [dict(r) for r in rows]
            return ticket
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def _enrich_list(tickets: list[dict], conn=None) -> list[dict]:
        if not tickets:
            return tickets
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            ids = [t["ticket_id"] for t in tickets]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ticket_id, text, session_id, created_at FROM jira_notes WHERE ticket_id = ANY(%s) ORDER BY created_at",
                    (ids,),
                )
                rows = cur.fetchall()
            notes_map: dict[str, list] = {}
            for r in rows:
                notes_map.setdefault(r["ticket_id"], []).append(
                    {"text": r["text"], "session_id": r["session_id"], "created_at": r["created_at"]}
                )
            for t in tickets:
                t["notes"] = notes_map.get(t["ticket_id"], [])
            return tickets
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def _get_by_id_with_conn(ticket_id: str, conn, user_id: str = "") -> dict | None:
        with conn.cursor() as cur:
            if user_id:
                cur.execute("SELECT * FROM jira_tickets WHERE ticket_id = %s AND user_id = %s", (ticket_id, user_id))
            else:
                cur.execute("SELECT * FROM jira_tickets WHERE ticket_id = %s", (ticket_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return JiraTicketDB._enrich_with_notes(_row_to_dict(row), conn=conn)

    @staticmethod
    def create(ticket: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jira_tickets
                       (ticket_id, workspace_id, ticket_key, title, status, priority,
                        assignee, reporter, created_at, updated_at, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        ticket["ticket_id"], ticket["workspace_id"], ticket["ticket_key"],
                        ticket.get("title", ""), ticket.get("status", "todo"),
                        ticket.get("priority", "medium"), ticket.get("assignee", ""),
                        ticket.get("reporter", ""), ticket.get("created_at", now),
                        ticket.get("updated_at", now), ticket.get("user_id", ""),
                    ),
                )
                for note in ticket.get("notes", []):
                    cur.execute(
                        "INSERT INTO jira_notes (ticket_id, text, session_id, created_at) VALUES (%s, %s, %s, %s)",
                        (ticket["ticket_id"], note.get("text", ""), note.get("session_id", ""), note.get("created_at", now)),
                    )
            conn.commit()
            return JiraTicketDB._get_by_id_with_conn(ticket["ticket_id"], conn, user_id=ticket.get("user_id", ""))
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(ticket_id: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            return JiraTicketDB._get_by_id_with_conn(ticket_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_key(ticket_key: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("SELECT * FROM jira_tickets WHERE ticket_key = %s AND user_id = %s", (ticket_key, user_id))
                else:
                    cur.execute("SELECT * FROM jira_tickets WHERE ticket_key = %s", (ticket_key,))
                row = cur.fetchone()
            if row is None:
                return None
            return JiraTicketDB._enrich_with_notes(_row_to_dict(row), conn=conn)
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
                    f"SELECT * FROM jira_tickets {where} ORDER BY created_at DESC LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
            return JiraTicketDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_status(status: str, limit: int = 1000, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM jira_tickets WHERE status = %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (status, user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM jira_tickets WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                        (status, limit),
                    )
                rows = cur.fetchall()
            return JiraTicketDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_assignee(assignee: str, limit: int = 1000, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM jira_tickets WHERE assignee = %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (assignee, user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM jira_tickets WHERE assignee = %s ORDER BY created_at DESC LIMIT %s",
                        (assignee, limit),
                    )
                rows = cur.fetchall()
            return JiraTicketDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_all(limit: int = 1000, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM jira_tickets WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM jira_tickets ORDER BY created_at DESC LIMIT %s",
                        (limit,),
                    )
                rows = cur.fetchall()
            return JiraTicketDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def update(ticket_id: str, updates: dict, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            updates["updated_at"] = _now()
            valid_cols = {
                "workspace_id", "title", "status", "priority",
                "assignee", "reporter", "updated_at", "ticket_key",
            }
            filtered = {k: v for k, v in updates.items() if k in valid_cols}
            if not filtered:
                return JiraTicketDB._get_by_id_with_conn(ticket_id, conn, user_id=user_id)

            set_clause = ", ".join(f"{k} = %s" for k in filtered)
            values = list(filtered.values()) + [ticket_id]
            where = "WHERE ticket_id = %s"
            if user_id:
                where += " AND user_id = %s"
                values.append(user_id)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE jira_tickets SET {set_clause} {where}", values)
            conn.commit()
            return JiraTicketDB._get_by_id_with_conn(ticket_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def delete(ticket_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("DELETE FROM jira_tickets WHERE ticket_id = %s AND user_id = %s", (ticket_id, user_id))
                else:
                    cur.execute("DELETE FROM jira_tickets WHERE ticket_id = %s", (ticket_id,))
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def add_note(ticket_id: str, text: str, session_id: str = "", user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO jira_notes (ticket_id, text, session_id, created_at) VALUES (%s, %s, %s, %s)",
                    (ticket_id, text, session_id, _now()),
                )
            conn.commit()
            return JiraTicketDB._get_by_id_with_conn(ticket_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def update_status(ticket_id: str, status: str, user_id: str = "") -> dict | None:
        return JiraTicketDB.update(ticket_id, {"status": status}, user_id=user_id)

    @staticmethod
    def update_assignee(ticket_id: str, assignee: str, user_id: str = "") -> dict | None:
        return JiraTicketDB.update(ticket_id, {"assignee": assignee}, user_id=user_id)

    # -- Session assignment --------------------------------------------------

    @staticmethod
    def assign_session(ticket_id: str, session_id: str, role: str = "assignee") -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jira_sessions (ticket_id, session_id, role, assigned_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (ticket_id, session_id) DO UPDATE SET role = EXCLUDED.role""",
                    (ticket_id, session_id, role or "assignee", _now()),
                )
            conn.commit()
            return {"ticket_id": ticket_id, "session_id": session_id, "role": role}
        finally:
            release_connection(conn)

    @staticmethod
    def unassign_session(ticket_id: str, session_id: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM jira_sessions WHERE ticket_id = %s AND session_id = %s",
                    (ticket_id, session_id),
                )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def list_sessions(ticket_id: str) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT session_id, role, assigned_at FROM jira_sessions WHERE ticket_id = %s ORDER BY assigned_at",
                    (ticket_id,),
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
                    "SELECT js.ticket_id, js.role, js.assigned_at FROM jira_sessions js WHERE js.session_id = %s",
                    (session_id,),
                )
                rows = cur.fetchall()
            return [dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_full_by_session(session_id: str, user_id: str = "") -> list[dict]:
        """Return full Jira ticket records linked to a session (JOIN with jira_tickets)."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        """SELECT t.*, js.role, js.assigned_at AS session_assigned_at
                           FROM jira_sessions js
                           JOIN jira_tickets t ON t.ticket_id = js.ticket_id
                           WHERE js.session_id = %s AND t.user_id = %s
                           ORDER BY js.assigned_at DESC""",
                        (session_id, user_id),
                    )
                else:
                    cur.execute(
                        """SELECT t.*, js.role, js.assigned_at AS session_assigned_at
                           FROM jira_sessions js
                           JOIN jira_tickets t ON t.ticket_id = js.ticket_id
                           WHERE js.session_id = %s
                           ORDER BY js.assigned_at DESC""",
                        (session_id,),
                    )
                rows = cur.fetchall()
            return JiraTicketDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)
