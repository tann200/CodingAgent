"""tests/e2e/test_local_model_smoke.py — End-to-end validation for local compact models.

Validates the full 6-node LangGraph pipeline for 9–14B compact models
(Qwen3.5-9B, Qwen3-14B, Gemma 4 12B, Phi-4) and 2026 local runners (Ollama / LM Studio):
1. Multi-turn native structured tool execution (read_file -> edit_file_atomic -> completion).
2. Zero-tool informational queries (plain-text completion without loop traps).
3. Fault-tolerant JSON argument recovery (unescaped newlines & trailing commas).
4. Pre-write syntax verification & surgical recovery retry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
import pytest

from src.core.orchestration.orchestrator import Orchestrator


_CALL_MODEL_TARGETS = [
    "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
    "src.core.inference.llm_manager.call_model",
]


class ScriptedLocalModelAdapter:
    """Mock adapter simulating native tool calls and text from local small/compact models."""

    def __init__(self, turns: List[Dict[str, Any]], model_id: str = "qwen/qwen3.5-9b"):
        self.turns = list(turns)
        self.call_count = 0
        self.model_id = model_id
        self.history_records: List[List[Dict[str, Any]]] = []

    async def __call__(self, messages: List[Dict[str, Any]], model: str | None = None, **kwargs) -> Dict[str, Any]:
        first_content = messages[0].get("content", "") if messages else ""
        # Filter internal agent utility calls (session title generation, memory distillation)
        if "historian" in first_content or "title" in first_content or kwargs.get("max_tokens", 1000) <= 64:
            return {
                "ok": True,
                "provider": "lm_studio",
                "model": self.model_id,
                "content": "Internal utility response",
                "choices": [{"message": {"role": "assistant", "content": "Internal utility response"}}],
            }

        self.history_records.append(list(messages))
        if self.call_count < len(self.turns):
            resp = self.turns[self.call_count]
        else:
            resp = self.turns[-1]
        self.call_count += 1

        content = resp.get("content", "")
        tool_calls = resp.get("tool_calls", None)

        choice_message: Dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls is not None:
            choice_message["tool_calls"] = tool_calls

        return {
            "ok": True,
            "provider": "lm_studio",
            "model": self.model_id,
            "latency": 0.01,
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "total_tokens": 150,
            "content": content,
            "choices": [{"message": choice_message}],
        }


def _patch_scripted_model(monkeypatch: pytest.MonkeyPatch, adapter: ScriptedLocalModelAdapter) -> None:
    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, adapter)
        except AttributeError:
            pass

    import subprocess

    def _fake_run_subprocess(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        if "pytest" in cmd_str:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="1 passed in 0.01s\n", stderr=""
            )
        if "ruff" in cmd_str:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="All checks passed!\n", stderr=""
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.tools.verification_tools._run_subprocess", _fake_run_subprocess)


class TestLocalModelSmokeE2E:
    def test_local_compact_model_multiturn_atomic_edit_e2e(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Compact 9-14B model: read_file -> edit_file_atomic -> verified completion."""
        calc_file = tmp_path / "calc.py"
        calc_file.write_text("def calculate(a, b):\n    return a - b\n", encoding="utf-8")

        adapter = ScriptedLocalModelAdapter(
            turns=[
                # Turn 1: Model requests read_file
                {
                    "content": "I need to inspect calc.py first.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "calc.py"}),
                            },
                        }
                    ],
                },
                # Turn 2: Model issues atomic edit
                {
                    "content": "Now I will fix the subtraction to addition.",
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "edit_file_atomic",
                                "arguments": json.dumps({
                                    "path": "calc.py",
                                    "old_string": "return a - b",
                                    "new_string": "return a + b",
                                }),
                            },
                        }
                    ],
                },
                # Turn 3: Model summarizes and completes
                {
                    "content": "I have successfully fixed the calculate function in calc.py to perform addition.",
                },
            ],
            model_id="qwen/qwen3.5-9b",
        )
        _patch_scripted_model(monkeypatch, adapter)

        orch = Orchestrator(working_dir=str(tmp_path))
        orch._session_title = "Smoke Test Session"
        orch._model = "qwen/qwen3.5-9b"
        orch._provider = "lm_studio"

        result = orch.run_agent_once(
            system_prompt_name=None,
            messages=[{"role": "user", "content": "Fix the calculate function in calc.py so it adds a and b."}],
            tools={},
        )

        assert isinstance(result, dict)
        assert adapter.call_count == 3
        # Confirm file was edited on disk
        updated_code = calc_file.read_text(encoding="utf-8")
        assert "return a + b" in updated_code
        # Run finished successfully
        assert result.get("error") is None

    def test_local_informational_zero_tools_e2e(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Compact model answering a general question with 0 tool calls concludes in 1 turn."""
        adapter = ScriptedLocalModelAdapter(
            turns=[
                {
                    "content": "Binary search runs in O(log n) time by halving the search space each step.",
                }
            ],
            model_id="qwen/qwen3.5-9b",
        )
        _patch_scripted_model(monkeypatch, adapter)

        orch = Orchestrator(working_dir=str(tmp_path))
        orch._session_title = "Smoke Test Session"
        orch._model = "qwen/qwen3.5-9b"

        result = orch.run_agent_once(
            system_prompt_name=None,
            messages=[{"role": "user", "content": "What is the time complexity of binary search?"}],
            tools={},
        )

        assert isinstance(result, dict)
        assert adapter.call_count == 1
        assert "O(log n)" in str(result.get("assistant_message", ""))
        assert result.get("error") is None

    def test_local_compact_model_fault_tolerant_arguments_e2e(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Model emitting raw newlines and trailing comma in arguments succeeds via relaxed parser."""
        target_file = tmp_path / "greeting.py"

        # Simulate small local model emitting raw newlines and trailing comma inside a JSON string
        malformed_args = '{\n  "path": "greeting.py",\n  "content": "def hello():\\n    return 42",\n}'

        adapter = ScriptedLocalModelAdapter(
            turns=[
                {
                    "content": "Writing greeting.py with multiline content.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "write_file",
                                "arguments": malformed_args,
                            },
                        }
                    ],
                },
                {
                    "content": "greeting.py written successfully.",
                },
            ],
            model_id="qwen3:14b",
        )
        _patch_scripted_model(monkeypatch, adapter)

        orch = Orchestrator(working_dir=str(tmp_path))
        orch._session_title = "Smoke Test Session"
        orch._model = "qwen3:14b"

        result = orch.run_agent_once(
            system_prompt_name=None,
            messages=[{"role": "user", "content": "Write greeting.py with def hello."}],
            tools={},
        )

        assert isinstance(result, dict)
        assert target_file.read_text(encoding="utf-8") == "def hello():\n    return 42"
        assert adapter.call_count == 2
        assert result.get("error") is None

    def test_local_deferred_rollback_recovery_retry_e2e(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Syntax error on Turn 1 keeps edits safe; surgical fix on Turn 2 passes verification."""
        code_file = tmp_path / "logic.py"
        code_file.write_text("def solve():\n    return 1\n", encoding="utf-8")

        adapter = ScriptedLocalModelAdapter(
            turns=[
                # Turn 1: Read file
                {
                    "content": "Reading logic.py",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "logic.py"}),
                            },
                        }
                    ],
                },
                # Turn 2: Edit with deliberate syntax error (unclosed paren)
                {
                    "content": "Editing logic.py with broken syntax",
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "edit_file_atomic",
                                "arguments": json.dumps({
                                    "path": "logic.py",
                                    "old_string": "return 1",
                                    "new_string": "return (1 +",
                                }),
                            },
                        }
                    ],
                },
                # Turn 3: Model sees pre-write verification failure and applies surgical fix
                {
                    "content": "Fixing the unclosed parenthesis.",
                    "tool_calls": [
                        {
                            "id": "call_3",
                            "type": "function",
                            "function": {
                                "name": "edit_file_atomic",
                                "arguments": json.dumps({
                                    "path": "logic.py",
                                    "old_string": "return 1",
                                    "new_string": "return (1 + 1)",
                                }),
                            },
                        }
                    ],
                },
                # Turn 4: Clean completion -> verify passes -> finalize commits
                {
                    "content": "Syntax fixed and task complete.",
                },
            ],
            model_id="qwen/qwen3.5-9b",
        )
        _patch_scripted_model(monkeypatch, adapter)

        orch = Orchestrator(working_dir=str(tmp_path))
        orch._session_title = "Smoke Test Session"
        orch._model = "qwen/qwen3.5-9b"

        result = orch.run_agent_once(
            system_prompt_name=None,
            messages=[{"role": "user", "content": "Update logic.py to return 2."}],
            tools={},
        )

        assert isinstance(result, dict)
        final_code = code_file.read_text(encoding="utf-8")
        assert "return (1 + 1)" in final_code
        assert adapter.call_count == 4
        assert result.get("error") is None
