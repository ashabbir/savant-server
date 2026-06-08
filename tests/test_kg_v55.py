"""TDD tests for v5.5 Change Request PRD.

Tests written FIRST — expected to fail until implementation is done.
Covers:
  - Workspace as metadata (not project node): link-workspace, multi-workspace
  - Export workspace KG (GET /api/knowledge/export?workspace_id=X)
  - Import workspace KG (POST /api/knowledge/import)
  - Bulk actions: delete, link-workspace, link edges
  - Info modal data endpoint (GET /api/knowledge/info)
"""

import sys, os, json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _create_node(client, title, node_type="insight", content="", status="committed"):
    resp = client.post("/api/knowledge/nodes", json={
        "node_type": node_type, "title": title, "content": content,
        "status": status,
    })
    assert resp.status_code == 200, f"create_node failed: {resp.data}"
    return resp.get_json()


def _create_edge(client, src, tgt, edge_type="relates_to"):
    resp = client.post("/api/knowledge/edges", json={
        "source_id": src, "target_id": tgt, "edge_type": edge_type,
    })
    assert resp.status_code == 200, f"create_edge failed: {resp.data}"
    return resp.get_json()


def _create_workspace(client, name="Test WS"):
    resp = client.post("/api/workspaces", json={"name": name})
    assert resp.status_code == 200
    return resp.get_json()["workspace_id"]


# ══════════════════════════════════════════════════════════════════════════════
# 1. Workspace as Metadata
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkspaceMetadata:
    """PRD #7: workspace is metadata on a node, not a project node relationship."""

    def test_link_workspace_stores_in_metadata(self, client):
        """link-workspace should store workspace_id in node.metadata.workspaces array."""
        ws_id = _create_workspace(client, "Meta WS")
        node = _create_node(client, "My Insight")
        resp = client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"], "workspace_id": ws_id,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["linked"] is True

        # Verify metadata has workspaces array
        got = client.get(f"/api/knowledge/nodes/{node['node_id']}").get_json()
        meta = got.get("metadata", {})
        assert ws_id in meta.get("workspaces", [])

    def test_link_multiple_workspaces(self, client):
        """PRD #6: should be able to add multiple workspaces to a node."""
        ws1 = _create_workspace(client, "WS One")
        ws2 = _create_workspace(client, "WS Two")
        node = _create_node(client, "Multi WS Node")

        client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"], "workspace_id": ws1,
        })
        client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"], "workspace_id": ws2,
        })

        got = client.get(f"/api/knowledge/nodes/{node['node_id']}").get_json()
        meta = got.get("metadata", {})
        ws_list = meta.get("workspaces", [])
        assert ws1 in ws_list
        assert ws2 in ws_list

    def test_link_workspace_idempotent(self, client):
        """Linking same workspace twice should not duplicate."""
        ws_id = _create_workspace(client, "Idem WS")
        node = _create_node(client, "Idem Node")

        client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"], "workspace_id": ws_id,
        })
        client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"], "workspace_id": ws_id,
        })

        got = client.get(f"/api/knowledge/nodes/{node['node_id']}").get_json()
        ws_list = got.get("metadata", {}).get("workspaces", [])
        assert ws_list.count(ws_id) == 1

    def test_graph_workspace_filter_uses_metadata(self, client):
        """GET /api/knowledge/graph?workspace_id=X should return nodes with that workspace in metadata."""
        ws_id = _create_workspace(client, "Filter WS")
        n1 = _create_node(client, "In WS")
        n2 = _create_node(client, "Not in WS")

        client.post("/api/knowledge/link-workspace", json={
            "node_id": n1["node_id"], "workspace_id": ws_id,
        })

        resp = client.get(f"/api/knowledge/graph?workspace_id={ws_id}")
        data = resp.get_json()
        node_ids = [n["node_id"] for n in data["nodes"]]
        assert n1["node_id"] in node_ids
        assert n2["node_id"] not in node_ids

    def test_graph_workspace_filter_includes_edges_between_ws_nodes(self, client):
        """Workspace graph should include edges between workspace nodes."""
        ws_id = _create_workspace(client, "Edge WS")
        n1 = _create_node(client, "Node A")
        n2 = _create_node(client, "Node B")
        _create_edge(client, n1["node_id"], n2["node_id"])

        client.post("/api/knowledge/link-workspace", json={
            "node_id": n1["node_id"], "workspace_id": ws_id,
        })
        client.post("/api/knowledge/link-workspace", json={
            "node_id": n2["node_id"], "workspace_id": ws_id,
        })

        resp = client.get(f"/api/knowledge/graph?workspace_id={ws_id}")
        data = resp.get_json()
        assert len(data["edges"]) >= 1

    def test_store_with_workspace_adds_metadata(self, client):
        """MCP store with workspace_id should add it to metadata.workspaces."""
        ws_id = _create_workspace(client, "Store WS")
        resp = client.post("/api/knowledge/store", json={
            "content": "Stored insight",
            "workspace_id": ws_id,
        })
        assert resp.status_code == 200
        node = resp.get_json()
        meta = node.get("metadata", {})
        assert ws_id in meta.get("workspaces", [])


# ══════════════════════════════════════════════════════════════════════════════
# 2. Export / Import
# ══════════════════════════════════════════════════════════════════════════════

class TestExportImport:
    """PRD #4/#5: Export KG from workspace, import into another."""

    def test_export_workspace_returns_nodes_and_edges(self, client):
        ws_id = _create_workspace(client, "Export WS")
        n1 = _create_node(client, "Export A", "domain")
        n2 = _create_node(client, "Export B", "service")
        _create_edge(client, n1["node_id"], n2["node_id"], "uses")

        client.post("/api/knowledge/link-workspace", json={
            "node_id": n1["node_id"], "workspace_id": ws_id,
        })
        client.post("/api/knowledge/link-workspace", json={
            "node_id": n2["node_id"], "workspace_id": ws_id,
        })

        resp = client.get(f"/api/knowledge/export?workspace_id={ws_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) == 2
        assert len(data["edges"]) >= 1
        # Nodes should have full data
        assert data["nodes"][0]["title"] in ("Export A", "Export B")

    def test_export_requires_workspace_id(self, client):
        resp = client.get("/api/knowledge/export")
        assert resp.status_code == 400

    def test_import_creates_nodes_and_edges(self, client):
        ws_id = _create_workspace(client, "Import WS")
        payload = {
            "workspace_id": ws_id,
            "nodes": [
                {"node_type": "domain", "title": "Imported Domain", "content": "From export"},
                {"node_type": "service", "title": "Imported Service", "content": "From export"},
            ],
            "edges": [
                {"source_title": "Imported Domain", "target_title": "Imported Service", "edge_type": "uses"},
            ],
        }
        resp = client.post("/api/knowledge/import", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["nodes_created"] >= 2
        assert data["edges_created"] >= 1

        # Verify nodes exist in workspace graph
        graph = client.get(f"/api/knowledge/graph?workspace_id={ws_id}").get_json()
        titles = [n["title"] for n in graph["nodes"]]
        assert "Imported Domain" in titles
        assert "Imported Service" in titles

    def test_import_deduplicates_by_title_and_type(self, client):
        """Importing a node with same title+type should not duplicate."""
        ws_id = _create_workspace(client, "Dedup WS")
        _create_node(client, "Existing Node", "domain")
        # Link it to workspace
        nodes = client.get("/api/knowledge/graph").get_json()["nodes"]
        existing = [n for n in nodes if n["title"] == "Existing Node"][0]
        client.post("/api/knowledge/link-workspace", json={
            "node_id": existing["node_id"], "workspace_id": ws_id,
        })

        payload = {
            "workspace_id": ws_id,
            "nodes": [
                {"node_type": "domain", "title": "Existing Node", "content": "Updated content"},
            ],
            "edges": [],
        }
        resp = client.post("/api/knowledge/import", json=payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("nodes_skipped", 0) >= 1

    def test_import_requires_workspace_id(self, client):
        resp = client.post("/api/knowledge/import", json={"nodes": [], "edges": []})
        assert resp.status_code == 400

    def test_export_import_roundtrip(self, client):
        """Export from one workspace, import into another."""
        ws1 = _create_workspace(client, "Source WS")
        ws2 = _create_workspace(client, "Target WS")

        n1 = _create_node(client, "Roundtrip A", "insight")
        n2 = _create_node(client, "Roundtrip B", "technology")
        _create_edge(client, n1["node_id"], n2["node_id"], "uses")

        client.post("/api/knowledge/link-workspace", json={
            "node_id": n1["node_id"], "workspace_id": ws1,
        })
        client.post("/api/knowledge/link-workspace", json={
            "node_id": n2["node_id"], "workspace_id": ws1,
        })

        exported = client.get(f"/api/knowledge/export?workspace_id={ws1}").get_json()

        resp = client.post("/api/knowledge/import", json={
            "workspace_id": ws2,
            **exported,
        })
        assert resp.status_code == 200

        # ws2 should now have the nodes
        graph = client.get(f"/api/knowledge/graph?workspace_id={ws2}").get_json()
        titles = [n["title"] for n in graph["nodes"]]
        assert "Roundtrip A" in titles
        assert "Roundtrip B" in titles


# ══════════════════════════════════════════════════════════════════════════════
# 3. Bulk Actions
# ══════════════════════════════════════════════════════════════════════════════

class TestBulkActions:
    """PRD #3: bulk delete, bulk add workspace, bulk link edges."""

    def test_bulk_delete(self, client):
        n1 = _create_node(client, "Bulk Del 1")
        n2 = _create_node(client, "Bulk Del 2")
        n3 = _create_node(client, "Keep This")

        resp = client.post("/api/knowledge/nodes/bulk-delete", json={
            "node_ids": [n1["node_id"], n2["node_id"]],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["deleted"] == 2

        # Verify they're gone
        r1 = client.get(f"/api/knowledge/nodes/{n1['node_id']}")
        assert r1.status_code == 404
        # n3 should still exist
        r3 = client.get(f"/api/knowledge/nodes/{n3['node_id']}")
        assert r3.status_code == 200

    def test_bulk_delete_requires_node_ids(self, client):
        resp = client.post("/api/knowledge/nodes/bulk-delete", json={})
        assert resp.status_code == 400

    def test_bulk_link_workspace(self, client):
        ws_id = _create_workspace(client, "Bulk WS")
        n1 = _create_node(client, "Bulk WS 1")
        n2 = _create_node(client, "Bulk WS 2")

        resp = client.post("/api/knowledge/nodes/bulk-link-workspace", json={
            "node_ids": [n1["node_id"], n2["node_id"]],
            "workspace_id": ws_id,
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["linked"] == 2

        # Both should be in workspace
        graph = client.get(f"/api/knowledge/graph?workspace_id={ws_id}").get_json()
        node_ids = [n["node_id"] for n in graph["nodes"]]
        assert n1["node_id"] in node_ids
        assert n2["node_id"] in node_ids

    def test_bulk_link_workspace_requires_fields(self, client):
        resp = client.post("/api/knowledge/nodes/bulk-link-workspace", json={
            "node_ids": ["abc"],
        })
        assert resp.status_code == 400

    def test_bulk_connect(self, client):
        """Bulk create edges from one source to multiple targets."""
        src = _create_node(client, "Hub")
        t1 = _create_node(client, "Spoke 1")
        t2 = _create_node(client, "Spoke 2")

        resp = client.post("/api/knowledge/edges/bulk", json={
            "source_id": src["node_id"],
            "target_ids": [t1["node_id"], t2["node_id"]],
            "edge_type": "relates_to",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["created"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# 4. Info Endpoint
# ══════════════════════════════════════════════════════════════════════════════

class TestInfoEndpoint:
    """KG info modal data — grouped counts and lists."""

    def test_info_returns_grouped_nodes_and_edges(self, client):
        n1 = _create_node(client, "Info A", "domain")
        n2 = _create_node(client, "Info B", "service")
        _create_edge(client, n1["node_id"], n2["node_id"], "uses")

        resp = client.get("/api/knowledge/info")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "nodes_by_type" in data
        assert "edges_by_type" in data
        assert "total_nodes" in data
        assert "total_edges" in data
        assert data["total_nodes"] >= 2
        assert data["total_edges"] >= 1
        # nodes_by_type should have at least domain and service
        types_present = [g["type"] for g in data["nodes_by_type"]]
        assert "domain" in types_present
        assert "service" in types_present

    def test_info_node_groups_have_items(self, client):
        _create_node(client, "Group A", "domain")
        _create_node(client, "Group B", "domain")

        resp = client.get("/api/knowledge/info")
        data = resp.get_json()
        domain_group = [g for g in data["nodes_by_type"] if g["type"] == "domain"]
        assert len(domain_group) == 1
        assert domain_group[0]["count"] >= 2
        assert len(domain_group[0]["items"]) >= 2

    def test_info_with_workspace_filter(self, client):
        ws_id = _create_workspace(client, "Info WS")
        n1 = _create_node(client, "WS Info Node", "insight")
        _create_node(client, "Other Node", "insight")

        client.post("/api/knowledge/link-workspace", json={
            "node_id": n1["node_id"], "workspace_id": ws_id,
        })

        resp = client.get(f"/api/knowledge/info?workspace_id={ws_id}")
        data = resp.get_json()
        assert data["total_nodes"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 5. Unlink Workspace
# ══════════════════════════════════════════════════════════════════════════════

class TestUnlinkWorkspace:
    """Should be able to remove a workspace from a node."""

    def test_unlink_workspace(self, client):
        ws_id = _create_workspace(client, "Unlink WS")
        node = _create_node(client, "Unlink Node")

        client.post("/api/knowledge/link-workspace", json={
            "node_id": node["node_id"], "workspace_id": ws_id,
        })
        # Verify linked
        got = client.get(f"/api/knowledge/nodes/{node['node_id']}").get_json()
        assert ws_id in got.get("metadata", {}).get("workspaces", [])

        # Unlink
        resp = client.post("/api/knowledge/unlink-workspace", json={
            "node_id": node["node_id"], "workspace_id": ws_id,
        })
        assert resp.status_code == 200

        got = client.get(f"/api/knowledge/nodes/{node['node_id']}").get_json()
        assert ws_id not in got.get("metadata", {}).get("workspaces", [])
