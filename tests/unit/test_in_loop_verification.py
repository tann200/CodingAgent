"""P3.1 — in-loop verification checkpoint in the frontier loop.

The production tier graphs execute tools only in ``frontier_loop_node``, so a
deterministic per-step check had no home: ``verification_node`` runs after the
loop exits.  These tests pin the contract of
``in_loop_verification`` (baseline diff, delete absence, alias normalization,
no-false-positives on pre-existing breakage) and the two integration points
that make it useful — the loop injecting feedback into history and the bounded
failure cap.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.orchestration.graph.nodes import frontier_loop_node as fln
from src.core.orchestration.graph.nodes.frontier_messages import (
    _extract_tool_call_from_text,
    _normalize_messages_for_native_tools,
)
from src.core.orchestration.graph.nodes.in_loop_verification import (
    INLINE_CHECK_EXTS,
    build_inline_verification_message,
    capture_lint_baseline,
    run_inline_check,
)

BROKEN = "def f(:\n    return 1\n"
VALID = "def f():\n    return 1\n"
#: A pre-existing syntax error the agent did *not* introduce.
ALREADY_BROKEN = "def g(:\n    pass\n"


# ---------------------------------------------------------------------------
# Frontmatter: the extracted message helpers must keep working + be re-exported
# ---------------------------------------------------------------------------


def test_extracted_helpers_are_reexported_from_the_node():
    """The legacy test surface imports these from frontier_loop_node."""
    assert fln._normalize_messages_for_native_tools is _normalize_messages_for_native_tools
    assert fln._extract_tool_call_from_text is _extract_tool_call_from_text
    for name in ("_normalize_messages_for_native_tools", "_extract_tool_call_from_text"):
        assert name in fln.__all__


def test_extraction_did_not_change_behavior():
    """Spot-check the moved code still behaves as it did in the node."""
    tc, rest = _extract_tool_call_from_text('thinking {"name": "read_file", "arguments": {"path": "a"}}')
    assert tc == {"name": "read_file", "arguments": {"path": "a"}}
    assert rest == "thinking"

    out = _normalize_messages_for_native_tools(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": '{"name": "bash", "arguments": {"command": "ls"}}'},
            {"role": "user", "content": json.dumps({"tool_execution_result": {"ok": True}})},
        ]
    )
    assert out[0] == {"role": "user", "content": "hi"}
    assert out[1]["role"] == "assistant" and out[1]["tool_calls"][0]["id"] == "call_1"
    assert out[2]["role"] == "tool" and out[2]["tool_call_id"] == "call_1"
    assert all("_call_id" not in m for m in out)


# ---------------------------------------------------------------------------
# Scope: which tools get a checkpoint
# ---------------------------------------------------------------------------


def test_read_only_tools_get_no_checkpoint(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text(BROKEN)
    assert capture_lint_baseline("read_file", {"path": "a.py"}, str(tmp_path)) is None
    assert (
        run_inline_check("read_file", {"path": "a.py"}, {"ok": True}, str(tmp_path)) is None
    )


def test_alias_cannot_evade_the_checkpoint(tmp_path: Path):
    """``write`` must be normalized before the MODIFYING_TOOLS test."""
    target = tmp_path / "a.py"
    target.write_text(VALID)
    baseline = capture_lint_baseline("write", {"path": "a.py"}, str(tmp_path))
    assert baseline is not None and "a.py" in baseline
    target.write_text(BROKEN)
    rec = run_inline_check("write", {"path": "a.py"}, {"ok": True}, str(tmp_path), baseline)
    assert rec is not None and rec["status"] == "failed"
    assert rec["tool"] == "write_file"


def test_type_checked_extensions_are_excluded(tmp_path: Path):
    """tsc/rustc are too slow and too noisy for a per-write checkpoint."""
    assert ".ts" not in INLINE_CHECK_EXTS
    assert ".rs" not in INLINE_CHECK_EXTS
    (tmp_path / "a.ts").write_text("const x: number = ;\n")
    assert capture_lint_baseline("write_file", {"path": "a.ts"}, str(tmp_path)) is None
    assert (
        run_inline_check(
            "write_file", {"path": "a.ts"}, {"ok": True}, str(tmp_path), {}
        )
        is None
    )


def test_tool_without_resolvable_path_gets_no_checkpoint(tmp_path: Path):
    """apply_patch on an unsupported extension, and path-less calls, are skipped."""
    assert capture_lint_baseline("manage_todo", {"todos": []}, str(tmp_path)) is None
    assert capture_lint_baseline("write_file", {}, str(tmp_path)) is None
    assert capture_lint_baseline("write_file", {"path": 123}, str(tmp_path)) is None
    assert (
        run_inline_check("write_file", {}, {"ok": True}, str(tmp_path), None) is None
    )


def test_unsupported_extension_inside_a_modifying_call_is_skipped(tmp_path: Path):
    assert capture_lint_baseline("edit_file", {"path": "notes.md"}, str(tmp_path)) is None


# ---------------------------------------------------------------------------
# Baseline diff: only newly introduced problems are reported
# ---------------------------------------------------------------------------


def test_new_syntax_error_is_caught(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text(VALID)
    baseline = capture_lint_baseline("write_file", {"path": "a.py"}, str(tmp_path))
    target.write_text(BROKEN)
    rec = run_inline_check("write_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), baseline)
    assert rec is not None
    assert rec["status"] == "failed"
    assert rec["failures"][0]["path"] == "a.py"
    assert rec["failures"][0]["errors"]


def test_preexisting_error_is_not_blamed_on_the_write(tmp_path: Path):
    """A file that was already broken before the write must still pass."""
    target = tmp_path / "a.py"
    target.write_text(ALREADY_BROKEN)
    baseline = capture_lint_baseline("write_file", {"path": "a.py"}, str(tmp_path))
    assert baseline and baseline["a.py"], "pre-existing error should be in the baseline"
    target.write_text(ALREADY_BROKEN + "x = 1\n")
    rec = run_inline_check("write_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), baseline)
    assert rec is not None
    assert rec["status"] == "passed"
    assert rec["failures"] == []


def test_clean_write_passes(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text(BROKEN)
    baseline = capture_lint_baseline("edit_file", {"path": "a.py"}, str(tmp_path))
    target.write_text(VALID)
    rec = run_inline_check("edit_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), baseline)
    assert rec is not None and rec["status"] == "passed"


def test_missing_baseline_is_treated_as_empty(tmp_path: Path):
    """A fresh file with no captured baseline still gets checked."""
    target = tmp_path / "a.py"
    target.write_text(BROKEN)
    rec = run_inline_check("write_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), None)
    assert rec is not None and rec["status"] == "failed"


def test_baseline_cache_makes_failures_sticky(tmp_path: Path):
    """A file the agent broke is re-reported until it is actually repaired.

    Per-call re-capture would go quiet on the second identical bad write
    (the error would have become "pre-existing"), so the model would never
    learn its first write was broken.
    """
    target = tmp_path / "a.py"
    target.write_text(VALID)
    cache: dict = {}
    first = capture_lint_baseline("write_file", {"path": "a.py"}, str(tmp_path), cache)
    target.write_text(BROKEN)
    assert run_inline_check("write_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), first)[
        "status"
    ] == "failed"

    # Same content rewritten: the cached baseline is reused, so it is still blamed.
    second = capture_lint_baseline("write_file", {"path": "a.py"}, str(tmp_path), cache)
    target.write_text(BROKEN)
    assert run_inline_check("write_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), second)[
        "status"
    ] == "failed"

    # Once repaired, the checkpoint goes quiet again.
    target.write_text(VALID)
    third = capture_lint_baseline("write_file", {"path": "a.py"}, str(tmp_path), cache)
    assert run_inline_check("write_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), third)[
        "status"
    ] == "passed"


def test_baseline_cache_keeps_preexisting_errors_unblamed_across_writes(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text(ALREADY_BROKEN)
    cache: dict = {}
    for _ in range(3):
        baseline = capture_lint_baseline("write_file", {"path": "a.py"}, str(tmp_path), cache)
        rec = run_inline_check("write_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), baseline)
        assert rec is not None and rec["status"] == "passed"


def test_absolute_path_argument_is_honoured(tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text(VALID)
    baseline = capture_lint_baseline("write_file", {"path": str(target)}, str(tmp_path))
    assert baseline is not None
    target.write_text(BROKEN)
    rec = run_inline_check("write_file", {"path": str(target)}, {"ok": True}, str(tmp_path), baseline)
    assert rec is not None and rec["status"] == "failed"


def test_rename_checks_the_destination(tmp_path: Path):
    old = tmp_path / "old.py"
    old.write_text(VALID)
    dest = tmp_path / "new.py"
    baseline = capture_lint_baseline(
        "rename_file", {"path": "old.py", "new_path": "new.py"}, str(tmp_path)
    )
    # The rename moves the file; the destination is what needs checking.
    old.rename(dest)
    dest.write_text(BROKEN)
    rec = run_inline_check(
        "rename_file", {"path": "old.py", "new_path": "new.py"}, {"ok": True}, str(tmp_path), baseline
    )
    # 'old.py' no longer exists after a successful rename -> skipped, not blamed.
    assert rec is not None and rec["checked"] == 1 and rec["status"] == "failed"
    assert rec["failures"][0]["path"] == "new.py"


# ---------------------------------------------------------------------------
# delete_file: absence assertion
# ---------------------------------------------------------------------------


def test_delete_file_that_removed_the_target_passes(tmp_path: Path):
    (tmp_path / "gone.py").write_text("x = 1\n")
    rec = run_inline_check(
        "delete_file", {"path": "gone.py"}, {"ok": True}, str(tmp_path), None
    )
    assert rec is not None and rec["status"] == "failed"  # file still there
    (tmp_path / "gone.py").unlink()
    rec = run_inline_check(
        "delete_file", {"path": "gone.py"}, {"ok": True}, str(tmp_path), None
    )
    assert rec is not None and rec["status"] == "passed"


def test_delete_failure_is_not_reported_as_a_verification_failure(tmp_path: Path):
    target = tmp_path / "keep.py"
    target.write_text("x = 1\n")
    assert (
        run_inline_check("delete_file", {"path": "keep.py"}, {"ok": False}, str(tmp_path)) is None
    )
    assert (
        run_inline_check("delete_file", {"path": "keep.py"}, {"status": "error"}, str(tmp_path))
        is None
    )


# ---------------------------------------------------------------------------
# Failure shapes that must not trigger the checkpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [None, {"ok": False}, {"success": False}, {"status": "error"}, {"result": {"ok": False}}],
)
def test_only_successful_tool_calls_are_checked(tmp_path: Path, result):
    target = tmp_path / "a.py"
    target.write_text(BROKEN)
    assert run_inline_check("write_file", {"path": "a.py"}, result, str(tmp_path)) is None


def test_empty_result_counts_as_success(tmp_path: Path):
    """``{}`` reports no error, so the write is still worth checking.

    Several tools return an empty mapping on success; treating it as a failure
    would silently disable the checkpoint for them.  The baseline diff keeps it
    harmless when nothing was actually written.
    """
    target = tmp_path / "a.py"
    target.write_text(VALID)
    baseline = capture_lint_baseline("write_file", {"path": "a.py"}, str(tmp_path))
    target.write_text(BROKEN)
    rec = run_inline_check("write_file", {"path": "a.py"}, {}, str(tmp_path), baseline)
    assert rec is not None and rec["status"] == "failed"


def test_linter_failure_never_breaks_the_loop(tmp_path: Path, monkeypatch):
    """A broken/unavailable linter degrades to 'nothing to report'."""
    def boom(*_a, **_kw):
        raise RuntimeError("linter exploded")

    import src.tools.lint_dispatch as ld

    monkeypatch.setattr(ld, "quick_lint", boom)
    assert capture_lint_baseline("write_file", {"path": "a.py"}, str(tmp_path)) == {"a.py": set()}
    assert run_inline_check("write_file", {"path": "a.py"}, {"ok": True}, str(tmp_path), None) is None


# ---------------------------------------------------------------------------
# Feedback message shape
# ---------------------------------------------------------------------------


def test_failed_record_renders_a_user_message():
    msg = build_inline_verification_message(
        {
            "tool": "write_file",
            "status": "failed",
            "checked": 1,
            "failures": [{"path": "a.py", "errors": ["line 1: invalid syntax [E999]"]}],
        }
    )
    assert msg["role"] == "user"
    assert "a.py" in msg["content"]
    assert "line 1: invalid syntax" in msg["content"]
    assert "write_file" in msg["content"]


def test_passing_or_missing_record_renders_nothing():
    assert build_inline_verification_message(None) == {}
    assert build_inline_verification_message({"status": "passed", "failures": []}) == {}


def test_feedback_survives_native_tool_normalization():
    """It must stay a plain user message, not a synthesised tool message.

    A ``role="tool"`` message without a matching ``tool_call_id`` is rejected
    by the OpenAI-compatible APIs, so the checkpoint uses a user turn.
    """
    msg = build_inline_verification_message(
        {
            "tool": "write_file",
            "status": "failed",
            "failures": [{"path": "a.py", "errors": ["boom"]}],
        }
    )
    out = _normalize_messages_for_native_tools([msg])
    assert out == [{"role": "user", "content": msg["content"]}]


# ---------------------------------------------------------------------------
# Integration: the frontier loop actually runs and uses the checkpoint
# ---------------------------------------------------------------------------


class _FakeOrchestrator:
    """Minimal orchestrator: ``execute_tool`` writes a broken Python file."""

    def __init__(self, workdir: Path) -> None:
        self.working_dir = workdir
        self.event_bus = None
        self.plan_mode = None

    def execute_tool(self, action: dict) -> dict:
        args = action.get("arguments") or {}
        (self.working_dir / args["path"]).write_text(BROKEN)
        return {"ok": True, "status": "ok"}


@pytest.fixture()
def no_index_refresh(monkeypatch):
    """Keep repo indexing out of these tests (it is covered elsewhere)."""
    monkeypatch.setattr(fln, "refresh_index_after_write", lambda *a, **k: None)
    monkeypatch.setattr(fln, "build_repo_context_block", lambda *a, **k: "")


@pytest.mark.asyncio
async def test_dispatch_injects_feedback_into_history(tmp_path: Path, no_index_refresh):
    hist, count, _res, tool, records = await fln._dispatch_tool_calls(
        tool_calls=[{"name": "write_file", "arguments": {"path": "new.py"}}],
        history=[],
        state={"working_dir": str(tmp_path)},
        orchestrator=_FakeOrchestrator(tmp_path),
        model_tier="small",
        bus=None,
        turns_taken=1,
        tool_call_count=0,
        max_tool_calls=10,
        iv_baselines={},
    )
    assert count == 1 and tool == "write_file"
    assert records and records[0]["status"] == "failed"
    injected = [m for m in hist if "[inline verification]" in str(m.get("content", ""))]
    assert len(injected) == 1
    assert injected[0]["role"] == "user"
    assert "new.py" in injected[0]["content"]


@pytest.mark.asyncio
async def test_dispatch_stays_quiet_for_read_only_calls(tmp_path: Path, no_index_refresh):
    class _Reader(_FakeOrchestrator):
        def execute_tool(self, action: dict) -> dict:
            return {"ok": True, "content": "hello"}

    hist, _c, _r, _t, records = await fln._dispatch_tool_calls(
        tool_calls=[{"name": "read_file", "arguments": {"path": "a.py"}}],
        history=[],
        state={"working_dir": str(tmp_path)},
        orchestrator=_Reader(tmp_path),
        model_tier="small",
        bus=None,
        turns_taken=1,
        tool_call_count=0,
        max_tool_calls=10,
        iv_baselines={},
    )
    assert records == []
    assert not [m for m in hist if "[inline verification]" in str(m.get("content", ""))]


@pytest.mark.asyncio
async def test_loop_exits_after_the_verification_failure_cap(
    tmp_path: Path, no_index_refresh, monkeypatch
):
    """Repeated unfixed writes must not spin for the whole turn budget."""
    cap = fln._MAX_INLINE_VERIFICATION_FAILURES
    assert cap == 3

    async def _fake_turn(*_a, **_kw):
        return (
            {},
            "",
            [{"name": "write_file", "arguments": {"path": "new.py"}}],
        )

    monkeypatch.setattr(fln, "_prepare_turn_messages", _no_history)
    monkeypatch.setattr(fln, "_call_llm_for_turn", _fake_turn)

    result = await fln.frontier_loop_node(
        {
            "working_dir": str(tmp_path),
            "task": "do it",
            "history": [],
            "orchestrator": _FakeOrchestrator(tmp_path),
        },
        None,
    )
    assert "inline_verification_failed" in result["errors"]
    assert result["last_result"]["ok"] is False
    assert len(result["inline_verification_failures"]) == cap
    # One attempt per turn, and the loop stopped at the cap.
    assert result["_frontier_loop_turns"] == cap
    injected = [
        m
        for m in result["history"]
        if "[inline verification]" in str(m.get("content", ""))
    ]
    assert len(injected) == cap


async def _no_history(**_kw):
    return [{"role": "user", "content": "go"}]


@pytest.mark.asyncio
async def test_cap_does_not_fire_for_successful_writes(tmp_path: Path, no_index_refresh, monkeypatch):
    class _GoodWriter(_FakeOrchestrator):
        def execute_tool(self, action: dict) -> dict:
            (self.working_dir / action["arguments"]["path"]).write_text(VALID)
            return {"ok": True}

    calls = {"n": 0}

    async def _fake_turn(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] > fln._MAX_INLINE_VERIFICATION_FAILURES:
            return {}, "all done", []
        return {}, "", [{"name": "write_file", "arguments": {"path": "new.py"}}]

    monkeypatch.setattr(fln, "_prepare_turn_messages", _no_history)
    monkeypatch.setattr(fln, "_call_llm_for_turn", _fake_turn)

    result = await fln.frontier_loop_node(
        {
            "working_dir": str(tmp_path),
            "task": "do it",
            "history": [],
            "orchestrator": _GoodWriter(tmp_path),
        },
        None,
    )
    assert "inline_verification_failed" not in result["errors"]
    assert "inline_verification_failures" not in result
