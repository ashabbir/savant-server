from pathlib import Path


def test_docker_image_installs_dulwich_from_declared_requirements():
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    contents = dockerfile.read_text()

    assert "grep '^dulwich' /app/requirements.txt" in contents
    assert "python -m pip install --no-cache-dir" in contents
