"""OPT-02 regression tests: pruned glob with secret exclusion and bounded rg/grep."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.tools._file_io import glob
from src.tools.system_tools import grep


def test_glob_prunes_venv_git_and_blocks_secret_files(tmp_path: Path):
    """glob() must prune .venv/.git/node_modules and never return secret files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "root.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "pkg.py").write_text("z = 3\n", encoding="utf-8")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "a.py").write_text("g = 4\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET_KEY=abc\n", encoding="utf-8")
    (tmp_path / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")

    res_py = glob("**/*.py", workdir=tmp_path)
    assert res_py["status"] == "ok"
    matches_py = set(res_py["matches"])
    assert "src/app.py" in matches_py
    assert "root.py" in matches_py
    assert not any(".venv" in m for m in matches_py)
    assert not any(".git" in m for m in matches_py)

    res_all = glob("**/*", workdir=tmp_path)
    assert res_all["status"] == "ok"
    matches_all = set(res_all["matches"])
    assert ".env" not in matches_all
    assert "id_rsa" not in matches_all
    assert not any(".git" in m for m in matches_all)


def test_grep_finds_matches_with_rg_or_fallback_and_excludes_secrets(tmp_path: Path):
    """grep() finds matches accurately under both rg fast-path and fallback while blocking .env."""
    (tmp_path / "mod.py").write_text("def target_symbol():\n    return 42\n", encoding="utf-8")
    (tmp_path / ".env").write_text("target_symbol=supersecret\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "dep.py").write_text("target_symbol = 0\n", encoding="utf-8")

    # Default path (uses rg if installed, else grep/python)
    r1 = grep("target_symbol", path=".", workdir=tmp_path)
    assert r1.get("ok") is True
    data1 = r1.get("output") if isinstance(r1.get("output"), dict) else r1
    matches1 = data1.get("matches", [])
    assert len(matches1) == 1
    assert matches1[0]["file_path"].endswith("mod.py")

    # Force POSIX grep / Python fallback path by hiding rg
    with patch("shutil.which", return_value=None):
        r2 = grep("target_symbol", path=".", workdir=tmp_path)
    assert r2.get("ok") is True
    data2 = r2.get("output") if isinstance(r2.get("output"), dict) else r2
    matches2 = data2.get("matches", [])
    assert len(matches2) == 1
    assert matches2[0]["file_path"].endswith("mod.py")
