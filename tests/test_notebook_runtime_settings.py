import pytest
from flask import Flask

pytestmark = pytest.mark.no_db

HEADERS = {"X-App-Name": "savant-notebook"}


@pytest.fixture
def client(monkeypatch):
    import routes.notebooks as notebooks
    from db.notebooks import NotebookDB

    saved = {}
    monkeypatch.setattr(notebooks, "_access", lambda *args, **kwargs: ({"role": "owner"}, None))
    monkeypatch.setattr(
        NotebookDB, "update",
        lambda notebook_id, updates, user_id: saved.update(updates) or {"notebook_id": notebook_id, **updates},
    )

    app = Flask(__name__)
    app.register_blueprint(notebooks.notebooks_bp)

    @app.before_request
    def _identify():
        from flask import g
        g.user_id = "user-1"

    test_client = app.test_client()
    test_client.saved = saved
    return test_client


def test_patch_persists_a_per_notebook_provider_and_model(client):
    response = client.patch(
        "/api/notebooks/nb-1",
        json={"title": "Notebook", "runtime_settings": {"provider": "claude", "model": "opus"}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert client.saved["runtime_settings"] == {"provider": "claude", "model": "opus"}


def test_patch_clears_a_pinned_runtime(client):
    response = client.patch(
        "/api/notebooks/nb-1",
        json={"title": "Notebook", "runtime_settings": {"provider": "", "model": ""}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert client.saved["runtime_settings"] == {"provider": "", "model": ""}


@pytest.mark.parametrize("settings", [
    {"provider": 123},
    {"model": ["opus"]},
    {"provider": "x" * 201},
])
def test_patch_rejects_non_string_or_oversized_runtime_values(client, settings):
    response = client.patch(
        "/api/notebooks/nb-1",
        json={"title": "Notebook", "runtime_settings": settings},
        headers=HEADERS,
    )
    assert response.status_code == 400
    assert "runtime_settings" in response.get_json()["error"]


def test_patch_rejects_non_object_runtime_settings(client):
    response = client.patch(
        "/api/notebooks/nb-1",
        json={"title": "Notebook", "runtime_settings": "claude"},
        headers=HEADERS,
    )
    assert response.status_code == 400
