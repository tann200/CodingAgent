import pytest
from unittest.mock import patch, MagicMock
from src.core.memory.distiller import distill_context
import json

@patch('src.core.llm_manager.call_model')
def test_distill_context_success(mock_call_model, tmp_path):
    # Mock the LLM response returning valid JSON
    expected_output = {
        "current_task": "Write test cases for MVP",
        "completed_steps": ["Read MVP roadmap", "Created ContextBuilder tests"],
        "next_step": "Create distiller tests"
    }
    mock_call_model.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(expected_output)
                }
            }
        ]
    }
    
    messages = [
        {"role": "user", "content": "Let's do this."},
        {"role": "assistant", "content": "I am working on it."}
    ]
    
    # The orchestrator creates .agent-context in production, so we mock that here
    agent_context_dir = tmp_path / ".agent-context"
    agent_context_dir.mkdir(parents=True, exist_ok=True)
    
    # We pass the tmp_path as the working_dir to test file writing
    result = distill_context(messages, working_dir=tmp_path)
    
    assert result == expected_output
    
    # Check if TASK_STATE.md was created and formatted properly
    agent_context_dir = tmp_path / ".agent-context"
    task_state_file = agent_context_dir / "TASK_STATE.md"
    assert task_state_file.exists()
    
    content = task_state_file.read_text()
    assert "Write test cases for MVP" in content
    assert "- Read MVP roadmap" in content
    assert "- Created ContextBuilder tests" in content
    assert "Create distiller tests" in content

@patch('src.core.llm_manager.call_model')
def test_distill_context_empty_messages(mock_call_model, tmp_path):
    # Should handle empty messages gracefully
    result = distill_context([], working_dir=tmp_path)
    assert result == {}
    assert not mock_call_model.called

@patch('src.core.llm_manager.call_model')
def test_distill_context_llm_failure(mock_call_model, tmp_path):
    # Should handle LLM failure (invalid JSON) gracefully
    mock_call_model.return_value = {
        "choices": [
            {
                "message": {
                    "content": "This is not valid JSON."
                }
            }
        ]
    }
    
    messages = [
        {"role": "user", "content": "test"}
    ]
    
    result = distill_context(messages, working_dir=tmp_path)
    assert result == {}
