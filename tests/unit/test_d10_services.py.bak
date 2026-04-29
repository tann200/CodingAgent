"""test_d10_services.py — Unit tests for D-10 extracted orchestrator services.

Tests:
  - SessionCostTracker: cost estimation, flush, reset
  - PreviewCoordinator: attach/detach/events
  - ToolExecutionService: permission mode checks, explore mode
  - config_loader: MCP config schema (S3-B)
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import pytest
from unittest.mock import MagicMock, patch


# ── SessionCostTracker ────────────────────────────────────────────────────────


class TestSessionCostTracker:
    def test_initial_state(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        t = SessionCostTracker(working_dir=tmp_path)
        assert t.session_cost_usd == 0.0
        assert t.tool_call_total == 0

    def test_record_tool_call(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        t = SessionCostTracker(working_dir=tmp_path)
        t.record_tool_call("read_file")
        t.record_tool_call("read_file")
        t.record_tool_call("write_file")
        assert t.tool_call_total == 3
        assert t.get_buffer() == {"read_file": 2, "write_file": 1}

    def test_reset_clears_state(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        t = SessionCostTracker(working_dir=tmp_path)
        t.record_tool_call("bash")
        t._session_cost_usd = 0.05
        t.reset()
        assert t.tool_call_total == 0
        assert t.session_cost_usd == 0.0

    def test_flush_writes_usage_json(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        t = SessionCostTracker(working_dir=tmp_path)
        t.record_tool_call("bash", 3)
        t.record_tool_call("read_file", 5)
        t.flush(task_id="abc123")

        usage_path = tmp_path / ".agent-context" / "usage.json"
        assert usage_path.exists()
        data = json.loads(usage_path.read_text())
        assert data["tools"]["bash"]["calls"] == 3
        assert data["tools"]["read_file"]["calls"] == 5
        assert data["last_task_id"] == "abc123"

    def test_flush_is_noop_when_buffer_empty(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        t = SessionCostTracker(working_dir=tmp_path)
        t.flush()  # should not create usage.json
        usage_path = tmp_path / ".agent-context" / "usage.json"
        assert not usage_path.exists()

    def test_flush_accumulates_across_calls(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        t = SessionCostTracker(working_dir=tmp_path)
        t.record_tool_call("read_file", 2)
        t.flush()
        t.reset()
        t.record_tool_call("read_file", 3)
        t.flush()

        usage_path = tmp_path / ".agent-context" / "usage.json"
        data = json.loads(usage_path.read_text())
        # 2 + 3 = 5
        assert data["tools"]["read_file"]["calls"] == 5

    def test_flush_is_idempotent_without_reset(self, tmp_path):
        """ET-1 (vol14): Calling flush() twice without reset() must not double-count.

        After the first flush() the buffer is cleared; the second flush() is a
        no-op that must leave usage.json unchanged.
        """
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        t = SessionCostTracker(working_dir=tmp_path)
        t.record_tool_call("read_file", 3)
        t.flush(task_id="task1")
        t.flush(task_id="task1")  # second flush — buffer already cleared

        usage_path = tmp_path / ".agent-context" / "usage.json"
        data = json.loads(usage_path.read_text())
        # Must still be 3, not 6
        assert data["tools"]["read_file"]["calls"] == 3, (
            "flush() called twice without reset() must not double-count tool calls"
        )

    def test_record_llm_usage_publishes_event(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        bus = MagicMock()
        t = SessionCostTracker(working_dir=tmp_path, event_bus=bus)
        t.record_llm_usage(
            prompt_tokens=1000,
            completion_tokens=200,
            model="gpt-4o-mini",
            task_id="task1",
        )
        bus.publish.assert_called_once()
        event_name, payload = bus.publish.call_args[0]
        assert event_name == "usage.turn_summary"
        assert payload["prompt_tokens"] == 1000
        assert payload["completion_tokens"] == 200

    def test_record_llm_usage_with_cache_tokens_publishes_cache_fields(self, tmp_path):
        """CP-9: cache_creation_tokens and cache_read_tokens are forwarded in the event."""
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        bus = MagicMock()
        t = SessionCostTracker(working_dir=tmp_path, event_bus=bus)
        t.record_llm_usage(
            prompt_tokens=500,
            completion_tokens=100,
            model="claude-3-5-sonnet",
            task_id="task2",
            cache_creation_tokens=200,
            cache_read_tokens=800,
        )
        bus.publish.assert_called_once()
        _, payload = bus.publish.call_args[0]
        assert payload["cache_creation_tokens"] == 200
        assert payload["cache_read_tokens"] == 800

    def test_estimate_cost_includes_cache_tokens(self, tmp_path):
        """CP-9: Fallback cost estimation includes cache write and cache read costs."""
        from src.core.orchestration.session_cost_tracker import SessionCostTracker
        from unittest.mock import patch

        # Force use of the built-in fallback by making provider_context unavailable.
        with patch.dict("sys.modules", {"src.core.inference.provider_context": None}):
            cost_no_cache = SessionCostTracker._estimate_cost(
                prompt_tokens=1000,
                completion_tokens=100,
                model="claude-3-5-sonnet",
            )
            cost_with_cache = SessionCostTracker._estimate_cost(
                prompt_tokens=1000,
                completion_tokens=100,
                model="claude-3-5-sonnet",
                cache_creation_tokens=500,
                cache_read_tokens=1000,
            )
        # cache_creation (500 tokens @ input rate) + cache_read (1000 @ 10% input)
        # both add to cost, so cost_with_cache > cost_no_cache
        assert cost_with_cache > cost_no_cache

    def test_record_llm_usage_defaults_cache_tokens_to_zero(self, tmp_path):
        """CP-9: Omitting cache token args defaults to zero (backwards compatible)."""
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        bus = MagicMock()
        t = SessionCostTracker(working_dir=tmp_path, event_bus=bus)
        t.record_llm_usage(prompt_tokens=100, completion_tokens=50, model="gpt-4o")
        _, payload = bus.publish.call_args[0]
        assert payload["cache_creation_tokens"] == 0
        assert payload["cache_read_tokens"] == 0

    def test_session_cost_accumulates(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        t = SessionCostTracker(working_dir=tmp_path)
        t.record_llm_usage(1000, 100, "gpt-4o-mini")
        t.record_llm_usage(500, 50, "gpt-4o-mini")
        assert t.session_cost_usd > 0.0

    def test_estimate_cost_uses_fallback_table(self, tmp_path):
        from src.core.orchestration.session_cost_tracker import SessionCostTracker

        # Mock missing provider_context
        with patch(
            "src.core.orchestration.session_cost_tracker.SessionCostTracker._estimate_cost"
        ) as mock_est:
            mock_est.return_value = 0.001
            t = SessionCostTracker(working_dir=tmp_path)
            t.record_llm_usage(100, 50, "unknown-model")
            assert t.session_cost_usd == pytest.approx(0.001, rel=1e-5)


# ── PreviewCoordinator ────────────────────────────────────────────────────────


class TestPreviewCoordinator:
    def test_attach_subscribes_to_events(self):
        from src.core.orchestration.preview_coordinator import PreviewCoordinator

        bus = MagicMock()
        coord = PreviewCoordinator()
        coord.attach(bus)
        assert bus.subscribe.call_count == 2
        event_names = {c.args[0] for c in bus.subscribe.call_args_list}
        assert "preview.confirmed" in event_names
        assert "preview.rejected" in event_names

    def test_double_attach_is_noop(self):
        from src.core.orchestration.preview_coordinator import PreviewCoordinator

        bus = MagicMock()
        coord = PreviewCoordinator()
        coord.attach(bus)
        coord.attach(bus)  # second call must not subscribe again
        assert bus.subscribe.call_count == 2  # still only 2

    def test_detach_unsubscribes(self):
        from src.core.orchestration.preview_coordinator import PreviewCoordinator

        bus = MagicMock()
        coord = PreviewCoordinator()
        coord.attach(bus)
        coord.detach()
        assert bus.unsubscribe.call_count == 2
        assert not coord._attached

    def test_on_confirmed_calls_resolve_gate(self):
        from src.core.orchestration.preview_coordinator import PreviewCoordinator

        with patch("src.tools.file_tools.resolve_preview_gate") as mock_resolve:
            coord = PreviewCoordinator()
            coord._on_confirmed({"path": "/workspace/foo.py"})
            mock_resolve.assert_called_once_with("/workspace/foo.py", approved=True)

    def test_on_rejected_calls_resolve_gate(self):
        from src.core.orchestration.preview_coordinator import PreviewCoordinator

        with patch("src.tools.file_tools.resolve_preview_gate") as mock_resolve:
            coord = PreviewCoordinator()
            coord._on_rejected({"path": "/workspace/foo.py"})
            mock_resolve.assert_called_once_with("/workspace/foo.py", approved=False)

    def test_on_confirmed_ignores_import_error(self):
        from src.core.orchestration.preview_coordinator import PreviewCoordinator

        coord = PreviewCoordinator()
        with patch("builtins.__import__", side_effect=ImportError):
            coord._on_confirmed({"path": "x"})  # must not raise


# ── ToolExecutionService ──────────────────────────────────────────────────────


class TestToolExecutionService:
    def _make_service(self):
        from src.core.orchestration.tool_execution_service import ToolExecutionService

        registry = MagicMock()
        event_bus = MagicMock()
        return ToolExecutionService(registry=registry, event_bus=event_bus)

    def test_pre_execute_returns_unblocked_for_safe_tool(self):
        import asyncio

        svc = self._make_service()
        with (
            patch("src.tools.tools_config.get_tool_permission") as mock_perm,
            patch("src.tools.tools_config.is_autonomous", return_value=True),
        ):
            from src.tools.tools_config import PermissionLevel

            mock_perm.return_value = PermissionLevel.READ_ONLY
            with patch(
                "src.tools.tools_config.get_active_permission_mode", return_value=None
            ):
                verdict = asyncio.run(svc.pre_execute("read_file", {"path": "foo.py"}))
                assert not verdict.blocked

    def test_pre_execute_strips_user_approved(self):
        import asyncio

        svc = self._make_service()
        args = {"path": "foo.py", "user_approved": True}
        with (
            patch("src.tools.tools_config.get_tool_permission") as mock_perm,
            patch("src.tools.tools_config.is_autonomous", return_value=True),
            patch(
                "src.tools.tools_config.get_active_permission_mode", return_value=None
            ),
        ):
            from src.tools.tools_config import PermissionLevel

            mock_perm.return_value = PermissionLevel.READ_ONLY
            asyncio.run(svc.pre_execute("read_file", args))
            assert "user_approved" not in args

    def test_check_permission_mode_blocks_danger_in_readonly(self):
        from src.core.orchestration.tool_execution_service import ToolExecutionService

        with (
            patch("src.tools.tools_config.get_active_permission_mode") as mock_mode,
            patch("src.tools.tools_config.get_tool_permission") as mock_perm,
        ):

            class _FakeLevel:
                value = "read_only"

            class _FakeDanger:
                value = "danger"

            mock_mode.return_value = _FakeLevel()
            mock_perm.return_value = _FakeDanger()
            verdict = ToolExecutionService._check_permission_mode("bash")
            assert verdict.blocked
            assert "bash" in verdict.result["error"]  # type: ignore[index]

    def test_check_explore_mode_blocks_write_tool(self):
        from src.core.orchestration.tool_execution_service import ToolExecutionService

        orch = MagicMock()
        orch.explore_mode = True
        with patch(
            "src.core.orchestration.role_config.is_tool_allowed_for_role",
            return_value=False,
        ):
            verdict = ToolExecutionService._check_explore_mode("write_file", orch)
            assert verdict.blocked

    def test_check_explore_mode_allows_read_tool(self):
        from src.core.orchestration.tool_execution_service import ToolExecutionService

        orch = MagicMock()
        orch.explore_mode = True
        with patch(
            "src.core.orchestration.role_config.is_tool_allowed_for_role",
            return_value=True,
        ):
            verdict = ToolExecutionService._check_explore_mode("read_file", orch)
            assert not verdict.blocked

    def test_reset_idempotency(self):
        svc = self._make_service()
        svc._seen_calls.add(("read_file", frozenset()))
        svc.reset_idempotency()
        assert len(svc._seen_calls) == 0

    def test_reset_idempotency_clears_between_tasks(self):
        """ET-2 (vol14): reset_idempotency() must clear _seen_calls so a tool
        call from task N does not block the same call in task N+1.
        """
        import asyncio

        svc = self._make_service()

        with (
            patch("src.tools.tools_config.get_tool_permission") as mock_perm,
            patch("src.tools.tools_config.is_autonomous", return_value=True),
            patch(
                "src.tools.tools_config.get_active_permission_mode", return_value=None
            ),
        ):
            from src.tools.tools_config import PermissionLevel

            mock_perm.return_value = PermissionLevel.READ_ONLY

            # Task 1: pre_execute records the call in _seen_calls
            verdict1 = asyncio.run(svc.pre_execute("read_file", {"path": "foo.py"}))
            assert not verdict1.blocked, "First call should not be blocked"
            assert len(svc._seen_calls) == 1

            # Simulate turn reset
            svc.reset_idempotency()
            assert len(svc._seen_calls) == 0, (
                "reset_idempotency() must clear _seen_calls"
            )

            # Task 2: same call should not be blocked
            verdict2 = asyncio.run(svc.pre_execute("read_file", {"path": "foo.py"}))
            assert not verdict2.blocked, (
                "After reset_idempotency(), the same tool call must not be blocked"
            )


# ── S3-B: MCP config schema ───────────────────────────────────────────────────


class TestMCPConfigSchema:
    def test_get_mcp_config_defaults(self):
        from src.core.config_loader import get_mcp_config

        cfg = get_mcp_config()
        assert "servers" in cfg
        assert isinstance(cfg["servers"], list)
        assert "timeout_seconds" in cfg
        assert cfg["timeout_seconds"] == 30
        assert cfg["auto_register_tools"] is True

    def test_get_mcp_servers_default_empty(self):
        from src.core.config_loader import get_mcp_servers

        servers = get_mcp_servers()
        # Default config has no servers configured
        assert isinstance(servers, list)

    def test_get_mcp_config_workspace_override(self, tmp_path):
        from src.core.config_loader import get_mcp_config

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        (agent_dir / "config.json").write_text(
            json.dumps(
                {
                    "mcp": {
                        "servers": [
                            {
                                "name": "filesystem",
                                "cmd": ["npx", "-y", "@mcp/server-filesystem"],
                            }
                        ],
                        "timeout_seconds": 60,
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = get_mcp_config(working_dir=tmp_path)
        assert len(cfg["servers"]) == 1
        assert cfg["servers"][0]["name"] == "filesystem"
        assert cfg["timeout_seconds"] == 60

    def test_get_mcp_servers_workspace_override(self, tmp_path):
        from src.core.config_loader import get_mcp_servers

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        (agent_dir / "config.json").write_text(
            json.dumps(
                {"mcp": {"servers": [{"name": "test-server", "cmd": ["test"]}]}}
            ),
            encoding="utf-8",
        )
        servers = get_mcp_servers(working_dir=tmp_path)
        assert len(servers) == 1
        assert servers[0]["name"] == "test-server"
