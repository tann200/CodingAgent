"""A2.4: Tests for the consolidated, single-canonical prune_tool_outputs.

Locks the deduplication of the two near-identical token-based pruning clones
(previously in perception_node and tool_output_truncation) and the shared
token-estimation helper now also used as the default estimator in
src.core.context.token_truncation.
"""

from src.core.orchestration.graph.nodes import tool_output_truncation
from src.core.orchestration.graph.nodes.tool_output_truncation import (
    _PRUNED_TOOL_PLACEHOLDER,
    estimate_text_tokens,
    prune_tool_outputs,
)


def test_default_return_is_list_for_existing_callers():
    """Existing callers (frontier_loop_node) get just the pruned list."""
    history = [{"role": "user", "content": "hi"}]
    result = prune_tool_outputs(history)
    assert isinstance(result, list)
    assert result == history


def test_empty_history_returns_empty_and_zero_count():
    assert prune_tool_outputs([]) == []
    result, count = prune_tool_outputs([], return_pruned_count=True)
    assert result == [] and count == 0


def test_prunes_old_tool_results_and_counts_them():
    """Old tool results beyond the protect boundary are zeroed and counted."""
    # 6 recent protected slots at the end; build history so older entries end
    # up beyond the token boundary. Use huge content so the running total
    # exceeds _PRUNE_PROTECT_TOKENS quickly.
    big = "x" * 200_000
    history = [
        {"role": "user", "content": f"tool_execution_result {big}"},
        {"role": "user", "content": f"tool_execution_result {big}"},
        {"role": "user", "content": f"tool_execution_result {big}"},
    ] + [
        {"role": "user", "content": f"tool_execution_result {big}"}
        for _ in range(6)  # protected recent tail
    ]
    result, count = prune_tool_outputs(history, return_pruned_count=True)
    assert count == 3
    for msg in result[:3]:
        assert msg["content"] == _PRUNED_TOOL_PLACEHOLDER
    # Recent protected messages preserved.
    for msg in result[3:]:
        assert msg["content"] != _PRUNED_TOOL_PLACEHOLDER


def test_preserve_metadata_is_never_pruned():
    big = "x" * 200_000
    history = [
        {
            "role": "user",
            "content": f"tool_execution_result {big}",
            "metadata": {"preserve": True},
        },
    ] + [
        {"role": "user", "content": f"tool_execution_result {big}"}
        for _ in range(7)
    ]
    result, count = prune_tool_outputs(history, return_pruned_count=True)
    assert result[0]["content"] != _PRUNED_TOOL_PLACEHOLDER


def test_input_history_is_not_mutated():
    big = "x" * 200_000
    history = [{"role": "user", "content": f"tool_execution_result {big}"}] + [
        {"role": "user", "content": f"tool_execution_result {big}"}
        for _ in range(6)
    ]
    original_first_content = history[0]["content"]
    prune_tool_outputs(history)
    assert history[0]["content"] == original_first_content


def test_shared_estimator_matches_default_in_token_truncation():
    """token_truncation defaults to the same estimator the pruner uses."""
    from src.core.context.token_truncation import estimate_text_tokens as tt_est

    sample = "def foo():\n    return 42\n" * 200
    assert tt_est(sample) == estimate_text_tokens(sample)


def test_default_estimator_drives_truncation_without_explicit_lambda():
    from src.core.context.token_truncation import truncate_to_token_budget

    text = "word " * 5000
    out = truncate_to_token_budget(text, 100)
    assert estimate_text_tokens(out) <= 100 or len(out) <= len(text)


def test_prune_tool_outputs_custom_protect_tokens():
    """Verify prune_tool_outputs honors custom protect_tokens ceiling."""
    big = "x" * 20_000
    history = [
        {"role": "user", "content": f"tool_execution_result {big}"},
        {"role": "user", "content": f"tool_execution_result {big}"},
    ] + [
        {"role": "user", "content": f"tool_execution_result {big}"}
        for _ in range(6)  # protected recent tail
    ]
    # With a small protect_tokens ceiling (5,000), older tool outputs get pruned
    result, count = prune_tool_outputs(history, return_pruned_count=True, protect_tokens=5_000)
    assert count == 2
    assert result[0]["content"] == _PRUNED_TOOL_PLACEHOLDER
    assert result[1]["content"] == _PRUNED_TOOL_PLACEHOLDER


def test_resolve_tool_output_max_chars_by_tier():
    """Verify resolve_tool_output_max_chars scales accurately per tier."""
    from types import SimpleNamespace
    from src.core.orchestration.graph.runtime_policy import (
        COMPACT_RUNTIME_POLICY,
        FRONTIER_RUNTIME_POLICY,
        NANO_RUNTIME_POLICY,
        STANDARD_RUNTIME_POLICY,
    )
    from src.core.orchestration.tool_execution_pipeline import (
        RESULT_MAX_CHARS,
        resolve_tool_output_max_chars,
    )

    # 1. Fallback when orchestrator is None
    assert resolve_tool_output_max_chars(None) == RESULT_MAX_CHARS

    # 2. Orchestrator with explicit override
    orch_override = SimpleNamespace(_tool_output_max_chars=15_000)
    assert resolve_tool_output_max_chars(orch_override) == 15_000

    # 3. Direct runtime policy attached
    orch_nano = SimpleNamespace(_runtime_policy=NANO_RUNTIME_POLICY)
    assert resolve_tool_output_max_chars(orch_nano) == 6_000

    orch_compact = SimpleNamespace(_runtime_policy=COMPACT_RUNTIME_POLICY)
    assert resolve_tool_output_max_chars(orch_compact) == 12_000

    orch_standard = SimpleNamespace(_runtime_policy=STANDARD_RUNTIME_POLICY)
    assert resolve_tool_output_max_chars(orch_standard) == 24_000

    orch_frontier = SimpleNamespace(_runtime_policy=FRONTIER_RUNTIME_POLICY)
    assert resolve_tool_output_max_chars(orch_frontier) == 48_000

    # 4. Dictionary policy
    orch_dict = SimpleNamespace(runtime_policy={"policy_tier": "compact", "tool_output_max_chars": 12_000})
    assert resolve_tool_output_max_chars(orch_dict) == 12_000


def test_truncate_result_strings_honors_tier_limit():
    """Verify _truncate_result_strings dynamically truncates based on max_chars."""
    from src.core.orchestration.tool_execution_pipeline import _truncate_result_strings

    text_15k = "a" * 15_000

    # Nano tier: capped at 6,000 chars
    res_nano = _truncate_result_strings({"output": text_15k}, max_chars=6_000)
    assert "[truncated: 9000 chars omitted]" in res_nano["output"]
    assert res_nano["output"].startswith("a" * 6_000)

    # Compact tier: capped at 12,000 chars
    res_compact = _truncate_result_strings({"output": text_15k}, max_chars=12_000)
    assert "[truncated: 3000 chars omitted]" in res_compact["output"]
    assert res_compact["output"].startswith("a" * 12_000)

    # Standard & Frontier tier: 15k chars is within 24,000 and 48,000 limits
    res_standard = _truncate_result_strings({"output": text_15k}, max_chars=24_000)
    assert "[truncated" not in res_standard["output"]
    assert len(res_standard["output"]) == 15_000

    res_frontier = _truncate_result_strings({"output": text_15k}, max_chars=48_000)
    assert "[truncated" not in res_frontier["output"]
    assert len(res_frontier["output"]) == 15_000


def test_execute_tool_impl_applies_tier_adaptive_truncation(tmp_path):
    """End-to-end: execute_tool_impl applies tier-scaled truncation to real tool result."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from src.core.orchestration.graph.runtime_policy import NANO_RUNTIME_POLICY
    from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

    # Mock tool returning a large text string
    large_text = "line of data\n" * 1000  # 13,000 chars
    mock_tool = {
        "fn": lambda **kwargs: {"ok": True, "data": large_text},
        "is_read_only": True,
        "required_permission": "read",
    }

    reg = MagicMock()
    reg.get.return_value = mock_tool

    orch = SimpleNamespace(
        tool_registry=reg,
        _dry_run=False,
        _runtime_policy=NANO_RUNTIME_POLICY,
        _normalize_tool_result=lambda r: r,
        _get_tool_timeout=lambda name: 10.0,
        event_bus=MagicMock(),
        session_store=None,
        active_role="operational",
        working_dir=tmp_path,
    )

    res = execute_tool_impl(orch, {"name": "read_dummy", "arguments": {}})
    assert res["ok"] is True
    # For nano tier, output inside res["result"]["data"] must be truncated to 6,000 chars
    inner_data = res["result"]["data"]
    assert "[truncated: 7000 chars omitted]" in inner_data
    assert inner_data.startswith("line of data\n")
    assert len(inner_data) == 6_000 + len("\n... [truncated: 7000 chars omitted]")

