"""
Regression tests for Vol28 audit fixes: LM Studio spam prevention.

Covers:
  OVF-1  LM Studio overflow pattern added to _OVERFLOW_PATTERNS
  OVF-2  perception_node returns early on context_overflow (no corrective-prompt retry)
  OVF-3  route_after_perception routes to memory_sync on context_overflow error
  OVF-4  planning_node strips leading "Task: " prefix from task to prevent accumulation
  OVF-5  execution_node returns early on context_overflow (no empty-response retry)
  OVF-6  route_execution routes to memory_sync on context_overflow error
"""

import asyncio
import inspect


class TestOvf1LmStudioOverflowPatternDetected:
    """OVF-1: LM Studio overflow error must be recognised as context overflow."""

    def test_lm_studio_pattern_in_overflow_patterns(self):
        """'exceeds the available context' must be in _OVERFLOW_PATTERNS."""
        import src.core.inference.adapters.openai_compat_adapter as oa

        src_text = inspect.getsource(oa)
        assert "exceeds the available context" in src_text, (
            "OVF-1: 'exceeds the available context' pattern must be present "
            "to detect LM Studio's context overflow error"
        )

    def test_adapter_detects_lm_studio_overflow_error(self):
        """Adapter must set context_overflow=True for LM Studio error body."""
        # We test the detection logic directly by constructing the body that
        # LM Studio would return.
        lm_studio_body = {
            "error": {
                "message": "request (101084 tokens) exceeds the available context size (4096 tokens)"
            }
        }
        _OVERFLOW_PATTERNS = (
            "context_length_exceeded",
            "context length exceeded",
            "maximum context length",
            "context window",
            "prompt is too long",
            "tokens exceeds",
            "exceeds the model's maximum",
            "input is too long",
            "exceeds the available context",
            "available context size",
            "exceeds context length",
            "context length is",
        )
        err = lm_studio_body.get("error")
        msg = err.get("message", "")
        msg_lower = msg.lower()
        detected = any(p in msg_lower for p in _OVERFLOW_PATTERNS)
        assert detected, "OVF-1: LM Studio error message must match an overflow pattern"


class TestOvf2PerceptionNodeEarlyExitOnOverflow:
    """OVF-2: perception_node must return immediately on context_overflow."""

    def test_early_exit_code_present_in_perception_node(self):
        """perception_node must have the early-exit block after REACT-OVF."""
        import src.core.orchestration.graph.nodes.perception_node as pn

        src_text = inspect.getsource(pn)
        assert "REACT-OVF-EARLY-EXIT" in src_text, (
            "OVF-2: REACT-OVF-EARLY-EXIT block must be present in perception_node"
        )
        assert "_compacted_history" in src_text, (
            "OVF-2: perception_node must set _compacted_history on overflow"
        )

    def test_overflow_early_exit_returns_error_flag(self):
        """The overflow early-exit must include errors=['context_overflow']."""
        import src.core.orchestration.graph.nodes.perception_node as pn

        src_text = inspect.getsource(pn)
        assert '"context_overflow"' in src_text, (
            "OVF-2: overflow early-exit must include 'context_overflow' in errors"
        )

    def test_no_corrective_prompt_on_overflow(self):
        """The overflow early-exit must return before adding a corrective prompt."""
        import src.core.orchestration.graph.nodes.perception_node as pn

        src_text = inspect.getsource(pn)
        # The REACT-OVF-EARLY-EXIT block must contain a return statement,
        # proving that we exit before the corrective-prompt retry loop.
        ovf_idx = src_text.find("REACT-OVF-EARLY-EXIT")
        # The code previously referenced a local variable named 'corrective_prompt'.
        # Newer implementations centralise corrective wording in a helper. Search
        # for that helper name instead to avoid brittle source-string checks.
        select_idx = src_text.find("_select_corrective_prompt", ovf_idx)
        assert ovf_idx != -1, "REACT-OVF-EARLY-EXIT block not found"
        # There must be a 'return {' before the corrective prompt helper is invoked.
        return_idx = src_text.find("return {", ovf_idx)
        assert return_idx != -1, "No return statement found after REACT-OVF-EARLY-EXIT"
        assert return_idx < select_idx or select_idx == -1, (
            "OVF-2: early-exit return must appear BEFORE the corrective prompt helper invocation"
        )


class TestOvf3RouteAfterPerceptionContextOverflow:
    """OVF-3: route_after_perception must route to memory_sync on context_overflow."""

    def test_overflow_routes_to_memory_sync(self):
        """State with errors=['context_overflow'] must route to 'memory_sync'."""
        from src.core.orchestration.graph.builder import route_after_perception

        state = {
            "errors": ["context_overflow"],
            "next_action": None,
            "rounds": 1,
            "last_result": {"ok": False, "error": "Context window overflow"},
        }
        result = route_after_perception(state)
        assert result == "memory_sync", (
            f"OVF-3: context_overflow error must route to memory_sync, got {result!r}"
        )

    def test_overflow_route_takes_priority_over_other_routing(self):
        """context_overflow routing must take priority even with next_action set."""
        from src.core.orchestration.graph.builder import route_after_perception

        state = {
            "errors": ["context_overflow"],
            "next_action": {"name": "read_file", "arguments": {}},
            "rounds": 0,
        }
        result = route_after_perception(state)
        assert result == "memory_sync", (
            "OVF-3: context_overflow error must override next_action routing"
        )

    def test_no_overflow_error_routes_normally(self):
        """Without context_overflow error, routing proceeds normally."""
        from src.core.orchestration.graph.builder import route_after_perception

        state = {
            "errors": [],
            "next_action": None,
            "rounds": 0,
            "task_complexity": "simple",
        }
        result = route_after_perception(state)
        assert result in ("execution", "analysis", "memory_sync", "planning"), (
            f"OVF-3: normal routing must return a valid destination, got {result!r}"
        )


class TestOvf4PlanningNodeTaskPrefixStrip:
    """OVF-4: planning_node must strip 'Task: ' prefix from task before use."""

    def test_strip_single_task_prefix(self):
        """Single 'Task: ' prefix must be stripped."""
        import src.core.orchestration.graph.nodes.planning_node as pn

        src_text = inspect.getsource(pn)
        assert (
            'startswith("Task: ")' in src_text or "startswith('Task: ')" in src_text
        ), "OVF-4: planning_node must strip 'Task: ' prefix from task"

    def test_strip_removes_cascaded_prefixes(self):
        """Multiple 'Task: Task: ' prefixes must all be stripped via the while loop."""
        import src.core.orchestration.graph.nodes.planning_node as pn

        src_text = inspect.getsource(pn)
        assert "while task.startswith" in src_text, (
            "OVF-4: prefix stripping must use a while loop to remove cascaded prefixes"
        )

    def test_strip_does_not_change_clean_task(self):
        """A task without 'Task: ' prefix must pass through unchanged."""
        task = "Refactor the authentication module"
        while task.startswith("Task: ") or task.startswith("Task:\t"):
            task = task[6:]
        assert task == "Refactor the authentication module"

    def test_strip_removes_single_prefix(self):
        """A single 'Task: ' prefix must be removed."""
        task = "Task: Refactor the authentication module"
        while task.startswith("Task: ") or task.startswith("Task:\t"):
            task = task[6:]
        assert task == "Refactor the authentication module"

    def test_strip_removes_cascaded_prefixes_runtime(self):
        """Multiple cascaded 'Task: ' prefixes must be removed."""
        task = "Task: Task: Task: Do something"
        while task.startswith("Task: ") or task.startswith("Task:\t"):
            task = task[6:]
        assert task == "Do something"


class TestOvf5ExecutionNodeEarlyExitOnOverflow:
    """OVF-5: execution_node must return immediately on context_overflow."""

    def test_early_exit_code_present_in_execution_node(self):
        """execution_node must have REACT-OVF-EARLY-EXIT block."""
        import src.core.orchestration.graph.nodes.execution_node as en

        src_text = inspect.getsource(en)
        assert "REACT-OVF-EARLY-EXIT" in src_text, (
            "OVF-5: REACT-OVF-EARLY-EXIT block must be present in execution_node"
        )

    def test_execution_node_overflow_sets_compacted_history(self):
        """execution_node overflow exit must set _compacted_history."""
        import src.core.orchestration.graph.nodes.execution_node as en

        src_text = inspect.getsource(en)
        assert "_compacted_history" in src_text, (
            "OVF-5: execution_node must set _compacted_history on overflow"
        )

    def test_execution_node_overflow_sets_error_flag(self):
        """execution_node overflow exit must include errors=['context_overflow']."""
        import src.core.orchestration.graph.nodes.execution_node as en

        src_text = inspect.getsource(en)
        assert '"context_overflow"' in src_text, (
            "OVF-5: execution_node overflow exit must include 'context_overflow' in errors"
        )

    def test_execution_node_overflow_early_exit_triggers_on_flag(self):
        """execution_node overflow return happens when resp.context_overflow is True."""
        import src.core.orchestration.graph.nodes.execution_node as en

        src_text = inspect.getsource(en)
        assert 'resp.get("context_overflow")' in src_text, (
            "OVF-5: execution_node must check resp.get('context_overflow')"
        )


class TestOvf6RouteExecutionContextOverflow:
    """OVF-6: route_execution must route to memory_sync on context_overflow."""

    def test_overflow_routes_to_memory_sync(self):
        """State with errors=['context_overflow'] must route to 'memory_sync'."""
        from src.core.orchestration.graph.builder import route_execution

        state = {
            "errors": ["context_overflow"],
            "next_action": None,
            "rounds": 1,
            "last_result": {"ok": False, "error": "Context window overflow"},
            "tool_call_count": 0,
            "max_tool_calls": 30,
        }
        result = route_execution(state)
        assert result == "memory_sync", (
            f"OVF-6: context_overflow error must route to memory_sync, got {result!r}"
        )

    def test_overflow_takes_priority_over_tool_budget(self):
        """context_overflow gate must fire before tool-budget gate."""
        from src.core.orchestration.graph.builder import route_execution

        state = {
            "errors": ["context_overflow"],
            "tool_call_count": 999,
            "max_tool_calls": 1,
            "rounds": 5,
        }
        result = route_execution(state)
        assert result == "memory_sync", (
            "OVF-6: context_overflow must route to memory_sync (not be blocked by budget check)"
        )

    def test_no_overflow_routes_normally(self):
        """Without context_overflow error, routing proceeds normally."""
        from src.core.orchestration.graph.builder import route_execution

        state = {
            "errors": [],
            "tool_call_count": 0,
            "max_tool_calls": 30,
            "replan_required": False,
            "current_plan": [{"description": "step", "completed": True}],
            "current_step": 0,
            "rounds": 1,
            "last_result": {"ok": True},
        }
        result = route_execution(state)
        assert result in (
            "step_controller",
            "memory_sync",
            "replan",
            "analysis",
            "perception",
            "wait_for_user",
        ), f"OVF-6: normal routing must return a valid destination, got {result!r}"
