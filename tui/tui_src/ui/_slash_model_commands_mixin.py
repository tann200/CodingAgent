"""Model, provider, skills, theme, reasoning, advisor, and queue slash commands.

Extracted from ``tui/tui_src/ui/_app_slash_commands_mixin.py`` (TASK-43).
"""

from __future__ import annotations

from pathlib import Path

from rich.markup import escape
from textual.widgets import Static

from src.core.messaging.event_types import ModelRouting

from ._app_protocol import AgentAppProtocol
from .logging import get_logger

logger = get_logger("app_slash")


class _SlashModelCommandsMixin(AgentAppProtocol):
    """Slash commands for model/provider switching, diagnostics, skills, and side tools."""

    async def _slash_doctor(self: AgentAppProtocol) -> None:
        """Run system & model capability diagnosis and mount report in chat."""
        import asyncio
        from types import SimpleNamespace
        from src.cli.doctor_cmd import build_doctor_diagnosis, format_doctor_tui_markup

        self.notify("Running doctor diagnostics…", severity="information")
        wdir = getattr(self._bridge, "working_dir", None)
        args = SimpleNamespace(
            workdir=wdir,
            provider=None,
            model=None,
            endpoint=None,
            validate_tools=False,
            json=False,
        )

        loop = asyncio.get_running_loop()
        try:
            diag = await loop.run_in_executor(None, build_doctor_diagnosis, args)
            markup_text = format_doctor_tui_markup(diag)
            w = Static(markup_text, classes="system_msg doctor_report", markup=True)
            await self._mount_chat_widget(w)
        except Exception as exc:
            from rich.markup import escape as _escape

            logger.exception("Failed to run doctor in TUI: %s", exc)
            err_w = Static(f"[bold red]Doctor audit failed:[/] {_escape(str(exc))}", classes="system_msg", markup=True)
            await self._mount_chat_widget(err_w)

    async def _slash_skills(self: AgentAppProtocol, args: str) -> None:
        """Inspect and display discovered skills (workspace, user, bundled) in the chat panel."""
        from src.cli.skill_cmd import get_skill_metadata, list_all_skills

        wdir = getattr(self._bridge, "working_dir", None)
        skill_arg = args.strip()
        if not skill_arg:
            skills = list_all_skills(workdir=wdir)
            lines = [
                f"[bold cyan]Available Agent Skills[/] ({len(skills)} discovered)",
                "[dim]Scopes: .codingagent > .agents > .claude > ~/.codingagent > bundled[/]",
                "",
            ]
            for s in skills:
                name = escape(str(s.get("name", "unknown")))
                desc_raw = str(s.get("description", "")).split("\n")[0]
                desc = escape(desc_raw)
                scope = escape(str(s.get("source_scope", "bundled")))
                lines.append(f"  • [bold]{name}[/] [dim]({scope})[/]")
                if desc:
                    lines.append(f"    {desc}")
            lines.append("")
            lines.append("[dim]Use '/skills <name>' to inspect, or '/<skill-name> <request>' to invoke directly.[/]")
            w = Static("\n".join(lines), classes="system_msg skills_list", markup=True)
            await self._mount_chat_widget(w)
        else:
            meta = get_skill_metadata(skill_arg, workdir=wdir)
            if not meta:
                self.notify(f"Skill '{skill_arg}' not found", severity="warning")
                w = Static(
                    f"[bold yellow]Skill '{escape(skill_arg)}' not found.[/] Type [bold]/skills[/] to list all packaged skills.",
                    classes="system_msg",
                    markup=True,
                )
                await self._mount_chat_widget(w)
                return

            doc_path = meta.get("path")
            content = ""
            if doc_path and Path(doc_path).exists():
                try:
                    content = Path(doc_path).read_text(encoding="utf-8")
                except Exception:
                    pass

            name_esc = escape(str(meta.get("name", skill_arg)))
            tier_esc = escape(str(meta.get("tier_recommendation", "compact")))
            ver_esc = escape(str(meta.get("version", "1.0.0")))
            desc_esc = escape(str(meta.get("description", "—")))

            lines = [
                f"[bold cyan]Skill: {name_esc}[/] (v{ver_esc})",
                f"Tier Target: [bold]{tier_esc}[/]",
                f"Description: {desc_esc}",
                "",
            ]
            if content:
                max_preview = 2500
                if len(content) > max_preview:
                    preview = content[:max_preview] + f"\n... [truncated: {len(content) - max_preview} chars omitted]"
                else:
                    preview = content
                file_name = Path(doc_path).name if doc_path else "SKILL.md"
                lines.append(f"[dim]── Instructions ({escape(file_name)}) ──[/]\n{escape(preview)}")

            w = Static("\n".join(lines), classes="system_msg skill_detail", markup=True)
            await self._mount_chat_widget(w)

    async def _slash_theme(self: AgentAppProtocol, args: str) -> None:
        """TASK-38: /theme [name] — list or switch built-in Textual themes, persisted in SettingsStore."""
        available = sorted(getattr(self, "available_themes", {}).keys())
        arg = (args or "").strip()
        if not arg:
            current = str(getattr(self, "theme", "textual-dark"))
            listing = ", ".join(available) if available else "textual-dark, textual-light"
            w = Static(
                f"[bold]Active theme:[/] {escape(current)}\n"
                f"[dim]Available ({len(available)}):[/] {escape(listing)}\n"
                f"[dim]Usage: /theme <name> (e.g. /theme textual-light, /theme nord)[/]",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            return

        if available and arg not in available:
            w = Static(
                f"[red]Unknown theme:[/] {escape(arg)}\n"
                f"[dim]Available:[/] {escape(', '.join(available))}",
                classes="error_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            return

        try:
            self._theme_user_selected = True  # type: ignore[attr-defined]
            self.theme = arg  # type: ignore[misc]
            if getattr(self, "_settings", None) is not None and hasattr(self._settings, "update"):
                self._settings.update({"theme": arg})
            w = Static(f"[green]✓ Theme switched to[/] [bold]{escape(arg)}[/]", classes="system_msg", markup=True)
            await self._mount_chat_widget(w)
        except Exception as exc:
            w = Static(f"[red]Failed to switch theme:[/] {escape(str(exc))}", classes="error_msg", markup=True)
            await self._mount_chat_widget(w)

    async def _slash_reasoning(self: AgentAppProtocol, args: str) -> None:
        """TASK-29: /reasoning [off|low|medium|high|unset|reset|status] (aliases: /effort, /thinking)."""
        from src.core.inference.thinking_utils import (
            get_active_reasoning_effort,
            reasoning_budget_tokens,
            set_active_reasoning_effort,
        )

        arg = (args or "").strip().lower()
        if not arg or arg == "status":
            cur = get_active_reasoning_effort()
            label = cur.value if cur is not None else "unset (tier default)"
            budget = reasoning_budget_tokens(cur) if cur is not None else None
            b_str = f" ({budget:,} tok budget)" if budget is not None else ""
            w = Static(
                f"[bold]Reasoning effort:[/] {escape(label)}{b_str}\n"
                f"  Usage: /reasoning [off|low|medium|high|unset|status]",
                classes="system_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            return

        try:
            resolved = set_active_reasoning_effort(arg)
        except ValueError:
            w = Static(
                f"[red]Invalid reasoning level:[/] {escape(arg)} "
                f"(expected: off | low | medium | high | unset | reset | status)",
                classes="error_msg",
                markup=True,
            )
            await self._mount_chat_widget(w)
            return

        budget = reasoning_budget_tokens(resolved)
        b_str = f" ({budget:,} tok budget)" if budget is not None else ""
        w = Static(
            f"[green]✓ Reasoning effort set to[/] [bold]{escape(resolved.value)}[/]{b_str}",
            classes="system_msg",
            markup=True,
        )
        await self._mount_chat_widget(w)

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
                self._bridge.publish_typed(
                    ModelRouting(
                        provider=self._settings.get("default_provider", ""),
                        selected=nano_model,
                        available_models=[nano_model],
                    )
                )
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
            self._bridge.publish_typed(
                ModelRouting(
                    provider=pid,
                    selected=first_model,
                    available_models=[str(m) for m in model_list if m],
                )
            )
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
            self._bridge.publish_typed(
                ModelRouting(
                    provider=target_provider or self._settings.get("default_provider", ""),
                    selected=target_model,
                    available_models=[m["model"] for m in all_models if m.get("model")] or [target_model],
                )
            )
            self.notify(f"Model switched to: {target_model}")

    def _get_advisor_state(self: AgentAppProtocol):
        """Return or lazily initialize the session AdvisorSessionState (TASK-36)."""
        state = getattr(self, "_advisor_state", None)
        if state is None:
            from src.tools.advisor_tool import AdvisorSessionState

            state = AdvisorSessionState()
            setattr(self, "_advisor_state", state)
            orch = getattr(getattr(self, "_bridge", None), "_orchestrator", None)
            if orch is not None:
                try:
                    setattr(orch, "_advisor_state", state)
                except Exception:
                    pass
        return state

    async def _slash_advisor(self: AgentAppProtocol, args: str) -> None:
        """Inspect or configure the opt-in budgeted advisor tool (/advisor)."""
        state = self._get_advisor_state()
        tokens = args.strip().split()
        sub = tokens[0].lower() if tokens else "status"

        if sub == "model":
            model_name = " ".join(tokens[1:]).strip()
            if not model_name:
                await self._mount_chat_widget(
                    Static("Usage: /advisor model <model_name> | /advisor model off", classes="system_msg", markup=False)
                )
                return
            state.config.advisor_model = None if model_name.lower() in ("off", "none", "clear") else model_name
        elif sub == "handoff":
            flag = tokens[1].lower() if len(tokens) > 1 else "on"
            state.config.handoff_enabled = flag in ("on", "true", "1", "yes")
        elif sub == "reset":
            state.reset_budget()
        elif sub != "status":
            await self._mount_chat_widget(
                Static(
                    "Usage: /advisor [status | model <name|off> | handoff on|off | reset]",
                    classes="system_msg",
                    markup=False,
                )
            )
            return

        st = state.status_summary()
        summary = (
            f"Advisor: {'ENABLED' if st['enabled'] else 'DISABLED (opt-in)'}\n"
            f"  Model:    {st['advisor_model'] or '—'}\n"
            f"  Handoff:  {'on' if st['handoff_enabled'] else 'off'}\n"
            f"  Budget:   {st['uses']} / {st['max_uses']} used ({st['remaining']} remaining)"
        )
        await self._mount_chat_widget(Static(summary, classes="system_msg", markup=False))

    async def _slash_btw(self: AgentAppProtocol, args: str) -> None:
        """Run a parallel read-only side question without mutating main conversation history (/btw)."""
        import asyncio as _asyncio

        question = args.strip()
        if not question:
            await self._mount_chat_widget(
                Static("Usage: /btw <question>  (read-only side query; does not mutate history)", classes="system_msg", markup=False)
            )
            return

        from src.core.orchestration.btw_query import run_btw_query

        bridge = getattr(self, "_bridge", None)
        orch = getattr(bridge, "_orchestrator", None) if bridge is not None else None
        msg_mgr = getattr(orch, "msg_mgr", None) if orch is not None else None
        history_snap = list(getattr(msg_mgr, "messages", None) or getattr(bridge, "history", []) or [])
        call_fn = getattr(self, "_btw_call_fn", None)

        result = await _asyncio.to_thread(
            run_btw_query,
            question,
            history_snapshot=history_snap,
            call_fn=call_fn,
        )
        if result.get("status") == "ok":
            text = f"[BTW — read-only, not added to history]\nQ: {question}\n{result.get('answer', '')}"
        else:
            text = f"[BTW error] {result.get('error', 'unknown error')}"
        await self._mount_chat_widget(Static(text, classes="system_msg btw_msg", markup=False))

    async def _slash_queue(self: AgentAppProtocol, args: str) -> None:
        """Inspect, pop, clear, or enqueue follow-up prompts (/queue [ls|pop|clear|<prompt>])."""
        q = getattr(self, "_queued_messages", None)
        if not isinstance(q, list):
            q = []
            setattr(self, "_queued_messages", q)

        raw = args.strip()
        lower = raw.lower()
        if not raw or lower in ("ls", "list"):
            if not q:
                msg = "Prompt queue is empty."
            else:
                lines = [f"Queued prompts ({len(q)}):"] + [f"  {i + 1}. {item}" for i, item in enumerate(q)]
                msg = "\n".join(lines)
        elif lower == "pop":
            if q:
                removed = q.pop()
                msg = f"Popped queued prompt: {removed}"
            else:
                msg = "Prompt queue is already empty."
        elif lower == "clear":
            count = len(q)
            q.clear()
            msg = f"Cleared {count} queued prompt(s)."
        else:
            q.append(raw)
            msg = f"Queued prompt #{len(q)}: {raw}"

        try:
            self.queue_size = len(q)  # type: ignore[attr-defined]
        except Exception:
            pass
        await self._mount_chat_widget(Static(msg, classes="system_msg queue_msg", markup=False))
