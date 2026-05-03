"""
system_prompt_builder.py — Two-part, model-adaptive system prompt assembly.

Implements the SystemPromptBuilder described in docs/orchestration-gap-analysis.md
(PROMPT-01 through PROMPT-06).

Design
------
The prompt is assembled in two parts to enable prompt caching on providers that
support it (Anthropic, OpenAI). The boundary between the two parts is marked
by ``__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__`` (already used by instruction_loader.py).

Part 0 — static / cache-eligible:
  base prompt  (selected by provider_id pattern)
  + agent prompt_override (if the active agent has one — replaces the base)
  + role suffix (from role_config.py — concise behavioural addendum)
  + plan reminder (if active agent is "plan")
  + build-switch notice (if plan → build transition just occurred)
  + max-steps warning (if steps_remaining <= STEPS_WARNING_THRESHOLD)

Part 1 — dynamic / cache-busted each turn:
  environment block (model name, provider, cwd, platform, date, git branch)
  + available skills list
  + project instructions (AGENT.md discovery via instruction_loader.py)
  + git context block (branch status, staged/unstaged diff)

For internal agents (mode == "internal") only the prompt_override is returned;
no dynamic section is appended.

Usage
-----
    from src.core.prompts.system_prompt_builder import SystemPromptBuilder, PromptContext

    ctx = PromptContext(
        provider_id="anthropic",
        model_id="claude-sonnet-4-5",
        cwd=Path("/path/to/project"),
        agent=get_agent("build"),
        role="operational",
        plan_mode_active=False,
        just_switched_from_plan=False,
        steps_taken=5,
        max_steps=30,
        available_skills=["code_review", "write_tests"],
    )
    static_part, dynamic_part = SystemPromptBuilder.build(ctx)
    # Pass both parts to the LLM as a list (enables provider-side caching).
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"
_STEPS_WARNING_THRESHOLD = 3  # inject max_steps reminder when ≤ N steps remain


# ---------------------------------------------------------------------------
# PromptContext
# ---------------------------------------------------------------------------


@dataclass
class PromptContext:
    """All inputs needed to build a system prompt for one turn.

    Attributes
    ----------
    provider_id:
        Lower-case provider identifier, e.g. ``"anthropic"``, ``"openai"``,
        ``"lmstudio"``, ``"ollama"``.  Used to select the base prompt template.
    model_id:
        Full model identifier, e.g. ``"claude-sonnet-4-5"``.  Included in the
        environment block for agent self-awareness.
    cwd:
        Working directory for the session.  Used for git context and
        AGENT.md discovery.
    agent:
        The active AgentDefinition.  If None, the build agent is used.
        Import lazily to avoid circular imports.
    role:
        The active role name (e.g. ``"operational"``).  Used to append the
        role_config system_prompt_suffix.
    plan_mode_active:
        True when the current agent is in plan mode and write tools are blocked.
    just_switched_from_plan:
        True for the single turn immediately after plan → build transition.
        Causes build_switch.txt to be injected.
    steps_taken:
        Number of tool-call steps taken so far in this session.
    max_steps:
        Hard maximum steps for this session.  When ``max_steps - steps_taken``
        falls to ``_STEPS_WARNING_THRESHOLD``, the max_steps reminder is injected.
    available_skills:
        List of skill names available via load_skill().  Listed in the dynamic
        section.
    extra_dynamic_sections:
        Additional text blocks to append to the dynamic section.  Useful for
        injecting MCP server context, LSP symbols, etc.
    """

    provider_id: str = "default"
    model_id: str = ""
    cwd: Optional[Path] = None
    agent: Optional[object] = (
        None  # AgentDefinition — typed as object to avoid circular import
    )
    role: str = "operational"
    plan_mode_active: bool = False
    just_switched_from_plan: bool = False
    steps_taken: int = 0
    max_steps: int = 30
    available_skills: List[str] = field(default_factory=list)
    extra_dynamic_sections: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Template loading (with caching)
# ---------------------------------------------------------------------------

_template_cache: dict = {}


def _load_template(name: str) -> str:
    """Load a .txt template from the templates/ directory, with in-process cache."""
    if name in _template_cache:
        return _template_cache[name]
    path = _TEMPLATES_DIR / name
    if not path.exists():
        logger.warning("SystemPromptBuilder: template '%s' not found at %s", name, path)
        _template_cache[name] = ""
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
        _template_cache[name] = text
        return text
    except Exception as exc:
        logger.warning(
            "SystemPromptBuilder: failed to read template '%s': %s", name, exc
        )
        _template_cache[name] = ""
        return ""


def reload_templates() -> None:
    """Flush the template cache (useful during development / hot-reload)."""
    _template_cache.clear()


# ---------------------------------------------------------------------------
# Provider pattern → template name
# ---------------------------------------------------------------------------

_PROVIDER_TEMPLATE_MAP = [
    # (substring to match in provider_id, template filename)
    ("anthropic", "anthropic.txt"),
    ("claude", "anthropic.txt"),
    ("openai", "openai.txt"),
    ("gpt", "openai.txt"),
    ("copilot", "openai.txt"),
    ("azure", "openai.txt"),
    ("openrouter", "default.txt"),
    ("ollama", "default.txt"),
    ("lmstudio", "default.txt"),
    ("gemini", "default.txt"),
    ("groq", "default.txt"),
    ("mistral", "default.txt"),
]


def _select_base_template(provider_id: str) -> str:
    """Return the template filename appropriate for *provider_id*."""
    pid = (provider_id or "").lower()
    for fragment, template_name in _PROVIDER_TEMPLATE_MAP:
        if fragment in pid:
            return template_name
    return "default.txt"


# ---------------------------------------------------------------------------
# SystemPromptBuilder
# ---------------------------------------------------------------------------


class SystemPromptBuilder:
    """Assembles the two-part system prompt for one LLM turn.

    All methods are class-methods — no instance state is needed.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def build(cls, ctx: PromptContext) -> Tuple[str, str]:
        """Return *(static_part, dynamic_part)* for the given context.

        Both parts are non-empty strings.  Pass them as a list to the LLM
        adapter so that provider-side caching can be applied to the static part.

        For internal agents (mode == "internal") the dynamic part is empty.
        """
        # Import here to avoid circular import (agent_types imports nothing from prompts)
        from src.core.orchestration.agent_types import AgentDefinition

        agent: Optional[AgentDefinition] = ctx.agent  # type: ignore[assignment]
        is_internal = agent is not None and getattr(agent, "mode", None) == "internal"

        static_part = cls._build_static(ctx, agent, is_internal)
        dynamic_part = "" if is_internal else cls._build_dynamic(ctx, agent)

        return static_part, dynamic_part

    @classmethod
    def build_combined(cls, ctx: PromptContext) -> str:
        """Return the full prompt as a single string (for adapters that do not
        support multi-part system prompts)."""
        static_part, dynamic_part = cls.build(ctx)
        if dynamic_part:
            return f"{static_part}\n\n{_DYNAMIC_BOUNDARY}\n\n{dynamic_part}"
        return static_part

    # ------------------------------------------------------------------
    # Static section (cache-eligible)
    # ------------------------------------------------------------------

    @classmethod
    def _build_static(cls, ctx: PromptContext, agent, is_internal: bool) -> str:
        parts: List[str] = []

        # 1. Base prompt — either agent.prompt_override or provider-selected template
        if agent is not None and getattr(agent, "prompt_override", None):
            parts.append(agent.prompt_override.strip())
        elif not is_internal:
            template_name = _select_base_template(ctx.provider_id)
            base = _load_template(template_name)
            if base:
                parts.append(base)

        # 2. Role suffix (brief behavioural addendum from role_config)
        if not is_internal:
            role_suffix = cls._get_role_suffix(ctx.role)
            if role_suffix:
                parts.append(f"\n## Role: {ctx.role}\n{role_suffix}")

        # 3. Plan mode reminder
        if ctx.plan_mode_active and not is_internal:
            reminder = _load_template("plan_reminder.txt")
            if reminder:
                parts.append(f"\n{reminder}")

        # 4. Build-switch notice (one-shot, first turn after plan → build)
        if ctx.just_switched_from_plan and not is_internal:
            switch_text = _load_template("build_switch.txt")
            if switch_text:
                parts.append(f"\n{switch_text}")

        # 5. Max-steps warning
        steps_remaining = ctx.max_steps - ctx.steps_taken
        if steps_remaining <= _STEPS_WARNING_THRESHOLD and not is_internal:
            max_steps_text = _load_template("max_steps.txt")
            if max_steps_text:
                parts.append(f"\n{max_steps_text}")

        return "\n".join(p for p in parts if p).strip()

    # ------------------------------------------------------------------
    # Dynamic section (cache-busted each turn)
    # ------------------------------------------------------------------

    @classmethod
    def _build_dynamic(cls, ctx: PromptContext, agent) -> str:
        include_dynamic = True
        if agent is not None:
            include_dynamic = getattr(agent, "include_dynamic_prompt", True)

        if not include_dynamic:
            return ""

        parts: List[str] = []

        # Environment block
        env_block = cls._build_env_block(ctx)
        if env_block:
            parts.append(env_block)

        # Available skills
        if ctx.available_skills:
            skill_list = "\n".join(f"  - {s}" for s in sorted(ctx.available_skills))
            parts.append(f"<available_skills>\n{skill_list}\n</available_skills>")

        # Project instructions (AGENT.md discovery + git context)
        try:
            from src.core.orchestration.instruction_loader import build_runtime_context

            cwd = ctx.cwd or Path.cwd()
            runtime_ctx = build_runtime_context(cwd)
            if runtime_ctx:
                # instruction_loader already prepends the DYNAMIC_BOUNDARY marker;
                # strip it to avoid duplication since we include it in build_combined().
                cleaned = runtime_ctx.replace(
                    f"\n\n{_DYNAMIC_BOUNDARY}\n\n", ""
                ).strip()
                if cleaned:
                    parts.append(cleaned)
        except Exception as exc:
            logger.debug("SystemPromptBuilder: runtime context error: %s", exc)

        # Caller-supplied extra sections (e.g. MCP server list, LSP symbols)
        for section in ctx.extra_dynamic_sections:
            if section and section.strip():
                parts.append(section.strip())

        return "\n\n".join(p for p in parts if p).strip()

    # ------------------------------------------------------------------
    # Environment block
    # ------------------------------------------------------------------

    @classmethod
    def _build_env_block(cls, ctx: PromptContext) -> str:
        lines: List[str] = ["<environment>"]
        if ctx.model_id:
            lines.append(f"  model: {ctx.model_id}")
        if ctx.provider_id and ctx.provider_id != "default":
            lines.append(f"  provider: {ctx.provider_id}")
        cwd = ctx.cwd or Path.cwd()
        lines.append(f"  working_directory: {cwd}")
        _platform = platform.system().lower()
        lines.append(f"  platform: {_platform}")
        _os_info = cls._get_os_info()
        if _os_info:
            lines.append(f"  os: {_os_info}")
        lines.append(f"  date: {date.today().isoformat()}")
        git_branch = cls._get_git_branch(cwd)
        if git_branch:
            lines.append(f"  git_branch: {git_branch}")
        lines.append("</environment>")
        return "\n".join(lines)

    @staticmethod
    def _get_git_branch(cwd: Path) -> str:
        """Return the current git branch name, or empty string on failure."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _get_os_info() -> str:
        """Return OS release/version string for darwin/linux/windows."""
        import subprocess

        try:
            _plat = platform.system().lower()
            if _plat == "darwin":
                result = subprocess.run(
                    ["sw_vers", "-productVersion"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    return f"macOS {result.stdout.strip()}"
            elif _plat == "linux":
                result = subprocess.run(
                    ["lsb_release", "-ds"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if result.returncode != 0:
                    result = subprocess.run(
                        ["cat", "/etc/os-release"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        if line.startswith("PRETTY_NAME="):
                            return line.split("=", 1)[1].strip().strip('"')
            elif _plat == "windows":
                result = subprocess.run(
                    ["cmd", "/c", "ver"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Role suffix helper
    # ------------------------------------------------------------------

    @staticmethod
    def _get_role_suffix(role: str) -> str:
        """Return the system_prompt_suffix for *role* from role_config.py."""
        try:
            from src.core.orchestration.role_config import get_role_system_prompt_suffix

            return get_role_system_prompt_suffix(role) or ""
        except Exception:
            return ""
