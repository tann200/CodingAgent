"""Prompt section assembly helpers mixin for ContextBuilder.

Extracted from ``src/core/context/context_builder.py`` (TASK-43).
"""

from __future__ import annotations

import logging
import subprocess
from datetime import date as _date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core.context.message_assembly import (
    append_task_message,
    sanitize_conversation_messages,
    truncate_conversation_to_quota,
)
from src.core.context.prompt_blocks import (
    build_repository_intelligence_block,
    build_session_context_blocks,
    build_task_prompt_content,
)
from src.core.context.retrieved_snippets import (
    build_context_controller_descriptors,
    filter_retrieved_snippets_by_budget,
)
from src.core.context.static_prompt_parts import (
    MODEL_ID_PARTIAL_MAP,
    build_instruction_files_block as _build_instruction_files_block_helper,
    build_model_constraints_block as _build_model_constraints_block_helper,
    build_output_format_block as _build_output_format_block_helper,
    build_project_instructions_block as _build_project_instructions_block_helper,
    build_thinking_guidance_block as _build_thinking_guidance_block_helper,
    build_thinking_mode_block as _build_thinking_mode_block_helper,
    load_prompt_partial as _load_prompt_partial_helper,
    prune_tools as _prune_tools_helper,
    render_tools_for_tier as _render_tools_for_tier_helper,
    select_prompt_partial as _select_prompt_partial_helper,
)

_ModelTier: Any = None
try:
    from src.core.inference.model_tiers import (
        ModelTier,
        get_plan_step_limit as _get_plan_step_limit,
        get_tool_limit as _get_tool_limit,
    )

    _ModelTier = ModelTier
except Exception:
    _get_tool_limit = None  # type: ignore[assignment]
    _get_plan_step_limit = None  # type: ignore[assignment]

try:
    from src.core.context.instruction_files import (
        discover_instruction_files as _discover_instruction_files,
        render_instruction_files as _render_instruction_files,
    )
except Exception:
    _discover_instruction_files = None  # type: ignore[assignment]
    _render_instruction_files = None  # type: ignore[assignment]

try:
    from src.core.orchestration.instruction_loader import (
        load_project_instructions as _load_project_instructions,
    )
except Exception:
    _load_project_instructions = None  # type: ignore[assignment]

try:
    from src.core.inference.provider_context import (
        get_context_budget as _get_context_budget,
    )
except Exception:
    _get_context_budget = None  # type: ignore[assignment]

try:
    from src.core.inference.thinking_utils import (
        is_reasoning_model as _is_reasoning_model,
    )
except Exception:
    _is_reasoning_model = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file",
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "bash",
        "bash_readonly",
        "grep",
        "glob",
        "search_code",
        "list_directory",
    }
)
_MIN_TASK_STATE_CHARS: int = 60
_MIN_TODO_CHARS: int = 20
_MIN_PREFS_CHARS: int = 10


def _today_iso() -> str:
    """Return today's date as YYYY-MM-DD (local time)."""
    return _date.today().isoformat()


class _ContextPromptSectionsMixin:
    """Provides prompt partial selection, tool rendering, and dynamic/message section assembly."""

    _agent_context_dir: Path
    working_dir: Path
    token_estimator: Callable[[str], int]

    def _read_text_cached(self, path: Path) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def _sanitize_text(self, text: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def _truncate_text(self, text: str, max_tokens: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def _truncate_to_token_budget(self, text: str, max_tokens: int) -> str:  # pragma: no cover
        raise NotImplementedError

    def _get_task_state_content(self) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def _get_todo_content(self) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def _get_preferences_content(self) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def _get_past_mistakes(self, task_description: str, limit: int = 4) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def _get_summary_cache(self) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def _get_provider_variant(model_name: str) -> Optional[str]:  # pragma: no cover
        raise NotImplementedError

    def _prune_stale_tool_outputs(
        self, conversation: List[Dict], current_step_hint: Optional[str] = None
    ) -> List[Dict]:  # pragma: no cover
        raise NotImplementedError

    def inject_prior_session_memories(self, task: str, limit: int = 3) -> str:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------
    # S1-B / S1-C helpers
    # ------------------------------------------------------------------

    _TEMPLATES_DIR: Path = Path(__file__).parent.parent / "prompts" / "templates"

    def _load_prompt_partial(self, filename: str) -> str:
        """Return the content of a prompt partial from the templates directory.

        S1-B: Used to inject provider/tier-specific guidance into the system prompt.
        Returns empty string when the file does not exist (silent no-op).
        """
        return _load_prompt_partial_helper(
            filename,
            self._TEMPLATES_DIR,
            self._read_text_cached,
        )

    # GAP-FRONTIER-1: model-ID → partial file mapping (checked before provider family).
    # Keys are regex patterns matched against the active model ID string.
    _MODEL_ID_PARTIAL_MAP: List[Tuple[str, str]] = MODEL_ID_PARTIAL_MAP

    def _select_prompt_partial(
        self,
        model_tier: Optional[str],
        provider_capabilities: Optional[Dict],
        is_reasoning: bool = False,
    ) -> str:
        """S1-B + GAP-FRONTIER-1: Choose the right prompt partial for the active model.

        Selection priority:
        1. Reasoning model (o1/o3/o4/qwen3/deepseek-r1) → beast.txt (unchanged)
        2. Model ID match → model-specific partial (GAP-FRONTIER-1)
        3. Provider family: anthropic → anthropic.txt
        4. Provider family: gemini   → gemini.txt
        5. Provider family: openai / openrouter → openai.txt
        6. Tier: NANO/SMALL → local-small.md, MEDIUM → local-medium.md
        7. Default → default.txt
        """
        return _select_prompt_partial_helper(
            model_tier=model_tier,
            provider_capabilities=provider_capabilities,
            is_reasoning=is_reasoning,
            load_partial=self._load_prompt_partial,
            model_id_partial_map=self._MODEL_ID_PARTIAL_MAP,
        )

    @staticmethod
    def _prune_tools(tools: List[Dict], model_tier: Optional[str]) -> List[Dict]:
        """S1-C: Limit the tool list based on model tier.

        Core tools (read/write/edit/bash/grep/glob/search) are always kept first.
        Non-core tools are appended up to the tier limit, then dropped from the tail.
        """
        return _prune_tools_helper(
            tools=tools,
            model_tier=model_tier,
            model_tier_enum=_ModelTier,
            get_tool_limit=_get_tool_limit,
            core_tool_names=tuple(_CORE_TOOL_NAMES),
        )

    def _render_tools_for_tier(
        self, tools: List[Dict], model_tier: Optional[str]
    ) -> str:
        """S8-A: Render tools list with verbosity appropriate to the model tier.

        NANO/SMALL: name + first-sentence description only (minimal tokens).
        MEDIUM+:    full description (unchanged behaviour).
        """
        return _render_tools_for_tier_helper(
            tools=tools,
            model_tier=model_tier,
            sanitize_text=self._sanitize_text,
            model_tier_enum=_ModelTier,
        )

    # ------------------------------------------------------------------
    # P3-A: Two-tier system prompt cache helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_git_head(working_dir: str) -> str:
        """Return the current git HEAD SHA (short) for *working_dir*, or '' on error.

        P3-A: Used as part of the dynamic-tier cache key so the env block is
        regenerated whenever HEAD changes (e.g. after a commit or checkout) but
        stays cached within the same HEAD across multiple turns.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as exc:
            logger.debug("context_builder: git branch detection failed: %s", exc)
        return ""

    def _build_model_constraints_block(
        self, model_tier: Optional[str], tools: Any
    ) -> str:
        """Return the <model_constraints> block for NANO/SMALL tiers, or ''."""
        return _build_model_constraints_block_helper(
            model_tier=model_tier,
            tools=tools,
            model_tier_enum=_ModelTier,
            get_plan_step_limit=_get_plan_step_limit,
            get_context_budget=_get_context_budget,
        )

    def _build_instruction_files_block(self, model_tier: Optional[str] = None) -> str:
        """Return <project_instructions> block from ancestor .md files, or ''."""
        return _build_instruction_files_block_helper(
            workdir=self._agent_context_dir.parent,
            discover_instruction_files=_discover_instruction_files,  # type: ignore[arg-type]
            render_instruction_files=_render_instruction_files,  # type: ignore[arg-type]
            model_tier=model_tier,
        )

    def _build_project_instructions_block(self) -> str:
        """Return <project_config_instructions> block, skipping duplicate AGENTS.md loading.

        ``_build_instruction_files_block`` is the canonical, nonce-fenced loader for
        ``AGENTS.md`` / ``CLAUDE.md`` / ``ARCHITECTURE.md``. Only fall back to
        ``instruction_loader.load_project_instructions`` when ``instruction_files``
        discovery is unavailable so ``AGENTS.md`` is never injected twice.
        """
        if _discover_instruction_files is not None and _render_instruction_files is not None:
            return ""
        return _build_project_instructions_block_helper(
            workdir=self._agent_context_dir.parent,
            load_project_instructions=_load_project_instructions,
        )

    def _build_output_format_block(
        self, use_native_tools: bool, is_simple_mode: bool, tier_str: str
    ) -> str:
        """Return the <output_format> block appropriate to the model tier."""
        return _build_output_format_block_helper(
            use_native_tools=use_native_tools,
            is_simple_mode=is_simple_mode,
            tier_str=tier_str,
        )

    def _build_thinking_guidance_block(
        self,
        model_tier: Optional[str],
        provider_capabilities: Optional[Dict],
        model_name: str,
    ) -> str:
        """Return the <model_guidance> block for reasoning models, or ''."""
        return _build_thinking_guidance_block_helper(
            model_tier=model_tier,
            provider_capabilities=provider_capabilities,
            is_reasoning_model_fn=_is_reasoning_model,
            select_prompt_partial_fn=lambda mt, pc, ir: self._select_prompt_partial(
                mt, pc, ir  # type: ignore[arg-type]
            ),
            model_name=model_name,
        )

    def _build_thinking_mode_block(
        self, tier_str: str, _is_reasoning_model: bool
    ) -> str:
        """Return the <thinking_mode> block for frontier/large/reasoning models, or ''."""
        return _build_thinking_mode_block_helper(
            tier_str=tier_str,
            is_reasoning_model=_is_reasoning_model,
        )


    def _prepare_repository_context_block(
        self,
        *,
        retrieved_snippets: Optional[List[Dict]],
        context_controller,
        conversation: List[Dict],
        static_prefix: str,
        dynamic_parts: List[str],
        conversation_quota: Optional[int] = None,
    ) -> Tuple[Optional[List[Dict]], str]:
        """Return budget-filtered retrieved snippets and the rendered repo block."""
        if not retrieved_snippets:
            return retrieved_snippets, ""

        if context_controller is not None:
            try:
                _sys_text = static_prefix + "\n\n".join(dynamic_parts)
                _file_descs = build_context_controller_descriptors(retrieved_snippets)
                try:
                    _included, _excluded = context_controller.enforce_budget(
                        _file_descs,
                        conversation,
                        _sys_text,
                        conversation_quota=conversation_quota,
                    )
                except TypeError:
                    _included, _excluded = context_controller.enforce_budget(
                        _file_descs, conversation, _sys_text
                    )
                if _excluded:
                    logging.getLogger(__name__).debug(
                        "ContextController: excluded %d snippet(s) to fit budget",
                        len(_excluded),
                    )
                retrieved_snippets = filter_retrieved_snippets_by_budget(
                    retrieved_snippets,
                    included_descriptors=_included,
                )
            except Exception as exc:
                logger.debug("context_builder: snippet budget enforcement failed: %s", exc)  # never fail prompt build due to budget enforcement

        if not retrieved_snippets:
            return retrieved_snippets, ""

        try:
            summary_cache = self._get_summary_cache()
            repo_block = build_repository_intelligence_block(
                retrieved_snippets=retrieved_snippets,
                summary_cache=summary_cache,
                sanitize_text=self._sanitize_text,
            )
            return retrieved_snippets, repo_block or ""
        except Exception as exc:
            logger.debug("context_builder: repository context block failed: %s", exc)
            return retrieved_snippets, ""

    def _build_dynamic_prompt_parts(
        self,
        *,
        conversation: List[Dict],
        task_description: str,
        static_prefix: str,
        retrieved_snippets: Optional[List[Dict]],
        context_controller,
        include_prior_context: bool,
    ) -> List[str]:
        """Assemble the dynamic, per-turn sections appended after the static prefix."""
        safe_task_description = self._sanitize_text(task_description)
        dynamic_parts: List[str] = []

        dynamic_parts.extend(
            build_session_context_blocks(
                conversation=conversation,
                task_description=safe_task_description,
                get_task_state_content=self._get_task_state_content,
                get_todo_content=self._get_todo_content,
                get_preferences_content=self._get_preferences_content,
                get_past_mistakes=self._get_past_mistakes,
                min_task_state_chars=_MIN_TASK_STATE_CHARS,
                min_todo_chars=_MIN_TODO_CHARS,
                min_prefs_chars=_MIN_PREFS_CHARS,
            )
        )

        if include_prior_context:
            try:
                prior_context_block = self.inject_prior_session_memories(
                    task=task_description,
                    limit=3,
                )
                if prior_context_block:
                    dynamic_parts.insert(0, prior_context_block)
            except Exception as exc:
                logger.debug("context_builder: prior session memory injection failed: %s", exc)

        _, repo_block = self._prepare_repository_context_block(
            retrieved_snippets=retrieved_snippets,
            context_controller=context_controller,
            conversation=conversation,
            static_prefix=static_prefix,
            dynamic_parts=dynamic_parts,
        )
        if repo_block:
            dynamic_parts.append(repo_block)

        # CP-10: LSP context injection — append workspace symbol context when
        # the feature is enabled (config: lsp_context.enabled or env var
        # CODINGAGENT_LSP_CONTEXT=1).  Returns an empty string when disabled
        # or when the symbol index is absent, so this is always a no-op by
        # default and never blocks prompt assembly.
        try:
            from src.core.indexing.lsp_context import (  # type: ignore[import]
                get_lsp_context_block,
            )

            _lsp_block = get_lsp_context_block(workdir=self._agent_context_dir.parent)
            if _lsp_block:
                dynamic_parts.append(_lsp_block)

            try:
                from src.core.indexing.lsp_context import (  # type: ignore[import]
                    get_lsp_diagnostics_block,
                )

                _lsp_diag_block = get_lsp_diagnostics_block(
                    workdir=self._agent_context_dir.parent
                )
                if _lsp_diag_block:
                    dynamic_parts.append(_lsp_diag_block)
            except ImportError:
                pass
        except Exception as exc:
            logger.debug("context_builder: LSP context block failed: %s", exc)  # never fail prompt build due to LSP errors

        return dynamic_parts

    def _append_conversation_and_task_messages(
        self,
        *,
        built_messages: List[Dict[str, str]],
        conversation: List[Dict],
        task_description: str,
        conversation_quota: int,
        use_native_tools: bool = False,
    ) -> List[Dict[str, str]]:
        """Append filtered conversation history and the final task user message."""
        pruned_conversation = self._prune_stale_tool_outputs(
            list(conversation),
            current_step_hint=task_description[:120] if task_description else None,
        )

        filtered_conv = sanitize_conversation_messages(
            conversation=pruned_conversation,
            sanitize_text=self._sanitize_text,
        )

        truncated_conversation = truncate_conversation_to_quota(
            conversation=filtered_conv,
            conversation_quota=conversation_quota,
            token_estimator=self.token_estimator,
        )

        prompt_content = build_task_prompt_content(
            self._sanitize_text(task_description),
            _today_iso(),
            use_native_tools=use_native_tools,
        )
        return append_task_message(
            built_messages=built_messages,
            truncated_conversation=truncated_conversation,
            task_prompt_content=prompt_content,
        )

