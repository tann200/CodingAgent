from src.core.context.prompt_blocks import (
    build_repository_intelligence_block,
    build_session_context_blocks,
    build_task_prompt_content,
    conversation_has_tool_results,
)


def test_conversation_has_tool_results_detects_tool_result_message():
    conversation = [
        {"role": "assistant", "content": "thinking"},
        {
            "role": "user",
            "content": '{"tool_execution_result": {"tool_name": "read_file"}}',
        },
    ]

    assert conversation_has_tool_results(conversation) is True


def test_build_session_context_blocks_skips_session_summary_during_tool_turns():
    conversation = [
        {
            "role": "user",
            "content": '{"tool_execution_result": {"tool_name": "grep"}}',
        }
    ]

    blocks = build_session_context_blocks(
        conversation=conversation,
        task_description="fix auth",
        get_task_state_content=lambda: "Current task details that would otherwise be injected",
        get_todo_content=lambda: "- [ ] step one",
        get_preferences_content=lambda: "Prefer small patches",
        get_past_mistakes=lambda _task: "Do not skip tests",
        min_task_state_chars=10,
        min_todo_chars=5,
        min_prefs_chars=5,
    )

    joined = "\n".join(blocks)
    assert "<session_summary>" not in joined
    assert "<past_mistakes>" not in joined
    assert "<task_progress>" in joined
    assert "<user_preferences>" in joined


def test_build_session_context_blocks_includes_expected_blocks_without_tool_results():
    blocks = build_session_context_blocks(
        conversation=[],
        task_description="fix auth",
        get_task_state_content=lambda: "Current task details that should be included",
        get_todo_content=lambda: "- [ ] step one",
        get_preferences_content=lambda: "Prefer small patches",
        get_past_mistakes=lambda _task: "Do not skip tests",
        min_task_state_chars=10,
        min_todo_chars=5,
        min_prefs_chars=5,
    )

    joined = "\n".join(blocks)
    assert "<session_summary>" in joined
    assert "<task_progress>" in joined
    assert "<user_preferences>" in joined
    assert "<past_mistakes>" in joined


def test_build_repository_intelligence_block_prefers_summary_cache():
    block = build_repository_intelligence_block(
        retrieved_snippets=[
            {
                "file_path": "src/app.py",
                "snippet": "raw snippet",
            }
        ],
        summary_cache={"src/app.py": "cached summary"},
        sanitize_text=lambda text: text,
    )

    assert "<repository_intelligence>" in block
    assert "cached summary" in block
    assert "raw snippet" not in block


def test_build_task_prompt_content_matches_expected_shape():
    content = build_task_prompt_content("Fix login bug", "2026-05-04")

    assert "<task>\nFix login bug\n</task>" in content
    assert "Today's date: 2026-05-04" in content
    assert content.endswith("Execute the next action using JSON function calling format.")
