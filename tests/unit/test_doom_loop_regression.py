"""
Regression tests for doom-loop bugs.

Bug 1 (inference_loop): original_task must be set from prompt, not None
"""

import importlib
import inspect


class TestInferenceLoopOriginalTask:
    """initial_state must always have original_task populated from the prompt."""

    def test_original_task_set_in_initial_state(self):
        """run_agent_once_impl must populate original_task from the user prompt."""
        mod = importlib.import_module("src.core.orchestration.inference_loop")
        src = inspect.getsource(mod)
        assert '"original_task": None' not in src, (
            "inference_loop.py still has 'original_task': None — "
            "it must be set to `prompt` so nodes have an authoritative task string"
        )
        assert "original_task" in src and "prompt" in src, (
            "inference_loop.py must set original_task from the prompt"
        )
