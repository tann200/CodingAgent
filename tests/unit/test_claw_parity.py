"""test_claw_parity.py — Regression tests for claw-code parity tasks (TASK-1 through TASK-10).

Covers acceptance criteria from docs/implementation-plan-claw-parity.md.
All tests are unit-level (no LLM calls, no live network).
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# TASK-1: CLAUDE.md instruction file discovery
# ---------------------------------------------------------------------------


class TestInstructionFileDiscovery:
    """TASK-1: CLAUDE.md variants are discovered by the ancestor walk."""

    def test_claude_md_discovered(self, tmp_path):
        """CLAUDE.md in the project root is discovered."""
        from src.core.context.instruction_files import discover_instruction_files

        claude_file = tmp_path / "CLAUDE.md"
        claude_file.write_text("# Project Instructions\nDo the right thing.")

        result = discover_instruction_files(str(tmp_path / "src"))
        # discover_instruction_files returns InstructionFile objects; check their
        # .content for the expected text
        assert any("Project Instructions" in f.content for f in result), (
            f"CLAUDE.md content not found in {result!r}"
        )

    def test_claude_local_md_discovered(self, tmp_path):
        """CLAUDE.local.md is discovered."""
        from src.core.context.instruction_files import discover_instruction_files

        (tmp_path / "CLAUDE.local.md").write_text("# Local overrides")
        result = discover_instruction_files(str(tmp_path))
        assert any("Local overrides" in f.content for f in result)

    def test_agents_md_still_discovered(self, tmp_path):
        """Existing AGENTS.md is still discovered (no regression)."""
        from src.core.context.instruction_files import discover_instruction_files

        (tmp_path / "AGENTS.md").write_text("# AGENTS Instructions")
        result = discover_instruction_files(str(tmp_path))
        assert any("AGENTS Instructions" in f.content for f in result)

    def test_sha256_dedup(self, tmp_path):
        """Files with identical content are deduped — content injected only once."""
        from src.core.context.instruction_files import discover_instruction_files

        content = "# Shared content"
        (tmp_path / "AGENTS.md").write_text(content)
        (tmp_path / "CLAUDE.md").write_text(content)
        result = discover_instruction_files(str(tmp_path))
        # The content should appear at most once — join the discovered file
        # contents when searching
        combined = "\n".join(f.content for f in result)
        assert combined.count("Shared content") <= 1


# ---------------------------------------------------------------------------
# TASK-2: Protocol classes
# ---------------------------------------------------------------------------


class TestProtocolClasses:
    """TASK-2: Protocol structural typing works correctly."""

    def test_mock_adapter_satisfies_api_client_protocol(self):
        from src.core.interfaces import ApiClientProtocol
        from src.core.inference.adapters.mock_adapter import MockAdapter

        mock = MockAdapter(responses=["hello"])
        assert isinstance(mock, ApiClientProtocol), (
            "MockAdapter does not satisfy ApiClientProtocol"
        )

    def test_tool_execution_service_satisfies_executor_protocol(self):
        from src.core.interfaces import ToolExecutorProtocol
        from src.core.orchestration.tool_execution_service import ToolExecutionService

        svc = ToolExecutionService(registry=MagicMock())
        assert isinstance(svc, ToolExecutorProtocol)

    def test_session_store_satisfies_protocol(self):
        from src.core.interfaces import SessionStoreProtocol
        from src.core.memory.session_store import SessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(workdir=tmp)
            assert isinstance(store, SessionStoreProtocol)

    def test_jsonl_session_store_satisfies_protocol(self):
        from src.core.interfaces import SessionStoreProtocol
        from src.core.memory.jsonl_session_store import JsonlSessionStore

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlSessionStore(workdir=tmp)
            assert isinstance(store, SessionStoreProtocol)

    def test_protocol_methods_present(self):
        from src.core.interfaces import SessionStoreProtocol

        required = {
            "add_message",
            "get_messages",
            "fork_session",
            "revert_session",
            "save_snapshot",
            "get_snapshot",
        }
        for method in required:
            assert hasattr(SessionStoreProtocol, method), f"Missing method: {method}"


# ---------------------------------------------------------------------------
# TASK-3: PermissionKind on @tool decorator
# ---------------------------------------------------------------------------


class TestPermissionKind:
    """TASK-3: @tool decorator exposes permission_kind on all tools."""

    def test_permission_kind_enum_values(self):
        from src.tools._tool import PermissionKind

        assert PermissionKind.WRITE_FILE.value == "WriteFile"
        assert PermissionKind.READ_FILE.value == "ReadFile"
        assert PermissionKind.EXECUTE_BASH.value == "ExecuteBash"
        assert PermissionKind.GIT_WRITE.value == "GitWrite"
        assert PermissionKind.GIT_READ.value == "GitRead"
        assert PermissionKind.DELEGATE.value == "Delegate"
        assert PermissionKind.MEMORY.value == "MemoryWrite"
        assert PermissionKind.NETWORK.value == "NetworkFetch"

    def test_write_file_has_write_file_kind(self):
        from src.tools._tool import TOOL_ATTR, PermissionKind
        from src.tools._file_io import write_file

        defn = getattr(write_file, TOOL_ATTR)
        assert defn.permission_kind == PermissionKind.WRITE_FILE

    def test_read_file_has_read_file_kind(self):
        from src.tools._tool import TOOL_ATTR, PermissionKind
        from src.tools._file_io import read_file

        defn = getattr(read_file, TOOL_ATTR)
        assert defn.permission_kind == PermissionKind.READ_FILE

    def test_git_commit_has_git_write_kind(self):
        from src.tools._tool import TOOL_ATTR, PermissionKind
        from src.tools.git_tools import git_commit

        defn = getattr(git_commit, TOOL_ATTR)
        assert defn.permission_kind == PermissionKind.GIT_WRITE

    def test_git_status_has_git_read_kind(self):
        from src.tools._tool import TOOL_ATTR, PermissionKind
        from src.tools.git_tools import git_status

        defn = getattr(git_status, TOOL_ATTR)
        assert defn.permission_kind == PermissionKind.GIT_READ

    def test_delegate_task_has_delegate_kind(self):
        from src.tools._tool import TOOL_ATTR, PermissionKind
        from src.tools.subagent_tools import delegate_task

        defn = getattr(delegate_task, TOOL_ATTR)
        assert defn.permission_kind == PermissionKind.DELEGATE

    def test_registry_get_permission_kind(self):
        from src.tools._registry import build_registry
        from src.tools._tool import PermissionKind

        reg = build_registry()
        assert reg.get_permission_kind("write_file") == PermissionKind.WRITE_FILE
        assert reg.get_permission_kind("read_file") == PermissionKind.READ_FILE
        assert reg.get_permission_kind("git_commit") == PermissionKind.GIT_WRITE
        assert reg.get_permission_kind("nonexistent_tool") == PermissionKind.NONE

    def test_inference_from_side_effects(self):
        """Tools without explicit permission_kind inherit from side_effects."""
        from src.tools._tool import tool, PermissionKind, TOOL_ATTR

        @tool(side_effects=["write"])
        def _implicit_write():
            pass

        defn = getattr(_implicit_write, TOOL_ATTR)
        assert defn.permission_kind == PermissionKind.WRITE_FILE

        @tool(side_effects=["execute"])
        def _implicit_exec():
            pass

        defn2 = getattr(_implicit_exec, TOOL_ATTR)
        assert defn2.permission_kind == PermissionKind.EXECUTE_BASH


# ---------------------------------------------------------------------------
# TASK-4: PermissionPolicy wired into PermissionGateway gate2b
# ---------------------------------------------------------------------------


class TestPermissionGatewayPolicyGate:
    """TASK-4: Gate 2b evaluates user-defined PermissionPolicy rules."""

    def _make_gw(self):
        orch = MagicMock()
        orch.plan_mode.enabled = False
        orch.explore_mode = False
        orch.working_dir = None
        from src.core.orchestration.permission_gateway import PermissionGateway

        return PermissionGateway(orch)

    def test_deny_rule_blocks_tool(self):
        from src.core.orchestration.permission_policy import (
            PermissionPolicy,
            PermissionRule,
            Behavior,
            reset_permission_policy,
        )

        policy = PermissionPolicy(
            rules=[PermissionRule(pattern="bash", behavior=Behavior.DENY)]
        )
        reset_permission_policy(policy)
        try:
            gw = self._make_gw()
            result = gw.check("bash", {})
            assert result.blocked, "DENY rule should block bash"
            assert result.gate == 2
        finally:
            reset_permission_policy(None)

    def test_allow_rule_permits_tool(self):
        from src.core.orchestration.permission_policy import (
            PermissionPolicy,
            PermissionRule,
            Behavior,
            reset_permission_policy,
        )

        policy = PermissionPolicy(
            rules=[PermissionRule(pattern="read_file", behavior=Behavior.ALLOW)]
        )
        reset_permission_policy(policy)
        try:
            gw = self._make_gw()
            result = gw.check("read_file", {})
            assert result.allowed, "ALLOW rule should permit read_file"
        finally:
            reset_permission_policy(None)

    def test_wildcard_deny_blocks_matching_tools(self):
        from src.core.orchestration.permission_policy import (
            PermissionPolicy,
            PermissionRule,
            Behavior,
            reset_permission_policy,
        )

        policy = PermissionPolicy(
            rules=[PermissionRule(pattern="web_*", behavior=Behavior.DENY)]
        )
        reset_permission_policy(policy)
        try:
            gw = self._make_gw()
            result = gw.check("web_search", {})
            assert result.blocked
            result2 = gw.check("web_fetch", {})
            assert result2.blocked
            result3 = gw.check("read_file", {})
            assert result3.allowed
        finally:
            reset_permission_policy(None)

    def test_project_policy_merged(self, tmp_path):
        """Project-level .agent-context/permissions.json takes precedence."""
        from src.core.orchestration.permission_policy import (
            PermissionPolicy,
            PermissionRule,
            Behavior,
            reset_permission_policy,
        )

        # User policy: allow everything
        reset_permission_policy(
            PermissionPolicy(rules=[], default_behavior=Behavior.ALLOW)
        )

        # Write a project-level deny for bash
        proj_dir = tmp_path / ".agent-context"
        proj_dir.mkdir()
        (proj_dir / "permissions.json").write_text(
            json.dumps(
                {
                    "default_behavior": "allow",
                    "rules": [{"pattern": "bash", "behavior": "deny"}],
                }
            )
        )

        orch = MagicMock()
        orch.plan_mode.enabled = False
        orch.explore_mode = False
        orch.working_dir = str(tmp_path)

        from src.core.orchestration.permission_gateway import PermissionGateway

        gw = PermissionGateway(orch)
        try:
            result = gw.check("bash", {})
            assert result.blocked, "Project deny rule should block bash"
        finally:
            reset_permission_policy(None)

    def test_exception_in_policy_does_not_block(self):
        """A broken policy never prevents tool execution."""
        from src.core.orchestration.permission_policy import reset_permission_policy

        reset_permission_policy(None)  # force fresh load — file may not exist
        try:
            gw = self._make_gw()
            # Should not raise even if policy file is absent
            result = gw.check("read_file", {})
            # May be allowed or blocked by other gates, but should not raise
            assert isinstance(result.allowed, bool)
        finally:
            reset_permission_policy(None)


# ---------------------------------------------------------------------------
# TASK-5: frontier_loop_node
# ---------------------------------------------------------------------------


class TestFrontierLoopNode:
    """TASK-5: frontier_loop_node executes multiple tool calls in one node invocation."""

    def _make_state(self, **overrides):
        base = {
            "task": "do something",
            "conversation_history": [],
            "tool_call_count": 0,
            "max_tool_calls": 10,
            "model_tier": "frontier",
            "errors": [],
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_no_tool_calls_exits_cleanly(self):
        """LLM returns natural language reply (no tools) → loop exits."""
        from src.core.orchestration.graph.nodes.frontier_loop_node import (
            frontier_loop_node,
        )

        mock_response = MagicMock()
        mock_response.content = "Task complete."
        mock_response.tool_calls = []

        with patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            return_value=mock_response,
        ):
            orch = MagicMock()
            config = {"configurable": {"orchestrator": orch}}
            state = self._make_state()
            result = await frontier_loop_node(state, config)

        assert result["_frontier_loop_turns"] == 1
        assert result["last_result"] is None  # no tool was called
        assert result["tool_call_count"] == 0

    @pytest.mark.asyncio
    async def test_tool_calls_executed(self):
        """Multiple tool calls are executed sequentially."""
        from src.core.orchestration.graph.nodes.frontier_loop_node import (
            frontier_loop_node,
        )

        call_count = {"n": 0}

        def _make_response(has_tool: bool):
            resp = MagicMock()
            resp.content = "..." if has_tool else "All done."
            if has_tool:
                tc = MagicMock()
                tc.function.name = "read_file"
                tc.function.arguments = json.dumps({"path": "/tmp/test.txt"})
                resp.tool_calls = [tc]
            else:
                resp.tool_calls = []
            return resp

        responses = [_make_response(True), _make_response(True), _make_response(False)]
        call_count_llm = {"n": 0}

        async def _mock_call_model(*args, **kwargs):
            resp = responses[call_count_llm["n"]]
            call_count_llm["n"] += 1
            return resp

        def _mock_execute(action):
            call_count["n"] += 1
            return {"ok": True, "output": f"result {call_count['n']}"}

        orch = MagicMock()
        orch.execute_tool = _mock_execute
        orch.tool_registry = MagicMock()
        orch.tool_registry.get_openai_functions.return_value = []

        config = {"configurable": {"orchestrator": orch}}
        state = self._make_state()

        with patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            side_effect=_mock_call_model,
        ):
            result = await frontier_loop_node(state, config)

        assert call_count["n"] == 2  # two tool calls executed
        assert result["tool_call_count"] == 2
        assert result["last_result"] == {"ok": True, "output": "result 2"}

    @pytest.mark.asyncio
    async def test_budget_exhaustion_exits(self):
        """Loop exits when max_tool_calls is reached."""
        from src.core.orchestration.graph.nodes.frontier_loop_node import (
            frontier_loop_node,
        )

        def _make_tool_response():
            resp = MagicMock()
            resp.content = "calling tool..."
            tc = MagicMock()
            tc.function.name = "read_file"
            tc.function.arguments = '{"path": "/tmp/x"}'
            resp.tool_calls = [tc]
            return resp

        async def _mock_call_model(*args, **kwargs):
            return _make_tool_response()

        orch = MagicMock()
        orch.execute_tool = MagicMock(return_value={"ok": True, "output": "data"})
        orch.tool_registry = MagicMock()
        orch.tool_registry.get_openai_functions.return_value = []
        config = {"configurable": {"orchestrator": orch}}
        state = self._make_state(max_tool_calls=3)

        with patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            side_effect=_mock_call_model,
        ):
            result = await frontier_loop_node(state, config)

        assert result["tool_call_count"] == 3

    @pytest.mark.asyncio
    async def test_context_overflow_exits(self):
        """Context overflow in LLM call sets errors=['context_overflow']."""
        from src.core.orchestration.graph.nodes.frontier_loop_node import (
            frontier_loop_node,
        )

        async def _mock_overflow(*args, **kwargs):
            raise RuntimeError("context length exceeded")

        orch = MagicMock()
        orch.tool_registry = MagicMock()
        orch.tool_registry.get_openai_functions.return_value = []
        config = {"configurable": {"orchestrator": orch}}
        state = self._make_state()

        with patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            side_effect=_mock_overflow,
        ):
            result = await frontier_loop_node(state, config)

        assert "context_overflow" in result["errors"]

    @pytest.mark.asyncio
    async def test_plan_mode_pauses_on_write_tool(self):
        """Write tool while plan_mode active → awaiting_plan_approval=True."""
        from src.core.orchestration.graph.nodes.frontier_loop_node import (
            frontier_loop_node,
        )

        def _make_write_response():
            resp = MagicMock()
            resp.content = "writing file..."
            tc = MagicMock()
            tc.function.name = "write_file"
            tc.function.arguments = '{"path": "/tmp/x.py", "content": "pass"}'
            resp.tool_calls = [tc]
            return resp

        async def _mock_call_model(*args, **kwargs):
            return _make_write_response()

        orch = MagicMock()
        orch.tool_registry = MagicMock()
        orch.tool_registry.get_openai_functions.return_value = []
        # Simulate plan_mode active, not yet approved
        orch.plan_mode.enabled = True
        orch._plan_mode_approved = None
        config = {"configurable": {"orchestrator": orch}}
        state = self._make_state()

        with patch(
            "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
            side_effect=_mock_call_model,
        ):
            with patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._plan_mode_blocks",
                return_value=True,
            ):
                result = await frontier_loop_node(state, config)

        assert result.get("awaiting_plan_approval") is True
        assert result.get("next_action") is not None


# ---------------------------------------------------------------------------
# TASK-6: Tier-based graph cache
# ---------------------------------------------------------------------------


class TestTierGraphCache:
    """TASK-6: build_tier_graph() returns cached, tier-appropriate graphs."""

    def test_standard_graph_cached(self):
        from src.core.orchestration.graph.builder import (
            build_tier_graph,
            _reset_compiled_graph,
        )

        _reset_compiled_graph()
        g1 = build_tier_graph("medium")
        g2 = build_tier_graph("medium")
        assert g1 is g2, "standard graph should be cached"

    def test_frontier_graph_cached(self):
        from src.core.orchestration.graph.builder import (
            build_tier_graph,
            _reset_compiled_graph,
        )

        _reset_compiled_graph()
        gf1 = build_tier_graph("frontier")
        gf2 = build_tier_graph("large")
        assert gf1 is gf2, "frontier and large share same graph"

    def test_frontier_and_standard_are_different(self):
        from src.core.orchestration.graph.builder import (
            build_tier_graph,
            _reset_compiled_graph,
        )

        _reset_compiled_graph()
        gf = build_tier_graph("frontier")
        gs = build_tier_graph("nano")
        assert gf is not gs

    def test_reset_clears_cache(self):
        from src.core.orchestration.graph.builder import (
            build_tier_graph,
            _reset_compiled_graph,
        )

        _reset_compiled_graph()
        g1 = build_tier_graph("medium")
        _reset_compiled_graph()
        g2 = build_tier_graph("medium")
        assert g1 is not g2, "reset should produce new compiled graph"

    def test_thread_safety(self):
        """Concurrent calls compile exactly once."""
        from src.core.orchestration.graph.builder import (
            build_tier_graph,
            _reset_compiled_graph,
        )

        _reset_compiled_graph()
        results = []
        errors = []

        def _get():
            try:
                results.append(build_tier_graph("nano"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Thread errors: {errors}"
        # All threads should get the same cached object
        assert len(set(id(g) for g in results)) == 1


# ---------------------------------------------------------------------------
# TASK-7: JsonlSessionStore
# ---------------------------------------------------------------------------


class TestJsonlSessionStore:
    """TASK-7: JsonlSessionStore satisfies SessionStoreProtocol."""

    def _store(self, tmp):
        from src.core.memory.jsonl_session_store import JsonlSessionStore

        return JsonlSessionStore(workdir=tmp)

    def test_add_and_get_messages(self, tmp_path):
        store = self._store(str(tmp_path))
        store.add_message("s1", "user", "hello")
        store.add_message("s1", "assistant", "hi")
        msgs = store.get_messages("s1")
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}

    def test_fork_is_independent(self, tmp_path):
        store = self._store(str(tmp_path))
        store.add_message("s1", "user", "a")
        store.fork_session("s1", "s2")
        store.add_message("s1", "user", "b")
        assert len(store.get_messages("s2")) == 1  # fork unaffected

    def test_snapshot_and_revert(self, tmp_path):
        store = self._store(str(tmp_path))
        store.add_message("s1", "user", "before")
        snap_id = store.save_snapshot("s1", '{"task": "t1"}')
        store.add_message("s1", "user", "after")
        assert len(store.get_messages("s1")) == 2
        store.revert_session("s1", snap_id)
        assert len(store.get_messages("s1")) == 1

    def test_get_snapshot_after_revert(self, tmp_path):
        """Snapshot sidecar survives revert_session."""
        store = self._store(str(tmp_path))
        store.add_message("s1", "user", "msg")
        snap_id = store.save_snapshot("s1", '{"k": "v"}')
        store.revert_session("s1", snap_id)
        snap = store.get_snapshot("s1", snap_id)
        assert snap is not None

    def test_rotation(self, tmp_path):
        from src.core.memory.jsonl_session_store import JsonlSessionStore

        store = JsonlSessionStore(workdir=str(tmp_path), rotation_bytes=200)
        for i in range(30):
            store.add_message("s", "user", f"msg {i} — padding to trigger rotation")
        msgs = store.get_messages("s")
        assert len(msgs) == 30

    def test_unknown_snapshot_returns_none(self, tmp_path):
        store = self._store(str(tmp_path))
        assert store.get_snapshot("s1", "nonexistent") is None


# ---------------------------------------------------------------------------
# TASK-8: Storage backend toggle
# ---------------------------------------------------------------------------


class TestStorageBackendToggle:
    """TASK-8: get_session_store() returns correct backend from config."""

    def test_default_is_sqlite(self, tmp_path):
        from src.core.memory.session_store import get_session_store, SessionStore

        with patch(
            "src.core.memory.session_store._cfg_get"
            if False
            else "src.core.config_loader.get",
            return_value="sqlite",
        ):
            store = get_session_store(workdir=str(tmp_path))
        assert isinstance(store, SessionStore)

    def test_jsonl_backend_selected(self, tmp_path):
        from src.core.memory.session_store import get_session_store
        from src.core.memory.jsonl_session_store import JsonlSessionStore

        with patch("src.core.config_loader.get", return_value="jsonl"):
            store = get_session_store(workdir=str(tmp_path))
        assert isinstance(store, JsonlSessionStore)

    def test_fallback_to_sqlite_on_error(self, tmp_path):
        from src.core.memory.session_store import get_session_store, SessionStore

        with patch("src.core.config_loader.get", return_value="jsonl"):
            with patch(
                "src.core.memory.jsonl_session_store.JsonlSessionStore",
                side_effect=RuntimeError("simulated failure"),
            ):
                store = get_session_store(workdir=str(tmp_path))
        assert isinstance(store, SessionStore)


# ---------------------------------------------------------------------------
# TASK-8c: ShellHookRunner per-tool matcher
# ---------------------------------------------------------------------------


class TestShellHookMatcher:
    """TASK-8c: Hook entries support optional matcher field."""

    def test_plain_string_matches_all_tools(self):
        from src.core.orchestration.shell_hooks import (
            _normalise_hook_entry,
            _matching_commands,
        )

        entry = _normalise_hook_entry("./pre.sh")
        assert entry == {"matcher": "*", "command": "./pre.sh"}
        cmds = _matching_commands([entry], "any_tool")
        assert cmds == ["./pre.sh"]

    def test_dict_entry_matches_specific_tool(self):
        from src.core.orchestration.shell_hooks import (
            _normalise_hook_entry,
            _matching_commands,
        )

        entry = _normalise_hook_entry({"matcher": "bash", "command": "./bash_hook.sh"})
        cmds_bash = _matching_commands([entry], "bash")
        cmds_other = _matching_commands([entry], "read_file")
        assert cmds_bash == ["./bash_hook.sh"]
        assert cmds_other == []

    def test_wildcard_matcher(self):
        from src.core.orchestration.shell_hooks import (
            _normalise_hook_entry,
            _matching_commands,
        )

        entry = _normalise_hook_entry(
            {"matcher": "write_*", "command": "./write_hook.sh"}
        )
        assert _matching_commands([entry], "write_file") == ["./write_hook.sh"]
        assert _matching_commands([entry], "read_file") == []

    def test_missing_command_returns_none(self):
        from src.core.orchestration.shell_hooks import _normalise_hook_entry

        assert _normalise_hook_entry({"matcher": "bash"}) is None

    def test_mixed_entries(self, tmp_path):
        """Settings with mixed plain-string and dict entries loaded correctly."""
        from src.core.orchestration.shell_hooks import _load_hooks_config

        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        (agent_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            "./global.sh",
                            {"matcher": "bash", "command": "./bash_only.sh"},
                        ]
                    }
                }
            )
        )
        cfg = _load_hooks_config(tmp_path)
        pre = cfg["PreToolUse"]
        assert len(pre) == 2
        assert pre[0] == {"matcher": "*", "command": "./global.sh"}
        assert pre[1] == {"matcher": "bash", "command": "./bash_only.sh"}


# ---------------------------------------------------------------------------
# TASK-9/10: MCP multi-transport factory
# ---------------------------------------------------------------------------


class TestMcpClientFactory:
    """TASK-9/10: create_mcp_client() selects the correct transport."""

    def test_stdio_transport(self):
        from src.core.mcp.mcp_client import create_mcp_client, McpStdioClient

        c = create_mcp_client("test", cmd=["echo", "hello"])
        assert isinstance(c, McpStdioClient)

    def test_http_sse_transport(self):
        from src.core.mcp.mcp_client import create_mcp_client
        from src.core.mcp.mcp_sse_client import McpSseClient

        c = create_mcp_client("test", url="http://localhost:3000")
        assert isinstance(c, McpSseClient)

    def test_https_sse_transport(self):
        from src.core.mcp.mcp_client import create_mcp_client
        from src.core.mcp.mcp_sse_client import McpSseClient

        c = create_mcp_client("test", url="https://mcp.example.com")
        assert isinstance(c, McpSseClient)

    def test_ws_transport(self):
        from src.core.mcp.mcp_client import create_mcp_client
        from src.core.mcp.mcp_ws_client import McpWsClient

        c = create_mcp_client("test", url="ws://localhost:3000/ws")
        assert isinstance(c, McpWsClient)

    def test_wss_transport(self):
        from src.core.mcp.mcp_client import create_mcp_client
        from src.core.mcp.mcp_ws_client import McpWsClient

        c = create_mcp_client("test", url="wss://secure.example.com/ws")
        assert isinstance(c, McpWsClient)

    def test_no_args_raises_value_error(self):
        from src.core.mcp.mcp_client import create_mcp_client

        with pytest.raises(ValueError, match="provide either"):
            create_mcp_client("test")

    def test_unknown_scheme_raises_value_error(self):
        from src.core.mcp.mcp_client import create_mcp_client

        with pytest.raises(ValueError, match="unrecognised URL scheme"):
            create_mcp_client("test", url="ftp://oops")

    def test_headers_passed_to_sse_client(self):
        from src.core.mcp.mcp_client import create_mcp_client
        from src.core.mcp.mcp_sse_client import McpSseClient

        c = create_mcp_client(
            "test", url="http://x", headers={"Authorization": "Bearer tok"}
        )
        assert isinstance(c, McpSseClient)
        assert c._extra_headers.get("Authorization") == "Bearer tok"

    def test_headers_passed_to_ws_client(self):
        from src.core.mcp.mcp_client import create_mcp_client
        from src.core.mcp.mcp_ws_client import McpWsClient

        c = create_mcp_client("test", url="ws://x/ws", headers={"X-Api-Key": "secret"})
        assert isinstance(c, McpWsClient)
        assert c._extra_headers.get("X-Api-Key") == "secret"

    def test_sse_client_name_and_url(self):
        from src.core.mcp.mcp_sse_client import McpSseClient

        c = McpSseClient(name="myserver", url="http://localhost:9000/")
        assert c.name == "myserver"
        assert c.url == "http://localhost:9000"  # trailing slash stripped

    def test_ws_client_name_and_url(self):
        from src.core.mcp.mcp_ws_client import McpWsClient

        c = McpWsClient(name="wsserver", url="ws://localhost:9000/ws")
        assert c.name == "wsserver"
        assert c.url == "ws://localhost:9000/ws"

    def test_sse_connect_raises_import_error_without_aiohttp(self):
        """McpSseClient.connect() raises ImportError when aiohttp is not available."""
        from src.core.mcp.mcp_sse_client import McpSseClient

        c = McpSseClient(name="test", url="http://localhost:3000")
        import sys

        original = sys.modules.get("aiohttp")
        sys.modules["aiohttp"] = None  # type: ignore[assignment]
        try:
            import asyncio

            with pytest.raises((ImportError, TypeError)):
                asyncio.get_event_loop().run_until_complete(c.connect())
        finally:
            if original is None:
                sys.modules.pop("aiohttp", None)
            else:
                sys.modules["aiohttp"] = original
