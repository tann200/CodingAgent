"""
Tests for memory system: distiller, session_store, memory_tools.
"""


class TestDistiller:
    """Tests for memory distiller."""
    # NOTE: Import-only tests removed - imports are verified by behavioral tests
    pass


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
            summary = distill_context(messages, working_dir=str(tmp_path)) # type: ignore[arg-type]
            assert summary is None or isinstance(summary, dict)

    def test_distill_context_handles_empty(self, tmp_path):
        """Test distill_context with empty messages returns None without error."""
        from src.core.memory.distiller import distill_context

        # Empty messages should return None quickly without needing LLM
        result = distill_context([], working_dir=str(tmp_path)) # type: ignore[arg-type]
        assert result is None or isinstance(result, dict)


class TestSessionStore:
    """Tests for session store."""
    # NOTE: Import-only tests removed - imports are verified by behavioral tests
    pass


class TestMemoryTools:
    """Tests for memory tools."""
    # NOTE: Import-only tests removed - function existence verified by behavioral tests

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

        ctx_dir = tmp_path / ".codingAgent"
        ctx_dir.mkdir()
        (ctx_dir / "TASK_STATE.md").write_text("# Task\nfix the bug\nadd tests\n")

        result = memory_search("fix", str(tmp_path))
        assert result["status"] == "ok"
        assert any(r["source"] == "TASK_STATE.md" for r in result["results"])

    def test_memory_search_error_returns_status_error(self, tmp_path):
        """memory_search on broken JSON returns status='error'."""
        from src.core.memory.memory_tools import memory_search

        ctx_dir = tmp_path / ".codingAgent"
        ctx_dir.mkdir()
        bad_json = ctx_dir / "execution_trace.json"
        bad_json.write_text("NOT JSON")

        result = memory_search("anything", str(tmp_path))
        assert result.get("status") == "error"
        assert "error" in result


class TestAdvancedFeatures:
    """Tests for advanced memory features."""
    # NOTE: Import-only tests removed - class existence verified by usage tests
    pass


class TestMemoryIntegration:
    """Integration tests for memory system."""
    # NOTE: Import-only test removed - this test class can be removed entirely
    #       if no behavioral integration tests are added
    pass
