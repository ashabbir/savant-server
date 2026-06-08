"""Tests for knowledge graph node merge feature."""

import pytest
import json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_node(client, title, node_type="insight", content=""):
    r = client.post("/api/knowledge/nodes", json={"title": title, "node_type": node_type, "content": content})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _create_edge(client, src, tgt, edge_type="relates_to"):
    r = client.post("/api/knowledge/edges", json={"source_id": src, "target_id": tgt, "edge_type": edge_type})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _get_node(client, node_id):
    return client.get(f"/api/knowledge/nodes/{node_id}")


# ---------------------------------------------------------------------------
# Merge API tests
# ---------------------------------------------------------------------------

class TestMergeValidation:
    """Merge endpoint input validation."""

    def test_merge_requires_at_least_2_ids(self, client):
        r = client.post("/api/knowledge/nodes/merge", json={"node_ids": ["a"]})
        assert r.status_code == 400
        assert "at least 2" in r.get_json()["error"]

    def test_merge_rejects_empty_list(self, client):
        r = client.post("/api/knowledge/nodes/merge", json={"node_ids": []})
        assert r.status_code == 400

    def test_merge_rejects_non_list(self, client):
        r = client.post("/api/knowledge/nodes/merge", json={"node_ids": "not-a-list"})
        assert r.status_code == 400

    def test_merge_rejects_invalid_node_type(self, client):
        n1 = _create_node(client, "A")
        n2 = _create_node(client, "B")
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]],
            "node_type": "invalid_type"
        })
        assert r.status_code == 400
        assert "invalid" in r.get_json()["error"].lower()

    def test_merge_404_if_survivor_missing(self, client):
        n1 = _create_node(client, "A")
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": ["nonexistent_id", n1["node_id"]]
        })
        assert r.status_code == 404


class TestMergeBasic:
    """Core merge behavior."""

    def test_merge_keeps_survivor_title(self, client):
        n1 = _create_node(client, "Survivor Title", "domain")
        n2 = _create_node(client, "Absorbed Title", "service")
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        assert merged["title"] == "Survivor Title"

    def test_merge_deletes_absorbed_nodes(self, client):
        n1 = _create_node(client, "Keep")
        n2 = _create_node(client, "Delete1")
        n3 = _create_node(client, "Delete2")
        client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"], n3["node_id"]]
        })
        assert _get_node(client, n2["node_id"]).status_code == 404
        assert _get_node(client, n3["node_id"]).status_code == 404
        assert _get_node(client, n1["node_id"]).status_code == 200

    def test_merge_applies_node_type(self, client):
        n1 = _create_node(client, "A", "insight")
        n2 = _create_node(client, "B", "service")
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]],
            "node_type": "domain"
        })
        assert r.get_json()["node_type"] == "domain"

    def test_merge_defaults_to_survivor_type(self, client):
        n1 = _create_node(client, "A", "library")
        n2 = _create_node(client, "B", "service")
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        assert r.get_json()["node_type"] == "library"


class TestMergeContent:
    """Content merging behavior."""

    def test_merge_concatenates_content(self, client):
        n1 = _create_node(client, "A", content="Alpha content")
        n2 = _create_node(client, "B", content="Beta content")
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        assert "Alpha content" in merged["content"]
        assert "Beta content" in merged["content"]

    def test_merge_skips_empty_content(self, client):
        n1 = _create_node(client, "A", content="Only real content")
        n2 = _create_node(client, "B", content="")
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        assert merged["content"] == "Only real content"

    def test_merge_skips_duplicate_content(self, client):
        n1 = _create_node(client, "A", content="Same stuff")
        n2 = _create_node(client, "B", content="Same stuff")
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        # Should not have "Same stuff" twice
        assert merged["content"].count("Same stuff") == 1


class TestMergeEdges:
    """Edge re-pointing and deduplication."""

    def test_merge_repoints_external_edges(self, client):
        """Edges from absorbed nodes to external nodes get re-pointed to survivor."""
        n1 = _create_node(client, "Survivor")
        n2 = _create_node(client, "Absorbed")
        ext = _create_node(client, "External")
        _create_edge(client, n2["node_id"], ext["node_id"], "uses")

        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        # Survivor should now have the edge to ext
        edges = merged.get("edges", [])
        assert any(
            (e["source_id"] == n1["node_id"] and e["target_id"] == ext["node_id"])
            or (e["target_id"] == n1["node_id"] and e["source_id"] == ext["node_id"])
            for e in edges
        ), f"Expected edge to external node, got: {edges}"

    def test_merge_removes_self_referential_edges(self, client):
        """Edges between merged nodes become self-referential and should be removed."""
        n1 = _create_node(client, "A")
        n2 = _create_node(client, "B")
        _create_edge(client, n1["node_id"], n2["node_id"])

        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        edges = merged.get("edges", [])
        # No self-referential edges
        for e in edges:
            assert not (e["source_id"] == n1["node_id"] and e["target_id"] == n1["node_id"])

    def test_merge_deduplicates_edges(self, client):
        """If survivor and absorbed both connect to same external node with same edge type, deduplicate."""
        n1 = _create_node(client, "Survivor")
        n2 = _create_node(client, "Absorbed")
        ext = _create_node(client, "External")
        _create_edge(client, n1["node_id"], ext["node_id"], "uses")
        _create_edge(client, n2["node_id"], ext["node_id"], "uses")

        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        edges = merged.get("edges", [])
        # Should be exactly 1 "uses" edge to ext, not 2
        uses_to_ext = [e for e in edges if e["edge_type"] == "uses"
                       and (e["target_id"] == ext["node_id"] or e["source_id"] == ext["node_id"])]
        assert len(uses_to_ext) == 1, f"Expected 1 deduped edge, got {len(uses_to_ext)}: {uses_to_ext}"

    def test_merge_keeps_different_edge_types(self, client):
        """Edges with different types to the same external node should both be kept."""
        n1 = _create_node(client, "Survivor")
        n2 = _create_node(client, "Absorbed")
        ext = _create_node(client, "External")
        _create_edge(client, n1["node_id"], ext["node_id"], "uses")
        _create_edge(client, n2["node_id"], ext["node_id"], "relates_to")

        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        edges = merged.get("edges", [])
        types_to_ext = set(e["edge_type"] for e in edges
                          if e["target_id"] == ext["node_id"] or e["source_id"] == ext["node_id"])
        assert "uses" in types_to_ext
        assert "relates_to" in types_to_ext


class TestMergeMetadata:
    """Metadata merging."""

    def test_merge_combines_files(self, client):
        n1 = client.post("/api/knowledge/nodes", json={
            "title": "A", "node_type": "insight",
            "metadata": {"files": ["a.py", "b.py"]}
        }).get_json()
        n2 = client.post("/api/knowledge/nodes", json={
            "title": "B", "node_type": "insight",
            "metadata": {"files": ["b.py", "c.py"]}
        }).get_json()
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        meta = merged.get("metadata", {})
        assert set(meta.get("files", [])) == {"a.py", "b.py", "c.py"}

    def test_merge_keeps_first_repo(self, client):
        n1 = client.post("/api/knowledge/nodes", json={
            "title": "A", "node_type": "insight",
            "metadata": {"repo": "repo-alpha"}
        }).get_json()
        n2 = client.post("/api/knowledge/nodes", json={
            "title": "B", "node_type": "insight",
            "metadata": {"repo": "repo-beta"}
        }).get_json()
        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"]]
        })
        merged = r.get_json()
        assert merged["metadata"]["repo"] == "repo-alpha"


class TestMergeThreeWay:
    """Merge 3+ nodes at once."""

    def test_three_way_merge(self, client):
        n1 = _create_node(client, "Primary", "domain", "Content 1")
        n2 = _create_node(client, "Secondary", "service", "Content 2")
        n3 = _create_node(client, "Tertiary", "library", "Content 3")
        ext1 = _create_node(client, "Ext1")
        ext2 = _create_node(client, "Ext2")

        _create_edge(client, n1["node_id"], ext1["node_id"], "uses")
        _create_edge(client, n2["node_id"], ext1["node_id"], "relates_to")
        _create_edge(client, n3["node_id"], ext2["node_id"], "uses")

        r = client.post("/api/knowledge/nodes/merge", json={
            "node_ids": [n1["node_id"], n2["node_id"], n3["node_id"]],
            "node_type": "technology"
        })
        assert r.status_code == 200
        merged = r.get_json()
        assert merged["title"] == "Primary"
        assert merged["node_type"] == "technology"
        assert "Content 1" in merged["content"]
        assert "Content 2" in merged["content"]
        assert "Content 3" in merged["content"]
        # n2, n3 deleted
        assert _get_node(client, n2["node_id"]).status_code == 404
        assert _get_node(client, n3["node_id"]).status_code == 404
        # Edges merged
        edges = merged.get("edges", [])
        targets = set()
        for e in edges:
            if e["source_id"] == n1["node_id"]:
                targets.add(e["target_id"])
            elif e["target_id"] == n1["node_id"]:
                targets.add(e["source_id"])
        assert ext1["node_id"] in targets
        assert ext2["node_id"] in targets
