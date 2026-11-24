"""TASK-25 Stage B regression tests for native async AgentRunner."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from src.core.orchestration.graph.builder import (
    _reset_compiled_graph,
    build_tier_graph,
    compile_canonical_graph,
)
from src.core.orchestration.graph.state import replace_state_list
from src.core.orchestration.runner import AgentRunState, AgentRunner, apply_state_update


def test_compile_canonical_graph_returns_native_agent_runner() -> None:
    _reset_compiled_graph()
    try:
        runner = compile_canonical_graph()
        assert isinstance(runner, AgentRunner)
        assert set(runner.nodes.keys()) == {
            "initialize",
            "agent_loop",
            "approval",
            "verify",
            "compact",
            "finalize",
        }
        view = runner.get_graph()
        assert {"__start__", "initialize", "agent_loop", "approval", "verify", "compact", "finalize", "__end__"}.issubset(
            set(view.nodes.keys())
        )
        assert isinstance(build_tier_graph("frontier"), AgentRunner)
    finally:
        _reset_compiled_graph()


def test_apply_state_update_linear_history_and_replace() -> None:
    state: Dict[str, Any] = {"history": [{"role": "user", "content": "start"}], "verified_reads": ["a.py"]}
    # Delta append grows linearly O(N)
    for i in range(5):
        state = apply_state_update(
            state,
            {"history": [{"role": "assistant", "content": f"step-{i}"}], "verified_reads": [f"f{i}.py"]},
        )
    assert len(state["history"]) == 6
    assert len(state["verified_reads"]) == 6

    # ReplaceList replaces wholesale (compaction path)
    state = apply_state_update(
        state,
        {"history": replace_state_list([{"role": "system", "content": "compacted"}])},
    )
    assert state["history"] == [{"role": "system", "content": "compacted"}]


@pytest.mark.asyncio
async def test_agent_runner_executes_canonical_routing_and_cancellation() -> None:
    visited: list[str] = []

    async def init_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("initialize")
        return {"runtime": {"tier": "frontier", "policy": {"recovery_limit": 2}}, "run": {"status": "running", "outcome": "continue"}}

    async def loop_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("agent_loop")
        turns = int((state.get("run") or {}).get("turns") or 0) + 1
        if turns == 1:
            return {
                "history": [{"role": "assistant", "content": "edited file"}],
                "modified_files": ["foo.py"],
                "run": {"status": "running", "outcome": "verify", "turns": turns},
            }
        return {
            "history": [{"role": "assistant", "content": "fixed after verify"}],
            "run": {"status": "completed", "outcome": "complete", "turns": turns},
        }

    async def approval_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("approval")
        return {"run": {"status": "running", "outcome": "continue"}}

    async def verify_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("verify")
        return {
            "verification_passed": False,
            "run": {"status": "running", "outcome": "recover", "recoveries": 1, "turns": 1},
        }

    async def compact_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("compact")
        return {"run": {"status": "running", "outcome": "continue"}}

    async def finalize_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        visited.append("finalize")
        return {"run": {"status": "completed", "outcome": "complete"}, "evaluation_result": "complete"}

    runner = AgentRunner(
        nodes={
            "initialize": init_node,
            "agent_loop": loop_node,
            "approval": approval_node,
            "verify": verify_node,
            "compact": compact_node,
            "finalize": finalize_node,
        }
    )

    out = await runner.ainvoke(
        {"task": "fix foo", "working_dir": ".", "history": [{"role": "user", "content": "fix foo"}]}
    )
    assert visited == ["initialize", "agent_loop", "verify", "agent_loop", "finalize"]
    assert out["evaluation_result"] == "complete"
    # Linear history: 1 user + 2 assistant deltas = 3
    assert len(out["history"]) == 3


@pytest.mark.asyncio
async def test_agent_runner_enforces_recursion_limit() -> None:
    async def noop(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
        return {"run": {"status": "running", "outcome": "continue"}}

    runner = AgentRunner(
        nodes={
            "initialize": noop,
            "agent_loop": noop,
            "approval": noop,
            "verify": noop,
            "compact": noop,
            "finalize": noop,
        }
    )
    with pytest.raises(RecursionError):
        await runner.ainvoke({"task": "loop"}, config={"recursion_limit": 5})


@pytest.mark.asyncio
async def test_agent_runner_zero_unserializable_channel_warnings_and_sqlite_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    from src.core.memory.session_store import SessionStore
    from src.core.orchestration.graph.checkpoint_saver import default_checkpointer

    monkeypatch.setenv("CODINGAGENT_GRAPH_CHECKPOINTING", "1")
    store = SessionStore(workdir=str(tmp_path))
    recorded_warnings: list[str] = []

    class _CaptureWarnings(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                recorded_warnings.append(record.getMessage())

    handler = _CaptureWarnings()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        orch = SimpleNamespace(
            working_dir=tmp_path,
            _working_dir=str(tmp_path),
            _current_task_id="runner-ckpt-1",
            session_store=store,
        )

        async def init_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
            return {"run": {"status": "running", "outcome": "continue"}}

        async def loop_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
            return {"run": {"status": "completed", "outcome": "complete"}}

        async def fin_node(state: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
            return {"run": {"status": "completed", "outcome": "complete"}}

        runner = AgentRunner(
            nodes={
                "initialize": init_node,
                "agent_loop": loop_node,
                "approval": fin_node,
                "verify": fin_node,
                "compact": fin_node,
                "finalize": fin_node,
            },
            checkpointer=default_checkpointer(),
        )

        class _UnserializableWorkflowConfig:
            pass

        await runner.ainvoke(
            {
                "task": "test task",
                "session_id": "runner-ckpt-1",
                "working_dir": str(tmp_path),
                "workflow_config": _UnserializableWorkflowConfig(),
                "history": [{"role": "user", "content": "secret ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD"}],
            },
            config={"configurable": {"orchestrator": orch, "thread_id": "runner-ckpt-1"}},
        )

        assert not any("dropping unserializable channel" in msg for msg in recorded_warnings)
        saved = store.load_session_state("runner-ckpt-1")
        assert saved is not None
        assert saved["last_node"] == "finalize"
        assert "ghp_0123456789" not in str(saved)
    finally:
        root_logger.removeHandler(handler)
        store.close()


def test_agent_run_state_roundtrip() -> None:
    st = AgentRunState.from_mapping(
        {"task": "hello", "working_dir": "/tmp", "session_id": "s1", "custom_flag": True}
    )
    d = st.to_dict()
    assert d["task"] == "hello"
    assert d["custom_flag"] is True
