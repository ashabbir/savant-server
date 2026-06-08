from pathlib import Path

from abilities import bootstrap as bootstrap_module
from abilities.bootstrap import (
    _resolve_seed_base,
    abilities_bootstrap_status,
    seed_abilities_if_missing,
    abilities_asset_count,
)
from server_paths import (
    _default_data_dir,
    get_server_abilities_base_dir,
    get_server_data_dir,
    get_server_db_path,
)
from sqlite_client import SQLiteClient


def test_abilities_seeded_when_missing(monkeypatch, tmp_path):
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

    result = seed_abilities_if_missing()
    assert result["seeded"] is True
    assert (data_dir / "abilities" / "personas" / "engineer.md").exists()

    # second run should no-op
    result2 = seed_abilities_if_missing()
    assert result2["seeded"] is False
    assert result2["reason"] == "already-populated"


def test_sqlite_connect_creates_db_when_missing(tmp_path):
    db_path = tmp_path / "mounted-data" / "savant.db"
    c = SQLiteClient()
    c.connect(str(db_path))
    try:
        assert db_path.exists()
    finally:
        c.disconnect()


def test_seed_returns_missing_when_seed_path_absent(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    missing_seed = tmp_path / "missing-seed"
    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SAVANT_ABILITIES_SEED_DIR", str(missing_seed))
    result = seed_abilities_if_missing()
    assert result["seeded"] is False
    assert result["reason"] == "seed-missing"


def test_resolve_seed_base_uses_server_data_when_present(monkeypatch, tmp_path):
    monkeypatch.delenv("SAVANT_ABILITIES_SEED_DIR", raising=False)
    original_exists = bootstrap_module.Path.exists

    # Make data/abilities appear to exist
    def fake_exists(path_obj):
        as_posix = str(path_obj).replace("\\", "/")
        if as_posix.endswith("/data/abilities"):
            return True
        return original_exists(path_obj)

    monkeypatch.setattr(bootstrap_module.Path, "exists", fake_exists)
    resolved = _resolve_seed_base()
    assert resolved.name == "abilities"
    assert resolved.parent.name == "data"


def test_resolve_seed_base_falls_back_when_repo_seed_missing(monkeypatch):
    monkeypatch.delenv("SAVANT_ABILITIES_SEED_DIR", raising=False)

    original_exists = bootstrap_module.Path.exists

    def fake_exists(path_obj):
        as_posix = str(path_obj).replace("\\", "/")
        if as_posix.endswith("/data/abilities"):
            return False
        return original_exists(path_obj)

    monkeypatch.setattr(bootstrap_module.Path, "exists", fake_exists)
    resolved = _resolve_seed_base()
    assert resolved.name == "savant-abilities-seed"
    assert (resolved / "abilities" / "personas" / "engineer.md").exists()


def test_resolve_seed_base_materializes_embedded_when_data_missing(monkeypatch):
    monkeypatch.delenv("SAVANT_ABILITIES_SEED_DIR", raising=False)
    original_exists = bootstrap_module.Path.exists

    def fake_exists(path_obj):
        as_posix = str(path_obj).replace("\\", "/")
        if as_posix.endswith("/data/abilities"):
            return False
        return original_exists(path_obj)

    monkeypatch.setattr(bootstrap_module.Path, "exists", fake_exists)
    resolved = _resolve_seed_base()
    assert resolved.name == "savant-abilities-seed"
    assert (resolved / "abilities" / "personas" / "engineer.md").exists()


def test_resolve_seed_base_materializes_embedded_seed_when_all_sources_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("SAVANT_ABILITIES_SEED_DIR", raising=False)
    bootstrap_module._EMBEDDED_SEED_CACHE = None
    monkeypatch.setattr(bootstrap_module.tempfile, "gettempdir", lambda: str(tmp_path))
    original_exists = bootstrap_module.Path.exists

    def fake_exists(path_obj):
        as_posix = str(path_obj).replace("\\", "/")
        if as_posix.endswith("/data/abilities"):
            return False
        if as_posix.endswith("/seed/abilities"):
            return False
        return original_exists(path_obj)

    monkeypatch.setattr(bootstrap_module.Path, "exists", fake_exists)
    resolved = _resolve_seed_base()
    assert resolved.name == "savant-abilities-seed"
    assert (resolved / "abilities" / "personas" / "engineer.md").exists()
    resolved_again = _resolve_seed_base()
    assert resolved_again == resolved


def test_server_paths_support_explicit_locations(monkeypatch, tmp_path):
    data_dir = tmp_path / "server-data"
    db_path = tmp_path / "db-dir" / "custom.db"
    abilities_dir = tmp_path / "abilities-root"

    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SAVANT_DB", str(db_path))
    monkeypatch.setenv("SAVANT_ABILITIES_DIR", str(abilities_dir))

    assert get_server_data_dir() == data_dir
    assert get_server_db_path() == db_path
    assert get_server_abilities_base_dir() == abilities_dir


def test_default_data_dir_switches_to_container_path(monkeypatch):
    monkeypatch.setenv("RUNNING_IN_DOCKER", "1")
    assert _default_data_dir().as_posix() == "/data/savant"


def test_abilities_asset_count_is_zero_when_target_dir_missing(monkeypatch, tmp_path):
    data_dir = tmp_path / "data-missing"
    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    assert abilities_asset_count() == 0


def test_bootstrap_status_reports_store_empty_when_target_missing(monkeypatch, tmp_path):
    data_dir = tmp_path / "data-status-missing"
    bootstrap_module._EMBEDDED_SEED_CACHE = None
    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))

    status = abilities_bootstrap_status()
    assert status["asset_count"] == 0
    assert status["store_has_files"] is False
    assert status["bootstrap_available"] is True


def test_bootstrap_status_reports_available_when_target_has_only_empty_dirs(monkeypatch, tmp_path):
    data_dir = tmp_path / "data-status-empty-dirs"
    target_root = data_dir / "abilities"
    for dirname in ("personas", "rules", "policies", "repos", "styles"):
        (target_root / dirname).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))

    status = abilities_bootstrap_status()
    assert status["asset_count"] == 0
    assert status["store_has_files"] is False
    assert status["bootstrap_available"] is True


def test_seed_when_target_exists_but_empty(monkeypatch, tmp_path):
    data_dir = tmp_path / "data-empty"
    target_root = data_dir / "abilities"
    target_root.mkdir(parents=True, exist_ok=True)

    seed_dir = tmp_path / "seed-empty"
    seed_file = seed_dir / "abilities" / "personas" / "engineer.md"
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(
        "---\nid: persona.engineer\ntype: persona\ntags: [engineering]\npriority: 100\n---\nbody\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SAVANT_ABILITIES_SEED_DIR", str(seed_dir))

    result = seed_abilities_if_missing()
    assert result["seeded"] is True
    assert (target_root / "personas" / "engineer.md").exists()


def test_seed_when_target_has_subdirs_but_no_files(monkeypatch, tmp_path):
    data_dir = tmp_path / "data-subdirs-empty"
    target_root = data_dir / "abilities"
    for dirname in ("personas", "rules", "policies", "repos", "styles"):
        (target_root / dirname).mkdir(parents=True, exist_ok=True)

    seed_dir = tmp_path / "seed-subdirs-empty"
    seed_file = seed_dir / "abilities" / "rules" / "backend_api.md"
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(
        "---\nid: rule.backend.api\ntype: rule\ntags: [backend]\npriority: 100\n---\nbody\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SAVANT_ABILITIES_SEED_DIR", str(seed_dir))

    result = seed_abilities_if_missing()
    assert result["seeded"] is True
    assert (target_root / "rules" / "backend_api.md").exists()


def test_no_seed_when_target_already_has_asset_file(monkeypatch, tmp_path):
    data_dir = tmp_path / "data-populated"
    target_file = data_dir / "abilities" / "personas" / "existing.md"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text(
        "---\nid: persona.existing\ntype: persona\ntags: [existing]\npriority: 100\n---\nbody\n",
        encoding="utf-8",
    )

    seed_dir = tmp_path / "seed-populated"
    seed_file = seed_dir / "abilities" / "personas" / "engineer.md"
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(
        "---\nid: persona.engineer\ntype: persona\ntags: [engineering]\npriority: 100\n---\nbody\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SAVANT_ABILITIES_SEED_DIR", str(seed_dir))

    result = seed_abilities_if_missing()
    assert result["seeded"] is False
    assert result["reason"] == "already-populated"


def test_no_seed_when_target_has_any_existing_file(monkeypatch, tmp_path):
    data_dir = tmp_path / "data-has-file"
    target_file = data_dir / "abilities" / "README.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("existing", encoding="utf-8")

    seed_dir = tmp_path / "seed-any-file"
    seed_file = seed_dir / "abilities" / "personas" / "engineer.md"
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(
        "---\nid: persona.engineer\ntype: persona\ntags: [engineering]\npriority: 100\n---\nbody\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SAVANT_ABILITIES_SEED_DIR", str(seed_dir))

    result = seed_abilities_if_missing()
    assert result["seeded"] is False
    assert result["reason"] == "already-populated"


def test_seed_bundle_includes_expected_structure(monkeypatch, tmp_path):
    data_dir = tmp_path / "data-structure"
    seed_dir = tmp_path / "seed-structure"
    abilities_root = seed_dir / "abilities"
    for rel in (
        "personas/engineer.md",
        "personas/architect.md",
        "rules/boundaries.md",
        "rules/delivery.md",
        "policies/style/concise.md",
    ):
        file_path = abilities_root / rel
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            "---\nid: test.asset\ntype: persona\ntags: [test]\npriority: 1\n---\nbody\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("SAVANT_SERVER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SAVANT_ABILITIES_SEED_DIR", str(seed_dir))

    result = seed_abilities_if_missing()
    assert result["seeded"] is True
    for rel in (
        "personas/engineer.md",
        "personas/architect.md",
        "rules/boundaries.md",
        "rules/delivery.md",
        "policies/style/concise.md",
    ):
        assert (data_dir / "abilities" / rel).exists()
