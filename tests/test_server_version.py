from server_version import get_build_info


def test_build_info_declares_server_version_14():
    assert get_build_info()["version"] == "14.0.2"


def test_version_and_health_endpoints_echo_build_metadata(client):
    version = client.get("/api/version").get_json()
    live = client.get("/health/live").get_json()
    ready = client.get("/health/ready").get_json()

    assert version["version"] == "14.0.2"
    assert version["app"] == "savant-server"
    assert live["version"] == "14.0.2"
    assert ready["version"] == "14.0.2"
    assert ready["branch"] == "main"
