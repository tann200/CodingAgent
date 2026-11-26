"""TASK-34 tests: validation authority (pipefail + red->green) and deterministic 9B loop guards."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.core.context.workspace_bootstrap import build_workspace_bootstrap_summary
from src.core.orchestration.graph.completion_contract import evaluate_completion_contract
from src.core.orchestration.graph.nodes.frontier_dispatch import execute_raw_tool
from src.core.orchestration.graph.validation_authority import (
    ValidationDebtTracker,
    classify_shell_validation,
    normalize_validation_command,
    wrap_with_pipefail,
)
from src.core.orchestration.loop_guards import (
    ReadDedupCache,
    augment_turn_messages,
    cap_consecutive_invalid_turns,
    compute_retry_temperature,
    format_unknown_tool_error,
    render_active_plan_anchor,
    suggest_closest_tool,
)


def test_wrap_with_pipefail_and_real_pipe_failure_propagation() -> None:
    wrapped = wrap_with_pipefail("pytest -q | head -n 20", shell="bash")
    assert wrapped == "set -o pipefail; pytest -q | head -n 20"
    # Idempotent
    assert wrap_with_pipefail(wrapped, shell="bash") == wrapped
    # Safe passthrough for dash / sh
    assert wrap_with_pipefail("pytest -q | head -n 20", shell="sh") == "pytest -q | head -n 20"
    assert wrap_with_pipefail("pytest -q | head -n 20", shell="/bin/dash") == "pytest -q | head -n 20"

    # Real bash proof: upstream failure propagates with pipefail
    if Path("/bin/bash").is_file():
        without_pf = subprocess.run(["/bin/bash", "-c", "false | cat"], capture_output=True)
        with_pf = subprocess.run(["/bin/bash", "-c", wrap_with_pipefail("false | cat", "bash")], capture_output=True)
        assert without_pf.returncode == 0
        assert with_pf.returncode != 0


def test_classify_shell_validation_operator_demotion_and_normalization() -> None:
    # Unquoted ;, ||, & demote authority to unconfirmed
    for cmd, expected_op in [
        ("pytest -q; true", ";"),
        ("pytest -q || true", "||"),
        ("pytest -q &", "&"),
    ]:
        info = classify_shell_validation(cmd)
        assert info["authority"] == "unconfirmed"
        assert expected_op in info["demoting_operators"]

    # && chain stays confirmed and extracts normalized stages
    chained = classify_shell_validation("ruff check src 2>&1 && pytest tests/unit -q 2>/dev/null")
    assert chained["authority"] == "confirmed"
    assert chained["stages"] == ["ruff check src", "pytest tests/unit -q"]

    # Quoted operators do NOT demote authority
    quoted = classify_shell_validation("pytest -k 'foo;bar || baz & qux' 2>&1")
    assert quoted["authority"] == "confirmed"
    assert quoted["stages"] == ["pytest -k 'foo;bar || baz & qux'"]

    # Normalization strips trailing bare redirections
    assert normalize_validation_command("pytest -q 2>&1") == "pytest -q"
    assert normalize_validation_command("set -o pipefail; pytest -q 2>/dev/null") == "pytest -q"


def test_validation_debt_tracker_red_to_green_and_completion_contract() -> None:
    tracker = ValidationDebtTracker()
    # Failing test before first mutation creates red debt
    tracker.record_command("pytest tests/test_math.py 2>&1", passed=False)
    assert tracker.unpaid_debt() == ["pytest tests/test_math.py"]

    # Mutate file
    tracker.record_mutation()

    # Masked pass via || true is unconfirmed -> debt stays unpaid!
    tracker.record_command("pytest tests/test_math.py || true", passed=True)
    assert tracker.unpaid_debt() == ["pytest tests/test_math.py"]

    # CompletionContract blocks completion while debt is unpaid
    verdict_blocked = evaluate_completion_contract(
        task="Fix add() in math_utils.py",
        content_text="Fixed.",
        tool_calls_count=2,
        modified_files=["math_utils.py"],
        unpaid_validation_debt=tracker.unpaid_debt(),
    )
    assert verdict_blocked.is_complete is False
    assert verdict_blocked.error_code == "unpaid_red_green_validation_debt"

    # Confirmed green pass after mutation clears debt
    tracker.record_command("pytest tests/test_math.py", passed=True)
    assert tracker.unpaid_debt() == []

    verdict_ok = evaluate_completion_contract(
        task="Fix add() in math_utils.py",
        content_text="Fixed and verified.",
        tool_calls_count=3,
        modified_files=["math_utils.py"],
        unpaid_validation_debt=tracker.unpaid_debt(),
    )
    assert verdict_ok.is_complete is True


def test_workspace_bootstrap_summary_polyglot_and_compact(tmp_path: Path) -> None:
    # Python repo with .venv
    py_repo = tmp_path / "py_repo"
    (py_repo / ".venv" / "bin").mkdir(parents=True)
    (py_repo / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (py_repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-q"\n[tool.ruff]\n', encoding="utf-8")
    py_summary = build_workspace_bootstrap_summary(py_repo)
    assert "<workspace_bootstrap>" in py_summary
    assert ".venv/bin/python -m pytest -q" in py_summary
    assert "pytest" in py_summary and "ruff" in py_summary
    body_lines = [ln for ln in py_summary.splitlines() if not ln.startswith("<")]
    assert len(body_lines) <= 5

    # JS/TS repo with pnpm + vitest
    js_repo = tmp_path / "js_repo"
    js_repo.mkdir()
    (js_repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (js_repo / "package.json").write_text(
        json.dumps({"devDependencies": {"typescript": "^5", "vitest": "^2"}, "scripts": {"test": "vitest"}}),
        encoding="utf-8",
    )
    js_summary = build_workspace_bootstrap_summary(js_repo)
    assert "TypeScript (pnpm)" in js_summary
    assert "vitest" in js_summary
    assert "pnpm test" in js_summary

    # Rust repo
    rs_repo = tmp_path / "rs_repo"
    rs_repo.mkdir()
    (rs_repo / "Cargo.toml").write_text('[package]\nname = "demo"\n', encoding="utf-8")
    assert "cargo test" in build_workspace_bootstrap_summary(rs_repo)

    # Go repo
    go_repo = tmp_path / "go_repo"
    go_repo.mkdir()
    (go_repo / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    assert "go test ./..." in build_workspace_bootstrap_summary(go_repo)


def test_read_dedup_cache_window_and_invalidation_on_edit_and_bash() -> None:
    cache = ReadDedupCache(max_entries=6)
    cache.begin_turn(0)

    cache.put("read_file", {"path": "a.py"}, {"ok": True, "content": "v1"})
    assert cache.get("read_file", {"path": "a.py"}) == {"ok": True, "content": "v1"}

    # Window size 6 evicts oldest entry on 7th unique pure read
    for i in range(6):
        cache.put("read_file", {"path": f"f{i}.py"}, {"ok": True, "content": str(i)})
    assert cache.get("read_file", {"path": "a.py"}) is None
    assert cache.get("read_file", {"path": "f5.py"}) == {"ok": True, "content": "5"}

    # Mutating tool invalidates entire cache
    cache.put("edit_file_atomic", {"path": "f5.py"}, {"ok": True})
    assert cache.get("read_file", {"path": "f5.py"}) is None

    # Same-turn manage_todo(read) dedup clears on next turn and on bash
    cache.put("manage_todo", {"action": "read"}, {"ok": True, "items": []})
    assert cache.get("manage_todo", {"action": "read"}) is not None
    cache.begin_turn(1)
    assert cache.get("manage_todo", {"action": "read"}) is None

    cache.put("grep", {"pattern": "foo"}, {"ok": True, "matches": []})
    cache.put("bash", {"command": "touch x"}, {"ok": True})
    assert cache.get("grep", {"pattern": "foo"}) is None


def test_execute_raw_tool_uses_read_dedup_and_invalidates_on_write() -> None:
    calls: list[dict] = []

    def _exec(action: dict) -> dict:
        calls.append(action)
        return {"ok": True, "status": "ok", "content": f"call_{len(calls)}"}

    orch = SimpleNamespace(execute_tool=_exec)
    r1 = asyncio.run(execute_raw_tool(orch, {"name": "read_file", "arguments": {"path": "x.py"}}, "small", False))
    r2 = asyncio.run(execute_raw_tool(orch, {"name": "read_file", "arguments": {"path": "x.py"}}, "small", False))
    assert len(calls) == 1
    assert r1["content"] == "call_1"
    assert r2.get("dedup_cache_hit") is True

    # Write invalidates cache -> next read hits execute_tool again
    asyncio.run(execute_raw_tool(orch, {"name": "write_file", "arguments": {"path": "x.py", "content": "new"}}, "small", False))
    r3 = asyncio.run(execute_raw_tool(orch, {"name": "read_file", "arguments": {"path": "x.py"}}, "small", False))
    assert len(calls) == 3
    assert r3["content"] == "call_3"


def test_hallucinated_tool_hint_plan_anchor_and_retry_temperature(tmp_path: Path) -> None:
    hint = suggest_closest_tool("read_files", ["read_file", "write_file", "edit_file_atomic", "grep"])
    assert "read_file" in hint

    fake_reg = MagicMock()
    fake_reg.list.return_value = ["read_file", "edit_file_atomic", "run_tests"]
    err_msg = format_unknown_tool_error("edit_atomic", SimpleNamespace(tool_registry=fake_reg))
    assert "Tool 'edit_atomic' not found." in err_msg
    assert "edit_file_atomic" in err_msg

    # Active plan anchor: < 2 steps returns empty, >= 2 steps returns 4-6 lines
    assert render_active_plan_anchor({"current_plan": [{"description": "Only one"}]}) == ""
    anchor = render_active_plan_anchor(
        {
            "current_step": 1,
            "current_plan": [
                {"description": "Inspect auth.py", "completed": True},
                {"description": "Fix token expiry bug", "completed": False},
                {"description": "Run pytest", "completed": False},
            ],
        }
    )
    assert "ACTIVE PLAN (step 2 of 3):" in anchor
    assert "[x] Inspect auth.py" in anchor
    assert "[>] Fix token expiry bug" in anchor
    assert 3 <= len(anchor.splitlines()) <= 6

    # augment_turn_messages injects bootstrap ONLY on turn 0
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n', encoding="utf-8")
    base_msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Fix bug"}]
    t0 = augment_turn_messages(base_msgs, {"working_dir": str(tmp_path)}, turns_taken=0)
    t1 = augment_turn_messages(base_msgs, {"working_dir": str(tmp_path)}, turns_taken=1)
    assert "<workspace_bootstrap>" in t0[-1]["content"]
    assert "<workspace_bootstrap>" not in t1[-1]["content"]

    # Adaptive retry temperature deltas (-0.15, +0.15, 0.0) clamped to [0, 1]
    assert compute_retry_temperature(0.2, 1) == 0.05
    assert compute_retry_temperature(0.2, 2) == 0.35
    assert compute_retry_temperature(0.2, 3) == 0.2
    assert compute_retry_temperature(0.05, 1) == 0.0
    assert compute_retry_temperature(0.95, 2) == 1.0

    assert cap_consecutive_invalid_turns(1) is False
    assert cap_consecutive_invalid_turns(2) is True
