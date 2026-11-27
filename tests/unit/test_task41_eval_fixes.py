"""Regression tests for bugs surfaced during TASK-41 live Ollama/Gemini evals."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.core.inference.adapters.ollama_adapter import OllamaAdapter
from src.core.orchestration.graph.completion_contract import (
    TaskType,
    classify_task,
    extract_requested_new_files,
)


def test_ollama_non_streaming_extracts_tokens_from_meta():
    """OllamaAdapter.generate() extracts prompt_eval_count/eval_count from raw_response['meta']."""
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434/api", models=["qwen3.8:9b"])
    adapter._chat_internal = MagicMock(
        return_value={
            "model": "qwen3.8:9b",
            "message": {"role": "assistant", "content": "OK"},
            "response": "OK",
            "meta": {
                "prompt_eval_count": 1420,
                "eval_count": 38,
                "done": True,
            },
        }
    )

    res = adapter.generate([{"role": "user", "content": "Say OK"}], model="qwen3.8:9b", stream=False)
    assert res["ok"] is True
    assert res["prompt_tokens"] == 1420
    assert res["completion_tokens"] == 38
    assert res["total_tokens"] == 1458
    assert res["usage"] == {
        "prompt_tokens": 1420,
        "completion_tokens": 38,
        "total_tokens": 1458,
    }


def test_completion_contract_classifies_gerund_delegated_writes_as_mutating():
    """Gerund prompts like 'Delegate ... writing a README.md' must classify as MUTATING."""
    t1 = "Delegate to a subagent the task of writing a README.md for this project."
    t2 = "We have a module math_ops.py. Delegate to a subagent the task of writing comprehensive unit tests in test_math_ops.py."
    assert classify_task(t1) == TaskType.MUTATING
    assert classify_task(t2) == TaskType.MUTATING
    assert "README.md" in extract_requested_new_files(t1)
    assert "test_math_ops.py" in extract_requested_new_files(t2)


def test_extract_requested_new_files_does_not_false_positive_on_fix_in_file():
    """'Fix the bug in foo.py' must not treat existing target foo.py as a new-file creation."""
    assert extract_requested_new_files("Fix the off-by-one bug in list_utils.py") == []


def test_frontier_loop_nudges_on_missing_named_deliverable():
    """frontier_loop_node includes missing_named_deliverable in its in-loop nudge set."""
    from pathlib import Path

    fln_src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "core"
        / "orchestration"
        / "graph"
        / "nodes"
        / "frontier_loop_node.py"
    ).read_text(encoding="utf-8")
    assert '"missing_named_deliverable"' in fln_src
