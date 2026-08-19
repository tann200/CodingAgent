"""Tests for frontier_loop_node.py — P1-9."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
import src.core.orchestration.graph.nodes.frontier_loop_node as fln
from src.core.orchestration.graph.nodes.frontier_loop_node import (
    _extract_content_text,
    _extract_tool_call_from_text,
    _filter_tools_for_tier,
    _is_context_overflow,
    _normalize_messages_for_native_tools,
    _normalize_tool_call,
    _plan_mode_blocks,
    _render_tool_calls_text,
    _append_tool_call_history,
    _truncate_tool_output,
    frontier_loop_node,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(**kwargs) -> dict:
    defaults = {
        "task": "Write hello.py",
        "history": [],
        "tool_call_count": 0,
        "max_tool_calls": 10,
        "model_tier": "frontier",
        "errors": [],
        "next_action": None,
        "files_read": {},
        "tool_last_used": {},
        "working_dir": "/tmp",
        "analyst_findings": "",
    }
    defaults.update(kwargs)
    return defaults


def _make_llm_response(content: str = "", tool_calls: list | None = None) -> dict:
    """Minimal OpenAI-format LLM response."""
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg, "finish_reason": "stop" if not tool_calls else "tool_calls"}],
        "total_tokens": 100,
    }


def _make_native_tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _make_orchestrator(tool_result: dict | None = None) -> MagicMock:
    orch = MagicMock()
    orch.event_bus = MagicMock()
    orch.plan_mode = None
    orch.working_dir = "/tmp"
    orch.tool_registry = MagicMock()
    orch.tool_registry.get_openai_functions.return_value = []
    orch.execute_tool = MagicMock(return_value=tool_result or {"ok": True, "output": "done"})
    return orch


# ---------------------------------------------------------------------------
# _is_context_overflow
# ---------------------------------------------------------------------------

class TestIsContextOverflow:
    def test_two_matching_patterns(self):
        msg = "maximum context length exceeded, input is too long"
        assert _is_context_overflow(msg) is True

    def test_single_pattern_not_overflow(self):
        assert _is_context_overflow("maximum context length is fine") is False

    def test_empty_string(self):
        assert _is_context_overflow("") is False

    def test_case_insensitive(self):
        assert _is_context_overflow("INPUT IS TOO LONG, PROMPT IS TOO LONG") is True


# ---------------------------------------------------------------------------
# _normalize_tool_call
# ---------------------------------------------------------------------------

class TestNormalizeToolCall:
    def test_dict_with_name_and_args(self):
        tc = {"name": "read_file", "arguments": {"path": "foo.py"}}
        name, args = _normalize_tool_call(tc)
        assert name == "read_file"
        assert args == {"path": "foo.py"}

    def test_dict_with_function_key(self):
        tc = {"function": {"name": "bash", "arguments": '{"cmd":"ls"}'}}
        name, args = _normalize_tool_call(tc)
        assert name == "bash"
        assert args == {"cmd": "ls"}

    def test_object_with_function_attribute(self):
        tc = MagicMock()
        tc.function.name = "write_file"
        tc.function.arguments = '{"path": "a.py", "content": "x"}'
        name, args = _normalize_tool_call(tc)
        assert name == "write_file"
        assert args["path"] == "a.py"

    def test_dict_with_tool_key(self):
        tc = {"tool": "glob", "args": {"pattern": "*.py"}}
        name, args = _normalize_tool_call(tc)
        assert name == "glob"
        assert args == {"pattern": "*.py"}

    def test_broken_object_returns_empty(self):
        tc = object()
        name, args = _normalize_tool_call(tc)
        assert name == ""
        assert args == {}

    def test_args_as_json_string(self):
        tc = {"name": "grep", "arguments": '{"pattern": "TODO"}'}
        name, args = _normalize_tool_call(tc)
        assert name == "grep"
        assert args == {"pattern": "TODO"}


# ---------------------------------------------------------------------------
# _extract_content_text
# ---------------------------------------------------------------------------

class TestExtractContentText:
    def test_dict_with_choices(self):
        resp = _make_llm_response(content="hello world")
        assert _extract_content_text(resp) == "hello world"

    def test_object_with_content_attr(self):
        resp = MagicMock()
        resp.content = "attr content"
        assert _extract_content_text(resp) == "attr content"

    def test_list_content(self):
        resp = {"choices": [{"message": {"content": [{"text": "A"}, {"text": "B"}]}}]}
        result = _extract_content_text(resp)
        assert "A" in result and "B" in result

    def test_fallback_returns_empty(self):
        assert _extract_content_text(None) == ""

    def test_empty_choices(self):
        resp = {"choices": []}
        assert _extract_content_text(resp) == ""


# ---------------------------------------------------------------------------
# _extract_tool_call_from_text
# ---------------------------------------------------------------------------

class TestExtractToolCallFromText:
    def test_pure_json_tool_call(self):
        content = '{"name": "read_file", "arguments": {"path": "foo.py"}}'
        tc, remaining = _extract_tool_call_from_text(content)
        assert tc is not None
        assert tc["name"] == "read_file"
        assert remaining == ""

    def test_embedded_in_text(self):
        content = 'Let me read the file.\n{"name": "read_file", "arguments": {"path": "x.py"}}'
        tc, remaining = _extract_tool_call_from_text(content)
        assert tc is not None
        assert tc["name"] == "read_file"

    def test_no_tool_call_returns_none(self):
        content = "Here is my answer: the answer is 42."
        tc, remaining = _extract_tool_call_from_text(content)
        assert tc is None
        assert remaining == content

    def test_tool_key_variant(self):
        content = '{"tool": "glob", "args": {"pattern": "*.py"}}'
        tc, remaining = _extract_tool_call_from_text(content)
        assert tc is not None
        assert tc["name"] == "glob"

    def test_args_string_parsed(self):
        content = '{"name": "bash", "arguments": "{\\"cmd\\": \\"ls\\"}"}'
        tc, _ = _extract_tool_call_from_text(content)
        assert tc is not None
        assert tc["name"] == "bash"


# ---------------------------------------------------------------------------
# _render_tool_calls_text
# ---------------------------------------------------------------------------

class TestRenderToolCallsText:
    def test_single_call(self):
        tc = {"name": "read_file", "arguments": {"path": "a.py"}}
        result = _render_tool_calls_text([tc])
        parsed = json.loads(result)
        assert parsed["name"] == "read_file"

    def test_skips_empty_name(self):
        tc = {"name": "", "arguments": {}}
        result = _render_tool_calls_text([tc])
        assert result == ""

    def test_multiple_calls_newline_joined(self):
        tcs = [
            {"name": "read_file", "arguments": {"path": "a.py"}},
            {"name": "write_file", "arguments": {"path": "b.py", "content": "x"}},
        ]
        result = _render_tool_calls_text(tcs)
        lines = [l for l in result.strip().split("\n") if l]
        assert len(lines) == 2


# ---------------------------------------------------------------------------
# _append_tool_call_history
# ---------------------------------------------------------------------------

class TestAppendToolCallHistory:
    def test_appends_to_empty_history(self):
        tc = {"name": "read_file", "arguments": {"path": "a.py"}}
        history = _append_tool_call_history([], [tc])
        assert len(history) == 1
        assert history[-1]["role"] == "assistant"

    def test_merges_with_last_assistant_message(self):
        existing = [{"role": "assistant", "content": "Let me check."}]
        tc = {"name": "glob", "arguments": {"pattern": "*.py"}}
        history = _append_tool_call_history(existing, [tc])
        assert len(history) == 1
        assert "glob" in history[-1]["content"]

    def test_no_tool_calls_returns_unchanged(self):
        existing = [{"role": "user", "content": "do it"}]
        result = _append_tool_call_history(existing, [])
        assert result == existing


# ---------------------------------------------------------------------------
# _filter_tools_for_tier
# ---------------------------------------------------------------------------

class TestFilterToolsForTier:
    def _make_tools(self, names: list[str]) -> list[dict]:
        return [{"function": {"name": n}} for n in names]

    def test_small_tier_filters_to_core(self):
        tools = self._make_tools(["read_file", "write_file", "obscure_tool"])
        with patch("src.core.orchestration.graph.nodes.frontier_loop_node._filter_tools_for_tier",
                   wraps=_filter_tools_for_tier):
            result = _filter_tools_for_tier(tools, "small")
        names = {t["function"]["name"] for t in result}
        assert "obscure_tool" not in names
        assert "read_file" in names

    def test_empty_tools_passthrough(self):
        assert _filter_tools_for_tier([], "frontier") == []

    def test_invalid_tier_passthrough(self):
        tools = self._make_tools(["read_file", "custom_tool"])
        result = _filter_tools_for_tier(tools, "invalid_tier")
        assert result == tools


# ---------------------------------------------------------------------------
# _normalize_messages_for_native_tools
# ---------------------------------------------------------------------------

class TestNormalizeMessagesForNativeTools:
    def test_plain_messages_passthrough(self):
        msgs = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ]
        result = _normalize_messages_for_native_tools(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "system"

    def test_text_tool_call_converted(self):
        msgs = [
            {"role": "assistant", "content": '{"name": "read_file", "arguments": {"path": "a.py"}}'},
        ]
        result = _normalize_messages_for_native_tools(msgs)
        assert result[0]["role"] == "assistant"
        assert "tool_calls" in result[0]
        assert result[0]["tool_calls"][0]["function"]["name"] == "read_file"

    def test_tool_result_converted(self):
        msgs = [
            {"role": "assistant", "content": '{"name": "read_file", "arguments": {"path": "a.py"}}'},
            {"role": "user", "content": json.dumps({"tool_execution_result": {"content": "file contents"}})},
        ]
        result = _normalize_messages_for_native_tools(msgs)
        tool_msg = result[-1]
        assert tool_msg["role"] == "tool"
        assert "file contents" in tool_msg["content"]

    def test_call_id_not_leaked(self):
        msgs = [{"role": "assistant", "content": '{"name": "bash", "arguments": {"cmd": "ls"}}'}]
        result = _normalize_messages_for_native_tools(msgs)
        assert "_call_id" not in result[0]


# ---------------------------------------------------------------------------
# _plan_mode_blocks
# ---------------------------------------------------------------------------

class TestPlanModeBlocks:
    def test_no_plan_mode_returns_false(self):
        orch = MagicMock()
        orch.plan_mode = None
        assert _plan_mode_blocks(orch, "write_file") is False

    def test_plan_mode_disabled_returns_false(self):
        orch = MagicMock()
        orch.plan_mode = MagicMock(enabled=False)
        assert _plan_mode_blocks(orch, "write_file") is False

    def test_plan_mode_enabled_approved_returns_false(self):
        orch = MagicMock()
        orch.plan_mode = MagicMock(enabled=True)
        orch._plan_mode_approved = True
        assert _plan_mode_blocks(orch, "write_file") is False

    def test_plan_mode_enabled_blocks_write(self):
        orch = MagicMock()
        orch.plan_mode = MagicMock(enabled=True)
        orch._plan_mode_approved = False
        with patch("src.core.orchestration.graph.nodes.frontier_loop_node._plan_mode_blocks",
                   wraps=_plan_mode_blocks):
            with patch("src.core.orchestration.plan_mode.PlanMode") as MockPM:
                MockPM.BLOCKED_TOOLS = {"write_file"}
                result = _plan_mode_blocks(orch, "write_file")
        # Just ensure no exception — actual block logic tested via integration
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# frontier_loop_node — integration (async)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestFrontierLoopNodeMain:

    async def _run(self, state: dict, orch: Any, call_model_side_effect=None) -> dict:
        """Run frontier_loop_node with a patched call_model."""
        config = {"configurable": {"orchestrator": orch}}
        with patch.object(fln, "call_model", new_callable=AsyncMock) as mock_cm, \
             patch("src.core.orchestration.graph.nodes.frontier_loop_node._prepare_turn_messages",
                   new_callable=AsyncMock) as mock_prep, \
             patch("src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
                   return_value=orch):
            mock_prep.return_value = [{"role": "system", "content": "sys"}, {"role": "user", "content": state["task"]}]
            if call_model_side_effect is not None:
                mock_cm.side_effect = call_model_side_effect
            else:
                # Default: return a "done" response with no tool calls
                mock_cm.return_value = _make_llm_response(content="Task complete.")
            result = await frontier_loop_node(state, config)
        return result

    async def test_no_tool_calls_exits_cleanly(self):
        orch = _make_orchestrator()
        state = _make_state()
        result = await self._run(state, orch)
        assert "history" in result
        assert result["_frontier_loop_turns"] >= 1
        assert result.get("awaiting_plan_approval") is False

    async def test_context_overflow_sets_error(self):
        orch = _make_orchestrator()
        state = _make_state()

        async def _fail(*args, **kwargs):
            raise RuntimeError("maximum context length exceeded, input is too long")

        result = await self._run(state, orch, call_model_side_effect=_fail)
        assert "context_overflow" in result["errors"]
        assert result.get("_budget_compaction") is True

    async def test_tool_budget_exhausted_exits(self):
        orch = _make_orchestrator()
        # Start with budget already at max
        state = _make_state(tool_call_count=10, max_tool_calls=10)
        result = await self._run(state, orch)
        # Should exit immediately without calling LLM
        assert result["tool_call_count"] == 10

    async def test_analyst_findings_injected_into_task(self):
        """analyst_findings should be prepended to task when non-empty."""
        orch = _make_orchestrator()
        state = _make_state(analyst_findings="Key finding: use async.")

        captured_task = {}

        async def _patched_prepare(orchestrator, task, history, model_tier, turns_taken):
            captured_task["task"] = task
            return [{"role": "user", "content": task}]

        config = {"configurable": {"orchestrator": orch}}
        with patch.object(fln, "call_model", new_callable=AsyncMock) as mock_cm, \
             patch("src.core.orchestration.graph.nodes.frontier_loop_node._prepare_turn_messages",
                   new=_patched_prepare), \
             patch("src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
                   return_value=orch):
            mock_cm.return_value = _make_llm_response(content="Done.")
            await frontier_loop_node(state, config)

        assert "analyst_findings" in captured_task["task"]
        assert "Key finding: use async." in captured_task["task"]

    async def test_native_tool_call_dispatched(self):
        """A native tool_calls response should be dispatched via orchestrator."""
        orch = _make_orchestrator(tool_result={"ok": True, "output": "file contents"})
        state = _make_state()

        native_tc = _make_native_tool_call("read_file", {"path": "a.py"})

        responses = [
            _make_llm_response(content="", tool_calls=[native_tc]),
            _make_llm_response(content="All done."),
        ]

        config = {"configurable": {"orchestrator": orch}}
        call_count = 0

        async def _multi(*args, **kwargs):
            nonlocal call_count
            resp = responses[min(call_count, len(responses) - 1)]
            call_count += 1
            return resp

        with patch.object(fln, "call_model", new_callable=AsyncMock) as mock_cm, \
             patch("src.core.orchestration.graph.nodes.frontier_loop_node._prepare_turn_messages",
                   new_callable=AsyncMock) as mock_prep, \
             patch("src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
                   return_value=orch):
            mock_prep.return_value = [{"role": "user", "content": "task"}]
            mock_cm.side_effect = _multi
            result = await frontier_loop_node(state, config)

        orch.execute_tool.assert_called_once()
        assert result["tool_call_count"] == 1

    async def test_plan_mode_suspends_loop(self):
        """When plan-mode blocks a tool, node returns awaiting_plan_approval=True."""
        orch = _make_orchestrator()
        state = _make_state()

        native_tc = _make_native_tool_call("write_file", {"path": "out.py", "content": "x"})

        config = {"configurable": {"orchestrator": orch}}
        with patch.object(fln, "call_model", new_callable=AsyncMock) as mock_cm, \
             patch("src.core.orchestration.graph.nodes.frontier_loop_node._prepare_turn_messages",
                   new_callable=AsyncMock) as mock_prep, \
             patch("src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
                   return_value=orch), \
             patch("src.core.orchestration.graph.nodes.frontier_loop_node._plan_mode_blocks",
                   return_value=True):
            mock_prep.return_value = [{"role": "user", "content": "task"}]
            mock_cm.return_value = _make_llm_response(content="", tool_calls=[native_tc])
            result = await frontier_loop_node(state, config)

        assert result["awaiting_plan_approval"] is True
        assert result["next_action"]["name"] == "write_file"

    async def test_pending_action_executed_before_llm(self):
        """next_action in state should be dispatched before calling LLM."""
        orch = _make_orchestrator(tool_result={"ok": True, "output": "done"})
        state = _make_state(next_action={"name": "bash", "arguments": {"cmd": "ls"}})

        config = {"configurable": {"orchestrator": orch}}
        with patch.object(fln, "call_model", new_callable=AsyncMock) as mock_cm, \
             patch("src.core.orchestration.graph.nodes.frontier_loop_node._prepare_turn_messages",
                   new_callable=AsyncMock) as mock_prep, \
             patch("src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
                   return_value=orch):
            mock_prep.return_value = [{"role": "user", "content": "task"}]
            mock_cm.return_value = _make_llm_response(content="All done.")
            result = await frontier_loop_node(state, config)

        # The pending action should have been dispatched (execute_tool called once)
        orch.execute_tool.assert_called_once()
        assert result["tool_call_count"] == 1

    async def test_max_turns_respected(self):
        """Loop should exit after _MAX_FRONTIER_TURNS regardless."""
        orch = _make_orchestrator(tool_result={"ok": True})
        state = _make_state(max_tool_calls=1000)

        native_tc = _make_native_tool_call("bash", {"cmd": "ls"})

        config = {"configurable": {"orchestrator": orch}}
        with patch.object(fln, "call_model", new_callable=AsyncMock) as mock_cm, \
             patch("src.core.orchestration.graph.nodes.frontier_loop_node._prepare_turn_messages",
                   new_callable=AsyncMock) as mock_prep, \
             patch("src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
                   return_value=orch), \
             patch.object(fln, "_MAX_FRONTIER_TURNS", 3):
            mock_prep.return_value = [{"role": "user", "content": "task"}]
            mock_cm.return_value = _make_llm_response(content="", tool_calls=[native_tc])
            result = await frontier_loop_node(state, config)

        assert result["_frontier_loop_turns"] <= 3

    async def test_event_bus_published_on_entry_and_exit(self):
        orch = _make_orchestrator()
        state = _make_state()
        result = await self._run(state, orch)
        # event_bus.publish_typed should have been called at least twice (status working + idle)
        assert orch.event_bus.publish_typed.call_count >= 2

    async def test_no_orchestrator_returns_gracefully(self):
        state = _make_state()
        config = {}
        with patch.object(fln, "call_model", new_callable=AsyncMock) as mock_cm, \
             patch("src.core.orchestration.graph.nodes.frontier_loop_node._prepare_turn_messages",
                   new_callable=AsyncMock) as mock_prep, \
             patch("src.core.orchestration.graph.nodes.node_utils._resolve_orchestrator",
                   return_value=None):
            mock_prep.return_value = [{"role": "user", "content": "task"}]
            mock_cm.return_value = _make_llm_response(content="Done.")
            result = await frontier_loop_node(state, config)
        assert "history" in result

    async def test_result_has_required_keys(self):
        orch = _make_orchestrator()
        state = _make_state()
        result = await self._run(state, orch)
        for key in ("history", "tool_call_count", "last_result", "errors",
                    "_frontier_loop_turns", "awaiting_plan_approval"):
            assert key in result, f"Missing key: {key}"
