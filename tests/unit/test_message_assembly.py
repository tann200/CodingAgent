from src.core.context.message_assembly import (
    append_task_message,
    sanitize_conversation_messages,
    truncate_conversation_to_quota,
)


def test_sanitize_conversation_messages_keeps_user_and_assistant_only():
    messages = sanitize_conversation_messages(
        conversation=[
            {"role": "system", "content": "ignore"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ],
        sanitize_text=lambda text: text.upper(),
    )

    assert messages == [
        {"role": "user", "content": "HELLO"},
        {"role": "assistant", "content": "WORLD"},
    ]


def test_truncate_conversation_to_quota_keeps_recent_messages():
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]

    truncated = truncate_conversation_to_quota(
        conversation=messages,
        conversation_quota=40,
        token_estimator=lambda text: len(text),
    )

    assert truncated
    assert truncated[-1]["content"] == "three"


def test_append_task_message_inserts_user_after_assistant_start():
    built = [{"role": "system", "content": "sys"}]
    conversation = [{"role": "assistant", "content": "tool output"}]

    result = append_task_message(
        built_messages=built,
        truncated_conversation=conversation,
        task_prompt_content="<task>do work</task>",
    )

    assert result[1] == {"role": "user", "content": "<task>do work</task>"}


def test_append_task_message_wraps_existing_user_message_when_needed():
    built = [{"role": "system", "content": "sys"}]
    conversation = [{"role": "user", "content": "continue"}]

    result = append_task_message(
        built_messages=built,
        truncated_conversation=conversation,
        task_prompt_content="<task>do work</task>",
    )

    assert result[-1]["role"] == "user"
    assert "<task>\ncontinue\n</task>" in result[-1]["content"]
