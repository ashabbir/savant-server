"""Characterization tests for AbilityStore and abilities API routes."""

import pytest
from pathlib import Path
from abilities.store import AbilityStore, Block


def test_ability_store_load_empty(tmp_path):
    store = AbilityStore(tmp_path)
    store.load()
    assert store.stats() == {
        "personas": 0,
        "rules": 0,
        "policies": 0,
        "styles": 0,
        "repos": 0,
    }


def test_ability_store_load_populated(tmp_path):
    abilities_dir = tmp_path / "abilities"
    (abilities_dir / "personas").mkdir(parents=True, exist_ok=True)
    (abilities_dir / "rules").mkdir(parents=True, exist_ok=True)

    (abilities_dir / "personas" / "architect.md").write_text(
        "---\nid: persona.architect\ntype: persona\ntags: [arch, design]\npriority: 100\nname: Architect\n---\nArchitect persona body.\n",
        encoding="utf-8",
    )
    (abilities_dir / "rules" / "security.md").write_text(
        "---\nid: rule.security\ntype: rule\ntags: [sec, auth]\npriority: 80\nincludes: [rule.auth]\n---\nSecurity rule body.\n",
        encoding="utf-8",
    )
    (abilities_dir / "rules" / "auth.md").write_text(
        "---\nid: rule.auth\ntype: rule\ntags: [auth]\npriority: 70\n---\nAuth rule body.\n",
        encoding="utf-8",
    )

    store = AbilityStore(tmp_path)
    store.load()

    assert store.stats()["personas"] == 1
    assert store.stats()["rules"] == 2
    assert store.get("persona.architect") is not None
    assert store.find_persona("architect") is not None
    assert store.find_persona("persona.architect") is not None
    assert store.validate_includes() is True


def test_ability_store_include_cycles(tmp_path):
    abilities_dir = tmp_path / "abilities"
    (abilities_dir / "rules").mkdir(parents=True, exist_ok=True)

    (abilities_dir / "rules" / "a.md").write_text(
        "---\nid: rule.a\ntype: rule\ntags: []\npriority: 10\nincludes: [rule.b]\n---\nRule A\n",
        encoding="utf-8",
    )
    (abilities_dir / "rules" / "b.md").write_text(
        "---\nid: rule.b\ntype: rule\ntags: []\npriority: 10\nincludes: [rule.a]\n---\nRule B\n",
        encoding="utf-8",
    )

    store = AbilityStore(tmp_path)
    with pytest.raises(RuntimeError, match="Circular include detected"):
        store.load()


def test_ability_store_unknown_include(tmp_path):
    abilities_dir = tmp_path / "abilities"
    (abilities_dir / "rules").mkdir(parents=True, exist_ok=True)

    (abilities_dir / "rules" / "a.md").write_text(
        "---\nid: rule.a\ntype: rule\ntags: []\npriority: 10\nincludes: [rule.nonexistent]\n---\nRule A\n",
        encoding="utf-8",
    )

    store = AbilityStore(tmp_path)
    store.load()
    with pytest.raises(RuntimeError, match="Unknown include 'rule.nonexistent'"):
        store.validate_includes(raise_on_error=True)
    assert store.validate_includes(raise_on_error=False) is False


def test_ability_store_find_repo_fuzzy(tmp_path):
    abilities_dir = tmp_path / "abilities"
    (abilities_dir / "repos").mkdir(parents=True, exist_ok=True)

    (abilities_dir / "repos" / "savant_server.md").write_text(
        "---\nid: repo.savant-server\ntype: repo\ntags: [server]\npriority: 50\nname: savant-server\naliases: [savant]\n---\nRepo savant server.\n",
        encoding="utf-8",
    )

    store = AbilityStore(tmp_path)
    store.load()

    match, detail = store.find_repo_fuzzy("savant")
    assert match is not None
    assert match.id == "repo.savant-server"
    assert detail["method"] in ("exact", "prefix", "compact", "fuzzy")

    match_none, _ = store.find_repo_fuzzy("completely-unrelated-repo-name-xyz")
    assert match_none is None


def test_ability_store_crud_and_learned(tmp_path):
    store = AbilityStore(tmp_path)
    store.load()

    # Create asset
    asset = store.create_asset(
        asset_type="rule",
        asset_id="rule.testing",
        tags=["test", "pytest"],
        priority=30,
        body="Always write tests.",
    )
    assert asset["id"] == "rule.testing"
    assert store.get("rule.testing") is not None

    # Append learned
    updated = store.append_learned("rule.testing", "Learned fact 1")
    assert "Learned fact 1" in updated["learned"][0]

    # Read asset
    fetched = store.get_asset_dict("rule.testing")
    assert fetched is not None
    assert len(fetched["learned"]) == 1

    # Update asset
    store.update_asset("rule.testing", priority=40)
    assert store.get("rule.testing").priority == 40

    # List grouped
    grouped = store.list_assets_grouped()
    assert "rule" in grouped
    assert any(a["id"] == "rule.testing" for a in grouped["rule"])

    # Delete asset
    deleted = store.delete_asset("rule.testing")
    assert deleted is True
    assert store.get("rule.testing") is None


def test_abilities_routes_api(client, tmp_path, monkeypatch):
    base = tmp_path / "abilities-root"
    monkeypatch.setenv("SAVANT_ABILITIES_DIR", str(base))
    from abilities import routes as _r
    _r._store = None
    _r._resolver = None

    abilities_dir = base / "abilities"
    (abilities_dir / "personas").mkdir(parents=True, exist_ok=True)
    (abilities_dir / "personas" / "dev.md").write_text(
        "---\nid: persona.dev\ntype: persona\ntags: [dev]\npriority: 10\n---\nDeveloper.\n",
        encoding="utf-8",
    )

    # 1. GET /api/abilities/stats
    res = client.get("/api/abilities/stats")
    assert res.status_code == 200
    assert res.get_json()["personas"] == 1

    # 2. GET /api/abilities/validate
    res = client.get("/api/abilities/validate")
    assert res.status_code == 200
    assert res.get_json()["ok"] is True

    # 3. POST /api/abilities/resolve
    res = client.post("/api/abilities/resolve", json={"persona": "dev", "tags": ["dev"]})
    assert res.status_code == 200
    payload = res.get_json()
    assert "prompt" in payload

    # Clean up singletons
    _r._store = None
    _r._resolver = None
