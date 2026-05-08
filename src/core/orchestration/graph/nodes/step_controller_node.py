from langchain_core.runnables import RunnableConfig
import logging
from pathlib import Path
from typing import Dict, Any, List

from src.core.orchestration.graph.state import StateLike

logger = logging.getLogger(__name__)


async def step_controller_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """
    Step Controller: Enforces single-step execution from the plan.
    Validates that the current step matches the planned action.

    WF-3: After each completed step, runs a lightweight lint check on the
    last written .py file so syntax errors surface before the next step.
    """
    logger.info("=== step_controller_node START ===")

    current_plan = state.get("current_plan")
    if not isinstance(current_plan, list):
        current_plan = []
    current_step = state.get("current_step")
    if not isinstance(current_step, int):
        current_step = 0
    step_controller_enabled = bool(state.get("step_controller_enabled", True))

    if not step_controller_enabled or not current_plan:
        logger.info("step_controller_node: disabled or no plan, passing through")
        return {}

    plan_len = len(current_plan)
    if current_step >= plan_len:
        logger.info(
            f"step_controller_node: step {current_step} >= plan length {plan_len}"
        )
        return {"next_action": None}

    current_step_data = current_plan[current_step]
    step_description = str(current_step_data.get("description", ""))
    planned_action = current_step_data.get("action")

    # H2: Track per-step retry counts so should_after_step_controller can cap retries.
    step_retry_counts: dict = dict(state.get("step_retry_counts") or {})
    # Normalize legacy int keys to str so all readers (which use str(key)) never miss entries.
    for _k in list(step_retry_counts):
        if not isinstance(_k, str):
            try:
                step_retry_counts[str(_k)] = step_retry_counts.pop(_k)
            except Exception:
                pass
    step_key = str(current_step)
    last_result = state.get("last_result")
    step_failed = bool(
        last_result
        and isinstance(last_result, dict)
        and not (last_result.get("ok") or last_result.get("status") == "ok")
    )
    if step_failed:
        step_retry_counts[step_key] = int(step_retry_counts.get(step_key, 0)) + 1
        logger.info(
            f"step_controller_node: step {current_step + 1} retry #{step_retry_counts[step_key]}"
        )

    # WF-3: Lightweight per-step lint check — run quick_lint on any .py file written
    # in the last step so syntax errors surface before advancing to the next step.
    # P3b-C: Skip for LARGE/FRONTIER — capable 30B+ models rarely produce syntax
    # errors and the extra lint round-trip adds latency without value.
    _tier_for_lint = (state.get("model_tier") or "").lower()
    _skip_lint = _tier_for_lint in ("large", "frontier")
    step_lint_warnings: List[str] = []
    if (
        not _skip_lint
        and last_result
        and isinstance(last_result, dict)
        and not step_failed
    ):
        _written_path = last_result.get("path") or last_result.get("file")
        if _written_path and str(_written_path).endswith(".py"):
            try:
                from src.tools.lint_dispatch import quick_lint as _quick_lint

                _workdir = Path(state.get("working_dir") or ".")
                _lr = _quick_lint(str(_written_path), _workdir)
                if _lr and _lr.get("lint_errors"):
                    step_lint_warnings = list(_lr["lint_errors"])
                    # Treat lint errors as a step failure so retry logic fires
                    step_failed = True
                    step_retry_counts[step_key] = (
                        int(step_retry_counts.get(step_key, 0)) + 1
                    )
                    logger.warning(
                        "step_controller_node WF-3: lint errors on step %d path=%s: %s",
                        current_step + 1,
                        _written_path,
                        step_lint_warnings[:3],
                    )
            except Exception as _lint_exc:
                logger.debug(
                    "step_controller_node WF-3: lint check skipped (non-fatal): %s",
                    _lint_exc,
                )
    elif _skip_lint:
        logger.debug(
            "step_controller_node WF-3: skipping lint for capable tier=%s (P3b-C)",
            _tier_for_lint,
        )

    logger.info(
        f"step_controller_node: enforcing step {current_step + 1}/{plan_len}: {step_description}"
    )

    return {
        "step_description": step_description,
        "planned_action": planned_action,
        "step_retry_counts": step_retry_counts,
        "step_lint_warnings": step_lint_warnings,
    }
