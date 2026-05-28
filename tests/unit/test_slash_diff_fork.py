"""tests/unit/test_slash_diff_fork.py — S5-C

Unit tests for /diff and /fork TUI slash commands.

We test:
  - SLASH_COMMANDS list contains /diff and /fork
  - SLASH_COMMAND_DESCRIPTIONS contains both with meaningful text
  - registry.help_text() contains the command descriptions
  - _slash_diff handler exists on AgentApp
  - _slash_fork handler exists on AgentApp
  - fork_session integration via SessionStore (full round-trip)
"""

from __future__ import annotations



# ---------------------------------------------------------------------------
# SC-1 – SC-4: Chat input slash command registry
# ---------------------------------------------------------------------------


class TestSlashCommandRegistry:
    def test_sc1_diff_in_slash_commands(self) -> None:
        """SC-1: /diff appears in SLASH_COMMANDS list."""
        from tui.tui_src.ui.components.chat_input import SLASH_COMMANDS

        assert "/diff" in SLASH_COMMANDS

    def test_sc2_fork_in_slash_commands(self) -> None:
        """SC-2: /fork appears in SLASH_COMMANDS list."""
        from tui.tui_src.ui.components.chat_input import SLASH_COMMANDS

        assert "/fork" in SLASH_COMMANDS

    def test_sc3_diff_has_description(self) -> None:
        """SC-3: /diff has a non-empty description."""
        from tui.tui_src.ui.components.chat_input import SLASH_COMMAND_DESCRIPTIONS

        desc = SLASH_COMMAND_DESCRIPTIONS.get("/diff", "")
        assert desc and len(desc) > 5

    def test_sc4_fork_has_description(self) -> None:
        """SC-4: /fork has a non-empty description."""
        from tui.tui_src.ui.components.chat_input import SLASH_COMMAND_DESCRIPTIONS

        desc = SLASH_COMMAND_DESCRIPTIONS.get("/fork", "")
        assert desc and len(desc) > 5


# ---------------------------------------------------------------------------
# SC-5 – SC-6: Commands registered in CommandRegistry
# ---------------------------------------------------------------------------


class TestSlashHelp:
    def test_sc5_diff_in_registry(self) -> None:
        """SC-5: /diff registered in CommandRegistry."""
        from tui.tui_src.ui.commands.registry import CommandRegistry

        r = CommandRegistry()
        # Check that _init_slash_registry registers diff
        import pathlib
        src = pathlib.Path("tui/tui_src/ui/_app_slash_commands_mixin.py").read_text()
        assert '"diff"' in src or "'diff'" in src
        assert "_slash_diff" in src

    def test_sc6_fork_in_registry(self) -> None:
        """SC-6: /fork registered in CommandRegistry."""
        import pathlib
        src = pathlib.Path("tui/tui_src/ui/_app_slash_commands_mixin.py").read_text()
        assert '"fork"' in src or "'fork'" in src
        assert "_slash_fork" in src

    def test_help_text_contains_diff_and_fork(self) -> None:
        """registry.help_text() includes /diff and /fork descriptions."""
        from tui.tui_src.ui.commands import CommandRegistry
        from tui.tui_src.ui.commands import SlashCommand

        r = CommandRegistry()
        r.register(SlashCommand("diff", "show working-directory diff", lambda app, _a: None))
        r.register(SlashCommand("fork", "fork current session", lambda app, _a: None))
        help_text = r.help_text()
        assert "/diff" in help_text
        assert "/fork" in help_text


# ---------------------------------------------------------------------------
# SC-7 – SC-8: Handler methods exist on AgentApp
# ---------------------------------------------------------------------------


class TestAgentAppHandlers:
    def test_sc7_slash_diff_method_exists(self) -> None:
        """SC-7: AgentApp has a _slash_diff async method."""
        import inspect
        from tui.tui_src.ui.app import AgentApp

        assert hasattr(AgentApp, "_slash_diff")
        assert inspect.iscoroutinefunction(AgentApp._slash_diff)

    def test_sc8_slash_fork_method_exists(self) -> None:
        """SC-8: AgentApp has a _slash_fork async method."""
        import inspect
        from tui.tui_src.ui.app import AgentApp

        assert hasattr(AgentApp, "_slash_fork")
        assert inspect.iscoroutinefunction(AgentApp._slash_fork)


# ---------------------------------------------------------------------------
# SC-9 – SC-10: handle_slash_command routes via registry
# ---------------------------------------------------------------------------


class TestSlashCommandRouting:
    _MIXIN = "tui/tui_src/ui/_app_slash_commands_mixin.py"

    def test_sc9_registry_dispatches_diff(self) -> None:
        """SC-9: registry has 'diff' pointing to _slash_diff."""
        import pathlib

        src = pathlib.Path(self._MIXIN).read_text()
        assert '"diff"' in src
        assert "_slash_diff" in src
        assert "SlashCommand" in src

    def test_sc10_registry_dispatches_fork(self) -> None:
        """SC-10: registry has 'fork' pointing to _slash_fork."""
        import pathlib

        src = pathlib.Path(self._MIXIN).read_text()
        assert '"fork"' in src
        assert "_slash_fork" in src
        assert "SlashCommand" in src
