"""Focused regression coverage for container and MCP runtime wiring."""

from pathlib import Path
import pytest

from routes.jobs_system import _list_mcp_tools

pytestmark = pytest.mark.no_db


ROOT = Path(__file__).resolve().parents[1]


def test_compose_keeps_postgres_internal_and_exposes_all_mcp_ports():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert '"5432:5432"' not in compose
    assert "postgresql://savant_user:savant_secure_password@savant-db:5432/savant" in compose
    for port in range(8091, 8096):
        assert f'"{port}:{port}"' in compose


def test_declared_mcp_mapping_matches_the_five_dedicated_ports():
    servers = _list_mcp_tools()

    assert [(server["name"], server["port"]) for server in servers] == [
        ("workspace", 8091),
        ("abilities", 8092),
        ("context", 8093),
        ("knowledge", 8094),
        ("reminders", 8095),
    ]


def test_runtime_dependencies_pin_the_mcp_major_version_and_http_client():
    requirements = (ROOT / "requirements.txt").read_text()

    assert "mcp>=1,<2" in requirements
    assert "requests>=2.31,<3" in requirements
