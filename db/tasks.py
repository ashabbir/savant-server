"""TaskDB — PostgreSQL backend."""

import json
from datetime import datetime, timedelta
from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection


def _next_available_workday(start_date_str: str, ended_days=None, work_week=None) -> str:
    """Return next available workday date string skipping non-workdays and ended days."""
    if not isinstance(work_week, (list, tuple, set)):
        work_week = [1, 2, 3, 4, 5]
    if not isinstance(ended_days, (set, list, tuple)):
        ended_days = set()
    else:
        ended_days = set(ended_days)

    valid_workdays = set(work_week)
    use_iso = max(valid_workdays) > 6 if valid_workdays else False

    try:
        dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return start_date_str

    for _ in range(14):
        dt += timedelta(days=1)
        ds = dt.strftime("%Y-%m-%d")
        iso_w = dt.isoweekday()
        js_w = 0 if iso_w == 7 else iso_w
        is_workday = (iso_w in valid_workdays) if use_iso else (js_w in valid_workdays)
        if is_workday and ds not in ended_days:
            return ds
    return start_date_str


class TaskDB:

    @staticmethod
    def clear_all() -> None:
        """Clear all tasks, task dependencies, and ended days for testing isolation."""
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM task_deps")
                cur.execute("DELETE FROM colosseum_tasks")
                cur.execute("DELETE FROM tasks")
                cur.execute("DELETE FROM task_ended_days")
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def ensure_indexes():
        """No-op: indexes created in schema."""
        pass

    @staticmethod
    def _get_by_id_with_conn(task_id: str, conn, user_id: str = "") -> dict | None:
        with conn.cursor() as cur:
            if user_id:
                cur.execute("SELECT * FROM tasks WHERE task_id = %s AND user_id = %s", (task_id, user_id))
            else:
                cur.execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,))
            row = cur.fetchone()
        if row is None:
            return None
        task = TaskDB._enrich_with_deps(_row_to_dict(row), conn=conn)
        return TaskDB._enrich_with_colosseum(task, conn=conn)

    @staticmethod
    def _next_seq(conn=None) -> int:
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE counters SET value = value + 1 WHERE name = 'task_seq' RETURNING value")
                row = cur.fetchone()
                if row is None:
                    # Counter missing — initialize it
                    cur.execute("INSERT INTO counters (name, value) VALUES ('task_seq', 1) ON CONFLICT (name) DO NOTHING")
                    conn.commit()
                    return 1
                conn.commit()
                return row["value"]
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def _enrich_with_deps(task: dict, conn=None) -> dict:
        """Attach depends_on list from task_deps table."""
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT depends_on FROM task_deps WHERE task_id = %s",
                    (task["task_id"],),
                )
                rows = cur.fetchall()
            task["depends_on"] = [r["depends_on"] for r in rows]
            return task
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def _enrich_list(tasks: list[dict], conn=None) -> list[dict]:
        """Batch-enrich a list of tasks with dependencies."""
        if not tasks:
            return tasks
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            task_ids = [t["task_id"] for t in tasks]
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id, depends_on FROM task_deps WHERE task_id = ANY(%s)",
                    (task_ids,),
                )
                rows = cur.fetchall()
            deps_map: dict[str, list[str]] = {}
            for r in rows:
                deps_map.setdefault(r["task_id"], []).append(r["depends_on"])
            for t in tasks:
                t["depends_on"] = deps_map.get(t["task_id"], [])
            return TaskDB._enrich_list_with_colosseum(tasks, conn=conn)
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def _enrich_with_colosseum(task: dict, conn=None) -> dict:
        return TaskDB._enrich_list_with_colosseum([task], conn=conn)[0]

    @staticmethod
    def _enrich_list_with_colosseum(tasks: list[dict], conn=None) -> list[dict]:
        if not tasks:
            return tasks
        local_conn = False
        if conn is None:
            conn = get_connection()
            local_conn = True
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT task_id, ready, config FROM colosseum_tasks WHERE task_id = ANY(%s)", ([task["task_id"] for task in tasks],))
                rows = cur.fetchall()
            by_task = {row["task_id"]: row for row in rows}
            for task in tasks:
                execution = by_task.get(task["task_id"])
                task["colosseum_ready"] = bool(execution and execution["ready"])
                task["colosseum_config"] = execution["config"] if execution else {}
            return tasks
        finally:
            if local_conn:
                release_connection(conn)

    @staticmethod
    def create(task: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            seq = task.get("seq") or TaskDB._next_seq(conn=conn)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO tasks
                       (task_id, seq, workspace_id, title, description, status, priority,
                        date, "order", created_at, updated_at, created_session_id, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (task_id) DO UPDATE SET
                         workspace_id=EXCLUDED.workspace_id,
                         title=EXCLUDED.title,
                         description=EXCLUDED.description,
                         status=EXCLUDED.status,
                         priority=EXCLUDED.priority,
                         date=EXCLUDED.date,
                         "order"=EXCLUDED."order",
                         updated_at=EXCLUDED.updated_at,
                         user_id=EXCLUDED.user_id""",
                    (
                        task["task_id"], seq, task["workspace_id"],
                        task.get("title", ""), task.get("description", ""),
                        task.get("status", "todo"), task.get("priority", "medium"),
                        task.get("date"), task.get("order", 0),
                        task.get("created_at", now), task.get("updated_at", now),
                        task.get("created_session_id"), task.get("user_id", ""),
                    ),
                )
                # Insert dependencies if provided
                for dep_id in task.get("depends_on", []):
                    cur.execute(
                        "INSERT INTO task_deps (task_id, depends_on) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (task["task_id"], dep_id),
                    )
            conn.commit()
            return TaskDB._get_by_id_with_conn(task["task_id"], conn, user_id=task.get("user_id", ""))
        finally:
            release_connection(conn)

    @staticmethod
    def bulk_upsert(tasks: list[dict]) -> int:
        conn = get_connection()
        try:
            now = _now()
            count = 0
            with conn.cursor() as cur:
                for task in tasks:
                    cur.execute(
                        """INSERT INTO tasks
                           (task_id, seq, workspace_id, title, description, status, priority,
                            date, "order", created_at, updated_at, created_session_id, user_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (task_id) DO UPDATE SET
                             seq=EXCLUDED.seq, workspace_id=EXCLUDED.workspace_id,
                             title=EXCLUDED.title, description=EXCLUDED.description,
                             status=EXCLUDED.status, priority=EXCLUDED.priority,
                             date=EXCLUDED.date, "order"=EXCLUDED."order",
                             updated_at=EXCLUDED.updated_at,
                             created_session_id=EXCLUDED.created_session_id,
                             user_id=EXCLUDED.user_id""",
                        (
                            task["task_id"], task.get("seq"), task["workspace_id"],
                            task.get("title", ""), task.get("description", ""),
                            task.get("status", "todo"), task.get("priority", "medium"),
                            task.get("date"), task.get("order", 0),
                            task.get("created_at", now), task.get("updated_at", now),
                            task.get("created_session_id"), task.get("user_id", ""),
                        ),
                    )
                    # Upsert deps
                    cur.execute("DELETE FROM task_deps WHERE task_id = %s", (task["task_id"],))
                    for dep_id in task.get("depends_on", []):
                        cur.execute(
                            "INSERT INTO task_deps (task_id, depends_on) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (task["task_id"], dep_id),
                        )
                    count += 1
            conn.commit()
            return count
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(task_id: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            return TaskDB._get_by_id_with_conn(task_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_seq(seq: int, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("SELECT * FROM tasks WHERE seq = %s AND user_id = %s", (seq, user_id))
                else:
                    cur.execute("SELECT * FROM tasks WHERE seq = %s", (seq,))
                row = cur.fetchone()
            if row is None:
                return None
            return TaskDB._enrich_with_deps(_row_to_dict(row), conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def resolve_id(ref: str, user_id: str = "") -> dict | None:
        """Resolve 'T-42' style refs or plain task_id."""
        if ref and ref.upper().startswith("T-"):
            try:
                seq = int(ref.split("-", 1)[1])
                return TaskDB.get_by_seq(seq, user_id=user_id)
            except (ValueError, IndexError):
                pass
        return TaskDB.get_by_id(ref, user_id=user_id)

    @staticmethod
    def list_all(workspace_id: str | None = None, user_id: str = "", date: str | None = None, status: str | None = None) -> list[dict]:
        conn = get_connection()
        try:
            clauses = []
            params = []
            if workspace_id:
                clauses.append("workspace_id = %s")
                params.append(workspace_id)
            if user_id:
                clauses.append("user_id = %s")
                params.append(user_id)
            if date:
                clauses.append("date = %s")
                params.append(date)
            if status:
                clauses.append("status = %s")
                params.append(status)
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT * FROM tasks {where} ORDER BY date ASC, "order" ASC, created_at ASC',
                    params,
                )
                rows = cur.fetchall()
            return TaskDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_date(date_str: str, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        'SELECT * FROM tasks WHERE date = %s AND user_id = %s ORDER BY "order" ASC, created_at ASC',
                        (date_str, user_id),
                    )
                else:
                    cur.execute(
                        'SELECT * FROM tasks WHERE date = %s ORDER BY "order" ASC, created_at ASC',
                        (date_str,),
                    )
                rows = cur.fetchall()
            return TaskDB._enrich_list([dict(r) for r in rows], conn=conn)
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
                    f'SELECT * FROM tasks {where} ORDER BY date ASC, "order" ASC LIMIT %s',
                    params,
                )
                rows = cur.fetchall()
            return TaskDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_status(status: str, limit: int = 1000, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT * FROM tasks WHERE status = %s AND user_id = %s ORDER BY created_at DESC LIMIT %s",
                        (status, user_id, limit),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM tasks WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                        (status, limit),
                    )
                rows = cur.fetchall()
            return TaskDB._enrich_list([dict(r) for r in rows], conn=conn)
        finally:
            release_connection(conn)

    @staticmethod
    def list_dates(user_id: str = "") -> list[str]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT DISTINCT date FROM tasks WHERE date IS NOT NULL AND user_id = %s ORDER BY date ASC",
                        (user_id,),
                    )
                else:
                    cur.execute(
                        "SELECT DISTINCT date FROM tasks WHERE date IS NOT NULL ORDER BY date ASC"
                    )
                rows = cur.fetchall()
            return [r["date"] for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def update(task_id: str, updates: dict, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            updates["updated_at"] = _now()
            valid_cols = {
                "title", "description", "status", "priority",
                "date", "order", "updated_at", "workspace_id", "created_session_id", "comments",
            }
            filtered = {k: v for k, v in updates.items() if k in valid_cols}
            if not filtered:
                return TaskDB._get_by_id_with_conn(task_id, conn, user_id=user_id)

            set_clause = ", ".join(
                f'"order" = %s' if k == "order" else f"{k} = %s::jsonb" if k == "comments" else f"{k} = %s"
                for k in filtered
            )
            values = [json.dumps(v) if k == "comments" else v for k, v in filtered.items()] + [task_id]
            where = "WHERE task_id = %s"
            if user_id:
                where += " AND user_id = %s"
                values.append(user_id)
            with conn.cursor() as cur:
                cur.execute(f"UPDATE tasks SET {set_clause} {where}", values)
            conn.commit()
            return TaskDB._get_by_id_with_conn(task_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def update_status(task_id: str, status: str, user_id: str = "") -> dict | None:
        return TaskDB.update(task_id, {"status": status}, user_id=user_id)

    @staticmethod
    def set_colosseum_ready(task_id: str, config: dict, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            task = TaskDB._get_by_id_with_conn(task_id, conn, user_id=user_id)
            if not task:
                return None
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO colosseum_tasks (task_id, ready, config, updated_at)
                       VALUES (%s, TRUE, %s::jsonb, %s)
                       ON CONFLICT (task_id) DO UPDATE SET ready = TRUE, config = EXCLUDED.config, updated_at = EXCLUDED.updated_at""",
                    (task_id, json.dumps(config), now),
                )
                cur.execute("UPDATE tasks SET updated_at = %s WHERE task_id = %s", (now, task_id))
            conn.commit()
            return TaskDB._get_by_id_with_conn(task_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def claim_todo(task_id: str, user_id: str = "") -> dict | None:
        """Atomically move a todo task into active execution.

        The status predicate is the claim guard: competing workers can list the
        same task, but only one UPDATE succeeds.
        """
        conn = get_connection()
        try:
            where = """WHERE task_id = %s AND status IN ('grooming', 'ready')"""
            values = [_now(), task_id]
            user_where = where + " AND user_id = %s" if user_id else where
            user_values = values + [user_id] if user_id else values
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE tasks SET status = 'in-progress', updated_at = %s {user_where}",
                    user_values,
                )
                claimed = cur.rowcount == 1
                if not claimed and user_id:
                    cur.execute(
                        f"UPDATE tasks SET status = 'in-progress', updated_at = %s {where}",
                        values,
                    )
                    claimed = cur.rowcount == 1
            conn.commit()
            if not claimed:
                return None
            return TaskDB._get_by_id_with_conn(task_id, conn, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def delete(task_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("DELETE FROM tasks WHERE task_id = %s AND user_id = %s", (task_id, user_id))
                else:
                    cur.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def add_dependency(task_id: str, depends_on: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            task = TaskDB.get_by_id(task_id, user_id=user_id)
            if not task:
                return None
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO task_deps (task_id, depends_on) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (task_id, depends_on),
                )
            conn.commit()
            return TaskDB.get_by_id(task_id, user_id=user_id)
        except Exception:
            return None
        finally:
            release_connection(conn)

    @staticmethod
    def remove_dependency(task_id: str, depends_on: str, user_id: str = "") -> dict | None:
        conn = get_connection()
        try:
            task = TaskDB.get_by_id(task_id, user_id=user_id)
            if not task:
                return None
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM task_deps WHERE task_id = %s AND depends_on = %s",
                    (task_id, depends_on),
                )
            conn.commit()
            return TaskDB.get_by_id(task_id, user_id=user_id)
        finally:
            release_connection(conn)

    @staticmethod
    def reorder(date_str: str, ordered_ids: list[str], user_id: str = "") -> None:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                for idx, task_id in enumerate(ordered_ids):
                    if user_id:
                        cur.execute(
                            'UPDATE tasks SET "order" = %s WHERE task_id = %s AND user_id = %s',
                            (idx, task_id, user_id),
                        )
                    else:
                        cur.execute(
                            'UPDATE tasks SET "order" = %s WHERE task_id = %s',
                            (idx, task_id),
                        )
            conn.commit()
        finally:
            release_connection(conn)

    @staticmethod
    def move_incomplete_tasks(from_date: str, to_date: str, user_id: str = "") -> int:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "UPDATE tasks SET date = %s, updated_at = %s WHERE date = %s AND status != 'done' AND user_id = %s",
                        (to_date, now, from_date, user_id),
                    )
                else:
                    cur.execute(
                        "UPDATE tasks SET date = %s, updated_at = %s WHERE date = %s AND status != 'done'",
                        (to_date, now, from_date),
                    )
                count = cur.rowcount
            conn.commit()
            return count
        finally:
            release_connection(conn)

    @staticmethod
    def move_incomplete_tasks_on_or_before(end_date: str, to_date: str, user_id: str = "") -> tuple[int, list[str]]:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT DISTINCT date FROM tasks "
                        "WHERE date <= %s AND status != 'done' AND user_id = %s "
                        "ORDER BY date ASC",
                        (end_date, user_id),
                    )
                    source_dates = [r["date"] for r in cur.fetchall()]
                    cur.execute(
                        "UPDATE tasks SET date = %s, updated_at = %s "
                        "WHERE date <= %s AND status != 'done' AND user_id = %s",
                        (to_date, now, end_date, user_id),
                    )
                else:
                    cur.execute(
                        "SELECT DISTINCT date FROM tasks "
                        "WHERE date <= %s AND status != 'done' "
                        "ORDER BY date ASC",
                        (end_date,),
                    )
                    source_dates = [r["date"] for r in cur.fetchall()]
                    cur.execute(
                        "UPDATE tasks SET date = %s, updated_at = %s "
                        "WHERE date <= %s AND status != 'done'",
                        (to_date, now, end_date),
                    )
                count = cur.rowcount
            conn.commit()
            return int(count or 0), source_dates
        finally:
            release_connection(conn)

    @staticmethod
    def distinct_task_dates_on_or_before(end_date: str, user_id: str = "") -> list[str]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT DISTINCT date FROM tasks "
                        "WHERE date IS NOT NULL AND date <= %s AND user_id = %s "
                        "ORDER BY date ASC",
                        (end_date, user_id),
                    )
                else:
                    cur.execute(
                        "SELECT DISTINCT date FROM tasks "
                        "WHERE date IS NOT NULL AND date <= %s "
                        "ORDER BY date ASC",
                        (end_date,),
                    )
                rows = cur.fetchall()
            return [r["date"] for r in rows if r["date"]]
        finally:
            release_connection(conn)

    @staticmethod
    def count_by_date_status(date_str: str, user_id: str = "") -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "SELECT status, COUNT(*) as cnt FROM tasks WHERE date = %s AND user_id = %s GROUP BY status",
                        (date_str, user_id),
                    )
                else:
                    cur.execute(
                        "SELECT status, COUNT(*) as cnt FROM tasks WHERE date = %s GROUP BY status",
                        (date_str,),
                    )
                rows = cur.fetchall()
            return {r["status"]: r["cnt"] for r in rows}
        finally:
            release_connection(conn)

    @staticmethod
    def get_ended_days(user_id: str = "") -> list[str]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("SELECT date FROM task_ended_days WHERE user_id = %s ORDER BY date ASC", (user_id,))
                else:
                    cur.execute("SELECT date FROM task_ended_days ORDER BY date ASC")
                rows = cur.fetchall()
            return [r["date"] for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def end_day(date_str: str, user_id: str = "", work_week: list[int] | None = None) -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO task_ended_days (date, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (date_str, user_id),
                )
            conn.commit()
            ended = TaskDB.get_ended_days(user_id=user_id)
            next_date = _next_available_workday(date_str, ended_days=ended, work_week=work_week)
            moved_count, _ = TaskDB.move_incomplete_tasks_on_or_before(date_str, next_date, user_id=user_id)
            return {"date": date_str, "next_date": next_date, "moved_count": moved_count, "ended_days": ended}
        finally:
            release_connection(conn)

    @staticmethod
    def unend_day(date_str: str, user_id: str = "") -> dict:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute("DELETE FROM task_ended_days WHERE date = %s AND user_id = %s", (date_str, user_id))
                else:
                    cur.execute("DELETE FROM task_ended_days WHERE date = %s", (date_str,))
            conn.commit()
            ended = TaskDB.get_ended_days(user_id=user_id)
            return {"date": date_str, "ended_days": ended}
        finally:
            release_connection(conn)
