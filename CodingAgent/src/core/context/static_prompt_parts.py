from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


MODEL_ID_PARTIAL_MAP: List[Tuple[str, str]] = [
    (r"o1|o3|o4", "openai-reasoning.md"),
    (r"gpt-4o|gpt-4\.5|gpt-4-turbo", "openai-frontier.md"),
    (
        r"claude-opus|claude-3-7|claude-sonnet-4-[5-9]|claude-3-5-sonnet",
        "anthropic-frontier.md",
    ),
    (r"claude-haiku|claude-3-5-haiku|claude-3-haiku", "anthropic-small.md"),
    (r"gemini-2\.5-pro|gemini-pro|gemini-ultra", "gemini-frontier.md"),
    (r"gemini-flash|gemini-nano|gemini-2\.0-flash", "gemini-small.md"),
    (r"gemma-4-31b|gemma-4-26b|gemma4:31b|gemma4:26b", "gemini-frontier.md"),
    (r"gemma-4-e[24]b|gemma4:e[24]b|gemma4-e[24]b", "local-small.md"),
]


def load_prompt_partial(
    filename: str,
    templates_dir: Path,
    read_text_cached: Callable[[Path], Optional[str]],
) -> str:
    return read_text_cached(templates_dir / filename) or ""


def select_prompt_partial(
    *,
    model_tier: Optional[str],
    provider_capabilities: Optional[Mapping[str, object]],
    is_reasoning: bool,
    load_partial: Callable[[str], str],
    model_id_partial_map: Sequence[Tuple[str, str]] = MODEL_ID_PARTIAL_MAP,
) -> str:
    if is_reasoning:
        partial = load_partial("beast.txt")
        if partial:
            return partial

    caps = provider_capabilities or {}
    active_model = str(caps.get("model", "")).lower()
    if active_model:
        for pattern, filename in model_id_partial_map:
            if re.search(pattern, active_model):
                partial = load_partial(filename)
                if partial:
                    return partial
                break

    provider_family = str(caps.get("provider_family", "")).lower()
    if "anthropic" in provider_family:
        partial = load_partial("anthropic.txt")
        if partial:
            return partial

    if "gemini" in provider_family:
        partial = load_partial("gemini.txt")
        if partial:
            return partial

    if "openai" in provider_family or "openrouter" in provider_family:
        partial = load_partial("openai.txt")
        if partial:
            return partial

    tier = (model_tier or "").lower()
    if tier == "small":
        partial = load_partial("local-small.md")
        if partial:
            return partial
    elif tier == "medium":
        partial = load_partial("local-medium.md")
        if partial:
            return partial

    return load_partial("default.txt")


def prune_tools(
    *,
    tools: List[Dict],
    model_tier: Optional[str],
    model_tier_enum: Any,
    get_tool_limit: Optional[Callable[[Any], int]],
    core_tool_names: Sequence[str],
) -> List[Dict]:
    try:
        if model_tier_enum is None or get_tool_limit is None:
            raise ImportError("model_tiers unavailable")
        tier = model_tier_enum(model_tier) if model_tier else model_tier_enum.MEDIUM
        limit = get_tool_limit(tier)
    except Exception:
        return tools

    if len(tools) <= limit:
        return tools

    core = [tool for tool in tools if tool.get("name") in core_tool_names]
    supplementary = [tool for tool in tools if tool.get("name") not in core_tool_names]
    return (core + supplementary)[:limit]


def render_tools_for_tier(
    *,
    tools: List[Dict],
    model_tier: Optional[str],
    sanitize_text: Callable[[str], str],
    model_tier_enum: Any,
) -> str:
    try:
        if model_tier_enum is None:
            raise ImportError("model_tiers unavailable")
        tier = model_tier_enum(model_tier) if model_tier else model_tier_enum.MEDIUM
        is_minimal = tier == model_tier_enum.SMALL
    except Exception:
        is_minimal = False

    lines: List[str] = []
    for tool in tools:
        desc = sanitize_text(tool.get("description", ""))
        if is_minimal:
            first_sentence = desc.split(".")[0].strip()
            if first_sentence:
                desc = first_sentence + "."
        lines.append(f"name: {tool['name']}\ndescription: {desc}")
    return "\n".join(lines) + "\n" if lines else ""


def build_model_constraints_block(
    *,
    model_tier: Optional[str],
    tools: Sequence[Any],
    model_tier_enum: Any,
    get_plan_step_limit: Optional[Callable[[Any], int]],
    get_context_budget: Optional[Callable[..., int]],
) -> str:
    tier_str = (model_tier or "").lower()
    if tier_str not in ("nano", "small"):
        return ""
    try:
        if model_tier_enum is None or get_plan_step_limit is None:
            raise ImportError("model_tiers unavailable")
        tier_enum = model_tier_enum(tier_str) if tier_str else None
        step_limit = get_plan_step_limit(tier_enum) if tier_enum else 6
        context_tokens = 0
        if get_context_budget is not None:
            try:
                context_tokens = get_context_budget(model_tier=tier_str)
            except Exception as exc:
                logger.debug("static_prompt_parts: get_context_budget failed: %s", exc)
        lines = [
            f"Tier: {tier_str.upper()} | Context: {context_tokens:,} tokens | Tools: {len(tools)} available",
            f"Max plan steps: {step_limit} | Output format: JSON function call (required, no YAML)",
            "Not available: parallel tool calls, subagent delegation, extended reasoning",
        ]
        return "<model_constraints>\n" + "\n".join(lines) + "\n</model_constraints>"
    except Exception as exc:
        logger.debug("static_prompt_parts: build_model_constraints_block failed: %s", exc)
        return ""


def build_instruction_files_block(
    *,
    workdir: Path,
    discover_instruction_files: Optional[Callable[[Path], Sequence[Path]]],
    render_instruction_files: Optional[Callable[[Sequence[Path]], str]],
) -> str:
    try:
        if discover_instruction_files is None or render_instruction_files is None:
            raise ImportError("instruction_files unavailable")
        instruction_files = discover_instruction_files(workdir)
        if instruction_files:
            instruction_block = render_instruction_files(instruction_files)
            if instruction_block:
                return f"<project_instructions>\n{instruction_block}\n</project_instructions>"
    except Exception as exc:
        logger.debug("static_prompt_parts: instruction files block failed: %s", exc)
    return ""


def build_project_instructions_block(
    *,
    workdir: Path,
    load_project_instructions: Optional[Callable[[Path], Sequence[str]]],
) -> str:
    try:
        if load_project_instructions is None:
            raise ImportError("instruction_loader unavailable")
        project_instructions = load_project_instructions(workdir)
        if project_instructions:
            instruction_block = "\n".join(
                f"- {instruction}" for instruction in project_instructions
            )
            return f"<project_config_instructions>\n{instruction_block}\n</project_config_instructions>"
    except Exception as exc:
        logger.debug("static_prompt_parts: project instructions block failed: %s", exc)
    return ""


def build_output_format_block(
    *,
    use_native_tools: bool,
    is_simple_mode: bool,
    tier_str: str,
) -> str:
    if use_native_tools:
        return (
            "<output_format>\n"
            "You MUST think step-by-step. Write your internal reasoning inside <think> tags.\n"
            "You have access to native tools. Use the native JSON function calling API.\n"
            "Do NOT output markdown code blocks for tool calls.\n"
            "IMPORTANT: Call tools using the native function calling format.\n"
            "After executing a tool, your response will include the tool's result.\n"
            "If the tool result completes the user's task, do NOT make more tool calls.\n"
            "Simply summarize the result or indicate task completion.\n"
            "Only call another tool if the result requires follow-up action.\n"
            "</output_format>"
        )
    if is_simple_mode:
        return (
            "<output_format>\n"
            "STRICT RULE: Output EXACTLY ONE tool call per response, no exceptions.\n"
            "Use the JSON function calling format:\n"
            '{"name": "the_tool_name", "arguments": {"arg_name": "arg_value"}}\n'
            "Do NOT output more than one tool call. Do NOT chain tool calls.\n"
            "After the tool result is returned, you may call one more tool if needed.\n"
            "</output_format>"
        )
    if tier_str == "small":
        return (
            "<output_format>\n"
            "Output ONLY the JSON function call. No explanation, no extra text.\n"
            '{"name": "tool_name", "arguments": {"arg": "value"}}\n'
            "</output_format>"
        )
    return (
        "<output_format>\n"
        "To execute an action, you MUST use the provided markdown YAML tool format:\n"
        "```yaml\n"
        "name: tool_name\n"
        "arguments:\n"
        "  arg_name: arg_value\n"
        "```\n"
        "Do not use JSON for tool calls in this mode.\n"
        "</output_format>"
    )


def build_thinking_guidance_block(
    *,
    model_tier: Optional[str],
    provider_capabilities: Optional[Mapping[str, object]],
    is_reasoning_model_fn: Optional[Callable[[str], bool]],
    select_prompt_partial_fn: Callable[[Optional[str], Optional[Mapping[str, object]], bool], str],
) -> str:
    caps = provider_capabilities or {}
    is_reasoning_model = False
    try:
        if is_reasoning_model_fn is None:
            raise ImportError("thinking_utils unavailable")
        active_model = str(caps.get("model", ""))
        is_reasoning_model = bool(active_model and is_reasoning_model_fn(active_model))
    except Exception as exc:
        logger.debug("static_prompt_parts: is_reasoning_model check failed: %s", exc)
    partial = select_prompt_partial_fn(model_tier, provider_capabilities, is_reasoning_model)
    if partial:
        return f"<model_guidance>\n{partial}\n</model_guidance>"
    return ""


def build_thinking_mode_block(*, tier_str: str, is_reasoning_model: bool) -> str:
    if tier_str in ("frontier", "large") or is_reasoning_model:
        return (
            "<thinking_mode>\n"
            "Before every tool call, briefly state:\n"
            "1. What you expect this call to return.\n"
            "2. What you will do if it fails or returns unexpected output.\n"
            "This reflection is mandatory - do not skip it.\n"
            "</thinking_mode>"
        )
    return ""


def build_static_system_parts(
    *,
    soul: str,
    role_content: str,
    active_skills: Sequence[str],
    get_skill: Callable[[str], str],
    sanitize_text: Callable[[str], str],
    system_prompt_dynamic_boundary: str,
    tools: List[Dict],
    model_tier: Optional[str],
    provider_capabilities: Optional[Mapping[str, object]],
    model_name: str,
    use_native_tools: bool,
    is_simple_mode: bool,
    build_model_constraints_block_fn: Callable[[Optional[str], Sequence[object]], str],
    build_instruction_files_block_fn: Callable[[], str],
    build_project_instructions_block_fn: Callable[[], str],
    render_tools_for_tier_fn: Callable[[List[Dict], Optional[str]], str],
    build_thinking_guidance_block_fn: Callable[[Optional[str], Optional[Mapping[str, object]], str], str],
    is_reasoning_model_fn: Optional[Callable[[str], bool]],
    build_thinking_mode_block_fn: Callable[[str, bool], str],
    build_output_format_block_fn: Callable[[bool, bool, str], str],
) -> str:
    parts: List[str] = []
    tier_str = (model_tier or "").lower()

    parts.append(f"<identity>\n{sanitize_text(soul)}\n</identity>")
    parts.append(f"<role>\n{sanitize_text(role_content)}\n</role>")

    model_constraints = build_model_constraints_block_fn(model_tier, tools)
    if model_constraints:
        parts.append(model_constraints)

    instruction_files_block = build_instruction_files_block_fn()
    if instruction_files_block:
        parts.append(instruction_files_block)

    project_instructions_block = build_project_instructions_block_fn()
    if project_instructions_block:
        parts.append(project_instructions_block)

    parts.append(system_prompt_dynamic_boundary)

    if active_skills:
        skill_contents = [
            sanitize_text(skill_content)
            for skill_name in active_skills
            if (skill_content := get_skill(skill_name))
        ]
        if skill_contents:
            parts.append(
                f"<active_skills>\n{chr(10).join(skill_contents)}\n</active_skills>"
            )

    tools_text = render_tools_for_tier_fn(tools, model_tier)
    parts.append(f"<available_tools>\n{tools_text}\n</available_tools>")

    thinking_guidance = build_thinking_guidance_block_fn(
        model_tier,
        provider_capabilities,
        model_name,
    )
    if thinking_guidance:
        parts.append(thinking_guidance)

    caps = provider_capabilities or {}
    is_reasoning_model = False
    try:
        if is_reasoning_model_fn is not None:
            active_model = str(caps.get("model", ""))
            is_reasoning_model = bool(
                active_model and is_reasoning_model_fn(active_model)
            )
    except Exception as exc:
        logger.debug("static_prompt_parts: is_reasoning_model check (2) failed: %s", exc)

    thinking_mode = build_thinking_mode_block_fn(tier_str, is_reasoning_model)
    if thinking_mode:
        parts.append(thinking_mode)

    parts.append(
        build_output_format_block_fn(use_native_tools, is_simple_mode, tier_str)
    )

    return "\n\n".join(parts)
