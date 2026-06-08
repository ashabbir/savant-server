"""ExperienceDB — PostgreSQL backend for the knowledge/experience layer."""

import json
from db.base import _now, _row_to_dict as _base_row
from postgres_client import get_connection, release_connection


def _row_to_dict(row):
    return _base_row(row, json_fields={"files": []})


class ExperienceDB:

    @staticmethod
    def _get_by_id_with_conn(experience_id: str, conn) -> dict | None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM experiences WHERE experience_id = %s", (experience_id,)
            )
            row = cur.fetchone()
        return _row_to_dict(row)

    @staticmethod
    def create(exp: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            files_json = json.dumps(exp.get("files", []))
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO experiences
                       (experience_id, content, source, workspace_id, repo, files, created_at, updated_at, user_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        exp["experience_id"],
                        exp["content"],
                        exp.get("source", "note"),
                        exp.get("workspace_id", ""),
                        exp.get("repo", ""),
                        files_json,
                        exp.get("created_at", now),
                        exp.get("updated_at", now),
                        exp.get("user_id", ""),
                    ),
                )
            conn.commit()
            return ExperienceDB._get_by_id_with_conn(exp["experience_id"], conn)
        finally:
            release_connection(conn)

    @staticmethod
    def get_by_id(experience_id: str) -> dict | None:
        conn = get_connection()
        try:
            return ExperienceDB._get_by_id_with_conn(experience_id, conn)
        finally:
            release_connection(conn)

    @staticmethod
    def search(query: str, workspace_id: str = "", limit: int = 20, user_id: str = "") -> list[dict]:
        conn = get_connection()
        try:
            clauses = ["content ILIKE %s"]
            params = [f"%{query}%"]
            if workspace_id:
                clauses.append("workspace_id = %s")
                params.append(workspace_id)
            if user_id:
                clauses.append("user_id = %s")
                params.append(user_id)
            where = "WHERE " + " AND ".join(clauses)
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM experiences {where} ORDER BY created_at DESC LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_recent(workspace_id: str = "", limit: int = 20, user_id: str = "") -> list[dict]:
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
            where = "WHERE " + " AND ".join(clauses) if clauses else ""
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM experiences {where} ORDER BY created_at DESC LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_workspace(workspace_id: str, limit: int = 100, user_id: str = "") -> list[dict]:
        return ExperienceDB.list_recent(workspace_id=workspace_id, limit=limit, user_id=user_id)

    @staticmethod
    def list_all(limit: int = 200, user_id: str = "") -> list[dict]:
        return ExperienceDB.list_recent(limit=limit, user_id=user_id)

    @staticmethod
    def delete(experience_id: str, user_id: str = "") -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "DELETE FROM experiences WHERE experience_id = %s AND user_id = %s", (experience_id, user_id)
                    )
                else:
                    cur.execute(
                        "DELETE FROM experiences WHERE experience_id = %s", (experience_id,)
                    )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def count_by_workspace(workspace_id: str, user_id: str = "") -> int:
        conn = get_connection()
        try:
            clauses = ["workspace_id = %s"]
            params = [workspace_id]
            if user_id:
                clauses.append("user_id = %s")
                params.append(user_id)
            where = "WHERE " + " AND ".join(clauses)
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) FROM experiences {where}",
                    params,
                )
                row = cur.fetchone()
            return row["count"] if row else 0
        finally:
            release_connection(conn)
