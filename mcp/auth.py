"""MCP auth helpers — passthrough API key from client to Flask.

Flow: AI client sends key → MCP captures it → MCP forwards to Flask → Flask validates against DB.

SSE flow:
  1. GET /sse?api_key=KEY — middleware stores key by session_id
  2. POST /messages/?session_id=X — middleware retrieves stored key
  3. Tool function calls auth_headers() → returns {"X-API-Key": KEY}
"""

import contextvars
import os
import threading
import time
from urllib.parse import parse_qs
import requests

# Session-level key storage: MCP session_id -> api_key
# Populated on GET /sse, read on POST /messages/
_session_keys: dict[str, str] = {}
_session_app_names: dict[str, str] = {}
_session_mcp_servers: dict[str, str] = {}

# Contextvar set per-request so tool functions can read key, app name, and mcp server name
_api_key_var: contextvars.ContextVar[str] = contextvars.ContextVar("savant_api_key", default="")
_app_name_var: contextvars.ContextVar[str] = contextvars.ContextVar("savant_app_name", default="")
_mcp_server_var: contextvars.ContextVar[str] = contextvars.ContextVar("savant_mcp_server", default="")
_in_http_context: contextvars.ContextVar[bool] = contextvars.ContextVar("savant_in_http", default=False)

# Role cache: api_key -> (role, expire_timestamp)
_role_cache: dict[str, tuple[str, float]] = {}
_ROLE_CACHE_TTL = 30.0  # seconds


def clear_role_cache() -> None:
    """Clear cached role lookups (useful for tests)."""
    _role_cache.clear()


def is_knowledge_server(server_name: str) -> bool:
    """Return True if the server is the knowledge graph MCP server."""
    norm = (server_name or "").strip().lower()
    return norm in ("savant-knowledge", "knowledge")


def get_user_role(api_key: str) -> str:
    """Resolve the role for an API key. Returns 'guest', 'user', 'operator', 'admin', etc."""
    if not api_key:
        return "guest"

    now = time.time()
    cached = _role_cache.get(api_key)
    if cached and now < cached[1]:
        return cached[0]

    role = None
    # 1. Try direct DB lookup if DB is configured and accessible
    try:
        from db.users import UserDB
        user = UserDB.get_by_api_key(api_key)
        if user:
            role = user.get("role", "user")
    except Exception:
        pass

    # 2. Try HTTP lookup to Flask API (/api/auth/validate)
    if not role:
        try:
            base = os.environ.get("SAVANT_API_BASE", "http://127.0.0.1:8090").rstrip("/")
            resp = requests.get(
                f"{base}/api/auth/validate",
                headers={"X-API-Key": api_key, "X-App-Name": "savant-mcp"},
                timeout=3,
            )
            if resp.status_code == 200:
                data = resp.json()
                role = data.get("role", "user")
            elif resp.status_code in (401, 403, 404):
                role = "guest"
        except Exception:
            pass

    if not role:
        role = "guest"

    _role_cache[api_key] = (role, now + _ROLE_CACHE_TTL)
    return role


def get_caller_api_key(context=None, mcp_instance=None) -> str:
    """Resolve the calling client's API key for authorization."""
    key = ""
    if context is not None:
        key = _usage_api_key(context)
    if not key and mcp_instance is not None:
        try:
            ctx = mcp_instance.get_context()
            key = _usage_api_key(ctx)
        except Exception:
            pass
    if not key:
        key = _api_key_var.get("")
    if not key and not _in_http_context.get(False):
        key = os.environ.get("SAVANT_API_KEY", "")
    return key


def is_guest_caller(context=None, mcp_instance=None) -> bool:
    """Return True if the calling client has the 'guest' role."""
    key = get_caller_api_key(context=context, mcp_instance=mcp_instance)
    role = get_user_role(key)
    return role.lower() == "guest"


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


def _lookup_session_db(session_id: str) -> dict | None:
    if not session_id or session_id == "_last":
        return None
    try:
        from db.mcp_sessions import MCPSessionDB
        return MCPSessionDB.get_session(session_id)
    except Exception:
        return None


def _persist_session_db(session_id: str, api_key: str, app_name: str, mcp_server: str) -> None:
    if not session_id or not api_key or session_id == "_last":
        return
    try:
        from db.mcp_sessions import MCPSessionDB
        MCPSessionDB.save_session(session_id, api_key, app_name, mcp_server)
    except Exception:
        pass


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

    # Multi-replica support: if request carries session_id but lacks api_key header,
    # resolve credentials from the shared database if not cached locally.
    if session_id and not api_key:
        if session_id not in _session_keys:
            stored = _lookup_session_db(session_id)
            if stored:
                api_key = stored.get("api_key", "")
                if not app_name:
                    app_name = stored.get("app_name", "")
                if not mcp_server:
                    mcp_server = stored.get("mcp_server", "")

    if session_id and api_key:
        _persist_session_db(session_id, api_key, app_name, mcp_server)

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

            _in_http_context.set(True)
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


def _usage_api_key(context=None, arguments: dict | None = None) -> str:
    """Resolve the calling client's key for usage attribution across all protocols (HTTP, SSE, stdio)."""
    # 1. HTTP / SSE request context
    request = None
    try:
        request = getattr(getattr(context, "request_context", None), "request", None)
    except Exception:
        request = None
    if request is not None:
        key = request.headers.get("x-api-key") or request.query_params.get("api_key")
        if key:
            return key
        session_id = request.headers.get("mcp-session-id") or request.query_params.get("session_id")
        if session_id:
            if _session_keys.get(session_id):
                return _session_keys[session_id]
            stored = _lookup_session_db(session_id)
            if stored and stored.get("api_key"):
                _session_keys[session_id] = stored["api_key"]
                return stored["api_key"]
        # In HTTP context with an active request, do not attribute anonymous HTTP traffic to server env
        return _api_key_var.get("")

    # 2. ContextVar (if bound)
    key = _api_key_var.get("")
    if key:
        return key

    # 3. Tool call arguments if passed
    if isinstance(arguments, dict):
        arg_key = arguments.get("api_key") or arguments.get("_api_key")
        if isinstance(arg_key, str) and arg_key.strip():
            return arg_key.strip()

    # 4. Protocol-agnostic / stdio / local agent environment fallback
    return os.environ.get("SAVANT_API_KEY", "").strip()


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

    if not api_key:
        return
    details = details or {}
    base = (
        os.environ.get("SAVANT_API_BASE")
        or os.environ.get("FLASK_URL")
        or "http://127.0.0.1:8090"
    ).rstrip("/")

    posted = False
    try:
        resp = requests.post(
            f"{base}/api/usage/mcp",
            json={"mcp_server": server_name, "tool_name": tool_name, **details},
            headers={"X-API-Key": api_key, "X-App-Name": "savant-mcp"},
            timeout=3,
        )
        if resp.status_code in (200, 201):
            posted = True
    except Exception:
        pass

    # Direct database fallback when HTTP request could not be completed
    if not posted:
        try:
            from db.users import UserDB
            from db.mcp_usage import McpUsageDB

            user = UserDB.get_by_api_key(api_key)
            if user and int(user.get("is_active", 1)) == 1:
                user_id = user["user_id"]
                raw_repos = details.get("repos") or []
                if isinstance(raw_repos, str):
                    raw_repos = [raw_repos]
                repos = [str(r).strip()[:200] for r in raw_repos if str(r).strip()]
                workspace_id = str(details.get("workspace_id") or "").strip()[:200]
                projects = [("repo", r) for r in repos]
                if workspace_id:
                    projects.append(("workspace", workspace_id))
                query = str(details.get("query") or "").strip()[:1000]

                McpUsageDB.record_call(
                    user_id,
                    server_name,
                    tool_name,
                    projects=projects,
                    query=query,
                    repo=",".join(repos)[:500],
                )
                UserDB.touch_last_login(user_id)
        except Exception:
            pass


def install_usage_tracking(mcp_instance, server_name: str) -> None:
    """Enforce guest access controls and record tool usage."""
    import threading

    manager = getattr(mcp_instance, "_tool_manager", None)
    if manager is None:
        return
    original_call_tool = manager.call_tool
    original_list_tools = manager.list_tools

    def list_tools():
        tools = original_list_tools()
        if is_guest_caller(mcp_instance=mcp_instance):
            if not is_knowledge_server(server_name):
                return []
            return [t for t in tools if getattr(t, "name", "") == "search"]
        return tools

    async def call_tool(name, arguments, context=None, **kwargs):
        if is_guest_caller(context=context, mcp_instance=mcp_instance):
            if not is_knowledge_server(server_name):
                raise PermissionError(
                    f"Access denied: guest users do not have access to {server_name}."
                )
            if name != "search":
                raise PermissionError(
                    "Access denied: guest users only have access to knowledge graph search."
                )

        try:
            key = _usage_api_key(context, arguments=arguments)
            if key:
                threading.Thread(
                    target=_post_usage,
                    args=(key, server_name, name, _usage_details(arguments)),
                    daemon=True,
                ).start()
        except Exception:
            pass
        return await original_call_tool(name, arguments, context=context, **kwargs)

    manager.list_tools = list_tools
    manager.call_tool = call_tool

    resource_mgr = getattr(mcp_instance, "_resource_manager", None)
    if resource_mgr is not None:
        orig_list_res = resource_mgr.list_resources
        orig_get_res = resource_mgr.get_resource

        def list_resources():
            if is_guest_caller(mcp_instance=mcp_instance):
                return []
            return orig_list_res()

        def get_resource(uri, context=None):
            if is_guest_caller(context=context, mcp_instance=mcp_instance):
                raise PermissionError(
                    f"Access denied: guest users do not have access to resources on {server_name}."
                )
            return orig_get_res(uri, context=context)

        resource_mgr.list_resources = list_resources
        resource_mgr.get_resource = get_resource

    prompt_mgr = getattr(mcp_instance, "_prompt_manager", None)
    if prompt_mgr is not None:
        orig_list_p = prompt_mgr.list_prompts
        orig_get_p = prompt_mgr.get_prompt

        def list_prompts():
            if is_guest_caller(mcp_instance=mcp_instance):
                return []
            return orig_list_p()

        def get_prompt(name):
            if is_guest_caller(mcp_instance=mcp_instance):
                raise PermissionError(
                    f"Access denied: guest users do not have access to prompts on {server_name}."
                )
            return orig_get_p(name)

        prompt_mgr.list_prompts = list_prompts
        prompt_mgr.get_prompt = get_prompt


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
