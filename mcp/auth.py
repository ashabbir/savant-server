"""MCP auth helpers — passthrough API key from client to Flask.

Flow: AI client sends key → MCP captures it → MCP forwards to Flask → Flask validates against DB.

SSE flow:
  1. GET /sse?api_key=KEY — middleware stores key by session_id
  2. POST /messages/?session_id=X — middleware retrieves stored key
  3. Tool function calls auth_headers() → returns {"X-API-Key": KEY}
"""

import contextvars
import logging
from urllib.parse import parse_qs

log = logging.getLogger("mcp-auth")

# Session-level key storage: MCP session_id -> api_key
# Populated on GET /sse, read on POST /messages/
_session_keys: dict[str, str] = {}

# Contextvar set per-request so tool functions can read the key
_api_key_var: contextvars.ContextVar[str] = contextvars.ContextVar("savant_api_key", default="")


def get_api_key() -> str:
    """Return the API key for the current request context.

    For SSE transport, the key is captured from the ?api_key= query param.
    For stdio transport (e.g. Jcode), falls back to the SAVANT_API_KEY env var.
    """
    import os
    return _api_key_var.get("") or os.environ.get("SAVANT_API_KEY", "")


def auth_headers() -> dict:
    """Return headers dict for forwarding the client key to Flask."""
    key = get_api_key()
    return {"X-API-Key": key} if key else {}


def install_header_capture(mcp_instance):
    """Wrap the FastMCP SSE app to capture and persist API keys per session."""
    original_sse_app = mcp_instance.sse_app

    def patched_sse_app(mount_path=None):
        inner_app = original_sse_app(mount_path)

        async def wrapper(scope, receive, send):
            if scope["type"] == "http":
                headers = dict(scope.get("headers", []))
                key = headers.get(b"x-api-key", b"").decode()

                qs = scope.get("query_string", b"").decode()
                params = parse_qs(qs)
                if not key:
                    key = params.get("api_key", [""])[0]

                path = scope.get("path", "")
                sid = params.get("session_id", [""])[0] if params.get("session_id") else ""

                if key:
                    _api_key_var.set(key)
                    if sid:
                        _session_keys[sid] = key
                    # Also store as "last" for servers with single concurrent user
                    _session_keys["_last"] = key
                    log.info("Key captured on %s %s (session=%s)", scope.get("method", "?"), path, sid[:12] if sid else "none")

                elif sid and sid in _session_keys:
                    _api_key_var.set(_session_keys[sid])
                    log.info("Key restored for session %s on %s", sid[:12], path)

                elif "_last" in _session_keys:
                    _api_key_var.set(_session_keys["_last"])
                    log.info("Key restored from last session on %s", path)

                else:
                    log.warning("No key on %s %s", scope.get("method", "?"), path)

            await inner_app(scope, receive, send)

        return wrapper

    mcp_instance.sse_app = patched_sse_app
