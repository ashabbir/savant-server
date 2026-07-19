import knowledge.routes as routes


def test_store_experience_coerces_scalar_text_fields(monkeypatch, client):
    monkeypatch.setattr(
        routes.KnowledgeGraphDB,
        "create_node",
        lambda payload: {"node_id": "node-1", **payload},
    )
    monkeypatch.setattr(routes.ExperienceDB, "create", lambda payload: payload)

    response = client.post(
        "/api/knowledge/store",
        json={"content": 123, "title": 456, "graph_type": 7},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["content"] == "123"
    assert payload["title"] == "456"
    assert payload["metadata"]["graph_type"] == "7"


def test_update_node_coerces_scalar_text_fields(monkeypatch, client):
    monkeypatch.setattr(routes, "check_domain_write_access", lambda *args, **kwargs: (True, None))
    monkeypatch.setattr(
        routes.KnowledgeGraphDB,
        "update_node",
        lambda node_id, payload: {"node_id": node_id, **payload},
    )

    response = client.put(
        "/api/knowledge/nodes/node-1",
        json={"title": 123, "content": 456, "graph_type": 789},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["title"] == "123"
    assert payload["content"] == "456"
    assert payload["metadata"]["graph_type"] == "789"
