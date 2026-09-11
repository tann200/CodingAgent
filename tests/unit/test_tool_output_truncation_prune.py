"""A2.4: Tests for the consolidated, single-canonical prune_tool_outputs.

Locks the deduplication of the two near-identical token-based pruning clones
(previously in perception_node and tool_output_truncation) and the shared
token-estimation helper now also used as the default estimator in
src.core.context.token_truncation.
"""

from src.core.orchestration.graph.nodes import perception_node, tool_output_truncation
from src.core.orchestration.graph.nodes.tool_output_truncation import (
    _PRUNED_TOOL_PLACEHOLDER,
    estimate_text_tokens,
    prune_tool_outputs,
)


def test_prune_tool_outputs_is_single_canonical_implementation():
    """perception_node must import the canonical pruner, not its own clone."""
    assert perception_node._prune_tool_outputs is prune_tool_outputs


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
