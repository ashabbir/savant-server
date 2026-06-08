import json
import os
import tempfile

os.environ["SAVANT_DISABLE_BG_CACHE"] = "1"

import pytest
from app import app

_AUTH = {"X-API-Key": "sk-ahmed-savant-001"}


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Every test gets its own fresh SQLite database."""
    db_path = str(tmp_path / "test_savant.db")
    monkeypatch.setenv("SAVANT_DB", db_path)

    from sqlite_client import SQLiteClient
    old = SQLiteClient._instance
    SQLiteClient._instance = None

    from sqlite_client import init_sqlite
    init_sqlite()

    from db.users import UserDB
    UserDB.seed_defaults()

    yield db_path

    from sqlite_client import close_sqlite
    close_sqlite()
    SQLiteClient._instance = old


def _client():
    app.config["TESTING"] = True
    return app.test_client()


def test_db_health_ok():
    c = _client()
    r = c.get("/api/db/health")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)
    assert data.get("status") == "healthy"
    assert data.get("connected") is True


def test_system_info_has_ports():
    c = _client()
    r = c.get("/api/system/info")
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, dict)
    assert "mcp_servers" in data
    assert isinstance(data["mcp_servers"], dict)
    assert "abilities" in data
    assert isinstance(data["abilities"], dict)
    assert "asset_count" in data["abilities"]
    assert "bootstrap_available" in data["abilities"]


def test_events_endpoint_returns_list():
    c = _client()
    r = c.get("/api/events?since=0", headers=_AUTH)
    assert r.status_code == 200
    data = r.get_json()
    assert isinstance(data, list)


def test_preferences_get_and_post_roundtrip():
    c = _client()
    payload = {
        "name": "Demo User",
        "work_week": [1, 2, 3, 4, 5],
        "enabled_providers": ["savant", "codex"],
        "theme": "dark",
        "terminal": {
            "externalTerminal": "auto",
            "shell": "/bin/zsh",
            "fontSize": 13,
            "scrollback": 5000,
            "customCommand": "",
        },
    }
    p = c.post("/api/preferences", data=json.dumps(payload), content_type="application/json", headers=_AUTH)
    assert p.status_code == 200
    saved = p.get_json()
    assert saved["name"] == "Demo User"
    assert saved["theme"] == "dark"
    g = c.get("/api/preferences", headers=_AUTH)
    assert g.status_code == 200
    loaded = g.get_json()
    assert loaded["name"] == "Demo User"
    assert loaded["terminal"]["fontSize"] == 13


def test_mcp_health_endpoint_shape():
    c = _client()
    r = c.get("/api/mcp/health/workspace")
    assert r.status_code in (200, 502, 503)
    data = r.get_json()
    assert isinstance(data, dict)
    assert "status" in data


def test_abilities_bootstrap_endpoint_seeds_when_empty(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    seed_dir = tmp_path / "seed"
    seed_file = seed_dir / "abilities" / "personas" / "engineer.md"
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(
        "---\nid: persona.engineer\ntype: persona\ntags: [engineering]\npriority: 100\n---\nbody\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SAVANT_ABILITIES_SEED_DIR", str(seed_dir))

    c = _client()
    r = c.post("/api/abilities/bootstrap", headers=_AUTH)
    assert r.status_code == 201
    data = r.get_json()
    assert data["seeded"] is True
    assert data["count"] == 1
    assert (data_dir / "abilities" / "personas" / "engineer.md").exists()


def test_abilities_bootstrap_endpoint_is_hidden_after_assets_exist(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    target = data_dir / "abilities" / "personas" / "qa.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\nid: persona.qa\ntype: persona\ntags: [qa]\npriority: 90\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))

    c = _client()
    info = c.get("/api/system/info")
    assert info.status_code == 200
    abilities = info.get_json()["abilities"]
    assert abilities["asset_count"] == 1
    assert abilities["bootstrap_available"] is False


def test_abilities_stats_include_styles(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    styles_file = data_dir / "abilities" / "styles" / "style" / "concise.md"
    styles_file.parent.mkdir(parents=True, exist_ok=True)
    styles_file.write_text(
        "---\nid: policy.style.concise\ntype: style\ntags: [style]\npriority: 60\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))

    c = _client()
    r = c.get("/api/abilities/stats", headers=_AUTH)
    assert r.status_code == 200
    data = r.get_json()
    assert data["styles"] == 1
