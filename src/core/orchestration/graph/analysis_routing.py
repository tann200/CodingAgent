import logging
from typing import Any, Literal, Mapping

from src.core.orchestration.graph.perception_routing import (
    _is_nano_or_small,
    _task_is_complex,
)

logger = logging.getLogger(__name__)


def should_after_analysis(
    state: Mapping[str, Any],
) -> Literal["analyst_delegation", "planning"]:
    """Route after analysis."""
    if _is_nano_or_small(state):
        logger.info(
            "should_after_analysis: P3-A constrained tier — skipping analyst_delegation, "
            "going directly to planning"
        )
        return "planning"

    if _task_is_complex(state):
        logger.info(
            "should_after_analysis: complex task → analyst_delegation before planning"
        )
        return "analyst_delegation"

    logger.info("should_after_analysis: simple task → planning directly")
    return "planning"
