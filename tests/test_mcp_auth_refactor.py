import asyncio
import importlib.util
from pathlib import Path
import pytest


pytestmark = pytest.mark.no_db


spec = importlib.util.spec_from_file_location("savant_mcp_auth", Path(__file__).parents[1] / "mcp" / "auth.py")
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)


def test_unknown_explicit_session_does_not_inherit_last_api_key(monkeypatch):
    observed = []

    class Mcp:
        name = "context"

        @staticmethod
        def sse_app(mount_path=None):
            async def inner(scope, receive, send):
                observed.append(auth.auth_headers())
            return inner

    auth._session_keys.clear()
    auth._session_app_names.clear()
    auth._session_mcp_servers.clear()
    auth._api_key_var.set("")
    auth._app_name_var.set("")
    auth._mcp_server_var.set("")
    auth._session_keys["_last"] = "key-from-another-session"
    monkeypatch.delenv("SAVANT_API_KEY", raising=False)

    instance = Mcp()
    auth.install_header_capture(instance)
    wrapped = instance.sse_app()
    asyncio.run(wrapped(
        {"type": "http", "headers": [], "query_string": b"session_id=unknown", "path": "/messages/"},
        None,
        None,
    ))

    assert "X-API-Key" not in observed[0]


def test_streamable_http_captures_client_headers_by_mcp_session_id(monkeypatch):
    observed = []

    class Mcp:
        name = "context"

        @staticmethod
        def sse_app(mount_path=None):
            async def inner(scope, receive, send):
                return None
            return inner

        @staticmethod
        def streamable_http_app():
            async def inner(scope, receive, send):
                observed.append(auth.auth_headers())
            return inner

    auth._session_keys.clear()
    auth._session_app_names.clear()
    auth._session_mcp_servers.clear()
    auth._api_key_var.set("")
    auth._app_name_var.set("")
    auth._mcp_server_var.set("")
    monkeypatch.delenv("SAVANT_API_KEY", raising=False)

    instance = Mcp()
    auth.install_header_capture(instance)
    wrapped = instance.streamable_http_app()
    asyncio.run(wrapped(
        {
            "type": "http",
            "headers": [
                (b"mcp-session-id", b"streamable-session"),
                (b"x-api-key", b"client-key"),
                (b"x-app-name", b"codex"),
            ],
            "query_string": b"",
            "path": "/mcp",
        },
        None,
        None,
    ))

    assert observed == [{
        "X-API-Key": "client-key",
        "X-App-Name": "codex",
        "X-MCP-Server": "context",
    }]
    assert auth._session_keys["streamable-session"] == "client-key"
