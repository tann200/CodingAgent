"""Slash commands for direct user control.

Similar to OpenClaw's CommandRegistry, this module provides
slash commands like /help, /skills, /session, /history, etc.
"""

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.orchestration.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class Command(ABC):
    """Base class for slash commands."""

    name: str = ""
    aliases: List[str] = []
    description: str = ""

    @abstractmethod
    def execute(self, args: str, session: Optional[Dict[str, Any]] = None) -> str:
        """Execute the command and return response string."""
        pass


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

        lines = ["**Available Skills:**", "- (skill listing not implemented)"]
        return "\n".join(lines)


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

        return "\n".join(lines)


class CommandRegistry:
    """Registry for slash commands."""

    def __init__(self, orchestrator: Optional["Orchestrator"] = None):
        self._commands: Dict[str, Command] = {}
        self._orchestrator = orchestrator
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register default commands."""
        self.register(HelpCommand(self))
        self.register(SessionCommand())
        self.register(HistoryCommand())
        self.register(CancelCommand())
        self.register(StatusCommand(self._orchestrator))
        self.register(SkillsCommand())

    def register(self, cmd: Command) -> None:
        """Register a command."""
        self._commands[cmd.name] = cmd
        for alias in cmd.aliases:
            self._commands[alias] = cmd

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

        # Parse command and args
        match = re.match(r"/(\w+)\s*(.*)", input)
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


def get_command_registry(
    orchestrator: Optional["Orchestrator"] = None,
) -> CommandRegistry:
    """Get the command registry singleton."""
    global _command_registry
    if _command_registry is None:
        _command_registry = CommandRegistry(orchestrator)
    return _command_registry
