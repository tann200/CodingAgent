"""Canary tests pinning security & untrusted-envelope invariants in system prompts."""

from __future__ import annotations

from src.core.context.context_builder import ContextBuilder
from src.core.context.tool_prompt_sections import (
    SECRET_FILE_RULE,
    UNTRUSTED_ENVELOPE_RULE,
    build_active_tool_guidance_block,
    build_security_contract_block,
)


def test_security_contract_canary_present_across_all_tiers(tmp_path):
    ContextBuilder.clear_cache()
    cb = ContextBuilder(working_dir=str(tmp_path))
    tools = [{"name": "read_file", "description": "Read a file."}]

    for tier, model in (
        ("nano", "qwen3.5-4b"),
        ("compact", "gemini-3.5-flash-lite"),
        ("standard", "qwen/qwen3.8-27b"),
        ("frontier", "claude-sonnet-4-6"),
    ):
        msgs = cb.build_prompt(
            role_name="operational",
            active_skills=[],
            task_description="Inspect app.py",
            tools=tools,
            conversation=[],
            model_tier=tier,
            model_name=model,
            provider_capabilities={"model": model, "supports_native_tools": True},
        )
        sys_msg = msgs[0]["content"]
        assert "<security>" in sys_msg, f"<security> missing for tier={tier}"
        assert SECRET_FILE_RULE in sys_msg, f"SECRET_FILE_RULE drifted for tier={tier}"
        assert UNTRUSTED_ENVELOPE_RULE in sys_msg, f"UNTRUSTED_ENVELOPE_RULE drifted for tier={tier}"


def test_security_contract_block_mentions_secret_files_and_envelopes():
    block = build_security_contract_block()
    assert ".env" in block
    assert "bash" in block
    assert "<untrusted_content>" in block
    assert "<project_instructions>" in block
    assert "<mcp_result>" in block


def test_active_tool_guidance_gates_clauses_strictly_to_active_tools():
    minimal = build_active_tool_guidance_block(
        [{"name": "read_file"}, {"name": "edit_file_atomic"}],
        model_tier="frontier",
    )
    assert minimal == ""

    with_todo_compact = build_active_tool_guidance_block(
        [{"name": "read_file"}, {"name": "manage_todo"}],
        model_tier="compact",
    )
    assert "manage_todo" not in with_todo_compact

    with_todo_frontier = build_active_tool_guidance_block(
        [{"name": "read_file"}, {"name": "manage_todo"}],
        model_tier="frontier",
    )
    assert "manage_todo" in with_todo_frontier
    assert "delegate_task" not in with_todo_frontier
    assert "find_symbol" not in with_todo_frontier

    # OpenAI-wrapped tool schema format is also supported
    wrapped = build_active_tool_guidance_block(
        [
            {"type": "function", "function": {"name": "find_symbol"}},
            {"type": "function", "function": {"name": "lsp_diagnostics"}},
            {"type": "function", "function": {"name": "delegate_task"}},
            {"type": "function", "function": {"name": "mcp_github_get_issue"}},
        ],
        model_tier="frontier",
    )
    assert "find_symbol" in wrapped
    assert "lsp_diagnostics" in wrapped
    assert "delegate_task" in wrapped
    assert "untrusted external data" in wrapped
