from __future__ import annotations

from typing import Any, Callable, List, Mapping, Optional, Sequence


_EMPTY_TASK_STATE = "# Current Task\n\n# Completed Steps\n\n# Next Step"


def _tagged_block(tag: str, content: str) -> str:
    return f"<{tag}>\n{content}\n</{tag}>"


def conversation_has_tool_results(conversation: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        message.get("role") == "user"
        and "tool_execution_result" in str(message.get("content", ""))
        for message in conversation
    )


def build_session_context_blocks(
    *,
    conversation: Sequence[Mapping[str, Any]],
    task_description: str,
    get_task_state_content: Callable[[], Optional[str]],
    get_todo_content: Callable[[], Optional[str]],
    get_preferences_content: Callable[[], Optional[str]],
    get_past_mistakes: Callable[[str], Optional[str]],
    min_task_state_chars: int,
    min_todo_chars: int,
    min_prefs_chars: int,
) -> List[str]:
    parts: List[str] = []
    has_tool_results = conversation_has_tool_results(conversation)

    # Only inject session summary at task start or after completion. Mid-execution
    # tool-result turns should not get TASK_STATE re-injected.
    try:
        task_state = get_task_state_content()
        if (
            not has_tool_results
            and task_state
            and task_state.strip() != _EMPTY_TASK_STATE.strip()
            and len(task_state) > min_task_state_chars
        ):
            parts.append(_tagged_block("session_summary", task_state))
    except Exception:
        pass

    try:
        todo_content = get_todo_content()
        if todo_content and len(todo_content) > min_todo_chars:
            parts.append(_tagged_block("task_progress", todo_content))
    except Exception:
        pass

    try:
        preferences_content = get_preferences_content()
        if preferences_content and len(preferences_content) > min_prefs_chars:
            parts.append(_tagged_block("user_preferences", preferences_content))
    except Exception:
        pass

    try:
        if not has_tool_results and task_description:
            past_mistakes = get_past_mistakes(task_description)
            if past_mistakes:
                parts.append(_tagged_block("past_mistakes", past_mistakes))
    except Exception:
        pass

    return parts


def build_repository_intelligence_block(
    *,
    retrieved_snippets: Sequence[Mapping[str, Any]],
    summary_cache: Mapping[str, Any],
    sanitize_text: Callable[[str], str],
) -> str:
    repo_entries: List[str] = []
    for snippet in retrieved_snippets[:10]:
        file_path = snippet.get("file_path")
        if file_path and file_path in summary_cache:
            entry_text = summary_cache.get(file_path)
        else:
            entry_text = snippet.get("snippet") or snippet.get("content") or ""
        repo_entries.append(
            f"File: {file_path or 'unknown'}\n{sanitize_text(str(entry_text))}\n---\n"
        )

    if not repo_entries:
        return ""
    return _tagged_block("repository_intelligence", "\n".join(repo_entries))


def build_task_prompt_content(task_description: str, today_iso: str) -> str:
    return (
        f"<task>\n{task_description}\n</task>\n"
        f"<context>\nToday's date: {today_iso}\n</context>\n\n"
        "Execute the next action using JSON function calling format."
    )
