"""
P3-3: Basic E2E workflow and behaviour tests using a mock LLM.

These tests exercise the core agent pipeline without a live provider.
"""

# ruff: noqa: E501
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workdir() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / ".codingAgent").mkdir()
    (d / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n"
    )
    return d


def _make_mock_orchestrator(workdir: Path) -> Any:
    orch = MagicMock()
    orch.working_dir = str(workdir)
    orch.session_store = MagicMock()
    orch.session_store.add_plan = MagicMock()
    orch.get_provider_capabilities = MagicMock(return_value={})
    orch.cancel_event = None
    return orch


def _plan_response(steps):
    payload = {
        "root_task": "task",
        "steps": [
            {
                "step_id": f"step_{i}",
                "description": s,
                "files": ["app.py"],
                "depends_on": [f"step_{i - 1}"] if i > 0 else [],
            }
            for i, s in enumerate(steps)
        ],
    }
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}





# ---------------------------------------------------------------------------
# Agent behaviour tests
# ---------------------------------------------------------------------------


class TestAgentBehaviorE2E:
    """End-to-end agent behavior tests."""

    def test_agent_read_before_edit(self):
        """execute_tool enforces read-before-write at the orchestrator layer."""
        from src.core.orchestration.orchestrator import Orchestrator

        workdir = _make_workdir()
        orch = Orchestrator(working_dir=str(workdir))

        # Edit without prior read must be rejected
        result = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "app.py",
                    "old_content": "def add",
                    "new_content": "def add2",
                },
            }
        )
        assert result.get("ok") is False
        assert "read" in result.get("error", "").lower()

        # After registering a read the block is lifted
        orch._session_read_files.add(str((workdir / "app.py").resolve()))
        result2 = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "app.py",
                    "old_content": "def add(a, b):\n    return a + b\n",
                    "new_content": "def add(a, b):\n    \"\"\"Return a+b.\"\"\"\n    return a + b\n",
                },
            }
        )
        assert "must read" not in result2.get("error", "")


