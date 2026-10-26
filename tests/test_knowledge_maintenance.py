"""Integration coverage for the scheduled institutional graph maintenance engine."""

from types import SimpleNamespace

import numpy as np
import pytest

from db.knowledge_graph import KnowledgeGraphDB
from knowledge.maintenance import (
    CONTEMPLATE_JOB_TYPE,
    CONTEMPLATE_TARGET,
    queue_contemplate,
    run_maintenance_now,
    start_maintenance_scheduler,
    stop_maintenance_scheduler,
)


@pytest.fixture(autouse=True)
def deterministic_embeddings(monkeypatch):
    from context.embeddings import EmbeddingModel

    class FakeModel:
        _model = SimpleNamespace(max_seq_length=512,
                                 tokenizer=lambda text, **kwargs: {"input_ids": text.split()})

        def embed(self, texts):
            return np.array([[1.0, 0.0] for _ in texts])

        def embed_one(self, text):
            return [1.0, 0.0]

    monkeypatch.setattr(EmbeddingModel, "get", lambda: FakeModel())
    from knowledge.contemplate_matching import SimilarityMatcher, text_guard
    original_score = SimilarityMatcher.score
    def distinct_facts(matcher, left, right, **kwargs):
        score = original_score(matcher, left, right, **kwargs)
        return score if text_guard(left, right) else 0.0
    monkeypatch.setattr(SimilarityMatcher, "score", distinct_facts)


def _node(title: str, *, metadata=None, content="", node_type="concept"):
    node = KnowledgeGraphDB.create_node({
        "title": title, "node_type": node_type, "content": content,
        "metadata": metadata or {"workspaces": ["maintenance-test"]},
    })
    if node_type != "domain" and (not metadata or "maintenance-test" in metadata.get("workspaces", [])):
        from postgres_client import get_connection, release_connection
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT node_id FROM kg_nodes WHERE title='Maintenance test domain' AND node_type='domain'")
                root = cur.fetchone()
            conn.commit()
        finally:
            release_connection(conn)
        if not root:
            root = KnowledgeGraphDB.create_node({"title": "Maintenance test domain", "node_type": "domain",
                                                 "metadata": {"contemplate_similarity_threshold": "default"}})
            KnowledgeGraphDB.commit_nodes([root["node_id"]])
        KnowledgeGraphDB.create_edge({"source_id": node["node_id"], "target_id": root["node_id"], "edge_type": "part_of"})
    return node


def test_maintenance_promotes_and_condenses_workspace_duplicates():
    first = _node("Shared API Authentication", content="Initial workspace knowledge")
    second = _node("  shared api authentication ", content="A second workspace observation",
                   metadata={"workspaces": ["maintenance-test", "other-workspace"]})

    result = run_maintenance_now("test")

    assert result["status"] == "success"
    assert result["nodes_promoted"] == 0
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
    queued = client.post("/api/knowledge/maintenance/run")
    assert queued.status_code == 202
    assert queued.get_json()["job"]["job_type"] == CONTEMPLATE_JOB_TYPE


def test_scheduler_has_a_single_four_hour_contemplate_cron_job():
    stop_maintenance_scheduler()
    start_maintenance_scheduler()
    from knowledge import maintenance
    job = maintenance._scheduler.get_job("contemplate")
    assert str(job.trigger) == "cron[hour='*/4', minute='0']"
    stop_maintenance_scheduler()


def test_contemplate_queue_is_durable_and_deduplicates_active_jobs():
    first = queue_contemplate("test")
    second = queue_contemplate("test")

    assert first["accepted"] is True
    assert first["reused"] is False
    assert first["job"]["job_type"] == CONTEMPLATE_JOB_TYPE
    assert first["job"]["target"] == CONTEMPLATE_TARGET
    assert second["accepted"] is True
    assert second["reused"] is True
    assert second["job"]["id"] == first["job"]["id"]


def test_worker_executes_contemplate_and_commits_staged_knowledge():
    from context.job_worker import _process_next_job
    from db.jobs import JobDB

    pending = _node("Contemplate me", content="Durable knowledge")
    queued = queue_contemplate("test")
    _process_next_job()

    job = JobDB.get_job(queued["job"]["id"])
    assert job["status"] == "done"
    assert job["result"]["status"] == "success"
    assert KnowledgeGraphDB.get_node(pending["node_id"])["status"] == "committed"


def _snapshot():
    from knowledge.contemplate_report import snapshot
    from postgres_client import get_connection, release_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            return snapshot(cur)
    finally:
        conn.rollback()
        release_connection(conn)


def _restore_export(report):
    """Independent consumer: uses only exported rows, no engine internals."""
    import hashlib
    import json
    from psycopg2 import sql
    from knowledge.maintenance import MAINTENANCE_LOCK_ID
    from knowledge.contemplate_report import snapshot
    from postgres_client import get_connection, release_connection

    report = dict(report)
    digest = report.pop("integrity_sha256")
    assert hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest() == digest
    assert report["schema_version"] == 1
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database() AS database, inet_server_addr()::text AS server_address, inet_server_port() AS server_port")
            assert dict(cur.fetchone()) == report["target_database"]
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (MAINTENANCE_LOCK_ID,))
            cur.execute("LOCK TABLE kg_nodes, kg_edges IN SHARE ROW EXCLUSIVE MODE")
            current = snapshot(cur)

            def matches(state):
                for table, rows in report["changes"].items():
                    if any(current[table].get(row["id"]) != row[state] for row in rows):
                        return False
                for node_id, inventory in report["incident_edges"].items():
                    edges = [edge for edge in current["kg_edges"].values()
                             if node_id in (edge["source_id"], edge["target_id"])]
                    if edges != inventory[state]:
                        return False
                return True

            if matches("before"):
                return "already_reverted"
            if not matches("after"):
                raise ValueError("later_edit_conflict")
            for row in report["changes"]["kg_edges"]:
                cur.execute("DELETE FROM kg_edges WHERE edge_id = %s", (row["id"],))
            for table, key in (("kg_nodes", "node_id"), ("kg_edges", "edge_id")):
                for change in report["changes"][table]:
                    row = change["before"]
                    if row is None:
                        cur.execute(sql.SQL("DELETE FROM {} WHERE {} = %s").format(sql.Identifier(table), sql.Identifier(key)), (change["id"],))
                        continue
                    columns = list(row)
                    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
                        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns), sql.Identifier(key),
                        sql.SQL(",").join(sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(col), sql.Identifier(col))
                                          for col in columns if col != key),
                    )
                    cur.execute(query, [row[col] for col in columns])
            current = snapshot(cur)
            assert matches("before")
        conn.commit()
        return "restored"
    finally:
        conn.rollback()
        release_connection(conn)


@pytest.mark.parametrize("pair_count", [50, 51])
def test_run_budget_counts_both_nodes_across_pairs_and_batches(monkeypatch, pair_count):
    monkeypatch.setenv("KG_MAINTENANCE_BATCH_SIZE", "1")
    for index in range(pair_count):
        _node(f"Duplicate pair {index}")
        _node(f"Duplicate pair {index}")
    result = run_maintenance_now("test")
    assert result["duplicates_merged"] == 50
    assert result["affected_node_count"] == 100
    assert len(result["affected_node_ids"]) == 100
    assert result["limit_reached"] is True
    assert result["deferred_operations"] == pair_count - 50
    assert len([node for node in KnowledgeGraphDB.list_nodes(include_staged=True, limit=100)
                if node["status"] == "staged"]) == 0


def test_one_survivor_can_absorb_only_ninety_nine_nodes():
    for _ in range(101):
        node = _node("Same knowledge", content="Identical evidence")
        KnowledgeGraphDB.commit_nodes([node["node_id"]])
    result = run_maintenance_now("test")
    assert result["duplicates_merged"] == 99
    assert result["affected_node_count"] == 100
    assert result["limit_reached"] is True


def test_supersession_and_duplicate_merges_share_the_same_budget():
    for index in range(51):
        original = _node(f"Budget original {index}", content=f"Old evidence {index}")
        KnowledgeGraphDB.commit_nodes([original["node_id"]])
        if index % 2 == 0:
            _node(f"Budget replacement {index}", content=f"New evidence {index}", metadata={
                "workspaces": ["maintenance-test"], "supersedes_node_id": original["node_id"],
            })
        else:
            _node(original["title"], content=f"Extra evidence {index}")
    result = run_maintenance_now("test")
    assert result["duplicates_merged"] == 50
    assert result["contradictions_resolved"] == 25
    assert result["affected_node_count"] == 100
    assert result["limit_reached"] is True


@pytest.mark.parametrize("score,expected", [(0.699999, 0), (0.70, 0), (0.700001, 1)])
def test_similarity_threshold_is_strict_for_semantic_matches(monkeypatch, score, expected):
    from knowledge.contemplate_matching import SimilarityMatcher
    monkeypatch.setattr(SimilarityMatcher, "score", lambda *args, **kwargs: score)
    node = _node("Original wording", content="One fact")
    KnowledgeGraphDB.commit_nodes([node["node_id"]])
    _node("Related wording", content="Another fact")
    assert run_maintenance_now("test")["duplicates_merged"] == expected


def test_identical_content_can_match_different_titles():
    _node("First wording", content="The exact same evidence")
    _node("Second wording", content="The exact same evidence")
    assert run_maintenance_now("test")["duplicates_merged"] == 1


def test_similar_but_distinct_facts_fail_strict_text_guard():
    _node("First wording", content="Use v1")
    _node("Second wording", content="Use v2")
    result = run_maintenance_now("test")
    assert result["duplicates_merged"] == 0
    assert result["nodes_promoted"] == 2


def test_incompatible_scopes_and_domains_never_merge():
    _node("Unscoped duplicate", metadata={"workspaces": ["a"]})
    _node("Unscoped duplicate", metadata={"workspaces": ["b"]})
    _node("Root domain", node_type="domain")
    _node("Root domain", node_type="domain")
    assert run_maintenance_now("test")["duplicates_merged"] == 0


def test_model_failure_and_long_inputs_defer_merges(monkeypatch):
    from context.embeddings import EmbeddingModel
    _node("Long first", content="first " * 600)
    _node("Long second", content="second " * 600)
    result = run_maintenance_now("test")
    assert result["duplicates_merged"] == 0
    assert result["status"] == "success"
    report = KnowledgeGraphDB.get_maintenance_report(result["run_id"])
    assert any(action.get("reason") == "input_too_long" for action in report["actions"])

    def unavailable():
        raise RuntimeError("Model offline")
    monkeypatch.setattr(EmbeddingModel, "get", unavailable)
    result = run_maintenance_now("test")
    report = KnowledgeGraphDB.get_maintenance_report(result["run_id"])
    assert any("embedding_unavailable" in action.get("reason", "") for action in report["actions"])


def test_report_restores_complete_graph_and_rejects_later_changes():
    from db.base import _now
    from postgres_client import get_connection, release_connection
    first = _node("Shared", content="First evidence")
    KnowledgeGraphDB.commit_nodes([first["node_id"]])
    second = _node("Shared", content="Second evidence")
    third = _node("Shared", content="Third evidence")
    external = _node("External", metadata={"workspaces": ["maintenance-test"], "expires_at": "2000-01-01T00:00:00Z"})
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for edge_id, source, target, weight, label in (
                ("edge-first", first["node_id"], external["node_id"], 0.2, "original label"),
                ("edge-second", second["node_id"], external["node_id"], 0.9, "stronger label"),
                ("edge-third", external["node_id"], third["node_id"], 0.5, "reverse edge"),
                ("edge-between", first["node_id"], second["node_id"], 1, "between nodes"),
                ("edge-self", external["node_id"], external["node_id"], 1, "self edge"),
            ):
                cur.execute("INSERT INTO kg_edges VALUES (%s,%s,%s,'relates_to',%s,%s,%s)",
                            (edge_id, source, target, weight, label, _now()))
        conn.commit()
    finally:
        release_connection(conn)
    before = _snapshot()
    result = run_maintenance_now("test")
    assert result["duplicates_merged"] == 2
    survivor = KnowledgeGraphDB.get_node(first["node_id"])
    assert all(text in survivor["content"] for text in ("First evidence", "Second evidence", "Third evidence"))
    assert result["nodes_expired"] == 1
    report = KnowledgeGraphDB.get_maintenance_report(result["run_id"])
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE kg_nodes SET content = content || ' later edit' WHERE node_id = %s", (first["node_id"],))
        conn.commit()
    finally:
        release_connection(conn)
    conflicted = _snapshot()
    with pytest.raises(ValueError, match="later_edit_conflict"):
        _restore_export(report)
    assert _snapshot() == conflicted
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE kg_nodes SET content = %s WHERE node_id = %s", (survivor["content"], first["node_id"]))
        conn.commit()
    finally:
        release_connection(conn)
    assert _restore_export(report) == "restored"
    assert _snapshot() == before
    assert _restore_export(report) == "already_reverted"


def test_failed_run_reports_zero_committed_changes_and_retains_run_id():
    node = _node("Malformed expiry", metadata={"workspaces": ["maintenance-test"], "expires_at": "not-a-date"})
    before = _snapshot()
    result = run_maintenance_now("test")
    assert result["status"] == "failed"
    assert result["nodes_promoted"] == 0
    report = KnowledgeGraphDB.get_maintenance_report(result["run_id"])
    assert report["attempted_summary"]["nodes_promoted"] == 1
    assert report["changes"] == {"kg_nodes": [], "kg_edges": []}
    assert _snapshot() == before
    assert KnowledgeGraphDB.get_node(node["node_id"])["status"] == "staged"


def test_worker_marks_failed_contemplation_as_failed_job():
    from context.job_worker import _process_next_job
    from db.jobs import JobDB
    _node("Worker invalid expiry", metadata={"workspaces": ["maintenance-test"], "expires_at": "not-a-date"})
    queued = queue_contemplate("test")
    _process_next_job()
    assert JobDB.get_job(queued["job"]["id"])["status"] == "failed"
    run = KnowledgeGraphDB.list_maintenance_runs()[0]
    assert run["status"] == "failed"
    assert run["job_id"] == queued["job"]["id"]


def test_matching_domain_sets_are_required_even_with_shared_workspace():
    from db.base import _now
    from postgres_client import get_connection, release_connection
    roots = [_node(f"Root {index}", node_type="domain") for index in range(2)]
    duplicates = [_node("Same scoped title") for _ in range(2)]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for index in range(2):
                cur.execute("INSERT INTO kg_edges VALUES (%s,%s,%s,'part_of',1,'',%s)",
                            (f"scope-edge-{index}", duplicates[index]["node_id"], roots[index]["node_id"], _now()))
        conn.commit()
    finally:
        release_connection(conn)
    assert run_maintenance_now("test")["duplicates_merged"] == 0


def test_skipped_and_no_change_runs_have_reports():
    from knowledge.maintenance import MAINTENANCE_LOCK_ID
    from postgres_client import get_connection, release_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (MAINTENANCE_LOCK_ID,))
        conn.commit()
        result = run_maintenance_now("test")
        assert result["status"] == "skipped"
        assert KnowledgeGraphDB.get_maintenance_report(result["run_id"])["status"] == "skipped"
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (MAINTENANCE_LOCK_ID,))
        conn.commit()
        release_connection(conn)
    result = run_maintenance_now("test")
    assert KnowledgeGraphDB.get_maintenance_report(result["run_id"])["changes"] == {"kg_nodes": [], "kg_edges": []}


def test_interrupted_job_is_reconciled_to_a_failed_report():
    from db.jobs import JobDB
    from knowledge.maintenance import _create_run
    from postgres_client import get_connection, release_connection
    job = JobDB.create_job("contemplate", "knowledge-graph")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            old_id = _create_run(cur, "scheduled")
            cur.execute("UPDATE kg_maintenance_runs SET job_id = %s WHERE id = %s", (job["id"], old_id))
            cur.execute("UPDATE jobs SET status = 'cancelled' WHERE id = %s", (job["id"],))
        conn.commit()
    finally:
        release_connection(conn)
    run_maintenance_now("test")
    report = KnowledgeGraphDB.get_maintenance_report(old_id)
    assert report["status"] == "failed"
    assert report["summary"]["reason"] == "interrupted"
    assert report["job_id"] == job["id"]
    assert report["changes"] == {"kg_nodes": [], "kg_edges": []}


def test_new_incident_edges_prevent_restore():
    from db.base import _now
    from postgres_client import get_connection, release_connection
    first = _node("Incident duplicate")
    KnowledgeGraphDB.commit_nodes([first["node_id"]])
    _node("Incident duplicate")
    outside = _node("Outside")
    KnowledgeGraphDB.commit_nodes([outside["node_id"]])
    result = run_maintenance_now("test")
    report = KnowledgeGraphDB.get_maintenance_report(result["run_id"])
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO kg_edges VALUES ('later-edge',%s,%s,'uses',1,'later relationship',%s)",
                        (first["node_id"], outside["node_id"], _now()))
        conn.commit()
    finally:
        release_connection(conn)
    before_restore = _snapshot()
    with pytest.raises(ValueError, match="later_edit_conflict"):
        _restore_export(report)
    assert _snapshot() == before_restore


def test_candidate_changes_during_preparation_are_revalidated(monkeypatch):
    from knowledge.contemplate_matching import SimilarityMatcher
    original_prepare = SimilarityMatcher.prepare
    _node("Revalidated", content="Old evidence")
    second = _node("Revalidated", content="Old evidence")

    def prepare_and_edit(matcher, nodes):
        original_prepare(matcher, nodes)
        KnowledgeGraphDB.update_node(second["node_id"], {"title": "Concurrent new title", "content": "Concurrent new evidence"})
    monkeypatch.setattr(SimilarityMatcher, "prepare", prepare_and_edit)
    result = run_maintenance_now("test")
    assert result["duplicates_merged"] == 0
    report = KnowledgeGraphDB.get_maintenance_report(result["run_id"])
    assert any(action.get("reason") == "candidate_changed_since_preparation" for action in report["actions"])


@pytest.mark.parametrize("threshold", ["-0.1", "1.01", "nan", "invalid"])
def test_invalid_threshold_fails_without_graph_changes(monkeypatch, threshold):
    monkeypatch.setenv("KG_CONTEMPLATE_SIMILARITY_THRESHOLD", threshold)
    _node("Unchanged")
    before = _snapshot()
    result = run_maintenance_now("test")
    assert result["status"] == "failed"
    assert _snapshot() == before
    assert KnowledgeGraphDB.get_maintenance_report(result["run_id"])["summary"]["nodes_promoted"] == 0


def test_report_api_pagination_and_legacy_availability(client):
    from postgres_client import get_connection, release_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO kg_maintenance_runs (status) VALUES ('success') RETURNING id")
            legacy_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        release_connection(conn)
    result = run_maintenance_now("test")
    page = client.get("/api/knowledge/maintenance/runs?limit=1&page=1").get_json()
    assert page["total"] == 2
    assert page["runs"][0]["report_available"] is True
    page = client.get("/api/knowledge/maintenance/runs?limit=1&page=2").get_json()
    assert page["runs"][0]["report_available"] is False
    assert client.get(f"/api/knowledge/maintenance/runs/{legacy_id}/report").status_code == 404
    response = client.get(f"/api/knowledge/maintenance/runs/{result['run_id']}/report")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    from db.users import UserDB
    guest = UserDB.create({"user_id": "report-guest", "name": "report-guest", "role": "guest"})
    assert client.get(f"/api/knowledge/maintenance/runs/{result['run_id']}/report",
                      headers={"X-API-Key": guest["api_key"]}).status_code == 403


@pytest.mark.no_db
@pytest.mark.parametrize("left,right,expected", [([2, 0], [10, 0], 1), ([1, 0], [0, 1], 0), ([1, 0], [-1, 0], -1)])
def test_cosine_normalizes_vectors(left, right, expected):
    from knowledge.contemplate_matching import cosine
    assert cosine(left, right) == pytest.approx(expected)


@pytest.mark.no_db
@pytest.mark.parametrize("vector", [[0, 0], [float("nan"), 1], [float("inf"), 1], [], [1]])
def test_cosine_rejects_invalid_vectors(vector):
    from knowledge.contemplate_matching import cosine
    with pytest.raises(ValueError, match="invalid_vector"):
        cosine(vector, [1, 0])
