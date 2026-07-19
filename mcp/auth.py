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
_session_app_names: dict[str, str] = {}
_session_mcp_servers: dict[str, str] = {}

# Contextvar set per-request so tool functions can read key, app name, and mcp server name
_api_key_var: contextvars.ContextVar[str] = contextvars.ContextVar("savant_api_key", default="")
_app_name_var: contextvars.ContextVar[str] = contextvars.ContextVar("savant_app_name", default="")
_mcp_server_var: contextvars.ContextVar[str] = contextvars.ContextVar("savant_mcp_server", default="")


def get_api_key() -> str:
    """Return the API key for the current request context."""
    import os
    return _api_key_var.get("") or os.environ.get("SAVANT_API_KEY", "")


def get_app_name() -> str:
    """Return the app name for the current request context."""
    import os
    return _app_name_var.get("") or os.environ.get("SAVANT_APP_NAME", "")


def get_mcp_server() -> str:
    """Return the MCP server name for the current request context."""
    import os
    return _mcp_server_var.get("") or os.environ.get("SAVANT_MCP_SERVER_NAME", "")


def auth_headers() -> dict:
    """Return headers dict for forwarding client key, app name, and MCP server name to Flask."""
    key = get_api_key()
    app_name = get_app_name()
    mcp_server = get_mcp_server()
    hdrs = {}
    if key:
        hdrs["X-API-Key"] = key
    if app_name:
        hdrs["X-App-Name"] = app_name
    if mcp_server:
        hdrs["X-MCP-Server"] = mcp_server
    return hdrs


def install_header_capture(mcp_instance):
    """Wrap the FastMCP SSE app to capture and persist API keys, app names, & MCP server names per session."""
    original_sse_app = mcp_instance.sse_app
    server_name = getattr(mcp_instance, "name", "savant-mcp")

    def patched_sse_app(mount_path=None):
        inner_app = original_sse_app(mount_path)

        async def wrapper(scope, receive, send):
            if scope["type"] == "http":
                headers = dict(scope.get("headers", []))
                key = headers.get(b"x-api-key", b"").decode()
                app_name = headers.get(b"x-app-name", b"").decode() or headers.get(b"x-savant-app", b"").decode()
                mcp_server = headers.get(b"x-mcp-server", b"").decode() or server_name

                qs = scope.get("query_string", b"").decode()
                params = parse_qs(qs)
                if not key:
                    key = params.get("api_key", [""])[0]
                if not app_name:
                    app_name = params.get("app_name", [""])[0] or params.get("savant_app", [""])[0]
                if not mcp_server:
                    mcp_server = params.get("mcp_server", [""])[0] or server_name

                path = scope.get("path", "")
                sid = params.get("session_id", [""])[0] if params.get("session_id") else ""

                if key:
                    _api_key_var.set(key)
                    if sid:
                        _session_keys[sid] = key
                    _session_keys["_last"] = key
                elif sid and sid in _session_keys:
                    _api_key_var.set(_session_keys[sid])
                elif "_last" in _session_keys:
                    _api_key_var.set(_session_keys["_last"])

                if app_name:
                    _app_name_var.set(app_name)
                    if sid:
                        _session_app_names[sid] = app_name
                    _session_app_names["_last"] = app_name
                elif sid and sid in _session_app_names:
                    _app_name_var.set(_session_app_names[sid])
                elif "_last" in _session_app_names:
                    _app_name_var.set(_session_app_names["_last"])

                if mcp_server:
                    _mcp_server_var.set(mcp_server)
                    if sid:
                        _session_mcp_servers[sid] = mcp_server
                    _session_mcp_servers["_last"] = mcp_server
                elif sid and sid in _session_mcp_servers:
                    _mcp_server_var.set(_session_mcp_servers[sid])
                elif "_last" in _session_mcp_servers:
                    _mcp_server_var.set(_session_mcp_servers["_last"])

            await inner_app(scope, receive, send)

        return wrapper

    mcp_instance.sse_app = patched_sse_app
