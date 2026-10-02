import asyncio
import os
import sys
import pytest
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import auth as mcp_auth
from db.users import UserDB
from utils.auth import check_domain_write_access


@pytest.fixture(autouse=True)
def setup_users(_isolated_db):
    mcp_auth.clear_role_cache()
    mcp_auth._session_keys.clear()
    mcp_auth._session_app_names.clear()
    mcp_auth._session_mcp_servers.clear()
    mcp_auth._api_key_var.set("")
    mcp_auth._app_name_var.set("")
    mcp_auth._mcp_server_var.set("")
    mcp_auth._in_http_context.set(False)

    # Create a test guest user
    UserDB.create({
        "user_id": "test_guest",
        "name": "Test Guest",
        "email": "guest@test.local",
        "role": "guest",
        "api_key": "sk-test-guest-token",
    })

    # Create a regular user
    UserDB.create({
        "user_id": "test_regular",
        "name": "Test Regular",
        "email": "regular@test.local",
        "role": "user",
        "api_key": "sk-test-regular-token",
    })


def test_get_user_role():
    assert mcp_auth.get_user_role("sk-test-guest-token") == "guest"
    assert mcp_auth.get_user_role("sk-test-regular-token") == "user"
    assert mcp_auth.get_user_role("sk-ahmed-savant-001") == "admin"
    assert mcp_auth.get_user_role("nonexistent-key") == "guest"
    assert mcp_auth.get_user_role("") == "guest"


def test_check_domain_write_access_blocks_guest():
    ok, err = check_domain_write_access("test_guest", node_id="any_node")
    assert not ok
    assert "Guest users have read-only search access" in err

    # Admin should still have access
    ok_admin, err_admin = check_domain_write_access("ahmed", node_id="any_node")
    assert ok_admin
    assert err_admin is None


def test_mcp_knowledge_guest_filtering_and_call():
    fm = FastMCP("savant-knowledge")

    @fm.tool()
    def search(query: str) -> dict:
        return {"results": [f"found: {query}"]}

    @fm.tool()
    def store(content: str) -> dict:
        return {"stored": content}

    @fm.tool()
    def recent(limit: int = 10) -> dict:
        return {"nodes": []}

    mcp_auth.install_header_capture(fm)

    async def run_scenario():
        # 1. As guest
        mcp_auth._api_key_var.set("sk-test-guest-token")
        mcp_auth._in_http_context.set(True)

        tools = await fm.list_tools()
        tool_names = [t.name for t in tools]
        assert tool_names == ["search"], f"Guest should only see 'search', got: {tool_names}"

        # Allowed tool call
        res = await fm.call_tool("search", {"query": "deep learning"})
        import json
        assert json.loads(res[0].text) == {"results": ["found: deep learning"]}

        # Blocked tool calls
        with pytest.raises(PermissionError) as exc_store:
            await fm.call_tool("store", {"content": "important insight"})
        assert "guest users only have access to knowledge graph search" in str(exc_store.value)

        with pytest.raises(PermissionError) as exc_recent:
            await fm.call_tool("recent", {"limit": 5})
        assert "guest users only have access to knowledge graph search" in str(exc_recent.value)

        # 2. As regular user
        mcp_auth._api_key_var.set("sk-test-regular-token")
        tools_reg = await fm.list_tools()
        tool_names_reg = [t.name for t in tools_reg]
        assert set(tool_names_reg) == {"search", "store", "recent"}

        res_store = await fm.call_tool("store", {"content": "valid insight"})
        assert json.loads(res_store[0].text) == {"stored": "valid insight"}

    asyncio.run(run_scenario())


def test_mcp_other_servers_guest_blocked():
    # Workspace server
    fm_ws = FastMCP("savant-workspace")

    @fm_ws.tool()
    def list_workspaces() -> list:
        return [{"id": "ws-1"}]

    @fm_ws.tool()
    def create_task(title: str) -> dict:
        return {"id": "t-1", "title": title}

    mcp_auth.install_header_capture(fm_ws)

    # Context server
    fm_ctx = FastMCP("savant-context")

    @fm_ctx.tool()
    def research(q: str) -> dict:
        return {"findings": q}

    mcp_auth.install_header_capture(fm_ctx)

    async def run_scenario():
        mcp_auth._api_key_var.set("sk-test-guest-token")
        mcp_auth._in_http_context.set(True)

        # Workspace: guest sees 0 tools and cannot call any
        ws_tools = await fm_ws.list_tools()
        assert ws_tools == [], f"Guest should see 0 workspace tools, got: {ws_tools}"

        with pytest.raises(PermissionError) as exc_ws:
            await fm_ws.call_tool("list_workspaces", {})
        assert "guest users do not have access to savant-workspace" in str(exc_ws.value)

        # Context: guest sees 0 tools and cannot call any
        ctx_tools = await fm_ctx.list_tools()
        assert ctx_tools == [], f"Guest should see 0 context tools, got: {ctx_tools}"

        with pytest.raises(PermissionError) as exc_ctx:
            await fm_ctx.call_tool("research", {"q": "auth"})
        assert "guest users do not have access to savant-context" in str(exc_ctx.value)

        # Admin user has full access
        mcp_auth._api_key_var.set("sk-ahmed-savant-001")
        assert len(await fm_ws.list_tools()) == 2
        assert len(await fm_ctx.list_tools()) == 1

        import json
        res_ctx = await fm_ctx.call_tool("research", {"q": "auth"})
        assert json.loads(res_ctx[0].text) == {"findings": "auth"}

    asyncio.run(run_scenario())


def test_flask_knowledge_routes_guest_permissions(client):
    GUEST_HEADERS = {
        "X-API-Key": "sk-test-guest-token",
        "X-App-Name": "savant-olympus",
    }
    ADMIN_HEADERS = {
        "X-API-Key": "sk-ahmed-savant-001",
        "X-App-Name": "savant-olympus",
    }

    # 1. Search is allowed for guest
    res_search = client.post(
        "/api/knowledge/search",
        json={"query": "test"},
        headers=GUEST_HEADERS,
    )
    assert res_search.status_code == 200

    # 2. Store is blocked (403) for guest
    res_store = client.post(
        "/api/knowledge/store",
        json={"content": "forbidden guest insight", "workspace_id": "ws-1"},
        headers=GUEST_HEADERS,
    )
    assert res_store.status_code == 403

    # 3. Create node is blocked (403) for guest
    res_create_node = client.post(
        "/api/knowledge/nodes",
        json={"title": "guest node", "node_type": "insight"},
        headers=GUEST_HEADERS,
    )
    assert res_create_node.status_code == 403

    # 4. Create edge is blocked (403) for guest
    res_edge = client.post(
        "/api/knowledge/edges",
        json={"source_id": "n1", "target_id": "n2"},
        headers=GUEST_HEADERS,
    )
    assert res_edge.status_code == 403


def test_guest_domain_scoped_search_and_read_only_assignment(client):
    from db.knowledge_graph import KnowledgeGraphDB

    GUEST_HEADERS = {
        "X-API-Key": "sk-test-guest-token",
        "X-App-Name": "savant-olympus",
    }
    ADMIN_HEADERS = {
        "X-API-Key": "sk-ahmed-savant-001",
        "X-App-Name": "savant-olympus",
    }

    # Create two domains and connected insights
    d1 = KnowledgeGraphDB.create_node({
        "node_type": "domain", "title": "Auth Domain", "content": "Auth domain capability",
        "status": "committed",
    })
    d2 = KnowledgeGraphDB.create_node({
        "node_type": "domain", "title": "Billing Domain", "content": "Billing domain capability",
        "status": "committed",
    })

    n1 = KnowledgeGraphDB.create_node({
        "node_type": "insight", "title": "Auth Insight", "content": "Insight about Auth domain tokens",
        "status": "committed",
    })
    n2 = KnowledgeGraphDB.create_node({
        "node_type": "insight", "title": "Billing Insight", "content": "Insight about Billing invoices",
        "status": "committed",
    })

    KnowledgeGraphDB.create_edge({"source_id": n1["node_id"], "target_id": d1["node_id"], "edge_type": "applies_to"})
    KnowledgeGraphDB.create_edge({"source_id": n2["node_id"], "target_id": d2["node_id"], "edge_type": "applies_to"})

    # 1. Guest has no domains assigned yet -> search returns 0 results
    res_empty = client.post("/api/knowledge/search", json={"query": "Insight"}, headers=GUEST_HEADERS)
    assert res_empty.status_code == 200
    assert res_empty.get_json()["result"] == []

    # 2. Admin assigns Auth Domain to guest with can_write=True
    # Verify that can_write is forced to False for guest users!
    assign_res = client.post(
        "/api/users/test_guest/domains",
        json={"domain_node_id": d1["node_id"], "can_write": True},
        headers=ADMIN_HEADERS,
    )
    assert assign_res.status_code == 200
    body = assign_res.get_json()
    assert body["can_write"] is False, "Guest domain assignment must be strictly read-only (can_write=False)"

    # Verify in DB
    assigned = UserDB.get_assigned_domains("test_guest")
    assert len(assigned) == 1
    assert assigned[0]["domain_node_id"] == d1["node_id"]
    assert assigned[0]["can_write"] == 0 or assigned[0]["can_write"] is False

    # 3. Guest searches again -> only Auth Insight is visible, Billing Insight is filtered out!
    res_search = client.post("/api/knowledge/search", json={"query": "Insight"}, headers=GUEST_HEADERS)
    assert res_search.status_code == 200
    search_nodes = res_search.get_json()["result"]
    assert len(search_nodes) == 1
    assert search_nodes[0]["node_id"] == n1["node_id"]

    # 4. Graph endpoint for guest only includes Auth Domain and Auth Insight
    res_graph = client.get("/api/knowledge/graph", headers=GUEST_HEADERS)
    assert res_graph.status_code == 200
    graph_data = res_graph.get_json()
    node_ids = {n["node_id"] for n in graph_data["nodes"]}
    assert n1["node_id"] in node_ids
    assert d1["node_id"] in node_ids
    assert n2["node_id"] not in node_ids
    assert d2["node_id"] not in node_ids

