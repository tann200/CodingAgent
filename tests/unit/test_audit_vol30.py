"""
Regression tests for Vol30 audit fixes.

Covers:
  P1-B  _prune_tools() now called from build_prompt() — SMALL/MEDIUM get correct tool count
  P1-C  Thinking-block detection uses strip_thinking() (re.sub DOTALL) not tag-only replace
  P1-C  Tool parsing attempted on thinking-stripped content first
  P2-A  Global recovery cap (total_recovery_attempts) in debug_node, replan_node, and routing

> Updated 2026-04-27: NANO removed, replaced with SMALL
"""

import inspect


# ---------------------------------------------------------------------------
# P1-B: _prune_tools() called from build_prompt()
# ---------------------------------------------------------------------------


class TestP1BPruneToolsCalledInBuildPrompt:
    """_prune_tools() must be invoked inside _build_static_system_prefix."""

    def test_prune_tools_called_before_render(self):
        """build_prompt pipeline must call _prune_tools() before _render_tools_for_tier()."""
        from src.core.context import context_builder as cb_mod

        src = inspect.getsource(cb_mod.ContextBuilder._build_static_system_prefix)
        assert "_prune_tools" in src, (
            "P1-B: _prune_tools() must be called inside _build_static_system_prefix"
        )
        assert "_render_tools_for_tier" in src, (
            "P1-B: _render_tools_for_tier() must be called inside _build_static_system_prefix"
        )
        prune_idx = src.index("_prune_tools")
        render_idx = src.index("_render_tools_for_tier")
        assert prune_idx < render_idx, (
            "P1-B: _prune_tools() must be called BEFORE _render_tools_for_tier() "
            f"(prune at {prune_idx}, render at {render_idx})"
        )

    # Updated 2026-04-27: NANO -> SMALL, removed duplicate

    def test_small_tier_tool_count_enforced(self):
        """build_prompt with SMALL tier must return ≤20 tools (ModelTier.SMALL limit = 20)."""
        import tempfile
        from pathlib import Path
        from src.core.context.context_builder import ContextBuilder

        dummy_tools = [
            {"name": f"tool_{i}", "description": f"A tool number {i}."}
            for i in range(40)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            from src.tools.tools_config import agent_context_path

            agent_context_path(Path(tmpdir)).mkdir(parents=True, exist_ok=True)
            cb = ContextBuilder(working_dir=tmpdir)
            messages = cb.build_prompt(
                role_name="operational",
                active_skills=[],
                task_description="test task",
                tools=dummy_tools,
                conversation=[],
                model_tier="small",
            )

        system_msg = next(
            (m["content"] for m in messages if m.get("role") == "system"), ""
        )
        import re

        tool_entries = re.findall(r"name: tool_\d+", system_msg)
        assert len(tool_entries) <= 20, (
            f"P1-B: SMALL tier must have ≤20 tools in prompt, got {len(tool_entries)}"
        )

    def test_medium_tier_tool_count_not_over_pruned(self):
        """MEDIUM tier with 35 tools should keep all 35 (limit is 35)."""
        import tempfile
        from pathlib import Path
        from src.core.context.context_builder import ContextBuilder

        dummy_tools = [
            {"name": f"tool_{i}", "description": f"A tool number {i}."}
            for i in range(35)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".codingAgent").mkdir()
            cb = ContextBuilder(working_dir=tmpdir)
            messages = cb.build_prompt(
                role_name="operational",
                active_skills=[],
                task_description="test task",
                tools=dummy_tools,
                conversation=[],
                model_tier="medium",
            )

        system_msg = next(
            (m["content"] for m in messages if m.get("role") == "system"), ""
        )
        import re

        tool_entries = re.findall(r"name: tool_\d+", system_msg)
        # MEDIUM gets 35 tools — none should be pruned when input is exactly 35
        assert len(tool_entries) == 35, (
            f"P1-B: MEDIUM tier should keep all 35 tools, got {len(tool_entries)}"
        )


# ---------------------------------------------------------------------------
# P1-C: Thinking-block detection uses strip_thinking()
# ---------------------------------------------------------------------------


class TestP1CThinkingBlockDetection:
    """Thinking-block checks must use strip_thinking() (re.sub DOTALL), not tag-only replace."""

    def test_perception_node_uses_strip_thinking_import(self):
        """perception_node must import strip_thinking for the empty-response check."""
        import src.core.orchestration.graph.nodes.perception_node as pn

        src_text = inspect.getsource(pn)
        assert "strip_thinking" in src_text, (
            "P1-C: perception_node must use strip_thinking() for thinking-block detection"
        )

    def test_strip_thinking_correctly_removes_content(self):
        """strip_thinking() must remove the full block including content."""
        from src.core.inference.thinking_utils import strip_thinking

        # The old approach (replace tags only) left the content behind
        result = strip_thinking("<think>I need to reason about this</think>")
        assert result == "", (
            "P1-C: strip_thinking must remove the full block including reasoning content"
        )

    def test_strip_thinking_preserves_yaml_after_think_block(self):
        """strip_thinking() must preserve content that comes after the think block."""
        from src.core.inference.thinking_utils import strip_thinking

        content = "<think>reasoning here</think>\n```yaml\nname: read_file\narguments:\n  path: /foo\n```"
        result = strip_thinking(content)
        assert "read_file" in result, (
            "P1-C: strip_thinking must preserve YAML content after </think>"
        )
        assert "<think>" not in result, (
            "P1-C: strip_thinking must remove the think tags"
        )
        assert "reasoning here" not in result, (
            "P1-C: strip_thinking must remove the reasoning content too"
        )

    def test_perception_node_parses_yaml_after_think_block(self):
        """parse_tool_block on thinking-stripped content must find YAML that follows a think block."""
        from src.core.inference.thinking_utils import strip_thinking
        from src.core.orchestration.tool_parser import parse_tool_block

        content = "<think>I should read the file first.</think>\n```yaml\nname: read_file\narguments:\n  path: /tmp/foo.py\n```"
        stripped = strip_thinking(content)
        tool_call = parse_tool_block(stripped)
        assert tool_call is not None, (
            "P1-C: parse_tool_block on think-stripped content must extract the YAML tool call"
        )
        assert tool_call["name"] == "read_file", (
            f"P1-C: expected tool name 'read_file', got {tool_call.get('name')!r}"
        )

    def test_thinking_only_response_detected_as_empty(self):
        """A response of only <think>...</think> must be detected as empty (no tool call)."""
        from src.core.inference.thinking_utils import strip_thinking

        content = "<think>Let me think about this task...</think>"
        stripped = strip_thinking(content)
        assert stripped == "", (
            "P1-C: thinking-only response must yield empty string after strip_thinking"
        )

    def test_old_tag_replace_approach_missed_content(self):
        """Demonstrate why the old approach was wrong — it left reasoning text behind."""
        content = "<think>reasoning content here</think>"
        # Old approach: only removes markers, leaves content
        old_result = content.replace("<think>", "").replace("</think>", "").strip()
        assert old_result == "reasoning content here", (
            "P1-C sanity: old replace() approach leaves content behind (this is why it was broken)"
        )
        # New approach: removes everything
        from src.core.inference.thinking_utils import strip_thinking

        new_result = strip_thinking(content)
        assert new_result == "", (
            "P1-C: new strip_thinking approach correctly removes the full block"
        )


# ---------------------------------------------------------------------------
# P2-A: Global recovery cap
# ---------------------------------------------------------------------------


class TestP2AGlobalRecoveryCap:
    """total_recovery_attempts must be incremented by both debug_node and replan_node."""

    def test_total_recovery_attempts_in_agentstate(self):
        """AgentState TypedDict must declare total_recovery_attempts."""
        import src.core.orchestration.graph.state as st

        src_text = inspect.getsource(st)
        assert "total_recovery_attempts" in src_text, (
            "P2-A: AgentState must declare total_recovery_attempts field"
        )

    def test_total_recovery_attempts_in_initial_state(self):
        """initial_state in inference_loop.py must initialise total_recovery_attempts to 0."""
        import src.core.orchestration.inference_loop as il

        src_text = inspect.getsource(il)
        assert (
            '"total_recovery_attempts": 0' in src_text
            or "'total_recovery_attempts': 0" in src_text
        ), (
            "P2-A: inference_loop must initialise total_recovery_attempts=0 in initial_state"
        )

    def test_debug_node_increments_total_recovery(self):
        """debug_node must increment total_recovery_attempts in its return dict."""
        import src.core.orchestration.graph.nodes.debug_node as dn

        src_text = inspect.getsource(dn)
        assert "total_recovery_attempts" in src_text, (
            "P2-A: debug_node must track and return total_recovery_attempts"
        )

    def test_replan_node_increments_total_recovery(self):
        """replan_node must increment total_recovery_attempts in its return dict."""
        import src.core.orchestration.graph.nodes.replan_node as rn

        src_text = inspect.getsource(rn)
        assert "total_recovery_attempts" in src_text, (
            "P2-A: replan_node must track and return total_recovery_attempts"
        )

    def test_should_after_debug_checks_global_cap(self):
        """should_after_debug must route to memory_sync when global cap is reached."""
        from src.core.orchestration.graph.builder import should_after_debug

        # With MEDIUM tier cap=8, a state with 8 total_recovery_attempts should bail
        state = {
            "total_recovery_attempts": 8,
            "model_tier": "medium",
            "next_action": {"name": "read_file"},  # would normally route to execution
            "debug_attempts": 1,
            "max_debug_attempts": 3,
        }
        result = should_after_debug(state)
        assert result == "memory_sync", (
            f"P2-A: should_after_debug must route to memory_sync at cap, got {result!r}"
        )

    def test_should_after_replan_checks_global_cap(self):
        """should_after_replan must route to memory_sync when global cap is reached."""
        from src.core.orchestration.graph.builder import should_after_replan

        state = {
            "total_recovery_attempts": 8,
            "model_tier": "medium",
            "replan_required": None,  # would normally route to step_controller
        }
        result = should_after_replan(state)
        assert result == "memory_sync", (
            f"P2-A: should_after_replan must route to memory_sync at cap, got {result!r}"
        )

    def test_recovery_cap_is_tier_aware(self):
        """SMALL tier cap (4) is lower than FRONTIER cap (12)."""
        from src.core.orchestration.graph.builder import should_after_debug

        # SMALL with 4 attempts should hit cap (cap=4)
        small_state = {
            "total_recovery_attempts": 4,
            "model_tier": "small",  # Changed from "nano"
            "next_action": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
        }
        assert should_after_debug(small_state) == "memory_sync", (
            "P2-A: SMALL tier should hit global cap at 4 total_recovery_attempts"
        )

        # FRONTIER with 2 attempts should NOT hit cap (cap=12)
        frontier_state = {
            "total_recovery_attempts": 2,
            "model_tier": "frontier",
            "next_action": None,
            "debug_attempts": 0,
            "max_debug_attempts": 3,
        }
        result = should_after_debug(frontier_state)
        assert result != "memory_sync", (
            f"P2-A: FRONTIER with 2 total_recovery should NOT be capped yet, got {result!r}"
        )

    def test_recovery_below_cap_still_routes_normally(self):
        """When below cap, routing should behave as before."""
        from src.core.orchestration.graph.builder import should_after_debug

        # Below cap, fix generated → execution
        state = {
            "total_recovery_attempts": 3,
            "model_tier": "medium",
            "next_action": {"name": "write_file"},
            "debug_attempts": 1,
            "max_debug_attempts": 3,
        }
        result = should_after_debug(state)
        assert result == "execution", (
            f"P2-A: below cap with next_action set should route to execution, got {result!r}"
        )

    def test_replan_memory_sync_wired_in_graph(self):
        """The graph must wire should_after_replan's memory_sync return value."""
        import src.core.orchestration.graph.builder as bld

        src_text = inspect.getsource(bld)
        # The conditional edges dict for the replan node must include memory_sync
        assert (
            '"memory_sync": "memory_sync"' in src_text
            or "'memory_sync': 'memory_sync'" in src_text
        ), (
            "P2-A: graph wiring for should_after_replan must include 'memory_sync' destination"
        )
