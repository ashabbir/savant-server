"""Workspace-session link persistence (server-owned mapping) backed by PostgreSQL."""

from db.base import _now, _row_to_dict, _rows_to_dicts
from postgres_client import get_connection, release_connection

_ALLOWED_PROVIDERS = {"copilot", "claude", "codex", "gemini", "savant"}


class WorkspaceSessionLinkDB:
    @staticmethod
    def _normalize_provider(provider: str) -> str:
        value = str(provider or "").strip().lower()
        if value not in _ALLOWED_PROVIDERS:
            raise ValueError("Invalid provider")
        return value

    @staticmethod
    def _get_link_with_conn(provider: str, session_id: str, conn) -> dict | None:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT workspace_id, provider, session_id, attached_at
                   FROM workspace_session_links
                   WHERE provider = %s AND session_id = %s""",
                (provider, session_id),
            )
            row = cur.fetchone()
        return _row_to_dict(row)

    @staticmethod
    def list_by_workspace(workspace_id: str) -> list[dict]:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT workspace_id, provider, session_id, attached_at
                       FROM workspace_session_links
                       WHERE workspace_id = %s
                       ORDER BY attached_at DESC""",
                    (workspace_id,),
                )
                rows = cur.fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            release_connection(conn)

    @staticmethod
    def list_by_workspaces(workspace_ids: list[str]) -> dict[str, list[dict]]:
        if not workspace_ids:
            return {}
        # Deduplicate and limit to prevent pool issues
        seen = list(dict.fromkeys(workspace_ids))[:500]
        result: dict[str, list[dict]] = {wid: [] for wid in seen}
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT workspace_id, provider, session_id, attached_at
                       FROM workspace_session_links
                       WHERE workspace_id = ANY(%s)
                       ORDER BY attached_at DESC""",
                    (seen,),
                )
                rows = cur.fetchall()
            for row in rows:
                link = _row_to_dict(row)
                wid = link["workspace_id"]
                if wid in result:
                    result[wid].append(link)
            return result
        finally:
            release_connection(conn)

    @staticmethod
    def resolve(provider: str, session_id: str) -> dict | None:
        conn = get_connection()
        try:
            return WorkspaceSessionLinkDB._get_link_with_conn(
                WorkspaceSessionLinkDB._normalize_provider(provider), str(session_id or ""), conn
            )
        finally:
            release_connection(conn)

    @staticmethod
    def upsert(workspace_id: str, provider: str, session_id: str) -> dict:
        conn = get_connection()
        try:
            now = _now()
            provider = WorkspaceSessionLinkDB._normalize_provider(provider)
            sid = str(session_id or "").strip()
            if not sid:
                raise ValueError("session_id required")
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO workspace_session_links (workspace_id, provider, session_id, attached_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT(provider, session_id)
                       DO UPDATE SET workspace_id = EXCLUDED.workspace_id, attached_at = EXCLUDED.attached_at""",
                    (workspace_id, provider, sid, now),
                )
            conn.commit()
            return {
                "workspace_id": workspace_id,
                "provider": provider,
                "session_id": sid,
                "attached_at": now,
            }
        finally:
            release_connection(conn)

    @staticmethod
    def delete_from_workspace(workspace_id: str, provider: str, session_id: str) -> bool:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM workspace_session_links
                       WHERE workspace_id = %s AND provider = %s AND session_id = %s""",
                    (workspace_id, WorkspaceSessionLinkDB._normalize_provider(provider), str(session_id or "")),
                )
                count = cur.rowcount
            conn.commit()
            return count > 0
        finally:
            release_connection(conn)

    @staticmethod
    def delete_by_workspace(workspace_id: str) -> int:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM workspace_session_links WHERE workspace_id = %s",
                    (workspace_id,),
                )
                count = cur.rowcount
            conn.commit()
            return int(count or 0)
        finally:
            release_connection(conn)
