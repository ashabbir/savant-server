"""Tests for User Topic Modeling & Activity Intelligence."""

import os
import sys

from db.mcp_usage import McpUsageDB
from db.topic_modeling import TopicModelingDB, clean_text, synthesize_topic_label
from db.users import UserDB
from postgres_client import get_connection, release_connection

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import auth as mcp_auth  # noqa: E402

AHMED_ADMIN = {"X-API-Key": "sk-ahmed-savant-001", "X-App-Name": "savant-olympus"}
LEX = {"X-API-Key": "sk-lex-savant-001", "X-App-Name": "savant-olympus"}


def test_clean_text_and_label_synthesis():
    cleaned = clean_text("Fix /Users/dev/code/src/auth.ts with tokens and keys")
    assert "tokens" in cleaned
    assert "keys" in cleaned
    assert "/Users" not in cleaned

    label = synthesize_topic_label(["auth", "tokens", "permission"])
    assert "Auth & Security" in label


def test_topic_modeling_empty_user():
    model = TopicModelingDB.get_user_topic_model("lex", days=30)
    assert model["user_id"] == "lex"
    assert model["total_documents_analyzed"] == 0
    assert "archetype" in model
    assert "dominant_persona" in model["archetype"]
    assert "topics" in model
    assert "abilities_profiles" in model
    assert "activity_summary" in model


def test_topic_modeling_with_searches_and_contributions():
    # Record queries
    for _ in range(3):
        McpUsageDB.record_call("lex", "savant-context", "research", query="jwt authentication tokens")
    for _ in range(2):
        McpUsageDB.record_call("lex", "savant-knowledge", "search", query="release gate pipeline review")

    # Record knowledge node contributions directly
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO kg_nodes (node_id, title, content, node_type, created_by, status, created_at, updated_at)
                   VALUES ('test_kgn_1', 'Release gate automated review intake', 'Workflow pipeline delivery review', 'insight', 'lex', 'committed', now(), now()),
                          ('test_kgn_2', 'Auth tokens session handling and roles', 'Security token access authorization', 'insight', 'lex', 'committed', now(), now())
                   ON CONFLICT (node_id) DO NOTHING"""
            )
        conn.commit()
    finally:
        release_connection(conn)

    model = TopicModelingDB.get_user_topic_model("lex", days=30)
    assert model["user_id"] == "lex"
    assert model["total_documents_analyzed"] >= 2
    assert len(model["topics"]) >= 1

    # Check topic structure
    first_topic = model["topics"][0]
    assert "label" in first_topic
    assert "weight_pct" in first_topic
    assert "keywords" in first_topic
    assert "activity_counts" in first_topic
    assert "searches" in first_topic["activity_counts"]
    assert "contributions" in first_topic["activity_counts"]

    # Check archetype & personas
    assert "archetype" in model
    assert model["archetype"]["confidence"] > 0
    assert len(model["abilities_profiles"]["personas"]) == 3
    assert len(model["abilities_profiles"]["top_rules"]) >= 1


def test_topic_model_api_endpoints(client):
    UserDB.seed_defaults()

    # Admin access to topic-model endpoint
    resp = client.get("/api/users/lex/topic-model?days=30&topics=4", headers=AHMED_ADMIN)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["user_id"] == "lex"
    assert "archetype" in data
    assert "topics" in data

    # Non-admin forbidden
    resp_forbidden = client.get("/api/users/lex/topic-model", headers=LEX)
    assert resp_forbidden.status_code == 403

    # usage endpoint includes topic_model
    resp_usage = client.get("/api/users/lex/usage?days=30", headers=AHMED_ADMIN)
    assert resp_usage.status_code == 200
    usage_data = resp_usage.get_json()
    assert "topic_model" in usage_data
    assert usage_data["topic_model"]["user_id"] == "lex"


def test_usage_details_captures_persona_and_ability():
    details = mcp_auth._usage_details({"persona": "architect", "repo": "savant-server"})
    assert details["repos"] == ["savant-server"]
    assert details["query"] == "persona:architect"

    details_ability = mcp_auth._usage_details({"asset_id": "backend_api"})
    assert details_ability["query"] == "ability:backend_api"
