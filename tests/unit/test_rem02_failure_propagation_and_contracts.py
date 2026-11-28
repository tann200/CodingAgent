"""Regression tests for REM-02: failure propagation, completion contracts, and validation debt."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.cli.headless_stream import classify_headless_status, exit_code_for_status
from src.core.orchestration.graph.completion_contract import (
    TaskType,
    classify_task,
    evaluate_completion_contract,
)
from src.core.orchestration.graph.validation_authority import (
    ValidationDebtTracker,
    classify_shell_validation,
)
from src.core.orchestration.inference_loop_responses import _build_success_response
from src.core.orchestration.inference_loop_rounds import _build_loop_exit_response


def _fake_orch() -> MagicMock:
    orch = MagicMock()
    orch.msg_mgr.messages = []
    orch.msg_mgr._lock = MagicMock()
    orch._session_modified_files = []
    orch._dry_run_log = []
    orch._current_task_id = "task-rem02"
    return orch


class TestFailurePropagation:
    def test_failed_graph_run_propagates_ok_false_and_nonzero_exit(self) -> None:
        orch = _fake_orch()
        final_state = {
            "history": [{"role": "assistant", "content": "Tried but failed."}],
            "run": {"status": "failed", "outcome": "failed"},
            "errors": ["verification_failed: syntax error in app.py"],
            "verification_passed": False,
        }
        res = _build_success_response(orch, final_state)
        assert res["ok"] is False
        assert res["status"] == "failed"
        assert "verification_failed" in res["error"]
        assert classify_headless_status(res) == "failed"
        assert exit_code_for_status(classify_headless_status(res)) == 1

    def test_cancelled_graph_run_propagates_cancelled_and_exit_130(self) -> None:
        orch = _fake_orch()
        final_state = {
            "history": [],
            "run": {"status": "cancelled", "outcome": "cancelled"},
        }
        res = _build_success_response(orch, final_state)
        assert res["ok"] is False
        assert res["status"] == "cancelled"
        assert res["cancelled"] is True
        assert classify_headless_status(res) == "aborted"
        assert exit_code_for_status(classify_headless_status(res)) == 130

    def test_completed_graph_run_propagates_ok_true(self) -> None:
        orch = _fake_orch()
        final_state = {
            "history": [{"role": "assistant", "content": "Done!"}],
            "run": {"status": "completed", "outcome": "complete"},
            "verification_passed": True,
        }
        res = _build_success_response(orch, final_state)
        assert res["ok"] is True
        assert res["status"] == "completed"
        assert classify_headless_status(res) == "complete"
        assert exit_code_for_status(classify_headless_status(res)) == 0

    def test_loop_exit_response_sets_ok_false(self) -> None:
        cancel = MagicMock()
        cancel.is_set.return_value = True
        r_cancel = _build_loop_exit_response(final_state={}, cancel_event=cancel)
        assert r_cancel is not None
        assert r_cancel["ok"] is False
        assert r_cancel["cancelled"] is True
        assert classify_headless_status(r_cancel) == "aborted"

        r_loop = _build_loop_exit_response(
            final_state={"errors": ["infinite_loop_detected"]},
            cancel_event=None,
        )
        assert r_loop is not None
        assert r_loop["ok"] is False
        assert r_loop["status"] == "failed"
        assert classify_headless_status(r_loop) == "failed"


class TestCompletionContractIntegrity:
    def test_read_only_bash_cannot_bypass_deliverable_or_debt_checks(self, tmp_path: Path) -> None:
        verdict = evaluate_completion_contract(
            task="Create hello.py and fix the failing tests",
            content_text="All done!",
            modified_files=[],
            last_tool_name="bash",
            last_result={"ok": True, "status": "ok", "output": "README.md"},
            tool_calls_count=2,
            mutating_tool_calls_count=1,
            working_dir=str(tmp_path),
            unpaid_validation_debt=["pytest tests/test_hello.py"],
        )
        assert verdict.is_complete is False
        assert verdict.error_code == "unverified_mutation_missing"
        assert "hello.py" in (verdict.failure_reason or "")

        # Even if a non-shell mutating tool (e.g. delete_file) succeeded with empty modified_files,
        # missing deliverable and unpaid debt checks still execute before accepting completion.
        verdict2 = evaluate_completion_contract(
            task="Create hello.py and fix the failing tests",
            content_text="All done!",
            modified_files=[],
            last_tool_name="delete_file",
            last_result={"ok": True, "status": "ok"},
            tool_calls_count=2,
            mutating_tool_calls_count=1,
            working_dir=str(tmp_path),
            unpaid_validation_debt=["pytest tests/test_hello.py"],
        )
        assert verdict2.is_complete is False
        assert verdict2.error_code == "missing_named_deliverable"

    def test_read_only_bash_cannot_satisfy_mutating_task_with_zero_edits(self, tmp_path: Path) -> None:
        verdict = evaluate_completion_contract(
            task="Fix the bug in auth.py",
            content_text="Fixed!",
            modified_files=[],
            last_tool_name="bash",
            last_result={"ok": True, "status": "ok", "output": "ls output"},
            tool_calls_count=1,
            mutating_tool_calls_count=1,
            working_dir=str(tmp_path),
        )
        assert verdict.is_complete is False
        assert verdict.error_code == "unverified_mutation_missing"

    def test_compound_explain_and_fix_classifies_as_mutating(self) -> None:
        assert (
            classify_task("Explain why auth.py fails and fix the bug in auth.py")
            == TaskType.MUTATING
        )
        assert classify_task("Explain why auth.py fails") == TaskType.INFORMATIONAL


class TestValidationAuthorityHardening:
    def test_newline_and_bang_negation_demoted_to_unconfirmed(self) -> None:
        c_nl = classify_shell_validation("pytest tests/\ntrue")
        assert c_nl["authority"] == "unconfirmed"
        assert "\n" in c_nl["demoting_operators"]

        c_bang = classify_shell_validation("! pytest tests/")
        assert c_bang["authority"] == "unconfirmed"
        assert "!" in c_bang["demoting_operators"]

    def test_unrelated_single_file_pytest_does_not_clear_other_file_debt(self) -> None:
        tracker = ValidationDebtTracker()
        tracker.record_command("pytest tests/test_auth.py", passed=False, exit_code=1)
        assert tracker.unpaid_debt() == ["pytest tests/test_auth.py"]

        tracker.record_mutation()
        # Passing unrelated test file must NOT pay down test_auth.py debt
        tracker.record_command("pytest tests/test_trivial.py", passed=True, exit_code=0)
        assert tracker.unpaid_debt() == ["pytest tests/test_auth.py"]

        # Wrapper-normalized matching command DOES pay down debt
        tracker.record_command(
            "env CI=1 .venv/bin/pytest tests/test_auth.py 2>&1",
            passed=True,
            exit_code=0,
        )
        assert tracker.unpaid_debt() == []

    def test_broad_pytest_clears_narrow_python_debt(self) -> None:
        tracker = ValidationDebtTracker()
        tracker.record_command("pytest tests/test_auth.py", passed=False, exit_code=1)
        tracker.record_mutation()
        tracker.record_command("pytest tests/", passed=True, exit_code=0)
        assert tracker.unpaid_debt() == []
