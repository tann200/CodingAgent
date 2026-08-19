from __future__ import annotations

from pathlib import Path

# ruff: noqa: E501
from unittest.mock import MagicMock, patch


def test_dispatch_result_event_published_to_parent(tmp_path: Path) -> None:
    """delegate_task should publish a DispatchResultEvent to the parent orchestrator's event_bus with the summary content."""

    # Fake async graph that returns a final_state with a work_summary
    async def _fake_ainvoke(state, config=None):
        return {
            "task": state.get("task", ""),
            "history": [{"role": "assistant", "content": "assistant summary"}],
            "errors": [],
            "last_result": {"status": "ok"},
            "session_id": state.get("session_id", "test"),
            "work_summary": "final work summary",
        }

    mock_graph = MagicMock()
    mock_graph.ainvoke = _fake_ainvoke

    # Parent orchestrator and fake event bus to capture published dispatch results
    class FakeEventBus:
        def __init__(self):
            self.published = []
            self.dispatch_results = []

        def publish(self, name, payload):
            self.published.append((name, payload))

        def publish_dispatch(self, event):
            self.published.append(("dispatch", event))

        def publish_dispatch_result(self, event):
            self.dispatch_results.append(event)

    parent_orch = MagicMock()
    parent_orch.event_bus = FakeEventBus()
    parent_orch._current_task_id = "parent_session"
    parent_orch.active_agent = None

    # Patch the canonical graph resolver, AgentBrainManager, and the parent-context var
    with (
        patch("src.tools.subagent_tools._resolve_subagent_graph") as mock_resolver,
        patch("src.tools.subagent_tools._get_agent_brain_manager") as mock_brain_mgr,
        patch("src.tools.subagent_tools._PARENT_ORCHESTRATOR_VAR") as mock_ctxvar,
    ):
        mock_resolver.return_value = mock_graph
        mock_brain = MagicMock()
        mock_brain.compile_system_prompt.return_value = "sys"
        mock_brain_mgr.return_value = mock_brain
        mock_ctxvar.get.return_value = parent_orch

        from src.tools.subagent_tools import delegate_task

        # Execute delegate_task — it will run the fake graph and should publish result
        delegate_task(
            role="analyst", subtask_description="do work", working_dir=str(tmp_path)
        )

    # Assert the DispatchResultEvent was published with the expected content
    assert (
        len(parent_orch.event_bus.dispatch_results) == 1
    ), "DispatchResultEvent not published"
    evt = parent_orch.event_bus.dispatch_results[0]
    # The published event should have a content attribute with our final summary
    assert hasattr(evt, "content")
    assert evt.content == "final work summary"
