import subprocess

from context.activity import collect_git_change_details


def test_collect_git_change_details_includes_commits_files_and_stats(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("one\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "first"], cwd=tmp_path, check=True, capture_output=True)
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    tracked.write_text("one\ntwo\n")
    added = tmp_path / "added.txt"
    added.write_text("new\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "record changes"], cwd=tmp_path, check=True, capture_output=True)
    after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    details = collect_git_change_details(tmp_path, before, after)

    assert details["commit_subject"] == "record changes"
    assert details["files_changed"]["added"] == ["added.txt"]
    assert details["files_changed"]["modified"] == ["tracked.txt"]
    assert details["change_stats"]["files_total"] == 2
    assert details["change_stats"]["insertions"] == 2
