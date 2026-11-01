"""
P3-3: E2E scenario tests using a mock LLM (no live provider required).

These tests exercise the full LangGraph pipeline:
  perception → analysis → planning → execution → verification

The LLM is mocked so tests run in CI without a running local provider.
"""

# ruff: noqa: E501
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_orchestrator(workdir: Path) -> Any:
    """Instantiate Orchestrator pointing at an isolated temp working directory."""
    from src.core.orchestration.orchestrator import Orchestrator

    orch = Orchestrator(working_dir=str(workdir))
    return orch


def _mock_call_model(response_text: str):
    """Return an async mock yielding an OpenAI-shaped choices dict with response_text."""

    async def _call(*args, **kwargs):
        return {"choices": [{"message": {"content": response_text}}]}

    return _call


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workdir(tmp_path):
    """Isolated working directory with a minimal Python file."""
    (tmp_path / "hello.py").write_text("def greet(name):\n    return f'Hello, {name}'\n")
    (tmp_path / ".codingAgent").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Scenario 1: Read-before-write enforcement
# ---------------------------------------------------------------------------


class TestReadBeforeWriteE2E:
    """Agent must read a file before writing it — enforced at orchestrator level."""

    def test_write_blocked_without_read(self, workdir):
        """execute_tool rejects edit_file when file not yet read."""
        orch = _make_orchestrator(workdir)

        result = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "hello.py",
                    "old_content": "def greet",
                    "new_content": "def greet2",
                },
            }
        )
        assert result.get("ok") is False
        assert "read" in result.get("error", "").lower()

    def test_write_allowed_after_read(self, workdir):
        """execute_tool allows edit_file after read_file registers the path."""
        orch = _make_orchestrator(workdir)

        # Register the read
        orch._session_read_files.add(str((workdir / "hello.py").resolve()))

        result = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "hello.py",
                    "old_content": "def greet(name):\n    return f'Hello, {name}'\n",
                    "new_content": "def greet(name):\n    return f'Hi, {name}'\n",
                },
            }
        )
        # Should not be a read-violation error
        error = result.get("error", "")
        assert "must read" not in error, f"Unexpected read-before-write block: {error}"





# ---------------------------------------------------------------------------
# Scenario 4: Distiller persists to VectorStore (P3-7)
# ---------------------------------------------------------------------------


class TestDistillerVectorStoreE2E:
    """distill_context must call VectorStore.add_memory after successful distillation."""

    def test_distill_context_calls_vector_store(self, workdir):
        """After distillation, add_memory must be called with the summary text."""
        from src.core.memory.distiller import distill_context

        messages = [
            {"role": "user", "content": "add a docstring to greet()"},
            {"role": "assistant", "content": "I'll read hello.py first."},
            {"role": "user", "content": '{"tool_execution_result": {"ok": true, "content": "def greet..."}}'},
            {"role": "assistant", "content": "Now I'll add the docstring."},
        ]

        distilled_result = {
            "current_task": "add docstring",
            "current_state": "reading file",
            "next_step": "write docstring",
        }

        add_memory_calls = []

        class FakeVectorStore:
            def __init__(self, **kwargs):
                pass

            def add_memory(self, text, metadata=None):
                add_memory_calls.append((text, metadata))

        with (
            patch("src.core.memory.distiller._call_llm_sync", return_value=json.dumps(distilled_result)),
            patch("src.core.indexing.vector_store.VectorStore", FakeVectorStore),
        ):
            result = distill_context(messages, working_dir=workdir)

        assert result == distilled_result
        assert len(add_memory_calls) == 1, "add_memory must be called exactly once"
        text, metadata = add_memory_calls[0]
        assert "add docstring" in text
        assert metadata == distilled_result
