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
    session_id = (
        headers.get(b"mcp-session-id", b"").decode(errors="replace")
        or _first_param(params, "session_id")
    )
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


def _capture_http_app(original_app, server_name: str, safe_sse_start: bool = False):
    """Wrap an MCP ASGI app with Savant client-context capture."""
    def patched_app(*args, **kwargs):
        inner_app = original_app(*args, **kwargs)

        async def wrapper(scope, receive, send):
            if scope["type"] != "http":
                await inner_app(scope, receive, send)
                return

            _capture_scope_context(scope, server_name)
            if not safe_sse_start:
                await inner_app(scope, receive, send)
                return

            # MCP SDK 1.25.0 SSE can return a second response start after
            # connect_sse() already emitted one; do not pass it to uvicorn.
            response_started = False

            async def safe_send(message):
                nonlocal response_started
                if message["type"] == "http.response.start":
                    if response_started:
                        return
                    response_started = True
                await send(message)

            await inner_app(scope, receive, safe_send)

        return wrapper

    return patched_app


def _usage_api_key(context) -> str:
    """Resolve the calling client's key for usage attribution.

    Deliberately never falls back to SAVANT_API_KEY or the process-wide last
    key: those would credit every anonymous call to the pod's default user.
    """
    request = None
    try:
        request = context.request_context.request
    except Exception:
        request = None
    if request is not None:
        key = request.headers.get("x-api-key") or request.query_params.get("api_key")
        if key:
            return key
        session_id = request.headers.get("mcp-session-id") or request.query_params.get("session_id")
        if session_id and _session_keys.get(session_id):
            return _session_keys[session_id]
    return _api_key_var.get("")


def _usage_details(arguments) -> dict:
    """Pull the project (repo/workspace) and query text out of tool arguments."""
    if not isinstance(arguments, dict):
        return {}
    repos = []
    for key in ("repo", "repo_id"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            repos.append(value.strip())
        elif isinstance(value, (list, tuple)):
            repos.extend(str(v).strip() for v in value if str(v).strip())
    details = {"repos": repos}
    workspace_id = arguments.get("workspace_id")
    if workspace_id:
        details["workspace_id"] = str(workspace_id)
    for key in ("q", "query"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            details["query"] = value.strip()
            break
    return details


def _post_usage(api_key: str, server_name: str, tool_name: str, details: dict | None = None) -> None:
    import os
    import requests

    base = os.environ.get("SAVANT_API_BASE", "http://127.0.0.1:8090").rstrip("/")
    try:
        requests.post(
            f"{base}/api/usage/mcp",
            json={"mcp_server": server_name, "tool_name": tool_name, **(details or {})},
            headers={"X-API-Key": api_key, "X-App-Name": "savant-mcp"},
            timeout=3,
        )
    except Exception:
        pass


def install_usage_tracking(mcp_instance, server_name: str) -> None:
    """Count every tools/call per user; tracking must never delay or fail a tool."""
    import threading

    manager = getattr(mcp_instance, "_tool_manager", None)
    if manager is None:
        return
    original_call_tool = manager.call_tool

    async def call_tool(name, arguments, context=None, **kwargs):
        try:
            key = _usage_api_key(context)
            if key:
                threading.Thread(
                    target=_post_usage,
                    args=(key, server_name, name, _usage_details(arguments)),
                    daemon=True,
                ).start()
        except Exception:
            pass
        return await original_call_tool(name, arguments, context=context, **kwargs)

    manager.call_tool = call_tool


def install_header_capture(mcp_instance):
    """Capture client headers for both legacy SSE and Streamable HTTP MCP apps."""
    original_sse_app = mcp_instance.sse_app
    original_streamable_http_app = getattr(mcp_instance, "streamable_http_app", None)
    server_name = getattr(mcp_instance, "name", "savant-mcp")
    install_usage_tracking(mcp_instance, server_name)
    mcp_instance.sse_app = _capture_http_app(
        original_sse_app,
        server_name,
        safe_sse_start=True,
    )
    if callable(original_streamable_http_app):
        mcp_instance.streamable_http_app = _capture_http_app(
            original_streamable_http_app,
            server_name,
        )
