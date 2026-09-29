"""tool_result_safety.py — production-path sanitization for tool results.

P1.1 (audit CRITICAL-1): ``detect_prompt_injection`` existed and was unit-tested,
but its only call sites were ``execution_node`` and ``execution_lifecycle`` —
both legacy-only nodes that neither the frontier nor the lite production graph
contains.  Untrusted content (file reads, web fetches, grep over third-party
code) therefore reached the model context completely unannotated on the path
that actually runs.

``sanitize_tool_result`` is the single choke point every production tool result
passes through.  It is kept separate from the node so the policy is testable
without constructing a graph, and so the node file stays within its size guard.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from src.core.orchestration.graph.nodes.tool_output_truncation import (
    detect_prompt_injection,
    truncate_tool_output,
)

logger = logging.getLogger(__name__)


def sanitize_tool_result(
    result: Mapping[str, Any],
    *,
    marker_label: str = "frontier_loop",
    log: Any = None,
) -> dict:
    """Scan *result* for prompt injection, then cap its large text fields.

    Detection runs **before** truncation on purpose.  Truncation keeps the head
    of each large field, so scanning afterwards would miss an injection sitting
    in the truncated tail — precisely the case where untrusted content is most
    likely to be smuggled in (a long file with a late instruction).

    The ``_prompt_injection_detected`` flag is a dict key rather than body text,
    so it survives truncation and always reaches the model in the serialized
    ``tool_execution_result`` even when the inline warning is itself cut off by
    the byte cap.

    Args:
        result:       Raw tool result.  Non-dict mappings are coerced to dict.
        marker_label: Label used in the truncation marker.
        log:          Optional logger; defaults to this module's logger.

    Returns:
        A dict carrying the (possibly annotated) result.  Never raises: a failed
        scan degrades to truncation-only rather than dropping the tool output.
    """
    _log = log or logger
    try:
        scanned: Any = detect_prompt_injection(result, logger=_log)
    except Exception:
        _log.warning(
            "sanitize_tool_result: prompt-injection scan failed; "
            "continuing without annotation",
            exc_info=True,
        )
        scanned = result
    return truncate_tool_output(scanned, marker_label=marker_label)
