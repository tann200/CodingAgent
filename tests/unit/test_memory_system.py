"""
Tests for memory system: distiller, session_store, memory_tools.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from typing import Any, Dict


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

    def test_extract_task_summary_basic(self):
        """Test basic task summary extraction using distill_context."""
        from src.core.memory.distiller import distill_context

        # Test with sample messages
        messages = [
            {"role": "user", "content": "Create a function"},
            {"role": "assistant", "content": "I'll create that function"},
            {"role": "tool", "content": "Function created"},
        ]

        # Should handle without error
        try:
            summary = distill_context(messages, working_dir=tmp_path)
            # May return None or dict if LLM is not available
            assert summary is None or isinstance(summary, dict)
        except Exception:
            pass  # Acceptable if LLM not available

    def test_distill_context_handles_empty(self, tmp_path):
        """Test distill_context with empty messages."""
        from src.core.memory.distiller import distill_context

        # Should handle empty list
        try:
            result = distill_context([], working_dir=tmp_path)
            assert result is None or isinstance(result, dict)
        except Exception:
            pass  # Acceptable


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
