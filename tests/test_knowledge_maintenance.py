"""Integration coverage for the scheduled institutional graph maintenance engine."""

from db.knowledge_graph import KnowledgeGraphDB
from knowledge.maintenance import run_maintenance_now, start_maintenance_scheduler, stop_maintenance_scheduler


def _node(title: str, *, metadata=None, content="", node_type="concept"):
    return KnowledgeGraphDB.create_node({
        "title": title, "node_type": node_type, "content": content,
        "metadata": metadata or {"workspaces": ["maintenance-test"]},
    })


def test_maintenance_promotes_and_condenses_workspace_duplicates():
    first = _node("Shared API Authentication", content="Initial workspace knowledge")
    second = _node("  shared api authentication ", content="A second workspace observation",
                   metadata={"workspaces": ["other-workspace"]})

    result = run_maintenance_now("test")

    assert result["status"] == "success"
    assert result["nodes_promoted"] == 1
    assert result["duplicates_merged"] == 1
    graph = KnowledgeGraphDB.list_nodes(include_staged=True, limit=20)
    matches = [node for node in graph if node["title"] == first["title"]]
    assert len(matches) == 1
    assert matches[0]["status"] == "committed"
    assert set(matches[0]["metadata"]["workspaces"]) == {"maintenance-test", "other-workspace"}
    assert KnowledgeGraphDB.get_node(second["node_id"]) is None


def test_maintenance_supersedes_explicit_contradiction_with_newer_payload():
    original = _node("Gateway API version", content="Use v1")
    KnowledgeGraphDB.commit_nodes([original["node_id"]])
    replacement = _node("Gateway API version update", content="Use v2", metadata={
        "workspaces": ["maintenance-test"], "supersedes_node_id": original["node_id"],
    })

    result = run_maintenance_now("test")

    assert result["contradictions_resolved"] == 1
    resolved = KnowledgeGraphDB.get_node(original["node_id"])
    assert resolved["content"] == "Use v2"
    assert resolved["metadata"]["superseded_by"] == replacement["node_id"]
    assert KnowledgeGraphDB.get_node(replacement["node_id"]) is None


def test_maintenance_processes_all_staged_batches(monkeypatch):
    monkeypatch.setenv("KG_MAINTENANCE_BATCH_SIZE", "1")
    _node("Batch one")
    _node("Batch two")
    _node("Batch three")

    result = run_maintenance_now("test")

    assert result["nodes_promoted"] == 3
    assert not [node for node in KnowledgeGraphDB.list_nodes(include_staged=True, limit=20)
                if node["status"] == "staged"]


def test_maintenance_records_audit_and_maintenance_api(client):
    _node("Audited maintenance entry")
    run_maintenance_now("test")

    records = KnowledgeGraphDB.list_maintenance_runs()
    assert records[0]["status"] == "success"
    assert records[0]["trigger"] == "test"
    response = client.get("/api/knowledge/maintenance/status")
    assert response.status_code == 200
    assert response.get_json()["runs"][0]["status"] == "success"


def test_scheduler_has_a_single_four_hour_cron_job():
    stop_maintenance_scheduler()
    start_maintenance_scheduler()
    from knowledge import maintenance
    job = maintenance._scheduler.get_job("kg-maintenance")
    assert str(job.trigger) == "cron[hour='*/4', minute='0']"
    stop_maintenance_scheduler()
