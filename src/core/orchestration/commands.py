"""Slash commands for direct user control.

Similar to OpenClaw's CommandRegistry, this module provides
slash commands like /help, /skills, /session, /history, etc.

Extended Design (P1-5)
----------------------
In addition to the class-based ``Command`` ABC, the registry now supports
**callable handlers** — plain functions or async coroutines — registered via
``CommandRegistry.register_handler()``.  This lets the TUI layer (``app.py``)
register its ``_slash_*`` methods without requiring a full ``Command`` subclass
per command, while still benefiting from the single source of truth for names,
descriptions, and aliases.

Usage::

    registry.register_handler(
        name="clear",
        description="Clear chat output",
        handler=self._slash_clear,
        aliases=["cls"],
    )

The ``list_metadata()`` method returns a list of ``SlashCommandMeta`` dicts
(``name``, ``description``, ``aliases``) that can be used to auto-generate
``SLASH_COMMANDS`` and ``SLASH_COMMAND_DESCRIPTIONS`` in the TUI without
duplicating the data.
"""

import re
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.orchestration.orchestrator import Orchestrator

try:
    from src.tools.git_tools import git_diff as _git_diff
except Exception:
    _git_diff = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# P1-5: SlashCommandMeta — lightweight metadata record
# ---------------------------------------------------------------------------

class SlashCommandMeta:
    """Metadata for a single slash command (name, description, aliases).

    Returned by ``CommandRegistry.list_metadata()`` so callers can build
    autocomplete lists and help text without coupling to ``Command`` internals.
    """

    __slots__ = ("name", "description", "aliases")

    def __init__(self, name: str, description: str, aliases: List[str]) -> None:
        self.name = name
        self.description = description
        self.aliases: List[str] = aliases

    def __repr__(self) -> str:  # pragma: no cover
        return f"SlashCommandMeta(name={self.name!r}, description={self.description!r}, aliases={self.aliases!r})"


class Command(ABC):
    """Base class for slash commands."""

    name: str = ""
    aliases: List[str] = []
    description: str = ""

    @abstractmethod
    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        """Execute the command and return response string."""
        pass


class _CallableCommand(Command):
    """Wraps a plain callable or coroutine as a ``Command`` instance.

    Used internally by ``CommandRegistry.register_handler()`` so that the
    TUI can register ``_slash_*`` methods without writing a full subclass.
    The callable receives ``(args: str)`` as its only positional argument;
    ``session`` is ignored (TUI handlers don't need it).
    """

    def __init__(
        self,
        name: str,
        description: str,
        handler: Callable,
        aliases: Optional[List[str]] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.aliases = aliases or []
        self._handler = handler

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        """Call the handler.  If it returns a coroutine, run it."""
        result = self._handler(args)
        import inspect
        import asyncio
        if inspect.isawaitable(result):
            # Previously this returned a sentinel and silently dropped the
            # coroutine (GC collected it), losing the handler's side effects.
            # Fix: actually run/schedule the coroutine instead.
            try:
                _loop = asyncio.get_running_loop()
            except RuntimeError:
                _loop = None
            if _loop is None:
                # No running loop -- run synchronously.
                result = asyncio.run(result)
            else:
                # Already in an async context -- schedule as a task so it runs.
                _loop.create_task(result)
                return f"/{self.name}: async handler scheduled."
        return result if isinstance(result, str) else ""


class HelpCommand(Command):
    name = "help"
    aliases = ["?"]
    description = "Show available commands"

    def __init__(self, registry: "CommandRegistry"):
        self._registry = registry

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        lines = ["**Available Commands:**"]
        for cmd in self._registry.list_commands():
            alias_str = f" ({', '.join(cmd.aliases)})" if cmd.aliases else ""
            lines.append(f"- /{cmd.name}{alias_str} - {cmd.description}")
        return "\n".join(lines)


class SkillsCommand(Command):
    name = "skills"
    aliases = ["skill"]
    description = "List all skills or show skill details"

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        if args:
            return f"Skill details for '{args}' not implemented. Use /skills to list available."

        try:
            from src.core.orchestration.agent_brain import get_agent_brain_manager
            summary = get_agent_brain_manager().list_skills_summary()
            if summary:
                return f"**Available Skills:**\n{summary}"
        except Exception:
            pass
        return "**Available Skills:**\n- (no skills loaded)"


class SessionCommand(Command):
    name = "session"
    aliases = []
    description = "Show current session details"

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        if not session:
            return "No active session"

        session_id = session.get("session_id", "unknown")
        messages = len(session.get("history", []))

        lines = [
            f"**Session ID:** `{session_id[:8]}...`",
            f"**Messages:** {messages}",
        ]
        return "\n".join(lines)


class HistoryCommand(Command):
    name = "history"
    aliases = ["hist"]
    description = "Show conversation history summary"

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        if not session:
            return "No active session"

        history = session.get("history", [])
        if not history:
            return "No conversation history"

        lines = ["**Recent Messages:**"]
        for msg in history[-5:]:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")[:50]
            lines.append(f"- {role}: {content}...")

        return "\n".join(lines)


class CancelCommand(Command):
    name = "cancel"
    aliases = ["abort"]
    description = "Cancel the current task"

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        return "Task cancellation not implemented. Use Ctrl+C in terminal."


class UndoCommand(Command):
    name = "undo"
    aliases = ["revert"]
    description = "Undo the last file changes (uses rollback snapshots)"

    def __init__(
        self,
        orchestrator: Optional["Orchestrator"] = None,
        rollback_manager: Optional[Any] = None,
    ):
        self._orchestrator = orchestrator
        self._rollback_manager = rollback_manager

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        rm = self._rollback_manager
        if rm is None:
            return "Rollback not available in this session."

        arg = args.strip().lower()

        if not arg:
            snapshots = rm.list_snapshots()
            if not snapshots:
                return "No snapshots available to undo."

            lines = ["**Recent Snapshots:**"]
            for snap in snapshots[:5]:
                sid = snap.get("snapshot_id", "")[:12]
                files = snap.get("file_count", 0)
                ts = snap.get("timestamp", "")
                lines.append(f"- `{sid}` — {files} files ({ts})")
            lines.append("")
            lines.append("Usage: `/undo <snapshot_id>` to restore a snapshot.")
            return "\n".join(lines)

        snapshot_id = arg
        result = rm.rollback(snapshot_id)

        if result.get("status") == "success":
            restored = result.get("restored_count", 0)
            return f"✓ Restored {restored} files from snapshot `{arg or 'latest'}`"
        else:
            error = result.get("error", "Unknown error")
            return f"✗ Rollback failed: {error}"


class DiffCommand(Command):
    name = "diff"
    aliases = ["session_diff"]
    description = "Show diff of files changed in session"

    def __init__(self, orchestrator: Optional["Orchestrator"] = None):
        self._orchestrator = orchestrator

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        if not self._orchestrator:
            return "Orchestrator not available for diff."

        workdir = getattr(self._orchestrator, "working_dir", None)
        if not workdir:
            return "No working directory set."

        if _git_diff is None:
            return "git_diff tool not available."

        staged = "staged" in args.lower() or "-s" in args
        result = _git_diff(workdir=str(workdir), staged=staged)

        if result.get("status") == "ok":
            diff_text = result.get("diff", "")
            if not diff_text.strip():
                return "No changes in working tree."
            lines = ["**Diff**", "```diff", diff_text, "```"]
            return "\n".join(lines)
        else:
            error = result.get("error", "Failed to get diff")
            return f"✗ {error}"


class ContextCommand(Command):
    name = "context"
    aliases = ["tokens", "mem"]
    description = "Show token usage and context statistics"

    def __init__(self, orchestrator: Optional["Orchestrator"] = None):
        self._orchestrator = orchestrator

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        lines = ["**Context Statistics:**"]

        if self._orchestrator:
            tb = getattr(self._orchestrator, "token_monitor", None)
            if tb:
                try:
                    budget = tb.get_budget()
                    if budget:
                        used = budget.get("used_tokens", 0)
                        max_tok = budget.get("max_tokens", 0)
                        ratio = used / max_tok * 100 if max_tok > 0 else 0
                        lines.append(f"- Tokens: {used:,} / {max_tok:,} ({ratio:.1f}%)")
                except Exception:
                    pass

            history = getattr(self._orchestrator, "state", {}).get("history", [])
            if history:
                lines.append(f"- Messages: {len(history)}")

            model = getattr(self._orchestrator, "model", None)
            if model:
                lines.append(f"- Model: {model}")

        if len(lines) == 1:
            lines.append("- (no data available)")

        return "\n".join(lines)


class StatusCommand(Command):
    name = "status"
    aliases = []
    description = "Show agent status"

    def __init__(self, orchestrator: Optional["Orchestrator"] = None):
        self._orchestrator = orchestrator

    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        lines = ["**Agent Status:**", "- State: ready"]

        if self._orchestrator:
            cost = getattr(self._orchestrator, "cost_tracker", None)
            if cost:
                lines.append(f"- Cost: ${cost.get_total_cost():.4f}")
            rm = getattr(self._orchestrator, "rollback_manager", None)
            if rm:
                snaps = rm.list_snapshots()
                lines.append(f"- Snapshots: {len(snaps)}")

        return "\n".join(lines)


class CommandRegistry:
    """Registry for slash commands."""

    def __init__(
        self,
        orchestrator: Optional["Orchestrator"] = None,
        rollback_manager: Optional[Any] = None,
    ):
        self._commands: Dict[str, Command] = {}
        self._orchestrator = orchestrator
        self._rollback_manager = rollback_manager
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register default commands."""
        self.register(HelpCommand(self))
        self.register(SessionCommand())
        self.register(HistoryCommand())
        self.register(CancelCommand())
        self.register(StatusCommand(self._orchestrator))
        self.register(SkillsCommand())
        self.register(UndoCommand(self._orchestrator, self._rollback_manager))
        self.register(DiffCommand(self._orchestrator))
        self.register(ContextCommand(self._orchestrator))

    def register(self, cmd: Command) -> None:
        """Register a command."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

    def register_handler(
        self,
        name: str,
        description: str,
        handler: Callable,
        aliases: Optional[List[str]] = None,
    ) -> None:
        """Register a plain callable (or coroutine function) as a slash command.

        This is the preferred API for TUI-side handlers that are already
        implemented as ``_slash_*`` methods on ``AgentApp`` and should not
        be re-wrapped in a ``Command`` subclass.

        Args:
            name:        Command name without leading ``/`` (e.g. ``"clear"``).
            description: One-line description shown in ``/help`` and autocomplete.
            handler:     Callable with signature ``(args: str) -> str | Awaitable``.
            aliases:     Optional list of alternative names.
        """
        cmd = _CallableCommand(
            name=name,
            description=description,
            handler=handler,
            aliases=list(aliases or []),
        )
        self.register(cmd)

    def unregister(self, name: str) -> None:
        """Unregister a command by name or alias."""
        if name in self._commands:
            cmd = self._commands[name]
            del self._commands[name]
            for alias in cmd.aliases:
                self._commands.pop(alias, None)

    def get(self, name: str) -> Optional[Command]:
        """Get a command by name."""
        return self._commands.get(name)

    def list_commands(self) -> List[Command]:
        """List all registered commands (unique by name)."""
        seen = set()
        commands = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                commands.append(cmd)
        return commands

    def list_metadata(self) -> List[SlashCommandMeta]:
        """Return lightweight metadata for every registered command (unique by name).

        Suitable for building ``SLASH_COMMANDS`` / ``SLASH_COMMAND_DESCRIPTIONS``
        in the TUI without importing ``Command`` subclasses.

        Returns:
            List of :class:`SlashCommandMeta`, sorted alphabetically by name.
        """
        seen: set = set()
        result: List[SlashCommandMeta] = []
        for cmd in self._commands.values():
            if cmd.name not in seen:
                seen.add(cmd.name)
                result.append(
                    SlashCommandMeta(
                        name=cmd.name,
                        description=cmd.description,
                        aliases=list(cmd.aliases),
                    )
                )
        result.sort(key=lambda m: m.name)
        return result

    def dispatch(
        self, input: str, session: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Parse and execute a slash command.

        Args:
            input: User input string (may start with /)
            session: Optional session dict with history, session_id, etc.

        Returns:
            Command output string, or None if not a command
        """
        input = input.strip()
        if not input.startswith("/"):
            return None

        # Parse command and args — \w+ handles alphanumeric names; \W+ handles
        # punctuation aliases like "?" (e.g. /? as alias for /help).
        match = re.match(r"/(\w+|\W+?)\s*(.*)", input)
        if not match:
            return None

        name = match.group(1)
        args = match.group(2).strip()

        cmd = self.get(name)
        if not cmd:
            return f"Unknown command: /{name}. Type /help for available commands."

        try:
            return cmd.execute(args, session)
        except Exception as e:
            logger.warning(f"Command /{name} failed: {e}")
            return f"Error executing /{name}: {e}"


# Module-level singleton
_command_registry: Optional[CommandRegistry] = None
_registry_lock = threading.Lock()


def get_command_registry(
    orchestrator: Optional["Orchestrator"] = None,
    rollback_manager: Optional[Any] = None,
) -> CommandRegistry:
    """Get the command registry singleton."""
    global _command_registry
    if _command_registry is None:
        with _registry_lock:
            if _command_registry is None:
                _command_registry = CommandRegistry(orchestrator, rollback_manager)
    return _command_registry
