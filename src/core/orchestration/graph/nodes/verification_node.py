import logging
from pathlib import Path
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.tools import verification_tools

logger = logging.getLogger(__name__)


def _has_js_project(workdir: Path) -> bool:
    """Return True if workdir contains a package.json (JS/TS project)."""
    return (workdir / "package.json").exists()


def _step_requests_verification(state: Dict[str, Any]) -> bool:
    """Return True if the current plan step explicitly asks for test/verify/lint."""
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    if current_plan and current_step < len(current_plan):
        desc = current_plan[current_step].get("description", "").lower()
        return any(k in desc for k in ("run_tests", "run_linter", "verify", "test", "lint", "run_js_tests", "run_ts_check"))
    return False


async def verification_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Verification Layer: Run tests / linters / syntax checks on proposed edits.
    Also validates file deletions to ensure files are actually deleted.
    This node is intentionally conservative: it will only run verification tools when
    the state indicates a recent edit or when the `current_plan` requests validation.
    Uses the 'reviewer' role for quality assurance.
    """
    logger.info("=== verification_node START ===")


    # Decide whether verification is needed
    last_result = state.get("last_result") or {}
    need_verify = False

    # Check if last action was a deletion
    if isinstance(last_result, dict):
        r = last_result.get("result") or {}
        if isinstance(r, dict) and r.get("status") == "ok" and r.get("deleted"):
            # This was a delete_file call - verify the file is actually gone
            deleted_path = r.get("path")
            if deleted_path:
                workdir = Path(state.get("working_dir", "."))
                full_path = (
                    workdir / deleted_path
                    if not Path(deleted_path).is_absolute()
                    else Path(deleted_path)
                )
                if full_path.exists():
                    logger.warning(
                        f"Verification FAILED: {deleted_path} still exists after deletion"
                    )
                    return {
                        "verification_result": {
                            "deletion_verification": "FAILED",
                            "error": f"File still exists: {deleted_path}",
                            "path": deleted_path,
                        }
                    }
                else:
                    logger.info(
                        f"Verification PASSED: {deleted_path} successfully deleted"
                    )
                    return {
                        "verification_result": {
                            "deletion_verification": "PASSED",
                            "path": deleted_path,
                        }
                    }

    # W1: Trigger verification for any side-effecting tool that reported success.
    # Previously only edit_file with a "path" field was caught; bash, write_file, and
    # patch tools were silently skipped.  Widen the check to cover all write tools.
    SIDE_EFFECT_TOOLS = {
        "write_file", "edit_file", "edit_file_atomic", "edit_by_line_range", "bash",
        "patch_apply", "apply_patch", "create_file", "delete_file",
    }
    last_tool_name: str = state.get("last_tool_name") or ""
    try:
        if isinstance(last_result, dict):
            r = last_result.get("result") or last_result  # handle both wrapped and flat results
            if isinstance(r, dict) and r.get("status") == "ok":
                # Any side-effecting tool that succeeded triggers verification
                if last_tool_name in SIDE_EFFECT_TOOLS:
                    need_verify = True
                # Fallback: path present → legacy edit_file shape
                elif r.get("path"):
                    need_verify = True
                # Fallback: bash success (returncode present and == 0)
                elif "returncode" in r and r["returncode"] == 0:
                    need_verify = True
    except Exception:
        pass

    # Also trigger verification when the current plan step explicitly requests it
    if not need_verify and _step_requests_verification(state):
        need_verify = True
        logger.info("verification_node: step explicitly requests verification — running tests")

    # H1: Determine whether we are at the final plan step.
    # Running the full test suite after every single tool call (pytest + ruff +
    # syntax) means a 5-step plan triggers 5 full pytest runs — very slow.
    # Fix: on intermediate steps run only the cheap syntax check; reserve the
    # full suite for the final step or when the step explicitly requests it.
    current_plan = state.get("current_plan") or []
    current_step = int(state.get("current_step") or 0)
    at_final_step = (not current_plan) or (current_step >= len(current_plan) - 1)
    step_requests_verification = _step_requests_verification(state)
    run_full_suite = at_final_step or step_requests_verification

    if need_verify and not run_full_suite:
        logger.info(
            f"verification_node: intermediate step {current_step + 1}/{len(current_plan)} "
            "— deferring full test suite to final step; running syntax check only"
        )

    # H4: Helper to check whether the user has requested cancellation.
    # verification tools (pytest, ruff) can block for up to 120 s each;
    # without this check the agent is uninterruptible during the full run.
    cancel_event = state.get("cancel_event")

    def _is_cancelled() -> bool:
        try:
            return bool(cancel_event and cancel_event.is_set())
        except Exception:
            return False

    results = {}
    if need_verify:
        if _is_cancelled():
            logger.info("verification_node: cancelled before running tools — skipping")
            return {"verification_result": {"cancelled": True}, "verification_passed": True}
        try:
            wd = Path(state.get("working_dir") or ".")
            is_js = _has_js_project(wd)
            if is_js:
                if run_full_suite:
                    # JS/TS project: run JS tests + TypeScript check + linter
                    logger.info("verification_node: JS/TS project detected — running JS test suite")
                    results["js_tests"] = verification_tools.run_js_tests(str(wd))
                    if _is_cancelled():
                        logger.info("verification_node: cancelled after js_tests")
                        return {"verification_result": {**results, "cancelled": True}, "verification_passed": True}
                    results["ts_check"] = verification_tools.run_ts_check(str(wd))
                    if _is_cancelled():
                        logger.info("verification_node: cancelled after ts_check")
                        return {"verification_result": {**results, "cancelled": True}, "verification_passed": True}
                    results["eslint"] = verification_tools.run_eslint(str(wd))
                else:
                    # F21/W4: Intermediate JS/TS step — run eslint on the modified file only
                    # (faster than full suite; catches syntax errors and obvious mistakes early)
                    modified_path: str | None = None
                    try:
                        lr = state.get("last_result") or {}
                        r = lr.get("result") or lr
                        modified_path = r.get("path")
                    except Exception:
                        pass
                    if modified_path:
                        logger.info(
                            f"verification_node: intermediate JS/TS step — running eslint on {modified_path}"
                        )
                        results["eslint"] = verification_tools.run_eslint(
                            str(wd), paths=[modified_path]
                        )
                    else:
                        logger.info(
                            "verification_node: intermediate JS/TS step — no modified path; skipping eslint"
                        )
            else:
                if run_full_suite:
                    # Python project full suite: pytest + ruff + syntax
                    results["tests"] = verification_tools.run_tests(str(wd))
                    if _is_cancelled():
                        logger.info("verification_node: cancelled after run_tests")
                        return {"verification_result": {**results, "cancelled": True}, "verification_passed": True}
                    results["linter"] = verification_tools.run_linter(str(wd))
                    if _is_cancelled():
                        logger.info("verification_node: cancelled after run_linter")
                        return {"verification_result": {**results, "cancelled": True}, "verification_passed": True}
                # Always run cheap syntax check (fast, catches import errors early)
                results["syntax"] = verification_tools.syntax_check(str(wd))
        except Exception as e:
            results["error"] = str(e)

    # Determine if verification passed (handles both Python and JS/TS result shapes)
    def _failed(r: Dict) -> bool:
        return isinstance(r, dict) and r.get("status") == "fail"

    verification_passed = True
    for key in ("tests", "linter", "syntax", "js_tests", "ts_check", "eslint"):
        if _failed(results.get(key, {})):
            verification_passed = False
            break

    # Step-level atomic rollback: if verification failed, restore all files written
    # during this step to their pre-edit state.
    if not verification_passed and need_verify:
        try:
            orchestrator = _resolve_orchestrator(state, config)
            if orchestrator and hasattr(orchestrator, "rollback_step_transaction"):
                if getattr(orchestrator, "_step_snapshot_id", None):
                    rb = orchestrator.rollback_step_transaction()
                    if rb.get("ok"):
                        logger.info(
                            f"verification_node: step rollback restored "
                            f"{rb.get('restored_count', 0)} file(s)"
                        )
                        results["step_rollback"] = {
                            "triggered": True,
                            "restored_files": rb.get("restored_files", []),
                        }
                    else:
                        logger.warning(
                            f"verification_node: step rollback failed: {rb.get('error')}"
                        )
        except Exception as rb_err:
            logger.warning(f"verification_node: step rollback error (non-fatal): {rb_err}")

    return {"verification_result": results, "verification_passed": verification_passed}
