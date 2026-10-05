"""tests/unit/test_frontier_completion.py — P3.3 plan-progress records.

The frontier loop runs a whole plan as one LLM+tool loop, so it never advanced
``current_step`` and never flagged a step completed. That made
``evaluation_node``'s ``plan_completed`` check unsatisfiable on the production
tier graphs: a successful run with an N-step plan was reported as
"PARTIAL COMPLETION" / ``evaluation_result="replan"``, and the plan could not be
persisted as finished, so the next turn silently re-ran completed work.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.core.orchestration.graph.nodes.frontier_completion import (
    build_exit_result,
    persist_plan_progress,
    plan_completion_patch,
)

# ---------------------------------------------------------------------------
# build_exit_result — extracted verbatim from frontier_loop_node
# ---------------------------------------------------------------------------


def _exit(**over):
    kw = dict(
        history=[{"role": "user", "content": "go"}],
        tool_call_count=2,
        last_result={"ok": True},
        last_tool_name="write_file",
        errors=[],
        turns_taken=3,
        inline_verifications=[],
        replan_required=None,
    )
    kw.update(over)
    return build_exit_result(**kw)


class TestBuildExitResult:
    def test_base_shape(self):
        out = _exit()
        assert out["tool_call_count"] == 2
        assert out["last_tool_name"] == "write_file"
        assert out["_frontier_loop_turns"] == 3
        assert out["awaiting_plan_approval"] is False

    def test_inline_failures_are_surfaced(self):
        out = _exit(inline_verifications=[{"status": "passed"}, {"status": "failed"}])
        assert out["inline_verification_failures"] == [{"status": "failed"}]

    def test_passing_verifications_are_not_surfaced(self):
        assert "inline_verification_failures" not in _exit(
            inline_verifications=[{"status": "passed"}]
        )

    def test_replan_marks_action_failed(self):
        out = _exit(replan_required="patch_too_large")
        assert out["replan_required"] == "patch_too_large"
        assert out["action_failed"] is True
        assert out["next_action"] is None

    def test_context_overflow_forces_a_failure_result(self):
        out = _exit(errors=["context_overflow"], last_result={"ok": True})
        assert out["_budget_compaction"] is True
        assert out["_should_distill"] is True
        assert out["last_result"]["ok"] is False
        assert "overflow" in out["last_result"]["error"]

    def test_context_overflow_keeps_an_existing_failure(self):
        existing = {"ok": False, "error": "tool exploded"}
        out = _exit(errors=["context_overflow"], last_result=existing)
        assert out["last_result"] is existing


# ---------------------------------------------------------------------------
# plan_completion_patch — the only truthful completion claim
# ---------------------------------------------------------------------------


class TestPlanCompletionPatch:
    def test_completion_is_recorded_when_the_loop_declares_done(self):
        state = {"current_plan": [{"description": "a"}, {"description": "b"}], "errors": []}
        result = {"ok": True, "_completion_detected": True}
        assert plan_completion_patch(state, result) == {"plan_completed": True}

    def test_no_plan_means_nothing_to_record(self):
        assert plan_completion_patch({"current_plan": []}, {"_completion_detected": True}) == {}
        assert plan_completion_patch({}, {"_completion_detected": True}) == {}

    def test_a_missing_detection_flag_is_not_completion(self):
        """Errors are not the signal: a cut-short run has none either."""
        state = {"current_plan": [{"description": "a"}], "errors": []}
        assert plan_completion_patch(state, {"ok": True}) == {}
        assert plan_completion_patch(state, None) == {}
        assert plan_completion_patch(state, {"_completion_detected": False}) == {}

    @pytest.mark.parametrize(
        "error",
        ["context_overflow", "inline_verification_failed", "canceled"],
    )
    def test_blocking_errors_block_completion(self, error):
        state = {"current_plan": [{"description": "a"}], "errors": [error]}
        result = {"ok": True, "_completion_detected": True}
        assert plan_completion_patch(state, result) == {}

    def test_pending_replan_is_not_completion(self):
        state = {"current_plan": [{"description": "a"}], "errors": []}
        result = {"ok": True, "_completion_detected": True, "replan_required": "split"}
        assert plan_completion_patch(state, result) == {}

    def test_suspended_approval_is_not_completion(self):
        state = {"current_plan": [{"description": "a"}], "errors": []}
        result = {"ok": True, "_completion_detected": True, "awaiting_plan_approval": True}
        assert plan_completion_patch(state, result) == {}

    def test_a_non_list_plan_is_not_completed(self):
        assert plan_completion_patch({"current_plan": "oops"}, {"_completion_detected": True}) == {}


# ---------------------------------------------------------------------------
# persist_plan_progress
# ---------------------------------------------------------------------------


class _Orch:
    def __init__(self, workdir=None):
        if workdir is not None:
            self.working_dir = workdir


class TestPersistPlanProgress:
    def test_completion_is_written_to_both_plan_records(self, tmp_path: Path):
        state = {
            "current_plan": [{"description": "a"}, {"description": "b"}],
            "task": "seed the util",
        }
        assert persist_plan_progress(_Orch(tmp_path), state) is True

        last = json.loads((tmp_path / ".codingAgent" / "last_plan.json").read_text())
        assert last["plan_completed"] is True
        assert last["current_step"] == 2  # past the end of a finished plan
        assert last["task"] == "seed the util"

        history = [
            json.loads(line)
            for line in (tmp_path / ".codingAgent" / "plan_history.jsonl")
            .read_text()
            .splitlines()
        ]
        assert history[-1]["reason"] == "plan_completed"

    def test_the_canonical_identity_keys_cannot_be_overridden(self, tmp_path: Path):
        """``extra`` merges first, so plan/task/step identity always wins."""
        from src.core.orchestration.graph.nodes.planning_helpers import save_last_plan

        save_last_plan(
            workdir=str(tmp_path),
            plan=[{"description": "real"}],
            task="real task",
            step=7,
            get_last_plan_path_fn=lambda wd: tmp_path / "p.json",
            logger=logging.getLogger(__name__),
            extra={"plan": ["hijacked"], "task": "hijacked", "plan_completed": True},
        )
        data = json.loads((tmp_path / "p.json").read_text())
        assert data["plan"] == [{"description": "real"}]
        assert data["task"] == "real task"
        assert data["current_step"] == 7
        assert data["plan_completed"] is True

    def test_a_missing_workdir_fails_closed(self):
        """Never default to "." — that would write into the process CWD."""
        state = {"current_plan": [{"description": "a"}], "task": "t"}
        assert persist_plan_progress(_Orch(), state) is False

    def test_no_plan_is_a_no_op(self, tmp_path: Path):
        assert persist_plan_progress(_Orch(tmp_path), {"task": "t"}) is False
        assert not (tmp_path / ".codingAgent" / "last_plan.json").exists()

    def test_a_write_failure_never_raises(self, tmp_path: Path):
        state = {"current_plan": [{"description": "a"}], "task": "t"}
        # A file where the .codingAgent directory should be makes mkdir fail.
        (tmp_path / ".codingAgent").write_text("not a directory")
        assert persist_plan_progress(_Orch(tmp_path), state) is False


# ---------------------------------------------------------------------------
# The defect this fixes, end to end
# ---------------------------------------------------------------------------


class TestEvaluationHonoursTheSignal:
    @staticmethod
    def _evaluate(state):
        import asyncio

        from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node

        return asyncio.run(evaluation_node(state, {}))

    def _frontier_state(self, **over):
        """State exactly as the production frontier path produces it."""
        state = {
            "current_plan": [
                {"description": "a"},
                {"description": "b"},
                {"description": "c"},
            ],
            "current_step": 0,
            "verification_passed": True,
            "verification_result": {"passed": True},
            "errors": [],
            "last_result": {"ok": True, "_completion_detected": True},
            "action_failed": False,
            "rounds": 1,
        }
        state.update(over)
        return state

    def test_a_finished_plan_reports_complete_not_replan(self):
        out = self._evaluate(self._frontier_state(plan_completed=True))
        assert out["evaluation_result"] == "complete"

    def test_without_the_signal_it_was_misreported_as_replan(self):
        """Pins the defect: the old behaviour is what P3.3 corrects."""
        out = self._evaluate(self._frontier_state())
        assert out["evaluation_result"] == "replan"

    def test_an_unfinished_plan_still_reports_replan(self):
        out = self._evaluate(
            self._frontier_state(
                plan_completed=False,
                last_result={"ok": False},
                action_failed=True,
            )
        )
        assert out["evaluation_result"] == "replan"


class TestCompletedPlansAreNotResumed:
    @staticmethod
    def _resumable(data, **over):
        from src.core.orchestration.graph.nodes.planning_helpers import (
            plan_is_resumable,
        )

        kw = dict(
            data=data,
            current_task="t",
            resume_ttl_seconds=1800,
            logger=logging.getLogger(__name__),
        )
        kw.update(over)
        return plan_is_resumable(**kw)

    def test_a_completed_plan_is_not_resumed(self):
        data = {
            "plan": [{"description": "a"}],
            "task": "t",
            "saved_at": "2099-01-01T00:00:00",
            "plan_completed": True,
        }
        assert self._resumable(data) is False

    def test_a_completed_plan_is_not_resumed_even_for_a_session_resume(self):
        data = {"plan": [{"description": "a"}], "plan_completed": True}
        assert self._resumable(data, resume_session=True) is False

    def test_an_unfinished_plan_is_still_resumed(self):
        data = {
            "plan": [{"description": "a"}],
            "task": "t",
            "saved_at": "2099-01-01T00:00:00",
        }
        assert self._resumable(data) is True

    @pytest.mark.parametrize("plan", ["a string plan", {"a": 1}, None, []])
    def test_a_corrupt_plan_shape_is_never_resumed(self, plan):
        """A non-list ``plan`` would crash the first ``step.get(...)`` on it."""
        assert self._resumable({"plan": plan, "task": "t"}) is False


# ---------------------------------------------------------------------------
# Production wiring — the node must actually call the recorder
# ---------------------------------------------------------------------------


def _run_loop(state, workdir=None, response=None, raises=None):
    """Drive the real frontier_loop_node with a stubbed model."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from src.core.orchestration.graph.nodes.frontier_loop_node import (
        frontier_loop_node,
    )

    orch = SimpleNamespace(
        working_dir=workdir,
        llm_client=None,
        tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
        event_bus=None,
    )
    payload = response or {"choices": [{"message": {"content": "all done"}}]}
    model = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=payload)
    with (
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
            return_value=orch,
        ),
        patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            new=model,
        ),
    ):
        return asyncio.run(frontier_loop_node(state, config={}))


def _plan_state():
    return {
        "task": "seed the util",
        "history": [{"role": "user", "content": "go"}],
        "tool_call_count": 0,
        "max_tool_calls": 5,
        "model_tier": "frontier",
        "current_plan": [{"description": "a"}, {"description": "b"}],
        "current_step": 0,
        "errors": [],
    }


class TestFrontierLoopRecordsCompletion:
    def test_a_finished_run_marks_the_plan_complete(self):
        result = _run_loop(_plan_state())
        assert result["plan_completed"] is True

    def test_a_finished_run_persists_the_plan(self, tmp_path: Path):
        result = _run_loop(_plan_state(), workdir=tmp_path)
        assert result["plan_completed"] is True
        last = json.loads((tmp_path / ".codingAgent" / "last_plan.json").read_text())
        assert last["plan_completed"] is True
        assert last["task"] == "seed the util"

    def test_a_failed_model_call_does_not_complete_the_plan(self):
        result = _run_loop(_plan_state(), raises=RuntimeError("provider exploded"))
        assert "plan_completed" not in result

    def test_a_context_overflow_does_not_complete_the_plan(self):
        state = _plan_state()
        state["errors"] = ["context_overflow"]
        result = _run_loop(state)
        assert "plan_completed" not in result

    def test_a_plain_task_with_no_plan_is_unaffected(self):
        state = _plan_state()
        state.pop("current_plan")
        result = _run_loop(state)
        assert "plan_completed" not in result
        # The exit result itself must still be well formed.
        assert result["last_result"]["_completion_detected"] is True
