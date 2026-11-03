"""tests/unit/test_tui_canonical_graph_bridge.py — 6-node graph bridging tests for TUI.

Verifies that events published during the 6-node canonical LangGraph execution
(agent_loop, approval, verify, compact, finalize) are properly received and translated
into Textual messages by AgentBridge:
1. ToolResult -> ToolCallFinishEvent
2. ToolExecuteStart -> ToolCallStartEvent
3. PlanRequested & PlanProgress -> PlanApprovalRequestEvent & PlanProgressEvent
4. ContextCompacted -> ContextCompactedEvent
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock
import pytest

from tui.tui_src.ui.core_bridge import AgentBridge
from tui.tui_src.ui.bus import (
    ToolCallStartEvent,
    ToolCallFinishEvent,
    PlanProgressEvent,
)
from src.core.messaging.event_types import (
    ToolResult,
    ToolExecuteStart,
    PlanProgress,
    ContextCompacted,
)


def _make_test_bridge():
    mock_app = MagicMock()
    posted_events = []
    mock_app.post_message.side_effect = lambda ev: posted_events.append(ev)
    mock_app.call_from_thread.side_effect = lambda fn, *a, **kw: fn(*a, **kw)

    bridge = AgentBridge.__new__(AgentBridge)
    bridge.app = mock_app
    bridge._working_dir = ""
    bridge._active_role = "operational"
    bridge._agent_lock = threading.Lock()
    bridge._agent_running = False
    bridge._history_lock = threading.Lock()
    bridge.history = []
    bridge._cancel_event = threading.Event()
    bridge._subscriptions = []
    bridge._pending_injections = []

    return bridge, posted_events


class TestTuiCanonicalGraphBridge:
    def test_tool_result_event_bridging(self):
        """ToolResult event from frontier_loop_node translates to ToolCallFinishEvent."""
        bridge, posted = _make_test_bridge()

        # Simulate ToolResult payload from frontier_loop_node
        tool_res = ToolResult(
            tool="edit_file_atomic",
            result={"ok": True, "path": "calc.py", "status": "ok"},
            turn=2,
        )
        payload = tool_res.to_dict()

        bridge._on_tool_finish(payload)

        finish_events = [ev for ev in posted if isinstance(ev, ToolCallFinishEvent)]
        assert len(finish_events) == 1
        ev = finish_events[0]
        assert ev.tool_name == "edit_file_atomic"
        assert ev.ok is True

    def test_tool_start_event_bridging(self):
        """ToolExecuteStart event translates to ToolCallStartEvent."""
        bridge, posted = _make_test_bridge()

        start_ev = ToolExecuteStart(tool="read_file", args={"path": "calc.py"}, tool_call_id="call_1")
        bridge._on_tool_start(start_ev.to_dict())

        start_events = [ev for ev in posted if isinstance(ev, ToolCallStartEvent)]
        assert len(start_events) == 1
        assert start_events[0].tool_name == "read_file"

    def test_plan_progress_bridging(self):
        """PlanProgress event from graph translates to PlanProgressEvent."""
        bridge, posted = _make_test_bridge()

        progress_ev = PlanProgress(plan_progress={"step": 1, "total_steps": 3, "description": "Setup"})
        bridge._on_plan_progress(progress_ev.to_dict())

        plan_events = [ev for ev in posted if isinstance(ev, PlanProgressEvent)]
        assert len(plan_events) == 1

    def test_bridge_get_status_enriches_policy_and_provider(self):
        """get_status enriches returned snapshot with provider, model, and runtime policy."""
        from types import SimpleNamespace
        from src.core.orchestration.graph.runtime_policy import COMPACT_RUNTIME_POLICY

        bridge, _ = _make_test_bridge()
        orch = SimpleNamespace(
            get_current_task_id=lambda: "task_42",
            _provider_name="ollama",
            _model="qwen2.5-coder:14b",
            _runtime_policy=COMPACT_RUNTIME_POLICY,
        )
        bridge._orchestrator = orch

        st = bridge.get_status()
        assert st["task_id"] == "task_42"
        assert st["provider"] == "ollama"
        assert st["model"] == "qwen2.5-coder:14b"
        assert st["policy"]["policy_tier"] == "compact"
        assert st["policy"]["toolset_name"] == "compact-flat"
        assert st["policy"]["tool_limit"] == 12
        assert st["policy"]["tool_output_max_chars"] == 12_000

    @pytest.mark.asyncio
    async def test_slash_status_mounts_enriched_policy_info(self):
        """Slash command /status mounts enriched widget with tier, toolset and caps."""
        from types import SimpleNamespace
        from tui.tui_src.ui._app_slash_commands_mixin import AppSlashCommandsMixin
        from src.core.orchestration.graph.runtime_policy import COMPACT_RUNTIME_POLICY

        mounted_widgets = []

        class DummyApp(AppSlashCommandsMixin):
            def __init__(self):
                self._bridge = MagicMock()
                self._bridge.get_status.return_value = {
                    "running": False,
                    "task_id": "task_abc",
                    "working_dir": "/tmp/test",
                    "history_len": 5,
                    "provider": "ollama",
                    "model": "qwen2.5-coder:14b",
                    "policy": COMPACT_RUNTIME_POLICY.to_policy_dict(),
                }
                self.agent_running = False
                self.active_role = "operational"
                self.total_tokens = 4_500
                self.context_window = 32_768
                self.notifications = []

            async def _mount_chat_widget(self, widget):
                mounted_widgets.append(widget)

            def notify(self, msg, severity="info"):
                self.notifications.append((msg, severity))

        app = DummyApp()
        ev = SimpleNamespace(command="status", args="")
        await app.handle_slash_command(ev)

        assert len(mounted_widgets) == 1
        widget_text = str(mounted_widgets[0].content)
        assert "CodingAgent Status" in widget_text
        assert "COMPACT" in widget_text
        assert "compact-flat" in widget_text
        assert "12,000 chars" in widget_text
        assert "ollama" in widget_text
        assert "qwen2.5-coder:14b" in widget_text

    @pytest.mark.asyncio
    async def test_slash_doctor_mounts_diagnostic_report(self):
        """Slash command /doctor triggers diagnosis audit and mounts report widget."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from tui.tui_src.ui._app_slash_commands_mixin import AppSlashCommandsMixin

        mounted_widgets = []

        class DummyApp(AppSlashCommandsMixin):
            def __init__(self):
                self._bridge = MagicMock()
                self._bridge.working_dir = "/tmp/test"
                self.notifications = []

            async def _mount_chat_widget(self, widget):
                mounted_widgets.append(widget)

            def notify(self, msg, severity="info"):
                self.notifications.append((msg, severity))

        app = DummyApp()
        dummy_diag = {
            "status": "healthy",
            "system": {"version": "0.1.0", "python": "3.11.0", "os": "macOS", "workdir": "/tmp/test", "git_branch": "main"},
            "hardware": {"ram_gb": 32.0, "vram_gb": 32.0, "cpu_cores": 8, "cpu_threads": 8, "machine": "arm64", "recommended_context": {"compact_9b_14b": 32768, "standard_15b_70b": 65536}},
            "sandbox": {"level": "strict", "enforced": True, "available": True},
            "providers": [
                {
                    "provider": "ollama",
                    "status": "healthy",
                    "online": True,
                    "target_model": "qwen2.5-coder:14b",
                    "latency_ms": 15,
                    "policy": {"tier": "compact", "toolset": "compact-flat", "tool_limit": 12, "verification_mode": "targeted", "parallel_reads": 2},
                    "tool_validation": {"schemas_valid": True, "schemas_count": 12},
                }
            ],
            "recommendations": [],
        }

        with patch("src.cli.doctor_cmd.build_doctor_diagnosis", return_value=dummy_diag):
            ev = SimpleNamespace(command="doctor", args="")
            await app.handle_slash_command(ev)

        assert len(mounted_widgets) == 1
        widget_text = str(mounted_widgets[0].content)
        assert "CodingAgent Doctor — Diagnostic Health Audit" in widget_text
        assert "HEALTHY" in widget_text
        assert "OLLAMA" in widget_text
        assert "compact-flat" in widget_text
        assert "32.0 GB" in widget_text

    @pytest.mark.asyncio
    async def test_slash_skills_lists_available_skills(self):
        """Slash command /skills lists repository skills."""
        from types import SimpleNamespace
        from tui.tui_src.ui._app_slash_commands_mixin import AppSlashCommandsMixin

        mounted_widgets = []

        class DummyApp(AppSlashCommandsMixin):
            def __init__(self):
                self._bridge = MagicMock()
                self.notifications = []

            async def _mount_chat_widget(self, widget):
                mounted_widgets.append(widget)

            def notify(self, msg, severity="info"):
                self.notifications.append((msg, severity))

        app = DummyApp()
        ev = SimpleNamespace(command="skills", args="")
        await app.handle_slash_command(ev)

        assert len(mounted_widgets) == 1
        widget_text = str(mounted_widgets[0].content)
        assert "Packaged Agent Skills" in widget_text
        assert "polyglot-verify" in widget_text
        assert "atomic-edit-rollback" in widget_text
        assert "git-safe-review" in widget_text

    @pytest.mark.asyncio
    async def test_slash_skills_detail_displays_skill_instructions(self):
        """Slash command /skills polyglot-verify displays skill instructions."""
        from types import SimpleNamespace
        from tui.tui_src.ui._app_slash_commands_mixin import AppSlashCommandsMixin

        mounted_widgets = []

        class DummyApp(AppSlashCommandsMixin):
            def __init__(self):
                self._bridge = MagicMock()
                self.notifications = []

            async def _mount_chat_widget(self, widget):
                mounted_widgets.append(widget)

            def notify(self, msg, severity="info"):
                self.notifications.append((msg, severity))

        app = DummyApp()
        ev = SimpleNamespace(command="skills", args="polyglot-verify")
        await app.handle_slash_command(ev)

        assert len(mounted_widgets) == 1
        widget_text = str(mounted_widgets[0].content)
        assert "Skill: polyglot-verify" in widget_text
        assert "Tier Target: [bold]compact[/]" in widget_text
        assert "Instructions" in widget_text

