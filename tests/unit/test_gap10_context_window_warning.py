"""tests/unit/test_gap10_context_window_warning.py

GAP-10: context-window misconfiguration warning.

Verifies that perception_node publishes a ``ui.notification`` (warning level)
on round 0 when the active model is SMALL or FRONTIER but the adapter reports
a context window below 16384 tokens — the primary case being Gemma 4 E4B in
LM Studio with the default n_ctx=7168.

Test cases:
  GAP10-1  SMALL model, ctx=7168 (LM Studio default) → warning published
  GAP10-2  SMALL model, ctx=32768 → no warning
  GAP10-3  FRONTIER model, ctx=8192 → warning published
  GAP10-4  NANO model, ctx=4096 → no warning (NANO is excluded)
  GAP10-5  Warning is NOT published on round > 0 (fires once only)
  GAP10-6  Warning message includes the model name and advice to raise n_ctx
  GAP10-7  No exception when adapter lacks context_window attribute
"""


# ruff: noqa: E501
from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_TOOL_RESPONSE = {
    "choices": [{"message": {"content": "name: read_file\narguments:\n  path: x.py"}}],
    "prompt_tokens": 100,
    "completion_tokens": 10,
    "total_tokens": 110,
}


def _make_state(
    rounds: int = 0, task: str = "fix the bug in utils.py"
) -> Dict[str, Any]:
    return {
        "task": task,
        "working_dir": "/tmp",
        "history": [],
        "verified_reads": [],
        "next_action": None,
        "last_result": None,
        "rounds": rounds,
        "system_prompt": "",
        "errors": [],
        "session_id": "s1",
        "current_plan": None,
        "current_step": 0,
        "tool_call_count": 0,
        "empty_response_count": 0,
        "cancel_event": None,
    }


def _make_adapter(model: str, ctx_window: int, has_ctx_attr: bool = True) -> MagicMock:
    adapter = MagicMock()
    adapter.models = [model]
    if has_ctx_attr:
        adapter.context_window = ctx_window
    else:
        # Simulate adapter that does not expose context_window
        del adapter.context_window
    return adapter


def _make_orc(adapter: MagicMock) -> MagicMock:
    orc = MagicMock()
    orc.adapter = adapter
    orc.cancel_event = None
    orc.event_bus = MagicMock()
    orc.event_bus.publish = MagicMock()
    orc.msg_mgr = MagicMock()
    orc.token_monitor = None
    return orc


_PATCHES = [
    "src.core.orchestration.graph.nodes.perception_node.call_model",
    "src.core.orchestration.graph.nodes.perception_node.ContextBuilder",
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap10_warning_fired_small_model_low_ctx():
    """GAP10-1: SMALL model with ctx=7168 triggers ui.notification warning."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    adapter = _make_adapter("gemma-4-e4b-it", ctx_window=7168)
    orc = _make_orc(adapter)
    state = _make_state(rounds=0)
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(_PATCHES[0], return_value=_FAKE_TOOL_RESPONSE),
        patch(_PATCHES[1]),
    ):
        await perception_node(state, config)

    calls = orc.event_bus.publish.call_args_list
    notif_calls = [c for c in calls if c[0][0] == "ui.notification"]
    assert notif_calls, "GAP10-1: expected ui.notification to be published"
    payload = notif_calls[0][0][1]
    assert payload["level"] == "warning"


@pytest.mark.asyncio
async def test_gap10_no_warning_small_model_adequate_ctx():
    """GAP10-2: SMALL model with ctx=32768 must NOT trigger a warning."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    adapter = _make_adapter("gemma-4-e4b-it", ctx_window=32768)
    orc = _make_orc(adapter)
    state = _make_state(rounds=0)
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(_PATCHES[0], return_value=_FAKE_TOOL_RESPONSE),
        patch(_PATCHES[1]),
    ):
        await perception_node(state, config)

    calls = orc.event_bus.publish.call_args_list
    notif_calls = [
        c
        for c in calls
        if c[0][0] == "ui.notification"
        and c[0][1].get("level") == "warning"
        and "context" in c[0][1].get("message", "").lower()
    ]
    assert not notif_calls, (
        "GAP10-2: must NOT publish context-window warning when ctx is adequate"
    )


@pytest.mark.asyncio
async def test_gap10_warning_fired_frontier_model_low_ctx():
    """GAP10-3: FRONTIER model with ctx=8192 (too small) triggers a warning."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    adapter = _make_adapter("gemma-4-31b-it", ctx_window=8192)
    orc = _make_orc(adapter)
    state = _make_state(rounds=0)
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(_PATCHES[0], return_value=_FAKE_TOOL_RESPONSE),
        patch(_PATCHES[1]),
    ):
        await perception_node(state, config)

    calls = orc.event_bus.publish.call_args_list
    notif_calls = [c for c in calls if c[0][0] == "ui.notification"]
    assert notif_calls, "GAP10-3: FRONTIER model with low ctx should warn"
    assert notif_calls[0][0][1]["level"] == "warning"


@pytest.mark.asyncio
async def test_gap10_no_warning_nano_model_low_ctx():
    """GAP10-4: NANO model is excluded — no warning even if ctx is small."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    # qwen3:7b → NANO
    adapter = _make_adapter("qwen3:7b", ctx_window=4096)
    orc = _make_orc(adapter)
    state = _make_state(rounds=0)
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(_PATCHES[0], return_value=_FAKE_TOOL_RESPONSE),
        patch(_PATCHES[1]),
    ):
        await perception_node(state, config)

    calls = orc.event_bus.publish.call_args_list
    ctx_warns = [
        c
        for c in calls
        if c[0][0] == "ui.notification"
        and "context_window_check" in str(c[0][1].get("source", ""))
    ]
    assert not ctx_warns, "GAP10-4: NANO model must not trigger context-window warning"


@pytest.mark.asyncio
async def test_gap10_no_warning_on_round_greater_than_zero():
    """GAP10-5: Warning must not fire on rounds > 0 (fires once only)."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    adapter = _make_adapter("gemma-4-e4b-it", ctx_window=7168)
    orc = _make_orc(adapter)
    # round 2 — warning should have fired on round 0 already
    state = _make_state(rounds=2)
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(_PATCHES[0], return_value=_FAKE_TOOL_RESPONSE),
        patch(_PATCHES[1]),
    ):
        await perception_node(state, config)

    calls = orc.event_bus.publish.call_args_list
    ctx_warns = [
        c
        for c in calls
        if c[0][0] == "ui.notification"
        and "context_window_check" in str(c[0][1].get("source", ""))
    ]
    assert not ctx_warns, "GAP10-5: context-window warning must only fire on round 0"


@pytest.mark.asyncio
async def test_gap10_warning_message_contains_model_name_and_advice():
    """GAP10-6: Warning message includes the model name and n_ctx advice."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    adapter = _make_adapter("gemma-4-e4b-it", ctx_window=7168)
    orc = _make_orc(adapter)
    state = _make_state(rounds=0)
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(_PATCHES[0], return_value=_FAKE_TOOL_RESPONSE),
        patch(_PATCHES[1]),
    ):
        await perception_node(state, config)

    calls = orc.event_bus.publish.call_args_list
    notif_calls = [c for c in calls if c[0][0] == "ui.notification"]
    assert notif_calls, "GAP10-6: expected ui.notification"
    msg = notif_calls[0][0][1]["message"]
    assert "gemma-4-e4b-it" in msg, f"GAP10-6: model name missing from warning: {msg!r}"
    assert "n_ctx" in msg.lower() or "32768" in msg, (
        f"GAP10-6: n_ctx advice missing from warning: {msg!r}"
    )


@pytest.mark.asyncio
async def test_gap10_no_crash_when_adapter_missing_context_window():
    """GAP10-7: perception_node must not raise when adapter lacks context_window attr."""
    from src.core.orchestration.graph.nodes.perception_node import perception_node

    # Adapter without context_window attribute
    adapter = MagicMock(spec=["models"])
    adapter.models = ["gemma-4-e4b-it"]
    orc = _make_orc(adapter)
    state = _make_state(rounds=0)
    config = {"configurable": {"orchestrator": orc}}

    with (
        patch(_PATCHES[0], return_value=_FAKE_TOOL_RESPONSE),
        patch(_PATCHES[1]),
    ):
        # Must complete without raising
        result = await perception_node(state, config)

    assert isinstance(result, dict), "GAP10-7: perception_node must return a dict"
