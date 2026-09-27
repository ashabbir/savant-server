import pytest

from server_version import get_build_info


@pytest.mark.no_db
def test_build_info_declares_server_version_17_0_0():
    assert get_build_info()["version"] == "17.0.0"


def test_version_and_health_endpoints_echo_build_metadata(client):
    version = client.get("/api/version").get_json()
    live = client.get("/health/live").get_json()
    ready = client.get("/health/ready").get_json()

    assert version["version"] == "17.0.0"
    assert version["app"] == "savant-server"
    assert live["version"] == "17.0.0"
    assert ready["version"] == "17.0.0"
    assert ready["branch"] == "main"
