"""Unit tests for lossless prompt and tool-schema token optimizations."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.core.context.context_builder import ContextBuilder
from src.core.context.static_prompt_parts import (
    build_output_format_block,
    build_static_system_parts,
    build_thinking_mode_block,
)
from src.core.orchestration.graph.runtime_policy import (
    COMPACT_RUNTIME_POLICY,
    resolve_runtime_policy,
)
from src.tools._registry import build_registry


def test_static_system_parts_skips_duplicate_available_tools_when_native_tools_enabled():
    result = build_static_system_parts(
        soul="Soul text",
        role_content="Role text",
        active_skills=[],
        get_skill=lambda _name: "",
        sanitize_text=lambda text: text,
        system_prompt_dynamic_boundary="__BOUNDARY__",
        tools=[{"name": "read_file", "description": "Read a file."}],
        model_tier="medium",
        provider_capabilities={"supports_native_tools": True},
        model_name="qwen/qwen3.8-27b",
        use_native_tools=True,
        is_simple_mode=False,
        build_model_constraints_block_fn=lambda _mt, _tools: "",
        build_instruction_files_block_fn=lambda: "",
        build_project_instructions_block_fn=lambda: "",
        render_tools_for_tier_fn=lambda _tools, _mt: "name: read_file\ndescription: Read a file.\n",
        build_thinking_guidance_block_fn=lambda _mt, _pc, _mn: "",
        is_reasoning_model_fn=lambda _model: False,
        build_thinking_mode_block_fn=lambda _tier, _is_rm: "",
        build_output_format_block_fn=lambda unt, ism, tier: build_output_format_block(
            use_native_tools=unt, is_simple_mode=ism, tier_str=tier
        ),
    )

    assert "<available_tools>" not in result
    assert "native JSON function calling API" in result
    assert "<think>" not in result


def test_thinking_mode_omitted_for_reasoning_models():
    assert build_thinking_mode_block(tier_str="medium", is_reasoning_model=True) == ""
    assert build_thinking_mode_block(tier_str="frontier", is_reasoning_model=True) == ""
    assert "<thinking_mode>" in build_thinking_mode_block(
        tier_str="frontier", is_reasoning_model=False
    )


def test_select_role_for_compact_and_nano_uses_small_role():
    cb = ContextBuilder()
    small = cb._select_role_for_tier("operational", "small")
    compact = cb._select_role_for_tier("operational", "compact")
    nano = cb._select_role_for_tier("operational", "nano")
    assert compact == small
    assert nano == small
    assert "Small Model" in compact


def test_compact_openai_schemas_reduce_size_without_dropping_parameters(tmp_path):
    reg = build_registry(working_dir=str(tmp_path))
    full = reg.get_openai_functions(compact=False)
    compact = reg.get_openai_functions(compact=True)

    assert len(full) == len(compact)
    full_bytes = len(json.dumps(full))
    compact_bytes = len(json.dumps(compact))
    assert compact_bytes < full_bytes

    by_name_full = {t["function"]["name"]: t["function"] for t in full}
    by_name_compact = {t["function"]["name"]: t["function"] for t in compact}
    for name, fn_full in by_name_full.items():
        fn_comp = by_name_compact[name]
        p_full = fn_full.get("parameters") or {}
        p_comp = fn_comp.get("parameters") or {}
        assert set((p_full.get("properties") or {}).keys()) == set(
            (p_comp.get("properties") or {}).keys()
        )
        assert p_full.get("required") == p_comp.get("required")
        for pname, pval in (p_full.get("properties") or {}).items():
            cval = (p_comp.get("properties") or {})[pname]
            assert pval.get("type") == cval.get("type")
            if "enum" in pval:
                assert pval["enum"] == cval.get("enum")


def test_resolve_runtime_policy_honors_provider_and_model_policy_tier_override():
    adapter = SimpleNamespace(
        name="groq",
        default_model="qwen/qwen3.8-27b",
        provider={"name": "Groq", "type": "groq", "policy_tier": "compact"},
    )
    orch = SimpleNamespace(_adapter=adapter, _provider_name="groq", model="qwen/qwen3.8-27b")
    pol = resolve_runtime_policy(orch, model="qwen/qwen3.8-27b", provider="groq")
    assert pol.policy_tier == "compact"
    assert pol.toolset_name == COMPACT_RUNTIME_POLICY.toolset_name
    assert pol.tool_limit == COMPACT_RUNTIME_POLICY.tool_limit

    # Per-model override takes precedence over provider default
    adapter2 = SimpleNamespace(
        name="custom",
        default_model="my-27b-model",
        provider={
            "name": "custom",
            "policy_tier": "standard",
            "model_policy_tiers": {"my-27b-model": "compact"},
        },
    )
    orch2 = SimpleNamespace(_adapter=adapter2, _provider_name="custom", model="my-27b-model")
    pol2 = resolve_runtime_policy(orch2, model="my-27b-model", provider="custom")
    assert pol2.policy_tier == "compact"
