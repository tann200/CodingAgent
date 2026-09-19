"""Tests for distillation wiring in _prepare_next_round_state."""

from unittest.mock import patch, MagicMock


def _make_orch():
    orch = MagicMock()
    orch._session_read_files = set()
    orch.deterministic = False
    orch.seed = None
    return orch


def test_compaction_triggered_when_history_large():
    """When token estimate exceeds threshold, compact_messages_to_prose is called."""
    large_history = [{"role": "user", "content": "x" * 25000}]
    final_state = {"history": large_history, "working_dir": "/tmp", "errors": []}

    with patch(
        "src.core.memory.distiller.compact_messages_to_prose",
        return_value="[summary prose]",
    ) as mock_compact, patch(
        "src.core.memory.distiller._estimate_tokens",
        return_value=7000,  # above threshold
    ):
        from src.core.orchestration.inference_loop_rounds import _prepare_next_round_state

        result = _prepare_next_round_state(
            final_state=final_state,
            current_state={},
            orch=_make_orch(),
            cancel_event=None,
        )
        mock_compact.assert_called_once()
        assert any(
            "summary" in str(m.get("content", "")).lower() for m in result["history"]
        )


def test_compaction_not_triggered_when_history_small():
    """When token estimate is below threshold, compact_messages_to_prose is NOT called."""
    small_history = [{"role": "user", "content": "hello"}]
    final_state = {"history": small_history, "working_dir": "/tmp", "errors": []}

    with patch(
        "src.core.memory.distiller.compact_messages_to_prose"
    ) as mock_compact, patch(
        "src.core.memory.distiller._estimate_tokens",
        return_value=10,  # below threshold
    ):
        from src.core.orchestration.inference_loop_rounds import _prepare_next_round_state

        _prepare_next_round_state(
            final_state=final_state,
            current_state={},
            orch=_make_orch(),
            cancel_event=None,
        )
        mock_compact.assert_not_called()


def test_compaction_preserves_role_alternation():
    """WR-3: compaction must not collapse history into a single user blob.

    The compacted history must alternate roles: system summary → recent
    messages (assistant/user) → one user continuation turn, never two
    consecutive user turns.
    """
    from src.core.orchestration.inference_loop_rounds import _prepare_next_round_state

    large_history = [{"role": "user", "content": "x" * 25000}]
    final_state = {"history": large_history, "working_dir": "/tmp", "errors": []}

    with patch(
        "src.core.memory.distiller.compact_messages_to_prose",
        return_value="[summary prose]",
    ) as mock_compact, patch(
        "src.core.memory.distiller._estimate_tokens",
        return_value=7000,  # above threshold
    ):
        result = _prepare_next_round_state(
            final_state=final_state,
            current_state={},
            orch=_make_orch(),
            cancel_event=None,
        )
        mock_compact.assert_called_once()

    history = result["history"]
    assert history[0]["role"] == "system"
    assert "[COMPACTED]" in history[0]["content"]
    roles = [m["role"] for m in history]
    for i in range(1, len(roles)):
        assert roles[i] != roles[i - 1], f"back-to-back {roles[i]!r} roles: {roles}"
    assert roles[-1] == "user"


def test_compaction_drops_trailing_user_continuation_from_recent():
    """WR-3: the appended 'Continue...' user turn must not be duplicated."""
    from src.core.orchestration.inference_loop_rounds import _prepare_next_round_state

    # Ends with the assistant turn so _prepare_next_round_state will have
    # appended the continuation user turn before compaction runs.
    large_history = [
        {"role": "user", "content": "task" * 12000},
        {"role": "assistant", "content": '{"tool": "grep", "args": {}}'},
    ]
    final_state = {"history": large_history, "working_dir": "/tmp", "errors": []}

    with patch(
        "src.core.memory.distiller.compact_messages_to_prose",
        return_value="[summary prose]",
    ), patch(
        "src.core.memory.distiller._estimate_tokens",
        return_value=7000,  # above threshold
    ):
        result = _prepare_next_round_state(
            final_state=final_state,
            current_state={},
            orch=_make_orch(),
            cancel_event=None,
        )

    roles = [m["role"] for m in result["history"]]
    for i in range(1, len(roles)):
        assert roles[i] != roles[i - 1], f"back-to-back {roles[i]!r} roles: {roles}"
    assert roles[0] == "system"
    assert roles[-1] == "user"
