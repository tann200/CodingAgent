from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_state(rounds: int = 0, session_id: str = "s1"):
    return {"rounds": rounds, "session_id": session_id}


def test_auto_compaction_skips_when_unavailable():
    # Ensure the helper is tolerant when auto-compactor globals are missing
    from src.core.orchestration.graph.nodes.perception_node import (
        _run_auto_compaction,
    )

    hist = [{"role": "assistant", "content": "a"}] * 10
    state = _make_state()
    adapter = MagicMock()
    orc = MagicMock()

    # Patch the module-level auto-compactor symbols to None to simulate missing
    with (
        patch(
            "src.core.orchestration.graph.nodes.perception_node._AutoCompactConfig",
            None,
        ),
        patch(
            "src.core.orchestration.graph.nodes.perception_node._should_compact",
            None,
        ),
        patch(
            "src.core.orchestration.graph.nodes.perception_node._compact_messages",
            None,
        ),
        patch(
            "src.core.orchestration.graph.nodes.perception_node._cfg_get",
            None,
        ),
    ):
        new_hist, new_compacted = _run_auto_compaction(hist, adapter, orc, state)

    assert new_hist == hist
    assert new_compacted is None


def test_auto_compaction_runs_and_emits_event():
    from src.core.orchestration.graph.nodes.perception_node import (
        _run_auto_compaction,
    )

    hist = [
        {"role": "assistant", "content": "old1"},
        {"role": "assistant", "content": "old2"},
    ]
    state = _make_state(rounds=5)
    adapter = MagicMock()
    orc = MagicMock()
    events = []
    orc.event_bus = MagicMock()
    orc.event_bus.publish = lambda e, p: events.append((e, p))

    # Create a fake compact_result object with expected attributes
    class FakeCompactResult:
        def __init__(self, removed, comp):
            self.removed_message_count = removed
            self.compacted_messages = comp

    def fake_should_compact(history, cfg):
        return True

    def fake_compact_messages(history, cfg):
        return FakeCompactResult(
            removed=1, comp=[{"role": "assistant", "content": "compact"}]
        )

    def fake_cfg_get(k, default=None):
        return default

    FakeConfig = lambda **kwargs: None

    with (
        patch(
            "src.core.orchestration.graph.nodes.perception_node._should_compact",
            fake_should_compact,
        ),
        patch(
            "src.core.orchestration.graph.nodes.perception_node._compact_messages",
            fake_compact_messages,
        ),
        patch(
            "src.core.orchestration.graph.nodes.perception_node._cfg_get",
            fake_cfg_get,
        ),
        patch(
            "src.core.orchestration.graph.nodes.perception_node._AutoCompactConfig",
            FakeConfig,
        ),
    ):
        new_hist, new_compacted = _run_auto_compaction(hist, adapter, orc, state)

    assert new_compacted is not None
    assert new_hist != hist
    assert any(e == "context.auto_compacted" for (e, _) in events)
