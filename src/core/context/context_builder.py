from __future__ import annotations
from typing import Callable, Dict, List, Optional, Tuple
import logging
import math
import json
import threading
from collections import OrderedDict
from pathlib import Path

from src.core.memory.frozen_snapshot import get_memory_for_prompt

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

# CP-12: Sentinel string that marks the boundary between the static (cacheable)
# and dynamic (per-turn) portions of the system prompt.
#
# The Anthropic adapter splits the system prompt on this sentinel and sends the
# static portion with ``cache_control: {"type": "ephemeral"}`` so that the
# prompt prefix is eligible for Anthropic's prompt caching.  Other providers
# ignore the sentinel (it is stripped before the prompt reaches the model).
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


# F10: Import dynamic token budget helper (lazy — avoids circular imports at module load).
def _default_max_tokens() -> int:
    try:
        from src.core.inference.provider_context import get_context_budget

        return get_context_budget()
    except Exception:
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
#            use_native_tools, is_simple_mode)
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


def _today_iso() -> str:
    """Return today's date as YYYY-MM-DD (local time)."""
    from datetime import date

    return date.today().isoformat()


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
            try:
                from src.core.inference.tokenizer import count_tokens as _ct

                self.token_estimator = _ct
            except Exception:
                self.token_estimator = lambda s: math.ceil(len(s) / 3.5)
        # Resolve working directory — use provided path; do NOT fall back to
        # Path.cwd() because cwd is unreliable in async/subprocess contexts and
        # would silently look for agent-brain files in the wrong location.
        # Callers that do not have a working_dir should pass the repo root
        # explicitly (NEW-10 fix).
        try:
            from src.tools.tools_config import agent_context_path

            if working_dir:
                self._agent_context_dir = agent_context_path(Path(working_dir))
            else:
                # Fall back to the current working directory so that tests using
                # monkeypatch.chdir() and callers without an explicit working_dir
                # both resolve agent-context files relative to the active cwd.
                self._agent_context_dir = agent_context_path(Path.cwd())
        except Exception:
            # Fallback: use agent_context_path which handles legacy dirs
            if working_dir:
                self._agent_context_dir = agent_context_path(Path(working_dir))
            else:
                self._agent_context_dir = agent_context_path(Path.cwd())

        # Token usage tracking for TokenBudgetMonitor
        self._last_token_count: int = 0
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
        self.roles: Dict[str, str] = {}
        roles_dir = config_root / "roles"
        if roles_dir.exists():
            for role_file in roles_dir.glob("*.md"):
                role_name = role_file.stem  # filename without extension
                self.roles[role_name] = self._read_text_cached(role_file) or ""

        # Load all skills by name — built-in first, then workspace overrides.
        self.skills: Dict[str, str] = {}
        skills_dir = config_root / "skills"
        if skills_dir.exists():
            for skill_file in skills_dir.glob("*.md"):
                skill_name = skill_file.stem
                self.skills[skill_name] = self._read_text_cached(skill_file) or ""

        # GAP-NEW-2: workspace skill discovery.
        # Load user-supplied skills from the working directory so users can add
        # custom skills without modifying the repo.  Workspace skills override
        # built-in skills of the same name, enabling per-project customisation.
        # Respect configured context-dir name when looking for workspace skills,
        # but fall back to legacy names for compatibility.
        try:
            from src.tools.tools_config import get_context_dir_name

            ctx_name = get_context_dir_name()
        except Exception:
            ctx_name = ".codingAgent"

        _workspace_skill_dirs = [
            self._agent_context_dir.parent / ctx_name / "skills",
            self._agent_context_dir.parent / ".claude" / "skills",
        ]
        for _wsd in _workspace_skill_dirs:
            if _wsd.exists():
                for _wsf in _wsd.glob("*.md"):
                    _wsn = _wsf.stem
                    _content = self._read_text_cached(_wsf)
                    if _content:
                        self.skills[_wsn] = _content

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
        if not text:
            return text

        # 1) Remove obvious prompt-injection lines
        lines = text.splitlines()
        cleaned_lines = []
        removed_any = False
        for ln in lines:
            s = ln.strip().lower()
            # heuristics for prompt-injection: match substrings anywhere
            if (
                "ignore all instructions" in s
                or "do not follow" in s
                or "disregard previous" in s
                or "forget all previous" in s
            ):
                # skip this line
                removed_any = True
                continue
            cleaned_lines.append(ln)
        text = "\n".join(cleaned_lines)

        # 4) Collapse very long comment blocks (consecutive comment lines > 20)
        collapsed = []
        comment_block = []
        for ln in text.splitlines():
            if ln.strip().startswith("#") or ln.strip().startswith("//"):
                comment_block.append(ln)
            else:
                if len(comment_block) > 20:
                    # keep first 3 and last 3
                    collapsed.extend(comment_block[:3])
                    collapsed.append(
                        f"[COMMENT BLOCK TRUNCATED - {len(comment_block)} lines]"
                    )
                    collapsed.extend(comment_block[-3:])
                    removed_any = True
                else:
                    collapsed.extend(comment_block)
                comment_block = []
                collapsed.append(ln)
        # flush tail comment block
        if comment_block:
            if len(comment_block) > 20:
                collapsed.extend(comment_block[:3])
                collapsed.append(
                    f"[COMMENT BLOCK TRUNCATED - {len(comment_block)} lines]"
                )
                collapsed.extend(comment_block[-3:])
                removed_any = True
            else:
                collapsed.extend(comment_block)

        sanitized = "\n".join(collapsed)

        # Best-effort audit log for sanitization events
        if removed_any:
            try:
                cwd = Path.cwd()
                try:
                    from src.tools.tools_config import get_context_dir_name

                    ctx_name = get_context_dir_name()
                except Exception:
                    ctx_name = ".codingAgent"

                ac = cwd / ctx_name
                if ac.exists():
                    logp = ac / "context_sanitization.log"
                    with open(logp, "a", encoding="utf-8") as f:
                        f.write("SANITIZE: removed suspicious content\n")
            except Exception:
                # never fail sanitization due to logging issues
                pass

        return sanitized

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
            from src.core.indexing.vector_store import VectorStore

            _vs = VectorStore(workdir=str(self._agent_context_dir.parent))
            results = _vs.search_memories(query=task, limit=limit)
            for r in results:
                text = r.get("text") or r.get("content") or str(r)
                lines.append(f"- {str(text)[:250]}")
        except Exception:
            pass

        # 2. GAP-NEW-4: memory.md — notes saved with memory_save()
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
        path = self._TEMPLATES_DIR / filename
        return self._read_text_cached(path) or ""

    # GAP-FRONTIER-1: model-ID → partial file mapping (checked before provider family).
    # Keys are regex patterns matched against the active model ID string.
    _MODEL_ID_PARTIAL_MAP: List[Tuple[str, str]] = [
        # Reasoning / frontier openai
        (r"o1|o3|o4", "openai-reasoning.md"),
        (r"gpt-4o|gpt-4\.5|gpt-4-turbo", "openai-frontier.md"),
        # Anthropic — separate frontier vs small
        (
            r"claude-opus|claude-3-7|claude-sonnet-4-[5-9]|claude-3-5-sonnet",
            "anthropic-frontier.md",
        ),
        (r"claude-haiku|claude-3-5-haiku|claude-3-haiku", "anthropic-small.md"),
        # Gemini — frontier vs flash/nano
        (r"gemini-2\.5-pro|gemini-pro|gemini-ultra", "gemini-frontier.md"),
        (r"gemini-flash|gemini-nano|gemini-2\.0-flash", "gemini-small.md"),
        # Gemma 4 — large (31B dense, 26B MoE) → frontier partial;
        # edge (E2B, E4B) → small partial.
        # 16GB VRAM target: 31B q4 (~15.5GB) and 26B MoE q4 (~13GB) both fit.
        (r"gemma-4-31b|gemma-4-26b|gemma4:31b|gemma4:26b", "gemini-frontier.md"),
        (r"gemma-4-e[24]b|gemma4:e[24]b|gemma4-e[24]b", "local-small.md"),
    ]

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
        import re

        if is_reasoning:
            partial = self._load_prompt_partial("beast.txt")
            if partial:
                return partial

        caps = provider_capabilities or {}
        # GAP-FRONTIER-1: try model-ID-specific partial first
        active_model = caps.get("model", "").lower()
        if active_model:
            for pattern, filename in self._MODEL_ID_PARTIAL_MAP:
                if re.search(pattern, active_model):
                    partial = self._load_prompt_partial(filename)
                    if partial:
                        return partial
                    break  # pattern matched but file absent — fall through

        provider_family = caps.get("provider_family", "").lower()

        if "anthropic" in provider_family:
            partial = self._load_prompt_partial("anthropic.txt")
            if partial:
                return partial

        if "gemini" in provider_family:
            partial = self._load_prompt_partial("gemini.txt")
            if partial:
                return partial

        if "openai" in provider_family or "openrouter" in provider_family:
            partial = self._load_prompt_partial("openai.txt")
            if partial:
                return partial

        tier = (model_tier or "").lower()
        if tier == "small":
            partial = self._load_prompt_partial("local-small.md")
            if partial:
                return partial
        elif tier == "medium":
            partial = self._load_prompt_partial("local-medium.md")
            if partial:
                return partial

        return self._load_prompt_partial("default.txt")

    @staticmethod
    def _prune_tools(tools: List[Dict], model_tier: Optional[str]) -> List[Dict]:
        """S1-C: Limit the tool list based on model tier.

        Core tools (read/write/edit/bash/grep/glob/search) are always kept first.
        Non-core tools are appended up to the tier limit, then dropped from the tail.
        """
        try:
            from src.core.inference.model_tiers import ModelTier, get_tool_limit

            tier = ModelTier(model_tier) if model_tier else ModelTier.MEDIUM
            limit = get_tool_limit(tier)
        except Exception:
            return tools  # no pruning if model_tiers unavailable

        if len(tools) <= limit:
            return tools

        # Separate core tools (always kept) from supplementary tools
        _CORE_NAMES = {
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
        core = [t for t in tools if t.get("name") in _CORE_NAMES]
        supplementary = [t for t in tools if t.get("name") not in _CORE_NAMES]

        # Fill up to limit: core first, then supplementary
        selected = core + supplementary
        return selected[:limit]

    def _render_tools_for_tier(
        self, tools: List[Dict], model_tier: Optional[str]
    ) -> str:
        """S8-A: Render tools list with verbosity appropriate to the model tier.

        NANO/SMALL: name + first-sentence description only (minimal tokens).
        MEDIUM+:    full description (unchanged behaviour).
        """
        try:
            from src.core.inference.model_tiers import ModelTier

            tier = ModelTier(model_tier) if model_tier else ModelTier.MEDIUM
            is_minimal = tier == ModelTier.SMALL
        except Exception:
            is_minimal = False

        lines = []
        for tool in tools:
            desc = self._sanitize_text(tool.get("description", ""))
            if is_minimal:
                # Keep only the first sentence to save tokens on tiny models.
                first_sentence = desc.split(".")[0].strip()
                if first_sentence:
                    desc = first_sentence + "."
            lines.append(f"name: {tool['name']}\ndescription: {desc}")
        return "\n".join(lines) + "\n" if lines else ""

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
            import subprocess

            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=working_dir,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

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
        # P1-B: Prune tools to the tier limit FIRST so the cache key reflects the
        # actual rendered tool set (not the raw caller-supplied list).  This makes
        # the cache more efficient: two callers passing different-sized lists that
        # prune to the same set will share a cache entry.
        tools = self._prune_tools(tools, model_tier)

        # Derive a compact key for the (pruned) tools list.
        try:
            tools_key = hash(
                tuple(
                    (t.get("name", ""), (t.get("description") or "")[:50])
                    for t in tools
                )
            )
        except Exception:
            tools_key = 0

        caps = provider_capabilities or {}
        provider_family = caps.get("provider_family", "")
        # OP-1: Include provider variant (e.g. "gemma4") in cache key so
        # per-provider prompt variants are cached separately.
        provider_variant = self._get_provider_variant(model_name) or ""
        cache_key: Tuple = (
            role_name,
            tuple(active_skills),
            tools_key,
            model_tier or "",
            provider_family,
            use_native_tools,
            is_simple_mode,
            provider_variant,
            # Include working_dir so that different projects (or test tmp_paths)
            # with different AGENTS.md / instruction files get separate cache entries.
            str(self._agent_context_dir.parent),
        )

        with _CACHE_LOCK:
            cached = _STATIC_PROMPT_CACHE.get(cache_key)
        if cached is not None:
            return cached

        # --- Build the static prefix (expensive path) ---
        parts: List[str] = []

        # GAP-SMALL-2 / GAP-FRONTIER-2: select tier-appropriate role prompt for
        # the operational role so that small models get a stripped-down prompt
        # and frontier models get exhaustive instructions.
        # OP-1: Also check for per-provider variant (e.g. operational-gemma4).
        tier_str = (model_tier or "").lower()
        role_content = self._select_role_for_tier(role_name, tier_str, model_name)
        parts.append(f"<identity>\n{self._sanitize_text(self.soul)}\n</identity>")
        parts.append(f"<role>\n{self._sanitize_text(role_content)}\n</role>")

        # P1-E: Model constraints block for NANO/SMALL tiers.
        # Injects a concise <model_constraints> block that tells small models
        # their tier, tool count, step limit, and required output format so they
        # don't try to use tools / format features they don't have.
        if tier_str in ("nano", "small"):
            try:
                from src.core.inference.model_tiers import (
                    ModelTier,
                    get_plan_step_limit,
                )

                _p1e_tier_enum = ModelTier(tier_str) if tier_str else None
                _p1e_step_limit = (
                    get_plan_step_limit(_p1e_tier_enum) if _p1e_tier_enum else 6
                )
                _p1e_tool_count = len(tools)  # already pruned
                _p1e_ctx_tokens = 0
                try:
                    from src.core.inference.provider_context import get_context_budget

                    _p1e_ctx_tokens = get_context_budget(model_tier=tier_str)
                except Exception:
                    pass
                _p1e_lines = [
                    f"Tier: {tier_str.upper()} | Context: {_p1e_ctx_tokens:,} tokens | Tools: {_p1e_tool_count} available",
                    f"Max plan steps: {_p1e_step_limit} | Output format: YAML tool calls only (no JSON, no prose before tool call)",
                    "Not available: parallel tool calls, subagent delegation, extended reasoning",
                ]
                parts.append(
                    "<model_constraints>\n"
                    + "\n".join(_p1e_lines)
                    + "\n</model_constraints>"
                )
            except Exception:
                pass

        # CP-11: Ancestor instruction file injection.
        try:
            from src.core.context.instruction_files import (
                discover_instruction_files,
                render_instruction_files,
            )

            _workdir = self._agent_context_dir.parent
            _instr_files = discover_instruction_files(_workdir)
            if _instr_files:
                _instr_block = render_instruction_files(_instr_files)
                if _instr_block:
                    parts.append(
                        f"<project_instructions>\n{_instr_block}\n</project_instructions>"
                    )
        except Exception:
            pass

        # OP-5: Per-project instructions from .agent-context/config.json.
        # These come after CP-11 file instructions so they take higher precedence.
        # Injected inside the static prefix so they are cached with the rest.
        try:
            # Use the orchestration instruction loader (correct location for this helper).
            from src.core.orchestration.instruction_loader import (
                load_project_instructions as _gpi,
            )

            # pass a Path object as the loader expects a Path-like cwd
            _proj_instructions = _gpi(self._agent_context_dir.parent)
            if _proj_instructions:
                _proj_block = "\n".join(f"- {instr}" for instr in _proj_instructions)
                parts.append(
                    f"<project_config_instructions>\n{_proj_block}\n"
                    "</project_config_instructions>"
                )
        except Exception:
            pass

        # Dynamic boundary sentinel — Anthropic adapter splits here.
        parts.append(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)

        if active_skills:
            skill_contents = [
                self._sanitize_text(sc)
                for sn in active_skills
                if (sc := self.get_skill(sn))
            ]
            if skill_contents:
                parts.append(
                    f"<active_skills>\n{chr(10).join(skill_contents)}\n</active_skills>"
                )

        # tools already pruned above; render descriptions for the tier.
        tools_text = self._render_tools_for_tier(tools, model_tier)
        parts.append(f"<available_tools>\n{tools_text}\n</available_tools>")

        # S1-B: prompt partial.
        _is_reasoning_model = False
        try:
            _is_reasoning_model = False
            try:
                from src.core.inference.thinking_utils import is_reasoning_model

                _active_model = caps.get("model", "")
                _is_reasoning_model = bool(
                    _active_model and is_reasoning_model(_active_model)
                )
            except Exception:
                pass
            _partial = self._select_prompt_partial(
                model_tier, provider_capabilities, _is_reasoning_model
            )
            if _partial:
                parts.append(f"<model_guidance>\n{_partial}\n</model_guidance>")
        except Exception:
            pass

        # GAP-FRONTIER-7: inject structured thinking gate for reasoning models and
        # frontier models that support extended thinking (FRONTIER + LARGE tiers).
        # This adds ~50 tokens but significantly improves tool-call accuracy on
        # complex tasks by forcing the model to plan before acting.
        if tier_str in ("frontier", "large") or _is_reasoning_model:
            parts.append(
                "<thinking_mode>\n"
                "Before every tool call, briefly state:\n"
                "1. What you expect this call to return.\n"
                "2. What you will do if it fails or returns unexpected output.\n"
                "This reflection is mandatory — do not skip it.\n"
                "</thinking_mode>"
            )

        # Output format instruction.
        if use_native_tools:
            format_instr = (
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
        elif is_simple_mode:
            format_instr = (
                "<output_format>\n"
                "STRICT RULE: Output EXACTLY ONE tool call per response, no exceptions.\n"
                "Use the YAML tool format in a fenced code block:\n"
                "```yaml\n"
                "name: the_tool_name\n"
                "arguments:\n"
                "  arg_name: arg_value\n"
                "```\n"
                "Do NOT output more than one yaml block. Do NOT chain tool calls.\n"
                "After the tool result is returned, you may call one more tool if needed.\n"
                "</output_format>"
            )
        elif tier_str == "small":
            # GAP-SMALL-1: simplified output format for small models — only STATUS: line required.
            format_instr = (
                "<output_format>\n"
                "Use the YAML tool format in a fenced code block:\n"
                "```yaml\n"
                "name: the_tool_name\n"
                "arguments:\n"
                "  arg_name: arg_value\n"
                "```\n"
                "Make ONE tool call per response. After the tool result, write:\n"
                "STATUS: complete | partial | failed\n"
                "</output_format>"
            )
        else:
            format_instr = (
                "<output_format>\n"
                "You MUST think step-by-step. Write your internal reasoning inside <think> tags.\n"
                "To execute an action, you MUST use the provided markdown YAML tool format.\n"
                "Format your tool calls exactly like this using a fenced code block:\n"
                "```yaml\n"
                "name: the_tool_name\n"
                "arguments:\n"
                "  arg_name: arg_value\n"
                "```\n"
                "IMPORTANT: Use markdown YAML format (not XML). Do not use <tool> tags.\n"
                "After executing a tool, your response will include the tool's result.\n"
                "If the tool result completes the user's task, do NOT make more tool calls.\n"
                "Simply summarize the result or indicate task completion.\n"
                "Only call another tool if the result requires follow-up action.\n"
                "</output_format>"
            )
        parts.append(format_instr)

        result = "\n\n".join(parts)
        with _CACHE_LOCK:
            _STATIC_PROMPT_CACHE[cache_key] = result
        return result

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
    ) -> List[Dict[str, str]]:
        # F10: Use dynamic token budget when max_tokens is not explicitly provided.
        if max_tokens is None:
            max_tokens = _default_max_tokens()
        # Token budgeting: reserve space for static system + tags overhead,
        # give remaining budget to conversation history.
        conversation_quota = max(
            0, max_tokens - 2100
        )  # ~2100 tokens for system overhead

        built_messages: List[Dict[str, str]] = []

        # 1. System Block (Identity + Role + Skills + Tools)
        # We consolidate these into a single system message for better compatibility

        # P3-A: Determine use_native_tools / is_simple_mode once, early, so we can
        # pass them to _build_static_system_prefix for correct cache-key derivation.
        _is_simple_mode = False
        try:
            from src.core.inference.model_tiers import (
                ModelTier,
                is_simple_mode as _check_simple,
            )

            _tier_val = ModelTier(model_tier) if model_tier else None
            _is_simple_mode = _check_simple(_tier_val) if _tier_val else False

            # GAP-SMALL-3: SMALL models on providers without proven parallel tool
            # support also use simple_mode (one tool per response).
            if not _is_simple_mode and _tier_val == ModelTier.SMALL:
                _caps_local = provider_capabilities or {}
                if not _caps_local.get("provider_supports_parallel_tools", True):
                    _is_simple_mode = True

        except Exception:
            pass

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

        # Dynamic parts (appended after the static prefix).
        safe_task_description = self._sanitize_text(task_description)
        dynamic_parts: List[str] = []

        # 1a. Session summary — auto-injected from TASK_STATE.md so the agent
        #     always has access to prior context without needing a tool call.
        #     (Mirrors the compaction injection used by Claude Code / OpenCode.)
        #
        # CRITICAL FIX: Only inject session summary at the START of a task (empty conversation)
        # or when the task is truly complete. During active execution (tool results present),
        # injecting TASK_STATE.md causes the model to output JSON state tracker instead of
        # continuing with YAML tool calls. This breaks multi-turn execution.
        try:
            has_tool_results = any(
                m.get("role") == "user"
                and "tool_execution_result" in str(m.get("content", ""))
                for m in conversation
            )
            ts_content = self._get_task_state_content()
            _empty = "# Current Task\n\n# Completed Steps\n\n# Next Step"
            if (
                not has_tool_results  # Only inject when NOT in active execution
                and ts_content
                and ts_content.strip() != _empty.strip()
                and len(ts_content) > 60
            ):
                dynamic_parts.append(
                    f"<session_summary>\n{ts_content}\n</session_summary>"
                )
        except Exception:
            pass  # never fail prompt building due to missing TASK_STATE.md

        # 1b. Task progress — auto-injected from TODO.md when it exists.
        #     TODO.md is the authoritative, deterministic plan tracker (written by planning_node,
        #     updated by execution_node). It takes precedence over TASK_STATE.md for step status.
        try:
            todo_content = self._get_todo_content()
            if todo_content and len(todo_content) > 20:
                dynamic_parts.append(
                    f"<task_progress>\n{todo_content}\n</task_progress>"
                )
        except Exception:
            pass  # never fail prompt building due to missing TODO.md

        # 1c. Project-specific user preferences — auto-injected from preferences.md
        #    User preferences, working style, explicit instructions for this project.
        try:
            prefs_content = self._get_preferences_content()
            if prefs_content and len(prefs_content) > 10:
                dynamic_parts.append(
                    f"<user_preferences>\n{prefs_content}\n</user_preferences>"
                )
        except Exception:
            pass  # never fail prompt building due to missing preferences.md

        # 1b. Repository Intelligence block (if any retrieved snippets provided)
        # Step 8: If a ContextController is available, run enforce_budget() to drop or
        # summarize snippets that would overflow the available token budget.
        if retrieved_snippets and context_controller is not None:
            try:
                # Convert snippet dicts to the file-descriptor format enforce_budget expects
                _sys_text = static_prefix + "\n\n".join(dynamic_parts)
                _file_descs = [
                    {
                        "path": s.get("file_path", ""),
                        "content": s.get("snippet") or s.get("content") or "",
                        "line_count": len(
                            (s.get("snippet") or s.get("content") or "").splitlines()
                        ),
                        "estimated_tokens": max(
                            1, len(s.get("snippet") or s.get("content") or "") // 4
                        ),
                    }
                    for s in retrieved_snippets
                ]
                _included, _excluded = context_controller.enforce_budget(
                    _file_descs, conversation, _sys_text
                )
                if _excluded:
                    logging.getLogger(__name__).debug(
                        f"ContextController: excluded {len(_excluded)} snippet(s) to fit budget"
                    )
                # Rebuild retrieved_snippets from included descriptors (preserve original keys)
                _included_paths = {d["path"] for d in _included}
                retrieved_snippets = [
                    s
                    for s in retrieved_snippets
                    if s.get("file_path", "") in _included_paths
                ]
            except Exception:
                pass  # never fail prompt build due to budget enforcement

        repo_block = ""
        if retrieved_snippets:
            try:
                # Use cached file summaries
                summary_cache = self._get_summary_cache()

                repo_entries = []
                for snip in retrieved_snippets[:10]:
                    # each snippet expected to be dict with keys: file_path, snippet, reason
                    fp = snip.get("file_path")
                    if fp and fp in summary_cache:
                        entry_text = summary_cache.get(fp)
                    else:
                        entry_text = snip.get("snippet") or snip.get("content") or ""
                    # sanitize entry
                    entry_text = self._sanitize_text(str(entry_text))
                    repo_entries.append(f"File: {fp or 'unknown'}\n{entry_text}\n---\n")

                if repo_entries:
                    repo_block = (
                        "<repository_intelligence>\n"
                        + "\n".join(repo_entries)
                        + "\n</repository_intelligence>"
                    )
                    dynamic_parts.append(repo_block)
            except Exception:
                # best-effort: do not fail prompt build
                pass

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
        except Exception:
            pass  # never fail prompt build due to LSP errors

        # Assemble final system message: static prefix + dynamic parts joined.
        if dynamic_parts:
            full_system = static_prefix + "\n\n" + "\n\n".join(dynamic_parts)
        else:
            full_system = static_prefix

        built_messages.append({"role": "system", "content": full_system})

        # 2. Conversation Logic
        # P3-B: Prune stale tool outputs before filtering to save context tokens.
        pruned_conversation = self._prune_stale_tool_outputs(
            list(conversation),
            current_step_hint=task_description[:120] if task_description else None,
        )

        # Filter msg_mgr to only include User and Assistant messages (strip system prompts)
        filtered_conv = [
            {
                "role": m.get("role"),
                "content": self._sanitize_text(m.get("content", "")),
            }
            for m in pruned_conversation
            if m.get("role") in ["user", "assistant"]
        ]

        truncated_conversation: List[Dict[str, str]] = []
        if conversation_quota > 0 and filtered_conv:
            total_conv_tokens = 0
            for message in reversed(filtered_conv):
                msg_json = json.dumps(message)
                message_token_count = self.token_estimator(msg_json)

                if total_conv_tokens + message_token_count <= conversation_quota:
                    truncated_conversation.insert(0, message)
                    total_conv_tokens += message_token_count
                else:
                    break

        # 3. Task / Prompt Logic
        # Ensure the last message is always USER for local model templates
        # If the history ends in ASSISTANT, we must append a "Proceed" user message.
        # If the history is empty, the task itself is the USER message.

        # Add conversation
        built_messages.extend(truncated_conversation)

        # Ensure there's a user message after system for Qwen compatibility
        # If conversation starts with assistant, insert task as user message first
        if (
            truncated_conversation
            and truncated_conversation[0].get("role") == "assistant"
        ):
            # Insert task as user message before the assistant messages
            prompt_content = f"<task>\n{safe_task_description}\n</task>\n<context>\nToday's date: {_today_iso()}\n</context>\n\nExecute the next action using the YAML tool format."
            # Insert at index 1 (after system message)
            built_messages.insert(1, {"role": "user", "content": prompt_content})
        # Final check: is the last message Assistant or is the list missing User?
        elif not built_messages or built_messages[-1].get("role") != "user":
            prompt_content = f"<task>\n{safe_task_description}\n</task>\n<context>\nToday's date: {_today_iso()}\n</context>\n\nExecute the next action using the YAML tool format."
            built_messages.append({"role": "user", "content": prompt_content})
        else:
            # If the last message is already User, we can either wrap it in <task>
            # or just leave it. For continuity, let's wrap it if it doesn't have it.
            last_msg = built_messages[-1]
            if "<task>" not in last_msg.get("content", ""):
                last_msg["content"] = (
                    f"<task>\n{last_msg['content']}\n</task>\n\nExecute the next action using the YAML tool format."
                )

        # Gap 3: HOOK_CONTEXT_BUILT — lets plugins inspect/log the final prompt.
        if _HAS_HOOKS and _hook_registry is not None:
            try:
                _ctx_dir = getattr(self, "_agent_context_dir", None)
                _hook_registry.call(
                    _HOOK_CONTEXT_BUILT,
                    {
                        "messages": built_messages,
                        "working_dir": str(_ctx_dir.parent) if _ctx_dir else "",
                    },
                )
            except Exception:
                pass

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
        """Replace oversized tool results that are >stale_after_turns old with stubs.

        P3-B: Keeps recent tool results verbatim (the last *stale_after_turns*
        user messages that contain tool_execution_result are considered "recent").
        Older tool result messages are replaced with a compact stub that preserves
        the tool name + ok/error status but discards the full output.

        The optional *current_step_hint* string is matched against the tool
        result content: if the current plan step description appears in the
        result, the message is kept verbatim regardless of age.

        Non-tool-result user messages and all assistant messages are left
        unchanged.
        """
        if not messages:
            return messages

        # Locate indices of all "tool result" user messages (newest-first).
        tool_result_indices: List[int] = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if "tool_execution_result" in str(content):
                    tool_result_indices.append(i)

        # The most recent stale_after_turns results are "recent" — keep them.
        recent_indices: set = set(tool_result_indices[-stale_after_turns:])

        pruned: List[Dict] = []
        for i, msg in enumerate(messages):
            if (
                msg.get("role") == "user"
                and i not in recent_indices
                and "tool_execution_result" in str(msg.get("content", ""))
            ):
                # Old tool result: extract minimal info and replace with stub.
                try:
                    content_str = msg.get("content", "")
                    data = (
                        json.loads(content_str) if isinstance(content_str, str) else {}
                    )
                    res = data.get("tool_execution_result", {})
                    tool_name = res.get("tool_name") or res.get("name") or "tool"
                    is_ok = bool(res.get("ok") or res.get("status") == "ok")
                    status = "ok" if is_ok else "error"

                    # Keep if current step hint appears in the full content.
                    if (
                        current_step_hint
                        and current_step_hint.lower() in str(content_str).lower()
                    ):
                        pruned.append(msg)
                        continue

                    stub = json.dumps(
                        {
                            "tool_execution_result": {
                                "tool_name": tool_name,
                                "status": status,
                                "_pruned": True,
                                "_note": "Full output pruned (stale — >3 turns ago). Use read_file to re-fetch if needed.",
                            }
                        }
                    )
                    pruned.append({"role": "user", "content": stub})
                except Exception:
                    # Never fail pruning: keep original on any error.
                    pruned.append(msg)
            else:
                pruned.append(msg)

        return pruned

    def _truncate_to_token_budget(self, text: str, budget: int) -> str:
        """Binary-search truncation: O(log N) tokeniser calls instead of O(N).

        Finds the longest prefix of *text* whose token count is <= *budget*.
        Falls back to empty string if even a single character exceeds the budget.
        """
        if self.token_estimator(text) <= budget:
            return text
        lo, hi = 0, len(text)
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.token_estimator(text[:mid]) <= budget:
                lo = mid
            else:
                hi = mid
        return text[:lo]

    def _truncate_text(self, text: str, max_tokens: int) -> str:
        # First, handle the base case: if text already fits, no truncation needed.
        if self.token_estimator(text) <= max_tokens:
            return text

        marker = "\n\n[TRUNCATED]"
        marker_tokens = self.token_estimator(marker)

        # If max_tokens is too small to even fit the marker, return whatever fits.
        if max_tokens < marker_tokens:
            # P1-D fix: use binary search instead of char-by-char O(N) loop.
            return self._truncate_to_token_budget(text, max_tokens)

        # Now, we know there's enough space for at least the marker.
        # Calculate budget for the actual content before the marker.
        content_budget_for_truncation = max(0, max_tokens - marker_tokens)

        truncated_text = text
        original_text_tokens = self.token_estimator(text)

        # Truncate content to fit content_budget_for_truncation
        if original_text_tokens > content_budget_for_truncation:
            # Heuristic starting point: slice to approximate char count, then
            # P1-D fix: binary-search fine-tune instead of char-by-char O(N) loop.
            approx_chars_per_token = (
                len(text) / original_text_tokens if original_text_tokens > 0 else 4
            )
            target_char_limit = max(
                0, int(content_budget_for_truncation * approx_chars_per_token)
            )

            if len(truncated_text) > target_char_limit:
                truncated_text = truncated_text[:target_char_limit]

            # P1-D fix: binary-search fine-tune instead of O(N) char-by-char loop.
            truncated_text = self._truncate_to_token_budget(
                truncated_text, content_budget_for_truncation
            )

            # If truncation actually occurred (original text was longer than what fits in content_budget_for_truncation)
            # and we have space for the marker, add it.
            if (
                self.token_estimator(text) > self.token_estimator(truncated_text)
                and self.token_estimator(truncated_text + marker) <= max_tokens
            ):
                return truncated_text + marker
            else:
                # If we couldn't fit the marker, or no effective truncation, just return the content within max_tokens
                return (
                    truncated_text
                    if self.token_estimator(truncated_text) <= max_tokens
                    else ""
                )  # Should already fit, but defensive
        else:
            # Content already fits within the budget for content + marker, so no truncation needed and no marker added.
            return text

    def _build_system_message(
        self, tag: str, raw_content: str, total_quota: int
    ) -> Dict[str, str]:
        # We need the final message to be <= total_quota
        # Format: <tag>\n{content}\n</tag>
        # If content needs truncation, format: <tag>\n{content}\n\n[TRUNCATED]\n</tag>

        # 1. Check if it fits without truncation
        ideal_full_msg = f"<{tag}>\n{raw_content}\n</{tag}>"
        if self.token_estimator(ideal_full_msg) <= total_quota:
            return {"role": "system", "content": ideal_full_msg}

        # 2. It doesn't fit. We need to truncate.
        # Construct the minimal wrapper with the marker to see how much budget we have for the content.
        wrapper_with_marker = f"<{tag}>\n\n\n[TRUNCATED]\n</{tag}>"
        wrapper_tokens = self.token_estimator(wrapper_with_marker)

        if total_quota <= wrapper_tokens:
            # We don't even have enough budget for the tags and the marker.
            # P1-D fix: binary-search instead of O(N) char-by-char loop.
            truncated_msg = self._truncate_to_token_budget(ideal_full_msg, total_quota)
            return {"role": "system", "content": truncated_msg}

        # 3. We have budget for the wrapper + marker + some content.
        content_budget = total_quota - wrapper_tokens

        # Heuristic starting point for content truncation
        approx_chars_per_token = (
            len(raw_content) / self.token_estimator(raw_content)
            if self.token_estimator(raw_content) > 0
            else 4
        )
        target_char_limit = max(0, int(content_budget * approx_chars_per_token))

        truncated_content = raw_content[:target_char_limit]

        # P1-D fix: binary-search fine-tune instead of O(N) char-by-char loop.
        # We search on raw_content directly, using content_budget as the constraint.
        truncated_content = self._truncate_to_token_budget(
            truncated_content, content_budget
        )

        return {
            "role": "system",
            "content": f"<{tag}>\n{truncated_content}\n\n[TRUNCATED]\n</{tag}>",
        }

    def get_token_usage(self) -> tuple[int, int]:
        """Return (used, max) for TokenBudgetMonitor."""
        return self._last_token_count, self._max_tokens

    def update_token_count(self, count: int):
        """Update the last token count after building context."""
        self._last_token_count = count
