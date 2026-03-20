"""
Tests for memory system: distiller, session_store, memory_tools.
"""


class TestDistiller:
    """Tests for memory distiller."""

    def test_distiller_import(self):
        """Test distiller can be imported."""
        from src.core.memory import distiller

        assert distiller is not None

    def test_extract_task_summary_exists(self):
        """Test distill_context function exists (previously extract_task_summary)."""
        from src.core.memory.distiller import distill_context

        assert distill_context is not None

    def test_distill_context_exists(self):
        """Test distill_context function exists."""
        from src.core.memory.distiller import distill_context

        assert distill_context is not None


class TestDistillerFunctions:
    """Tests for distiller functions."""

    def test_extract_task_summary_basic(self, tmp_path):
        """Test basic task summary extraction using distill_context."""
        from unittest.mock import patch
        from src.core.memory.distiller import distill_context

        messages = [
            {"role": "user", "content": "Create a function"},
            {"role": "assistant", "content": "I'll create that function"},
            {"role": "tool", "content": "Function created"},
        ]

        # Patch the LLM call so the test doesn't require a real model
        with patch("src.core.memory.distiller._call_llm_sync", return_value=None):
            summary = distill_context(messages, working_dir=str(tmp_path))
            assert summary is None or isinstance(summary, dict)

    def test_distill_context_handles_empty(self, tmp_path):
        """Test distill_context with empty messages returns None without error."""
        from src.core.memory.distiller import distill_context

        # Empty messages should return None quickly without needing LLM
        result = distill_context([], working_dir=str(tmp_path))
        assert result is None or isinstance(result, dict)


class TestSessionStore:
    """Tests for session store."""

    def test_session_store_import(self):
        """Test session_store can be imported."""
        from src.core.memory import session_store

        assert session_store is not None


class TestMemoryTools:
    """Tests for memory tools."""

    def test_memory_tools_import(self):
        """Test memory_tools can be imported."""
        from src.core.memory import memory_tools

        assert memory_tools is not None

    def test_search_memory_exists(self):
        """Test memory_search function exists."""
        from src.core.memory.memory_tools import memory_search

        assert memory_search is not None

    def test_memory_search_returns_status_ok(self, tmp_path):
        """T3: memory_search success path must include status='ok'."""
        from src.core.memory.memory_tools import memory_search

        result = memory_search("anything", str(tmp_path))
        assert result.get("status") == "ok"
        assert "results" in result
        assert "query" in result

    def test_memory_search_with_task_state(self, tmp_path):
        """memory_search finds lines in TASK_STATE.md."""
        from src.core.memory.memory_tools import memory_search

        ctx_dir = tmp_path / ".agent-context"
        ctx_dir.mkdir()
        (ctx_dir / "TASK_STATE.md").write_text("# Task\nfix the bug\nadd tests\n")

        result = memory_search("fix", str(tmp_path))
        assert result["status"] == "ok"
        assert any(r["source"] == "TASK_STATE.md" for r in result["results"])

    def test_memory_search_error_returns_status_error(self, tmp_path):
        """memory_search on broken JSON returns status='error'."""
        from src.core.memory.memory_tools import memory_search
        from unittest.mock import patch

        ctx_dir = tmp_path / ".agent-context"
        ctx_dir.mkdir()
        bad_json = ctx_dir / "execution_trace.json"
        bad_json.write_text("NOT JSON")

        result = memory_search("anything", str(tmp_path))
        assert result.get("status") == "error"
        assert "error" in result


class TestAdvancedFeatures:
    """Tests for advanced memory features."""

    def test_advanced_features_import(self):
        """Test advanced_features can be imported."""
        from src.core.memory import advanced_features

        assert advanced_features is not None

    def test_trajectory_logger_exists(self):
        """Test TrajectoryLogger class exists."""
        from src.core.memory.advanced_features import TrajectoryLogger

        assert TrajectoryLogger is not None

    def test_dream_consolidator_exists(self):
        """Test DreamConsolidator class exists."""
        from src.core.memory.advanced_features import DreamConsolidator

        assert DreamConsolidator is not None

    def test_refactoring_agent_exists(self):
        """Test RefactoringAgent class exists."""
        from src.core.memory.advanced_features import RefactoringAgent

        assert RefactoringAgent is not None

    def test_review_agent_exists(self):
        """Test ReviewAgent class exists."""
        from src.core.memory.advanced_features import ReviewAgent

        assert ReviewAgent is not None

    def test_skill_learner_exists(self):
        """Test SkillLearner class exists."""
        from src.core.memory.advanced_features import SkillLearner

        assert SkillLearner is not None


class TestMemoryIntegration:
    """Integration tests for memory system."""

    def test_memory_tools_workflow(self, tmp_path):
        """Test memory tools can work together."""
        from src.core.memory import distiller, session_store, memory_tools

        # Basic sanity check - all should be importable
        assert distiller is not None
        assert session_store is not None
        assert memory_tools is not None
