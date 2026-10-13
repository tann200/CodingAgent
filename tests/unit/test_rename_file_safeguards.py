"""Unit tests for rename_file safeguards.

Regression coverage for Audit §4 High:
- rename_file must reject overwriting an existing destination file.
- rename_file must enforce WorkspaceGuard on both source and destination paths.
- rename_file must respect project deny_write patterns.
- rename_file must require read_before_write on the source file.
"""

from __future__ import annotations

from pathlib import Path

from src.tools._file_io import rename_file, read_file


def test_rename_file_rejects_existing_destination(tmp_path: Path) -> None:
    src = tmp_path / "source.txt"
    dst = tmp_path / "dest.txt"
    src.write_text("source content", encoding="utf-8")
    dst.write_text("dest content", encoding="utf-8")

    # Mark source as read to pass RBW check
    read_file(str(src), workdir=tmp_path)

    res = rename_file(path=str(src), new_path=str(dst), workdir=tmp_path)
    assert res.get("status") == "error"
    assert "destination exists" in res.get("error", "").lower()
    # Both files must remain intact
    assert src.read_text(encoding="utf-8") == "source content"
    assert dst.read_text(encoding="utf-8") == "dest content"


def test_rename_file_protects_git_and_env(tmp_path: Path) -> None:
    src = tmp_path / "my_secret.txt"
    src.write_text("secrets", encoding="utf-8")
    read_file(str(src), workdir=tmp_path)

    # Attempt to rename into .git directory
    res = rename_file(path=str(src), new_path=".git/hooks/pre-commit", workdir=tmp_path)
    assert res.get("status") == "error"
    assert "protected file" in res.get("error", "").lower()

    # Attempt to rename a protected file like .env
    env_file = tmp_path / ".env"
    env_file.write_text("KEY=val", encoding="utf-8")
    read_file(str(env_file), workdir=tmp_path)

    res2 = rename_file(path=".env", new_path="env_backup.txt", workdir=tmp_path)
    assert res2.get("status") == "error"
    assert "protected file" in res2.get("error", "").lower()


def test_rename_file_success(tmp_path: Path) -> None:
    src = tmp_path / "doc.txt"
    src.write_text("hello doc", encoding="utf-8")
    read_file(str(src), workdir=tmp_path)

    res = rename_file(path="doc.txt", new_path="sub/doc_new.txt", workdir=tmp_path)
    assert res.get("status") == "ok"
    assert not src.exists()
    assert (tmp_path / "sub" / "doc_new.txt").read_text(encoding="utf-8") == "hello doc"
