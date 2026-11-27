"""Session, undo, MCP, diff, fork, share, rename, and worktree slash commands.

Extracted from ``tui/tui_src/ui/_app_slash_commands_mixin.py`` (TASK-43).
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.containers import Horizontal
from textual.widgets import Button, Static

from ._app_protocol import AgentAppProtocol
from .logging import get_logger

logger = get_logger("app_slash")


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


class _SlashSessionCommandsMixin(AgentAppProtocol):
    """Slash commands for session management, git diff/worktree, and MCP servers."""

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
                from src.tools.tools_config import agent_context_path

                _wdir = (
                    getattr(
                        getattr(self._bridge, "_orchestrator", None),
                        "working_dir",
                        None,
                    )
                    or getattr(self._bridge, "working_dir", None)
                    or "."
                )
                agent_dir = agent_context_path(_Path(_wdir))
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
        """Show working-directory diff via RollbackManager snapshots or git diff."""
        try:
            import asyncio as _asyncio
            from pathlib import Path as _Path

            orch = self._bridge._orchestrator
            workdir = _Path(getattr(orch, "working_dir", ".") if orch else ".")

            def _compute_diff() -> str:
                from src.tools.git_tools import git_diff

                res = git_diff(workdir=str(workdir))
                if isinstance(res, dict) and res.get("status") == "ok":
                    txt = str(res.get("diff") or res.get("output") or "").strip()
                    if txt:
                        return txt
                # Fallback: summarize modified files recorded by RollbackManager
                rb = getattr(orch, "rollback_manager", None) if orch else None
                if rb is not None and hasattr(rb, "list_snapshots"):
                    snaps = rb.list_snapshots()
                    if snaps:
                        latest = snaps[-1]
                        files = latest.get("files") if isinstance(latest, dict) else []
                        if files:
                            return "Tracked rollback snapshot files:\n" + "\n".join(
                                f"  - {f}" for f in files
                            )
                return ""

            diff_text = await _asyncio.wait_for(
                _asyncio.to_thread(_compute_diff), timeout=5.0
            )
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
            current_id: str = (
                getattr(orch, "_current_task_id", None)
                or getattr(self, "_session_id", "")
                or ""
            )

            if store is None or not current_id:
                self.notify("No active session to fork", severity="warning")
                return

            fork_id = store.fork_session(current_id)
            w = Static(
                f"[bold]Session forked[/]\n"
                f"  Source:  {escape(str(current_id))}\n"
                f"  Fork ID: {escape(str(fork_id))}\n\n"
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
                    from rich.markup import escape as _escape

                    w = Static(
                        f"[bold #ff5555]✗ Export failed:[/] {_escape(str(exc))}",
                        classes="system_msg",
                        markup=True,
                    )
                else:
                    from rich.markup import escape as _escape

                    w = Static(
                        f"[bold]✓ Exported[/] {len(history)} messages\n  → {_escape(str(export_path))}",
                        classes="system_msg",
                        markup=True,
                    )
            except Exception as exc:
                from rich.markup import escape as _escape

                w = Static(
                    f"[bold #ff5555]✗ Export failed:[/] {_escape(str(exc))}",
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
        self._session_custom_title = new_name
        try:
            orch = self._bridge._orchestrator
            store = getattr(orch, "session_store", None) if orch else None
            current_id: str = (
                getattr(orch, "_current_task_id", None)
                or getattr(self, "_session_id", "")
                or ""
            )
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
            self._save_session_snapshot()
            renamed = True
        except Exception as exc:
            logger.debug(f"/rename: session snapshot update: {exc}")

        try:
            self.sub_title = new_name
        except Exception:
            pass
        self._bridge.publish("session.renamed", {"name": new_name})

        msg = (
            f"[bold]Session renamed:[/] {escape(new_name)}"
            if renamed
            else f"[bold]Display name set:[/] {escape(new_name)}"
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
                # Save parent working dir for restoration on removal
                if not hasattr(self, "_parent_working_dir") or self._parent_working_dir is None:
                    self._parent_working_dir = getattr(self._bridge, "working_dir", None) or workspace

                wt_path = await self._worktree_mgr.create(task_id)

                # Switch active bridge and orchestrator working directories
                self._bridge.working_dir = _Path(wt_path)
                if orch is not None:
                    orch.working_dir = str(wt_path)
                    try:
                        if hasattr(orch, "_publish_git_status"):
                            orch._publish_git_status()
                    except Exception:
                        pass

                try:
                    self.query_one("#sb_workdir", Static).update(str(wt_path))
                except Exception:
                    pass

                w = Static(
                    f"[bold #22c55e]✓ Worktree created & switched[/]\n"
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

            wt_path_to_remove = self._worktree_mgr.active.get(task_id)
            removed = await self._worktree_mgr.remove(task_id)
            if removed:
                parent_dir = getattr(self, "_parent_working_dir", None) or workspace
                is_current = False
                if wt_path_to_remove and (
                    str(getattr(self._bridge, "working_dir", "")) == str(wt_path_to_remove)
                    or (orch and str(getattr(orch, "working_dir", "")) == str(wt_path_to_remove))
                ):
                    is_current = True

                if is_current:
                    self._bridge.working_dir = _Path(parent_dir)
                    if orch is not None:
                        orch.working_dir = str(parent_dir)
                        try:
                            if hasattr(orch, "_publish_git_status"):
                                orch._publish_git_status()
                        except Exception:
                            pass
                    try:
                        self.query_one("#sb_workdir", Static).update(str(parent_dir))
                    except Exception:
                        pass

                msg = (
                    f"[bold #22c55e]✓ Worktree removed:[/] {task_id}"
                    + (f" (switched back to {parent_dir})" if is_current else "")
                )
            else:
                msg = f"[dim]No worktree found for task_id: {task_id}[/]"

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
