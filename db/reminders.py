"""ReminderDB — PostgreSQL backend for user reminders."""

from datetime import datetime, timezone, timedelta
from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection


class ReminderDB:

    @staticmethod
    def _get_by_id_with_conn(reminder_id: str, conn, user_id: str = "") -> dict | None:
        with conn.cursor() as cur:
            if user_id:
                cur.execute(
                    "SELECT * FROM reminders WHERE reminder_id = %s AND user_id = %s", (reminder_id, user_id)
                )
            else:
                cur.execute(
                    "SELECT * FROM reminders WHERE reminder_id = %s", (reminder_id,)
                )
            row = cur.fetchone()
        return _row_to_dict(row) if row else None

    @staticmethod
    def create(reminder: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO reminders
                       (reminder_id, title, description, priority, status,
                        start_date, due_date, remind_before_hrs, notified, created_at, updated_at, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        reminder["reminder_id"],
                        reminder.get("title", ""),
                        reminder.get("description", ""),
                        reminder.get("priority", "medium"),
                        reminder.get("status", "pending"),
                        reminder.get("start_date", now),
                        reminder["due_date"],
                        reminder.get("remind_before_hrs", 1),
                        0,
                        reminder.get("created_at", now),
                        reminder.get("updated_at", now),
                        reminder.get("user_id", ""),
                    ),
                )
            conn.commit()
            return ReminderDB._get_by_id_with_conn(reminder["reminder_id"], conn, user_id=reminder.get("user_id", ""))
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(reminder_id: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            return ReminderDB._get_by_id_with_conn(reminder_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def list_all(status: str | None = None, user_id: str = "") -> list[dict]:
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
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM reminders {where} ORDER BY due_date ASC, created_at ASC",
                    params,
                )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_due_soon(within_hrs: int = 1, user_id: str = "") -> list[dict]:
        """Return pending reminders whose due_date is within within_hrs from now."""
        conn = get_connection()
        try:
            now = datetime.now(timezone.utc)
            window_end = (now + timedelta(hours=within_hrs)).isoformat()
            clauses = ["status = 'pending'", "due_date <= %s", "due_date >= %s"]
            params: list = [window_end, now.isoformat()]
            if user_id:
                clauses.append("user_id = %s")
                params.append(user_id)
            where = "WHERE " + " AND ".join(clauses)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM reminders {where} ORDER BY due_date ASC",
                    params,
                )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_overdue(user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            now = datetime.now(timezone.utc).isoformat()
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM reminders WHERE status = 'pending' AND due_date < %s AND user_id = %s ORDER BY due_date ASC",
                        (now, user_id),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM reminders WHERE status = 'pending' AND due_date < %s ORDER BY due_date ASC",
                        (now,),
                    )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_due_today(user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        """SELECT * FROM reminders
                           WHERE status = 'pending'
                             AND due_date LIKE %s
                             AND user_id = %s
                           ORDER BY due_date ASC""",
                        (f"{today}%", user_id),
                    )
                else:
                    cur.execute(
                        """SELECT * FROM reminders
                           WHERE status = 'pending'
                             AND due_date LIKE %s
                           ORDER BY due_date ASC""",
                        (f"{today}%",),
                    )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def update(reminder_id: str, updates: dict, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            updates["updated_at"] = _now()
            valid_cols = {
                "title", "description", "priority", "status",
                "due_date", "remind_before_hrs", "notified", "updated_at",
            }
            filtered = {k: v for k, v in updates.items() if k in valid_cols}
            if not filtered:
                return ReminderDB._get_by_id_with_conn(reminder_id, conn, user_id=user_id)
            set_clause = ", ".join(f"{k} = %s" for k in filtered)
            values = list(filtered.values()) + [reminder_id]
            where = "WHERE reminder_id = %s"
            if user_id:
                where += " AND user_id = %s"
                values.append(user_id)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE reminders SET {set_clause} {where}", values
                )
            conn.commit()
            return ReminderDB._get_by_id_with_conn(reminder_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def complete(reminder_id: str, user_id: str = "") -> dict | None:
        return ReminderDB.update(reminder_id, {"status": "done"}, user_id=user_id)

    @staticmethod
    def dismiss(reminder_id: str, user_id: str = "") -> dict | None:
        return ReminderDB.update(reminder_id, {"status": "dismissed"}, user_id=user_id)

    @staticmethod
    def mark_notified(reminder_id: str, user_id: str = "") -> dict | None:
        return ReminderDB.update(reminder_id, {"notified": 1}, user_id=user_id)

    @staticmethod
    def delete(reminder_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "DELETE FROM reminders WHERE reminder_id = %s AND user_id = %s", (reminder_id, user_id)
                    )
                else:
                    cur.execute(
                        "DELETE FROM reminders WHERE reminder_id = %s", (reminder_id,)
                    )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)
