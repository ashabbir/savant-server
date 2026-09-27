"""NotificationDB — PostgreSQL backend."""

import json
import uuid
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
                    "SELECT * FROM notifications WHERE notification_id = %s AND (user_id = %s OR user_id = '' OR user_id IS NULL)",
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
            notification_id = str(notification.get("notification_id") or uuid.uuid4())
            event_type = str(notification.get("event_type") or "system")
            message = str(notification.get("message") or "")
            detail = notification.get("detail", {})
            if isinstance(detail, str):
                try:
                    detail = json.loads(detail)
                except Exception:
                    detail = {"raw": detail}
            elif not isinstance(detail, dict):
                detail = {"value": detail}
            else:
                detail = dict(detail)

            if "level" in notification and "level" not in detail:
                detail["level"] = notification["level"]

            detail_json = json.dumps(detail)
            user_id = notification.get("user_id", "")

            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO notifications
                       (notification_id, event_type, message, detail,
                        workspace_id, session_id, read, created_at, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        notification_id,
                        event_type,
                        message,
                        detail_json,
                        notification.get("workspace_id"),
                        notification.get("session_id"),
                        1 if notification.get("read") else 0,
                        notification.get("created_at", now),
                        user_id,
                    ),
                )
            conn.commit()
            return NotificationDB._get_by_id_with_conn(notification_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def notify(
        message: str,
        event_type: str = "system",
        level: str = "info",
        detail: dict | None = None,
        user_id: str = "",
        workspace_id: str | None = None,
        session_id: str | None = None,
    ) -> dict:
        """Convenience method to dispatch a notification."""
        payload = {
            "notification_id": str(uuid.uuid4()),
            "event_type": event_type,
            "message": message,
            "detail": {**(detail or {}), "level": level},
            "user_id": user_id,
            "workspace_id": workspace_id,
            "session_id": session_id,
        }
        return NotificationDB.create(payload)

    @staticmethod
    def notify_job_failure(
        job_id: str,
        job_type: str,
        target: str,
        error: str,
        user_id: str = "",
        payload: dict | None = None,
    ) -> dict:
        """Create a high-priority notification when a background job fails."""
        human_job = {
            "initial_repo_sync": "Repository download & index",
            "initial_repo_processing": "Repository indexing & analysis",
            "index": "Semantic indexing",
            "reindex": "Repository re-indexing",
            "ast": "AST structure analysis",
            "codegraph_index": "CodeGraph build",
            "codegraph_sync": "CodeGraph synchronization",
            "differential_sync": "Differential synchronization",
            "index-all": "Batch indexing",
            "ast-all": "Batch AST analysis",
        }.get(job_type, job_type)

        short_err = (error or "Unknown error").strip()
        if len(short_err) > 300:
            short_err = short_err[:297] + "..."

        msg = f"Job failed ({human_job}) for '{target}': {short_err}"
        return NotificationDB.notify(
            message=msg,
            event_type="job_failed",
            level="error",
            detail={
                "job_id": job_id,
                "job_type": job_type,
                "target": target,
                "error": error,
                "payload": payload or {},
            },
            user_id=user_id,
        )

    @staticmethod
    def notify_job_success(
        job_id: str,
        job_type: str,
        target: str,
        result: dict | None = None,
        user_id: str = "",
    ) -> dict:
        """Create a notification when a background job completes successfully."""
        human_job = {
            "initial_repo_sync": "Repository download & index completed",
            "initial_repo_processing": "Repository indexing & analysis completed",
            "index": "Semantic indexing completed",
            "reindex": "Repository re-indexing completed",
            "ast": "AST structure analysis completed",
            "codegraph_index": "CodeGraph build completed",
            "codegraph_sync": "CodeGraph synchronization completed",
            "differential_sync": "Differential sync completed",
            "index-all": "Batch indexing completed",
            "ast-all": "Batch AST analysis completed",
        }.get(job_type, f"{job_type} completed")

        msg = f"{human_job} for '{target}'"
        return NotificationDB.notify(
            message=msg,
            event_type="job_completed",
            level="info",
            detail={
                "job_id": job_id,
                "job_type": job_type,
                "target": target,
                "result": result or {},
            },
            user_id=user_id,
        )

    @staticmethod
    def notify_sync_failure(
        repo_name: str,
        error: str,
        errors: list[str] | None = None,
        user_id: str = "",
    ) -> dict:
        """Create a notification when a scheduled/periodic repository sync encounters failures."""
        err_text = error or ("; ".join(errors) if errors else "Periodic sync failed")
        short_err = err_text.strip()
        if len(short_err) > 300:
            short_err = short_err[:297] + "..."
        msg = f"Sync failed for repository '{repo_name}': {short_err}"
        return NotificationDB.notify(
            message=msg,
            event_type="sync_failed",
            level="warning",
            detail={
                "repo_name": repo_name,
                "error": error,
                "errors": errors or [],
            },
            user_id=user_id,
        )

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
                                "SELECT * FROM notifications WHERE created_at > %s AND (user_id = %s OR user_id = '' OR user_id IS NULL) ORDER BY created_at DESC LIMIT %s",
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
                                "SELECT * FROM notifications WHERE (user_id = %s OR user_id = '' OR user_id IS NULL) ORDER BY created_at DESC LIMIT %s",
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
                            "SELECT * FROM notifications WHERE (user_id = %s OR user_id = '' OR user_id IS NULL) ORDER BY created_at DESC LIMIT %s",
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
                        "SELECT * FROM notifications WHERE read = 0 AND (user_id = %s OR user_id = '' OR user_id IS NULL) ORDER BY created_at DESC LIMIT %s",
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
                        "SELECT * FROM notifications WHERE workspace_id = %s AND (user_id = %s OR user_id = '' OR user_id IS NULL) ORDER BY created_at DESC LIMIT %s",
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
                        "SELECT COUNT(*) as cnt FROM notifications WHERE read = 0 AND (user_id = %s OR user_id = '' OR user_id IS NULL)",
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
