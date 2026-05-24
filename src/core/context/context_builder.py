from __future__ import annotations
from typing import Callable, Dict, List, Optional, Tuple
import logging
import math
import json
import subprocess
import threading
from collections import OrderedDict
from datetime import date as _date
from pathlib import Path

from src.core.memory.frozen_snapshot import get_memory_for_prompt
from src.core.context.prompt_blocks import (
    build_repository_intelligence_block,
    build_session_context_blocks,
    build_task_prompt_content,
)
from src.core.context.prompt_cache import (
    compute_static_prompt_cache_key,
    get_static_prompt_cache_entry,
    store_static_prompt_cache_entry,
)
from src.core.context.message_assembly import (
    append_task_message,
    sanitize_conversation_messages,
    truncate_conversation_to_quota,
)
from src.core.context.token_truncation import (
    truncate_text_to_max_tokens,
    truncate_to_token_budget,
)
from src.core.context.retrieved_snippets import (
    build_context_controller_descriptors,
    filter_retrieved_snippets_by_budget,
)
from src.core.context.sanitization import sanitize_prompt_text
from src.core.context.tool_output_pruning import prune_stale_tool_outputs
from src.core.context.agent_brain_loading import (
    load_prompt_directory,
    merge_workspace_skill_overrides,
)
from src.core.context.static_prompt_parts import (
    MODEL_ID_PARTIAL_MAP,
    build_static_system_parts as _build_static_system_parts_helper,
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

# Gap 3: Plugin hooks — lazy import so the registry is not required at import time.
try:
    from src.core.plugin.hook_registry import (
        registry as _hook_registry,
        HOOK_CONTEXT_BUILT as _HOOK_CONTEXT_BUILT,
    )

    _HAS_HOOKS = True
except Exception:
    _hook_registry = None  # type: ignore[assignment]
    _HOOK_CONTEXT_BUILT = "context.built"
    _HAS_HOOKS = False

# Lazy imports — guarded against circular-import and optional-dependency failures.
try:
    from src.core.inference.model_tiers import (
        ModelTier,
        get_tool_limit as _get_tool_limit,
        get_plan_step_limit as _get_plan_step_limit,
    )

    _ModelTier = ModelTier
except Exception:
    _ModelTier = None  # type: ignore[assignment]
    _get_tool_limit = None  # type: ignore[assignment]
    _get_plan_step_limit = None  # type: ignore[assignment]

try:
    from src.core.inference.provider_context import (
        get_context_budget as _get_context_budget,
    )
except Exception:
    _get_context_budget = None  # type: ignore[assignment]

try:
    from src.core.indexing.vector_store import VectorStore as _VectorStore
except Exception:
    _VectorStore = None  # type: ignore[assignment]

try:
    from src.core.context.instruction_files import (
        load_instruction_files as _load_instruction_files,
        get_instruction_summary as _get_instruction_summary,
        discover_instruction_files as _discover_instruction_files,
        render_instruction_files as _render_instruction_files,
    )
except Exception:
    _load_instruction_files = None  # type: ignore[assignment]
    _get_instruction_summary = None  # type: ignore[assignment]
    _discover_instruction_files = None  # type: ignore[assignment]
    _render_instruction_files = None  # type: ignore[assignment]

try:
    from src.core.orchestration.instruction_loader import (
        load_instructions as _load_instructions,
        load_project_instructions as _load_project_instructions,
    )
except Exception:
    _load_instructions = None  # type: ignore[assignment]
    _load_project_instructions = None  # type: ignore[assignment]

try:
    from src.core.inference.thinking_utils import (
        is_reasoning_model as _is_reasoning_model,
    )
except Exception:
    _is_reasoning_model = None  # type: ignore[assignment]

try:
    from src.core.inference.tokenizer import count_tokens as _count_tokens_mod
except Exception:
    _count_tokens_mod = None  # type: ignore[assignment]

try:
    from src.tools.tools_config import agent_context_path as _agent_context_path
except Exception:
    _agent_context_path = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# CP-12: Sentinel string that marks the boundary between the static (cacheable)
# and dynamic (per-turn) portions of the system prompt.
#
# The Anthropic adapter splits the system prompt on this sentinel and sends the
# static portion with ``cache_control: {"type": "ephemeral"}`` so that the
# prompt prefix is eligible for Anthropic's prompt caching.  Other providers
# ignore the sentinel (it is stripped before the prompt reaches the model).
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

# Tools always retained when pruning supplementary tools from the context.
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


# F10: Import dynamic token budget helper (lazy — avoids circular imports at module load).
def _default_max_tokens() -> int:
    try:
        if _get_context_budget is not None:
            return _get_context_budget()
    except Exception as exc:
        logger.debug("context_builder: _default_max_tokens failed: %s", exc)
    return 6000


# Module-level caches keyed by absolute file path (NEW-20).
# ContextBuilder is re-instantiated on every node call, so instance-level caches
# were always empty and provided zero benefit.  Module-level caches persist across
# calls as long as the process is alive and the file has not changed on disk.
# F15: Use OrderedDict with a max-size cap to prevent unbounded memory growth.
# SCAN2-6: Protect all multi-step cache mutations (move_to_end / popitem / setitem)
# with a single lock — these sequences are not atomic under the GIL.
_TEXT_CACHE: OrderedDict = OrderedDict()  # path → (mtime, content); max 256 entries
_JSON_CACHE: OrderedDict = OrderedDict()  # path → (mtime, parsed);  max 256 entries
_CACHE_MAX = 256
_CACHE_LOCK = threading.Lock()

# P3-A: Two-tier system prompt cache.
#
# Tier 1 — STATIC: SOUL.md + role + skills + tools + prompt partial.
#   Key:   (role_name, active_skills_tuple, tools_hash, model_tier, provider_family,
#            provider_model, requested_model, use_native_tools, is_simple_mode)
#   Value: str — the joined static prefix (everything before DYNAMIC_BOUNDARY).
#   Lifetime: process-lifetime, cleared by ContextBuilder.clear_cache().
#
# Tier 2 — DYNAMIC: env block (date + git HEAD).
#   Key:   (working_dir_str, date_iso, git_head)
#   Value: str — the dynamic env fragment.
#   Lifetime: process-lifetime (tiny; at most one entry per working-dir per day).
#
# Both caches use the same _CACHE_LOCK for safety.
_STATIC_PROMPT_CACHE: Dict[Tuple, str] = {}
_DYNAMIC_ENV_CACHE: Dict[Tuple, str] = {}

# Token / character thresholds
_SYSTEM_OVERHEAD_TOKENS: int = (
    2100  # reserved for system prompt overhead in token budget
)
_MIN_TASK_STATE_CHARS: int = 60  # min chars before injecting task-state block
_MIN_TODO_CHARS: int = 20  # min chars before injecting todo block
_MIN_PREFS_CHARS: int = 10  # min chars before injecting preferences block


def _get_ctx_name() -> str:
    """Return the configured context-dir name, defaulting to '.codingAgent'."""
    try:
        from src.tools.tools_config import get_context_dir_name

        return get_context_dir_name()
    except Exception:
        return ".codingAgent"


def _today_iso() -> str:
    """Return today's date as YYYY-MM-DD (local time)."""
    return _date.today().isoformat()


class ContextBuilder:
    @classmethod
    def clear_cache(cls) -> None:
        """PB-2 fix: Invalidate the module-level file caches.

        Call this at the start of each new task so role/skill YAML files that were
        modified on disk between tasks are re-read rather than served from a stale
        cache entry.  The caches persist for the lifetime of the process, so without
        explicit invalidation a change to agent-brain files would be invisible until
        restart.

        P3-A: Also clears the static and dynamic prompt caches so that prompt partial
        changes and tool-list changes are picked up immediately at task boundaries.
        """
        with _CACHE_LOCK:
            _TEXT_CACHE.clear()
            _JSON_CACHE.clear()
            _STATIC_PROMPT_CACHE.clear()
            _DYNAMIC_ENV_CACHE.clear()

    @classmethod
    def invalidate_path(cls, path: str) -> None:
        """MEM-1: Remove a specific path from the text and JSON caches.

        Call this immediately after any file write so the next ContextBuilder
        instantiation re-reads the file from disk rather than serving the
        pre-write cached content.  Prevents stale file content being injected
        into the system prompt during active editing sessions.
        """
        key = str(Path(path).resolve())
        with _CACHE_LOCK:
            _TEXT_CACHE.pop(key, None)
            _JSON_CACHE.pop(key, None)

    def __init__(
        self,
        token_estimator: Optional[Callable[[str], int]] = None,
        working_dir: Optional[str] = None,
        max_tokens: int = 6000,
    ):
        # D-06/S0-A: Use accurate tiktoken-based estimator; fall back to len/3.5
        # if the caller supplies a custom estimator, honour it (test override path).
        if token_estimator is not None:
            self.token_estimator: Callable[[str], int] = token_estimator
        else:
            if _count_tokens_mod is not None:
                self.token_estimator = _count_tokens_mod
            else:
                self.token_estimator = lambda s: math.ceil(len(s) / 3.5)
        # Resolve working directory — use provided path; do NOT fall back to
        # Path.cwd() because cwd is unreliable in async/subprocess contexts and
        # would silently look for agent-brain files in the wrong location.
        # Callers that do not have a working_dir should pass the repo root
        # explicitly (NEW-10 fix).
        if _agent_context_path is not None:
            if working_dir:
                self._agent_context_dir = _agent_context_path(Path(working_dir))
            else:
                self._agent_context_dir = _agent_context_path(Path.cwd())
        else:
            self._agent_context_dir = (
                Path(working_dir if working_dir else Path.cwd()) / _get_ctx_name()
            )

        self._max_tokens: int = max_tokens

        # Load identity and role prompts from agent-brain
        self._load_agent_brain()

    def _load_agent_brain(self) -> None:
        """Load identity, role, and skill prompts from agent-brain configuration.

        D-08: All file reads go through _read_text_cached so repeated
        ContextBuilder instantiations (one per node call) hit the mtime-keyed
        module-level cache rather than re-reading from disk every turn.
        """
        config_root = Path(__file__).parent.parent.parent / "config" / "agent-brain"

        # Load identity (SOUL.md)
        soul_path = config_root / "identity" / "SOUL.md"
        self.soul = self._read_text_cached(soul_path) or ""

        # Load all roles by name
        roles_dir = config_root / "roles"
        self.roles = load_prompt_directory(roles_dir, self._read_text_cached)

        # Load all skills by name — built-in first, then workspace overrides.
        skills_dir = config_root / "skills"
        self.skills = load_prompt_directory(skills_dir, self._read_text_cached)

        # GAP-NEW-2: workspace skill discovery.
        # Load user-supplied skills from the working directory so users can add
        # custom skills without modifying the repo.  Workspace skills override
        # built-in skills of the same name, enabling per-project customisation.
        # Respect configured context-dir name when looking for workspace skills,
        # but fall back to legacy names for compatibility.
        ctx_name = _get_ctx_name()

        _workspace_skill_dirs = [
            self._agent_context_dir.parent / ctx_name / "skills",
            self._agent_context_dir.parent / ".claude" / "skills",
        ]
        self.skills = merge_workspace_skill_overrides(
            self.skills,
            _workspace_skill_dirs,
            self._read_text_cached,
        )

    def get_skill(self, skill_name: str) -> str:
        """Get skill content by name."""
        return self.skills.get(skill_name, "")

    @staticmethod
    def _get_provider_variant(model_name: str) -> Optional[str]:
        """Return a provider-specific role suffix for known model families.

        Returns 'gemma4' for any Gemma 4 model, None otherwise.
        Extend this as new per-provider prompts are added.
        """
        if not model_name:
            return None
        name_lower = model_name.lower()
        if "gemma-4" in name_lower or "gemma4" in name_lower or "gemma_4" in name_lower:
            return "gemma4"
        return None

    def _select_role_for_tier(
        self, role_name: str, tier: str, model_name: str = ""
    ) -> str:
        """Return the best role prompt for the given tier and model.

        Selection priority (highest wins):
          1. role-provider variant  (e.g. operational-gemma4)
          2. role-tier variant      (e.g. operational-small, operational-frontier)
          3. base role              (e.g. operational)

        For the `operational` role:
          - NANO / SMALL → operational-small  (stripped, ≤60 lines)
          - LARGE / FRONTIER → operational-frontier  (exhaustive, reflection gate)
          - MEDIUM and all other roles → base role unchanged
        """
        if role_name == "operational":
            # 1. Provider-specific variant (highest priority)
            provider_variant = self._get_provider_variant(model_name)
            if provider_variant:
                content = self.roles.get(f"operational-{provider_variant}", "")
                if content:
                    return content
            # 2. Tier variant
            if tier == "small":
                content = self.roles.get("operational-small", "")
                if content:
                    return content
            elif tier in ("large", "frontier"):
                content = self.roles.get("operational-frontier", "")
                if content:
                    return content
        return self.roles.get(role_name, "")

    @staticmethod
    def _read_text_cached(path: Path) -> Optional[str]:
        """Read a text file, returning cached content if mtime unchanged."""
        if not path.exists():
            return None
        try:
            key = str(path)
            mtime = path.stat().st_mtime
            with _CACHE_LOCK:
                if key in _TEXT_CACHE and _TEXT_CACHE[key][0] == mtime:
                    _TEXT_CACHE.move_to_end(key)
                    return _TEXT_CACHE[key][1]
            # Read outside the lock to avoid blocking other threads during I/O
            content = path.read_text(encoding="utf-8").strip()
            with _CACHE_LOCK:
                # F15: Evict oldest entry when cache is full
                if len(_TEXT_CACHE) >= _CACHE_MAX:
                    _TEXT_CACHE.popitem(last=False)
                _TEXT_CACHE[key] = (mtime, content)
            return content
        except Exception:
            return None

    @staticmethod
    def _read_json_cached(path: Path) -> Dict:
        """Read a JSON file, returning cached parsed dict if mtime unchanged."""
        if not path.exists():
            return {}
        try:
            key = str(path)
            mtime = path.stat().st_mtime
            with _CACHE_LOCK:
                if key in _JSON_CACHE and _JSON_CACHE[key][0] == mtime:
                    _JSON_CACHE.move_to_end(key)
                    return _JSON_CACHE[key][1]
            # Read outside the lock to avoid blocking other threads during I/O
            data = json.loads(path.read_text(encoding="utf-8"))
            with _CACHE_LOCK:
                # F15: Evict oldest entry when cache is full
                if len(_JSON_CACHE) >= _CACHE_MAX:
                    _JSON_CACHE.popitem(last=False)
                _JSON_CACHE[key] = (mtime, data)
            return data
        except Exception:
            return {}

    def _get_task_state_content(self) -> Optional[str]:
        """Get TASK_STATE.md content with module-level mtime caching."""
        return self._read_text_cached(self._agent_context_dir / "TASK_STATE.md")

    def _get_todo_content(self) -> Optional[str]:
        """Get TODO.md content with module-level mtime caching."""
        return self._read_text_cached(self._agent_context_dir / "TODO.md")

    def _get_preferences_content(self) -> Optional[str]:
        """Get preferences.md content with module-level mtime caching.

        Project-specific user preferences stored in .agent-context/preferences.md.
        This file contains user preferences, working style, and explicit instructions.
        """
        return self._read_text_cached(self._agent_context_dir / "preferences.md")

    def _get_past_mistakes(
        self, task_description: str, limit: int = 4
    ) -> Optional[str]:
        """Query the SQLite mistakes FTS5 table for past mistakes relevant to
        *task_description*.  Returns a formatted block string or None when no
        relevant mistakes are found or the store is unavailable.

        This implements the cross-session learning loop: the agent sees past
        mistakes surfaced by BM25 similarity before it starts work, so it can
        avoid repeating them.
        """
        try:
            from src.core.memory.sqlite_session_store import SqliteSessionStore

            db_path = self._agent_context_dir / "session.db"
            if not db_path.exists():
                return None
            store = SqliteSessionStore(str(db_path))
            # Use first 120 chars of task as the FTS query — enough signal,
            # avoids FTS5 special-character issues with long queries.
            query = task_description[:120].strip()
            if not query:
                return None
            results = store.search_mistakes(query, limit=limit)
            if not results:
                return None
            lines = ["Past mistakes to avoid (retrieved by similarity):"]
            for r in results:
                tool_tag = f" [{r['tool']}]" if r.get("tool") else ""
                lines.append(f"- {r['summary']}{tool_tag}")
                if r.get("context"):
                    lines.append(f"  context: {str(r['context'])[:200]}")
            return "\n".join(lines)
        except Exception:
            return None

    def _get_summary_cache(self) -> Dict:
        """Get file_summaries.json with module-level mtime caching."""
        return self._read_json_cached(self._agent_context_dir / "file_summaries.json")

    def _sanitize_text(self, text: str) -> str:
        """Sanitize file / conversation text to reduce prompt-injection risk.
        CRITICAL: Fenced code blocks are NOT removed. Stripping code blocks
        destroys agent tool calls and causes infinite loops.
        - Remove top-level prompt-injection lines like "ignore all instructions".
        - Collapse long comment blocks (keep first/last few lines).
        """
        return sanitize_prompt_text(text, get_context_dir_name=_get_ctx_name)

    @staticmethod
    def _normalize_tools_for_prompt(tools: List[Dict]) -> List[Dict]:
        """Normalize tool schemas to the prompt-friendly shape.

        Prompt rendering and cache-key generation expect tool dicts with top-level
        ``name`` and ``description`` fields. Capable-loop callers may instead pass
        OpenAI-style function schemas, so normalize them here at the prompt boundary.
        """
        normalized: List[Dict] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("name"):
                normalized.append(tool)
                continue
            function_schema = tool.get("function")
            if not isinstance(function_schema, dict):
                normalized.append(tool)
                continue
            name = function_schema.get("name")
            if not name:
                normalized.append(tool)
                continue
            normalized.append(
                {
                    **tool,
                    "name": name,
                    "description": tool.get("description")
                    or function_schema.get("description", ""),
                }
            )
        return normalized

    # ------------------------------------------------------------------
    # S9-A: Cross-session memory injection
    # ------------------------------------------------------------------

    def inject_prior_session_memories(self, task: str, limit: int = 3) -> str:
        """Search the VectorStore for summaries from prior sessions relevant to *task*.

        Returns a formatted ``<prior_context>`` XML block (suitable for
        inclusion in the system prompt) or an empty string when no memories
        are available or the VectorStore is not configured.

        Called by ``perception_node`` on round 0 so that long-running projects
        can benefit from accumulated session summaries without the LLM having
        to ask for them explicitly.

        Args:
            task:   The current task description (used as the search query).
            limit:  Maximum number of memory results to include.
        """
        lines: List[str] = []

        # 1. VectorStore — episodic memories from prior sessions
        try:
            if _VectorStore is None:
                raise ImportError("VectorStore unavailable")
            _vs = _VectorStore(workdir=str(self._agent_context_dir.parent))
            results = _vs.search_memories(query=task, limit=limit)
            for r in results:
                text = r.get("text") or r.get("content") or str(r)
                lines.append(f"- {str(text)[:250]}")
        except Exception as exc:
            logger.debug("context_builder: VectorStore memory retrieval failed: %s", exc)
        # Use frozen snapshot for system prompt stability (prompt caching)
        _mem_snapshot = get_memory_for_prompt()
        if _mem_snapshot:
            lines.append("")
            lines.append(_mem_snapshot)

        if not lines:
            return ""
        return "\n".join(
            ["<prior_context>", "Relevant context from previous sessions:"]
            + lines
            + ["</prior_context>"]
        )

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

    def _build_model_constraints_block(
        self, model_tier: Optional[str], tools: list
    ) -> str:
        """Return the <model_constraints> block for NANO/SMALL tiers, or ''."""
        return _build_model_constraints_block_helper(
            model_tier=model_tier,
            tools=tools,
            model_tier_enum=_ModelTier,
            get_plan_step_limit=_get_plan_step_limit,
            get_context_budget=_get_context_budget,
        )

    def _build_instruction_files_block(self) -> str:
        """Return <project_instructions> block from ancestor .md files, or ''."""
        return _build_instruction_files_block_helper(
            workdir=self._agent_context_dir.parent,
            discover_instruction_files=_discover_instruction_files,
            render_instruction_files=_render_instruction_files,
        )

    def _build_project_instructions_block(self) -> str:
        """Return <project_config_instructions> block from config.json, or ''."""
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
                mt, pc, ir
            ),
        )

    def _build_thinking_mode_block(
        self, tier_str: str, _is_reasoning_model: bool
    ) -> str:
        """Return the <thinking_mode> block for frontier/large/reasoning models, or ''."""
        return _build_thinking_mode_block_helper(
            tier_str=tier_str,
            is_reasoning_model=_is_reasoning_model,
        )

    def _build_static_system_prefix(
        self,
        role_name: str,
        active_skills: List[str],
        tools: List[Dict],
        model_tier: Optional[str],
        provider_capabilities: Optional[Dict],
        use_native_tools: bool,
        is_simple_mode: bool,
        model_name: str = "",
    ) -> str:
        """Return the static portion of the system prompt (before DYNAMIC_BOUNDARY).

        P3-A: This method is the expensive part of build_prompt (SOUL.md + role +
        skills + tools rendering + prompt partial).  Its output is memoised in
        _STATIC_PROMPT_CACHE keyed by a tuple of all inputs that affect it.
        The cache is cleared at each task boundary by clear_cache().
        """
        tools = self._normalize_tools_for_prompt(tools)

        # P1-B: Prune tools to the tier limit FIRST so the cache key reflects the
        # actual rendered tool set (not the raw caller-supplied list).  This makes
        # the cache more efficient: two callers passing different-sized lists that
        # prune to the same set will share a cache entry.
        tools = self._prune_tools(tools, model_tier)

        provider_variant = self._get_provider_variant(model_name) or ""
        cache_key: Tuple = compute_static_prompt_cache_key(
            role_name=role_name,
            active_skills=active_skills,
            tools=tools,
            model_tier=model_tier,
            provider_capabilities=provider_capabilities,
            model_name=model_name,
            use_native_tools=use_native_tools,
            is_simple_mode=is_simple_mode,
            provider_variant=provider_variant,
            working_dir=str(self._agent_context_dir.parent),
        )

        with _CACHE_LOCK:
            cached = get_static_prompt_cache_entry(
                cache=_STATIC_PROMPT_CACHE,
                cache_key=cache_key,
            )
        if cached is not None:
            return cached

        tier_str = (model_tier or "").lower()
        role_content = self._select_role_for_tier(role_name, tier_str, model_name)
        result = _build_static_system_parts_helper(
            soul=self.soul,
            role_content=role_content,
            active_skills=active_skills,
            get_skill=self.get_skill,
            sanitize_text=self._sanitize_text,
            system_prompt_dynamic_boundary=SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
            tools=tools,
            model_tier=model_tier,
            provider_capabilities=provider_capabilities,
            model_name=model_name,
            use_native_tools=use_native_tools,
            is_simple_mode=is_simple_mode,
            build_model_constraints_block_fn=lambda mt, ts: self._build_model_constraints_block(
                mt, ts
            ),
            build_instruction_files_block_fn=self._build_instruction_files_block,
            build_project_instructions_block_fn=self._build_project_instructions_block,
            render_tools_for_tier_fn=lambda ts, mt: self._render_tools_for_tier(ts, mt),
            build_thinking_guidance_block_fn=lambda mt, pc, mn: self._build_thinking_guidance_block(
                mt, pc, mn
            ),
            is_reasoning_model_fn=_is_reasoning_model,
            build_thinking_mode_block_fn=lambda tier, is_rm: self._build_thinking_mode_block(
                tier, is_rm
            ),
            build_output_format_block_fn=lambda unt, ism, tier: self._build_output_format_block(
                unt, ism, tier
            ),
        )
        with _CACHE_LOCK:
            store_static_prompt_cache_entry(
                cache=_STATIC_PROMPT_CACHE,
                cache_key=cache_key,
                value=result,
            )
        return result

    def _prepare_repository_context_block(
        self,
        *,
        retrieved_snippets: Optional[List[Dict]],
        context_controller,
        conversation: List[Dict],
        static_prefix: str,
        dynamic_parts: List[str],
    ) -> Tuple[Optional[List[Dict]], str]:
        """Return budget-filtered retrieved snippets and the rendered repo block."""
        if not retrieved_snippets:
            return retrieved_snippets, ""

        if context_controller is not None:
            try:
                _sys_text = static_prefix + "\n\n".join(dynamic_parts)
                _file_descs = build_context_controller_descriptors(retrieved_snippets)
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
            self._sanitize_text(task_description), _today_iso()
        )
        return append_task_message(
            built_messages=built_messages,
            truncated_conversation=truncated_conversation,
            task_prompt_content=prompt_content,
        )

    def _emit_context_built_hook(self, built_messages: List[Dict[str, str]]) -> None:
        """Best-effort plugin hook fired after prompt assembly completes."""
        if not (_HAS_HOOKS and _hook_registry is not None):
            return
        try:
            _ctx_dir = getattr(self, "_agent_context_dir", None)
            _hook_registry.call(
                _HOOK_CONTEXT_BUILT,
                {
                    "messages": built_messages,
                    "working_dir": str(_ctx_dir.parent) if _ctx_dir else "",
                },
            )
        except Exception as exc:
            logger.debug("context_builder: hook emit failed: %s", exc)

    def build_prompt(
        self,
        role_name: str,
        active_skills: List[str],
        task_description: str,
        tools: List[Dict],
        conversation: List[Dict],
        max_tokens: Optional[int] = None,
        retrieved_snippets: Optional[List[Dict]] = None,
        provider_capabilities: Optional[Dict] = None,
        context_controller=None,
        model_tier: Optional[str] = None,
        model_name: Optional[str] = None,
        include_prior_context: bool = True,
    ) -> List[Dict[str, str]]:
        # F10: Use dynamic token budget when max_tokens is not explicitly provided.
        if max_tokens is None:
            max_tokens = _default_max_tokens()
        # Token budgeting: reserve space for static system + tags overhead,
        # give remaining budget to conversation history.
        conversation_quota = max(
            0, max_tokens - _SYSTEM_OVERHEAD_TOKENS
        )  # reserve tokens for system overhead

        built_messages: List[Dict[str, str]] = []

        # 1. System Block (Identity + Role + Skills + Tools)
        # We consolidate these into a single system message for better compatibility

        # P3-A: Determine use_native_tools / is_simple_mode once, early, so we can
        # pass them to _build_static_system_prefix for correct cache-key derivation.
        _is_simple_mode = False
        try:
            if _ModelTier is None or _get_tool_limit is None:
                raise ImportError("model_tiers unavailable")
            _tier_val = _ModelTier(model_tier) if model_tier else None
            _is_simple_mode = _get_tool_limit(_tier_val) == 0 if _tier_val else False

            # GAP-SMALL-3: SMALL models on providers without proven parallel tool
            # support also use simple_mode (one tool per response).
            if not _is_simple_mode and _tier_val == _ModelTier.SMALL:
                _caps_local = provider_capabilities or {}
                if not _caps_local.get("provider_supports_parallel_tools", True):
                    _is_simple_mode = True

        except Exception as exc:
            logger.debug("context_builder: simple_mode/tier check failed: %s", exc)

        use_native_tools = (
            not _is_simple_mode  # NANO never uses native tools
            and provider_capabilities is not None
            and provider_capabilities.get("supports_native_tools", False)
        )

        # P3-A: Fetch (or build + cache) the static system prefix.
        # NOTE: _prune_tools() is called inside _build_static_system_prefix() (P1-B)
        # so the tools list passed here is intentionally unpruned — pruning and rendering
        # are co-located so the cache key reflects the pruned output correctly.
        static_prefix = self._build_static_system_prefix(
            role_name=role_name,
            active_skills=active_skills,
            tools=tools,
            model_tier=model_tier,
            provider_capabilities=provider_capabilities,
            use_native_tools=use_native_tools,
            is_simple_mode=_is_simple_mode,
            model_name=model_name or "",
        )

        dynamic_parts = self._build_dynamic_prompt_parts(
            conversation=conversation,
            task_description=task_description,
            static_prefix=static_prefix,
            retrieved_snippets=retrieved_snippets,
            context_controller=context_controller,
            include_prior_context=include_prior_context,
        )

        # Assemble final system message: static prefix + dynamic parts joined.
        if dynamic_parts:
            full_system = static_prefix + "\n\n" + "\n\n".join(dynamic_parts)
        else:
            full_system = static_prefix

        built_messages.append({"role": "system", "content": full_system})

        built_messages = self._append_conversation_and_task_messages(
            built_messages=built_messages,
            conversation=conversation,
            task_description=task_description,
            conversation_quota=conversation_quota,
        )

        self._emit_context_built_hook(built_messages)

        return built_messages

    # ------------------------------------------------------------------
    # P3-B: Proactive tool output pruning
    # ------------------------------------------------------------------

    @staticmethod
    def _prune_stale_tool_outputs(
        messages: List[Dict],
        current_step_hint: Optional[str] = None,
        stale_after_turns: int = 3,
    ) -> List[Dict]:
        return prune_stale_tool_outputs(
            messages,
            current_step_hint=current_step_hint,
            stale_after_turns=stale_after_turns,
        )

    def _truncate_to_token_budget(self, text: str, budget: int) -> str:
        return truncate_to_token_budget(
            text,
            budget,
            token_estimator=self.token_estimator,
        )

    def _truncate_text(self, text: str, max_tokens: int) -> str:
        return truncate_text_to_max_tokens(
            text,
            max_tokens,
            token_estimator=self.token_estimator,
        )
