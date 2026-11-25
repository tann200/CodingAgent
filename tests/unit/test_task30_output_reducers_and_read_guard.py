"""TASK-30: Tests for content-type tool output reducers, ToolOutputStore, read_tool_output, and Context-Aware Read Guard."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.orchestration.graph.runtime_policy import (
    COMPACT_RUNTIME_POLICY,
    NANO_RUNTIME_POLICY,
)
from src.core.orchestration.tool_execution_pipeline import execute_tool_impl
from src.core.orchestration.tool_output_reducer import (
    ToolOutputStore,
    classify_tool_output,
    reduce_tool_output,
)
from src.tools._capabilities import resolve_to_capability
from src.tools._file_io import read_file
from src.tools._registry import build_registry
from src.tools.guardrails import reset_guardrail_state
from src.tools.tools_config import TOOL_PERMISSIONS, PermissionLevel


def test_tool_output_store_lru_eviction_and_pagination():
    store = ToolOutputStore(max_entries=3, max_total_bytes=4096, max_entry_bytes=2048)
    h1 = store.store("A" * 500, tool_name="bash", content_type="generic")
    h2 = store.store("B" * 500, tool_name="bash", content_type="generic")
    h3 = store.store("C" * 500, tool_name="bash", content_type="generic")
    assert (h1, h2, h3) == ("out_1", "out_2", "out_3")

    # Touch h1 so h2 becomes LRU
    r1 = store.read(h1, offset=0, limit=100)
    assert r1["status"] == "ok"
    assert r1["content"] == "A" * 100
    assert r1["has_more"] is True
    assert r1["total_chars"] == 500

    # Adding h4 should evict h2 (not h1)
    h4 = store.store("D" * 500, tool_name="bash", content_type="generic")
    assert h4 == "out_4"
    assert store.read(h1)["status"] == "ok"
    evicted = store.read(h2)
    assert evicted["status"] == "error"
    assert evicted["recoverable"] is True
    assert store.stats()["evicted_count"] == 1


def test_tool_output_store_byte_budget_and_secret_preredaction():
    store = ToolOutputStore(max_entries=32, max_total_bytes=2048, max_entry_bytes=1024)
    secret_payload = "prefix openai key sk-1234567890abcdef1234567890abcdef suffix " + ("x" * 900)
    h1 = store.store(secret_payload, tool_name="bash", content_type="generic")

    res = store.read(h1, offset=0, limit=2000)
    assert res["status"] == "ok"
    assert "sk-1234567890abcdef1234567890abcdef" not in res["content"]

    # Exceeding max_total_bytes evicts oldest
    store.store("Y" * 900, tool_name="bash")
    store.store("Z" * 900, tool_name="bash")
    assert store.stats()["total_bytes"] <= 2048
    assert store.read(h1)["status"] == "error"


def test_reducers_for_all_content_types():
    store = ToolOutputStore()

    # 1. Validation reducer preserves failures & summary, collapses passing noise
    passing = "\n".join(f"tests/test_a.py::test_ok_{i} PASSED" for i in range(400))
    val_text = (
        passing
        + "\n=== FAILURES ===\n"
        + "FAILED tests/test_math.py::test_div\n"
        + '  File "tests/test_math.py", line 14, in test_div\n'
        + "E   AssertionError: assert 1 == 2\n"
        + "=== short test summary info ===\n"
        + "1 failed, 400 passed in 1.23s\n"
    )
    red_val, h_val, ct_val = reduce_tool_output(
        val_text, tool_name="run_tests", max_chars=2000, store=store
    )
    assert ct_val == "validation"
    assert h_val == "out_1"
    assert "FAILED tests/test_math.py::test_div" in red_val
    assert "AssertionError" in red_val
    assert "1 failed, 400 passed" in red_val
    assert "passing/setup lines collapsed" in red_val
    assert 'read_tool_output(handle="out_1"' in red_val

    # 2. Search reducer caps hits per file and summarizes totals
    search_lines = [f"src/a.py:{i}: match line {i} " + ("z" * 40) for i in range(1, 51)]
    search_lines += [f"src/b.py:{i}: match line {i} " + ("w" * 40) for i in range(1, 51)]
    red_search, h_search, ct_search = reduce_tool_output(
        "\n".join(search_lines), tool_name="grep", max_chars=1500, store=store
    )
    assert ct_search == "search"
    assert h_search == "out_2"
    assert "src/a.py:1:" in red_search
    assert "src/a.py:5:" in red_search
    assert "src/a.py:6:" not in red_search
    assert "100 total matches across 2 files" in red_search

    # 3. Diff reducer preserves file headers and truncates giant hunks
    diff_text = (
        "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1,2 +1,200 @@\n"
        + "\n".join(f"+added line {i} " + ("q" * 30) for i in range(200))
    )
    red_diff, h_diff, ct_diff = reduce_tool_output(
        diff_text, tool_name="git_diff", max_chars=2000, store=store
    )
    assert ct_diff == "diff"
    assert h_diff == "out_3"
    assert "diff --git a/foo.py b/foo.py" in red_diff

    # 4. Git status/log reducer keeps branch & file summary
    git_text = "On branch main\nChanges not staged for commit:\n" + "\n".join(
        f"M  src/file_{i}.py | 12 +++---" for i in range(250)
    )
    red_git, h_git, ct_git = reduce_tool_output(
        git_text, tool_name="git_status", max_chars=1500, store=store
    )
    assert ct_git == "git"
    assert h_git == "out_4"
    assert "On branch main" in red_git

    # 5. JSON & generic reducers preserve head + tail
    json_text = '{"items": [' + ",".join(f'{{"id": {i}}}' for i in range(500)) + '], "tail_ok": true}'
    red_json, h_json, ct_json = reduce_tool_output(
        json_text, tool_name="bash", max_chars=1000, store=store
    )
    assert ct_json == "json"
    assert h_json == "out_5"
    assert '"tail_ok": true' in red_json

    # 6. Inline pass-through under max_chars does not allocate a handle
    small, h_small, ct_small = reduce_tool_output("short output", max_chars=1000, store=store)
    assert small == "short output"
    assert h_small is None
    assert ct_small == "inline"

    # Classification check for bash validation commands
    assert classify_tool_output("bash", {"command": "pytest -q"}, "plain text") == "validation"


def test_read_tool_output_registered_and_end_to_end_pipeline(tmp_path):
    reg = build_registry(working_dir=str(tmp_path))
    assert "read_tool_output" in reg.list()
    assert TOOL_PERMISSIONS["read_tool_output"] == PermissionLevel.READ_ONLY
    cap, op, _ = resolve_to_capability("read_tool_output", {"handle": "out_1"})
    assert (cap, op) == ("files_read", "read")

    # Present in all 3 flat toolsets
    from src.config.toolsets.loader import get_tools_for_toolset

    for ts in ("compact-flat", "standard-flat", "frontier-flat"):
        assert "read_tool_output" in get_tools_for_toolset(ts)

    # End-to-end: oversized tool output -> handle -> read_tool_output retrieves raw slice
    raw_lines = "".join(f"row-{i:04d}-payload-content\n" for i in range(500))  # ~12k chars
    reg.register("emit_big", lambda **kw: {"status": "ok", "output": raw_lines}, side_effects=[])

    orch = SimpleNamespace(
        tool_registry=reg,
        _dry_run=False,
        _runtime_policy=NANO_RUNTIME_POLICY,
        _normalize_tool_result=lambda r: r,
        _get_tool_timeout=lambda name: 10.0,
        event_bus=MagicMock(),
        session_store=None,
        active_role="operational",
        working_dir=tmp_path,
        _session_read_files=set(),
        _session_modified_files=set(),
        _usage_buffer={},
        cost_tracker=MagicMock(),
        _append_execution_trace=lambda e: None,
        _normalize_args=lambda a: a,
        _sync_session_state=lambda: None,
    )

    big_res = execute_tool_impl(orch, {"name": "emit_big", "arguments": {}})
    assert big_res["ok"] is True
    assert 'read_tool_output(handle="out_1"' in big_res["result"]["output"]

    page1 = execute_tool_impl(
        orch, {"name": "read_tool_output", "arguments": {"handle": "out_1", "offset": 0, "limit": 200}}
    )
    assert page1["ok"] is True
    assert page1["result"]["status"] == "ok"
    assert page1["result"]["content"].startswith("row-0000-payload-content\n")
    assert page1["result"]["has_more"] is True


def test_context_aware_read_guard_and_read_before_write_invariant(tmp_path):
    reset_guardrail_state()
    big_file = tmp_path / "big_module.py"
    lines = [f"# header line {i}\n" for i in range(1, 31)]
    lines += [f"def fn_{i}():\n    return {i}\n" for i in range(31, 600)]
    big_file.write_text("".join(lines), encoding="utf-8")

    reg = build_registry(working_dir=str(tmp_path))
    orch = SimpleNamespace(
        tool_registry=reg,
        _dry_run=False,
        _runtime_policy=COMPACT_RUNTIME_POLICY,
        _normalize_tool_result=lambda r: r,
        _get_tool_timeout=lambda name: 10.0,
        event_bus=MagicMock(),
        session_store=None,
        active_role="operational",
        working_dir=tmp_path,
        _session_read_files=set(),
        _session_modified_files=set(),
        _usage_buffer={},
        cost_tracker=MagicMock(),
        _append_execution_trace=lambda e: None,
        _normalize_args=lambda a: a,
        _sync_session_state=lambda: None,
    )

    # 1. Blind whole-file read triggers Context-Aware Read Guard on compact tier (>500 lines)
    res = execute_tool_impl(orch, {"name": "read_file", "arguments": {"path": "big_module.py"}})
    assert res["ok"] is True
    inner = res["result"]
    assert inner["status"] == "ok"
    assert inner["truncated_by_read_guard"] is True
    assert inner["total_lines"] > 500
    assert "# header line 1\n" in inner["content"]
    assert "# header line 30\n" in inner["content"]
    assert "def fn_50" not in inner["content"]
    assert "start_line" in inner["hint"]

    # 2. CRITICAL INVARIANT: read-guard preview must NOT satisfy read-before-write for blind write_file!
    assert str(big_file.resolve()) not in orch._session_read_files
    write_attempt = execute_tool_impl(
        orch,
        {"name": "write_file", "arguments": {"path": "big_module.py", "content": "# wiped\n"}},
    )
    assert write_attempt["ok"] is False
    assert "must read" in write_attempt["error"].lower()

    # 3. Surgical edit on text NOT seen in the 30-line preview is STILL blocked
    unseen_edit = execute_tool_impl(
        orch,
        {
            "name": "edit_file_atomic",
            "arguments": {
                "path": "big_module.py",
                "old_string": "def fn_100():\n    return 100",
                "new_string": "def fn_100():\n    return 999",
            },
        },
    )
    assert unseen_edit["ok"] is False
    assert "must read" in unseen_edit["error"].lower()

    # 4. Surgical edit strictly on lines ACTUALLY seen in the 30-line preview IS allowed
    seen_edit = execute_tool_impl(
        orch,
        {
            "name": "edit_file_atomic",
            "arguments": {
                "path": "big_module.py",
                "old_string": "# header line 1\n",
                "new_string": "# updated header line 1\n",
            },
        },
    )
    assert seen_edit["ok"] is True
    assert seen_edit["result"]["status"] == "ok"
    assert big_file.read_text(encoding="utf-8").startswith("# updated header line 1\n")

    # 5. Targeted line-range read satisfies read-before-write cleanly
    sliced = execute_tool_impl(
        orch,
        {
            "name": "read_file",
            "arguments": {"path": "big_module.py", "start_line": 31, "end_line": 40},
        },
    )
    assert sliced["ok"] is True
    assert sliced["result"]["start_line"] == 31
    assert sliced["result"]["end_line"] == 40
    assert "def fn_31" in sliced["result"]["content"]
    assert str(big_file.resolve()) in orch._session_read_files


def test_read_file_direct_line_slicing_without_orchestrator(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("L1\nL2\nL3\nL4\nL5\n", encoding="utf-8")
    res = read_file("sample.txt", start_line=2, end_line=4, workdir=tmp_path)
    assert res["status"] == "ok"
    assert res["content"] == "L2\nL3\nL4\n"
    assert res["start_line"] == 2
    assert res["end_line"] == 4
    assert res["total_lines"] == 5
