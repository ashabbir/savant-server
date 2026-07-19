"""PostgreSQL-backed persistence for the Savant tool registry."""

from __future__ import annotations

from psycopg2.extras import Json

from db.base import _now, _row_to_dict, _rows_to_dicts
from postgres_client import get_connection, release_connection


class ToolPackageDB:
    @staticmethod
    def list_all() -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT name, description, input_schema, author, uploaded_by,
                              service_node_id, kg_node_ids, created_at, updated_at
                       FROM tool_packages ORDER BY name ASC"""
                )
                return _rows_to_dicts(cur.fetchall())
        finally:
            release_connection(conn)

    @staticmethod
    def get(name: str, *, include_archive: bool = False) -> dict | None:
        conn = get_connection()
        try:
            columns = "*" if include_archive else (
                "name, description, input_schema, author, uploaded_by, "
                "service_node_id, kg_node_ids, created_at, updated_at"
            )
            with conn.cursor() as cur:
                cur.execute(f"SELECT {columns} FROM tool_packages WHERE name = %s", (name,))
                return _row_to_dict(cur.fetchone())
        finally:
            release_connection(conn)

    @staticmethod
    def create(tool: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO tool_packages
                       (name, description, input_schema, archive_data, author, uploaded_by,
                        service_node_id, kg_node_ids, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        tool["name"], tool.get("description", ""), Json(tool.get("input_schema", {})),
                        tool["archive_data"], tool.get("author", ""), tool["uploaded_by"],
                        tool.get("service_node_id", ""), Json(tool.get("kg_node_ids", [])), now, now,
                    ),
                )
            conn.commit()
            return ToolPackageDB.get(tool["name"])
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def upsert(tool: dict) -> dict:
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO tool_packages
                       (name, description, input_schema, archive_data, author, uploaded_by,
                        service_node_id, kg_node_ids, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (name) DO UPDATE SET
                         description = EXCLUDED.description,
                         input_schema = EXCLUDED.input_schema,
                         archive_data = EXCLUDED.archive_data,
                         author = EXCLUDED.author,
                         uploaded_by = EXCLUDED.uploaded_by,
                         service_node_id = EXCLUDED.service_node_id,
                         kg_node_ids = EXCLUDED.kg_node_ids,
                         updated_at = EXCLUDED.updated_at""",
                    (
                        tool["name"], tool.get("description", ""), Json(tool.get("input_schema", {})),
                        tool["archive_data"], tool.get("author", ""), tool["uploaded_by"],
                        tool.get("service_node_id", ""), Json(tool.get("kg_node_ids", [])), now, now,
                    ),
                )
            conn.commit()
            return ToolPackageDB.get(tool["name"])
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)

    @staticmethod
    def delete(name: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tool_packages WHERE name = %s", (name,))
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            release_connection(conn)
