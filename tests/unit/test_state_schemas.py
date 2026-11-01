"""STATE-01: focused node-result schemas + graph-boundary transition enforcement.

Contract tests for ``src/core/orchestration/graph/state_schemas.py``.
Tests the canonical 6-node architecture: initialize, agent_loop, approval, verify, compact, finalize.
"""

import asyncio

import pytest

from src.core.messaging.event_types import NodeResultValidationFailed
from src.core.observability.metrics import metrics
from src.core.orchestration.graph.state_schemas import (
    NodeResultViolation,
    validate_node_result,
    wrap_node,
    get_node_output_schema,
    register_node_output_schema,
)

# ── Representative correct results per scenario path ──

VALID_INITIALIZE = {
    "runtime": {"tier": "frontier", "provider": "mock"},
    "run": {"status": "running", "outcome": "continue"},
    "model_tier": "frontier",
    "errors": [],
}

VALID_AGENT_LOOP = {
    "history": [{"role": "assistant", "content": "Done editing."}],
    "tool_call_count": 2,
    "last_result": {"ok": True},
    "last_tool_name": "edit_file",
    "modified_files": ["src/main.py"],
    "run": {"status": "running", "outcome": "verify"},
}

VALID_APPROVAL = {
    "awaiting_plan_approval": True,
    "awaiting_user_input": True,
    "plan_mode_blocked_tool": "write_file",
    "last_result": {"ok": False, "error": "blocked"},
}

VALID_VERIFY = {
    "verification_result": {"ok": True},
    "verification_passed": True,
    "run": {"status": "running", "outcome": "continue"},
}

VALID_COMPACT = {
    "history": [{"role": "user", "content": "hello"}],
    "run": {"status": "running", "outcome": "continue"},
    "_force_compact": True,
}

VALID_FINALIZE = {
    "run": {"status": "completed", "outcome": "complete"},
    "session_cost_usd": 0.05,
}


class TestSchemaInventory:
    """All 6 canonical nodes have a registered, non-trivial output schema."""

    @pytest.mark.parametrize(
        "node_name,expected_keys,expected_core",
        [
            ("initialize", 8, ("runtime", "run")),
            ("agent_loop", 24, ()),
            ("approval", 12, ()),
            ("verify", 8, ()),
            ("compact", 10, ()),
            ("finalize", 7, ()),
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
            ("initialize", VALID_INITIALIZE),
            ("agent_loop", VALID_AGENT_LOOP),
            ("approval", VALID_APPROVAL),
            ("verify", VALID_VERIFY),
            ("compact", VALID_COMPACT),
            ("finalize", VALID_FINALIZE),
        ],
    )
    def test_correct_result_yields_no_violations(self, node, result):
        assert validate_node_result(node, result) == []


class TestInvalidTransitionsFail:
    """Invalid node->state transitions produce structured violations."""

    def test_unknown_key_is_rejected(self):
        bad = {**VALID_INITIALIZE, "totally_bogus_field": 1}
        violations = validate_node_result("initialize", bad)
        assert len(violations) == 1
        assert violations[0].reason == "unknown_key"
        assert "totally_bogus_field" in violations[0].details[0]

    def test_missing_core_key_is_rejected(self):
        bad = {k: v for k, v in VALID_INITIALIZE.items() if k != "runtime"}
        violations = validate_node_result("initialize", bad)
        assert len(violations) == 1
        assert violations[0].reason == "missing_core_key"
        assert "runtime" in violations[0].details[0]

    def test_non_mapping_result_is_rejected(self):
        violations = validate_node_result("initialize", "not-a-dict")
        assert len(violations) == 1
        assert violations[0].reason == "non_mapping_result"

    def test_strict_wrapper_raises_structured_error(self):
        async def _node(_s, _c):
            return {**VALID_INITIALIZE, "bogus": 1}

        wrapped = wrap_node("initialize", _node, strict=True)
        with pytest.raises(NodeResultViolation) as exc:
            asyncio.run(wrapped({}, {}))
        assert exc.value.node_name == "initialize"
        assert exc.value.reason == "unknown_key"

    def test_strict_wrapper_rejects_missing_core_key(self):
        async def _node(_s, _c):
            return {"model_tier": "frontier"}  # missing runtime + run

        wrapped = wrap_node("initialize", _node, strict=True)
        with pytest.raises(NodeResultViolation) as exc:
            asyncio.run(wrapped({}, {}))
        assert exc.value.reason == "missing_core_key"


class TestFailOpenHeadroom:
    """Non-strict (live default) logs + surfaces violations without raising."""

    def test_fail_open_preserves_result_and_calls_publisher(self):
        captured = []

        async def _node(_s, _c):
            return {**VALID_AGENT_LOOP, "bogus_loop_field": 1}

        wrapped = wrap_node(
            "agent_loop", _node, strict=False, publish_violation=captured.append
        )
        result = asyncio.run(wrapped({}, {}))
        assert result["bogus_loop_field"] == 1  # node result returned unchanged
        assert len(captured) == 1
        assert captured[0].node_name == "agent_loop"
        assert captured[0].reason == "unknown_key"

    def test_fail_open_publishes_typed_event_via_orchestrator(self, monkeypatch):
        published = []

        class FakeBus:
            def publish_typed(self, event):
                published.append(event)

        class FakeOrch:
            event_bus = FakeBus()

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

    def test_fail_open_increments_validation_counter(self, monkeypatch):
        from src.core.orchestration.graph.nodes import node_utils

        monkeypatch.setattr(
            node_utils, "_resolve_orchestrator", lambda state, config: None
        )

        def _counters():
            return metrics.snapshot()["counters"]

        before = _counters()
        total_b = before.get("graph.node_validation_failed", 0)
        node_b = before.get("graph.node_validation_failed.node.verification", 0)
        reason_b = before.get("graph.node_validation_failed.reason.unknown_key", 0)

        async def _node(_s, _c):
            return {"verification_result": {}, "bogus": 1}

        wrapped = wrap_node("verification", _node, strict=False)
        asyncio.run(wrapped({"session_id": "s1"}, {}))

        after = _counters()
        assert (
            after.get("graph.node_validation_failed", 0) == total_b + 1
        ), "total violation counter must increment per violation"
        assert (
            after.get("graph.node_validation_failed.node.verification", 0)
            == node_b + 1
        ), "per-node counter must increment"
        assert (
            after.get("graph.node_validation_failed.reason.unknown_key", 0)
            == reason_b + 1
        ), "per-reason counter must increment"

    def test_valid_result_does_not_increment_counter(self, monkeypatch):
        from src.core.orchestration.graph.nodes import node_utils

        monkeypatch.setattr(
            node_utils, "_resolve_orchestrator", lambda state, config: None
        )

        counters_before = metrics.snapshot()["counters"]
        total_before = counters_before.get("graph.node_validation_failed", 0)

        async def _node(_s, _c):
            return {"verification_result": {}}

        wrapped = wrap_node("verification", _node, strict=False)
        asyncio.run(wrapped({}, {}))

        after = metrics.snapshot()["counters"]
        assert (
            after.get("graph.node_validation_failed", 0) == total_before
        ), "valid result must not increment the violation counter"

    def test_wrapping_unknown_node_is_identity(self):
        def _node(_s, _c):
            return {"anything": 1}

        assert wrap_node("nope", _node) is _node


class TestWrapperSemantics:
    """Wrap preserves sync/async behaviour and return values."""

    @pytest.mark.asyncio
    async def test_async_wrapper_returns_same_value(self):
        async def _node(_s, _c):
            return dict(VALID_VERIFY)

        wrapped = wrap_node("verify", _node, strict=True)
        assert await wrapped({}, {}) == VALID_VERIFY

    def test_sync_wrapper_returns_same_value(self):
        def _node(_s, _c):
            return dict(VALID_INITIALIZE)

        wrapped = wrap_node("initialize", _node, strict=False)
        assert wrapped({}, {}) == VALID_INITIALIZE

    def test_registered_schema_can_be_extended(self):
        register_node_output_schema("custom", type(
            "S", (), {"allowed_keys": frozenset({"a"}), "core_keys": ("a",)}
        )())
        assert validate_node_result("custom", {"a": 1}) == []
        assert validate_node_result("custom", {"b": 1})[0].reason == "unknown_key"


class TestSpec05StrictStateSchemas:
    @pytest.mark.asyncio
    async def test_frontier_loop_retrieved_snippets_and_modified_files_pass_strict_validation(self, monkeypatch, tmp_path):
        from unittest.mock import AsyncMock, patch
        from types import SimpleNamespace
        from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node
        from src.core.orchestration.graph.builder import _validated

        monkeypatch.setenv("CODINGAGENT_STATE_SCHEMAS_STRICT", "1")

        orch = SimpleNamespace(
            working_dir=str(tmp_path),
            _session_modified_files={str(tmp_path / "hello.py")},
            msg_mgr=SimpleNamespace(messages=[]),
            plan_mode=None,
            _current_task_id="t-spec-05",
            _tool_executor=None,
        )

        state = {
            "task": "Explain architecture",
            "history": [],
            "tool_call_count": 0,
            "max_tool_calls": 5,
            "model_tier": "frontier",
            "working_dir": str(tmp_path),
            "retrieved_snippets": [{"file": "arch.md", "snippet": "System architecture overview"}],
            "modified_files": [str(tmp_path / "hello.py")],
            "errors": [],
        }

        wrapped_node = _validated("frontier_loop", frontier_loop_node)

        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(return_value={"choices": [{"message": {"content": "Architecture is modular."}}]}),
            ),
        ):
            result = await wrapped_node(state, {})

        assert result["last_result"]["ok"] is True
        assert result.get("retrieved_snippets") == [{"file": "arch.md", "snippet": "System architecture overview"}]
        assert result.get("modified_files") == [str(tmp_path / "hello.py")]
