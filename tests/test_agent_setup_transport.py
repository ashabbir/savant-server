"""Agent configuration transport selection regression coverage."""

import json

import pytest
from flask import Flask

import routes.jobs_system as jobs_system


pytestmark = pytest.mark.no_db


@pytest.fixture
def agent_client():
    app = Flask(__name__)
    app.register_blueprint(jobs_system.jobs_system_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _profile(root, provider):
    agent_dir = root / provider
    return {
        "label": provider.title(),
        "description": f"{provider} test profile",
        "presence_path": str(agent_dir),
        "mcp_path": str(agent_dir / "mcp.json"),
        "instructions_path": str(agent_dir / "instructions.md"),
        "skills_dir": str(agent_dir / "skills"),
        "hook_path": str(agent_dir / "record-learning.sh"),
    }


@pytest.fixture
def agent_profiles(tmp_path, monkeypatch):
    profiles = {
        "copilot": _profile(tmp_path, "copilot"),
        "claude": _profile(tmp_path, "claude"),
        "hermes": _profile(tmp_path, "hermes"),
        "codex": _profile(tmp_path, "codex"),
    }
    monkeypatch.setattr(jobs_system, "_AGENT_SETUP_PROFILES", profiles)
    return profiles


def test_agent_setup_defaults_to_streamable_http_and_reports_it(agent_client, agent_profiles):
    response = agent_client.post("/api/agents/setup/trigger", json={"provider": "codex"})

    assert response.status_code == 200
    payload = response.get_json()
    config = json.loads(open(agent_profiles["codex"]["mcp_path"], encoding="utf-8").read())

    assert payload["success"] is True
    assert payload["report"]["agents"]["codex"]["mcpTransport"] == "streamable-http"
    assert config["mcpServers"]["savant-workspace"] == {
        "type": "streamable-http",
        "url": "http://127.0.0.1:8191/mcp?api_key=sk-ahmed-savant-001&app_name=savant-mcp",
    }
    assert config["mcpServers"]["savant-context"]["url"].startswith("http://127.0.0.1:8193/mcp?")
    assert config["mcpServers"]["savant-knowledge"]["url"].startswith("http://127.0.0.1:8194/mcp?")


def test_agent_setup_allows_explicit_sse_fallback(agent_client, agent_profiles):
    response = agent_client.post(
        "/api/agents/setup/trigger",
        json={"provider": "claude", "transport": "sse"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    config = json.loads(open(agent_profiles["claude"]["mcp_path"], encoding="utf-8").read())

    assert payload["report"]["agents"]["claude"]["mcpTransport"] == "sse"
    assert config["mcpServers"]["savant-workspace"] == {
        "type": "sse",
        "url": "http://127.0.0.1:8091/sse?api_key=sk-ahmed-savant-001&app_name=savant-mcp",
    }
    assert config["mcpServers"]["savant-context"]["url"].startswith("http://127.0.0.1:8093/sse?")
    assert config["mcpServers"]["savant-knowledge"]["url"].startswith("http://127.0.0.1:8094/sse?")


@pytest.mark.parametrize("transport", ["stdio", "http", "", 42, {"type": "sse"}])
def test_agent_setup_rejects_unknown_transport(agent_client, agent_profiles, transport):
    response = agent_client.post(
        "/api/agents/setup/trigger",
        json={"provider": "codex", "transport": transport},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "transport must be one of: streamable-http, sse"
