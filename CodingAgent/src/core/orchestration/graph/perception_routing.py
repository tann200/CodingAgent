import logging
import re as _re
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)

# Canonical read-only tool set reused by multiple routers/helpers.
# Keep this list in sync with the one used by _check_no_plan_fast_path.
READ_ONLY_TOOLS = {
    # canonical names
    "read_file",
    "grep",
    "glob",
    "find_symbol",
    "search_code",
    "list_files",
    "fs.read",
    "fs.list",
    # common aliases / additional read-only tools
    "ls",
    "cat",
    "read",
    "find",
    "find_files",
    "rg",
    "bash_readonly",
    "git_status",
    "git_diff",
    "git_log",
    "batched_file_read",
    "read_file_bytes",
    "read_file_chunk",
    "ast_list_symbols",
    "find_references",
    "memory_search",
    "web_search",
    "fetch",
    "browse",
}

# Tools that are considered "query" tools which usually require an
# interpretation/perception follow-up instead of immediately ending.
QUERY_TOOLS = {
    "glob",
    "find",
    "find_files",
    "grep",
    "rg",
    "search_code",
    "find_symbol",
    "find_references",
    "memory_search",
    "web_search",
    "fetch",
    "browse",
}

# HR-7 fix: exact multi-word phrases are safe to match as substrings; single
# ambiguous words ("add", "edit", etc.) are matched with word-boundary regex
# so that incidental occurrences ("authentication", "before you know it") do not
# false-positive trigger expensive analyst_delegation.
_COMPLEXITY_KEYWORDS_EXACT = (
    "refactor",
    "rewrite",
    "implement",
    "migrate",
    "redesign",
    "add feature",
    "add support",
    "create module",
    "create system",
    "integrate",
    "replace all",
    "convert all",
    "update all",
    "multi-step",
    "multiple files",
    "entire",
    "codebase",
)

# These short verbs are matched with word boundaries to avoid false positives.
# HR-2 fix: Only genuinely multi-step verbs remain. Common single-file action
# verbs (add, edit, change, update, delete, remove, insert, modify, append,
# prepend) were removed because they fire on virtually all coding tasks,
# making the fast-path dead code and forcing 6 extra LLM calls per simple edit.
_COMPLEXITY_KEYWORDS_WORD = (
    "refactor",
    "rewrite",
    "implement",
    "migrate",
    "migrate all",
    "restructure",
)

_COMPLEXITY_WORD_RE = _re.compile(
    r"\b(?:" + "|".join(_COMPLEXITY_KEYWORDS_WORD) + r")\b"
)

# Keep the old name for backwards compat (tests may reference it)
_COMPLEXITY_KEYWORDS = _COMPLEXITY_KEYWORDS_EXACT + tuple(
    kw + " " for kw in _COMPLEXITY_KEYWORDS_WORD
)


def _task_is_complex(state: Mapping[str, Any]) -> bool:
    """
    W3: Heuristic to detect tasks that are too complex for the fast-path.

    Returns True when ANY of the following are true:
    - task description contains a complexity keyword (exact phrase or word match)
    - relevant_files list has more than 3 entries (analysis already ran and found scope)
    - current_plan is already set with 2+ steps (planning already ran)

    HR-7 fix: short ambiguous keywords like "add", "edit" are now matched with
    word-boundary regex so strings like "authentication" or "before you know it"
    no longer false-positive trigger analyst_delegation.
    """
    task: str = (state.get("task") or "").lower()
    if any(kw in task for kw in _COMPLEXITY_KEYWORDS_EXACT):
        logger.info(
            "route_after_perception: task classified as complex (exact keyword match)"
        )
        return True
    if _COMPLEXITY_WORD_RE.search(task):
        logger.info(
            "route_after_perception: task classified as complex (word-boundary keyword match)"
        )
        return True

    relevant_files = state.get("relevant_files") or []
    if len(relevant_files) > 3:
        logger.info(
            f"route_after_perception: task classified as complex ({len(relevant_files)} relevant files)"
        )
        return True

    current_plan = state.get("current_plan") or []
    if len(current_plan) >= 2:
        logger.info(
            f"route_after_perception: task classified as complex ({len(current_plan)}-step plan already set)"
        )
        return True

    return False


def _task_has_more_steps(state: Mapping[str, Any]) -> bool:
    """
    Detect if the task likely has more steps needed.

    Checks for multi-step indicators in the original task description:
    - Sequential keywords: "and", "then", "after that", "next", "also", "as well"
    - Multiple file operations with "and" connector
    - Common multi-step patterns

    Returns True if the task appears to have more steps beyond what was just executed.
    """
    original_task = state.get("original_task") or ""
    task = state.get("task") or ""
    combined_task = f"{original_task} {task}".lower()

    multi_step_patterns = [
        # WF-VOL23-2: removed r"\band\b" — matches virtually every English sentence,
        # causing spurious re-analysis on all compound-sentence tasks (e.g. "read and
        # summarize", "find and fix").  The remaining patterns are more discriminating.
        r"\bthen\b",  # "do this then do that"
        r"\bafter that\b",
        r"\bnext\b",
        r"\balso\b",
        r"\bas well\b",
        r",\s*\w+\s+and\s+\w+",  # "file1, file2 and file3"
    ]

    for pattern in multi_step_patterns:
        if _re.search(pattern, combined_task):
            tool_call_count = int(state.get("tool_call_count") or 0)
            if tool_call_count < 3:
                logger.info(
                    f"route_after_perception: multi-step task detected "
                    f"(pattern='{pattern}', tool_calls={tool_call_count})"
                )
                return True

    return False


def _is_large_or_frontier(state: Mapping[str, Any]) -> bool:
    """Return True when the active model tier is LARGE or FRONTIER.

    Used by P3b-A/B/E to bypass overhead nodes (analyst_delegation,
    plan_validator) that were calibrated for 7–9B failure modes and add
    pure latency for capable 30B+ models.
    """
    tier = (state.get("model_tier") or "").lower()
    return tier in ("large", "frontier")


def _is_nano_or_small(state: Mapping[str, Any]) -> bool:
    """Return True when the active model tier is SMALL.

    Used by P3-A to skip expensive overhead nodes (analysis, analyst_delegation)
    that add context pressure without improving outcomes on 7-9B models.
    """
    tier = (state.get("model_tier") or "").lower()
    return tier == "small"


def _is_success(result: Any) -> bool:
    """Simple result success check - did execution succeed?"""
    if not result:
        return False
    _ok_flag = result.get("ok")
    return (_ok_flag is True) or (_ok_flag is None and result.get("status") == "ok")


def _extract_next_action_name(act: Any) -> str | None:
    """Extract a next-action/tool name from known state shapes."""
    if not act:
        return None
    if isinstance(act, str):
        return act
    if isinstance(act, dict):
        for key in ("name", "tool", "tool_name"):
            value = act.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def route_after_perception(
    state: Mapping[str, Any],
) -> Literal[
    "perception", "analysis", "step_controller", "execution", "memory_sync", "planning"
]:
    """Route after perception node."""
    if state.get("needs_clarification"):
        logger.info(
            "route_after_perception: needs_clarification=True — routing to memory_sync"
        )
        return "memory_sync"

    if "context_overflow" in (state.get("errors") or []):
        logger.info(
            "route_after_perception: context_overflow in errors — routing to memory_sync"
        )
        return "memory_sync"

    last_result = state.get("last_result")
    rounds = int(state.get("rounds") or 0)
    next_action = state.get("next_action")

    if next_action:
        action_name = (_extract_next_action_name(next_action) or "").lower()
        task_complex_flag = state.get("task_complexity")
        is_complex = (task_complex_flag == "complex") or _task_is_complex(state)

        if rounds == 0:
            if _is_nano_or_small(state):
                logger.info(
                    "route_after_perception: NANO/SMALL tier with next_action on first round — execution"
                )
                return "execution"

            if is_complex:
                if _is_large_or_frontier(state):
                    logger.info(
                        "route_after_perception: first-round complex task on LARGE/FRONTIER — planning"
                    )
                    return "planning"
                logger.info(
                    "route_after_perception: first-round complex task — analysis"
                )
                return "analysis"

            logger.info(
                "route_after_perception: next_action present on first round — execution"
            )
            return "execution"

        if is_complex:
            if action_name in READ_ONLY_TOOLS:
                logger.info(
                    "route_after_perception: complex task + read-only next_action on subsequent round — analysis"
                )
                return "analysis"
            logger.info(
                "route_after_perception: complex task + write/unknown next_action — execution"
            )
            return "execution"

        logger.info(
            "route_after_perception: next_action present (non-complex) — execution"
        )
        return "execution"

    if last_result and rounds > 0 and _is_success(last_result):
        if _task_has_more_steps(state):
            logger.info(
                "route_after_perception: successful tool but task has more steps — analysis"
            )
            return "analysis"
        logger.info("route_after_perception: task complete after tool — memory_sync")
        return "memory_sync"

    if rounds == 0:
        task_complexity = state.get("task_complexity")
        if task_complexity == "simple":
            logger.info(
                "route_after_perception: explicit simple task on first round — planning"
            )
            return "planning"

        if _is_large_or_frontier(state) or _is_nano_or_small(state):
            logger.info(
                "route_after_perception: tier prefers planning on first round — planning"
            )
            return "planning"

        if (
            task_complexity == "complex"
            and not _is_large_or_frontier(state)
            and not _is_nano_or_small(state)
        ):
            logger.info(
                "route_after_perception: MEDIUM explicit complex on first round — analysis"
            )
            return "analysis"

        if task_complexity is None and _task_is_complex(state):
            if _is_large_or_frontier(state) or _is_nano_or_small(state):
                logger.info(
                    "route_after_perception: heuristic-complex but tier prefers planning — planning"
                )
                return "planning"
            logger.info(
                "route_after_perception: heuristic-complex on MEDIUM — analysis"
            )
            return "analysis"

        logger.info("route_after_perception: default first round -> planning")
        return "planning"

    logger.info("route_after_perception: default fallback -> analysis")
    return "analysis"
