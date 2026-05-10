from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_build_perception_result_preserves_state_and_snapshot(monkeypatch):
    from src.core.orchestration.graph.nodes.perception_node import (
        _build_perception_result,
    )
    from src.core.orchestration.graph.nodes import perception_node as perception_node_module

    monkeypatch.setattr(perception_node_module, "_tic", lambda state: True)

    class SnapshotManager:
        async def track(self):
            return "snap-new"

    orchestrator = SimpleNamespace(
        snapshot_manager=SnapshotManager(),
        _agent_mode="planning",
    )
    state = {
        "rounds": 4,
        "empty_response_count": 2,
        "session_cost_usd": 1.25,
        "snapshots": [f"snap-{i}" for i in range(12)],
        "current_plan": [{"description": "step"}],
        "current_step": 1,
        "task_decomposed": True,
        "original_task": "orig task",
        "task": "complex task",
    }

    result = await _build_perception_result(
        state=state,
        orchestrator=orchestrator,
        content="assistant reply",
        tool_call={"name": "read", "arguments": {}},
        turn_count=5,
        overflow_compaction={"_budget_compaction": True},
        model_tier_str="small",
        session_cost_delta=0.5,
        new_compacted_history=[{"role": "system", "content": "compact"}],
    )

    assert result["history"] == [{"role": "assistant", "content": "assistant reply"}]
    assert result["next_action"] == {"name": "read", "arguments": {}}
    assert result["rounds"] == 5
    assert result["turn_count"] == 5
    assert result["empty_response_count"] == 2
    assert result["errors"] == []
    assert result["_budget_compaction"] is True
    assert result["model_tier"] == "small"
    assert result["session_cost_usd"] == 1.75
    assert result["snapshots"] == [f"snap-{i}" for i in range(3, 12)] + ["snap-new"]
    assert result["current_plan"] == [{"description": "step"}]
    assert result["current_step"] == 1
    assert result["task_decomposed"] is True
    assert result["original_task"] == "orig task"
    assert result["agent_mode"] == "planning"
    assert result["task_complexity"] == "complex"
    assert result["_compacted_history"] == [{"role": "system", "content": "compact"}]
    assert result["_compaction_last_round"] == 4


@pytest.mark.asyncio
async def test_build_perception_result_omits_optional_fields_when_absent(monkeypatch):
    from src.core.orchestration.graph.nodes.perception_node import (
        _build_perception_result,
    )
    from src.core.orchestration.graph.nodes import perception_node as perception_node_module

    monkeypatch.setattr(perception_node_module, "_tic", lambda state: False)

    orchestrator = SimpleNamespace(snapshot_manager=None)
    state = {
        "rounds": 0,
        "empty_response_count": 0,
        "task": "tiny task",
    }

    result = await _build_perception_result(
        state=state,
        orchestrator=orchestrator,
        content="",
        tool_call=None,
        turn_count=1,
        overflow_compaction={},
        model_tier_str=None,
        session_cost_delta=0.0,
        new_compacted_history=None,
    )

    assert result["history"] == []
    assert result["next_action"] is None
    assert result["rounds"] == 1
    assert result["turn_count"] == 1
    assert result["empty_response_count"] == 0
    assert result["errors"] == []
    assert result["task_complexity"] == "simple"
    assert "model_tier" not in result
    assert "session_cost_usd" not in result
    assert "snapshots" not in result
    assert "agent_mode" not in result
    assert "_compacted_history" not in result
