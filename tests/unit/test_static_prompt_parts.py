from pathlib import Path

from src.core.context.static_prompt_parts import (
    build_static_system_parts,
    build_model_constraints_block,
    build_output_format_block,
    build_project_instructions_block,
    build_thinking_mode_block,
    load_prompt_partial,
    prune_tools,
    render_tools_for_tier,
    select_prompt_partial,
)


class _FakeTier:
    SMALL = "small"
    MEDIUM = "medium"

    def __call__(self, value):
        return value


def test_load_prompt_partial_reads_from_templates_dir(tmp_path):
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir()
    (templates_dir / "default.txt").write_text("hello", encoding="utf-8")

    result = load_prompt_partial(
        "default.txt",
        templates_dir,
        lambda path: path.read_text(encoding="utf-8"),
    )

    assert result == "hello"


def test_select_prompt_partial_prefers_provider_specific_template():
    def _load(name: str) -> str:
        return {
            "anthropic.txt": "anthropic template",
            "default.txt": "default template",
        }.get(name, "")

    result = select_prompt_partial(
        model_tier=None,
        provider_capabilities={"provider_family": "anthropic"},
        is_reasoning=False,
        load_partial=_load,
    )

    assert result == "anthropic template"


def test_prune_tools_keeps_core_tools_first():
    tools = [
        {"name": "custom_a"},
        {"name": "read_file"},
        {"name": "custom_b"},
        {"name": "write_file"},
    ]

    result = prune_tools(
        tools=tools,
        model_tier="small",
        model_tier_enum=_FakeTier(),
        get_tool_limit=lambda _tier: 2,
        core_tool_names=("read_file", "write_file"),
    )

    assert [tool["name"] for tool in result] == ["read_file", "write_file"]


def test_render_tools_for_small_tier_uses_first_sentence_only():
    rendered = render_tools_for_tier(
        tools=[{"name": "read_file", "description": "Read a file. Then do more."}],
        model_tier="small",
        sanitize_text=lambda text: text,
        model_tier_enum=_FakeTier(),
    )

    assert "description: Read a file." in rendered
    assert "Then do more" not in rendered


def test_render_tools_for_tier_requires_top_level_names():
    rendered = render_tools_for_tier(
        tools=[{"name": "read_file", "description": "Read a file."}],
        model_tier="medium",
        sanitize_text=lambda text: text,
        model_tier_enum=_FakeTier(),
    )

    assert rendered == "name: read_file\ndescription: Read a file.\n"


def test_build_model_constraints_block_for_small_tier():
    result = build_model_constraints_block(
        model_tier="small",
        tools=[{"name": "read_file"}],
        model_tier_enum=_FakeTier(),
        get_plan_step_limit=lambda _tier: 3,
        get_context_budget=lambda **_kwargs: 4096,
    )

    assert "<model_constraints>" in result
    assert "Context: 4,096 tokens" in result
    assert "Max plan steps: 3" in result


def test_build_project_instructions_block_formats_bullets(tmp_path):
    result = build_project_instructions_block(
        workdir=tmp_path,
        load_project_instructions=lambda _workdir: ["Keep patches small", "Run tests"],
    )

    assert "<project_config_instructions>" in result
    assert "- Keep patches small" in result
    assert "- Run tests" in result


def test_build_output_format_block_native_tools_contains_native_instruction():
    result = build_output_format_block(
        use_native_tools=True,
        is_simple_mode=False,
        tier_str="medium",
    )

    assert "native JSON function calling API" in result


def test_build_output_format_block_small_contains_json_only_instruction():
    result = build_output_format_block(
        use_native_tools=False,
        is_simple_mode=False,
        tier_str="small",
    )

    assert "Output ONLY the JSON function call" in result


def test_build_thinking_mode_block_frontier_enabled():
    result = build_thinking_mode_block(tier_str="frontier", is_reasoning_model=False)

    assert "<thinking_mode>" in result
    assert "Before every tool call" in result


def test_build_static_system_parts_assembles_expected_sections():
    result = build_static_system_parts(
        soul="Soul text",
        role_content="Role text",
        active_skills=["debug"],
        get_skill=lambda name: "Debug carefully" if name == "debug" else "",
        sanitize_text=lambda text: text,
        system_prompt_dynamic_boundary="__BOUNDARY__",
        tools=[{"name": "read_file", "description": "Read a file."}],
        model_tier="small",
        provider_capabilities={"model": "claude-3-5-sonnet"},
        model_name="claude-3-5-sonnet",
        use_native_tools=False,
        is_simple_mode=False,
        build_model_constraints_block_fn=lambda _mt, _tools: "<model_constraints>ok</model_constraints>",
        build_instruction_files_block_fn=lambda: "<project_instructions>instr</project_instructions>",
        build_project_instructions_block_fn=lambda: "<project_config_instructions>cfg</project_config_instructions>",
        render_tools_for_tier_fn=lambda _tools, _mt: "name: read_file\ndescription: Read a file.\n",
        build_thinking_guidance_block_fn=lambda _mt, _pc, _mn: "<model_guidance>guide</model_guidance>",
        is_reasoning_model_fn=lambda _model: True,
        build_thinking_mode_block_fn=lambda _tier, is_rm: "<thinking_mode>on</thinking_mode>" if is_rm else "",
        build_output_format_block_fn=lambda _unt, _ism, _tier: "<output_format>fmt</output_format>",
    )

    assert "<identity>\nSoul text\n</identity>" in result
    assert "<role>\nRole text\n</role>" in result
    assert "<active_skills>\nDebug carefully\n</active_skills>" in result
    assert "<available_tools>" in result
    assert "<model_guidance>guide</model_guidance>" in result
    assert "<thinking_mode>on</thinking_mode>" in result
    assert "<output_format>fmt</output_format>" in result
    assert "__BOUNDARY__" in result
