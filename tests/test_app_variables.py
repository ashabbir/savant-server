"""Tests for AppVariablesDB and /api/app-variables endpoints."""

import os
import pytest
from db.app_variables import AppVariablesDB
from context.ingestion import (
    get_source_availability,
    _token_for_provider,
    _sanitize_git_error,
    prepare_repository_registration,
    IngestionError,
)


def test_app_variables_crud():
    # Initial state should be None
    assert AppVariablesDB.get("TEST_KEY_1") is None
    assert AppVariablesDB.delete("TEST_KEY_1") is False

    # Set value
    AppVariablesDB.set("TEST_KEY_1", "val_1")
    assert AppVariablesDB.get("TEST_KEY_1") == "val_1"

    # Update value (upsert)
    AppVariablesDB.set("TEST_KEY_1", "val_updated")
    assert AppVariablesDB.get("TEST_KEY_1") == "val_updated"

    # List all
    AppVariablesDB.set("TEST_KEY_2", "val_2")
    all_vars = AppVariablesDB.list_all()
    assert all_vars.get("TEST_KEY_1") == "val_updated"
    assert all_vars.get("TEST_KEY_2") == "val_2"

    # Delete
    assert AppVariablesDB.delete("TEST_KEY_1") is True
    assert AppVariablesDB.get("TEST_KEY_1") is None
    assert AppVariablesDB.delete("TEST_KEY_1") is False


def test_app_variables_preference_db_over_env(monkeypatch):
    """Verify the logic: env variables or app_variables table, preferring db over env."""
    key = "GITHUB_TOKEN"
    monkeypatch.delenv(key, raising=False)

    # 1. Neither env nor DB set
    assert AppVariablesDB.get_effective_variable(key) == ""
    info = AppVariablesDB.get_effective_info(key)
    assert info["is_set"] is False
    assert info["source"] == "none"

    # 2. Only ENV set
    monkeypatch.setenv(key, "ghp_env_token_123")
    assert AppVariablesDB.get_effective_variable(key) == "ghp_env_token_123"
    info = AppVariablesDB.get_effective_info(key)
    assert info["is_set"] is True
    assert info["source"] == "env"
    assert info["env_configured"] is True
    assert info["db_configured"] is False

    # 3. Both ENV and DB set -> PREFER DB OVER ENV
    AppVariablesDB.set(key, "ghp_db_token_456")
    assert AppVariablesDB.get_effective_variable(key) == "ghp_db_token_456"
    info = AppVariablesDB.get_effective_info(key)
    assert info["is_set"] is True
    assert info["source"] == "db"
    assert info["db_configured"] is True
    assert info["env_configured"] is True

    # 4. Only DB set (env cleared)
    monkeypatch.delenv(key, raising=False)
    assert AppVariablesDB.get_effective_variable(key) == "ghp_db_token_456"
    info = AppVariablesDB.get_effective_info(key)
    assert info["is_set"] is True
    assert info["source"] == "db"

    # 5. DB set to empty string -> falls back to env if env set
    monkeypatch.setenv(key, "ghp_fallback_env")
    AppVariablesDB.set(key, "   ")
    assert AppVariablesDB.get_effective_variable(key) == "ghp_fallback_env"
    info = AppVariablesDB.get_effective_info(key)
    assert info["source"] == "env"

    # Clean up DB
    AppVariablesDB.delete(key)


def test_source_availability_reflects_db_tokens(monkeypatch):
    """Verify get_source_availability reflects tokens set in DB."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("BASE_CODE_DIR", "/tmp/code")
    monkeypatch.setattr("context.ingestion._detect_base_host_dir", lambda _: "/host/code")

    # With neither token set:
    avail = get_source_availability()
    assert avail.github is False
    assert avail.gitlab is False
    assert avail.directory is True

    # Set GITHUB_TOKEN in DB
    AppVariablesDB.set("GITHUB_TOKEN", "ghp_db_secret")
    avail = get_source_availability()
    assert avail.github is True
    assert avail.gitlab is False

    # Set GITLAB_TOKEN in DB
    AppVariablesDB.set("GITLAB_TOKEN", "glpat_db_secret")
    avail = get_source_availability()
    assert avail.github is True
    assert avail.gitlab is True

    # Clean up
    AppVariablesDB.delete("GITHUB_TOKEN")
    AppVariablesDB.delete("GITLAB_TOKEN")


def test_token_for_provider_uses_effective_tokens(monkeypatch):
    """Verify _token_for_provider returns the DB token when set, otherwise env."""
    monkeypatch.setenv("GITHUB_TOKEN", "gh_env")
    monkeypatch.setenv("GITLAB_TOKEN", "gl_env")

    # DB not set yet -> env is returned
    assert _token_for_provider("github") == "gh_env"
    assert _token_for_provider("gitlab") == "gl_env"

    # Set DB tokens -> DB is preferred
    AppVariablesDB.set("GITHUB_TOKEN", "gh_db")
    AppVariablesDB.set("GITLAB_TOKEN", "gl_db")
    assert _token_for_provider("github") == "gh_db"
    assert _token_for_provider("gitlab") == "gl_db"

    # Clean up
    AppVariablesDB.delete("GITHUB_TOKEN")
    AppVariablesDB.delete("GITLAB_TOKEN")


def test_sanitize_git_error_redacts_db_and_env_tokens(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env_secret_token_123")
    AppVariablesDB.set("GITHUB_TOKEN", "db_secret_token_456")

    raw_message = "Failed: invalid credentials env_secret_token_123 and db_secret_token_456"
    sanitized = _sanitize_git_error(raw_message)
    assert "env_secret_token_123" not in sanitized
    assert "db_secret_token_456" not in sanitized
    assert "[REDACTED]" in sanitized

    AppVariablesDB.delete("GITHUB_TOKEN")


def test_prepare_repository_registration_with_db_token(tmp_path, monkeypatch):
    base = tmp_path / "repos"
    base.mkdir(parents=True)
    monkeypatch.setenv("BASE_CODE_DIR", str(base))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    # Without token in DB or ENV, registration fails
    with pytest.raises(IngestionError, match="Github source is not configured"):
        prepare_repository_registration("https://github.com/myorg/myproject.git")

    # With token in DB, registration succeeds
    AppVariablesDB.set("GITHUB_TOKEN", "ghp_test_db_token")
    reg = prepare_repository_registration("https://github.com/myorg/myproject.git")
    assert reg.name == "myproject"
    assert reg.provider == "github"

    AppVariablesDB.delete("GITHUB_TOKEN")


def test_sources_api_endpoint_with_db_tokens(client, monkeypatch):
    """Test /api/context/repos/sources endpoint with DB tokens."""
    from context import routes
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setenv("BASE_CODE_DIR", "/tmp/repos")
    monkeypatch.setattr("context.ingestion._detect_base_host_dir", lambda _: None)

    # When DB is empty and env is empty
    resp = client.get("/api/context/repos/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sources"]["github"]["enabled"] is False
    assert data["sources"]["gitlab"]["enabled"] is False
    assert data["sources"]["directory"]["enabled"] is True

    # Put GITHUB_TOKEN in DB
    AppVariablesDB.set("GITHUB_TOKEN", "ghp_db_token_api")
    resp = client.get("/api/context/repos/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sources"]["github"]["enabled"] is True
    assert data["sources"]["gitlab"]["enabled"] is False

    # Put GITLAB_TOKEN in DB
    AppVariablesDB.set("GITLAB_TOKEN", "glpat_db_token_api")
    resp = client.get("/api/context/repos/sources")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["sources"]["github"]["enabled"] is True
    assert data["sources"]["gitlab"]["enabled"] is True

    AppVariablesDB.delete("GITHUB_TOKEN")
    AppVariablesDB.delete("GITLAB_TOKEN")


def test_app_variables_api_routes(client):
    """Test CRUD operations via /api/app-variables REST API."""
    # 1. Set variable via POST (single)
    resp = client.post("/api/app-variables", json={"key": "GITHUB_TOKEN", "value": "ghp_api_123456789"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "saved"
    assert data["key"] == "GITHUB_TOKEN"
    assert "..." in data["value"]  # masked

    # 2. Get single variable
    resp = client.get("/api/app-variables/GITHUB_TOKEN")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["key"] == "GITHUB_TOKEN"
    assert data["effective_info"]["source"] == "db"
    assert data["effective_info"]["db_configured"] is True

    # 3. Get with raw=true
    resp = client.get("/api/app-variables/GITHUB_TOKEN?raw=true")
    assert resp.status_code == 200
    assert resp.get_json()["value"] == "ghp_api_123456789"

    # 4. Batch set via POST
    resp = client.post("/api/app-variables", json={
        "variables": {
            "GITLAB_TOKEN": "glpat_api_987654321",
            "CUSTOM_VAR": "custom_val"
        }
    })
    assert resp.status_code == 200
    assert "GITLAB_TOKEN" in resp.get_json()["updated"]

    # 5. List all variables
    resp = client.get("/api/app-variables")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "GITHUB_TOKEN" in data["variables"]
    assert "GITLAB_TOKEN" in data["variables"]
    assert data["effective"]["GITHUB_TOKEN"]["is_set"] is True
    assert data["effective"]["GITLAB_TOKEN"]["is_set"] is True

    # 6. PUT update
    resp = client.put("/api/app-variables/CUSTOM_VAR", json={"value": "updated_custom_val"})
    assert resp.status_code == 200

    resp = client.get("/api/app-variables/CUSTOM_VAR")
    assert resp.get_json()["value"] == "updated_custom_val"

    # 7. DELETE variable
    resp = client.delete("/api/app-variables/CUSTOM_VAR")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "deleted"

    # Delete non-existent
    resp = client.delete("/api/app-variables/NON_EXISTENT")
    assert resp.status_code == 404

    # Clean up
    AppVariablesDB.delete("GITHUB_TOKEN")
    AppVariablesDB.delete("GITLAB_TOKEN")
