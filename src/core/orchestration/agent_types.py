"""
agent_types.py — Typed agent definitions for CodingAgent.

Provides the AgentDefinition dataclass and the built-in agent registry.
This mirrors OpenCode's packages/opencode/src/agent/agent.ts and
claw-code-main's SubagentToolExecutor allowlist pattern, adapted for the
Python / LangGraph architecture.

Design principles:
- AgentDefinition is a plain dataclass — no LangGraph coupling.
    - Built-in agents are module-level singletons; custom agents can be loaded
    from the path returned by ``src.core.paths.get_agents_path()`` (user-level
    agents.json) or passed in at runtime.
- The registry supports get(), list(), register(), and is the single source
  of truth for "what is this agent allowed to do?".
- AgentDefinition integrates with:
    role_config.py  — allowed/denied tool lists
    permission_policy.py  — wildcard permission rules
    agent_brain.py  — system prompt content
    SystemPromptBuilder  — prompt assembly
"""

from __future__ import annotations

import json
import os
import shutil
import logging
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set

from src.core.paths import get_agents_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AgentMode
# ---------------------------------------------------------------------------

AgentMode = Literal["primary", "subagent", "internal"]
"""
primary  — top-level interactive agents (build, plan); shown to users.
subagent — delegated specialist agents (explore, verification); spawned by task tool.
internal — utility agents (compaction, title); never shown; no tool loop.
"""


# ---------------------------------------------------------------------------
# AgentDefinition
# ---------------------------------------------------------------------------


@dataclass
class AgentDefinition:
    """Complete specification of an agent.

    Fields
    ------
    id : str
        Canonical identifier. Used as the key in AGENT_REGISTRY and as the
        ``role`` string passed to subagent_tools.delegate_task().
    name : str
        Human-readable display name.
    description : str
        One-sentence description shown in the agent selection dialog.
    mode : AgentMode
        Whether this is a top-level (primary), delegated (subagent), or
        internal utility (internal) agent.
    prompt_override : str | None
        When set, this string **completely replaces** the base section of the
        system prompt (the role/soul/laws block from AgentBrainManager).
        The dynamic section (env block, git context, instructions) is still
        appended unless ``include_dynamic_prompt`` is False.
    include_dynamic_prompt : bool
        If False, skip the dynamic section (git context, project instructions,
        AGENT.md discovery).  Default True.  Set to False for internal agents
        like compaction where environment detail is noise.
    toolset : str | None
        Name of a YAML toolset file in src/config/toolsets/ that provides the
        base tool list.  If None, the agent inherits all registered tools.
    allowed_tools : set[str] | None
        If non-None, the agent is restricted to exactly these tool names
        (after toolset loading).  Acts as an allowlist enforced at runtime in
        execute_tool().
    denied_tools : set[str]
        Tools always denied for this agent regardless of toolset.  Evaluated
        after allowed_tools.
    temperature : float | None
        Model temperature override.  None means use provider default.
    max_rounds : int
        Maximum loop iterations before the agent is forced to stop.
    hidden : bool
        If True, the agent is not shown in the TUI agent selection dialog.
    extra : dict
        Arbitrary extra metadata for extensions.
    """

    id: str
    name: str
    description: str
    mode: AgentMode = "primary"
    prompt_override: Optional[str] = None
    include_dynamic_prompt: bool = True
    toolset: Optional[str] = None
    allowed_tools: Optional[Set[str]] = None
    denied_tools: Set[str] = field(default_factory=set)
    temperature: Optional[float] = None
    max_rounds: int = 20
    hidden: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)
    # PERM-W4: per-agent permission rule overrides.  These rules are merged with
    # (appended after) the global PermissionPolicy rules, so agent-specific
    # rules take precedence over global ones (last-matching-wins semantics).
    permission_rules: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def is_tool_permitted(self, tool_name: str) -> bool:
        """Return False if *tool_name* is denied or outside the allowlist."""
        if tool_name in self.denied_tools:
            return False
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            return False
        return True

    def get_merged_policy(self, base_policy: "Any | None" = None) -> "Any | None":
        """Return a PermissionPolicy that merges *base_policy* rules with this agent's
        ``permission_rules``.  Agent-specific rules are appended last so they take
        precedence (last-matching-wins semantics).  Returns *base_policy* unchanged
        when ``permission_rules`` is empty.
        """
        if not self.permission_rules:
            return base_policy
        try:
            from src.core.orchestration.permission_policy import (
                PermissionPolicy,
                PermissionRule,
            )

            base_rules: list = []
            if base_policy is not None:
                base_rules = list(getattr(base_policy, "_rules", []))
            agent_rules = [PermissionRule.from_dict(r) for r in self.permission_rules]
            return PermissionPolicy(rules=base_rules + agent_rules)
        except Exception as exc:
            logger.debug("AgentDefinition.get_merged_policy: error %s", exc)
            return base_policy

    def effective_max_rounds(self) -> int:
        return self.max_rounds

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "mode": self.mode,
            "prompt_override": self.prompt_override,
            "include_dynamic_prompt": self.include_dynamic_prompt,
            "toolset": self.toolset,
            "allowed_tools": sorted(self.allowed_tools)
            if self.allowed_tools is not None
            else None,
            "denied_tools": sorted(self.denied_tools),
            "temperature": self.temperature,
            "max_rounds": self.max_rounds,
            "hidden": self.hidden,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentDefinition":
        allowed = data.get("allowed_tools")
        denied = data.get("denied_tools", [])
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            mode=data.get("mode", "primary"),
            prompt_override=data.get("prompt_override"),
            include_dynamic_prompt=data.get("include_dynamic_prompt", True),
            toolset=data.get("toolset"),
            allowed_tools=set(allowed) if allowed is not None else None,
            denied_tools=set(denied),
            temperature=data.get("temperature"),
            max_rounds=data.get("max_rounds", 20),
            hidden=data.get("hidden", False),
            extra=data.get("extra", {}),
        )


# ---------------------------------------------------------------------------
# Built-in agent definitions (AGENT-02)
# ---------------------------------------------------------------------------

#: Full-access primary agent — the default for all interactive sessions.
BUILD_AGENT = AgentDefinition(
    id="build",
    name="Build",
    description=(
        "Full-access coding agent. Reads, writes, runs tests, executes shell "
        "commands, and delegates to subagents. Default for all coding tasks."
    ),
    mode="primary",
    toolset="coding",
    denied_tools=set(),
    max_rounds=30,
)

#: Full-access subagent — parallel-capable workhorse for research+execution.
#: Mirrors opencode's 'general' subagent type (agent.ts:146-159).
GENERAL_AGENT = AgentDefinition(
    id="general",
    name="General",
    description=(
        "Full-access workhorse subagent. Reads, writes, searches, runs tests, "
        "and executes shell commands. Designed for parallel research and "
        "execution tasks where read-only analysis is insufficient."
    ),
    mode="subagent",
    toolset="coding",
    allowed_tools=None,
    denied_tools=set(),
    max_rounds=15,
    include_dynamic_prompt=True,
)

#: Read-only exploration subagent — spawned by the build agent for codebase
#: reconnaissance before writing.  Mirrors OpenCode's 'explore' agent and
#: claw-code's 'Explore' subagent type.
EXPLORE_AGENT = AgentDefinition(
    id="explore",
    name="Explore",
    description=(
        "Read-only codebase explorer. Searches, reads, and analyses code. "
        "Cannot write, edit, delete files, run tests, or spawn further agents."
    ),
    mode="subagent",
    toolset="analysis",
    # Explicit allowlist — nothing outside this set is reachable at all.
    allowed_tools={
        "read_file",
        "list_files",
        "glob",
        "grep",
        "search_code",
        "find_symbol",
        "find_references",
        "bash_readonly",
        "web_search",
        "read_web_page",
        "memory_search",
        "analyze_repository",
        "initialize_repo_intelligence",
        "multi_file_summary",
        "batched_file_read",
        "load_skill",
        "list_skills",
        "batch",
    },
    denied_tools={
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "multiedit",
        "delete_file",
        "apply_patch",
        "generate_patch",
        "bash",
        "run_tests",
        "run_linter",
        "run_js_tests",
        "run_ts_check",
        "delegate_task",
        "ask_user",
        "submit_plan_for_review",
        "git_commit",
        "manage_todo",
    },
    max_rounds=15,
    include_dynamic_prompt=True,
    # Explore agents get a focused prompt override — concise, no creativity.
    prompt_override=(
        "You are a read-only codebase exploration agent. "
        "Your only job is to gather accurate information about the codebase and "
        "return a clear, structured summary. "
        "You may read files, search code, run read-only shell commands, and fetch web pages. "
        "You must NOT write, edit, delete, or create any files. "
        "You must NOT run tests, linters, or compilers. "
        "You must NOT delegate to other agents. "
        "Be thorough and precise. Return everything you find that is relevant to the delegated task."
    ),
)

#: Plan-mode primary agent — write-restricted, focused on structured planning.
#: Mirrors OpenCode's 'plan' agent and PlanMode in the existing orchestrator.
PLAN_AGENT = AgentDefinition(
    id="plan",
    name="Plan",
    description=(
        "Plan-first agent. Reads the codebase and produces a structured plan. "
        "Cannot write or edit files until the plan is approved and the session "
        "transitions to the build agent."
    ),
    mode="primary",
    toolset="planning",
    allowed_tools=None,  # start from full toolset, deny writes below
    denied_tools={
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "multiedit",
        "delete_file",
        "apply_patch",
        "generate_patch",
        "git_commit",
        "bash",  # no arbitrary execution in plan mode
    },
    max_rounds=20,
    # Plan agents get a reminder injected on top of the base prompt; the
    # reminder is added by SystemPromptBuilder when active_agent.id == "plan".
    prompt_override=None,
)

#: Verification subagent — runs tests and linters, reports results.
#: Mirrors claw-code's 'Verification' subagent type.
VERIFICATION_AGENT = AgentDefinition(
    id="verification",
    name="Verification",
    description=(
        "Test and lint runner. Reads code, runs the test suite and linter, "
        "and returns a structured quality report. Cannot modify files."
    ),
    mode="subagent",
    toolset="review",
    allowed_tools={
        "read_file",
        "list_files",
        "glob",
        "grep",
        "search_code",
        "find_symbol",
        "find_references",
        "bash_readonly",
        "bash",  # allowed for running test commands
        "run_tests",
        "run_linter",
        "syntax_check",
        "multi_file_summary",
        "batched_file_read",
        "git_status",
        "git_diff",
        "git_log",
        "batch",
    },
    denied_tools={
        "write_file",
        "edit_file",
        "edit_file_atomic",
        "edit_by_line_range",
        "multiedit",
        "delete_file",
        "apply_patch",
        "git_commit",
        "delegate_task",
        "ask_user",
        "submit_plan_for_review",
    },
    max_rounds=10,
    prompt_override=(
        "You are a verification agent. Run the test suite and linter, then "
        "return a structured report: which tests passed, which failed, any lint "
        "warnings, and a short summary of overall quality. "
        "Do not modify any files. Report only — do not attempt to fix issues."
    ),
)

#: Internal compaction agent — summarises conversation history for context
#: management.  Never shown to users, never enters the tool loop.
COMPACTION_AGENT = AgentDefinition(
    id="compaction",
    name="Compaction",
    description="Internal. Summarises conversation history for context compaction.",
    mode="internal",
    toolset=None,
    allowed_tools=set(),  # no tools — pure LLM call
    denied_tools=set(),
    max_rounds=1,
    hidden=True,
    include_dynamic_prompt=False,
    prompt_override=(
        "You are a context compaction assistant. "
        "You will receive a conversation history. "
        "Produce a concise, structured summary that preserves:\n"
        "- The user's original task and goal\n"
        "- All decisions made and reasons why\n"
        "- All files created or modified (with paths)\n"
        "- Current state: what has been done, what still needs doing\n"
        "- Any blockers or open questions\n"
        "Be precise and terse. Use bullet points. Do not editorialize."
    ),
)

#: Internal title-generation agent — one-shot, no tools.
TITLE_AGENT = AgentDefinition(
    id="title",
    name="Title",
    description="Internal. Generates a short session title from the first user message.",
    mode="internal",
    toolset=None,
    allowed_tools=set(),
    denied_tools=set(),
    max_rounds=1,
    hidden=True,
    include_dynamic_prompt=False,
    temperature=0.5,
    prompt_override=(
        "Generate a concise 3-7 word title for a coding session based on the "
        "user's first message. Output only the title text, nothing else."
    ),
)


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Thread-safe registry of AgentDefinition instances.

    The registry is pre-populated with built-in agents and can be extended
    with custom agents loaded from the user data directory (see
    ``src.core.paths.get_agents_path()``) or passed
    programmatically.
    """

    _BUILTIN_AGENTS: List[AgentDefinition] = [
        BUILD_AGENT,
        GENERAL_AGENT,
        EXPLORE_AGENT,
        PLAN_AGENT,
        VERIFICATION_AGENT,
        COMPACTION_AGENT,
        TITLE_AGENT,
    ]

    def __init__(self) -> None:
        self._agents: Dict[str, AgentDefinition] = {}
        for agent in self._BUILTIN_AGENTS:
            self._agents[agent.id] = agent

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(self, agent: AgentDefinition, *, allow_override: bool = False) -> None:
        """Register a custom agent.  Raises ValueError if id conflicts with a
        built-in agent and allow_override is False."""
        builtin_ids = {a.id for a in self._BUILTIN_AGENTS}
        if agent.id in builtin_ids and not allow_override:
            raise ValueError(
                f"Agent id '{agent.id}' conflicts with a built-in agent. "
                "Pass allow_override=True to replace it."
            )
        self._agents[agent.id] = agent
        logger.info("AgentRegistry: registered agent '%s'", agent.id)

    def get(self, agent_id: str) -> Optional[AgentDefinition]:
        """Return the AgentDefinition for *agent_id*, or None."""
        return self._agents.get(agent_id)

    def get_or_default(self, agent_id: Optional[str]) -> AgentDefinition:
        """Return the agent for *agent_id*, falling back to BUILD_AGENT."""
        if agent_id:
            found = self._agents.get(agent_id)
            if found:
                return found
            logger.warning(
                "AgentRegistry: unknown agent id '%s'; using build agent", agent_id
            )
        return BUILD_AGENT

    def list(
        self, *, include_hidden: bool = False, mode: Optional[AgentMode] = None
    ) -> List[AgentDefinition]:
        """Return all registered agents, optionally filtered."""
        result = list(self._agents.values())
        if not include_hidden:
            result = [a for a in result if not a.hidden]
        if mode is not None:
            result = [a for a in result if a.mode == mode]
        return result

    def list_ids(self) -> List[str]:
        return list(self._agents.keys())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load_custom_agents(self, path: Optional[Path] = None) -> int:
        """Load custom agent definitions from a JSON file.

        The file is expected to contain a JSON array of agent definition
        objects (as produced by AgentDefinition.to_dict()).

        Returns the number of agents loaded.
        """
        if path is None:
            path = get_agents_path()
        if not path.exists():
            return 0
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                logger.warning(
                    "AgentRegistry: agents.json must be a JSON array; skipping"
                )
                return 0
            count = 0
            for item in data:
                try:
                    agent = AgentDefinition.from_dict(item)
                    self.register(agent, allow_override=True)
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "AgentRegistry: failed to load agent from dict: %s", exc
                    )
            logger.info("AgentRegistry: loaded %d custom agent(s) from %s", count, path)
            return count
        except Exception as exc:
            logger.warning("AgentRegistry: failed to read %s: %s", path, exc)
            return 0

    def save_custom_agents(self, path: Optional[Path] = None) -> None:
        """Persist non-built-in agents to *path* as JSON."""
        if path is None:
            path = get_agents_path()
        builtin_ids = {a.id for a in self._BUILTIN_AGENTS}
        custom = [a.to_dict() for a in self._agents.values() if a.id not in builtin_ids]
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from src.core.io_utils import atomic_write_json

            ok = atomic_write_json(path, custom, logger=logger)
            if ok:
                logger.info(
                    "AgentRegistry: saved %d custom agent(s) to %s", len(custom), path
                )
                return
            logger.warning(
                "AgentRegistry: atomic_write_json returned False for %s; falling back",
                path,
            )
        except Exception:
            logger.debug(
                "AgentRegistry: atomic_write_json unavailable or failed for %s; falling back\n%s",
                path,
                traceback.format_exc(),
            )

        # Fallback: write via mkstemp -> os.replace with fsync and cleanup,
        # final fallback to Path.write_text only if necessary.
        try:
            import tempfile

            fd = None
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        fd = None
                        json.dump(custom, f, indent=2)
                        try:
                            f.flush()
                            os.fsync(f.fileno())
                        except Exception:
                            pass
                    try:
                        os.replace(tmp_path, str(path))
                    except Exception:
                        try:
                            shutil.move(tmp_path, str(path))
                        except Exception:
                            # final fallback
                            path.write_text(
                                json.dumps(custom, indent=2), encoding="utf-8"
                            )
                except Exception:
                    try:
                        if fd is not None:
                            os.close(fd)
                    except Exception:
                        pass
                    raise
            except Exception:
                try:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass
                raise
            logger.info(
                "AgentRegistry: saved %d custom agent(s) to %s", len(custom), path
            )
        except Exception:
            logger.exception("AgentRegistry: failed to write custom agents to %s", path)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_REGISTRY: Optional[AgentRegistry] = None


def get_agent_registry() -> AgentRegistry:
    """Return the global AgentRegistry singleton, loading custom agents on
    first access."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = AgentRegistry()
        _REGISTRY.load_custom_agents()
    return _REGISTRY


def get_agent(agent_id: str) -> Optional[AgentDefinition]:
    """Convenience: look up an agent by id from the global registry."""
    return get_agent_registry().get(agent_id)


def get_agent_or_default(agent_id: Optional[str]) -> AgentDefinition:
    """Convenience: look up an agent by id, falling back to build agent."""
    return get_agent_registry().get_or_default(agent_id)
