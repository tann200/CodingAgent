"""Unit tests for unified git_read / lsp tools, optimized 3-tier toolsets, and conditional gating."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.orchestration.graph import runtime_policy as rp
from src.tools._registry import build_registry
from src.tools.git_tools import git_read
from src.tools.lsp_tools import lsp


def test_git_read_dispatches_status_diff_log_and_rejects_invalid(tmp_path, monkeypatch):
    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        "src.tools.git_tools.git_status",
        lambda workdir=None: calls.append(("status", {"workdir": workdir})) or {"status": "ok", "branch": "main", "files": []},
    )
    monkeypatch.setattr(
        "src.tools.git_tools.git_diff",
        lambda workdir=None, staged=False, path=None: calls.append(("diff", {"staged": staged, "path": path})) or {"status": "ok", "diff": ""},
    )
    monkeypatch.setattr(
        "src.tools.git_tools.git_log",
        lambda workdir=None, max_count=10: calls.append(("log", {"max_count": max_count})) or {"status": "ok", "commits": []},
    )

    assert git_read(action="status", workdir=tmp_path)["status"] == "ok"
    assert git_read(action="diff", staged=True, path="a.py", workdir=tmp_path)["status"] == "ok"
    assert git_read(action="log", max_count=5, workdir=tmp_path)["status"] == "ok"
    assert calls[0][0] == "status"
    assert calls[1] == ("diff", {"staged": True, "path": "a.py"})
    assert calls[2] == ("log", {"max_count": 5})

    bad = git_read(action="push", workdir=tmp_path)  # type: ignore[arg-type]
    assert bad["status"] == "error"
    assert "Unknown git_read action" in bad["error"]


def test_lsp_unified_dispatches_and_validates_coordinates(monkeypatch):
    diag_mock = AsyncMock(return_value={"ok": True, "output": "clean"})
    sym_mock = AsyncMock(return_value={"ok": True, "output": "foo"})
    def_mock = AsyncMock(return_value={"ok": True, "output": "a.py:1"})
    ref_mock = AsyncMock(return_value={"ok": True, "output": "a.py:2"})
    hov_mock = AsyncMock(return_value={"ok": True, "output": "int"})

    monkeypatch.setattr("src.tools.lsp_tools.lsp_diagnostics", diag_mock)
    monkeypatch.setattr("src.tools.lsp_tools.lsp_symbols", sym_mock)
    monkeypatch.setattr("src.tools.lsp_tools.lsp_definition", def_mock)
    monkeypatch.setattr("src.tools.lsp_tools.lsp_references", ref_mock)
    monkeypatch.setattr("src.tools.lsp_tools.lsp_hover", hov_mock)

    assert asyncio.run(lsp(action="diagnostics", path="a.py"))["output"] == "clean"
    assert asyncio.run(lsp(action="symbols", path="a.py"))["output"] == "foo"
    assert asyncio.run(lsp(action="definition", path="a.py", line=1, col=2))["output"] == "a.py:1"
    assert asyncio.run(lsp(action="references", path="a.py", line=1, col=2))["output"] == "a.py:2"
    assert asyncio.run(lsp(action="hover", path="a.py", line=1, col=2))["output"] == "int"

    missing_coords = asyncio.run(lsp(action="definition", path="a.py"))
    assert missing_coords["ok"] is False
    assert "line" in missing_coords["error"]

    bad_action = asyncio.run(lsp(action="rename", path="a.py"))  # type: ignore[arg-type]
    assert bad_action["ok"] is False
    assert "Unknown lsp action" in bad_action["error"]


def test_conditional_tool_gating_git_lsp_and_background_task(tmp_path: Path, monkeypatch):
    reg = build_registry(working_dir=str(tmp_path))

    # Non-git directory + no LSP + no background tasks
    non_git = tmp_path / "plain_dir"
    non_git.mkdir()
    monkeypatch.setattr(rp, "_is_lsp_available", lambda _wd=None: False)
    orch_plain = SimpleNamespace(tool_registry=reg, working_dir=str(non_git), explore_mode=False)

    std_names = {
        s["function"]["name"]
        for s in rp.resolve_effective_tool_schemas(orch_plain, rp.STANDARD_RUNTIME_POLICY)
    }
    assert "git_read" not in std_names
    assert "check_background_task" not in std_names
    assert "delete_file" in std_names
    assert "read_web_page" in std_names

    fr_names = {
        s["function"]["name"]
        for s in rp.resolve_effective_tool_schemas(orch_plain, rp.FRONTIER_RUNTIME_POLICY)
    }
    assert "git_read" not in fr_names
    assert "lsp" not in fr_names
    assert "check_background_task" not in fr_names
    assert "delegate_task" in fr_names

    # Git directory + LSP available + active background task on this orchestrator -> all 3 admitted!
    git_dir = tmp_path / "git_repo"
    (git_dir / ".git").mkdir(parents=True)
    monkeypatch.setattr(rp, "_is_lsp_available", lambda _wd=None: True)
    orch_full = SimpleNamespace(
        tool_registry=reg,
        working_dir=str(git_dir),
        explore_mode=False,
        _has_background_tasks=True,
    )

    fr_full_names = {
        s["function"]["name"]
        for s in rp.resolve_effective_tool_schemas(orch_full, rp.FRONTIER_RUNTIME_POLICY)
    }
    assert "git_read" in fr_full_names
    assert "lsp" in fr_full_names
    assert "check_background_task" in fr_full_names
    assert "load_tool_group" in fr_full_names
    assert len(fr_full_names) == 19  # 18 frontier base + 1 dynamic check_background_task

    # Compact tier (idle) includes delete_file, excludes manage_todo/list_files/git_read/check_background_task
    comp_names = {
        s["function"]["name"]
        for s in rp.resolve_effective_tool_schemas(orch_plain, rp.COMPACT_RUNTIME_POLICY)
    }
    assert comp_names == {
        "read_file",
        "read_tool_output",
        "glob",
        "grep",
        "edit_file_atomic",
        "write_file",
        "delete_file",
        "bash",
        "ask_user",
        "load_skill",
    }


def test_read_tool_output_eviction_gating_and_load_tool_group(tmp_path: Path, monkeypatch):
    from src.core.orchestration.tool_output_reducer import ToolOutputStore
    from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR
    from src.tools.tool_group_tools import load_tool_group

    reg = build_registry(working_dir=str(tmp_path))
    monkeypatch.setattr(rp, "_is_lsp_available", lambda _wd=None: False)
    monkeypatch.delenv("CODINGAGENT_PRIVATE", raising=False)

    # Real-orchestrator-like object (has execute_tool) with NO evicted outputs -> read_tool_output omitted
    orch = SimpleNamespace(
        tool_registry=reg,
        working_dir=str(tmp_path),
        explore_mode=False,
        execute_tool=lambda _a: {"ok": True},
    )
    idle_names = {
        s["function"]["name"]
        for s in rp.resolve_effective_tool_schemas(orch, rp.COMPACT_RUNTIME_POLICY)
    }
    assert "read_tool_output" not in idle_names

    # Once an output is evicted into ToolOutputStore -> read_tool_output appears immediately
    store = ToolOutputStore()
    store.store("big output" * 500, tool_name="bash")
    orch._tool_output_store = store
    after_evict_names = {
        s["function"]["name"]
        for s in rp.resolve_effective_tool_schemas(orch, rp.COMPACT_RUNTIME_POLICY)
    }
    assert "read_tool_output" in after_evict_names

    # load_tool_group unlocks git-write & ast on demand, while respecting private mode & explore_mode
    tok = _PARENT_ORCHESTRATOR_VAR.set(orch)
    try:
        res_git = load_tool_group(group="git-write")
        assert res_git["status"] == "ok"
        assert "git_commit" in res_git["tools"]

        fr_loaded = {
            s["function"]["name"]
            for s in rp.resolve_effective_tool_schemas(orch, rp.FRONTIER_RUNTIME_POLICY)
        }
        assert {"git_commit", "git_stash", "git_restore"}.issubset(fr_loaded)

        # Read-only explore_mode still blocks git-write even if loaded!
        orch.explore_mode = True
        fr_explore = {
            s["function"]["name"]
            for s in rp.resolve_effective_tool_schemas(orch, rp.FRONTIER_RUNTIME_POLICY)
        }
        assert {"git_commit", "git_stash", "git_restore"}.isdisjoint(fr_explore)
        orch.explore_mode = False

        # Private mode blocks memory group
        monkeypatch.setenv("CODINGAGENT_PRIVATE", "1")
        res_mem = load_tool_group(group="memory")
        assert res_mem["status"] == "error"
        assert "private" in res_mem["error"].lower()
    finally:
        _PARENT_ORCHESTRATOR_VAR.reset(tok)
