"""Focused regression coverage for container and MCP runtime wiring."""

from pathlib import Path
import pytest

from routes.jobs_system import _list_mcp_tools

pytestmark = pytest.mark.no_db


ROOT = Path(__file__).resolve().parents[1]


def test_compose_keeps_postgres_internal_and_exposes_both_mcp_transport_port_ranges():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert '"5432:5432"' not in compose
    assert "postgresql://savant_user:savant_secure_password@savant-db:5432/savant" in compose
    for port in range(8091, 8096):
        assert f'"{port}:{port}"' in compose
    for port in range(8191, 8196):
        assert f'"{port}:{port}"' in compose


def test_declared_mcp_mapping_exposes_sse_and_streamable_http_for_every_server():
    servers = _list_mcp_tools()

    assert [(server["name"], server["port"]) for server in servers] == [
        ("workspace", 8091),
        ("abilities", 8092),
        ("context", 8093),
        ("knowledge", 8094),
        ("reminders", 8095),
    ]
    assert [
        (server["name"], server["streamable_http"]["port"], server["streamable_http"]["url"])
        for server in servers
    ] == [
        ("workspace", 8191, "http://127.0.0.1:8191/mcp"),
        ("abilities", 8192, "http://127.0.0.1:8192/mcp"),
        ("context", 8193, "http://127.0.0.1:8193/mcp"),
        ("knowledge", 8194, "http://127.0.0.1:8194/mcp"),
        ("reminders", 8195, "http://127.0.0.1:8195/mcp"),
    ]


def test_runtime_dependencies_pin_the_mcp_major_version_and_http_client():
    requirements = (ROOT / "requirements.txt").read_text()

    assert "mcp>=1,<2" in requirements
    assert "requests>=2.31,<3" in requirements
