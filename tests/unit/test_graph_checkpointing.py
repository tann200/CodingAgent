"""Audit PHASE-3 item 3.4: graph-state checkpointing tests.

Covers the JSONL langgraph saver, the round-boundary state snapshots that
drive crash recovery, checkpointing toggles, and the tier-graph wiring.
"""

from __future__ import annotations

import json
import threading
import typing
from pathlib import Path

import pytest

from langgraph.graph import END, START, StateGraph

from src.core.orchestration.graph import checkpoint_saver as cs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Orch:
    def __init__(self, workdir: Path, task_id: str = "T1"):
        self.working_dir = workdir
        self._current_task_id = task_id


def _full_checkpoint(channel_values: dict | None = None, cid: str = "c1") -> dict:
    return {
        "v": 4,
        "ts": "2026-01-01T00:00:00Z+00:00",
        "id": cid,
        "channel_values": channel_values or {} if channel_values is not None else {},
        "channel_versions": {"__start__": 1},
        "versions_seen": {"__input__": {}, "__start__": {"__start__": 1}},
        "versions": {},
        "pending_sends": [],
        "current_tasks": {},
        "next": (),
        "metadata": {},
    }


@pytest.fixture
def orch(tmp_path: Path) -> _Orch:
    return _Orch(tmp_path, task_id="T1")


@pytest.fixture(autouse=True)
def _enable_checkpointing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODINGAGENT_GRAPH_CHECKPOINTING", "1")


def _base_config(orch: _Orch) -> typing.Any:
    return {"configurable": {"thread_id": cs.thread_key(orch), "orchestrator": orch}}


# ---------------------------------------------------------------------------
# TestPickleSerde
# ---------------------------------------------------------------------------


class TestPickleSerde:
    class _Sample:
        def __init__(self, n: int) -> None:
            self.n = n

        def __eq__(self, other: object) -> bool:
            return isinstance(other, TestPickleSerde._Sample) and other.n == self.n

    def test_round_trip_primitive_and_objects(self) -> None:
        for payload in (42, "hi", {"a": [1, 2]}, self._Sample(7)):
            blob = cs.PickleSerde.dumps_typed(payload)
            assert isinstance(blob, tuple)
            assert blob[0] == "pickle"
            loaded = cs.PickleSerde.loads_typed(blob)
            if isinstance(payload, self._Sample):
                assert isinstance(loaded, self._Sample) and loaded.n == 7
            else:
                assert loaded == payload

    def test_threading_event_unpicklable(self) -> None:
        with pytest.raises(Exception):
            cs.PickleSerde.dumps_typed(threading.Event())


# ---------------------------------------------------------------------------
# TestJsonlCheckpointSaver
# ---------------------------------------------------------------------------


class TestJsonlCheckpointSaver:
    def test_round_trip(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        ckpt = _full_checkpoint(channel_values={"count": 3, "label": "B"}, cid="c1")
        cfg = _base_config(orch)
        returned = saver.put(cfg, ckpt, {"source": "loop", "step": 2}, {"__start__": 1})
        assert returned["configurable"]["checkpoint_id"] == "c1"

        tup = saver.get_tuple(cfg)
        assert tup is not None
        assert tup.checkpoint["channel_values"] == {"count": 3, "label": "B"}
        assert tup.checkpoint["channel_versions"] == {"__start__": 1}
        assert tup.metadata["step"] == 2
        assert tup.parent_config is None

    def test_live_channels_excluded(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        ev = threading.Event()
        ckpt = _full_checkpoint(
            channel_values={"count": 1, "cancel_event": ev, "_file_lock_manager": object()},
            cid="c1",
        )
        saver.put(_base_config(orch), ckpt, {}, {})
        tup = saver.get_tuple(_base_config(orch))
        assert tup is not None
        assert tup.checkpoint["channel_values"] == {"count": 1}
        assert "cancel_event" not in tup.checkpoint["channel_values"]

    def test_unpicklable_value_dropped(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        ckpt = _full_checkpoint(
            channel_values={"count": 1, "fn": lambda: 1},
            cid="c1",
        )
        saver.put(_base_config(orch), ckpt, {}, {})
        tup = saver.get_tuple(_base_config(orch))
        assert tup is not None
        assert tup.checkpoint["channel_values"] == {"count": 1}

    def test_get_tuple_missing_thread_returns_none(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        assert saver.get_tuple(_base_config(orch)) is None

    def test_latest_wins(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        saver.put(
            _base_config(orch), _full_checkpoint(channel_values={"count": 1}, cid="c1"), {}, {}
        )
        saver.put(
            _base_config(orch), _full_checkpoint(channel_values={"count": 2}, cid="c2"), {}, {}
        )
        tup = saver.get_tuple(_base_config(orch))
        assert tup is not None
        assert tup.checkpoint["channel_values"] == {"count": 2}

    def test_explicit_checkpoint_id(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        saver.put(
            _base_config(orch), _full_checkpoint(channel_values={"count": 1}, cid="c1"), {}, {}
        )
        saver.put(
            _base_config(orch), _full_checkpoint(channel_values={"count": 2}, cid="c2"), {}, {}
        )
        cfg = {**_base_config(orch)}
        cfg["configurable"] = {**cfg["configurable"], "checkpoint_id": "c1"}
        tup = saver.get_tuple(cfg)
        assert tup is not None
        assert tup.checkpoint["channel_values"] == {"count": 1}

    def test_list_newest_first_before_limit(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        for i in range(4):
            saver.put(
                _base_config(orch),
                _full_checkpoint(channel_values={"count": i}, cid=f"c{i}"),
                {},
                {},
            )
        listed = list(saver.list(_base_config(orch)))
        assert [t.checkpoint["id"] for t in listed] == ["c3", "c2", "c1", "c0"]
        limited = list(saver.list(_base_config(orch), limit=2))
        assert [t.checkpoint["id"] for t in limited] == ["c3", "c2"]
        before_c2 = list(saver.list(_base_config(orch), before={"configurable": {"checkpoint_id": "c2"}}))
        assert [t.checkpoint["id"] for t in before_c2] == ["c1", "c0"]

    def test_put_writes_pending_and_dedup(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        cfg = _base_config(orch)
        saver.put(cfg, _full_checkpoint(channel_values={"count": 0}, cid="c1"), {}, {})
        writes_cfg = {
            "configurable": {**cfg["configurable"], "checkpoint_id": "c1"}
        }
        saver.put_writes(writes_cfg, [("branch:to:__interrupt__", ("msg", "hello"))], "t1")
        saver.put_writes(writes_cfg, [("branch:to:__interrupt__", ("msg", "bye"))], "t2")
        tup = saver.get_tuple(cfg)
        assert tup is not None
        assert tup.pending_writes is not None
        assert len(tup.pending_writes) == 2
        assert tup.pending_writes[0][1] == "branch:to:__interrupt__"

    def test_delete_thread_removes_files(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        saver.put(_base_config(orch), _full_checkpoint(channel_values={"count": 1}, cid="c1"), {}, {})
        saver.put_writes(
            {"configurable": {**_base_config(orch)["configurable"], "checkpoint_id": "c1"}},
            [("branch:to:x", "value")],
            "t",
        )
        base = Path(orch.working_dir) / ".codingAgent" / "checkpoints"
        assert (base / "checkpoint_T1.jsonl").exists()
        assert (base / "checkpoint_T1.writes.jsonl").exists()
        saver.delete_thread("T1")
        assert not (base / "checkpoint_T1.jsonl").exists()
        assert not (base / "checkpoint_T1.writes.jsonl").exists()
        assert "T1" not in cs._DIR_REGISTRY

    def test_get_tuple_corrupt_record(self, orch: _Orch) -> None:
        saver = cs.JsonlCheckpointSaver()
        base = Path(orch.working_dir) / ".codingAgent" / "checkpoints"
        base.mkdir(parents=True, exist_ok=True)
        (base / "checkpoint_T1.jsonl").write_text("{not-json\n", encoding="utf-8")
        assert saver.get_tuple(_base_config(orch)) is None


# ---------------------------------------------------------------------------
# TestCheckpointingToggle
# ---------------------------------------------------------------------------


class TestCheckpointingToggle:
    def test_default_off_under_pytest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CODINGAGENT_GRAPH_CHECKPOINTING", raising=False)
        monkeypatch.setattr(
            "src.core.orchestration.graph.checkpoint_saver._checkpointing_enabled",
            lambda: False,
        )
        assert cs._checkpointing_enabled() is False
        monkeypatch.undo()

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODINGAGENT_GRAPH_CHECKPOINTING", "1")
        assert cs._checkpointing_enabled() is True
        monkeypatch.setenv("CODINGAGENT_GRAPH_CHECKPOINTING", "0")
        assert cs._checkpointing_enabled() is False

    def test_snapshot_noop_when_disabled(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CODINGAGENT_GRAPH_CHECKPOINTING", "0")
        o = _Orch(tmp_path, task_id="T2")
        cs.save_thread_state(o, {"turn_count": 7})
        assert cs.load_thread_state(o) is None


# ---------------------------------------------------------------------------
# TestResumeIntegration
# ---------------------------------------------------------------------------


class TestResumeIntegration:
    def test_snapshot_round_trip_and_merge(self, orch: _Orch) -> None:
        state = {
            "turn_count": 3,
            "history": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "STATUS: complete"}],
            "working_dir": str(orch.working_dir),
            "cancel_event": threading.Event(),
            "files_read": {"/x": "content"},
            "drop_me": object(),
        }
        cs.save_thread_state(orch, state)
        prior = cs.load_thread_state(orch)
        assert prior is not None
        assert prior["turn_count"] == 3
        assert prior["history"][-1]["role"] == "assistant"
        assert "cancel_event" not in prior
        assert "drop_me" not in prior

        fresh = {"turn_count": 0, "history": [{"role": "user", "content": "hi"}], "working_dir": str(orch.working_dir)}
        merged = cs.rehydrate_initial_state(fresh, prior)
        assert merged["turn_count"] == 3
        assert merged["files_read"]["/x"] == "content"
        assert "cancel_event" not in merged

    def test_has_resumable_cancel_guard(self, orch: _Orch) -> None:
        class St(typing.TypedDict, total=False):
            x: int

        sg = StateGraph(St)
        sg.add_node("a", lambda s: {"x": 1})
        sg.add_edge(START, "a")
        sg.add_edge("a", END)
        graph = sg.compile()
        assert cs.has_resumable_checkpoint(graph, orch) is False
        cs.save_thread_state(orch, {"turn_count": 1})
        assert cs.has_resumable_checkpoint(graph, orch) is True
        ev = threading.Event()
        ev.set()
        assert cs.has_resumable_checkpoint(graph, orch, cancel_event=ev) is False
        cs.purge_thread_checkpoint(orch)
        assert cs.has_resumable_checkpoint(graph, orch) is False

    def test_crash_round_recovery(self, orch: _Orch) -> None:
        class S(typing.TypedDict, total=False):
            count: int
            history: list
            label: str

        calls: list[str] = []

        def node(name: str, label: str):
            def run(state: S) -> dict:
                calls.append(name)
                return {
                    "count": (state.get("count") or 0) + 1,
                    "label": label,
                    "history": state.get("history", []) + [{"role": "assistant", "content": label}],
                }
            return run

        g = StateGraph(S)
        g.add_node("a", node("a", "A"))
        g.add_node("b", node("b", "B"))
        g.add_node("c", node("c", "C"))
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", END)
        app: typing.Any = g.compile(checkpointer=cs.JsonlCheckpointSaver())
        cfg = _base_config(orch)
        svc = app.invoke({"count": 0, "history": [], "label": ""}, cfg)

        # Round 1 completed cleanly — simulate the inference loop persisting it.
        cs.save_thread_state(orch, svc)
        assert cs.has_resumable_checkpoint(app, orch) is True

        # "Process crash": drop the langgraph thread files, keep the round snapshot.
        saver = cs.JsonlCheckpointSaver()
        saver.delete_thread(cs.thread_key(orch))
        calls.clear()

        # Fresh restart of the same thread: rehydrate, then run the next round.
        fresh = {"count": 0, "history": [{"role": "user", "content": "x"}], "label": ""}
        prior = cs.load_thread_state(orch)
        assert prior is not None and prior["count"] > 0
        state = cs.rehydrate_initial_state(fresh, prior)
        out = app.invoke(state, cfg)  # type: ignore[call-overload]

        assert out["count"] > 0
        assert out["history"][0]["content"] == "A"
        cs.purge_thread_checkpoint(orch)
        assert cs.has_resumable_checkpoint(app, orch) is False


# ---------------------------------------------------------------------------
# TestBuilderIntegration
# ---------------------------------------------------------------------------


class TestBuilderIntegration:
    def test_tier_graphs_use_checkpointer(self) -> None:
        from src.core.orchestration.graph.builder import (
            _reset_compiled_graph,
            build_tier_graph,
        )

        try:
            for tier in ("frontier", "lite"):
                _reset_compiled_graph()
                compiled = build_tier_graph(tier)
                assert isinstance(compiled.checkpointer, cs.JsonlCheckpointSaver)
        finally:
            _reset_compiled_graph()

    def test_run_graph_round_sync_config_carries_thread(self, orch: _Orch) -> None:
        from src.core.orchestration.inference_loop_rounds import _run_graph_round_sync

        captured: dict = {}

        class FakeGraph:
            async def ainvoke(self, state: dict, config: dict) -> dict:
                captured["config"] = config
                return {"ok": True}

        result = _run_graph_round_sync(FakeGraph(), orch, {"x": 1})
        assert result == {"ok": True}
        assert captured["config"]["configurable"]["thread_id"] == "T1"
        assert captured["config"]["configurable"]["orchestrator"] is orch


def test_thread_key_default() -> None:
    class BareOrch:
        pass

    assert cs.thread_key(BareOrch()) == "default"
