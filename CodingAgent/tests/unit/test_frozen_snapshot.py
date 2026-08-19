"""Tests for frozen snapshot memory (FrozenMemoryStore / get_memory_for_prompt)."""

from pathlib import Path
from src.core.memory.frozen_snapshot import FrozenMemoryStore


def test_fresh_store_has_empty_snapshot():
    store = FrozenMemoryStore()
    assert store.get_frozen_snapshot() == ""


def test_load_from_disk_populates_snapshot(tmp_path: Path):
    mem_path = tmp_path / ".memory.md"
    mem_path.write_text("- [x] Completed login flow\n- [ ] Add tests\n", encoding="utf-8")
    store = FrozenMemoryStore()
    # Monkey-patch by replacing the reference
    import src.core.memory.frozen_snapshot as fs_mod
    original = fs_mod.get_memory_path
    fs_mod.get_memory_path = lambda: mem_path
    try:
        store.load_from_disk()
        snapshot = store.get_frozen_snapshot()
        assert "Completed login flow" in snapshot
        assert "MEMORY" in snapshot
    finally:
        fs_mod.get_memory_path = original


def test_get_live_entries(tmp_path: Path):
    mem_path = tmp_path / ".memory.md"
    mem_path.write_text("- [x] Task 1\n- [ ] Task 2\n", encoding="utf-8")
    store = FrozenMemoryStore()
    import src.core.memory.frozen_snapshot as fs_mod
    original = fs_mod.get_memory_path
    fs_mod.get_memory_path = lambda: mem_path
    try:
        store.load_from_disk()
        entries = store.get_live_entries()
        assert len(entries) == 2
        assert "- [x] Task 1" in entries
    finally:
        fs_mod.get_memory_path = original


def test_get_usage_after_load(tmp_path: Path):
    mem_path = tmp_path / ".memory.md"
    mem_path.write_text("- [x] Hello world\n", encoding="utf-8")
    store = FrozenMemoryStore()
    import src.core.memory.frozen_snapshot as fs_mod
    original = fs_mod.get_memory_path
    fs_mod.get_memory_path = lambda: mem_path
    try:
        store.load_from_disk()
        usage = store.get_usage()
        assert usage["entry_count"] == 1
        assert usage["char_count"] > 0
    finally:
        fs_mod.get_memory_path = original


def test_lazy_load_via_get_memory_for_prompt(tmp_path: Path):
    """Verify that get_memory_for_prompt loads from disk on first call."""
    import src.core.memory.frozen_snapshot as fs_mod
    mem_path = tmp_path / ".memory.md"
    mem_path.write_text("- [x] Lazy loaded\n", encoding="utf-8")
    original_get = fs_mod.get_memory_path
    original_store = fs_mod._memory_store
    try:
        fs_mod._memory_store = None  # force fresh singleton
        fs_mod.get_memory_path = lambda: mem_path
        snapshot = fs_mod.get_memory_for_prompt()
        assert "Lazy loaded" in snapshot
    finally:
        fs_mod._memory_store = original_store
        fs_mod.get_memory_path = original_get


def test_reload_refreshes_snapshot(tmp_path: Path):
    """Reload must not deadlock (regression: _load_lock was Lock, not RLock)."""
    mem_path = tmp_path / ".memory.md"
    mem_path.write_text("- [x] Version 1\n", encoding="utf-8")
    store = FrozenMemoryStore()
    import src.core.memory.frozen_snapshot as fs_mod
    original = fs_mod.get_memory_path
    fs_mod.get_memory_path = lambda: mem_path
    try:
        store.load_from_disk()
        assert "Version 1" in store.get_frozen_snapshot()
        mem_path.write_text("- [x] Version 2\n", encoding="utf-8")
        store.reload()
        assert "Version 2" in store.get_frozen_snapshot()
    finally:
        fs_mod.get_memory_path = original
