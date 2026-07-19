import postgres_client
import requests


class _Cursor:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *args):
        pass

    def fetchone(self):
        return {"?column?": 1}


class _Connection:
    def cursor(self):
        return _Cursor()


def test_system_info_falls_back_from_invalid_port_environment(monkeypatch, client):
    monkeypatch.setenv("FLASK_PORT", "invalid")
    monkeypatch.setenv("SAVANT_MCP_CONTEXT_PORT", "invalid")
    monkeypatch.setattr(postgres_client, "get_connection", lambda: _Connection())
    monkeypatch.setattr(postgres_client, "release_connection", lambda connection: None)
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))

    response = client.get("/api/system/info")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["flask"]["port"] == 8090
    assert payload["mcp_servers"]["context"]["port"] == 8093
