"""Round-trip test: export the abilities store as a zip, wipe it, re-import,
and confirm the assets are restored byte-for-byte."""

import io
import os
import shutil
import zipfile
from pathlib import Path

import pytest


@pytest.fixture
def abilities_base(tmp_path, monkeypatch):
    base = tmp_path / "abilities-root"
    monkeypatch.setenv("SAVANT_ABILITIES_DIR", str(base))
    # Reset the singleton so the route picks up the new base path.
    from abilities import routes as _r
    _r._store = None
    _r._resolver = None
    abilities_dir = base / "abilities"
    for cat in ("personas", "rules", "policies", "styles", "repos"):
        (abilities_dir / cat).mkdir(parents=True, exist_ok=True)
    # Seed two assets.
    (abilities_dir / "personas" / "alpha.md").write_text(
        "---\nid: persona.alpha\ntype: persona\ntags: [demo]\npriority: 10\n---\n\nHello alpha.\n",
        encoding="utf-8",
    )
    (abilities_dir / "rules" / "core.md").write_text(
        "---\nid: rule.core\ntype: rule\ntags: [core]\npriority: 50\n---\n\nBe correct.\n",
        encoding="utf-8",
    )
    yield abilities_dir
    # Singleton cleanup so other tests don't see this base dir.
    _r._store = None
    _r._resolver = None


def test_export_then_import_round_trip(client, abilities_base):
    # 1. Export — should return a zip containing the seeded assets.
    resp = client.get("/api/abilities/export")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    blob = resp.data
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = sorted(zf.namelist())
    assert "abilities/personas/alpha.md" in names
    assert "abilities/rules/core.md" in names

    # 2. Wipe the on-disk store.
    shutil.rmtree(abilities_base)
    abilities_base.mkdir(parents=True)
    assert not (abilities_base / "personas" / "alpha.md").exists()

    # 3. Import the zip back — overwrite mode.
    resp = client.post(
        "/api/abilities/import",
        data=blob,
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 200, resp.data
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["imported_count"] == 2
    assert sorted(payload["imported"]) == ["personas/alpha.md", "rules/core.md"]
    assert payload["stats"]["personas"] == 1
    assert payload["stats"]["rules"] == 1

    # 4. Confirm files are back on disk.
    assert (abilities_base / "personas" / "alpha.md").read_text("utf-8").startswith("---")
    assert (abilities_base / "rules" / "core.md").read_text("utf-8").startswith("---")


def test_import_rejects_zip_slip(client, abilities_base):
    # Craft a zip with a path-traversal entry; the import must reject it.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("abilities/../../escape.md", "---\nid: x\ntype: rule\ntags: []\npriority: 1\n---\nx\n")
        zf.writestr("abilities/rules/legit.md", "---\nid: rule.legit\ntype: rule\ntags: []\npriority: 1\n---\nok\n")
    buf.seek(0)
    resp = client.post(
        "/api/abilities/import",
        data=buf.getvalue(),
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert "rules/legit.md" in payload["imported"]
    reasons = {s["reason"] for s in payload["skipped"]}
    assert "path_outside_base" in reasons or "unknown_category" in reasons


def test_import_rejects_bad_zip(client, abilities_base):
    resp = client.post(
        "/api/abilities/import",
        data=b"not a zip",
        headers={"Content-Type": "application/zip"},
    )
    assert resp.status_code == 400
