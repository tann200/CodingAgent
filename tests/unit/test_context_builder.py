from src.core.context.context_builder import ContextBuilder


def test_build_prompt_basic_structure():
    builder = ContextBuilder()
    identity = "I am an AI assistant."
    role = "My role is to help users with coding tasks."
    active_skills = ["Skill A", "Skill B"]
    task_description = "Fix the bug in the given Python code."
    tools = [
        {"name": "read_file", "description": "Reads content from a file."},
        {"name": "write_file", "description": "Writes content to a file."},
    ]
    conversation = [
        {"role": "user", "content": "Start by reading main.py"},
        {"role": "assistant", "content": "Okay, I will read main.py"},
    ]

    messages = builder.build_prompt(
        identity, role, active_skills, task_description, tools, conversation
    )

    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert "<identity>" in messages[0]["content"]
    assert "<role>" in messages[0]["content"]
    assert "<available_tools>" in messages[0]["content"]
    assert "<output_format>" in messages[0]["content"]
    assert messages[1]["content"] == conversation[0]["content"]
    assert messages[2]["content"] == conversation[1]["content"]
    assert messages[3]["role"] == "user"
    assert "<task>" in messages[3]["content"]


def test_build_prompt_token_budgeting_truncation():
    builder = ContextBuilder(token_estimator=lambda s: len(s))
    max_tokens = 100

    identity = "A short identity."
    long_identity = "This is a very very very very very very very very very very very long identity that will be truncated."

    role = "A short role."
    long_role = "This is a very very very very very very very very very very very long role that will be truncated."

    tools = [
        {"name": "tool1", "description": "desc1"},
        {"name": "tool2", "description": "desc2"},
    ]
    tools_content_len = len(
        "<available_tools>\nname: tool1\ndescription: desc1\nname: tool2\ndescription: desc2\n</available_tools>"
    )

    long_conversation = []
    for i in range(10):
        long_conversation.append(
            {
                "role": "user",
                "content": f"Message {i}: This is a long conversation message that will be truncated or dropped.",
            }
        )

    messages = builder.build_prompt(
        long_identity, role, [], "task", [], [], max_tokens=max_tokens
    )
    assert messages[0]["role"] == "system"

    messages = builder.build_prompt(
        identity, long_role, [], "task", [], [], max_tokens=max_tokens
    )
    assert messages[0]["role"] == "system"

    messages = builder.build_prompt(
        identity, role, [], "task", tools, [], max_tokens=max_tokens
    )
    assert messages[0]["role"] == "system"

    messages = builder.build_prompt(
        identity, role, [], "task", [], long_conversation, max_tokens=max_tokens
    )
    assert messages[0]["role"] == "system"
    assert len([m for m in messages if m["role"] == "user"]) >= 1

    messages = builder.build_prompt(
        identity, role, [], "task", [], long_conversation, max_tokens=2
    )
    assert len([m for m in messages if m["role"] == "user"]) >= 1


def test_qwen_compatibility_user_after_system():
    """Test that user message comes immediately after system for Qwen Jinja template compatibility."""
    builder = ContextBuilder()

    # Test 1: Empty conversation - user should be after system
    messages = builder.build_prompt(
        "identity", "role", [], "task", [], [], max_tokens=6000
    )
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"

    # Test 2: Conversation starts with assistant - user should be inserted after system
    conversation = [
        {
            "role": "assistant",
            "content": "```yaml\nname: bash\narguments:\n  command: ls\n```",
        },
        {"role": "user", "content": "result here"},
    ]
    messages = builder.build_prompt(
        "identity", "role", [], "task", [], conversation, max_tokens=6000
    )
    # First should be system, second should be user (inserted for Qwen)
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    # Then assistant and user alternation
    assert messages[2]["role"] == "assistant"
    assert messages[3]["role"] == "user"


def test_qwen_compatibility_no_empty_assistant():
    """Test that empty assistant messages are handled properly."""
    builder = ContextBuilder()

    # Conversation with empty assistant message
    conversation = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": ""},  # Empty - should not break
        {"role": "assistant", "content": "Hi there"},  # Non-empty - should be kept
    ]
    messages = builder.build_prompt(
        "identity", "role", [], "task", [], conversation, max_tokens=6000
    )
    # Should have system, user (from task), conversation user, conversation assistant
    roles = [m["role"] for m in messages]
    assert "system" in roles
    assert "user" in roles
    # Should have assistant with non-empty content
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert any(m["content"].strip() for m in assistant_msgs)
