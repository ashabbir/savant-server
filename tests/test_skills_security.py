import io
import tarfile
import zipfile

import pytest


def test_archive_target_rejects_prefix_escape(tmp_path):
    from abilities.skills_shared import _resolve_extraction_target

    target = tmp_path / "skills"
    target.mkdir()
    with pytest.raises(ValueError, match="Unsafe archive"):
        _resolve_extraction_target(target, "../skills-escape/file.txt")


def test_tar_extraction_rejects_symlink(tmp_path):
    from abilities.skills_shared import _safe_extract_tar

    archive = tmp_path / "skill.tar"
    target = tmp_path / "out"
    target.mkdir()
    with tarfile.open(archive, "w") as tf:
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)

    with pytest.raises(ValueError, match="Links are not allowed"):
        _safe_extract_tar(archive, target)


def test_zip_extraction_accepts_nested_file(tmp_path):
    from abilities.skills_shared import _safe_extract_zip

    archive = tmp_path / "skill.zip"
    target = tmp_path / "out"
    target.mkdir()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("nested/SKILL.md", "# Skill\n")

    _safe_extract_zip(archive, target)
    assert (target / "nested" / "SKILL.md").read_text() == "# Skill\n"
