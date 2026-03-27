"""
Unit tests for token_budget.py - Phase 4: Token Auto-Compact
"""

from src.core.orchestration.token_budget import (
    TokenBudget,
    TokenBudgetMonitor,
    get_token_budget_monitor,
)


class TestTokenBudget:
    def test_initialization(self):
        budget = TokenBudget(used_tokens=1000, max_tokens=6000)
        assert budget.used_tokens == 1000
        assert budget.max_tokens == 6000
        assert budget.warning_threshold == 0.70
        assert budget.compact_threshold == 0.85

    def test_usage_ratio(self):
        budget = TokenBudget(used_tokens=3000, max_tokens=6000)
        assert budget.usage_ratio == 0.5

    def test_should_warn_at_threshold(self):
        budget = TokenBudget(used_tokens=4200, max_tokens=6000)
        assert budget.should_warn is True

    def test_should_not_warn_below_threshold(self):
        budget = TokenBudget(used_tokens=3000, max_tokens=6000)
        assert budget.should_warn is False

    def test_should_compact_at_threshold_with_enough_turns(self):
        budget = TokenBudget(
            used_tokens=5200, max_tokens=6000, current_turn=10, last_compact_turn=0
        )
        assert budget.should_compact is True

    def test_should_not_compact_without_enough_turns(self):
        budget = TokenBudget(
            used_tokens=5200, max_tokens=6000, current_turn=3, last_compact_turn=0
        )
        assert budget.should_compact is False

    def test_record_compaction(self):
        budget = TokenBudget(
            used_tokens=5200, max_tokens=6000, current_turn=10, last_compact_turn=0
        )
        budget.record_compaction()
        assert budget.last_compact_turn == 10


class TestTokenBudgetMonitor:
    def test_singleton(self):
        monitor1 = get_token_budget_monitor()
        monitor2 = get_token_budget_monitor()
        assert monitor1 is monitor2

    def test_update_budget(self):
        monitor = TokenBudgetMonitor()
        monitor.update("session1", used_tokens=1000, max_tokens=6000, turn=5)

        budget = monitor.get_budget("session1")
        assert budget.used_tokens == 1000
        assert budget.max_tokens == 6000
        assert budget.current_turn == 5

    def test_check_and_prepare_compaction(self):
        monitor = TokenBudgetMonitor()
        monitor.update("session1", used_tokens=5500, max_tokens=6000, turn=10)

        result = monitor.check_and_prepare_compaction("session1")
        assert result is True

    def test_check_and_prepare_compaction_no_compact(self):
        monitor = TokenBudgetMonitor()
        monitor.update("session1", used_tokens=1000, max_tokens=6000, turn=10)

        result = monitor.check_and_prepare_compaction("session1")
        assert result is False

    def test_get_status_message(self):
        monitor = TokenBudgetMonitor()
        monitor.update("session1", used_tokens=1000, max_tokens=6000)

        status = monitor.get_status_message("session1")
        assert "Token budget" in status

    def test_check_budget_with_state(self):
        monitor = TokenBudgetMonitor()

        state = {
            "history": [
                {"role": "user", "content": "test message " * 100},
                {"role": "assistant", "content": "response " * 100},
            ],
            "session_id": "test_session",
            "rounds": 5,
            "_p2p_context": [],
        }

        result = monitor.check_budget(state)

        assert result in ["ok", "compact"]


class TestTokenEstimation:
    """Regression: _estimate_tokens must return token count (~chars/4), not char count."""

    def test_estimate_tokens_not_character_count(self):
        """400-char message should estimate ~100 tokens, not 400."""
        monitor = TokenBudgetMonitor()
        msg = {"role": "user", "content": "x" * 400}
        tokens = monitor._estimate_tokens(msg)
        # Character count would be ~404; token estimate should be ~101
        assert tokens < 200, (
            f"_estimate_tokens returned {tokens} — looks like a char count, not tokens"
        )
        assert tokens >= 1

    def test_estimate_tokens_nonempty_message(self):
        """Non-empty messages must return at least 1 token."""
        monitor = TokenBudgetMonitor()
        msg = {"role": "user", "content": "hi"}
        assert monitor._estimate_tokens(msg) >= 1

    def test_estimate_tokens_large_message_lower_than_chars(self):
        """Token estimate for 1000-char content must be < 1000."""
        monitor = TokenBudgetMonitor()
        msg = {"role": "assistant", "content": "a" * 1000}
        tokens = monitor._estimate_tokens(msg)
        char_count = len("assistant") + 1000
        assert tokens < char_count, (
            "Token estimate should be less than raw character count"
        )


# ---------------------------------------------------------------------------
# Regression: check_budget uses _context_budget from provider
# (Previously the <= 6000 threshold never fired since default is 32_768)
# ---------------------------------------------------------------------------
class TestTokenBudgetContextBudget:
    def _make_state(self, context_budget=None, session_id="test-sess", rounds=1):
        state = {
            "history": [{"role": "user", "content": "hello"}],
            "rounds": rounds,
            "session_id": session_id,
        }
        if context_budget is not None:
            state["_context_budget"] = context_budget
        return state

    def test_context_budget_used_when_present(self):
        """_context_budget in state must set max_tokens on the budget."""
        monitor = TokenBudgetMonitor()
        state = self._make_state(context_budget="8192", session_id="cb-sess1")
        monitor.check_budget(state)
        budget = monitor.get_budget("cb-sess1")
        assert budget.max_tokens == 8192, (
            f"Expected max_tokens=8192 from _context_budget, got {budget.max_tokens}. "
            "The <= 6000 threshold bug prevented _context_budget from being used."
        )

    def test_existing_value_preserved_when_no_context_budget(self):
        """Without _context_budget, the cached max_tokens must be preserved."""
        monitor = TokenBudgetMonitor()
        monitor.update("cb-sess2", used_tokens=1000, max_tokens=16384, turn=0)
        state = self._make_state(session_id="cb-sess2")  # no _context_budget
        monitor.check_budget(state)
        budget = monitor.get_budget("cb-sess2")
        assert budget.max_tokens == 16384, (
            f"Cached max_tokens should be preserved when _context_budget absent, got {budget.max_tokens}"
        )

    def test_zero_context_budget_ignored(self):
        """_context_budget=0 must not overwrite a valid cached value."""
        monitor = TokenBudgetMonitor()
        monitor.update("cb-sess3", used_tokens=500, max_tokens=4096, turn=0)
        state = self._make_state(context_budget=0, session_id="cb-sess3")
        monitor.check_budget(state)
        budget = monitor.get_budget("cb-sess3")
        assert budget.max_tokens == 4096
