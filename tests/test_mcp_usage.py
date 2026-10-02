import os
import sys
from types import SimpleNamespace

from db.mcp_usage import McpUsageDB
from db.users import UserDB

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import auth as mcp_auth  # noqa: E402

LEX = {"X-API-Key": "sk-lex-savant-001", "X-App-Name": "savant-olympus"}


def test_seed_defaults_preserves_changed_api_key():
    rotated = UserDB.rotate_api_key("ahmed")
    UserDB.seed_defaults()
    assert UserDB.get_by_id("ahmed")["api_key"] == rotated["api_key"]
    assert UserDB.get_by_api_key(rotated["api_key"])["user_id"] == "ahmed"
    assert UserDB.get_by_api_key("sk-ahmed-savant-001") is None


def test_touch_last_login_sets_then_throttles():
    assert UserDB.get_by_id("lex").get("last_login_at") is None
    UserDB.touch_last_login("lex")
    first = UserDB.get_by_id("lex")["last_login_at"]
    assert first is not None
    UserDB.touch_last_login("lex")
    assert UserDB.get_by_id("lex")["last_login_at"] == first


def test_usage_aggregates_per_tool():
    for _ in range(3):
        McpUsageDB.record_call("lex", "savant-context", "research")
    McpUsageDB.record_call("lex", "savant-knowledge", "search")
    usage = McpUsageDB.get_user_usage("lex", days=30)
    assert usage["total_calls"] == 4
    top = usage["tools"][0]
    assert (top["mcp_server"], top["tool_name"], top["calls"]) == ("savant-context", "research", 3)
    assert top["active_days"] == 1
    assert top["avg_calls_per_active_day"] == 3.0
    assert len(usage["daily"]) == 2
    assert McpUsageDB.get_user_usage("ahmed")["total_calls"] == 0


def test_record_endpoint_attributes_to_caller(client):
    import app as app_module
    app_module._last_login_touched.clear()

    resp = client.post("/api/usage/mcp", json={"mcp_server": "savant-context", "tool_name": "research"}, headers=LEX)
    assert resp.status_code == 201
    assert McpUsageDB.get_user_usage("lex")["total_calls"] == 1
    assert McpUsageDB.get_user_usage("ahmed")["total_calls"] == 0
    assert UserDB.get_by_id("lex")["last_login_at"] is not None

    bad = client.post("/api/usage/mcp", json={"mcp_server": "savant-context"}, headers=LEX)
    assert bad.status_code == 400


def test_usage_endpoint_is_admin_only(client):
    McpUsageDB.record_call("lex", "savant-context", "research")
    UserDB.touch_last_login("lex")

    admin = client.get("/api/users/lex/usage?days=7")
    assert admin.status_code == 200
    body = admin.get_json()
    assert body["user_id"] == "lex"
    assert body["days"] == 7
    assert body["total_calls"] == 1
    assert body["last_login_at"]

    assert client.get("/api/users/ahmed/usage", headers=LEX).status_code == 403
    assert client.get("/api/users/nobody/usage").status_code == 404
    assert client.get("/api/users/lex/usage?days=abc").status_code == 400


def _age_last_login(user_id, minutes):
    from postgres_client import get_connection, release_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_login_at = now() - make_interval(mins => %s) WHERE user_id = %s",
                (minutes, user_id),
            )
        conn.commit()
    finally:
        release_connection(conn)


def test_logins_per_day_counts_sessions_after_idle_gap():
    UserDB.touch_last_login("lex")
    UserDB.touch_last_login("lex")
    _age_last_login("lex", 10)
    UserDB.touch_last_login("lex")
    assert McpUsageDB.get_user_usage("lex")["logins_per_day"][0]["logins"] == 1

    _age_last_login("lex", 45)
    UserDB.touch_last_login("lex")
    assert McpUsageDB.get_user_usage("lex")["logins_per_day"][0]["logins"] == 2


def test_record_endpoint_captures_projects_and_queries(client):
    from db.workspaces import WorkspaceDB
    ws_id = WorkspaceDB.create({"name": "Data Sync", "user_id": "lex"})["workspace_id"]

    client.post("/api/usage/mcp", headers=LEX, json={
        "mcp_server": "savant-context", "tool_name": "research",
        "repos": ["icn", "icn"], "query": "  kafka consumer retries  ",
    })
    client.post("/api/usage/mcp", headers=LEX, json={
        "mcp_server": "savant-workspace", "tool_name": "list_tasks", "workspace_id": ws_id,
    })

    usage = McpUsageDB.get_user_usage("lex")
    projects = {(p["project_type"], p["project"]): p for p in usage["projects"]}
    assert projects[("repo", "icn")]["calls"] == 1
    assert projects[("workspace", ws_id)]["project_name"] == "Data Sync"
    assert [q["query"] for q in usage["recent_queries"]] == ["kafka consumer retries"]
    assert usage["recent_queries"][0]["repo"] == "icn"
    assert usage["recent_queries"][0]["tool_name"] == "research"


def test_usage_details_extracts_repo_workspace_and_query():
    assert mcp_auth._usage_details({"q": "auth flow", "repo": ["icn", "networks"], "limit": 5}) == {
        "repos": ["icn", "networks"], "query": "auth flow",
    }
    assert mcp_auth._usage_details({"query": "x", "repo_id": "icn", "workspace_id": 42}) == {
        "repos": ["icn"], "workspace_id": "42", "query": "x",
    }
    assert mcp_auth._usage_details(None) == {}
    assert mcp_auth._usage_details({"limit": 3}) == {"repos": []}


def _ctx(headers=None, query=None):
    request = SimpleNamespace(headers=headers or {}, query_params=query or {})
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


def test_usage_key_comes_from_request_never_env(monkeypatch):
    monkeypatch.setenv("SAVANT_API_KEY", "sk-env-default")
    mcp_auth._session_keys["_last"] = "sk-last-seen"
    mcp_auth._api_key_var.set("")

    assert mcp_auth._usage_api_key(_ctx(headers={"x-api-key": "sk-header"})) == "sk-header"
    assert mcp_auth._usage_api_key(_ctx(query={"api_key": "sk-query"})) == "sk-query"

    mcp_auth._session_keys["sess-1"] = "sk-sse-session"
    assert mcp_auth._usage_api_key(_ctx(query={"session_id": "sess-1"})) == "sk-sse-session"

    assert mcp_auth._usage_api_key(_ctx()) == ""
    assert mcp_auth._usage_api_key(None) == ""


def test_forked_worker_gets_its_own_pool():
    import postgres_client

    postgres_client.release_connection(postgres_client.get_connection())
    parent_pool = postgres_client._POOL
    assert parent_pool is not None

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        ok = postgres_client._POOL is None and parent_pool in postgres_client._INHERITED_POOLS
        os.write(write_fd, b"1" if ok else b"0")
        os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 1)
    os.waitpid(pid, 0)
    assert result == b"1"
    assert postgres_client._POOL is parent_pool
