from __future__ import annotations
from typing import Callable, Dict, List, Optional
import math
import json
import threading
from collections import OrderedDict
from pathlib import Path

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
        """
        with _CACHE_LOCK:
            _TEXT_CACHE.clear()
            _JSON_CACHE.clear()

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
        if working_dir:
            self._agent_context_dir = Path(working_dir) / ".agent-context"
        else:
            # Derive from this file's location (src/core/context/ → repo root)
            self._agent_context_dir = (
                Path(__file__).resolve().parents[3] / ".agent-context"
            )

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

        # Load all skills by name
        self.skills: Dict[str, str] = {}
        skills_dir = config_root / "skills"
        if skills_dir.exists():
            for skill_file in skills_dir.glob("*.md"):
                skill_name = skill_file.stem
                self.skills[skill_name] = self._read_text_cached(skill_file) or ""

    def get_skill(self, skill_name: str) -> str:
        """Get skill content by name."""
        return self.skills.get(skill_name, "")

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
                ac = cwd / ".agent-context"
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
        try:
            from src.core.indexing.vector_store import VectorStore

            _vs = VectorStore(workdir=str(self._agent_context_dir.parent))
            results = _vs.search_memories(query=task, limit=limit)
            if not results:
                return ""
            lines = [
                "<prior_context>",
                "Relevant context from previous sessions:",
            ]
            for r in results:
                # Each result is a dict — extract the text field.
                text = r.get("text") or r.get("content") or str(r)
                lines.append(f"- {str(text)[:250]}")
            lines.append("</prior_context>")
            return "\n".join(lines)
        except Exception:
            return ""

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

    def _select_prompt_partial(
        self,
        model_tier: Optional[str],
        provider_capabilities: Optional[Dict],
        is_reasoning: bool = False,
    ) -> str:
        """S1-B: Choose the right prompt partial for the active model.

        Selection priority:
        1. Reasoning model → reasoning.md
        2. Provider family: anthropic → anthropic.txt, openai → openai.txt
        3. Tier: NANO/SMALL → local-small.md, MEDIUM → local-medium.md
        4. Default → default.txt
        """
        if is_reasoning:
            partial = self._load_prompt_partial("reasoning.md")
            if partial:
                return partial

        caps = provider_capabilities or {}
        provider_family = caps.get("provider_family", "").lower()

        if "anthropic" in provider_family:
            partial = self._load_prompt_partial("anthropic.txt")
            if partial:
                return partial

        if "openai" in provider_family or "openrouter" in provider_family:
            partial = self._load_prompt_partial("openai.txt")
            if partial:
                return partial

        tier = (model_tier or "").lower()
        if tier in ("nano", "small"):
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
            is_minimal = tier in (ModelTier.NANO, ModelTier.SMALL)
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
    ) -> List[Dict[str, str]]:
        # F10: Use dynamic token budget when max_tokens is not explicitly provided.
        if max_tokens is None:
            max_tokens = _default_max_tokens()
        # Token budgeting rules
        identity_quota = min(math.ceil(0.12 * max_tokens), 800)
        role_quota = min(math.ceil(0.12 * max_tokens), 800)
        tools_quota = min(math.ceil(0.06 * max_tokens), 400)
        conversation_quota = max(
            0, max_tokens - (identity_quota + role_quota + tools_quota + 500)
        )  # buffer for tags

        built_messages: List[Dict[str, str]] = []

        # 1. System Block (Identity + Role + Skills + Tools)
        # We consolidate these into a single system message for better compatibility
        system_parts = []

        # Load role content from agent-brain based on role_name
        role_content = self.roles.get(role_name, "")
        safe_identity = self._sanitize_text(self.soul)
        safe_role = self._sanitize_text(role_content)
        safe_task_description = self._sanitize_text(task_description)

        system_parts.append(f"<identity>\n{safe_identity}\n</identity>")
        system_parts.append(f"<role>\n{safe_role}\n</role>")

        # CP-11: Ancestor instruction file injection.
        # Walk from the workspace root up to the filesystem root collecting
        # AGENTS.md, AGENTS.local.md, .agent/AGENTS.md, .agent/instructions.md.
        # Files are deduplicated by content hash and rendered with per-file (4k)
        # and total (12k) character budgets.  Injected before the dynamic
        # boundary so this content is part of the static cacheable block.
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
                    system_parts.append(
                        f"<project_instructions>\n{_instr_block}\n</project_instructions>"
                    )
        except Exception:
            pass  # never fail prompt build due to instruction file errors

        # CP-12: Dynamic-boundary sentinel.  Everything appended after this
        # point is session/turn-specific and should NOT be included in the
        # static cache block.  The Anthropic adapter (and any future
        # cache-aware adapter) splits on this constant.
        system_parts.append(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)

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
                system_parts.append(
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
                system_parts.append(
                    f"<task_progress>\n{todo_content}\n</task_progress>"
                )
        except Exception:
            pass  # never fail prompt building due to missing TODO.md

        # 1b. Repository Intelligence block (if any retrieved snippets provided)
        # Step 8: If a ContextController is available, run enforce_budget() to drop or
        # summarize snippets that would overflow the available token budget.
        if retrieved_snippets and context_controller is not None:
            try:
                # Convert snippet dicts to the file-descriptor format enforce_budget expects
                _sys_text = "\n".join(system_parts)
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
                    import logging as _logging

                    _logging.getLogger(__name__).debug(
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
                    system_parts.append(repo_block)
            except Exception:
                # best-effort: do not fail prompt build
                pass

        if active_skills:
            # active_skills is now a list of skill names to load from files
            skill_contents = []
            for skill_name in active_skills:
                skill_content = self.get_skill(skill_name)
                if skill_content:
                    skill_contents.append(self._sanitize_text(skill_content))
            if skill_contents:
                system_parts.append(
                    f"<active_skills>\n{chr(10).join(skill_contents)}\n</active_skills>"
                )

        # S1-C: Prune tool list based on model tier before rendering.
        if model_tier:
            tools = self._prune_tools(tools, model_tier)

        # S8-A: Render tools with tier-appropriate verbosity.
        tools_text = self._render_tools_for_tier(tools, model_tier)
        system_parts.append(f"<available_tools>\n{tools_text}\n</available_tools>")

        # S1-B: Inject per-provider / per-tier prompt partial after skills and tools.
        try:
            _is_reasoning = False
            try:
                from src.core.inference.thinking_utils import is_reasoning_model

                _active_model = (provider_capabilities or {}).get("model", "")
                _is_reasoning = bool(
                    _active_model and is_reasoning_model(_active_model)
                )
            except Exception:
                pass
            _partial = self._select_prompt_partial(
                model_tier, provider_capabilities, _is_reasoning
            )
            if _partial:
                system_parts.append(f"<model_guidance>\n{_partial}\n</model_guidance>")
        except Exception:
            pass  # never fail prompt build due to missing partial

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
                system_parts.append(_lsp_block)
        except Exception:
            pass  # never fail prompt build due to LSP errors

        # 1.5 Mandatory Output Format (Last part of system instructions)
        # Phase 7: Conditional format based on provider capabilities
        # S8-B: For NANO tier (simple_mode), force native tools off and restrict to one tool.
        _is_simple_mode = False
        try:
            from src.core.inference.model_tiers import (
                ModelTier,
                is_simple_mode as _check_simple,
            )

            _is_simple_mode = (
                _check_simple(ModelTier(model_tier)) if model_tier else False
            )
        except Exception:
            pass

        use_native_tools = (
            not _is_simple_mode  # NANO never uses native tools
            and provider_capabilities is not None
            and provider_capabilities.get("supports_native_tools", False)
        )

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
        elif _is_simple_mode:
            # S8-B: NANO simple_mode — strict single YAML tool rule in output format.
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
        system_parts.append(format_instr)

        built_messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # 2. Conversation Logic
        # Filter msg_mgr to only include User and Assistant messages (strip system prompts)
        filtered_conv = [
            {
                "role": m.get("role"),
                "content": self._sanitize_text(m.get("content", "")),
            }
            for m in conversation
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

        return built_messages

    def _truncate_text(self, text: str, max_tokens: int) -> str:
        # First, handle the base case: if text already fits, no truncation needed.
        if self.token_estimator(text) <= max_tokens:
            return text

        marker = "\n\n[TRUNCATED]"
        marker_tokens = self.token_estimator(marker)

        # If max_tokens is too small to even fit the marker, return empty or what minimal fits.
        # This ensures we don't try to add a marker that itself exceeds the budget.
        if max_tokens < marker_tokens:
            # Try to fit as much of the original text as possible within max_tokens
            truncated_text = ""
            for i in range(len(text)):
                if self.token_estimator(text[: i + 1]) <= max_tokens:
                    truncated_text = text[: i + 1]
                else:
                    break
            return truncated_text  # No marker in this case

        # Now, we know there's enough space for at least the marker.
        # Calculate budget for the actual content before the marker.
        content_budget_for_truncation = max(0, max_tokens - marker_tokens)

        truncated_text = text
        original_text_tokens = self.token_estimator(text)

        # Truncate content to fit content_budget_for_truncation
        if original_text_tokens > content_budget_for_truncation:
            # Heuristic for character limit based on average chars/token for efficiency
            approx_chars_per_token = (
                len(text) / original_text_tokens if original_text_tokens > 0 else 4
            )
            target_char_limit = max(
                0, int(content_budget_for_truncation * approx_chars_per_token)
            )

            if len(truncated_text) > target_char_limit:
                truncated_text = truncated_text[:target_char_limit]

            # Fine-tune by removing characters one by one until within budget
            while (
                self.token_estimator(truncated_text) > content_budget_for_truncation
                and len(truncated_text) > 0
            ):
                truncated_text = truncated_text[:-1]

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
            # Just return whatever we can fit of the ideal full message, no marker guarantees.
            truncated_msg = ""
            for i in range(len(ideal_full_msg)):
                if self.token_estimator(ideal_full_msg[: i + 1]) <= total_quota:
                    truncated_msg = ideal_full_msg[: i + 1]
                else:
                    break
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

        # Function to test a specific truncated content length
        def test_fit(content_candidate):
            return (
                self.token_estimator(
                    f"<{tag}>\n{content_candidate}\n\n[TRUNCATED]\n</{tag}>"
                )
                <= total_quota
            )

        # If it's too big, shrink it
        while not test_fit(truncated_content) and len(truncated_content) > 0:
            truncated_content = truncated_content[:-1]

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
