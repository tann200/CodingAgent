"""tests/unit/test_core_bridge_decomposition.py — P1-3

Structural tests verifying that the core_bridge.py decomposition into 6 mixin
files is correct:

1. All 6 mixin classes exist and are importable.
2. AgentBridge's MRO includes all 6 mixins.
3. Each method lives on the expected mixin (not duplicated in AgentBridge itself).
4. AgentBridge still owns the core methods that were NOT moved.
5. Both tui/src and tui/tui_src trees export identical AgentBridge.
6. The _subscribe helper (kept on AgentBridge) is accessible from mixin context.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MIXIN_MODULES = {
    "BridgeSubscriptionsMixin": "tui.tui_src.ui._bridge_subscriptions",
    "BridgeProviderMixin": "tui.tui_src.ui._bridge_provider",
    "BridgeToolsMixin": "tui.tui_src.ui._bridge_tools",
    "BridgeContextMixin": "tui.tui_src.ui._bridge_context",
    "BridgeSessionMixin": "tui.tui_src.ui._bridge_session",
    "BridgeAgentMixin": "tui.tui_src.ui._bridge_agent",
}


def _get_bridge():
    from tui.tui_src.ui.core_bridge import AgentBridge
    return AgentBridge


def _get_mixin(name: str):
    mod = importlib.import_module(MIXIN_MODULES[name])
    return getattr(mod, name)


# ---------------------------------------------------------------------------
# 1. All mixin modules import cleanly
# ---------------------------------------------------------------------------


class TestMixinImports:
    @pytest.mark.parametrize("mixin_name", list(MIXIN_MODULES.keys()))
    def test_mixin_importable(self, mixin_name: str) -> None:
        mixin = _get_mixin(mixin_name)
        assert mixin is not None
        assert inspect.isclass(mixin)

    def test_agent_bridge_importable(self) -> None:
        bridge = _get_bridge()
        assert inspect.isclass(bridge)


# ---------------------------------------------------------------------------
# 2. MRO includes all 6 mixins
# ---------------------------------------------------------------------------


class TestMRO:
    def test_all_mixins_in_mro(self) -> None:
        AgentBridge = _get_bridge()
        mro_names = {c.__name__ for c in AgentBridge.__mro__}
        for mixin_name in MIXIN_MODULES:
            assert mixin_name in mro_names, f"{mixin_name} missing from MRO"

    def test_mro_order(self) -> None:
        """AgentBridge must appear before all mixins in MRO."""
        AgentBridge = _get_bridge()
        mro = [c.__name__ for c in AgentBridge.__mro__]
        assert mro[0] == "AgentBridge"
        for mixin_name in MIXIN_MODULES:
            assert mro.index("AgentBridge") < mro.index(mixin_name)


# ---------------------------------------------------------------------------
# 3. Methods live on the correct mixin
# ---------------------------------------------------------------------------


SUBSCRIPTIONS_METHODS = ["setup_subscriptions", "_seed_context_window_from_config", "cleanup"]
PROVIDER_METHODS = [
    "_publish_system_settings", "_publish_active_provider_status",
    "_check_provider_auth_on_startup", "_on_orchestrator_startup",
    "_on_system_settings", "_on_provider_status", "_on_models_list",
    "_on_model_routing", "_on_model_response", "_on_model_token",
    "_on_stream_chunk", "_on_provider_context_window", "get_fast_model",
]
TOOLS_METHODS = [
    "_on_tool_start", "_on_tool_finish", "_on_tool_error",
    "_on_delegation_start", "_on_delegation_finish",
    "_on_diff_preview", "_on_file_modified", "_on_file_deleted",
    "_on_plan_progress", "_on_plan_requested", "_on_mcp_server_status",
    "_on_tool_permission_required", "_on_spawn_permission_required",
    "_on_doom_loop_detected", "_on_agent_message",
    "approve_plan", "reject_plan", "bash_approved", "bash_denied",
    "confirm_file_preview", "reject_file_preview",
]
CONTEXT_METHODS = [
    "_on_token_budget", "_on_token_budget_warning", "_on_context_degraded",
    "_on_context_compacted", "_on_task_queue_updated", "_on_step_start",
    "_on_step_finish", "_on_role_transition", "_on_preview_pending",
    "_on_git_branch", "_on_retry_attempt", "_on_retry_succeeded",
    "_on_retry_failed", "_on_session_new", "_on_session_hydrated",
    "_on_session_health", "_on_ui_notification", "_on_log_new",
    "_on_usage_turn_summary", "_on_subagent_cost",
    "_get_active_context_length", "compact_context",
]
SESSION_METHODS = [
    "load_history", "_save_history", "save_history", "clear_history",
    "undo_last_user_message", "_get_prompt_history_path",
    "load_prompt_history", "update_prompt_history",
    "publish_session_request", "publish_session_new",
    "start_new_session", "restore_and_continue",
]
AGENT_METHODS = [
    "send_prompt", "_run_agent", "interrupt", "force_interrupt",
    "pop_pending_injections", "get_turn_count", "get_usage_totals",
]

BRIDGE_OWN_METHODS = [
    "__init__", "_ensure_deferred_init", "_schedule_callback",
    "_subscribe", "_post", "get_status", "is_running", "publish",
]


def _owner(method_name: str) -> str:
    """Return the class name that defines *method_name* first in MRO."""
    AgentBridge = _get_bridge()
    for cls in AgentBridge.__mro__:
        if method_name in cls.__dict__:
            return cls.__name__
    return ""


class TestMethodOwnership:
    @pytest.mark.parametrize("method", SUBSCRIPTIONS_METHODS)
    def test_subscriptions_mixin_owns(self, method: str) -> None:
        assert _owner(method) == "BridgeSubscriptionsMixin", f"{method} not on BridgeSubscriptionsMixin"

    @pytest.mark.parametrize("method", PROVIDER_METHODS)
    def test_provider_mixin_owns(self, method: str) -> None:
        assert _owner(method) == "BridgeProviderMixin", f"{method} not on BridgeProviderMixin"

    @pytest.mark.parametrize("method", TOOLS_METHODS)
    def test_tools_mixin_owns(self, method: str) -> None:
        assert _owner(method) == "BridgeToolsMixin", f"{method} not on BridgeToolsMixin"

    @pytest.mark.parametrize("method", CONTEXT_METHODS)
    def test_context_mixin_owns(self, method: str) -> None:
        assert _owner(method) == "BridgeContextMixin", f"{method} not on BridgeContextMixin"

    @pytest.mark.parametrize("method", SESSION_METHODS)
    def test_session_mixin_owns(self, method: str) -> None:
        assert _owner(method) == "BridgeSessionMixin", f"{method} not on BridgeSessionMixin"

    @pytest.mark.parametrize("method", AGENT_METHODS)
    def test_agent_mixin_owns(self, method: str) -> None:
        assert _owner(method) == "BridgeAgentMixin", f"{method} not on BridgeAgentMixin"

    @pytest.mark.parametrize("method", BRIDGE_OWN_METHODS)
    def test_bridge_owns_core_methods(self, method: str) -> None:
        assert _owner(method) == "AgentBridge", f"{method} not on AgentBridge itself"


# ---------------------------------------------------------------------------
# 4. No method is duplicated across mixin and AgentBridge
# ---------------------------------------------------------------------------


class TestNoDuplication:
    def test_moved_methods_not_in_bridge_dict(self) -> None:
        AgentBridge = _get_bridge()
        all_moved = (
            SUBSCRIPTIONS_METHODS + PROVIDER_METHODS + TOOLS_METHODS +
            CONTEXT_METHODS + SESSION_METHODS + AGENT_METHODS
        )
        duplicated = [m for m in all_moved if m in AgentBridge.__dict__]
        assert duplicated == [], f"Methods duplicated in AgentBridge.__dict__: {duplicated}"


# ---------------------------------------------------------------------------
# 5. tui/src mirror exports identical AgentBridge
# ---------------------------------------------------------------------------


class TestMirrorSync:
    def test_src_mirror_has_agent_bridge(self) -> None:
        from tui.src.ui.core_bridge import AgentBridge as BridgeSrc  # type: ignore[import]
        from tui.tui_src.ui.core_bridge import AgentBridge as BridgeDst
        # Same MRO structure (names match, even if different objects due to separate import)
        src_mro = [c.__name__ for c in BridgeSrc.__mro__]
        dst_mro = [c.__name__ for c in BridgeDst.__mro__]
        assert src_mro == dst_mro

    @pytest.mark.parametrize("mixin_name", list(MIXIN_MODULES.keys()))
    def test_src_mirror_has_mixin(self, mixin_name: str) -> None:
        src_mod_name = MIXIN_MODULES[mixin_name].replace("tui.tui_src", "tui.src")
        src_mod = importlib.import_module(src_mod_name)
        mixin = getattr(src_mod, mixin_name)
        assert inspect.isclass(mixin)


# ---------------------------------------------------------------------------
# 6. core_bridge.py is significantly smaller than 1840 lines
# ---------------------------------------------------------------------------


class TestCoreBridgeSize:
    def test_core_bridge_under_500_lines(self) -> None:
        import tui.tui_src.ui.core_bridge as cb_mod
        src = inspect.getsource(cb_mod)
        lines = src.count("\n")
        assert lines < 500, f"core_bridge.py is {lines} lines — expected < 500 after decomposition"

    def test_each_mixin_under_500_lines(self) -> None:
        for mixin_name, mod_name in MIXIN_MODULES.items():
            mod = importlib.import_module(mod_name)
            src = inspect.getsource(mod)
            lines = src.count("\n")
            assert lines < 500, f"{mod_name} is {lines} lines — unexpectedly large"
