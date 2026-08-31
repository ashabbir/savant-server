"""Readiness and MCP-health endpoint behavior."""

import pytest

import routes.jobs_system as jobs_system

pytestmark = pytest.mark.no_db


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args):
        return None


class _Connection:
    def cursor(self):
        return _Cursor()


class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def close(self):
        return None


def test_liveness_does_not_depend_on_postgres(client, monkeypatch):
    monkeypatch.setattr(jobs_system, "get_connection", lambda: (_ for _ in ()).throw(OSError("offline")))

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.get_json()["status"] == "live"


def test_readiness_reports_postgres_failure_without_leaking_connection_details(client, monkeypatch):
    monkeypatch.setattr(
        jobs_system,
        "get_connection",
        lambda: (_ for _ in ()).throw(OSError("password=sensitive database offline")),
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "not_ready"
    assert payload["dependencies"]["postgres"]["status"] == "unavailable"
    assert "password" not in str(payload).lower()


def test_readiness_reports_postgres_when_available(client, monkeypatch):
    connection = _Connection()
    released = []
    monkeypatch.setattr(jobs_system, "get_connection", lambda: connection)
    monkeypatch.setattr(jobs_system, "release_connection", released.append)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.get_json()["dependencies"]["postgres"]["status"] == "ok"
    assert released == [connection]


def test_mcp_health_checks_every_configured_port_and_reports_failures(client, monkeypatch):
    checked_urls = []

    def fake_get(url, **_kwargs):
        checked_urls.append(url)
        if ":8094/" in url:
            raise OSError("offline")
        return _Response()

    monkeypatch.setattr(jobs_system.requests, "get", fake_get)

    response = client.get("/api/mcp/health")

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["status"] == "degraded"
    assert {server["port"] for server in payload["servers"]} == {8091, 8092, 8093, 8094, 8095}
    failed = next(server for server in payload["servers"] if server["port"] == 8094)
    assert failed["status"] == "unavailable"
    assert failed["diagnostic"] == "connection failed"
    assert len(checked_urls) == 5


def test_single_mcp_health_uses_live_probe(client, monkeypatch):
    monkeypatch.setattr(jobs_system.requests, "get", lambda *_args, **_kwargs: _Response(503))

    response = client.get("/api/mcp/health/workspace")

    assert response.status_code == 503
    assert response.get_json()["status"] == "unavailable"
    assert response.get_json()["diagnostic"] == "HTTP 503"
