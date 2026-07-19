import importlib.util
from pathlib import Path

from context.routes import _build_impact_surface_internal


spec = importlib.util.spec_from_file_location(
    "savant_mcp_context_server",
    Path(__file__).parents[1] / "mcp" / "context_server.py",
)
context_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context_server)
_build_impact_surface = context_server._build_impact_surface


def test_impact_surface_ignores_malformed_graph_nodes_and_edges():
    results = {
        "code_graph_search": {
            "query": [
                {"node_id": "target", "title": "Target", "edges": None},
                {"node_id": "target", "title": "Target", "edges": [None, {"source_id": None}]},
                "not-a-node",
                {
                    "node_id": "target",
                    "title": "Target",
                    "edges": [{"source_id": "caller", "target_id": "target", "label": "calls"}],
                },
            ]
        }
    }

    surface = _build_impact_surface(results, ["target"], {"target.py"})

    assert surface["upstream_dependencies_1_level_up"] == [{
        "upstream_caller": "caller",
        "relationship": "calls",
        "target_symbol": "Target",
    }]
    assert surface["affected_files"] == ["target.py"]


def test_internal_impact_surface_ignores_malformed_graph_nodes_and_edges():
    results = {
        "code_graph_search": {
            "query": [
                {"node_id": "target", "edges": None},
                {"node_id": "target", "edges": [{"source_id": None}]},
                {
                    "node_id": "target",
                    "title": "Target",
                    "edges": [{"source_id": "caller", "target_id": "target", "label": "calls"}],
                },
            ]
        }
    }

    surface = _build_impact_surface_internal(results, ["target"], {"target.py"})

    assert surface["upstream_dependencies_1_level_up"] == [{
        "upstream_caller": "caller",
        "relationship": "calls",
        "target_symbol": "Target",
    }]
