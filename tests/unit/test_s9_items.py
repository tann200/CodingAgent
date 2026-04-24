"""tests/unit/test_s9_items.py — Tests for S9-A (cross-session memory injection)
and S9-B (compact_context on Orchestrator).
"""

# ruff: noqa: E501
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# S9-A — inject_prior_session_memories
# ---------------------------------------------------------------------------


class TestInjectPriorSessionMemories:
    def _make_builder(self, tmp_path):
        from src.core.context.context_builder import ContextBuilder

        return ContextBuilder(working_dir=str(tmp_path))

    def test_returns_empty_when_vector_store_raises_on_import(self, tmp_path):
        """VectorStore import failure → empty string, no exception."""
        builder = self._make_builder(tmp_path)
        # Patch the class at the source module so the local import inside the method gets it
        with patch(
            "src.core.indexing.vector_store.VectorStore",
            side_effect=RuntimeError("no vectorstore"),
        ):
            result = builder.inject_prior_session_memories("write a hello world file")
        assert result == ""

    def test_returns_empty_when_no_memories_found(self, tmp_path):
        """VectorStore present but search returns []."""
        builder = self._make_builder(tmp_path)
        mock_vs = MagicMock()
        mock_vs.search_memories.return_value = []
        with patch("src.core.indexing.vector_store.VectorStore", return_value=mock_vs):
            result = builder.inject_prior_session_memories("fix the login bug")
        assert result == ""

    def test_returns_prior_context_block_with_results(self, tmp_path):
        """VectorStore returns results → well-formed <prior_context> block."""
        builder = self._make_builder(tmp_path)
        mock_vs = MagicMock()
        mock_vs.search_memories.return_value = [
            {"text": "Fixed auth bug in session store"},
            {"text": "Added user model with email field"},
        ]
        with patch("src.core.indexing.vector_store.VectorStore", return_value=mock_vs):
            result = builder.inject_prior_session_memories("add user registration")
        assert "<prior_context>" in result
        assert "</prior_context>" in result
        assert "Fixed auth bug" in result
        assert "Added user model" in result

    def test_truncates_long_memory_entries(self, tmp_path):
        """Memory text longer than 250 chars is truncated."""
        builder = self._make_builder(tmp_path)
        long_text = "x" * 400
        mock_vs = MagicMock()
        mock_vs.search_memories.return_value = [{"text": long_text}]
        with patch("src.core.indexing.vector_store.VectorStore", return_value=mock_vs):
            result = builder.inject_prior_session_memories("task")
        # Should be truncated — the full 400-char string should not appear
        assert "x" * 251 not in result

    def test_passes_task_as_query(self, tmp_path):
        """The task string is used as the VectorStore search query."""
        builder = self._make_builder(tmp_path)
        mock_vs = MagicMock()
        mock_vs.search_memories.return_value = []
        with patch("src.core.indexing.vector_store.VectorStore", return_value=mock_vs):
            builder.inject_prior_session_memories("implement checkout flow", limit=2)
        mock_vs.search_memories.assert_called_once_with(
            query="implement checkout flow", limit=2
        )

    def test_handles_search_exception_gracefully(self, tmp_path):
        """Exception inside search_memories → empty string, no crash."""
        builder = self._make_builder(tmp_path)
        mock_vs = MagicMock()
        mock_vs.search_memories.side_effect = RuntimeError("DB locked")
        with patch("src.core.indexing.vector_store.VectorStore", return_value=mock_vs):
            result = builder.inject_prior_session_memories("some task")
        assert result == ""

    def test_memory_with_content_key(self, tmp_path):
        """Result dicts with 'content' key (not 'text') are handled."""
        builder = self._make_builder(tmp_path)
        mock_vs = MagicMock()
        mock_vs.search_memories.return_value = [
            {"content": "Session summary: refactored auth module"}
        ]
        with patch("src.core.indexing.vector_store.VectorStore", return_value=mock_vs):
            result = builder.inject_prior_session_memories("refactor auth")
        assert "refactored auth module" in result


# ---------------------------------------------------------------------------
# S9-A — perception_node wiring (round 0 only)
# ---------------------------------------------------------------------------


class TestPerceptionNodePriorContext:
    """Verify inject_prior_session_memories is callable and returns the right type."""

    def test_inject_returns_string(self, tmp_path):
        from src.core.context.context_builder import ContextBuilder

        builder = ContextBuilder(working_dir=str(tmp_path))
        mock_vs = MagicMock()
        mock_vs.search_memories.return_value = []
        with patch("src.core.indexing.vector_store.VectorStore", return_value=mock_vs):
            result = builder.inject_prior_session_memories("hello task")
        assert isinstance(result, str)

    def test_inject_is_method_on_context_builder(self):
        from src.core.context.context_builder import ContextBuilder

        assert callable(getattr(ContextBuilder, "inject_prior_session_memories", None))


# ---------------------------------------------------------------------------
# S9-B — compact_context on Orchestrator
# ---------------------------------------------------------------------------


class TestOrchestratorCompactContext:
    def test_compact_context_method_exists_on_orchestrator_class(self):
        from src.core.orchestration.orchestrator import Orchestrator

        assert callable(getattr(Orchestrator, "compact_context", None))

    def test_compact_returns_false_when_no_history(self):
        """Empty history → compact_context returns False without calling distill."""
        from src.core.orchestration.orchestrator import Orchestrator

        mock_self = MagicMock()
        mock_self.msg_mgr.messages = []
        mock_self.working_dir = None
        mock_self.event_bus = None

        result = Orchestrator.compact_context(mock_self)
        assert result is False

    def test_compact_calls_distill_and_returns_true(self):
        """Non-empty history → distill_context is called, returns True."""
        from src.core.orchestration.orchestrator import Orchestrator
        import src.core.memory.distiller as distiller_mod

        mock_self = MagicMock()
        mock_self.msg_mgr.messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        mock_self.working_dir = None
        mock_self.event_bus = None

        captured = []
        original = distiller_mod.distill_context

        def _fake_distill(messages, working_dir=None):
            captured.append(list(messages))
            return {}

        distiller_mod.distill_context = _fake_distill
        try:
            result = Orchestrator.compact_context(mock_self)
        finally:
            distiller_mod.distill_context = original

        assert result is True
        assert len(captured) == 1
        assert len(captured[0]) == 2

    def test_compact_returns_false_on_exception(self):
        """Exception in distill_context → compact_context returns False, no crash."""
        from src.core.orchestration.orchestrator import Orchestrator
        import src.core.memory.distiller as distiller_mod

        mock_self = MagicMock()
        mock_self.msg_mgr.messages = [{"role": "user", "content": "hi"}]
        mock_self.working_dir = None
        mock_self.event_bus = None

        original = distiller_mod.distill_context
        distiller_mod.distill_context = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("distill failed")
        )
        try:
            result = Orchestrator.compact_context(mock_self)
        finally:
            distiller_mod.distill_context = original

        assert result is False

    def test_compact_publishes_event_when_event_bus_present(self):
        """compact_context publishes context.compacted when event_bus is available."""
        from src.core.orchestration.orchestrator import Orchestrator
        import src.core.memory.distiller as distiller_mod

        mock_self = MagicMock()
        mock_self.msg_mgr.messages = [{"role": "user", "content": "hi"}]
        mock_self.working_dir = None
        mock_bus = MagicMock()
        mock_self.event_bus = mock_bus

        original = distiller_mod.distill_context
        distiller_mod.distill_context = lambda *a, **kw: {}
        try:
            result = Orchestrator.compact_context(mock_self)
        finally:
            distiller_mod.distill_context = original

        assert result is True
        mock_bus.publish.assert_called_once()
        event_name = mock_bus.publish.call_args[0][0]
        assert event_name == "context.compacted"

    def test_compact_skips_event_bus_when_none(self):
        """No event_bus → no exception, still returns True."""
        from src.core.orchestration.orchestrator import Orchestrator
        import src.core.memory.distiller as distiller_mod

        mock_self = MagicMock()
        mock_self.msg_mgr.messages = [{"role": "user", "content": "hi"}]
        mock_self.working_dir = None
        mock_self.event_bus = None

        original = distiller_mod.distill_context
        distiller_mod.distill_context = lambda *a, **kw: {}
        try:
            result = Orchestrator.compact_context(mock_self)
        finally:
            distiller_mod.distill_context = original

        assert result is True
