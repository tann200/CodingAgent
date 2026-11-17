from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_agent_bridge_publish_typed_reaches_event_bus_and_legacy_subscribers():
    from src.core.messaging.bus import MessageBus
    from src.core.orchestration.event_bus import EventBus
    from src.core.messaging.event_types import ModelRouting
    from tui.tui_src.ui.core_bridge import AgentBridge

    bus = EventBus(typed_bus=MessageBus())
    received_legacy: list[dict] = []
    bus.subscribe("model.routing", received_legacy.append)

    app = MagicMock()
    bridge = AgentBridge(app=app)
    bridge._bus = bus
    bridge._typed_bus = bus._typed
    try:
        bridge.publish_typed(ModelRouting(provider="ollama", selected="qwen3:14b", available_models=[]))
    finally:
        bus._typed.shutdown(timeout=1.0)

    assert len(received_legacy) == 1
    assert received_legacy[0]["provider"] == "ollama"
    assert received_legacy[0]["selected"] == "qwen3:14b"


def test_bash_approval_required_typed_mapping_and_bridge_forwarding():
    from src.core.messaging.bus import MessageBus
    from src.core.orchestration.event_bus import EventBus
    from src.core.messaging.event_types import BashApprovalRequired
    from tui.tui_src.ui.bus import BashApprovalEvent, ToolCallStartEvent
    from tui.tui_src.ui._bridge_tools import BridgeToolsMixin

    bus = EventBus(typed_bus=MessageBus())
    typed_seen: list[BashApprovalRequired] = []
    done = threading.Event()

    class _Handler:
        def handle(self, ev):
            typed_seen.append(ev)
            done.set()

    bus._typed.subscribe(BashApprovalRequired, _Handler())
    try:
        bus.publish("bash.approval_required", {"tool_id": "bash-42", "command": "pip install foo"})
        assert done.wait(timeout=2.0)
    finally:
        bus._typed.shutdown(timeout=1.0)

    assert len(typed_seen) == 1
    assert typed_seen[0].tool_id == "bash-42"

    posted: list[object] = []

    class DummyBridge(BridgeToolsMixin):
        def __init__(self, orchestrator=None):
            self._orchestrator = orchestrator

        def _post(self, ev):
            posted.append(ev)

    b_real = DummyBridge(orchestrator=object())
    b_real._on_bash_approval_required({"tool_id": "gate-99", "command": "rm -rf /tmp/x"})
    assert any(isinstance(e, BashApprovalEvent) and e.tool_id == "gate-99" for e in posted)

    posted.clear()
    b_real._on_tool_start({"tool": "bash", "tool_call_id": "tc-1", "args": {"command": "pip install x"}})
    assert any(isinstance(e, ToolCallStartEvent) for e in posted)
    assert not any(isinstance(e, BashApprovalEvent) for e in posted)


def test_delegate_task_emits_only_spawn_permission_required(monkeypatch):
    from src.core.orchestration.tool_execution_pipeline import _run_permission_gate
    from src.core.messaging.event_types import SpawnPermissionRequired, ToolPermissionRequired

    published: list[object] = []

    class DummyBus:
        def publish_typed(self, ev):
            published.append(ev)

    orch = MagicMock()
    orch.event_bus = DummyBus()
    orch.permission_mode = None
    orch._approval_gate = None

    monkeypatch.setattr("src.tools.tools_config.is_autonomous", lambda: False)
    monkeypatch.setattr(
        "src.core.orchestration.tool_execution_pipeline.register_tool_gate",
        lambda _tid: type("E", (), {"wait": lambda _s, timeout=None: True})(),
    )
    monkeypatch.setattr("src.core.orchestration.tool_execution_pipeline.is_tool_denied", lambda _tid: False)
    monkeypatch.setattr("src.core.orchestration.tool_execution_pipeline.discard_tool_denied", lambda _tid: None)

    res = _run_permission_gate(
        orch,
        "delegate_task",
        {"role": "scout", "subtask_description": "inspect"},
        "tc-spawn-1",
        needs_gate=True,
    )
    assert res is None
    assert any(isinstance(ev, SpawnPermissionRequired) for ev in published)
    assert not any(isinstance(ev, ToolPermissionRequired) for ev in published)


@pytest.mark.asyncio
async def test_allow_always_blocks_delete_file_and_uuid_fallback_and_doom_force_interrupt():
    from tui.tui_src.ui._app_tool_handlers_mixin import AppToolHandlersMixin

    class FakeBtn:
        def __init__(self, btn_id: str):
            self.id = btn_id

    class FakeEvent:
        def __init__(self, btn_id: str):
            self.button = FakeBtn(btn_id)
            self.stopped = False

        def stop(self):
            self.stopped = True

    class FakeApp(AppToolHandlersMixin):
        def __init__(self):
            self._allow_always_tools = set()
            self._perm_tool_names = {"tid_del": "delete_file"}
            self._perm_raw_ids = {"safe_doom": "doom:raw/1"}
            self._tool_widgets = {}
            self._bridge = MagicMock()
            self._mounted = []
            self._posted = []
            self._notified = []

        def _update_perm_badge(self, _d):
            pass

        def query_one(self, *_a, **_kw):
            raise RuntimeError("no widget")

        async def _mount_chat_widget(self, w):
            self._mounted.append(w)

        def post_message(self, m):
            self._posted.append(m)

        def notify(self, msg, **_kw):
            self._notified.append(msg)

    app = FakeApp()
    # 1) delete_file must never be added to _allow_always_tools
    await app.on_any_button(FakeEvent("btn_tool_perm_always_tid_del"))
    assert "delete_file" not in app._allow_always_tools
    # 2) unknown UUID fallback must never be added to _allow_always_tools
    await app.on_any_button(FakeEvent("btn_tool_perm_always_unknown-uuid-1234"))
    assert "unknown-uuid-1234" not in app._allow_always_tools
    assert app._allow_always_tools == set()
    # 3) doom stop button must call force_interrupt()
    await app.on_any_button(FakeEvent("btn_doom_deny_safe_doom"))
    app._bridge.force_interrupt.assert_called_once()


def test_in_memory_handler_thread_safe_and_console_marshals():
    from tui.tui_src.ui.logging import InMemoryHandler
    from tui.tui_src.ui.components.console import ConsolePanel
    import logging

    handler = InMemoryHandler(maxlen=100)
    assert hasattr(handler, "_lock")
    rec = logging.LogRecord("x", logging.INFO, "", 1, "msg", (), None)
    handler.emit(rec)
    assert len(handler.get_lines()) == 1

    panel = ConsolePanel.__new__(ConsolePanel)
    mock_app = MagicMock()
    mock_app._loop = None
    mock_app._thread_id = threading.get_ident() + 999999  # different thread id
    panel._append_log_line = MagicMock()
    panel_app_prop = property(lambda self: mock_app)
    orig_app = getattr(ConsolePanel, "app", None)
    try:
        ConsolePanel.app = panel_app_prop  # type: ignore[assignment]
        panel._on_new_log("line-from-worker")
    finally:
        if orig_app is not None:
            ConsolePanel.app = orig_app  # type: ignore[assignment]

    mock_app.call_from_thread.assert_called_once_with(panel._append_log_line, "line-from-worker")


@pytest.mark.asyncio
async def test_rich_markup_escaping_and_safe_dom_ids_and_css():
    from textual.app import App, ComposeResult
    from tui.tui_src.ui.components.todo_list import TodoListWidget
    from tui.tui_src.ui.components.subagent_progress import SubagentProgress
    from tui.tui_src.ui.components.thinking import ThinkingProcess
    from tui.tui_src.ui._app_tool_handlers_mixin import AppToolHandlersMixin

    safe1 = AppToolHandlersMixin._safe_dom_id("bad:id/with [brackets]")
    safe2 = AppToolHandlersMixin._safe_dom_id("1starts-with-digit")
    assert safe1.replace("-", "").replace("_", "").isalnum()
    assert safe2[0].isalpha() or safe2[0] == "_"

    rendered = TodoListWidget._render_item({"status": "in_progress", "description": "Fix [red]bug[/red]"})
    assert "\\[red]" in rendered

    class MarkupApp(App):
        def compose(self) -> ComposeResult:
            yield TodoListWidget(items=[{"status": "pending", "description": "[bad/markup]"}])
            yield SubagentProgress(role="[bold red]evil", task="inspect [foo]", child_session_id="c1")
            yield ThinkingProcess("Think about [invalid_tag] here")

    async with MarkupApp().run_test() as pilot:
        sp = pilot.app.query_one(SubagentProgress)
        sp.finish(True)
        await pilot.pause()

    css_text = Path("tui/tui_src/ui/styles/app.tcss").read_text(encoding="utf-8")
    assert "#mcp_status_chip" in css_text
    assert "#subagent_footer_chip" in css_text
