import logging
from pathlib import Path
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.tools import verification_tools
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


async def verification_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Verification Layer: Run tests / linters / syntax checks on proposed edits.
    Also validates file deletions to ensure files are actually deleted.
    This node is intentionally conservative: it will only run verification tools when
    the state indicates a recent edit or when the `current_plan` requests validation.
    Uses the 'reviewer' role for quality assurance.
    """
    logger.info("=== verification_node START ===")

    brain = get_agent_brain_manager()
    reviewer_role = brain.get_role("reviewer") or "You are a QA reviewer."

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

    # If the last action was an edit_file that reported ok, run verification
    try:
        if isinstance(last_result, dict):
            r = last_result.get("result") or {}
            if isinstance(r, dict) and r.get("status") == "ok" and r.get("path"):
                need_verify = True
    except Exception:
        pass

    results = {}
    if need_verify:
        try:
            wd = Path(state.get("working_dir"))
            results["tests"] = verification_tools.run_tests(str(wd))
            results["linter"] = verification_tools.run_linter(str(wd))
            results["syntax"] = verification_tools.syntax_check(str(wd))
        except Exception as e:
            results["error"] = str(e)

    # Determine if verification passed
    tests_status = results.get("tests", {}).get("status")
    linter_status = results.get("linter", {}).get("status")
    syntax_status = results.get("syntax", {}).get("status")

    verification_passed = True
    if tests_status == "fail" or linter_status == "fail" or syntax_status == "fail":
        verification_passed = False

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
