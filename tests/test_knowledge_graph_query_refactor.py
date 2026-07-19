import db.knowledge_graph as knowledge_graph
from db.knowledge_graph import KnowledgeGraphDB


class _Cursor:
    def __init__(self, result_sets):
        self.result_sets = iter(result_sets)
        self.rows = []
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        self.queries.append((query, params))
        self.rows = next(self.result_sets)

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, result_sets):
        self.cursor_instance = _Cursor(result_sets)

    def cursor(self):
        return self.cursor_instance


def test_typed_full_graph_expands_to_connected_committed_nodes(monkeypatch):
    matching_node = {
        "node_id": "service-1", "node_type": "service", "title": "Service",
        "status": "committed", "metadata": {},
    }
    connected_node = {
        "node_id": "repo-1", "node_type": "repo", "title": "Repo",
        "status": "committed", "metadata": {},
    }
    edge = {
        "edge_id": "edge-1", "source_id": "service-1", "target_id": "repo-1",
        "edge_type": "belongs_to", "metadata": {},
    }
    connection = _Connection([[matching_node], [edge], [connected_node]])
    monkeypatch.setattr(knowledge_graph, "get_connection", lambda: connection)
    monkeypatch.setattr(knowledge_graph, "release_connection", lambda conn: None)

    graph = KnowledgeGraphDB.get_full_graph(node_type="service")

    assert {node["node_id"] for node in graph["nodes"]} == {"service-1", "repo-1"}
    assert [item["edge_id"] for item in graph["edges"]] == ["edge-1"]
    assert all("status = 'committed'" in query for query, _ in connection.cursor_instance.queries[::2])
