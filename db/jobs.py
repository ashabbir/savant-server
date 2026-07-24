"""JobDB — Persistent job queue backed by PostgreSQL."""

import json
import uuid
from db.base import _now, _row_to_dict, _rows_to_dicts
from postgres_client import get_connection, release_connection


_JSON_FIELDS = {"result": {}}


class JobDB:
    """CRUD for the jobs table — persistent job queue."""

    @staticmethod
    def _get_job_with_conn(job_id: str, conn) -> dict | None:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
        return _row_to_dict(row, _JSON_FIELDS)

    @staticmethod
    def create_job(job_type: str, target: str) -> dict:
        """Insert a new queued job. Returns the job dict."""
        conn = get_connection()
        try:
            job_id = str(uuid.uuid4())
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jobs (id, job_type, target, status, progress, phase,
                                         message, created_at, result)
                       VALUES (%s, %s, %s, 'queued', 0, 'Queued', '', %s, '{}')""",
                    (job_id, job_type, target, now),
                )
            conn.commit()
            return {
                "id": job_id, "job_type": job_type, "target": target,
                "status": "queued", "progress": 0, "phase": "Queued",
                "message": "", "created_at": now, "started_at": None,
                "finished_at": None, "result": {},
            }
        finally:
            release_connection(conn)

    @staticmethod
    def get_job(job_id: str) -> dict | None:
        conn = get_connection()
        try:
            return JobDB._get_job_with_conn(job_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def find_active(job_type: str, target: str) -> dict | None:
        """Find a queued or running job for the same (type, target)."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM jobs
                       WHERE job_type = %s AND target = %s AND status IN ('queued', 'running')
                       ORDER BY created_at DESC LIMIT 1""",
                    (job_type, target),
                )
                row = cur.fetchone()
            return _row_to_dict(row, _JSON_FIELDS)
        finally:
            release_connection(conn)

    @staticmethod
    def find_active_types(job_types: list[str], target: str) -> dict | None:
        """Find a queued or running job matching any of the specified types and target."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT * FROM jobs
                       WHERE job_type = ANY(%s) AND target = %s AND status IN ('queued', 'running')
                       ORDER BY created_at DESC LIMIT 1""",
                    (list(job_types), target),
                )
                row = cur.fetchone()
            return _row_to_dict(row, _JSON_FIELDS)
        finally:
            release_connection(conn)

    @staticmethod
    def next_queued() -> dict | None:
        """Atomically claim and return the oldest queued job (FIFO).

        SKIP LOCKED permits multiple Gunicorn worker threads to poll safely without
        executing the same row. The status transition commits in this transaction.
        """
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """WITH candidate AS (
                           SELECT id FROM jobs
                           WHERE status = 'queued'
                           ORDER BY created_at ASC
                           FOR UPDATE SKIP LOCKED
                           LIMIT 1
                       )
                       UPDATE jobs AS j
                       SET status = 'running', started_at = %s, phase = 'Starting'
                       FROM candidate
                       WHERE j.id = candidate.id
                       RETURNING j.*""",
                    (_now(),),
                )
                row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row, _JSON_FIELDS)
        finally:
            release_connection(conn)

    @staticmethod
    def set_running(job_id: str):
        """Compatibility no-op transition for callers holding an atomic claim."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, %s)
                       WHERE id = %s AND status IN ('queued', 'running')""",
                    (_now(), job_id),
                )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def update_progress(job_id: str, progress: int, phase: str = "", message: str = ""):
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET progress = %s, phase = %s, message = %s WHERE id = %s",
                    (min(max(progress, 0), 100), phase, message, job_id),
                )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def set_done(job_id: str, result: dict | None = None):
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs SET status = 'done', progress = 100, phase = 'Complete',
                                      finished_at = %s, result = %s WHERE id = %s""",
                    (_now(), json.dumps(result or {}), job_id),
                )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def set_failed(job_id: str, error_message: str):
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs SET status = 'failed', phase = 'Failed',
                                      message = %s, finished_at = %s WHERE id = %s""",
                    (error_message[:2000], _now(), job_id),
                )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def set_cancelled(job_id: str):
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs SET status = 'cancelled', phase = 'Cancelled',
                                      finished_at = %s WHERE id = %s""",
                    (_now(), job_id),
                )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def is_cancel_requested(job_id: str) -> bool:
        """Check if job status has been set to 'cancelling' by the cancel endpoint."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM jobs WHERE id = %s", (job_id,)
                )
                row = cur.fetchone()
            return row is not None and row["status"] == "cancelling"
        finally:
            release_connection(conn)

    @staticmethod
    def request_cancel(job_id: str) -> bool:
        """Mark a job for cancellation. Returns True if the job was running/queued."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jobs SET status = 'cancelling', phase = 'Cancelling'
                       WHERE id = %s AND status IN ('queued', 'running')""",
                    (job_id,),
                )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def list_jobs(status: str | None = None, target: str | None = None,
                  limit: int = 20) -> list[dict]:
        conn = get_connection()
        try:
            sql = "SELECT * FROM jobs"
            params = []
            clauses = []
            if status:
                clauses.append("status = %s")
                params.append(status)
            if target:
                clauses.append("target = %s")
                params.append(target)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT %s"
            params.append(min(limit, 100))
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            return _rows_to_dicts(rows, _JSON_FIELDS)
        finally:
            release_connection(conn)

    @staticmethod
    def delete_job(job_id: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM jobs WHERE id = %s AND status IN ('done', 'failed', 'cancelled')",
                    (job_id,),
                )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def cleanup_old(max_age_hours: int = 72):
        """Remove finished jobs older than max_age_hours."""
        conn = get_connection()
        try:
            from datetime import datetime, timezone, timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM jobs WHERE status IN ('done', 'failed', 'cancelled')
                       AND finished_at < %s""",
                    (cutoff,),
                )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def get_running_job() -> dict | None:
        """Get the currently running job, if any."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM jobs WHERE status = 'running' LIMIT 1"
                )
                row = cur.fetchone()
            return _row_to_dict(row, _JSON_FIELDS)
        finally:
            release_connection(conn)
