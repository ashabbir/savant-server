from server_version import get_build_info


def test_build_info_declares_server_version_15():
    assert get_build_info()["version"] == "15.0.0"


def test_version_and_health_endpoints_echo_build_metadata(client):
    version = client.get("/api/version").get_json()
    live = client.get("/health/live").get_json()
    ready = client.get("/health/ready").get_json()

    assert version["version"] == "15.0.0"
    assert version["app"] == "savant-server"
    assert live["version"] == "15.0.0"
    assert ready["version"] == "15.0.0"
    assert ready["branch"] == "main"
