"""AppSlashCommandsMixin — all /-command handlers for AgentApp.

Extracted from ``tui/src/ui/app.py`` (lines 115–137, 1916–2754) to reduce
AgentApp to a ≤400-line core.
"""

from __future__ import annotations


from src.core.messaging.event_types import ModelRouting
from pathlib import Path

from textual.containers import Horizontal
from textual.widgets import Button, Label, Static

from .logging import get_logger

from ._app_protocol import AgentAppProtocol

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
  /status        — show agent/provider/model status
  /fast          — switch to fastest/smallest model (NANO tier)
  /provider [n]  — list or switch provider by index/name
  /model [n]     — list or switch model for active provider
  /settings      — open settings screen
  /sessions      — browse saved sessions
  /timeline      — view session message timeline
  /diff          — show working-directory diff since last snapshot
  /fork          — fork current session to a new independent copy
  /share         — export conversation to a markdown file (clipboard if pyperclip available)
  /rename <name> — rename the current session
  /worktree [list|create|remove <id>] — manage git worktree isolation
  /mcp [list|add <name> <cmd…>|status] — manage MCP servers
  /quit          — exit the application"""

def _load_config_loader_module():
    """Return the real src.core.config_loader module, cached in sys.modules."""
    import importlib.util
    import sys as _sys

    _MOD_NAME = "_config_loader_real"
    if _MOD_NAME in _sys.modules:
        return _sys.modules[_MOD_NAME]
    _path = Path(__file__).parents[3] / "src" / "core" / "config_loader.py"
    spec = importlib.util.spec_from_file_location(_MOD_NAME, str(_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load config_loader from {_path}")
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[_MOD_NAME] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        _sys.modules.pop(_MOD_NAME, None)
        raise
    return mod

class AppSlashCommandsMixin(AgentAppProtocol):
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
            w = Static(
                f"[bold]Status[/]\n"
                f"  Running:   {'yes' if self.agent_running else 'no'}\n"
                f"  Task ID:   {st['task_id']}\n"
                f"  Role:      {self.active_role}\n"
                f"  Tokens:    {self.total_tokens:,} / {self.context_window:,}\n"
                f"  History:   {st['history_len']} messages\n"
                f"  WorkDir:   {st['working_dir']}",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)

        elif cmd == "fast":
            await self._slash_fast()

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

        elif cmd == "share":
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
            # Unrecognised: pass as plain text to agent
            if not self.agent_running:
                val = (
                    event.command if not event.args else f"{event.command} {event.args}"
                )
                widget = Label(
                    f"[bold #3b82f6]You:[/] /{val}",
                    classes="user_msg",
                    markup=True,
                )
                await self._mount_chat_widget(widget)
                self._bridge.send_prompt(f"/{val}")
            else:
                self.notify(f"Unknown command: /{cmd}", severity="warning")

    async def _slash_undo_with_confirm(self: AgentAppProtocol) -> None:
        """P2-2: Show a confirmation dialog before removing the last user message."""
        from textual.screen import ModalScreen

        class _UndoConfirmScreen(ModalScreen):
            DEFAULT_CSS = """
            _UndoConfirmScreen { align: center middle; }
            #_undo_dlg {
                background: $surface; border: tall $primary;
                padding: 1 2; width: 56; height: auto;
            }
            #_undo_msg { margin-bottom: 1; }
            #_undo_btns { align: right middle; height: auto; }
            Button { margin-left: 1; }
            """

            def compose(self):
                from textual.containers import Container
                from textual.widgets import Label

                with Container(id="_undo_dlg"):
                    yield Label(
                        "Remove the last user message from history?\n\n"
                        "  This cannot be undone.",
                        id="_undo_msg",
                    )
                    with Horizontal(id="_undo_btns"):
                        yield Button("Undo", id="btn_yes", variant="error")
                        yield Button("Cancel", id="btn_no", variant="default")

            def on_button_pressed(self, event):
                self.dismiss(event.button.id == "btn_yes")

            def on_key(self, event):
                if event.key == "escape":
                    self.dismiss(False)
                    event.prevent_default()

        async def _after_confirm(confirmed):
            if not confirmed:
                return
            removed = self._bridge.undo_last_user_message()
            if removed:
                with self._bridge._history_lock:
                    hist = self._bridge.history
                    while hist and hist[-1][0] != "user":
                        hist.pop()
                self._bridge._save_history()
                w = Static(
                    "[dim]↩ Undone: last user message removed[/]",
                    classes="system_msg",
                    markup=True,
                )
                await self._mount_chat_widget(w)
                self.notify("Last message undone")
            else:
                self.notify("Nothing to undo", severity="warning")

        self.push_screen(_UndoConfirmScreen(), _after_confirm)

    async def _slash_fast(self: AgentAppProtocol) -> None:
        """S8-C: /fast — switch to the smallest/fastest configured model (NANO tier)."""
        try:
            result = self._bridge.get_fast_model()
            nano_model = result.get("model") if result else None
            if not nano_model:
                all_models = self._settings.get_all_models_flat()
                for m in all_models:
                    name_lower = m.get("model", "").lower()
                    if any(kw in name_lower for kw in ("nano", "tiny", "1b", "3b")):
                        nano_model = m["model"]
                        break
            if not nano_model:
                nano_model = self._settings.get("default_model", "")

            if nano_model:
                self._settings.set("default_model", nano_model)
                self._settings.save()
                self._bridge.publish_typed(ModelRouting(provider=self._settings.get("default_provider", ""), selected=nano_model, model_tier="nano"))
                try:
                    self.query_one("#sb_model_info", Static).update(nano_model)
                except Exception:
                    pass
                self.notify(f"Fast mode: {nano_model} (NANO tier)")
            else:
                self.notify(
                    "No fast model configured — add model_routing.nano_model to config",
                    severity="warning",
                )
        except Exception as exc:
            self.notify(f"/fast error: {exc}", severity="warning")

    async def _slash_provider(self: AgentAppProtocol, args: str) -> None:
        from .settings import SettingsStore

        providers = self._settings.available_providers
        if not providers:
            w = Static("No providers configured.", classes="system_msg")
            await self._mount_chat_widget(w)
            return
        if not args:
            lines = [
                f"  {i + 1}. {p.get('name', p)} ({p.get('type', '?')})"
                if isinstance(p, dict)
                else f"  {i + 1}. {p}"
                for i, p in enumerate(providers)
            ]
            w = Static("Providers:\n" + "\n".join(lines), classes="system_msg")
            await self._mount_chat_widget(w)
        else:
            target = None
            if args.isdigit():
                idx = int(args) - 1
                if 0 <= idx < len(providers):
                    target = providers[idx]
            else:
                for p in providers:
                    if SettingsStore._provider_matches(p, args):
                        target = p
                        break
            if target is None:
                self.notify(f"Provider not found: {args}", severity="warning")
                return
            pid = SettingsStore._normalize_provider_id(target)
            role = self.active_role
            self._settings.set(f"{role}_provider", pid)
            self._settings.set("default_provider", pid)
            self._settings.save()
            model_list = target.get("models") or [""]
            first_model = model_list[0] if model_list else ""
            try:
                self.query_one("#sb_provider", Static).update(pid)
                banner = self.query_one("#provider_banner", Static)
                banner.update(f"  {pid}  ·  {first_model}")
                banner.remove_class("connected", "error")
                banner.add_class("connected")
            except Exception:
                pass
            self._bridge.publish_typed(ModelRouting(provider=pid, selected=first_model))
            self.notify(f"Switched to provider: {pid}")

    async def _slash_model(self: AgentAppProtocol, args: str) -> None:
        all_models = self._settings.get_all_models_flat()
        if not args:
            if not all_models:
                current = self._settings.get("default_model", "—")
                w = Static(f"Current model: {current}", classes="system_msg")
            else:
                lines = [
                    f"  {i + 1}. {m['provider_name']}: {m['model']}"
                    for i, m in enumerate(all_models)
                ]
                current = self._settings.get("default_model", "—")
                w = Static(
                    f"Current: {current}\nAll models:\n" + "\n".join(lines),
                    classes="system_msg",
                )
            await self._mount_chat_widget(w)
        else:
            target_model = None
            target_provider = None
            if args.isdigit():
                idx = int(args) - 1
                if 0 <= idx < len(all_models):
                    target_model = all_models[idx]["model"]
                    target_provider = all_models[idx].get("provider_id")
            else:
                q = args.lower()
                for m in all_models:
                    if q == m["model"].lower() or q in m["model"].lower():
                        target_model = m["model"]
                        target_provider = m.get("provider_id")
                        break
            if target_model is None:
                target_model = args
            role = self.active_role
            self._settings.set(f"{role}_model", target_model)
            self._settings.set("default_model", target_model)
            if target_provider:
                self._settings.set(f"{role}_provider", target_provider)
                self._settings.set("default_provider", target_provider)
            self._settings.save()
            try:
                self.query_one("#sb_model_info", Static).update(target_model)
                if target_provider:
                    self.query_one("#sb_provider", Static).update(target_provider)
                    provider_id = target_provider
                    banner = self.query_one("#provider_banner", Static)
                    banner.update(f"  {provider_id}  ·  {target_model}")
            except Exception:
                pass
            self._bridge.publish_typed(ModelRouting(provider=target_provider
                    or self._settings.get("default_provider", ""), selected=target_model))
            self.notify(f"Model switched to: {target_model}")

    async def _slash_mcp(self: AgentAppProtocol, args: str) -> None:
        """S3-C: /mcp [list|add <name> <cmd…>|status] — manage MCP servers."""
        try:
            _cl = _load_config_loader_module()
            get_mcp_servers = _cl.get_mcp_servers
            from pathlib import Path as _Path
            import json as _json

            sub = args.strip().split(None, 1)
            subcmd = sub[0].lower() if sub else "list"
            rest = sub[1] if len(sub) > 1 else ""

            if subcmd in ("list", ""):
                servers = get_mcp_servers()
                if not servers:
                    text = "[dim]No MCP servers configured.[/]\n\nAdd one with: [bold]/mcp add <name> <cmd>[/]"
                else:
                    lines = ["[bold]Configured MCP servers:[/]"]
                    for s in servers:
                        name = s.get("name", "?")
                        cmd = " ".join(s.get("cmd", [])) or "(no cmd)"
                        auto = (
                            "auto" if s.get("auto_register_tools", True) else "manual"
                        )
                        lines.append(f"  • [bold]{name}[/] — {cmd}  [{auto}]")
                    text = "\n".join(lines)
                w = Static(text, classes="system_msg", markup=True)
                await self._mount_chat_widget(w)

            elif subcmd == "add":
                parts = rest.split()
                if len(parts) < 2:
                    self.notify(
                        "Usage: /mcp add <name> <cmd> [args…]", severity="warning"
                    )
                    return
                new_name = parts[0]
                new_cmd = parts[1:]
                import re as _re
                if not _re.match(r"^[a-zA-Z0-9_-]+$", new_name):
                    self.notify(
                        f"Invalid server name '{new_name}': use only letters, digits, - and _",
                        severity="warning",
                    )
                    return
                if _re.search(r"[;&$`\\|<>]", " ".join(new_cmd)):
                    self.notify(
                        "Command contains unsafe shell characters",
                        severity="warning",
                    )
                    return
                agent_dir = (
                    _Path(
                        getattr(
                            getattr(self._bridge, "_orchestrator", None),
                            "working_dir",
                            ".",
                        )
                    )
                    / ".agent"
                )
                agent_dir.mkdir(parents=True, exist_ok=True)
                config_path = agent_dir / "config.json"
                cfg: dict = {}
                if config_path.exists():
                    try:
                        cfg = _json.loads(config_path.read_text(encoding="utf-8"))
                    except Exception:
                        cfg = {}
                mcp_section = cfg.setdefault("mcp", {})
                servers_list: list = mcp_section.setdefault("servers", [])
                servers_list = [s for s in servers_list if s.get("name") != new_name]
                servers_list.append(
                    {"name": new_name, "cmd": new_cmd, "auto_register_tools": True}
                )
                mcp_section["servers"] = servers_list
                cfg["mcp"] = mcp_section
                try:
                    from src.core.io_utils import atomic_write_json

                    logger.debug(
                        "app: attempting atomic_write_json for %s", config_path
                    )
                    ok = atomic_write_json(config_path, cfg, logger=logger)
                    if ok:
                        logger.info("MCP config saved atomically: %s", config_path)
                    else:
                        logger.warning(
                            "app: atomic_write_json returned False for %s; falling back",
                            config_path,
                        )
                        raise RuntimeError("atomic_write_json returned False")
                except Exception:
                    import tempfile as _tempfile
                    import os as _os
                    import shutil as _shutil
                    import traceback as _traceback

                    fd = None
                    tmp = None
                    try:
                        config_path.parent.mkdir(parents=True, exist_ok=True)
                        fd, tmp = _tempfile.mkstemp(
                            dir=str(config_path.parent), suffix=".tmp"
                        )
                        with _os.fdopen(fd, "w", encoding="utf-8") as f:
                            fd = None
                            f.write(_json.dumps(cfg, indent=2))
                            try:
                                f.flush()
                                _os.fsync(f.fileno())
                            except Exception:
                                pass
                        try:
                            _os.replace(tmp, str(config_path))
                        except Exception:
                            try:
                                _shutil.move(tmp, str(config_path))
                            except Exception:
                                logger.debug(
                                    "app: fallback write failed for %s\n%s",
                                    config_path,
                                    _traceback.format_exc(),
                                )
                    finally:
                        try:
                            if fd is not None:
                                _os.close(fd)
                        except Exception:
                            pass
                text = (
                    f"[bold]MCP server added:[/] {new_name}\n"
                    f"  cmd: {' '.join(new_cmd)}\n"
                    f"  Config saved to [dim]{config_path}[/]\n\n"
                    f"Restart the agent session to connect to the new server."
                )
                w = Static(text, classes="system_msg", markup=True)
                await self._mount_chat_widget(w)
                self.notify(f"MCP server '{new_name}' added")

            elif subcmd == "status":
                chip = self.query_one("#mcp_status_chip", Static)
                chip_text = str(chip.render()) if chip else "(none)"
                orch = getattr(self._bridge, "_orchestrator", None)
                clients = getattr(orch, "_mcp_clients", {}) if orch else {}
                if clients:
                    lines = ["[bold]MCP server status:[/]"]
                    for name, client in clients.items():
                        connected = getattr(client, "_connected", False)
                        state = (
                            "[green]connected[/]"
                            if connected
                            else "[red]disconnected[/]"
                        )
                        lines.append(f"  • [bold]{name}[/] — {state}")
                    text = "\n".join(lines)
                else:
                    text = (
                        "[dim]No active MCP connections.[/]\n\nStatus chip: "
                        + chip_text
                    )
                w = Static(text, classes="system_msg", markup=True)
                await self._mount_chat_widget(w)

            else:
                self.notify(
                    f"Unknown /mcp subcommand: {subcmd}. Use list, add, or status.",
                    severity="warning",
                )
        except Exception as exc:
            self.notify(f"/mcp error: {exc}", severity="error")

    async def _slash_diff(self: AgentAppProtocol) -> None:
        """Show working-directory diff since the last git snapshot."""
        try:
            orch = self._bridge._orchestrator
            snap_mgr = getattr(orch, "snapshot_manager", None) if orch else None
            if snap_mgr is None:
                from src.core.orchestration.snapshot_manager import GitSnapshotManager
                from pathlib import Path as _Path

                _workdir = _Path(getattr(orch, "working_dir", ".") if orch else ".")
                snap_mgr = GitSnapshotManager(
                    workspace=_workdir,
                    project_id=_workdir.name or "default",
                )

            import asyncio as _asyncio

            _base_hash: str = "HEAD"
            try:
                _state = getattr(orch, "_last_agent_state", None) if orch else None
                _snaps = (_state or {}).get("snapshots") or []
                if _snaps:
                    _base_hash = _snaps[0]
            except Exception:
                pass

            diff_text: str = ""
            try:
                diff_text = await _asyncio.wait_for(
                    snap_mgr.diff(_base_hash), timeout=5.0
                )
            except Exception:
                diff_text = ""

            if not diff_text or not diff_text.strip():
                diff_text = "(no changes since last snapshot)"

            w = Static(
                f"[bold]Working-directory diff[/]\n{diff_text}",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
        except Exception as exc:
            self.notify(f"diff failed: {exc}", severity="error")

    async def _slash_fork(self: AgentAppProtocol) -> None:
        """Fork the current session to a new independent copy."""
        try:
            orch = self._bridge._orchestrator
            store = getattr(orch, "session_store", None) if orch else None
            current_id: str = getattr(orch, "_current_task_id", "") or ""

            if store is None or not current_id:
                self.notify("No active session to fork", severity="warning")
                return

            fork_id = store.fork_session(current_id)
            w = Static(
                f"[bold]Session forked[/]\n"
                f"  Source:  {current_id}\n"
                f"  Fork ID: {fork_id}\n\n"
                f"The forked session is an independent copy.  Switch to it with "
                f"[bold]/sessions[/] → select the fork.",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            self.notify(f"Session forked: {fork_id[:12]}…")
        except Exception as exc:
            self.notify(f"fork failed: {exc}", severity="error")

    async def _slash_share(self: AgentAppProtocol) -> None:
        """GAP-CMD-2: export conversation to markdown; copy to clipboard if pyperclip available."""
        import datetime as _dt

        history = list(self._bridge.history)
        lines: list[str] = [
            "# Conversation Export",
            f"_Exported: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
            "",
        ]
        for msg in history:
            if isinstance(msg, (list, tuple)) and len(msg) == 2:
                role, content = str(msg[0]), str(msg[1])
            elif isinstance(msg, dict):
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        (p.get("text") or p.get("content") or "")
                        if isinstance(p, dict)
                        else str(p)
                        for p in content
                    )
            else:
                role, content = "unknown", str(msg)
            lines.append(f"**{role.upper()}**\n\n{content}\n\n---\n")

        md_text = "\n".join(lines)

        copied = False
        try:
            import pyperclip

            pyperclip.copy(md_text)
            copied = True
        except Exception:
            pass

        if copied:
            w = Static(
                "[bold #22c55e]✓ Conversation copied to clipboard[/]"
                f"  ({len(history)} messages)",
                classes="system_msg",
                markup=True,
            )
        else:
            from ._core_paths_loader import get_data_dir as _get_data_dir_helper

            export_path = (
                _get_data_dir_helper()
                / f"export_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            )
            try:
                export_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    import tempfile as _tempfile
                    import os as _os
                    import shutil as _shutil
                    import traceback as _traceback

                    fd = None
                    tmp = None
                    try:
                        fd, tmp = _tempfile.mkstemp(
                            dir=str(export_path.parent), suffix=".md.tmp"
                        )
                        with _os.fdopen(fd, "w", encoding="utf-8") as f:
                            fd = None
                            f.write(md_text)
                            try:
                                f.flush()
                                _os.fsync(f.fileno())
                            except Exception as _fsync_err:
                                logger.debug("export fsync failed: %s", _fsync_err)
                        try:
                            _os.replace(tmp, str(export_path))
                        except Exception as _replace_err:
                            logger.debug("export replace failed, trying move: %s", _replace_err)
                            try:
                                _shutil.move(tmp, str(export_path))
                            except Exception:
                                logger.debug(
                                    "app: export fallback write failed for %s\n%s",
                                    export_path,
                                    _traceback.format_exc(),
                                )
                    finally:
                        try:
                            if fd is not None:
                                _os.close(fd)
                        except Exception:
                            pass
                except Exception as exc:
                    w = Static(
                        f"[bold #ff5555]✗ Export failed:[/] {exc}",
                        classes="system_msg",
                        markup=True,
                    )
                    await self._mount_chat_widget(w)
                else:
                    w = Static(
                        f"[bold]✓ Exported[/] {len(history)} messages\n  → {export_path}",
                        classes="system_msg",
                        markup=True,
                    )
                    await self._mount_chat_widget(w)
            except Exception as exc:
                w = Static(
                    f"[bold #ff5555]✗ Export failed:[/] {exc}",
                    classes="system_msg",
                    markup=True,
                )
        await self._mount_chat_widget(w)

    async def _slash_rename(self: AgentAppProtocol, args: str) -> None:
        """GAP-CMD-3: rename the current session."""
        new_name = args.strip()
        if not new_name:
            w = Static(
                "Usage: [bold]/rename <new-name>[/]",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            return

        renamed = False
        try:
            orch = self._bridge._orchestrator
            store = getattr(orch, "session_store", None) if orch else None
            current_id: str = getattr(orch, "_current_task_id", "") or ""
            if store is not None and current_id:
                if hasattr(store, "rename_session"):
                    store.rename_session(current_id, new_name)
                    renamed = True
                elif hasattr(store, "_sessions") and current_id in store._sessions:
                    store._sessions[current_id]["title"] = new_name
                    renamed = True
        except Exception as exc:
            logger.warning(f"/rename: session rename failed: {exc}")

        try:
            self.sub_title = new_name
        except Exception:
            pass
        self._bridge.publish("session.renamed", {"name": new_name})

        msg = (
            f"[bold]Session renamed:[/] {new_name}"
            if renamed
            else f"[bold]Display name set:[/] {new_name} [dim](session store not updated)[/]"
        )
        w = Static(msg, classes="system_msg", markup=True)
        await self._mount_chat_widget(w)

    async def _slash_worktree(self: AgentAppProtocol, args: str) -> None:
        """GAP-WORKTREE-1: manage git worktree isolation for tasks."""
        from pathlib import Path as _Path

        try:
            from src.core.orchestration.git_worktree_manager import GitWorktreeManager
        except ImportError:
            self.notify("GitWorktreeManager not available", severity="error")
            return

        orch = self._bridge._orchestrator
        workspace = _Path(getattr(orch, "working_dir", ".") if orch else ".")
        if not hasattr(self, "_worktree_mgr"):
            self._worktree_mgr = GitWorktreeManager(workspace)

        parts = args.strip().split(None, 1)
        sub = parts[0].lower() if parts else "list"
        sub_args = parts[1] if len(parts) > 1 else ""

        if sub == "list":
            registered = await self._worktree_mgr.list_registered()
            active = self._worktree_mgr.active
            if not registered:
                w = Static("No worktrees registered", classes="system_msg")
            else:
                lines = []
                for entry in registered:
                    path = entry.get("worktree", "?")
                    head = entry.get("HEAD", "?")[:8]
                    branch = entry.get("branch") or (
                        "detached" if entry.get("detached") else "?"
                    )
                    marker = (
                        " [agent]" if path in (str(v) for v in active.values()) else ""
                    )
                    lines.append(f"  {path}  {head}  [{branch}]{marker}")
                w = Static(
                    "[bold]Worktrees:[/]\n" + "\n".join(lines),
                    classes="system_msg",
                    markup=True,
                )
            await self._mount_chat_widget(w)

        elif sub == "create":
            task_id = sub_args.strip() or (
                getattr(orch, "_current_task_id", None) or "manual"
                if orch
                else "manual"
            )
            try:
                wt_path = await self._worktree_mgr.create(task_id)
                w = Static(
                    f"[bold #22c55e]✓ Worktree created[/]\n"
                    f"  task_id: {task_id}\n"
                    f"  path:    {wt_path}",
                    classes="system_msg",
                    markup=True,
                )
            except Exception as exc:
                w = Static(
                    f"[bold #ff5555]✗ Worktree create failed:[/] {exc}",
                    classes="system_msg",
                    markup=True,
                )
            await self._mount_chat_widget(w)

        elif sub == "remove":
            task_id = sub_args.strip()
            if not task_id:
                self.notify("Usage: /worktree remove <task_id>", severity="warning")
                return
            removed = await self._worktree_mgr.remove(task_id)
            msg = (
                f"[bold #22c55e]✓ Worktree removed:[/] {task_id}"
                if removed
                else f"[dim]No worktree found for task_id: {task_id}[/]"
            )
            await self._mount_chat_widget(
                Static(msg, classes="system_msg", markup=True)
            )

        else:
            w = Static(
                "Usage: [bold]/worktree[/] [list | create [<task_id>] | remove <task_id>]",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
