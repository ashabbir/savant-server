"""MCP auth helpers — passthrough API key from client to Flask.

Flow: AI client sends key → MCP captures it → MCP forwards to Flask → Flask validates against DB.

SSE flow:
  1. GET /sse?api_key=KEY — middleware stores key by session_id
  2. POST /messages/?session_id=X — middleware retrieves stored key
  3. Tool function calls auth_headers() → returns {"X-API-Key": KEY}
"""

import contextvars
from urllib.parse import parse_qs

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


def _first_param(params: dict, *names: str) -> str:
    for name in names:
        values = params.get(name) or []
        if values and values[0]:
            return values[0]
    return ""


def _bind_request_value(value: str, session_id: str, context_var, session_values: dict[str, str]) -> None:
    if value:
        context_var.set(value)
        if session_id:
            session_values[session_id] = value
        session_values["_last"] = value
        return
    if session_id:
        context_var.set(session_values.get(session_id, ""))
        return
    context_var.set(session_values.get("_last", ""))


def _capture_scope_context(scope: dict, server_name: str) -> None:
    headers = dict(scope.get("headers", []))
    params = parse_qs(scope.get("query_string", b"").decode(errors="replace"))
    session_id = _first_param(params, "session_id")
    api_key = headers.get(b"x-api-key", b"").decode(errors="replace") or _first_param(params, "api_key")
    app_name = (
        headers.get(b"x-app-name", b"").decode(errors="replace")
        or headers.get(b"x-savant-app", b"").decode(errors="replace")
        or _first_param(params, "app_name", "savant_app")
    )
    mcp_server = (
        headers.get(b"x-mcp-server", b"").decode(errors="replace")
        or _first_param(params, "mcp_server")
        or server_name
    )
    _bind_request_value(api_key, session_id, _api_key_var, _session_keys)
    _bind_request_value(app_name, session_id, _app_name_var, _session_app_names)
    _bind_request_value(mcp_server, session_id, _mcp_server_var, _session_mcp_servers)


def install_header_capture(mcp_instance):
    """Wrap the FastMCP SSE app to capture and persist API keys, app names, & MCP server names per session."""
    original_sse_app = mcp_instance.sse_app
    server_name = getattr(mcp_instance, "name", "savant-mcp")

    def patched_sse_app(mount_path=None):
        inner_app = original_sse_app(mount_path)

        async def wrapper(scope, receive, send):
            if scope["type"] == "http":
                _capture_scope_context(scope, server_name)
                # MCP SDK 1.25.0 bug: SSE handle_sse() returns Response() after
                # connect_sse() already sent http.response.start, causing a double-
                # start that crashes uvicorn. Drop the second http.response.start.
                response_started = False

                async def safe_send(message):
                    nonlocal response_started
                    if message["type"] == "http.response.start":
                        if response_started:
                            return
                        response_started = True
                    await send(message)

                await inner_app(scope, receive, safe_send)
            else:
                await inner_app(scope, receive, send)

        return wrapper

    mcp_instance.sse_app = patched_sse_app
