"""STATE-01: focused node-result schemas + graph-boundary transition enforcement.

Contract tests for ``src/core/orchestration/graph/state_schemas.py``.

Scenario coverage (roadmap acceptance): fast, full, approval, cancellation, and
recovery paths.  Each scenario asserts that a *correct* node result for that path
passes the boundary validator, and that an *invalid* transition (unknown /
missing-core key) on that path fails with a structured ``NodeResultViolation``
(fail-closed) or surfaces a ``NodeResultValidationFailed`` event (fail-open).
"""

import asyncio

import pytest

from src.core.messaging.event_types import NodeResultValidationFailed
from src.core.orchestration.graph.state_schemas import (
    NodeResultViolation,
    validate_node_result,
    wrap_node,
    get_node_output_schema,
    register_node_output_schema,
)


# ── Representative correct results per scenario path (from the node inventory) ──

# Fast-path: perception → execution round-trip (read-only tool call).
FAST_PERCEPTION = {
    "history": [{"role": "assistant", "content": "Checking files."}],
    "next_action": {"name": "list_files", "arguments": {"path": "."}},
    "rounds": 1,
    "turn_count": 1,
    "empty_response_count": 0,
    "errors": [],
    "model_tier": "medium",
    "task_complexity": "simple",
}

# Full-path: analysis → planning → execution → verification chain results.
FULL_PLANNING = {
    "current_plan": [{"step": 1, "action": "edit"}],
    "current_step": 0,
    "plan_attempts": 1,
    "plan_mode_approved": False,
    "task_decomposed": True,
    "plan_dag": {"nodes": [1]},
    "execution_waves": [["1"]],
    "current_wave": 0,
    "affected_files": ["a.py"],
    "relevant_files": ["a.py"],
    "key_symbols": ["Foo"],
}

FULL_EXECUTION = {
    "last_result": {"ok": True},
    "last_tool_name": "write_file",
    "verified_reads": ["a.py"],
    "history": [{"role": "tool"}],
    "next_action": None,
    "tool_call_count": 1,
    "tool_last_used": {},
    "files_read": {"a.py": True},
    "recent_tool_calls": ["write_file"],
    "current_step": 1,
    "current_plan": FULL_PLANNING["current_plan"],
    "task": "edit a.py",
    "snapshots": [],
}

FULL_VERIFICATION = {
    "verification_result": {"ok": True},
    "verification_passed": True,
}

# Approval path: wait_for_user → execution (plan-mode blocked tool).
APPROVAL_EXECUTION = {
    "awaiting_plan_approval": True,
    "awaiting_user_input": True,
    "plan_mode_blocked_tool": "write_file",
    "last_result": {"ok": False, "error": "blocked"},
    "history": [],
    "next_action": None,
}

# Cancellation path: execution/-perception canceled before running.
CANCELLATION_PERCEPTION = {
    "history": [],
    "next_action": None,
    "rounds": 1,
    "last_result": {"ok": False, "error": "cancelled"},
    "errors": ["cancelled"],
    "empty_response_count": 0,
}

# Recovery path: no-tool infinite-loop guard on perception.
RECOVERY_PERCEPTION = {
    "history": [{"role": "assistant", "content": "retrying"}],
    "next_action": None,
    "rounds": 2,
    "last_result": {"ok": False, "error": "infinite loop"},
    "errors": ["infinite_loop_no_tool"],
    "empty_response_count": 0,
}


class TestSchemaInventory:
    """All four target nodes have a registered, non-trivial output schema."""

    @pytest.mark.parametrize(
        "node_name,expected_keys,expected_core",
        [
            ("perception", 21, ("history", "next_action", "rounds")),
            ("planning", 16, ("current_plan", "current_step", "plan_attempts", "plan_mode_approved")),
            ("execution", 31, ()),  # divergent preview path → structural only
            ("verification", 2, ("verification_result",)),
        ],
    )
    def test_known_node_has_schema(self, node_name, expected_keys, expected_core):
        schema = get_node_output_schema(node_name)
        assert schema is not None
        assert len(schema.allowed_keys) == expected_keys
        assert schema.core_keys == expected_core

    def test_unknown_node_has_no_schema(self):
        assert get_node_output_schema("does_not_exist") is None


class TestValidResultsPass:
    """Correct per-scenario results are never rejected (superset allow-lists)."""

    @pytest.mark.parametrize(
        "node,result",
        [
            ("perception", FAST_PERCEPTION),
            ("perception", CANCELLATION_PERCEPTION),
            ("perception", RECOVERY_PERCEPTION),
            ("planning", FULL_PLANNING),
            ("execution", FULL_EXECUTION),
            ("execution", APPROVAL_EXECUTION),
            ("verification", FULL_VERIFICATION),
        ],
    )
    def test_correct_result_yields_no_violations(self, node, result):
        assert validate_node_result(node, result) == []


class TestInvalidTransitionsFail:
    """Invalid node->state transitions produce structured violations."""

    def test_unknown_key_is_rejected(self):
        bad = {**FAST_PERCEPTION, "totally_bogus_field": 1}
        violations = validate_node_result("perception", bad)
        assert len(violations) == 1
        assert violations[0].reason == "unknown_key"
        assert "totally_bogus_field" in violations[0].details[0]

    def test_missing_core_key_is_rejected(self):
        bad = {k: v for k, v in FAST_PERCEPTION.items() if k != "next_action"}
        violations = validate_node_result("perception", bad)
        assert len(violations) == 1
        assert violations[0].reason == "missing_core_key"
        assert "next_action" in violations[0].details[0]

    def test_non_mapping_result_is_rejected(self):
        violations = validate_node_result("perception", "not-a-dict")
        assert len(violations) == 1
        assert violations[0].reason == "non_mapping_result"

    def test_strict_wrapper_raises_structured_error(self):
        async def _node(_s, _c):
            return {**FAST_PERCEPTION, "bogus": 1}

        wrapped = wrap_node("perception", _node, strict=True)
        with pytest.raises(NodeResultViolation) as exc:
            asyncio.run(wrapped({}, {}))
        assert exc.value.node_name == "perception"
        assert exc.value.reason == "unknown_key"

    def test_strict_wrapper_rejects_missing_core_key(self):
        async def _node(_s, _c):
            return {"history": []}  # missing next_action + rounds

        wrapped = wrap_node("perception", _node, strict=True)
        with pytest.raises(NodeResultViolation) as exc:
            asyncio.run(wrapped({}, {}))
        assert exc.value.reason == "missing_core_key"


class TestFailOpenHeadroom:
    """Non-strict (live default) logs + surfaces violations without raising."""

    def test_fail_open_preserves_result_and_calls_publisher(self):
        captured = []
        async def _node(_s, _c):
            return {**FULL_PLANNING, "bogus_planning": 1}

        wrapped = wrap_node(
            "planning", _node, strict=False, publish_violation=captured.append
        )
        result = asyncio.run(wrapped({}, {}))
        assert result["bogus_planning"] == 1  # node result returned unchanged
        assert len(captured) == 1
        assert captured[0].node_name == "planning"
        assert captured[0].reason == "unknown_key"

    def test_fail_open_publishes_typed_event_via_orchestrator(self, monkeypatch):
        published = []

        class FakeBus:
            def publish_typed(self, event):
                published.append(event)

        class FakeOrch:
            event_bus = FakeBus()

        import src.core.orchestration.graph.state_schemas as schemas
        from src.core.orchestration.graph.nodes import node_utils

        monkeypatch.setattr(
            node_utils, "_resolve_orchestrator", lambda state, config: FakeOrch()
        )

        async def _node(_s, _c):
            return {"verification_result": {}, "bogus": 1}

        wrapped = wrap_node("verification", _node, strict=False)
        asyncio.run(wrapped({"session_id": "s1"}, {}))
        assert len(published) == 1
        event = published[0]
        assert isinstance(event, NodeResultValidationFailed)
        assert event.node_name == "verification"
        assert event.reason == "unknown_key"
        assert event.session_id == "s1"

    def test_wrapping_unknown_node_is_identity(self):
        def _node(_s, _c):
            return {"anything": 1}

        assert wrap_node("nope", _node) is _node


class TestWrapperSemantics:
    """Wrap preserves sync/async behaviour and return values."""

    @pytest.mark.asyncio
    async def test_async_wrapper_returns_same_value(self):
        async def _node(_s, _c):
            return dict(FULL_VERIFICATION)

        wrapped = wrap_node("verification", _node, strict=True)
        assert await wrapped({}, {}) == FULL_VERIFICATION

    def test_sync_wrapper_returns_same_value(self):
        def _node(_s, _c):
            return dict(FAST_PERCEPTION)

        wrapped = wrap_node("perception", _node, strict=False)
        assert wrapped({}, {}) == FAST_PERCEPTION

    def test_registered_schema_can_be_extended(self):
        register_node_output_schema("custom", type(
            "S", (), {"allowed_keys": frozenset({"a"}), "core_keys": ("a",)}
        )())
        assert validate_node_result("custom", {"a": 1}) == []
        assert validate_node_result("custom", {"b": 1})[0].reason == "unknown_key"
