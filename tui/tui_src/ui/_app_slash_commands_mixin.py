"""AppSlashCommandsMixin — all /-command handlers for AgentApp.

Extracted from ``tui/src/ui/app.py`` (lines 115–137, 1916–2754) to reduce
AgentApp to a ≤400-line core.
"""

from __future__ import annotations

from rich.markup import escape
from textual import on
from textual.widgets import Label, Static

from .events import SlashCommand
from .logging import get_logger

from ._app_protocol import AgentAppProtocol
from ._slash_model_commands_mixin import _SlashModelCommandsMixin
from ._slash_session_commands_mixin import (
    _SlashSessionCommandsMixin,
    _load_config_loader_module,  # noqa: F401
)

logger = get_logger("app_slash")

SLASH_HELP = """\
Available commands:
  /help          — show this list
  /clear         — clear chat output (keeps history)
  /new  /reset   — new session (clears history)
  /compact       — compact conversation context
  /undo          — undo last user message (removes it from history)
  /continue      — restore & re-run previous task
  /interrupt     — cancel running agent
  /status        — show agent/provider/model status & runtime policy
  /doctor        — diagnose running local models & flat tool schemas
  /skills [name] — inspect packaged skills (polyglot-verify, git-safe-review…)
  /theme [name]  — list or switch built-in Textual TUI theme
  /advisor [model <m>|handoff on|off|status|reset] — configure opt-in advisor (max 3/session)
  /btw <question> — parallel read-only side question (never mutates main history)
  /queue [ls|pop|clear|<prompt>] — manage queued follow-up prompts
  /fast          — switch to fastest/smallest model (NANO tier)
  /provider [n]  — list or switch provider by index/name
  /model [n]     — list or switch model for active provider
  /settings      — open settings screen
  /sessions      — browse saved sessions
  /timeline      — view session message timeline
  /diff          — show working-directory diff since last snapshot
  /fork          — fork current session to a new independent copy
  /share  /export — export conversation to a markdown file (clipboard if pyperclip available)
  /rename <name> — rename the current session
  /worktree [list|create|remove <id>] — manage git worktree isolation
  /mcp [list|add <name> <cmd…>|status] — manage MCP servers
  /quit          — exit the application"""


class AppSlashCommandsMixin(_SlashModelCommandsMixin, _SlashSessionCommandsMixin):
    """Slash-command dispatcher and individual command implementations.

    Expects the host class to expose:
    - ``self._bridge`` (AgentBridge)
    - ``self._settings`` (SettingsStore)
    - ``self.agent_running``, ``self.active_role``, ``self.total_tokens``,
      ``self.context_window`` (reactives)
    - ``self._last_task_text`` (str)
    - ``self._continue_state`` (dict | None)
    - ``self._worktree_mgr`` (GitWorktreeManager, optional)
    - ``self._clear_chat_panel``, ``self._reset_sidebar``, ``self._save_session_snapshot``
    - ``self._mount_chat_widget``, ``self._finalize_stream``
    - ``self.query_one``, ``self.notify``, ``self.push_screen``
    - ``self.action_quit_app``, ``self.action_open_settings``, ``self.action_open_sessions``
    """

    # ── Slash command dispatcher (§11) ────────────────────────────────────

    @on(SlashCommand)
    async def handle_slash_command(self: AgentAppProtocol, event) -> None:
        cmd = event.command.lower()
        args = event.args.strip()
        logger.info(f"Slash command: /{cmd} {args}")

        if cmd == "help":
            w = Static(SLASH_HELP, classes="system_msg help_msg", markup=False)
            await self._mount_chat_widget(w)

        elif cmd == "clear":
            self._clear_chat_panel()
            self.notify("Chat cleared")

        elif cmd in ("new", "reset"):
            self._save_session_snapshot()
            self._bridge.clear_history()
            self._clear_chat_panel()
            self._reset_sidebar()
            self._session_custom_title = None
            try:
                import uuid as _uuid

                self._session_id = str(_uuid.uuid4())[:8]
                self.sub_title = ""
            except Exception:
                pass
            self._bridge.start_new_session()
            self.notify("New session started")

        elif cmd == "compact":
            self.notify("Compacting context…", severity="information")
            compacted = self._bridge.compact_context()
            if compacted:
                msg = "Context compacted — context window freed."
                divider = Static(
                    "[dim]══════════════ Compaction ══════════════[/]",
                    classes="system_msg compaction_divider",
                    markup=True,
                )
                await self._mount_chat_widget(divider)
                w = Static(f"[dim]{msg}[/]", classes="system_msg", markup=True)
            else:
                msg = "Context compaction failed or nothing to compact — see logs for details."
                self.notify(msg, severity="warning")
                w = Static(f"[bold yellow]{msg}[/]", classes="system_msg", markup=True)
            await self._mount_chat_widget(w)

        elif cmd == "continue":
            if not self._last_task_text:
                self.notify("No previous task to continue", severity="warning")
            else:
                self._bridge.restore_and_continue(
                    self._last_task_text, self._continue_state
                )

        elif cmd == "undo":
            if self.agent_running:
                self.notify(
                    "Cannot undo while agent is running — use /interrupt first",
                    severity="warning",
                )
            else:
                await self._slash_undo_with_confirm()

        elif cmd == "revert":
            from .screens.session_screen import SessionScreen

            self.push_screen(SessionScreen())

        elif cmd == "interrupt":
            self._bridge.force_interrupt()
            self.notify("Interrupt signal sent")

        elif cmd == "status":
            st = self._bridge.get_status()
            pol_info = ""
            p = st.get("policy")
            if isinstance(p, dict) and p:
                pol_info = (
                    f"  Policy Tier: [bold cyan]{str(p.get('policy_tier', '—')).upper()}[/]\n"
                    f"  Flat Toolset:{p.get('toolset_name', '—')} ({p.get('tool_limit', '—')} tools)\n"
                    f"  Verify Mode: {p.get('verification_mode', '—')} (SHA-256 deduplicated)\n"
                    f"  Parallel:    {p.get('max_parallel_reads', 1)} read waves\n"
                    f"  Turn Limit:  {p.get('turn_limit', '—')} turns\n"
                    f"  Output Cap:  {p.get('tool_output_max_chars', 24_000):,} chars\n"
                )

            prov_str = ""
            if st.get("provider") or st.get("model"):
                prov_str = (
                    f"  Provider:    [bold]{st.get('provider') or '—'}[/]\n"
                    f"  Model:       [cyan]{st.get('model') or '—'}[/]\n"
                )

            w = Static(
                f"[bold]CodingAgent Status[/]\n"
                f"  Running:     {'yes' if self.agent_running else 'no'}\n"
                f"  Task ID:     {st['task_id']}\n"
                f"{prov_str}"
                f"{pol_info}"
                f"  Role:        {self.active_role}\n"
                f"  Tokens:      {self.total_tokens:,} / {self.context_window:,}\n"
                f"  History:     {st['history_len']} messages\n"
                f"  WorkDir:     {st['working_dir']}",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)

        elif cmd == "doctor":
            await self._slash_doctor()

        elif cmd in ("skills", "skill"):
            await self._slash_skills(args)

        elif cmd == "theme":
            await self._slash_theme(args)

        elif cmd == "advisor":
            await self._slash_advisor(args)

        elif cmd == "btw":
            await self._slash_btw(args)

        elif cmd == "queue":
            await self._slash_queue(args)

        elif cmd == "fast":
            await self._slash_fast()

        elif cmd in ("reasoning", "effort", "thinking"):
            await self._slash_reasoning(args)

        elif cmd == "provider":
            await self._slash_provider(args)

        elif cmd == "model":
            await self._slash_model(args)

        elif cmd == "settings":
            self.action_open_settings()

        elif cmd == "sessions":
            self.action_open_sessions()

        elif cmd == "timeline":
            from .screens.timeline import TimelineScreen

            self.push_screen(TimelineScreen(self._bridge.history))

        elif cmd == "diff":
            await self._slash_diff()

        elif cmd == "fork":
            await self._slash_fork()

        elif cmd in ("share", "export"):
            await self._slash_share()

        elif cmd == "rename":
            await self._slash_rename(args)

        elif cmd == "worktree":
            await self._slash_worktree(args)

        elif cmd == "mcp":
            await self._slash_mcp(args)

        elif cmd == "quit":
            self.action_quit_app()

        else:
            # TASK-32: Check if /<cmd> matches a discovered skill before plain-text fallback
            _skill_meta = None
            try:
                from src.core.context.agent_brain_loading import (
                    discover_skills,
                    find_skill_meta,
                    read_skill_content,
                )

                _wd = getattr(self._bridge, "working_dir", None)
                _skill_meta = find_skill_meta(cmd, discover_skills(_wd))
            except Exception:
                _skill_meta = None

            if _skill_meta is not None:
                if self.agent_running:
                    self.notify(
                        f"Cannot invoke skill /{_skill_meta.name} while agent is running",
                        severity="warning",
                    )
                else:
                    try:
                        skill_body = read_skill_content(_skill_meta)
                    except Exception as exc:
                        self.notify(f"Failed to load skill '{_skill_meta.name}': {exc}", severity="error")
                        return
                    notice = Static(
                        f"[bold cyan]Skill loaded:[/] {escape(_skill_meta.name)} "
                        f"[dim]({escape(_skill_meta.source_scope)})[/]",
                        classes="system_msg",
                        markup=True,
                    )
                    await self._mount_chat_widget(notice)
                    if args:
                        user_w = Label(
                            f"[bold #3b82f6]You:[/] /{escape(_skill_meta.name)} {escape(args)}",
                            classes="user_msg",
                            markup=True,
                        )
                        await self._mount_chat_widget(user_w)
                        prompt_text = (
                            f"Apply the `{_skill_meta.name}` skill below to this request:\n\n"
                            f"{skill_body}\n\nUser request:\n{args}"
                        )
                        self._bridge.send_prompt(prompt_text)
                    else:
                        await self._slash_skills(_skill_meta.name)
            elif not self.agent_running:
                val = (
                    event.command if not event.args else f"{event.command} {event.args}"
                )
                widget = Label(
                    f"[bold #3b82f6]You:[/] /{escape(val)}",
                    classes="user_msg",
                    markup=True,
                )
                await self._mount_chat_widget(widget)
                self._bridge.send_prompt(f"/{val}")
            else:
                self.notify(f"Unknown command: /{cmd}", severity="warning")
