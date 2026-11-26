"""Native Textual 8.1.0 CommandPalette Provider for CodingAgent (TASK-38).

Replaces the 475-LOC custom ``features/palette/`` implementation with a ~120-LOC
``textual.command.Provider`` exposing slash commands, discovered skills, models,
sessions, and built-in Textual themes under unified ``Ctrl+P`` / ``Ctrl+O``.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Callable, List, Tuple

from textual.command import Hit, Hits, Provider

from .events import PaletteCommand, SlashCommand

# (display_label, slash_or_action, help_text)
STATIC_PALETTE_COMMANDS: Tuple[Tuple[str, str, str], ...] = (
    ("Help (/help)", "slash:help", "Show all available slash commands"),
    ("Status (/status)", "slash:status", "Inspect provider, model, policy tier & toolset"),
    ("Doctor (/doctor)", "slash:doctor", "Diagnose local models, context & flat tool schemas"),
    ("Skills (/skills)", "slash:skills", "List discovered workspace, user & bundled skills"),
    ("Theme (/theme)", "slash:theme", "List or switch TUI theme"),
    ("Reasoning Effort (/reasoning)", "slash:reasoning", "Inspect or set reasoning budget (off|low|medium|high)"),
    ("Compact Context (/compact)", "slash:compact", "Compact conversation history deterministically"),
    ("Clear Chat (/clear)", "slash:clear", "Clear visible chat panel (keeps session history)"),
    ("New Session (/new)", "slash:new", "Start a fresh conversation session"),
    ("Undo Last Message (/undo)", "slash:undo", "Remove the last user message from history"),
    ("Continue Task (/continue)", "slash:continue", "Restore and resume previous task"),
    ("Interrupt Agent (/interrupt)", "slash:interrupt", "Cancel currently running agent turn"),
    ("Git Diff (/diff)", "slash:diff", "Show working-directory git diff"),
    ("Git Worktree (/worktree)", "slash:worktree", "List or manage git worktrees"),
    ("Timeline (/timeline)", "slash:timeline", "Open session message timeline"),
    ("MCP Servers (/mcp)", "slash:mcp", "List or manage MCP server connections"),
    ("Fast Model (/fast)", "slash:fast", "Switch to fastest/smallest configured model"),
    ("Fork Session (/fork)", "slash:fork", "Fork current session to an independent copy"),
    ("Export Conversation (/export)", "slash:export", "Export conversation to Markdown"),
    ("Sessions Browser", "action_open_sessions", "Browse and restore saved sessions (Ctrl+R)"),
    ("Settings", "action_open_settings", "Open provider and TUI settings (Ctrl+S)"),
    ("Toggle Sidebar", "action_toggle_sidebar", "Show or hide right status sidebar (Ctrl+B)"),
    ("Toggle Console", "action_toggle_console", "Show or hide debug console panel (Ctrl+L)"),
    ("External Editor", "action_open_editor", "Show configured $EDITOR / $VISUAL"),
    ("Quit (/quit)", "slash:quit", "Exit CodingAgent (Ctrl+Q)"),
)


class CodingAgentCommands(Provider):
    """Native Textual 8.1.0 command provider for slash commands, skills, models, and themes."""

    def _collect_entries(self) -> List[Tuple[str, Callable[[], None], str]]:
        app: Any = self.app
        entries: List[Tuple[str, Callable[[], None], str]] = []

        def _dispatch(action: str) -> None:
            app.post_message(PaletteCommand(action))

        for label, action, help_text in STATIC_PALETTE_COMMANDS:
            entries.append((label, partial(_dispatch, action), help_text))

        # Themes
        for theme_name in sorted(getattr(app, "available_themes", {}).keys()):
            entries.append(
                (
                    f"Theme: {theme_name}",
                    partial(app.post_message, SlashCommand("theme", theme_name)),
                    f"Switch UI theme to {theme_name}",
                )
            )

        # Discovered skills
        try:
            from src.core.context.agent_brain_loading import discover_skills

            bridge = getattr(app, "_bridge", None)
            wdir = getattr(bridge, "working_dir", None) if bridge is not None else None
            for meta in discover_skills(wdir).values():
                entries.append(
                    (
                        f"Skill: /{meta.name}",
                        partial(app.post_message, SlashCommand("skills", meta.name)),
                        f"[{meta.source_scope}] {meta.description}",
                    )
                )
        except Exception:
            pass

        # Configured models from SettingsStore
        try:
            settings = getattr(app, "_settings", None)
            if settings is not None and hasattr(settings, "get_all_models_flat"):
                for item in settings.get_all_models_flat():
                    model_id = str(item.get("model") or "")
                    prov_name = str(item.get("provider_name") or item.get("provider_id") or "")
                    if model_id:
                        entries.append(
                            (
                                f"Model: {model_id} ({prov_name})",
                                partial(app.post_message, SlashCommand("model", model_id)),
                                f"Switch active model to {model_id}",
                            )
                        )
        except Exception:
            pass

        return entries

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        q_clean = (query or "").strip().lower()
        for label, callback, help_text in self._collect_entries():
            score = matcher.match(label)
            if score == 0 and q_clean:
                if q_clean in help_text.lower() or q_clean in label.lower():
                    score = 0.5
            if score > 0:
                yield Hit(score, matcher.highlight(label), callback, help=help_text)
