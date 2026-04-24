"""tests/unit/test_file_lock.py — TASK-REL-2

Tests for src.core.memory.file_lock — OS-level file locking for memory files.
Verifies thread-safety, correct exclusive/shared semantics, and that
memory_save never interleaves concurrent writes.
"""


# ruff: noqa: E501
from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path

import pytest

from src.core.memory.file_lock import locked_file


# ---------------------------------------------------------------------------
# Basic read / write / append
# ---------------------------------------------------------------------------


def test_locked_write_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "test.txt"
    with locked_file(p, mode="w") as fh:
        fh.write("hello")
    assert p.read_text() == "hello"


def test_locked_read_existing_file(tmp_path: Path) -> None:
    p = tmp_path / "test.txt"
    p.write_text("world")
    with locked_file(p, mode="r") as fh:
        content = fh.read()
    assert content == "world"


def test_locked_append(tmp_path: Path) -> None:
    p = tmp_path / "test.txt"
    p.write_text("line1\n")
    with locked_file(p, mode="a") as fh:
        fh.write("line2\n")
    assert p.read_text() == "line1\nline2\n"


def test_locked_readwrite_mode(tmp_path: Path) -> None:
    """a+ mode: seek to 0 to read, then overwrite with new content."""
    p = tmp_path / "test.txt"
    p.write_text("old content")
    with locked_file(p, mode="a+") as fh:
        fh.seek(0)
        old = fh.read()
        new = old.replace("old", "new")
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as t:
            t.write(new)
        os.replace(tmp, str(p))
    assert p.read_text() == "new content"


# ---------------------------------------------------------------------------
# Concurrency: 10 threads write 10 lines each — no interleaving
# ---------------------------------------------------------------------------


def test_concurrent_writes_no_interleaving(tmp_path: Path) -> None:
    """10 threads each appending a unique marker; result must contain all 100."""
    p = tmp_path / "memory.md"
    p.write_text("")
    errors: list[str] = []

    def _writer(thread_id: int) -> None:
        for i in range(10):
            try:
                with locked_file(p, mode="a+") as fh:
                    fh.seek(0)
                    existing = fh.read()
                    new = existing + f"T{thread_id}-L{i}\n"
                    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
                    with os.fdopen(fd, "w") as t:
                        t.write(new)
                    os.replace(tmp, str(p))
            except Exception as exc:
                errors.append(f"thread {thread_id} iter {i}: {exc}")

    threads = [threading.Thread(target=_writer, args=(tid,)) for tid in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"Errors in writer threads: {errors}"

    final = p.read_text()
    lines = [ln for ln in final.splitlines() if ln]
    # All 100 unique markers must be present
    assert len(lines) == 100, f"Expected 100 lines, got {len(lines)}"
    for tid in range(10):
        for i in range(10):
            assert f"T{tid}-L{i}" in final, f"Missing T{tid}-L{i}"


# ---------------------------------------------------------------------------
# Different paths get different locks (no cross-file blocking)
# ---------------------------------------------------------------------------


def test_different_paths_independent(tmp_path: Path) -> None:
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("")
    p2.write_text("")

    results: list[str] = []

    def _write_both() -> None:
        with locked_file(p1, mode="a") as f1:
            # While holding p1 lock, also acquire p2 — should not deadlock
            with locked_file(p2, mode="a") as f2:
                f1.write("p1\n")
                f2.write("p2\n")
                results.append("ok")

    t = threading.Thread(target=_write_both)
    t.start()
    t.join(timeout=5)

    assert results == ["ok"]
    assert p1.read_text() == "p1\n"
    assert p2.read_text() == "p2\n"


# ---------------------------------------------------------------------------
# memory_save integration: concurrent saves produce consistent output
# ---------------------------------------------------------------------------


def test_memory_save_concurrent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """10 concurrent memory_save calls all persist their entry."""
    from src.tools import memory_tools

    mem_file = tmp_path / "memory.md"
    mem_file.write_text("")

    # Point the module at our temp file
    monkeypatch.setattr(memory_tools, "_MEMORY_FILE", mem_file)

    errors: list[str] = []

    def _save(i: int) -> None:
        try:
            result = memory_tools.memory_save(
                content=f"concurrent entry {i}",
                category="test",
            )
            if result.get("status") != "ok":
                errors.append(f"save {i} failed: {result}")
        except Exception as exc:
            errors.append(f"save {i} exception: {exc}")

    threads = [threading.Thread(target=_save, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"Errors: {errors}"

    final = mem_file.read_text()
    for i in range(10):
        assert f"concurrent entry {i}" in final, f"Missing entry {i}"
