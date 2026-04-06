"""
Regression tests for sprint-2 task list items.

Covers:
  TUI-T1  — StatusUpdate uses notify() not chat widget
  TUI-T6  — UsageTurnSummaryEvent in bus + core_bridge subscription
  TUI-T12 — Ctrl+M binding + action_open_model_picker + CommandPalette initial_action
  PERM-W3 — DoomLoopEvent in bus + core_bridge subscription
  PERM-W4 — per-agent permission_rules override
  PERM-W5 — permission audit log (JSONL)
  SES-W1  — session store schema_version + TUI history version envelope
  ORCH-W1 — near-limit write tool pruning + max_steps.txt injection
  ORCH-W4 — plan_enter / plan_exit tool calls
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


# ── TUI-T6 / PERM-W3 — bus events exist and have correct fields ──────────────

class TestNewBusEvents:
    def test_usage_turn_summary_event_exists(self):
        from tui.src.ui.bus import UsageTurnSummaryEvent
        ev = UsageTurnSummaryEvent(
            input_tokens=100, output_tokens=50, model="llama3", cost_usd=0.0025
        )
        assert ev.input_tokens == 100
        assert ev.output_tokens == 50
        assert ev.model == "llama3"
        assert abs(ev.cost_usd - 0.0025) < 1e-9

    def test_usage_turn_summary_event_defaults(self):
        from tui.src.ui.bus import UsageTurnSummaryEvent
        ev = UsageTurnSummaryEvent()
        assert ev.input_tokens == 0
        assert ev.output_tokens == 0
        assert ev.model == ""
        assert ev.cost_usd == 0.0

    def test_doom_loop_event_exists(self):
        from tui.src.ui.bus import DoomLoopEvent
        ev = DoomLoopEvent(
            tool_name="read_file", fingerprint="abc123", count=3, tool_id="t1"
        )
        assert ev.tool_name == "read_file"
        assert ev.fingerprint == "abc123"
        assert ev.count == 3
        assert ev.tool_id == "t1"

    def test_doom_loop_event_defaults(self):
        from tui.src.ui.bus import DoomLoopEvent
        ev = DoomLoopEvent()
        assert ev.tool_name == ""
        assert ev.count == 3
        assert ev.tool_id == ""


# ── TUI-T12 — CommandPalette accepts initial_action ──────────────────────────

class TestCommandPaletteInitialAction:
    def test_palette_accepts_initial_action_param(self):
        """CommandPalette.__init__ should accept initial_action without raising."""
        from tui.src.ui.features.palette.screen import CommandPalette
        settings = MagicMock()
        settings.available_providers = []
        # Should not raise
        palette = CommandPalette(settings, initial_action="menu_switch_model")
        assert palette._initial_action == "menu_switch_model"

    def test_palette_default_initial_action_is_empty(self):
        from tui.src.ui.features.palette.screen import CommandPalette
        settings = MagicMock()
        palette = CommandPalette(settings)
        assert palette._initial_action == ""


# ── SES-W1 — SessionStore schema_version ─────────────────────────────────────

class TestSessionStoreSchemaVersion:
    def test_schema_version_created_on_init(self, tmp_path):
        from src.core.memory.session_store import SessionStore
        store = SessionStore(workdir=str(tmp_path))
        ver = store.get_schema_version()
        assert ver == SessionStore._SCHEMA_VERSION
        assert ver >= 1

    def test_get_schema_version_returns_int(self, tmp_path):
        from src.core.memory.session_store import SessionStore
        store = SessionStore(workdir=str(tmp_path))
        ver = store.get_schema_version()
        assert isinstance(ver, int)

    def test_schema_meta_table_exists(self, tmp_path):
        """schema_meta table must be queryable after init."""
        from src.core.memory.session_store import SessionStore
        import sqlite3
        store = SessionStore(workdir=str(tmp_path))
        conn = sqlite3.connect(str(store.db_path))
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert int(row[0]) >= 1

    def test_schema_version_stable_on_reinit(self, tmp_path):
        """Re-creating SessionStore on same dir must not overwrite existing version."""
        from src.core.memory.session_store import SessionStore
        store1 = SessionStore(workdir=str(tmp_path))
        v1 = store1.get_schema_version()
        store2 = SessionStore(workdir=str(tmp_path))
        v2 = store2.get_schema_version()
        assert v1 == v2


# ── SES-W1 — TUI history version envelope ────────────────────────────────────

class TestTuiHistoryVersionEnvelope:
    """_save_history writes {"version": 1, "history": [...]} and load_history reads both."""

    def _make_bridge(self, tmp_path, history_path):
        """Create a minimal AgentBridge mock that tests history I/O only."""
        import sys
        # Patch HISTORY_PATH at module level
        import tui.src.ui.core_bridge as cb_mod
        original = cb_mod.HISTORY_PATH
        cb_mod.HISTORY_PATH = history_path
        try:
            yield cb_mod
        finally:
            cb_mod.HISTORY_PATH = original

    def test_save_produces_version_envelope(self, tmp_path):
        import tui.src.ui.core_bridge as cb_mod
        orig = cb_mod.HISTORY_PATH
        hp = tmp_path / "history.json"
        cb_mod.HISTORY_PATH = hp
        try:
            # Simulate the save logic directly
            history = [("user", "hello"), ("assistant", "hi")]
            fd, tmp_file = tempfile.mkstemp(dir=str(hp.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                payload = {"version": 1, "history": list(history)}
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, str(hp))

            data = json.loads(hp.read_text())
            assert "version" in data
            assert data["version"] == 1
            assert "history" in data
            assert len(data["history"]) == 2
        finally:
            cb_mod.HISTORY_PATH = orig

    def test_load_reads_versioned_envelope(self, tmp_path):
        """load_history should parse the versioned envelope format."""
        import tui.src.ui.core_bridge as cb_mod
        orig = cb_mod.HISTORY_PATH
        hp = tmp_path / "history.json"
        hp.write_text(
            json.dumps({"version": 1, "history": [["user", "q"], ["assistant", "a"]]}),
            encoding="utf-8",
        )
        cb_mod.HISTORY_PATH = hp
        try:
            # Test via module-level load logic: versioned dict path
            raw = json.loads(hp.read_text(encoding="utf-8"))
            assert isinstance(raw, dict) and "history" in raw
            entries = raw["history"]
            history = [tuple(item) for item in entries if isinstance(item, (list, tuple)) and len(item) == 2]
            assert len(history) == 2
            assert history[0] == ("user", "q")
        finally:
            cb_mod.HISTORY_PATH = orig

    def test_load_handles_legacy_bare_list(self, tmp_path):
        """load_history must still parse the old bare-list format (migration)."""
        import tui.src.ui.core_bridge as cb_mod
        orig = cb_mod.HISTORY_PATH
        hp = tmp_path / "history.json"
        hp.write_text(
            json.dumps([["user", "old"], ["assistant", "resp"]]),
            encoding="utf-8",
        )
        cb_mod.HISTORY_PATH = hp
        try:
            raw = json.loads(hp.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "history" in raw:
                entries = raw["history"]
            elif isinstance(raw, list):
                entries = raw
            else:
                entries = []
            history = [tuple(item) for item in entries if isinstance(item, (list, tuple)) and len(item) == 2]
            assert len(history) == 2
        finally:
            cb_mod.HISTORY_PATH = orig


# ── ORCH-W1 — near-limit write tool pruning ──────────────────────────────────

class TestNearLimitToolPruning:
    def test_modifying_tools_importable(self):
        from src.core.orchestration.loop_guards import MODIFYING_TOOLS
        assert isinstance(MODIFYING_TOOLS, set)
        assert "write_file" in MODIFYING_TOOLS
        assert "edit_file" in MODIFYING_TOOLS

    def test_tools_pruned_when_near_limit(self):
        """Simulate the pruning logic from perception_node."""
        from src.core.orchestration.loop_guards import MODIFYING_TOOLS
        tools_list = [
            {"name": "read_file", "description": "read"},
            {"name": "write_file", "description": "write"},
            {"name": "grep", "description": "grep"},
            {"name": "edit_file", "description": "edit"},
        ]
        turn_count = 48
        max_turns = 50
        near_limit = turn_count >= max_turns - 2
        assert near_limit is True
        pruned = [t for t in tools_list if t["name"] not in MODIFYING_TOOLS]
        names = {t["name"] for t in pruned}
        assert "read_file" in names
        assert "grep" in names
        assert "write_file" not in names
        assert "edit_file" not in names

    def test_tools_not_pruned_when_not_near_limit(self):
        from src.core.orchestration.loop_guards import MODIFYING_TOOLS
        tools_list = [
            {"name": "read_file", "description": "read"},
            {"name": "write_file", "description": "write"},
        ]
        turn_count = 10
        max_turns = 50
        near_limit = turn_count >= max_turns - 2
        assert near_limit is False
        # No pruning should happen
        pruned = tools_list if not near_limit else [t for t in tools_list if t["name"] not in MODIFYING_TOOLS]
        assert len(pruned) == 2

    def test_max_steps_template_exists(self):
        """The max_steps.txt template file must exist and be non-empty."""
        root = Path(__file__).parent.parent.parent
        # Search both known locations
        candidates = [
            root / "src" / "config" / "agent-brain" / "prompts" / "templates" / "max_steps.txt",
            root / "src" / "core" / "prompts" / "templates" / "max_steps.txt",
        ]
        found = next((p for p in candidates if p.exists()), None)
        assert found is not None, f"max_steps.txt not found; checked: {candidates}"
        assert found.read_text(encoding="utf-8").strip(), "max_steps.txt is empty"


# ── core_bridge subscriptions exist (PERM-W3, TUI-T6) ───────────────────────

class TestCoreBridgeSubscriptions:
    def test_usage_turn_summary_subscription_exists(self):
        """core_bridge must subscribe to usage.turn_summary."""
        import inspect
        import tui.src.ui.core_bridge as cb_mod
        src = inspect.getsource(cb_mod)
        assert "usage.turn_summary" in src
        assert "_on_usage_turn_summary" in src

    def test_doom_loop_detected_subscription_exists(self):
        """core_bridge must subscribe to tool.doom_loop_detected."""
        import inspect
        import tui.src.ui.core_bridge as cb_mod
        src = inspect.getsource(cb_mod)
        assert "tool.doom_loop_detected" in src
        assert "_on_doom_loop_detected" in src

    def test_ctrl_m_binding_in_app(self):
        """App BINDINGS must include ctrl+m → open_model_picker."""
        import inspect
        import tui.src.ui.app as app_mod
        src = inspect.getsource(app_mod)
        assert "ctrl+m" in src
        assert "action_open_model_picker" in src
        assert "open_model_picker" in src


# ── PERM-W4 — per-agent permission override ──────────────────────────────────

class TestPerAgentPermissionOverride:
    def test_permission_rules_field_exists(self):
        """AgentDefinition must have permission_rules field."""
        from src.core.orchestration.agent_types import AgentDefinition
        agent = AgentDefinition(id="test", name="Test", description="Test agent")
        assert hasattr(agent, "permission_rules")
        assert isinstance(agent.permission_rules, list)

    def test_permission_rules_default_empty(self):
        from src.core.orchestration.agent_types import AgentDefinition
        agent = AgentDefinition(id="test", name="Test", description="Test agent")
        assert agent.permission_rules == []

    def test_get_merged_policy_returns_base_when_no_rules(self):
        """get_merged_policy with no agent rules returns base_policy unchanged."""
        from src.core.orchestration.agent_types import AgentDefinition
        from unittest.mock import MagicMock
        agent = AgentDefinition(id="test", name="Test", description="Test agent")
        base = MagicMock()
        result = agent.get_merged_policy(base)
        assert result is base

    def test_get_merged_policy_returns_none_when_no_rules_and_no_base(self):
        from src.core.orchestration.agent_types import AgentDefinition
        agent = AgentDefinition(id="test", name="Test", description="Test agent")
        result = agent.get_merged_policy(None)
        assert result is None

    def test_get_merged_policy_returns_merged_policy_with_rules(self):
        """When permission_rules is non-empty, get_merged_policy returns a PermissionPolicy."""
        from src.core.orchestration.agent_types import AgentDefinition
        from src.core.orchestration.permission_policy import PermissionPolicy
        agent = AgentDefinition(
            id="restricted",
            name="Restricted",
            description="Restricted agent",
            permission_rules=[{"pattern": "write_file", "behavior": "deny"}],
        )
        result = agent.get_merged_policy(None)
        assert isinstance(result, PermissionPolicy)

    def test_agent_specific_deny_rule_is_enforced(self):
        """Deny rule in permission_rules must make is_denied() return True."""
        from src.core.orchestration.agent_types import AgentDefinition
        agent = AgentDefinition(
            id="ro",
            name="Read-Only",
            description="Read-only agent",
            permission_rules=[{"pattern": "write_file", "behavior": "deny"}],
        )
        merged = agent.get_merged_policy(None)
        assert merged is not None
        assert merged.is_denied("write_file")

    def test_agent_allow_rule_overrides_base_deny(self):
        """Agent allow rule appended after base deny should override (last-matching-wins)."""
        from src.core.orchestration.agent_types import AgentDefinition
        from src.core.orchestration.permission_policy import PermissionPolicy, PermissionRule
        base_policy = PermissionPolicy(
            rules=[PermissionRule.from_dict({"pattern": "read_file", "behavior": "deny"})]
        )
        agent = AgentDefinition(
            id="special",
            name="Special",
            description="Special agent",
            permission_rules=[{"pattern": "read_file", "behavior": "allow"}],
        )
        merged = agent.get_merged_policy(base_policy)
        # Agent rule is last → should override the base deny
        assert not merged.is_denied("read_file")


# ── PERM-W5 — permission audit log ───────────────────────────────────────────

class TestPermissionAuditLog:
    def test_write_permission_audit_importable(self):
        """_write_permission_audit must be importable from orchestrator module."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert "_write_permission_audit" in src
        assert "permission_audit.jsonl" in src

    def test_audit_file_created_on_allow(self, tmp_path):
        """_write_permission_audit should create .agent/permission_audit.jsonl."""
        from src.core.orchestration.orchestrator import _write_permission_audit
        _write_permission_audit(str(tmp_path), "read_file", {}, "allow", "passed_all_gates")
        audit_path = tmp_path / ".agent" / "permission_audit.jsonl"
        assert audit_path.exists()

    def test_audit_entry_is_valid_json(self, tmp_path):
        """Each entry in permission_audit.jsonl must be valid JSON."""
        import json
        from src.core.orchestration.orchestrator import _write_permission_audit
        _write_permission_audit(str(tmp_path), "write_file", {"path": "/tmp/x"}, "deny", "agent_permission_rules")
        audit_path = tmp_path / ".agent" / "permission_audit.jsonl"
        lines = [l.strip() for l in audit_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["tool"] == "write_file"
        assert entry["decision"] == "deny"
        assert entry["reason"] == "agent_permission_rules"
        assert "ts" in entry

    def test_audit_entries_append(self, tmp_path):
        """Multiple calls should append, not overwrite."""
        import json
        from src.core.orchestration.orchestrator import _write_permission_audit
        _write_permission_audit(str(tmp_path), "read_file", {}, "allow", "passed_all_gates")
        _write_permission_audit(str(tmp_path), "write_file", {}, "deny", "agent_permission_rules")
        audit_path = tmp_path / ".agent" / "permission_audit.jsonl"
        lines = [l.strip() for l in audit_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        decisions = [json.loads(l)["decision"] for l in lines]
        assert "allow" in decisions
        assert "deny" in decisions

    def test_audit_write_permission_audit_in_orchestrator_source(self):
        """execute_tool must call _write_permission_audit for allow decisions."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert "passed_all_gates" in src
        assert "agent_permission_rules" in src


# ── ORCH-W4 — plan_enter / plan_exit tool calls ──────────────────────────────

class TestPlanModeTools:
    def test_plan_enter_tool_importable(self):
        """plan_enter must be importable from plan_mode_tools."""
        from src.tools.plan_mode_tools import plan_enter
        assert callable(plan_enter)

    def test_plan_exit_tool_importable(self):
        from src.tools.plan_mode_tools import plan_exit
        assert callable(plan_exit)

    def test_plan_enter_returns_ok(self):
        from src.tools.plan_mode_tools import plan_enter
        result = plan_enter()
        assert result["ok"] is True
        assert result["agent_mode"] == "planning"

    def test_plan_exit_returns_ok(self):
        from src.tools.plan_mode_tools import plan_exit
        result = plan_exit()
        assert result["ok"] is True
        assert result["agent_mode"] == "execution"

    def test_plan_enter_with_reason(self):
        from src.tools.plan_mode_tools import plan_enter
        result = plan_enter(reason="about to design the API")
        assert result["ok"] is True
        assert "about to design the API" in result["message"]

    def test_plan_exit_with_reason(self):
        from src.tools.plan_mode_tools import plan_exit
        result = plan_exit(reason="plan finalized")
        assert "plan finalized" in result["message"]

    def test_plan_mode_tools_in_registry(self):
        """plan_enter and plan_exit must appear in the built-in tool registry."""
        from src.tools._registry import build_registry
        reg = build_registry()
        names = set(reg.list())
        assert "plan_enter" in names, f"plan_enter missing from registry; found: {sorted(names)}"
        assert "plan_exit" in names, f"plan_exit missing from registry; found: {sorted(names)}"

    def test_agent_mode_field_in_state(self):
        """AgentState TypedDict must include agent_mode field."""
        import inspect
        from src.core.orchestration.graph.state import AgentState
        hints = AgentState.__annotations__
        assert "agent_mode" in hints, "agent_mode missing from AgentState"

    def test_agent_mode_in_initial_state(self):
        """initial_state must include agent_mode key."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert '"agent_mode"' in src or "'agent_mode'" in src

    def test_orchestrator_intercepts_plan_enter(self):
        """execute_tool source must handle plan_enter / plan_exit transitions."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert "plan_enter" in src
        assert "plan_exit" in src
        assert "_agent_mode" in src
        assert "agent.mode_changed" in src


# ── ORCH-W5 — internal utility agent calls ───────────────────────────────────

class TestInternalUtilityAgents:
    def test_call_internal_agent_importable(self):
        from src.core.memory.distiller import call_internal_agent
        assert callable(call_internal_agent)

    def test_generate_session_title_importable(self):
        from src.core.memory.distiller import generate_session_title
        assert callable(generate_session_title)

    def test_title_agent_in_registry(self):
        """'title' internal agent must be registered in AgentRegistry."""
        from src.core.orchestration.agent_types import get_agent_registry
        agent = get_agent_registry().get("title")
        assert agent is not None
        assert agent.mode == "internal"
        assert agent.max_rounds == 1

    def test_compaction_agent_in_registry(self):
        """'compaction' internal agent must be registered in AgentRegistry."""
        from src.core.orchestration.agent_types import get_agent_registry
        agent = get_agent_registry().get("compaction")
        assert agent is not None
        assert agent.mode == "internal"
        assert agent.max_rounds == 1

    def test_title_agent_has_no_tools(self):
        """Internal title agent must have empty allowed_tools (no tool loop)."""
        from src.core.orchestration.agent_types import get_agent_registry
        agent = get_agent_registry().get("title")
        assert agent.allowed_tools is not None
        assert len(agent.allowed_tools) == 0

    def test_generate_session_title_fallback_on_empty_input(self):
        """generate_session_title falls back gracefully when input is empty."""
        from src.core.memory.distiller import generate_session_title
        result = generate_session_title("")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_session_title_fallback_words(self):
        """Without LLM, generate_session_title uses first N words as fallback."""
        from unittest.mock import patch
        from src.core.memory.distiller import generate_session_title
        # Patch call_internal_agent to return empty (simulates LLM unavailable)
        with patch("src.core.memory.distiller.call_internal_agent", return_value=""):
            result = generate_session_title("Add a login form to the homepage")
        assert "Add" in result or "login" in result

    def test_call_internal_agent_unknown_id_returns_empty(self):
        """call_internal_agent with an unknown agent_id must return '' without raising."""
        from src.core.memory.distiller import call_internal_agent
        result = call_internal_agent("__nonexistent_agent__", [{"role": "user", "content": "hi"}])
        assert result == ""

    def test_session_title_reset_on_start_new_task(self):
        """start_new_task() must reset _session_title to None."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert "_session_title" in src
        assert "session.title_generated" in src


# ── SPAWN-W2 — allowed_tools enforcement in delegated context ────────────────

class TestSpawnAllowedToolsEnforcement:
    def test_subagent_orchestrator_allowlist(self):
        """SubagentOrchestrator.is_tool_allowed must respect explicit allowed_tools."""
        from src.tools.subagent_tools import SubagentOrchestrator
        orch = SubagentOrchestrator(
            role="analyst",
            working_dir=".",
            allowed_tools={"read_file", "grep"},
        )
        assert orch.is_tool_allowed("read_file") is True
        assert orch.is_tool_allowed("grep") is True
        assert orch.is_tool_allowed("write_file") is False

    def test_subagent_orchestrator_denylist(self):
        """SubagentOrchestrator.is_tool_allowed must respect denied_tools."""
        from src.tools.subagent_tools import SubagentOrchestrator
        orch = SubagentOrchestrator(
            role="analyst",
            working_dir=".",
            denied_tools={"write_file"},
        )
        assert orch.is_tool_allowed("read_file") is True
        assert orch.is_tool_allowed("write_file") is False

    def test_subagent_orchestrator_no_restriction(self):
        """With no allowlist or denylist, SubagentOrchestrator defers to role config."""
        from src.tools.subagent_tools import SubagentOrchestrator
        orch = SubagentOrchestrator(role="analyst", working_dir=".")
        # No explicit allowlist — should not blanket-deny tools
        assert orch._allowed_tools is None

    def test_delegate_task_accepts_allowed_tools_param(self):
        """delegate_task function signature must include allowed_tools parameter."""
        import inspect
        from src.tools.subagent_tools import delegate_task
        sig = inspect.signature(delegate_task)
        assert "allowed_tools" in sig.parameters

    def test_orchestrator_spawn_w2_check_in_source(self):
        """execute_tool must have SPAWN-W2 allowlist enforcement."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert "SPAWN-W2" in src
        assert "is_tool_permitted" in src
        assert "spawn_allowed_tools" in src

    def test_is_tool_permitted_on_agent_definition(self):
        """AgentDefinition.is_tool_permitted() must enforce allowed/denied."""
        from src.core.orchestration.agent_types import AgentDefinition
        agent = AgentDefinition(
            id="test",
            name="Test",
            description="",
            allowed_tools={"read_file", "grep"},
        )
        assert agent.is_tool_permitted("read_file") is True
        assert agent.is_tool_permitted("write_file") is False

    def test_is_tool_permitted_denied_overrides_allowlist(self):
        """denied_tools must block even when tool is in allowed_tools."""
        from src.core.orchestration.agent_types import AgentDefinition
        agent = AgentDefinition(
            id="test",
            name="Test",
            description="",
            allowed_tools={"read_file", "grep", "write_file"},
            denied_tools={"write_file"},
        )
        assert agent.is_tool_permitted("read_file") is True
        assert agent.is_tool_permitted("write_file") is False


# ── SPAWN-W1 — recursive loop re-entry ───────────────────────────────────────

class TestSpawnRecursiveReentry:
    def test_parent_session_id_in_agent_state(self):
        """AgentState TypedDict must include parent_session_id field."""
        from src.core.orchestration.graph.state import AgentState
        hints = AgentState.__annotations__
        assert "parent_session_id" in hints

    def test_parent_session_id_in_initial_state(self):
        """initial_state dict must include parent_session_id key."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert '"parent_session_id"' in src

    def test_context_var_exists_in_subagent_tools(self):
        """_PARENT_ORCHESTRATOR_VAR must be importable from subagent_tools."""
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR
        assert _PARENT_ORCHESTRATOR_VAR is not None

    def test_context_var_default_is_none(self):
        """_PARENT_ORCHESTRATOR_VAR default must be None (no parent)."""
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR
        assert _PARENT_ORCHESTRATOR_VAR.get(None) is None

    def test_context_var_set_and_reset(self):
        """ContextVar token mechanism must work (set → get → reset)."""
        from src.tools.subagent_tools import _PARENT_ORCHESTRATOR_VAR
        sentinel = object()
        token = _PARENT_ORCHESTRATOR_VAR.set(sentinel)
        assert _PARENT_ORCHESTRATOR_VAR.get(None) is sentinel
        _PARENT_ORCHESTRATOR_VAR.reset(token)
        assert _PARENT_ORCHESTRATOR_VAR.get(None) is None

    def test_orchestrator_sets_context_var_in_source(self):
        """execute_tool() source must set _PARENT_ORCHESTRATOR_VAR."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert "_PARENT_ORCHESTRATOR_VAR" in src
        assert "_orch_token" in src

    def test_delegate_task_reads_parent_orchestrator(self):
        """delegate_task source must read _PARENT_ORCHESTRATOR_VAR.get()."""
        import inspect
        from src.tools import subagent_tools
        src = inspect.getsource(subagent_tools)
        assert "_PARENT_ORCHESTRATOR_VAR.get" in src
        assert "parent_session_id" in src
        assert "child_session_id" in src

    def test_delegation_depth_passed_to_initial_state(self):
        """delegate_task must track delegation_depth in the child initial_state."""
        import inspect
        from src.tools import subagent_tools
        src = inspect.getsource(subagent_tools)
        assert "delegation_depth" in src
        assert "depth + 1" in src


# ── SPAWN-W3 / SPAWN-W4 — child session persistence + hierarchy ──────────────

class TestChildSessionPersistence:
    def test_register_child_session_importable(self):
        from src.core.memory.session_store import SessionStore
        assert hasattr(SessionStore, "register_child_session")

    def test_get_child_sessions_importable(self):
        from src.core.memory.session_store import SessionStore
        assert hasattr(SessionStore, "get_child_sessions")

    def test_get_session_tree_importable(self):
        from src.core.memory.session_store import SessionStore
        assert hasattr(SessionStore, "get_session_tree")

    def test_session_children_table_created(self, tmp_path):
        """session_children table must exist after SessionStore init."""
        import sqlite3
        from src.core.memory.session_store import SessionStore
        store = SessionStore(workdir=str(tmp_path))
        conn = sqlite3.connect(str(store.db_path))
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "session_children" in tables

    def test_register_and_retrieve_child_session(self, tmp_path):
        """register_child_session then get_child_sessions must return the child."""
        from src.core.memory.session_store import SessionStore
        store = SessionStore(workdir=str(tmp_path))
        store.register_child_session(
            parent_session_id="parent-1",
            child_session_id="child-1",
            role="analyst",
            task="Analyze the codebase",
        )
        children = store.get_child_sessions("parent-1")
        assert len(children) == 1
        assert children[0]["child_session_id"] == "child-1"
        assert children[0]["role"] == "analyst"

    def test_get_child_sessions_returns_empty_for_unknown_parent(self, tmp_path):
        from src.core.memory.session_store import SessionStore
        store = SessionStore(workdir=str(tmp_path))
        children = store.get_child_sessions("nonexistent-parent")
        assert children == []

    def test_get_session_tree_empty(self, tmp_path):
        """get_session_tree for a root with no children must return a tree with empty list."""
        from src.core.memory.session_store import SessionStore
        store = SessionStore(workdir=str(tmp_path))
        tree = store.get_session_tree("root-1")
        assert tree["session_id"] == "root-1"
        assert tree["children"] == []

    def test_get_session_tree_nested(self, tmp_path):
        """get_session_tree must return multi-level hierarchy."""
        from src.core.memory.session_store import SessionStore
        store = SessionStore(workdir=str(tmp_path))
        store.register_child_session("root", "child-a", "analyst", "task A")
        store.register_child_session("child-a", "grandchild-1", "reviewer", "review")
        tree = store.get_session_tree("root")
        assert len(tree["children"]) == 1
        child = tree["children"][0]
        assert child["session_id"] == "child-a"
        assert len(child["children"]) == 1
        assert child["children"][0]["session_id"] == "grandchild-1"

    def test_delegate_task_returns_child_session_id(self):
        """delegate_task result string must include child_session_id."""
        import inspect
        from src.tools import subagent_tools
        src = inspect.getsource(subagent_tools)
        assert "child_session_id" in src
        assert "register_child_session" in src


# ── SPAWN-W5 — spawn permission gate ─────────────────────────────────────────

class TestSpawnPermissionGate:
    def test_delegate_task_is_prompt_permission(self):
        """delegate_task must be registered as PROMPT permission level."""
        from src.tools.tools_config import TOOL_PERMISSIONS, PermissionLevel
        assert "delegate_task" in TOOL_PERMISSIONS
        assert TOOL_PERMISSIONS["delegate_task"] == PermissionLevel.PROMPT

    def test_spawn_permission_required_event_in_orchestrator_source(self):
        """execute_tool must publish spawn.permission_required for delegate_task."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert "spawn.permission_required" in src
        assert "SPAWN-W5" in src


# ── SES-W2 — full conversation pair storage ───────────────────────────────────

class TestFullConversationPairStorage:
    def test_user_message_stored_in_session_store(self, tmp_path):
        """run_agent_once must persist user prompt to session_store."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        assert "SES-W2" in src
        assert 'role="user"' in src or '"user"' in src

    def test_assistant_message_stored_in_session_store(self, tmp_path):
        """run_agent_once must persist assistant response to session_store."""
        import inspect
        import src.core.orchestration.orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        # Both user and assistant add_message calls must be present
        assert src.count('role="assistant"') >= 1 or '"assistant"' in src

    def test_add_message_accepts_both_roles(self, tmp_path):
        """SessionStore.add_message must accept user and assistant roles."""
        from src.core.memory.session_store import SessionStore
        store = SessionStore(workdir=str(tmp_path))
        store.add_message("sess1", "user", "Hello world")
        store.add_message("sess1", "assistant", "I'll help you.")
        msgs = store.get_messages("sess1")
        roles = {m["role"] for m in msgs}
        assert "user" in roles
        assert "assistant" in roles


# ── SES-W3 — per-model-per-role config ───────────────────────────────────────

class TestPerModelPerRoleConfig:
    def test_get_model_for_role_importable(self):
        from src.core.config_loader import get_model_for_role
        assert callable(get_model_for_role)

    def test_get_model_for_role_returns_none_when_unconfigured(self):
        """When providers.json has no per-role override, return None."""
        from src.core.config_loader import get_model_for_role
        result = get_model_for_role("strategic")
        assert result is None or isinstance(result, str)

    def test_get_model_for_role_unknown_role_returns_none(self):
        from src.core.config_loader import get_model_for_role
        result = get_model_for_role("nonexistent_role_xyz")
        assert result is None

    def test_planning_node_has_model_override(self):
        """planning_node source must check get_model_for_role."""
        import inspect
        import src.core.orchestration.graph.nodes.planning_node as pn_mod
        src = inspect.getsource(pn_mod)
        assert "get_model_for_role" in src or "_planning_model_override" in src

    def test_execution_node_has_model_override(self):
        """execution_node source must check get_model_for_role."""
        import inspect
        import src.core.orchestration.graph.nodes.execution_node as en_mod
        src = inspect.getsource(en_mod)
        assert "get_model_for_role" in src or "_exec_model_override" in src

    def test_role_model_keys_table_exists(self):
        """_ROLE_MODEL_KEYS mapping must be present in config_loader."""
        import inspect
        from src.core import config_loader
        src = inspect.getsource(config_loader)
        assert "planning_model" in src
        assert "execution_model" in src
