"""Unit tests for the unified 7-node LangGraph architecture (Engineering Audit §Appendix)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.core.orchestration.graph.builder import (
    compile_canonical_graph,
    build_tier_graph,
)
from src.core.orchestration.graph.policy import (
    compute_action_digest,
    create_last_action,
    create_run_state,
    create_runtime_state,
    get_capabilities_for_tier,
    get_policy_for_tier,
    normalize_target_tier,
    MEDIUM_POLICY,
    FRONTIER_POLICY,
)
from src.core.orchestration.graph import canonical_routing
from src.core.orchestration.graph.nodes.initialize_node import _initialize_node_impl
from src.core.orchestration.graph.nodes.approval_node import _approval_node_impl
from src.core.orchestration.graph.nodes.compact_node import _compact_node_impl
from src.core.orchestration.graph.nodes.recover_node import _recover_node_impl
from src.core.orchestration.graph.completion_contract import (
    NoToolClassification,
    classify_no_tool_response,
)


class TestTargetPoliciesAndState:
    def test_tier_normalization(self):
        assert normalize_target_tier("small") == "medium"
        assert normalize_target_tier("medium") == "medium"
        assert normalize_target_tier("lite") == "medium"
        assert normalize_target_tier("large") == "frontier"
        assert normalize_target_tier("frontier") == "frontier"
        assert normalize_target_tier(None) == "medium"

    def test_tier_capability_policies(self):
        med_policy = get_policy_for_tier("medium")
        assert med_policy["turn_limit"] == 6
        assert med_policy["tool_limit"] == 20
        assert med_policy["recovery_limit"] == 2
        assert med_policy["compaction_limit"] == 1
        assert med_policy["verification_mode"] == "targeted"
        assert med_policy["readonly_parallelism"] is False

        fr_policy = get_policy_for_tier("frontier")
        assert fr_policy["turn_limit"] == 12
        assert fr_policy["tool_limit"] == 40
        assert fr_policy["recovery_limit"] == 3
        assert fr_policy["compaction_limit"] == 2
        assert fr_policy["verification_mode"] == "full"
        assert fr_policy["readonly_parallelism"] is True

    def test_capabilities_partition(self):
        med_caps = get_capabilities_for_tier("medium")
        assert len(med_caps) == 18
        assert "files_read" in med_caps
        assert "files_delete" not in med_caps

        fr_caps = get_capabilities_for_tier("frontier")
        assert len(fr_caps) == 27
        assert "files_delete" in fr_caps
        assert "lsp" in fr_caps

    def test_compute_action_digest_deterministic(self):
        d1 = compute_action_digest("files_write", {"path": "a.py", "content": "print(1)"})
        d2 = compute_action_digest("files_write", {"content": "print(1)", "path": "a.py"})
        d3 = compute_action_digest("files_write", {"path": "a.py", "content": "print(2)"})
        assert d1 == d2
        assert d1 != d3

    def test_create_target_state_envelopes(self):
        rt = create_runtime_state(tier="medium", provider="mock", model="m1")
        assert rt["tier"] == "medium"
        assert len(rt["capabilities"]) == 18

        run = create_run_state(status="running", outcome="continue")
        assert run["status"] == "running"
        assert run["outcome"] == "continue"
        assert run["turns"] == 0

        action = create_last_action(
            name="files_write",
            arguments={"path": "x.py"},
            result={"ok": True},
            modified_files=["x.py"],
        )
        assert action["name"] == "files_write"
        assert action["modified_files"] == ["x.py"]
        assert len(action["digest"]) == 64


class TestCanonicalTopology:
    def test_compiled_graph_structure(self):
        graph = compile_canonical_graph()
        nodes = set(graph.get_graph().nodes.keys())
        expected = {
            "__start__",
            "initialize",
            "agent_loop",
            "approval",
            "verify",
            "recover",
            "compact",
            "finalize",
            "__end__",
        }
        assert expected.issubset(nodes)

    def test_build_tier_graph_uses_canonical_topology(self):
        g_med = build_tier_graph("medium")
        g_fr = build_tier_graph("frontier")
        expected_nodes = {"initialize", "agent_loop", "approval", "verify", "recover", "compact", "finalize"}
        assert expected_nodes.issubset(set(g_med.get_graph().nodes.keys()))
        assert expected_nodes.issubset(set(g_fr.get_graph().nodes.keys()))


class TestCanonicalRouting:
    def test_route_after_agent_loop(self):
        assert canonical_routing.route_after_agent_loop({"run": {"outcome": "continue"}}) == "agent_loop"
        assert canonical_routing.route_after_agent_loop({"run": {"outcome": "approval_required"}}) == "approval"
        assert canonical_routing.route_after_agent_loop({"run": {"outcome": "verify"}}) == "verify"
        assert canonical_routing.route_after_agent_loop({"run": {"outcome": "recover"}}) == "recover"
        assert canonical_routing.route_after_agent_loop({"run": {"outcome": "compact"}}) == "compact"
        assert canonical_routing.route_after_agent_loop({"run": {"outcome": "complete"}}) == "finalize"
        assert canonical_routing.route_after_agent_loop({"run": {"outcome": "failed"}}) == "finalize"
        assert canonical_routing.route_after_agent_loop({"run": {"outcome": "cancelled"}}) == "finalize"

    def test_route_after_approval(self):
        assert canonical_routing.route_after_approval({"run": {"outcome": "continue"}}) == "agent_loop"
        assert canonical_routing.route_after_approval({"plan_mode_approved": True}) == "agent_loop"
        assert canonical_routing.route_after_approval({"run": {"outcome": "cancelled"}}) == "finalize"

    def test_route_after_verify(self):
        assert canonical_routing.route_after_verify({"run": {"outcome": "complete"}, "verification_passed": True}) == "finalize"
        # Recovery budget remaining
        state = {
            "run": {"outcome": "recover", "recoveries": 1},
            "runtime": {"policy": {"recovery_limit": 2}},
            "verification_passed": False,
        }
        assert canonical_routing.route_after_verify(state) == "recover"
        # Recovery budget exhausted
        state["run"]["recoveries"] = 2
        assert canonical_routing.route_after_verify(state) == "finalize"

    def test_route_after_recover(self):
        assert canonical_routing.route_after_recover({"run": {"outcome": "continue"}}) == "agent_loop"
        assert canonical_routing.route_after_recover({"pending_action": {"name": "fix"}}) == "agent_loop"
        assert canonical_routing.route_after_recover({"run": {"outcome": "failed"}}) == "finalize"

    def test_route_after_compact(self):
        assert canonical_routing.route_after_compact({"run": {"outcome": "continue"}}) == "agent_loop"
        assert canonical_routing.route_after_compact({"run": {"outcome": "continue"}, "errors": ["context_overflow_exhausted"]}) == "finalize"


class TestCanonicalNodes:
    @pytest.mark.asyncio
    async def test_initialize_node_deterministic(self):
        state = {
            "task": "Test initialize",
            "modified_files": ["foo.py"],
        }
        res = await _initialize_node_impl(state, config=MagicMock())
        assert res["runtime"]["tier"] == "medium"
        assert res["run"]["status"] == "running"
        assert res["run"]["outcome"] == "continue"
        assert res["pending_action"] is None
        assert res["last_action"] is None
        assert res["verification"] is None
        assert res["modified_files"] == ["foo.py"]

    @pytest.mark.asyncio
    async def test_approval_node_digest_integrity(self):
        # 1. Reject empty
        res_empty = await _approval_node_impl({}, config=MagicMock())
        assert res_empty["run"]["status"] == "failed"
        assert "Cannot approve empty" in res_empty["errors"][0]

        # 2. Reject mismatched digest (arguments modified after display)
        pending = {
            "name": "files_write",
            "arguments": {"path": "a.py", "content": "original"},
            "digest": compute_action_digest("files_write", {"path": "a.py", "content": "tampered"}),
        }
        res_tampered = await _approval_node_impl({"pending_action": pending}, config=MagicMock())
        assert res_tampered["run"]["status"] == "cancelled"
        assert "arguments changed after display" in res_tampered["errors"][0]

    @pytest.mark.asyncio
    async def test_compact_node_reduction_and_bounding(self):
        history = [{"role": "user", "content": f"msg {i}"} for i in range(50)]
        state = {
            "history": history,
            "run": {"compactions": 0},
            "runtime": {"policy": {"compaction_limit": 1}},
        }
        res = await _compact_node_impl(state, config=MagicMock())
        assert res["run"]["status"] == "running"
        assert res["run"]["outcome"] == "continue"
        assert len(res["history"]) < len(history)

        # Exceed limit
        state["run"]["compactions"] = 1
        res_exceeded = await _compact_node_impl(state, config=MagicMock())
        assert res_exceeded["run"]["status"] == "failed"
        assert "context_overflow_exhausted" in res_exceeded["errors"]

    def test_no_tool_classification(self):
        # Incomplete (empty)
        assert classify_no_tool_response(task="test", content_text="") == NoToolClassification.INCOMPLETE

        # Refusal
        assert classify_no_tool_response(task="test", content_text="I cannot fulfill this request as an AI.") == NoToolClassification.REFUSAL

        # Clarification
        assert classify_no_tool_response(task="test", content_text="Could you please clarify which file you want to edit?") == NoToolClassification.CLARIFICATION

        # Incomplete (mutating task without modifications)
        assert classify_no_tool_response(task="modify file src/main.py", content_text="I will do it.") == NoToolClassification.INCOMPLETE

        # Answer (informational query)
        assert classify_no_tool_response(task="explain how quicksort works", content_text="Quicksort is a divide-and-conquer algorithm...") == NoToolClassification.ANSWER
