from unittest.mock import patch
from src.core.memory.distiller import distill_context
import json


def test_distill_context_success(tmp_path):
    expected_output = {
        "current_task": "Write test cases for MVP",
        "completed_steps": ["Read MVP roadmap", "Created ContextBuilder tests"],
        "next_step": "Create distiller tests",
        "current_state": "in progress",
        "files_modified": [],
        "errors_resolved": [],
    }

    (tmp_path / ".agent-context").mkdir(parents=True, exist_ok=True)

    with patch(
        "src.core.memory.distiller._call_llm_sync",
        return_value=json.dumps(expected_output),
    ):
        result = distill_context(
            [{"role": "user", "content": "Let's do this."}],
            working_dir=tmp_path,
        )

    assert result["current_task"] == "Write test cases for MVP"

    task_state_file = tmp_path / ".agent-context" / "TASK_STATE.md"
    assert task_state_file.exists()
    content = task_state_file.read_text()
    assert "Write test cases for MVP" in content
    assert "- Read MVP roadmap" in content
    assert "Create distiller tests" in content


def test_distill_context_empty_messages(tmp_path):
    result = distill_context([], working_dir=tmp_path)
    assert result == {}


def test_distill_context_llm_failure(tmp_path):
    with patch(
        "src.core.memory.distiller._call_llm_sync",
        return_value="This is not valid JSON.",
    ):
        result = distill_context(
            [{"role": "user", "content": "test"}],
            working_dir=tmp_path,
        )
    assert result == {}
