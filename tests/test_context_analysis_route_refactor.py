import context.routes as routes


def test_analysis_route_rejects_non_object_json(monkeypatch, client):
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)

    response = client.post(
        "/api/context/analysis",
        data="[1]",
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "request body must be a JSON object"


def test_research_route_rejects_unknown_search_type(monkeypatch, client):
    monkeypatch.setattr(routes, "_ensure_init", lambda: True)

    response = client.post(
        "/api/context/research",
        json={"q": "sample", "type": "invalid"},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "type must be one of: all, code, memory",
        "allowed_types": ["all", "code", "memory"],
    }
