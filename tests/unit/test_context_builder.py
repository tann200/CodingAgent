from src.core.context.context_builder import ContextBuilder


def test_build_prompt_basic_structure():
    builder = ContextBuilder()
    active_skills = ["dry", "context_hygiene"]
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
        role_name="operational",
        active_skills=active_skills,
        task_description=task_description,
        tools=tools,
        conversation=conversation,
    )

    assert len(messages) >= 3
    assert messages[0]["role"] == "system"
    assert "<identity>" in messages[0]["content"]
    assert "<role>" in messages[0]["content"]
    assert "<available_tools>" in messages[0]["content"]
    assert "<output_format>" in messages[0]["content"]


def test_build_prompt_token_budgeting_truncation():
    builder = ContextBuilder(token_estimator=lambda s: len(s))
    max_tokens = 100

    tools = [
        {"name": "tool1", "description": "desc1"},
        {"name": "tool2", "description": "desc2"},
    ]

    long_conversation = []
    for i in range(10):
        long_conversation.append(
            {
                "role": "user",
                "content": f"Message {i}: This is a long conversation message that will be truncated or dropped.",
            }
        )

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=tools,
        conversation=[],
        max_tokens=max_tokens,
    )
    assert messages[0]["role"] == "system"

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=long_conversation,
        max_tokens=max_tokens,
    )
    assert messages[0]["role"] == "system"
    assert len([m for m in messages if m["role"] == "user"]) >= 1

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=long_conversation,
        max_tokens=2,
    )
    assert len([m for m in messages if m["role"] == "user"]) >= 1


def test_qwen_compatibility_user_after_system():
    """Test that user message comes immediately after system for Qwen Jinja template compatibility."""
    builder = ContextBuilder()

    # Test 1: Empty conversation - user should be after system
    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=[],
        max_tokens=6000,
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
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=conversation,
        max_tokens=6000,
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
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=conversation,
        max_tokens=6000,
    )
    # Should have system, user (from task), conversation user, conversation assistant
    roles = [m["role"] for m in messages]
    assert "system" in roles
    assert "user" in roles
    # Should have assistant with non-empty content
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert any(m["content"].strip() for m in assistant_msgs)


def test_build_prompt_native_tools_true():
    """Test that provider_capabilities with supports_native_tools=True uses native format."""
    builder = ContextBuilder()

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="Test task",
        tools=[{"name": "read_file", "description": "Read a file"}],
        conversation=[],
        provider_capabilities={"supports_native_tools": True},
    )

    system_content = messages[0]["content"]

    # Should have native tools instructions
    assert (
        "native tools" in system_content.lower() or "native" in system_content.lower()
    )
    # The native format should NOT have the YAML tool calling instructions block
    # (there may still be ```yaml in other contexts like examples)
    assert (
        "To execute an action, you MUST use the provided markdown YAML tool format"
        not in system_content
    )


def test_build_prompt_native_tools_false():
    """Test that provider_capabilities with supports_native_tools=False uses YAML format."""
    builder = ContextBuilder()

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="Test task",
        tools=[{"name": "read_file", "description": "Read a file"}],
        conversation=[],
        provider_capabilities={"supports_native_tools": False},
    )

    system_content = messages[0]["content"]

    # Should have YAML format instructions
    assert "```yaml" in system_content
    # Should NOT have native tools instructions
    assert "native" not in system_content.lower() or "Use the native" in system_content


def test_build_prompt_native_tools_none():
    """Test that missing provider_capabilities defaults to YAML format."""
    builder = ContextBuilder()

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="Test task",
        tools=[{"name": "read_file", "description": "Read a file"}],
        conversation=[],
        provider_capabilities=None,
    )

    system_content = messages[0]["content"]

    # Should default to YAML format
    assert "```yaml" in system_content


def test_build_prompt_native_tools_empty():
    """Test that empty provider_capabilities defaults to YAML format."""
    builder = ContextBuilder()

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="Test task",
        tools=[{"name": "read_file", "description": "Read a file"}],
        conversation=[],
        provider_capabilities={},
    )

    system_content = messages[0]["content"]

    # Should default to YAML format
    assert "```yaml" in system_content


def test_role_name_loads_correct_role():
    """Test that role_name parameter loads the correct role from agent-brain."""
    builder = ContextBuilder()

    # Test operational role
    messages_op = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=[],
    )
    assert (
        "Execute planned steps" in messages_op[0]["content"]
        or "Operational Role" in messages_op[0]["content"]
    )

    # Test strategic role
    messages_strat = builder.build_prompt(
        role_name="strategic",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=[],
    )
    assert (
        "decompose complex tasks" in messages_strat[0]["content"]
        or "Strategic Role" in messages_strat[0]["content"]
    )


def test_active_skills_loads_from_files():
    """Test that active_skills parameter loads skill content from files."""
    builder = ContextBuilder()

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=["dry"],
        task_description="task",
        tools=[],
        conversation=[],
    )

    system_content = messages[0]["content"]
    # Should contain DRY skill content
    assert "Don't Repeat Yourself" in system_content or "DRY" in system_content
