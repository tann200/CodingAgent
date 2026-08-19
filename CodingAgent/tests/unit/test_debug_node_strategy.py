"""Tests for debug node strategy escalation (P3-T2)."""

from src.core.orchestration.graph.nodes.debug_node import _STRATEGY_ESCALATION


def test_strategy_escalates_across_attempts():
    s0 = _STRATEGY_ESCALATION[0]("syntax_error", "SyntaxError")
    s1 = _STRATEGY_ESCALATION[1]("syntax_error", "SyntaxError")
    s2 = _STRATEGY_ESCALATION[2]("syntax_error", "SyntaxError")
    # Each strategy should be distinct
    assert s0 != s1
    assert s1 != s2
    # Attempt 2 should mention broader analysis
    assert "root cause" in s2.lower() or "bash" in s2.lower()


def test_strategy_0_uses_type_guidance():
    s0 = _STRATEGY_ESCALATION[0]("syntax_error", "SyntaxError")
    # Should embed the TYPE_GUIDANCE content
    assert "syntax" in s0.lower() or "guidance" in s0.lower()


def test_strategy_1_mentions_read_file():
    s1 = _STRATEGY_ESCALATION[1]("runtime_error", "AttributeError")
    assert "read_file" in s1 or "read" in s1.lower()


def test_strategy_list_has_three_entries():
    assert len(_STRATEGY_ESCALATION) == 3
