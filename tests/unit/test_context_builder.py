import pytest
from src.core.context.context_builder import ContextBuilder

def test_build_prompt_basic_structure():
    builder = ContextBuilder()
    identity = "I am an AI assistant."
    role = "My role is to help users with coding tasks."
    active_skills = ["Skill A", "Skill B"]
    task_description = "Fix the bug in the given Python code."
    tools = [
        {"name": "read_file", "description": "Reads content from a file."},
        {"name": "write_file", "description": "Writes content to a file."}
    ]
    conversation = [
        {"role": "user", "content": "Start by reading main.py"},
        {"role": "assistant", "content": "Okay, I will read main.py"}
    ]

    messages = builder.build_prompt(identity, role, active_skills, task_description, tools, conversation)
    
    assert len(messages) == 7 # identity, role, active_skills, task, tools, user_conv, assistant_conv
    assert messages[0]["content"] == f"<identity>\n{identity}\n</identity>"
    assert messages[1]["content"] == f"<role>\n{role}\n</role>"
    assert messages[2]["content"] == f"<active_skills>\nSkill A\nSkill B\n</active_skills>"
    assert messages[3]["content"] == f"<task>\n{task_description}\n</task>"
    assert "<tools>" in messages[4]["content"]
    assert "name: read_file" in messages[4]["content"]
    assert "name: write_file" in messages[4]["content"]
    assert messages[5]["content"] == conversation[0]["content"] # First conversation message
    assert messages[6]["content"] == conversation[1]["content"] # Second conversation message

def test_build_prompt_token_budgeting_truncation():
    # Use a custom token estimator where 1 char = 1 token for simpler testing
    builder = ContextBuilder(token_estimator=lambda s: len(s))
    max_tokens = 100

    # Identity: 10 chars, quota is min(0.12*100, 800) = 12
    identity = "A short identity."
    long_identity = "This is a very very very very very very very very very very very long identity that will be truncated."

    # Role: 10 chars, quota is min(0.12*100, 800) = 12
    role = "A short role."
    long_role = "This is a very very very very very very very very very very very long role that will be truncated."
    
    # Tools: 20 chars, quota is min(0.06*100, 400) = 6
    tools = [
        {"name": "tool1", "description": "desc1"},
        {"name": "tool2", "description": "desc2"}
    ]
    tools_content_len = len("<tools>\nname: tool1\ndescription: desc1\nname: tool2\ndescription: desc2\n</tools>") # ~70 chars

    # Conversation: remaining quota
    # 100 - (12 + 12 + 6) = 70
    long_conversation = []
    for i in range(10):
        long_conversation.append({"role": "user", "content": f"Message {i}: This is a long conversation message that will be truncated or dropped."})

    # Test identity truncation
    messages = builder.build_prompt(long_identity, role, [], "task", [], [], max_tokens=max_tokens)
    assert builder.token_estimator(messages[0]["content"]) <= 12
    # If the budget is so small (12) that the marker itself doesn't fit with the XML tags, 
    # it might just truncate the raw string without adding the marker, to stay under budget.
    # Let's test with a larger budget to ensure the marker is added.
    
    messages_larger_budget = builder.build_prompt(long_identity, role, [], "task", [], [], max_tokens=600)
    assert "[TRUNCATED]" in messages_larger_budget[0]["content"]

    # Test role truncation
    messages = builder.build_prompt(identity, long_role, [], "task", [], [], max_tokens=max_tokens)
    assert builder.token_estimator(messages[1]["content"]) <= 12
    
    # Test tools truncation
    messages = builder.build_prompt(identity, role, [], "task", tools, [], max_tokens=max_tokens)
    assert builder.token_estimator(messages[3]["content"]) <= 6

    # Test conversation truncation - should drop oldest messages
    messages = builder.build_prompt(identity, role, [], "task", [], long_conversation, max_tokens=max_tokens)
    assert any("[CONTEXT FULL" in m["content"] for m in messages)
    # Check that at least one conversation message is present if space allows
    # The last message is inserted at index -1 (after other system messages), so check the last item
    last_conv_msg = messages[-1]
    assert "Message 9: This is a long" in last_conv_msg["content"]

    # Test conversation quota <= 0
    messages = builder.build_prompt(identity, role, [], "task", [], long_conversation, max_tokens=2) 
    assert any("[CONTEXT FULL" in m["content"] for m in messages)
    # Should only have identity, role, task, tools and a truncation message, no actual conversation messages
    assert len([m for m in messages if m["role"] == "user" or m["role"] == "assistant"]) == 0
