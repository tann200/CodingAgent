from src.core.context.context_builder import ContextBuilder
from src.core.context.context_builder import _STATIC_PROMPT_CACHE

# ruff: noqa: E501


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


def test_build_prompt_normalizes_openai_function_schemas():
    ContextBuilder.clear_cache()
    builder = ContextBuilder()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Reads content from a file.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="Inspect main.py",
        tools=tools,
        conversation=[],
    )

    system_content = messages[0]["content"]
    assert "name: read_file" in system_content
    assert "description: Reads content from a file." in system_content


def test_build_prompt_preserves_tool_result_user_messages_in_conversation_history():
    builder = ContextBuilder()

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="Finish the next step",
        tools=[],
        conversation=[
            {"role": "assistant", "content": '{"name": "write_file", "arguments": {"path": "buggy.py"}}'},
            {"role": "user", "content": '{"tool_execution_result": {"ok": true, "path": "buggy.py"}}'},
        ],
    )

    assert any(
        message["role"] == "user"
        and '"tool_execution_result"' in message["content"]
        and '"ok": true' in message["content"]
        for message in messages
    )


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


def test_static_prompt_cache_separates_model_specific_prefixes():
    ContextBuilder.clear_cache()
    builder = ContextBuilder()

    common_kwargs = dict(
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=[],
        model_tier="frontier",
    )

    gemma_messages = builder.build_prompt(
        model_name="gemma-4-31b",
        provider_capabilities={
            "provider_family": "openai",
            "model": "gemma-4-31b",
        },
        **common_kwargs,
    )
    gpt_messages = builder.build_prompt(
        model_name="gpt-4o",
        provider_capabilities={
            "provider_family": "openai",
            "model": "gpt-4o",
        },
        **common_kwargs,
    )

    gemma_system = gemma_messages[0]["content"]
    gpt_system = gpt_messages[0]["content"]

    assert gemma_system != gpt_system
    assert len(_STATIC_PROMPT_CACHE) >= 2


def test_build_prompt_includes_frozen_memory_prior_context(monkeypatch):
    builder = ContextBuilder()
    monkeypatch.setattr(
        builder,
        "inject_prior_session_memories",
        lambda task, limit=3: "<prior_context>Frozen memory</prior_context>",
    )

    messages = builder.build_prompt(
        role_name="operational",
        active_skills=[],
        task_description="task",
        tools=[],
        conversation=[],
    )

    assert "<prior_context>Frozen memory</prior_context>" in messages[0]["content"]


def test_build_prompt_appends_lsp_context_after_prior_context(monkeypatch):
    builder = ContextBuilder()
    monkeypatch.setattr(
        builder,
        "inject_prior_session_memories",
        lambda task, limit=3: "<prior_context>Earlier memory</prior_context>",
    )

    import sys
    import types

    fake_mod = types.ModuleType("src.core.indexing.lsp_context")
    fake_mod.get_lsp_context_block = (
        lambda workdir: "<lsp_context>LSP block</lsp_context>"
    )
    old_mod = sys.modules.get("src.core.indexing.lsp_context")
    sys.modules["src.core.indexing.lsp_context"] = fake_mod
    try:
        messages = builder.build_prompt(
            role_name="operational",
            active_skills=[],
            task_description="task",
            tools=[],
            conversation=[],
        )
    finally:
        if old_mod is not None:
            sys.modules["src.core.indexing.lsp_context"] = old_mod
        else:
            sys.modules.pop("src.core.indexing.lsp_context", None)

    system = messages[0]["content"]
    assert "<prior_context>Earlier memory</prior_context>" in system
    assert "<lsp_context>LSP block</lsp_context>" in system
    assert system.index("<prior_context>Earlier memory</prior_context>") < system.index(
        "<lsp_context>LSP block</lsp_context>"
    )
