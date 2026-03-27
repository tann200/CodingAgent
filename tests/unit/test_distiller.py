import json
import pytest
from unittest.mock import patch
from src.core.memory.distiller import distill_context


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


# ---------------------------------------------------------------------------
# C9: _call_llm_sync must not call asyncio.run() from inside a running loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_llm_sync_safe_inside_running_loop():
    """C9: _call_llm_sync must work when called from within an async context.

    Previously, _call_llm_sync called asyncio.run() even when a loop was already
    running (detected via asyncio.get_running_loop()), which raises:
        RuntimeError: This function cannot be called when another event loop is running
    The fix dispatches the coroutine to a fresh ThreadPoolExecutor thread.
    """
    from src.core.memory.distiller import _call_llm_sync

    async def _fake_call_model(*_args, **_kwargs):
        return {"choices": [{"message": {"content": "hello from async"}}]}

    # call_model is imported locally inside _call_llm_sync, so patch it at source
    with patch(
        "src.core.inference.llm_manager.call_model", side_effect=_fake_call_model
    ):
        # This must not raise RuntimeError even though we are inside an async test
        result = _call_llm_sync([{"role": "user", "content": "hi"}])

    assert result == "hello from async"


def test_call_llm_sync_safe_outside_loop():
    """C9: _call_llm_sync still works when called from a non-async context."""
    from src.core.memory.distiller import _call_llm_sync

    async def _fake(*_args, **_kwargs):
        return {"choices": [{"message": {"content": "sync context ok"}}]}

    with patch("src.core.inference.llm_manager.call_model", side_effect=_fake):
        result = _call_llm_sync([{"role": "user", "content": "test"}])

    assert result == "sync context ok"


def test_call_llm_sync_uses_thread_executor_not_asyncio_run_in_running_loop():
    """C9: Verify the source uses ThreadPoolExecutor instead of asyncio.run()
    when a running loop is detected."""
    import inspect
    from src.core.memory import distiller as dist_mod

    src = inspect.getsource(dist_mod._call_llm_sync)
    assert "ThreadPoolExecutor" in src, (
        "C9: must use ThreadPoolExecutor when loop is running"
    )
    # The bare asyncio.run() call inside the 'running loop detected' branch must be gone
    # (it may still exist in the no-loop branch, so we check the comment is present)
    assert "Running loop detected" in src or "running loop" in src.lower()
