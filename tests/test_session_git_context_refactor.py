import json

import app as server_app


def test_session_git_context_rejects_directory_that_is_not_a_git_repository(monkeypatch, tmp_path):
    session_dir = tmp_path / "session"
    project_dir = tmp_path / "project"
    session_dir.mkdir()
    project_dir.mkdir()
    (session_dir / "config.json").write_text(
        json.dumps({"projectPath": str(project_dir)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server_app, "_resolve_session_dir", lambda session_id, provider=None: str(session_dir))

    class Result:
        returncode = 128
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())

    assert server_app._get_session_git_context("session-1", "codex") == (None, None, None)


def test_git_commit_parser_uses_destination_path_for_renames(monkeypatch):
    class Result:
        returncode = 0
        stdout = (
            "COMMIT_SEPabcdef123456|Rename module|2026-07-19T00:00:00Z|Ahmed|HEAD -> main\n"
            "R100\told.py\tnew.py\n"
        )

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(server_app, "_enrich_commit", lambda git_root, commit: None)

    commits = server_app._get_git_commits("/repo")

    assert commits[0]["files"] == [{"path": "new.py", "status": "renamed", "diff": ""}]


def test_session_git_metadata_skips_non_mapping_config(monkeypatch, tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "workspace.yaml").write_text("- invalid\n- mapping\n", encoding="utf-8")
    (session_dir / "config.json").write_text(
        json.dumps({"projectPath": "/repo", "startTime": "2026-07-19T00:00:00Z"}),
        encoding="utf-8",
    )

    metadata = server_app._git_metadata_from_session_dir(str(session_dir))

    assert metadata == ("/repo", "2026-07-19T00:00:00Z", None)
