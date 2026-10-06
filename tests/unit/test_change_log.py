"""P3.2 — live repo-change audit trail.

Before this, the only record of what the agent touched on disk was
``orch._session_modified_files`` (in-memory, consumed only by the end-of-task git
diff) plus ``execution_trace.json`` (tool calls, flushed once per task, never
reconciled with the plan).  These tests pin the durable, attributed,
fingerprint-reconciled replacement.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.orchestration.change_log import (
    CREATED,
    DELETED,
    JOURNAL_NAME,
    MODIFIED,
    UNCHANGED,
    begin_change,
    fingerprint,
    journal_path,
    read_change_log,
    record_change,
    set_change_context,
    summarize,
)
from src.tools._path_utils import changed_paths


class _FakeOrchestrator:
    def __init__(self, workdir: Path, task_id: str = "task-1") -> None:
        self.working_dir = workdir
        self._current_task_id = task_id
        self.event_bus = None
        self.plan_mode = None


class _RecordingBus:
    """Minimal typed-bus stand-in that captures published events."""

    def __init__(self) -> None:
        self.events: list = []

    def publish_typed(self, event) -> None:
        self.events.append(event)


@pytest.fixture()
def orch(tmp_path: Path) -> _FakeOrchestrator:
    o = _FakeOrchestrator(tmp_path)
    set_change_context(
        o,
        {
            "task": "add a retry helper",
            "current_step": 1,
            "current_plan": [
                {"description": "read the module"},
                {"description": "add retry() to util.py"},
            ],
            "affected_files": ["util.py"],
        },
    )
    return o


# ---------------------------------------------------------------------------
# changed_paths: one source of truth for "what did this tool touch"
# ---------------------------------------------------------------------------


def test_changed_paths_handles_aliases_and_rename():
    assert changed_paths("write", {"path": "a.py"}) == ["a.py"]
    assert changed_paths("write_file", {"file_path": "a.py"}) == ["a.py"]
    both = changed_paths("rename_file", {"path": "old.py", "new_path": "new.py"})
    assert both == ["old.py", "new.py"]
    assert changed_paths("read_file", {}) == []
    assert changed_paths("write_file", "not-a-dict") == []
    assert changed_paths("write_file", {"path": 5}) == []


def test_repo_context_shares_the_helper():
    """The index refresh and the audit trail must not diverge on a rename."""
    from src.core.orchestration.graph.nodes.repo_context import _candidate_paths

    args = {"path": "old.py", "new_path": "new.py"}
    assert _candidate_paths("rename_file", args) == changed_paths("rename_file", args)


# ---------------------------------------------------------------------------
# Attribution context
# ---------------------------------------------------------------------------


def test_set_change_context_captures_step_and_plan(orch: _FakeOrchestrator):
    ctx = orch._change_context
    assert ctx["task_id"] == "task-1"
    assert ctx["step"] == 1
    assert ctx["step_total"] == 2
    assert "2/2" in ctx["reason"] and "add retry()" in ctx["reason"]
    assert ctx["planned_files"] == ["util.py"]


def test_set_change_context_falls_back_to_the_task(tmp_path: Path):
    o = _FakeOrchestrator(tmp_path, task_id="t")
    set_change_context(o, {"task": "just do it"})
    assert o._change_context["reason"] == "just do it"


def test_set_change_context_survives_a_broken_state(tmp_path: Path):
    o = _FakeOrchestrator(tmp_path)
    set_change_context(o, {"current_plan": "not-a-list", "current_step": "nope"})
    assert o._change_context["reason"]


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def test_fingerprint_distinguishes_content_size_and_absence(tmp_path: Path):
    target = tmp_path / "a.py"
    assert fingerprint(target) == (False, 0, "")
    target.write_text("x = 1\n")
    exists, size, digest = fingerprint(target)
    assert exists and size == 6 and digest
    assert fingerprint(target)[2] == digest
    target.write_text("x = 22\n")
    assert fingerprint(target)[2] != digest


def test_fingerprint_of_a_directory_is_marked(tmp_path: Path):
    exists, size, digest = fingerprint(tmp_path)
    assert exists and size == 0 and digest == "dir"


def test_fingerprint_of_a_missing_path_never_raises(tmp_path: Path):
    assert fingerprint(tmp_path / "nope" / "deeper.py")[0] is False


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def test_created_file_is_recorded_with_attribution(orch: _FakeOrchestrator, tmp_path: Path):
    begin_change(orch, "write_file", {"path": "util.py"}, tmp_path)
    (tmp_path / "util.py").write_text("def retry():\n    pass\n")

    entry = record_change(orch, "write_file", {"path": "util.py"}, tmp_path)
    assert entry is not None
    assert entry["change"] == CREATED
    assert entry["path"] == "util.py"
    assert entry["tool"] == "write_file"
    assert entry["task_id"] == "task-1"
    assert "add retry()" in entry["reason"]
    assert entry["step"] == 1
    assert entry["in_plan"] is True
    assert entry["outside_workdir"] is False
    assert entry["bytes"] == len("def retry():\n    pass\n")


def test_modified_file_is_distinguished_from_created(orch: _FakeOrchestrator, tmp_path: Path):
    target = tmp_path / "util.py"
    target.write_text("x = 1\n")
    begin_change(orch, "edit_file", {"path": "util.py"}, tmp_path)
    target.write_text("x = 2\n")
    entry = record_change(orch, "edit_file", {"path": "util.py"}, tmp_path)
    assert entry is not None and entry["change"] == MODIFIED


def test_deleted_file_is_recorded(orch: _FakeOrchestrator, tmp_path: Path):
    target = tmp_path / "old.py"
    target.write_text("x = 1\n")
    begin_change(orch, "delete_file", {"path": "old.py"}, tmp_path)
    target.unlink()
    entry = record_change(orch, "delete_file", {"path": "old.py"}, tmp_path)
    assert entry is not None and entry["change"] == DELETED
    assert entry["bytes"] == 0
    assert entry["digest_after"] == ""


def test_unchanged_file_leaves_no_phantom_record(orch: _FakeOrchestrator, tmp_path: Path):
    """A tool that reported success but changed nothing must not be recorded."""
    (tmp_path / "same.py").write_text("x = 1\n")
    begin_change(orch, "write_file", {"path": "same.py"}, tmp_path)
    assert record_change(orch, "write_file", {"path": "same.py"}, tmp_path) is None
    assert read_change_log(tmp_path) == []


def test_failed_write_that_still_touched_disk_is_recorded(
    orch: _FakeOrchestrator, tmp_path: Path
):
    """The reconciliation is against the filesystem, not the tool's report."""
    target = tmp_path / "partial.py"
    target.write_text("x = 1\n")
    begin_change(orch, "write_file", {"path": "partial.py"}, tmp_path)
    target.write_text("x = 1\ny = 2\n")
    entry = record_change(orch, "write_file", {"path": "partial.py"}, tmp_path)
    assert entry is not None and entry["change"] == MODIFIED


def test_record_without_a_baseline_is_not_credited(
    orch: _FakeOrchestrator, tmp_path: Path
):
    """No "before" fingerprint means we cannot tell creation from pre-existing.

    Guessing would put a phantom creation in the audit record, so the trail
    stays silent rather than wrong.
    """
    (tmp_path / "fresh.py").write_text("x = 1\n")
    assert record_change(orch, "write_file", {"path": "fresh.py"}, tmp_path) is None
    assert read_change_log(tmp_path) == []


def test_read_calls_cost_no_fingerprinting(orch: _FakeOrchestrator, tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    begin_change(orch, "read_file", {"path": "a.py"}, tmp_path)
    assert getattr(orch, "_change_pending", {}) == {}


def test_rename_records_both_ends(orch: _FakeOrchestrator, tmp_path: Path):
    old = tmp_path / "old.py"
    old.write_text("x = 1\n")
    args = {"path": "old.py", "new_path": "new.py"}
    begin_change(orch, "rename_file", args, tmp_path)
    old.rename(tmp_path / "new.py")

    record_change(orch, "rename_file", args, tmp_path)
    entries = read_change_log(tmp_path)
    kinds = {(e["path"], e["change"]) for e in entries}
    assert kinds == {("old.py", DELETED), ("new.py", CREATED)}


def test_path_outside_the_workdir_is_flagged(orch: _FakeOrchestrator, tmp_path: Path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}.py"
    outside.write_text("x = 1\n")
    try:
        begin_change(orch, "write_file", {"path": str(outside)}, tmp_path)
        outside.write_text("x = 2\n")
        entry = record_change(orch, "write_file", {"path": str(outside)}, tmp_path)
        assert entry is not None
        assert entry["outside_workdir"] is True
        assert entry["path"] == str(outside.resolve())
    finally:
        outside.unlink()


def test_read_only_tools_are_never_recorded(orch: _FakeOrchestrator, tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert record_change(orch, "read_file", {"path": "a.py"}, tmp_path) is None
    assert record_change(orch, "grep", {"pattern": "x"}, tmp_path) is None


def test_unresolvable_workdir_is_a_no_op(orch: _FakeOrchestrator):
    assert record_change(orch, "write_file", {"path": "a.py"}, None) is None
    assert begin_change(orch, "write_file", {"path": "a.py"}, None) is None


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


def test_journal_is_jsonl_under_the_agent_context_dir(orch: _FakeOrchestrator, tmp_path: Path):
    begin_change(orch, "write_file", {"path": "a.py"}, tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    record_change(orch, "write_file", {"path": "a.py"}, tmp_path)

    target = tmp_path / ".codingAgent" / JOURNAL_NAME
    assert target.exists()
    lines = [ln for ln in target.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["path"] == "a.py"


def test_read_change_log_is_oldest_first_and_limitable(
    orch: _FakeOrchestrator, tmp_path: Path
):
    for i in range(3):
        begin_change(orch, "write_file", {"path": f"f{i}.py"}, tmp_path)
        (tmp_path / f"f{i}.py").write_text(f"x = {i}\n")
        record_change(orch, "write_file", {"path": f"f{i}.py"}, tmp_path)
    entries = read_change_log(tmp_path)
    assert [e["path"] for e in entries] == ["f0.py", "f1.py", "f2.py"]
    assert [e["path"] for e in read_change_log(tmp_path, limit=1)] == ["f2.py"]
    assert read_change_log(tmp_path, limit=0) == []


def test_corrupt_journal_yields_empty_list(orch: _FakeOrchestrator, tmp_path: Path):
    target = tmp_path / ".codingAgent" / JOURNAL_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("not json\n{}\n\n", encoding="utf-8")
    assert read_change_log(tmp_path) == [{}]


def test_missing_journal_yields_empty_list(tmp_path: Path):
    assert read_change_log(tmp_path) == []
    assert read_change_log(None) == []
    assert journal_path(None) is None


# ---------------------------------------------------------------------------
# Live event
# ---------------------------------------------------------------------------


def test_record_publishes_a_typed_event(orch: _FakeOrchestrator, tmp_path: Path):
    from src.core.messaging.event_types import RepoChangeRecorded

    bus = _RecordingBus()
    orch.event_bus = bus
    begin_change(orch, "write_file", {"path": "util.py"}, tmp_path)
    (tmp_path / "util.py").write_text("x = 1\n")
    record_change(orch, "write_file", {"path": "util.py"}, tmp_path)

    assert len(bus.events) == 1
    event = bus.events[0]
    assert isinstance(event, RepoChangeRecorded)
    assert event.path == "util.py"
    assert event.change == CREATED
    assert event.tool == "write_file"
    assert "add retry()" in event.reason
    assert event.bytes_after == 6


def test_event_bus_name_resolves_to_the_typed_event():
    """The string-addressed EventBus must resolve the new name (Phase 5 map)."""
    from src.core.messaging.event_types import RepoChangeRecorded
    from src.core.orchestration.event_bus import EventBus

    bus = EventBus()
    bus.subscribe("repo.change.recorded", lambda payload: None)
    bus.publish("repo.change.recorded", {"path": "a.py", "tool": "write_file"})
    assert RepoChangeRecorded.__name__ == "RepoChangeRecorded"


def test_publish_failure_never_breaks_recording(orch: _FakeOrchestrator, tmp_path: Path):
    class _Boom:
        def publish_typed(self, _event):
            raise RuntimeError("bus down")

    orch.event_bus = _Boom()
    begin_change(orch, "write_file", {"path": "a.py"}, tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n")
    assert record_change(orch, "write_file", {"path": "a.py"}, tmp_path) is not None
    assert len(read_change_log(tmp_path)) == 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_summarize_answers_what_and_why(orch: _FakeOrchestrator, tmp_path: Path):
    begin_change(orch, "write_file", {"path": "util.py"}, tmp_path)
    (tmp_path / "util.py").write_text("x = 1\n")
    record_change(orch, "write_file", {"path": "util.py"}, tmp_path)

    text = summarize(read_change_log(tmp_path))
    assert "util.py" in text
    assert "created" in text
    assert "add retry()" in text


def test_summarize_reports_the_latest_write_per_file(orch: _FakeOrchestrator, tmp_path: Path):
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")
    for content in ("x = 2\n", "x = 3\n"):
        begin_change(orch, "edit_file", {"path": "a.py"}, tmp_path)
        target.write_text(content)
        record_change(orch, "edit_file", {"path": "a.py"}, tmp_path)
    text = summarize(read_change_log(tmp_path))
    assert text.count("a.py") == 1


def test_summarize_of_an_empty_trail_is_empty():
    assert summarize([]) == ""
    assert summarize([{}, {"path": ""}]) == ""


# ---------------------------------------------------------------------------
# Pipeline wiring: the hooks must actually fire on the production choke point
# ---------------------------------------------------------------------------


def _pipeline_orch(tmp_path: Path):
    """A real ``Orchestrator`` shell, wired like the pipeline tests do."""
    from unittest.mock import MagicMock

    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.working_dir = tmp_path
    orch._session_read_files = set()
    orch._session_modified_files = set()
    orch._usage_buffer = {}
    orch._step_snapshot_id = None
    orch._current_snapshot_id = None
    orch.rollback_manager = MagicMock()
    orch.event_bus = None
    orch.plan_mode = None
    orch._plan_mode_approved = None
    orch.current_role = None
    orch._tool_executor = None
    orch.explore_mode = False
    orch.cost_tracker = MagicMock()
    orch.session_store = None
    orch._dry_run = False
    return orch


def _run_write(orch, tmp_path: Path, name: str, args: dict) -> None:
    """Drive ``execute_tool_impl`` with a tool that writes to disk."""
    from unittest.mock import MagicMock, patch

    from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

    def _fake_fn(**_kwargs):
        (tmp_path / args["path"]).write_text("x = 1\n")
        return {"ok": True}

    fn = MagicMock(side_effect=_fake_fn)
    orch.tool_registry = MagicMock()
    orch.tool_registry.get = MagicMock(
        return_value={"fn": fn, "side_effects": ["write"], "description": "w"}
    )
    orch._normalize_tool_result = lambda r: r if isinstance(r, dict) else {"result": r}
    orch._get_tool_timeout = MagicMock(return_value=0)
    orch._normalize_args = MagicMock(return_value={})
    orch._append_execution_trace = MagicMock()
    orch._sync_session_state = MagicMock()

    with (
        patch(
            "src.core.orchestration.tool_execution_pipeline.WRITE_TOOLS_REQUIRING_READ",
            set(),
        ),
        patch(
            "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
            set(),
        ),
        patch(
            "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
            return_value=None,
        ),
    ):
        res = execute_tool_impl(orch, {"name": name, "arguments": args})
    assert res.get("ok") is True, res


def test_pipeline_write_lands_in_the_journal(tmp_path: Path):
    orch = _pipeline_orch(tmp_path)
    set_change_context(orch, {"task": "seed util", "current_step": 0, "current_plan": [{"description": "seed util"}]})
    _run_write(orch, tmp_path, "write_file", {"path": "seeded.py"})

    entries = read_change_log(tmp_path)
    assert [e["path"] for e in entries] == ["seeded.py"]
    assert entries[0]["change"] == CREATED
    assert entries[0]["tool"] == "write_file"
    assert "seed util" in entries[0]["reason"]


def test_pipeline_alias_is_normalized_before_the_trail(tmp_path: Path):
    """``write`` -> ``write_file`` must not escape the trail."""
    orch = _pipeline_orch(tmp_path)
    _run_write(orch, tmp_path, "write", {"path": "aliased.py"})
    entries = read_change_log(tmp_path)
    assert [e["path"] for e in entries] == ["aliased.py"]


def test_pipeline_read_call_writes_nothing_to_the_trail(tmp_path: Path):
    from unittest.mock import MagicMock, patch

    from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

    (tmp_path / "a.py").write_text("x = 1\n")
    orch = _pipeline_orch(tmp_path)
    fn = MagicMock(return_value={"ok": True, "content": "x = 1"})
    orch.tool_registry = MagicMock()
    orch.tool_registry.get = MagicMock(
        return_value={"fn": fn, "side_effects": [], "description": "read"}
    )
    orch._normalize_tool_result = lambda r: r if isinstance(r, dict) else {"result": r}
    orch._get_tool_timeout = MagicMock(return_value=0)
    orch._normalize_args = MagicMock(return_value={})
    orch._append_execution_trace = MagicMock()
    orch._sync_session_state = MagicMock()

    with patch(
        "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
        return_value=None,
    ):
        execute_tool_impl(orch, {"name": "read_file", "arguments": {"path": "a.py"}})

    assert read_change_log(tmp_path) == []
