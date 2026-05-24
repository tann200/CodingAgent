"""Tests for P1-5: structured slash command registry extensions.

Covers:
- SlashCommandMeta dataclass
- CommandRegistry.register_handler() — callable (sync + async)
- CommandRegistry.list_metadata()
- _CallableCommand.execute() for sync and async handlers
- existing dispatch() still works after adding new API
"""

from __future__ import annotations

import asyncio
from typing import List
from unittest.mock import MagicMock

import pytest

from src.core.orchestration.commands import (
    Command,
    CommandRegistry,
    SlashCommandMeta,
    _CallableCommand,
)


# ---------------------------------------------------------------------------
# SlashCommandMeta
# ---------------------------------------------------------------------------


class TestSlashCommandMeta:
    def test_fields(self):
        m = SlashCommandMeta(name="clear", description="Clear chat", aliases=["cls"])
        assert m.name == "clear"
        assert m.description == "Clear chat"
        assert m.aliases == ["cls"]

    def test_empty_aliases(self):
        m = SlashCommandMeta(name="quit", description="Exit", aliases=[])
        assert m.aliases == []


# ---------------------------------------------------------------------------
# _CallableCommand
# ---------------------------------------------------------------------------


class TestCallableCommand:
    def test_execute_sync_handler(self):
        def handler(args: str) -> str:
            return f"echo:{args}"

        cmd = _CallableCommand(name="echo", description="Echo", handler=handler)
        assert cmd.execute("hello") == "echo:hello"

    def test_execute_non_string_return_gives_empty(self):
        def handler(args: str):
            return None

        cmd = _CallableCommand(name="noop", description="", handler=handler)
        assert cmd.execute("") == ""

    def test_execute_async_handler_returns_sentinel_string(self):
        async def async_handler(args: str) -> str:
            return "done"

        cmd = _CallableCommand(name="async_cmd", description="", handler=async_handler)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            result = cmd.execute("arg")
        assert isinstance(result, str)
        assert "async" in result.lower() or "async_cmd" in result

    def test_name_description_aliases_stored(self):
        cmd = _CallableCommand(
            name="foo",
            description="Foo command",
            handler=lambda a: "",
            aliases=["f"],
        )
        assert cmd.name == "foo"
        assert cmd.description == "Foo command"
        assert cmd.aliases == ["f"]


# ---------------------------------------------------------------------------
# CommandRegistry.register_handler()
# ---------------------------------------------------------------------------


class TestRegisterHandler:
    def _empty_registry(self) -> CommandRegistry:
        """Registry with no orchestrator to avoid side effects."""
        reg = CommandRegistry.__new__(CommandRegistry)
        reg._commands = {}
        reg._orchestrator = None
        reg._rollback_manager = None
        return reg

    def test_register_handler_adds_command(self):
        reg = self._empty_registry()
        reg.register_handler("clear", "Clear the screen", lambda a: "cleared")
        assert reg.get("clear") is not None

    def test_register_handler_with_aliases(self):
        reg = self._empty_registry()
        reg.register_handler("quit", "Exit", lambda a: "", aliases=["q", "exit"])
        assert reg.get("quit") is not None
        assert reg.get("q") is reg.get("quit")
        assert reg.get("exit") is reg.get("quit")

    def test_register_handler_dispatch_works(self):
        reg = self._empty_registry()
        reg.register_handler("hello", "Say hello", lambda a: f"Hello, {a}!")
        result = reg.dispatch("/hello world")
        assert result == "Hello, world!"

    def test_register_handler_dispatch_no_args(self):
        reg = self._empty_registry()
        reg.register_handler("ping", "Ping", lambda a: "pong")
        result = reg.dispatch("/ping")
        assert result == "pong"

    def test_register_handler_overwrites_previous(self):
        reg = self._empty_registry()
        reg.register_handler("cmd", "v1", lambda a: "v1")
        reg.register_handler("cmd", "v2", lambda a: "v2")
        assert reg.dispatch("/cmd") == "v2"

    def test_register_handler_does_not_affect_other_commands(self):
        reg = self._empty_registry()
        reg.register_handler("a", "A", lambda a: "a-result")
        reg.register_handler("b", "B", lambda a: "b-result")
        assert reg.dispatch("/a") == "a-result"
        assert reg.dispatch("/b") == "b-result"

    def test_async_handler_registered_and_dispatchable(self):
        """dispatch() should not raise even if handler is async."""
        reg = self._empty_registry()

        async def my_async(args: str) -> str:
            return "async done"

        reg.register_handler("async_cmd", "Async", my_async)
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            result = reg.dispatch("/async_cmd")
        assert isinstance(result, str)

    def test_list_commands_includes_registered_handler(self):
        reg = self._empty_registry()
        reg.register_handler("myhandler", "My handler", lambda a: "")
        names = [c.name for c in reg.list_commands()]
        assert "myhandler" in names


# ---------------------------------------------------------------------------
# CommandRegistry.list_metadata()
# ---------------------------------------------------------------------------


class TestListMetadata:
    def _empty_registry(self) -> CommandRegistry:
        reg = CommandRegistry.__new__(CommandRegistry)
        reg._commands = {}
        reg._orchestrator = None
        reg._rollback_manager = None
        return reg

    def test_returns_list_of_slash_command_meta(self):
        reg = self._empty_registry()
        reg.register_handler("foo", "Foo desc", lambda a: "")
        meta = reg.list_metadata()
        assert isinstance(meta, list)
        assert all(isinstance(m, SlashCommandMeta) for m in meta)

    def test_each_entry_has_name_description_aliases(self):
        reg = self._empty_registry()
        reg.register_handler("bar", "Bar description", lambda a: "", aliases=["b"])
        meta = reg.list_metadata()
        entry = next(m for m in meta if m.name == "bar")
        assert entry.description == "Bar description"
        assert "b" in entry.aliases

    def test_sorted_alphabetically(self):
        reg = self._empty_registry()
        for name in ["zebra", "alpha", "middle"]:
            reg.register_handler(name, f"{name} desc", lambda a: "")
        names = [m.name for m in reg.list_metadata()]
        assert names == sorted(names)

    def test_unique_by_name_not_alias(self):
        """Aliases must not appear as separate entries."""
        reg = self._empty_registry()
        reg.register_handler("quit", "Quit", lambda a: "", aliases=["q", "exit"])
        meta = reg.list_metadata()
        names = [m.name for m in meta]
        assert names.count("quit") == 1
        assert "q" not in names
        assert "exit" not in names

    def test_full_registry_list_metadata(self):
        """Default registry should expose all registered command names."""
        reg = CommandRegistry()
        meta = reg.list_metadata()
        names = {m.name for m in meta}
        expected = {"help", "status", "history", "undo", "diff", "context", "session", "cancel", "skills"}
        assert expected.issubset(names), f"Missing: {expected - names}"

    def test_list_metadata_usable_as_slash_commands_list(self):
        """Simulate building SLASH_COMMANDS and SLASH_COMMAND_DESCRIPTIONS from metadata."""
        reg = self._empty_registry()
        reg.register_handler("clear", "Clear chat output", lambda a: "")
        reg.register_handler("quit", "Exit the app", lambda a: "", aliases=["q"])

        slash_commands = [f"/{m.name}" for m in reg.list_metadata()]
        slash_descriptions = {f"/{m.name}": m.description for m in reg.list_metadata()}

        assert "/clear" in slash_commands
        assert "/quit" in slash_commands
        assert slash_descriptions["/clear"] == "Clear chat output"
        assert slash_descriptions["/quit"] == "Exit the app"


# ---------------------------------------------------------------------------
# Regression: existing dispatch() still works after API additions
# ---------------------------------------------------------------------------


class TestDispatchRegression:
    def test_dispatch_none_for_non_command(self):
        reg = CommandRegistry()
        assert reg.dispatch("just a message") is None

    def test_dispatch_unknown_command_returns_string(self):
        reg = CommandRegistry()
        result = reg.dispatch("/definitely_not_a_real_command_xyz")
        assert result is not None
        assert "Unknown" in result or "not" in result.lower()

    def test_dispatch_help_returns_string(self):
        reg = CommandRegistry()
        result = reg.dispatch("/help")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_dispatch_alias(self):
        reg = CommandRegistry()
        # /? is alias for /help
        result = reg.dispatch("/?")
        assert isinstance(result, str)
        assert len(result) > 0
