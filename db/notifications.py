"""NotificationDB — PostgreSQL backend."""

import json
from datetime import datetime, timezone, timedelta
from db.base import _now, _row_to_dict as _base_row
from postgres_client import get_connection, release_connection


def _row_to_dict(row):
    d = _base_row(row, json_fields={"detail": {}})
    if d and "read" in d:
        d["read"] = bool(d["read"])
    return d


class NotificationDB:

    @staticmethod
    def _get_by_id_with_conn(notification_id: str, conn, user_id: str = "") -> dict | None:
        with conn.cursor() as cur:
            if user_id:
                cur.execute(
                    "SELECT * FROM notifications WHERE notification_id = %s AND user_id = %s",
                    (notification_id, user_id),
                )
            else:
                cur.execute(
                    "SELECT * FROM notifications WHERE notification_id = %s",
                    (notification_id,),
                )
            row = cur.fetchone()
        return _row_to_dict(row)

    @staticmethod
    def create(notification: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            detail = notification.get("detail", {})
            if not isinstance(detail, str):
                detail = json.dumps(detail)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notifications
                       (notification_id, event_type, message, detail,
                        workspace_id, session_id, read, created_at, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        notification["notification_id"],
                        notification.get("event_type", ""),
                        notification.get("message", ""),
                        detail,
                        notification.get("workspace_id"),
                        notification.get("session_id"),
                        1 if notification.get("read") else 0,
                        notification.get("created_at", now),
                        notification.get("user_id", ""),
                    ),
                )
            conn.commit()
            return NotificationDB._get_by_id_with_conn(notification["notification_id"], conn, user_id=notification.get("user_id", ""))
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(notification_id: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            return NotificationDB._get_by_id_with_conn(notification_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def list_recent(limit: int = 50, since_id: str | None = None, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if since_id:
                    cur.execute(
                        "SELECT created_at FROM notifications WHERE notification_id = %s",
                        (since_id,),
                    )
                    ref = cur.fetchone()
                    if ref:
                        if user_id:
                            cur.execute(
                                "SELECT * FROM notifications WHERE created_at > %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                                (ref["created_at"], user_id, limit),
                            )
                        else:
                            cur.execute(
                                "SELECT * FROM notifications WHERE created_at > %s ORDER BY created_at DESC LIMIT %s",
                                (ref["created_at"], limit),
                            )
                        rows = cur.fetchall()
                    else:
                        if user_id:
                            cur.execute(
                                "SELECT * FROM notifications WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                                (user_id, limit),
                            )
                        else:
                            cur.execute(
                                "SELECT * FROM notifications ORDER BY created_at DESC LIMIT %s",
                                (limit,),
                            )
                        rows = cur.fetchall()
                else:
                    if user_id:
                        cur.execute(
                            "SELECT * FROM notifications WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                            (user_id, limit),
                        )
                    else:
                        cur.execute(
                            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT %s",
                            (limit,),
                        )
                    rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_unread(limit: int = 50, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM notifications WHERE read = 0 AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM notifications WHERE read = 0 ORDER BY created_at DESC LIMIT %s",
                        (limit,),
                    )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_workspace(workspace_id: str, limit: int = 50, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM notifications WHERE workspace_id = %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (workspace_id, user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM notifications WHERE workspace_id = %s ORDER BY created_at DESC LIMIT %s",
                        (workspace_id, limit),
                    )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_session(session_id: str, limit: int = 50) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM notifications WHERE session_id = %s ORDER BY created_at DESC LIMIT %s",
                    (session_id, limit),
                )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def mark_as_read(notification_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "UPDATE notifications SET read = 1 WHERE notification_id = %s AND user_id = %s",
                        (notification_id, user_id),
                    )
                else:
                    cur.execute(
                        "UPDATE notifications SET read = 1 WHERE notification_id = %s",
                        (notification_id,),
                    )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def mark_all_as_read(user_id: str = "") -> int:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("UPDATE notifications SET read = 1 WHERE read = 0 AND user_id = %s", (user_id,))
                else:
                    cur.execute("UPDATE notifications SET read = 1 WHERE read = 0")
                count = cur.rowcount
            conn.commit()
            return count
        finally:
            release_connection(conn)

    @staticmethod
    def delete(notification_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "DELETE FROM notifications WHERE notification_id = %s AND user_id = %s",
                        (notification_id, user_id),
                    )
                else:
                    cur.execute(
                        "DELETE FROM notifications WHERE notification_id = %s",
                        (notification_id,),
                    )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def delete_old(days: int = 30) -> int:
        conn = get_connection()
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM notifications WHERE created_at < %s",
                    (cutoff,),
                )
                count = cur.rowcount
            conn.commit()
            return count
        finally:
            release_connection(conn)

    @staticmethod
    def count_unread(user_id: str = "") -> int:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT COUNT(*) as cnt FROM notifications WHERE read = 0 AND user_id = %s",
                        (user_id,),
                    )
                else:
                    cur.execute(
                        "SELECT COUNT(*) as cnt FROM notifications WHERE read = 0"
                    )
                row = cur.fetchone()
            return row["cnt"] if row else 0
        finally:
            release_connection(conn)
