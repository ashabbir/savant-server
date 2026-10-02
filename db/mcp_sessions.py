"""MCP Session persistence for multi-replica Kubernetes deployments."""

from __future__ import annotations

import logging
from typing import Any

from db.base import _now, _row_to_dict
from postgres_client import get_connection, release_connection

logger = logging.getLogger(__name__)


class MCPSessionDB:
    @staticmethod
    def save_session(session_id: str, api_key: str, app_name: str = "", mcp_server: str = "") -> dict[str, Any] | None:
        sid = str(session_id or "").strip()
        key = str(api_key or "").strip()
        if not sid or not key:
            return None
        conn = get_connection()
        try:
            now = _now()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO mcp_sessions (session_id, api_key, app_name, mcp_server, updated_at)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT(session_id)
                       DO UPDATE SET api_key = EXCLUDED.api_key,
                                     app_name = EXCLUDED.app_name,
                                     mcp_server = EXCLUDED.mcp_server,
                                     updated_at = EXCLUDED.updated_at""",
                    (sid, key, app_name or "", mcp_server or "", now),
                )
            conn.commit()
            return {"session_id": sid, "api_key": key, "app_name": app_name, "mcp_server": mcp_server}
        except Exception as e:
            logger.debug(f"Failed to persist MCP session to DB: {e}")
            return None
        finally:
            release_connection(conn)

    @staticmethod
    def get_session(session_id: str) -> dict[str, Any] | None:
        sid = str(session_id or "").strip()
        if not sid:
            return None
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT session_id, api_key, app_name, mcp_server, updated_at
                       FROM mcp_sessions
                       WHERE session_id = %s""",
                    (sid,),
                )
                row = cur.fetchone()
            return _row_to_dict(row) if row else None
        except Exception as e:
            logger.debug(f"Failed to retrieve MCP session from DB: {e}")
            return None
        finally:
            release_connection(conn)
