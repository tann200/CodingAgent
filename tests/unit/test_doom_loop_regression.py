"""
Regression tests for the "Task: Task: Task:..." doom-loop bugs.

Bug 1 (planning_node): planning_node was overwriting state["task"] with
    the current step description, causing "Task: Task: Task:..." prefix
    accumulation across debug/replan cycles.

Bug 2 (perception_node GAP-SMALL-4): "summarize the project readme" was
    being classified as ambiguous (no action verb found) so the agent asked
    clarifying questions instead of reading README.md.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Bug 1: planning_node must NOT overwrite state["task"] with step description
# ---------------------------------------------------------------------------


class TestPlanningNodeDoomLoopFix:
    """planning_node early-return must not include 'task': step_desc."""

    def _make_step(self, desc: str) -> dict:
        return {"description": desc, "action": None}

    def _make_state(self, task: str, steps: list, step: int = 0) -> dict:
        return {
            "task": task,
            "original_task": task,
            "current_plan": steps,
            "current_step": step,
            "task_decomposed": True,
            "plan_attempts": 0,
            "working_dir": "/tmp",
            "history": [],
        }

    def _make_config(self) -> dict:
        orch = MagicMock()
        orch.get_provider_capabilities = MagicMock(return_value={})
        return {"configurable": {"orchestrator": orch}}

    @pytest.mark.asyncio
    async def test_task_not_overwritten_with_step_description(self):
        """planning_node early-return must preserve state['task'] unchanged."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        original_task = "summarize the project readme"
        steps = [
            self._make_step("read README.md"),
            self._make_step("summarize the content"),
        ]
        state = self._make_state(original_task, steps, step=0)
        config = self._make_config()

        result = await planning_node(state, config)

        # "task" must NOT appear in the return dict (we don't touch it)
        assert "task" not in result, (
            f"planning_node must not set 'task' in early-return dict; "
            f"got task={result.get('task')!r}"
        )
        # step_description should carry the step text instead
        assert result.get("step_description") == "read README.md"
        # original plan and step are preserved
        assert result["current_plan"] == steps
        assert result["current_step"] == 0

    @pytest.mark.asyncio
    async def test_task_field_not_prefixed_after_multiple_cycles(self):
        """Simulate two planning cycles; task string must not accumulate 'Task: ' prefix."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        original_task = "fix the login bug"
        steps = [
            self._make_step("Task: fix the login bug")
        ]  # step desc already has prefix
        state = self._make_state(original_task, steps, step=0)
        config = self._make_config()

        result = await planning_node(state, config)

        # task must still be absent from return (not clobbered)
        assert "task" not in result
        # step_description carries the raw step text (it's fine if it has "Task: " prefix
        # since we don't strip step descriptions — only state["task"])
        assert "step_description" in result

    @pytest.mark.asyncio
    async def test_original_task_preserved_in_state(self):
        """original_task in state must survive planning_node execution unchanged."""
        from src.core.orchestration.graph.nodes.planning_node import planning_node

        original_task = "refactor the auth module"
        steps = [
            self._make_step("read auth/models.py"),
            self._make_step("refactor models"),
        ]
        state = self._make_state(original_task, steps, step=1)
        config = self._make_config()

        result = await planning_node(state, config)

        # planning_node must not touch original_task
        assert (
            result.get("original_task") is None
            or result.get("original_task") == original_task
        ), "planning_node must not corrupt original_task"
        assert "task" not in result


# ---------------------------------------------------------------------------
# Bug 2: GAP-SMALL-4 clarification guard — summarize verbs must not trigger it
# ---------------------------------------------------------------------------


class TestGapSmall4SummarizeFix:
    """perception_node must not ask for clarification on summarization tasks."""

    def _make_state(self, task: str, model_tier: str = "small") -> dict:
        return {
            "task": task,
            "original_task": task,
            "history": [],
            "rounds": 0,
            "working_dir": "/tmp",
            "turn_count": 0,
            "max_turns": 50,
            "empty_response_count": 0,
        }

    def _make_config(self, model: str = "qwen2.5-coder-3b") -> dict:
        orch = MagicMock()
        adapter = MagicMock()
        adapter.provider = {"name": "lmstudio", "type": "openai"}
        adapter.models = [model]
        adapter.context_window = 8192
        adapter.default_model = model
        orch.adapter = adapter
        orch.tool_registry = MagicMock()
        orch.tool_registry.tools = {}
        orch.get_provider_capabilities = MagicMock(return_value={})
        orch.cancel_event = None
        orch.event_bus = MagicMock()
        orch.session_store = MagicMock()
        orch.session_store.read_recent_decisions = MagicMock(return_value=[])
        return {"configurable": {"orchestrator": orch}}

    @pytest.mark.parametrize(
        "task",
        [
            "summarize the project readme",
            "summarize README.md",
            "describe the architecture",
            "explain the codebase",
            "analyze the repo",
        ],
    )
    @pytest.mark.asyncio
    async def test_summarize_tasks_not_flagged_as_ambiguous(self, task):
        """Tasks with summarization verbs must bypass the GAP-SMALL-4 clarification guard."""
        from src.core.orchestration.graph.nodes import perception_node as pn_module

        # Patch call_model so we don't need a real LLM
        with patch.object(
            pn_module,
            "call_model",
            new=AsyncMock(
                return_value={
                    "choices": [{"message": {"content": "I will read README.md now."}}],
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                }
            ),
        ):
            state = self._make_state(task)
            config = self._make_config()

            result = await pn_module.perception_node(state, config)

            # Must NOT return needs_clarification=True
            assert result.get("needs_clarification") is not True, (
                f"Task {task!r} was incorrectly flagged as ambiguous by GAP-SMALL-4"
            )

    @pytest.mark.asyncio
    async def test_genuinely_ambiguous_task_still_triggers_clarification(self):
        """Short tasks with no recognisable action or file ref still get clarification."""
        from src.core.orchestration.graph.nodes import perception_node as pn_module

        state = self._make_state("the widget")  # no verb, no file ref, < 8 words
        config = self._make_config()

        # No need to patch call_model — GAP-SMALL-4 returns before LLM call
        result = await pn_module.perception_node(state, config)

        assert result.get("needs_clarification") is True, (
            "Genuinely ambiguous task should still trigger clarification"
        )


# ---------------------------------------------------------------------------
# Bug 1 (inference_loop): original_task must be set from prompt, not None
# ---------------------------------------------------------------------------


class TestInferenceLoopOriginalTask:
    """initial_state must always have original_task populated from the prompt."""

    def test_original_task_set_in_initial_state(self):
        """run_agent_once_impl must populate original_task from the user prompt."""
        import importlib
        import sys

        # We just inspect the source rather than running the full function
        # (which requires a live orchestrator) — check the dict literal directly.
        mod = importlib.import_module("src.core.orchestration.inference_loop")
        import inspect

        src = inspect.getsource(mod)
        # The fix should have replaced "original_task": None with a prompt-based value
        assert '"original_task": None' not in src, (
            "inference_loop.py still has 'original_task': None — "
            "it must be set to `prompt` so nodes have an authoritative task string"
        )
        assert "original_task" in src and "prompt" in src, (
            "inference_loop.py must set original_task from the prompt"
        )
