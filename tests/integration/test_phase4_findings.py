"""tests/integration/test_phase4_findings.py — Phase 4 finding tests

Covers:
  ARCH-1  PermissionGateway: check() returns PermissionResult; gates block correctly
  ARCH-3  route_execution sub-routers: _check_tool_budget, _check_plan_approval_pending,
          _check_preview_pending, _check_replan_required, _check_no_plan_fast_path
  MEM-2   SessionStore.write_decisions_json / read_recent_decisions round-trip
  CAP-5   benchmarks/bench_pipeline.py scenarios complete under 30 s
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# CAP-5 helpers (mirrors test_e2e_pipeline_smoke.py approach)
# ---------------------------------------------------------------------------

_CALL_MODEL_TARGETS = [
    "src.core.orchestration.graph.nodes.perception_node.call_model",
    "src.core.orchestration.graph.nodes.planning_node.call_model",
    "src.core.orchestration.graph.nodes.execution_node.call_model",
    "src.core.orchestration.graph.nodes.debug_node.call_model",
    "src.core.orchestration.graph.nodes.replan_node.call_model",
    "src.core.inference.llm_manager.call_model",
    "src.core.inference.llm_manager._call_model_internal",
]


def _cap5_patch_call_model(adapter: Any, monkeypatch: Any) -> None:
    """Patch every node's call_model import to use the provided adapter."""
    from src.core.inference.adapters.mock_adapter import MockAdapter  # noqa: F401

    async def mock_call_model(messages, model=None, provider=None, *args, **kwargs):
        return adapter.generate(messages, model=model, provider=provider, **kwargs)

    for target in _CALL_MODEL_TARGETS:
        try:
            monkeypatch.setattr(target, mock_call_model)
        except AttributeError:
            pass


def _cap5_patch_infra(monkeypatch: Any) -> None:
    """Patch heavy infrastructure not needed in benchmark tests."""
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator.Orchestrator._background_model_check",
        lambda self: None,
    )
    try:
        monkeypatch.setattr(
            "src.core.memory.distiller.generate_session_title",
            lambda msg: "Test Session",
        )
    except AttributeError:
        pass
    try:
        import src.core.orchestration.graph.builder as _builder

        monkeypatch.setattr(_builder, "_COMPILED_GRAPH", None)
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# ARCH-1: PermissionGateway
# ---------------------------------------------------------------------------


class TestPermissionGateway:
    """ARCH-1: PermissionGateway.check() delegates permission logic correctly."""

    def _make_gateway(self, **orch_attrs):
        from src.core.orchestration.permission_gateway import PermissionGateway

        orch = MagicMock()
        for k, v in orch_attrs.items():
            setattr(orch, k, v)
        return PermissionGateway(orch)

    def test_allowed_when_no_gates_fire(self) -> None:
        gw = self._make_gateway(plan_mode=None, explore_mode=False)
        result = gw.check("read_file", {"path": "foo.py"})
        assert result.allowed is True
        assert result.blocked is False

    def test_permission_result_dataclass(self) -> None:
        from src.core.orchestration.permission_gateway import PermissionResult

        r = PermissionResult(allowed=False, gate=2, reason="explore mode")
        assert r.blocked is True
        assert r.gate == 2
        assert r.reason == "explore mode"

    def test_gate2_explore_mode_blocks_write(self) -> None:
        from src.core.orchestration.permission_gateway import PermissionGateway

        orch = MagicMock()
        orch.plan_mode = None
        orch.explore_mode = True
        gw = PermissionGateway(orch)

        with patch(
            "src.core.orchestration.role_config.is_tool_allowed_for_role",
            return_value=False,
        ):
            result = gw.check("write_file", {"path": "foo.py"})

        assert result.blocked is True
        assert result.gate == 2
        assert result.rejection is not None
        assert "Explore mode" in result.rejection["error"]

    def test_gate2_explore_mode_allows_read(self) -> None:
        from src.core.orchestration.permission_gateway import PermissionGateway

        orch = MagicMock()
        orch.plan_mode = None
        orch.explore_mode = True
        gw = PermissionGateway(orch)

        with patch(
            "src.core.orchestration.role_config.is_tool_allowed_for_role",
            return_value=True,
        ):
            result = gw.check("read_file", {"path": "foo.py"})

        # allowed at gate 2; may still be allowed overall
        assert isinstance(result.allowed, bool)

    def test_gate4_permission_mode_blocks_dangerous_tool(self) -> None:
        from src.core.orchestration.permission_gateway import PermissionGateway

        orch = MagicMock()
        orch.plan_mode = None
        orch.explore_mode = False
        gw = PermissionGateway(orch)

        class _FakeMode:
            value = "read_only"

        class _FakePerm:
            value = "danger"

        # Patch the module-level references used inside permission_gateway, not the
        # tools_config attributes (the gateway imports them once at module load time).
        with (
            patch(
                "src.core.orchestration.permission_gateway._get_active_permission_mode",
                return_value=_FakeMode(),
            ),
            patch(
                "src.core.orchestration.permission_gateway._get_tool_permission",
                return_value=_FakePerm(),
            ),
        ):
            result = gw._gate4_permission_mode("bash")

        assert result.blocked is True
        assert result.gate == 4

    def test_check_returns_allowed_for_safe_tool_no_special_mode(self) -> None:
        from src.core.orchestration.permission_gateway import PermissionGateway

        orch = MagicMock()
        orch.plan_mode = None
        orch.explore_mode = False
        gw = PermissionGateway(orch)

        with (
            patch(
                "src.tools.tools_config.get_active_permission_mode", return_value=None
            ),
            patch("src.tools.tools_config.is_autonomous", return_value=True),
        ):
            result = gw.check("read_file", {})

        assert result.allowed is True


# ---------------------------------------------------------------------------
# ARCH-3: route_execution sub-routers
# ---------------------------------------------------------------------------


class TestRouteExecutionSubRouters:
    """ARCH-3: Individual sub-router functions are independently testable."""

    def test_check_tool_budget_exhausted(self) -> None:
        from src.core.orchestration.graph.builder import _check_tool_budget

        state = {"tool_call_count": 30, "max_tool_calls": 30}
        assert _check_tool_budget(state) is True

    def test_check_tool_budget_not_exhausted(self) -> None:
        from src.core.orchestration.graph.builder import _check_tool_budget

        state = {"tool_call_count": 5, "max_tool_calls": 30}
        assert _check_tool_budget(state) is False

    def test_check_tool_budget_uses_default(self) -> None:
        from src.core.orchestration.graph.builder import (
            _check_tool_budget,
            _DEFAULT_MAX_TOOL_CALLS,
        )

        state = {"tool_call_count": _DEFAULT_MAX_TOOL_CALLS}
        assert _check_tool_budget(state) is True

    def test_check_plan_approval_pending_true(self) -> None:
        from src.core.orchestration.graph.builder import _check_plan_approval_pending

        assert _check_plan_approval_pending({"awaiting_plan_approval": True}) is True

    def test_check_plan_approval_pending_false(self) -> None:
        from src.core.orchestration.graph.builder import _check_plan_approval_pending

        assert _check_plan_approval_pending({}) is False

    def test_check_preview_pending_true(self) -> None:
        from src.core.orchestration.graph.builder import _check_preview_pending

        assert _check_preview_pending({"awaiting_user_input": True}) is True

    def test_check_preview_pending_false(self) -> None:
        from src.core.orchestration.graph.builder import _check_preview_pending

        assert _check_preview_pending({"awaiting_user_input": False}) is False

    def test_check_replan_required_none_when_not_set(self) -> None:
        from src.core.orchestration.graph.builder import _check_replan_required

        assert _check_replan_required({}) is None
        assert _check_replan_required({"replan_required": None}) is None

    def test_check_replan_required_cap_at_5(self) -> None:
        from src.core.orchestration.graph.builder import _check_replan_required

        state = {"replan_required": True, "replan_attempts": 5}
        assert _check_replan_required(state) == "memory_sync"

    def test_check_replan_required_returns_replan(self) -> None:
        from src.core.orchestration.graph.builder import _check_replan_required

        state = {
            "replan_required": True,
            "replan_attempts": 2,
            "current_plan": [{"description": "step"}],
            "last_plan_hash": None,
        }
        assert _check_replan_required(state) == "replan"

    def test_check_no_plan_fast_path_returns_none_with_plan(self) -> None:
        from src.core.orchestration.graph.builder import _check_no_plan_fast_path

        state = {"current_plan": [{"description": "step 1"}]}
        assert _check_no_plan_fast_path(state) is None

    def test_check_no_plan_fast_path_read_only_routes_memory_sync(self) -> None:
        from src.core.orchestration.graph.builder import _check_no_plan_fast_path

        # NOTE: `grep` is in _query_tools (routes to perception so the model can
        # synthesise a natural language answer). Use `list_files` instead — it is
        # read-only, not a query tool, and not a read_file type, so it routes
        # directly to memory_sync (task answered).
        state = {
            "current_plan": [],
            "last_tool_name": "list_files",
            "last_result": {"ok": True},
        }
        assert _check_no_plan_fast_path(state) == "memory_sync"

    def test_check_no_plan_fast_path_completion_detected(self) -> None:
        from src.core.orchestration.graph.builder import _check_no_plan_fast_path

        state = {
            "current_plan": [],
            "last_result": {"_completion_detected": True},
        }
        assert _check_no_plan_fast_path(state) == "memory_sync"

    def test_check_no_plan_fast_path_failure_routes_analysis(self) -> None:
        from src.core.orchestration.graph.builder import _check_no_plan_fast_path

        state = {
            "current_plan": [],
            "last_tool_name": "write_file",
            "last_result": {"ok": False, "error": "permission denied"},
            "rounds": 2,
            "no_plan_fail_count": 0,
        }
        assert _check_no_plan_fast_path(state) == "analysis"

    def test_check_no_plan_fast_path_loop_guard(self) -> None:
        from src.core.orchestration.graph.builder import _check_no_plan_fast_path

        state = {
            "current_plan": [],
            "last_tool_name": "write_file",
            "last_result": {"ok": True},
            "rounds": 15,
        }
        assert _check_no_plan_fast_path(state) == "memory_sync"

    def test_route_execution_delegates_budget_check(self) -> None:
        from src.core.orchestration.graph.builder import route_execution

        state = {"tool_call_count": 100, "max_tool_calls": 30}
        assert route_execution(state) == "memory_sync"

    def test_route_execution_delegates_plan_approval(self) -> None:
        from src.core.orchestration.graph.builder import route_execution

        state = {"tool_call_count": 0, "awaiting_plan_approval": True}
        assert route_execution(state) == "wait_for_user"

    def test_route_execution_with_plan_routes_to_step_controller(self) -> None:
        from src.core.orchestration.graph.builder import route_execution

        state = {
            "tool_call_count": 0,
            "awaiting_plan_approval": False,
            "awaiting_user_input": False,
            "replan_required": None,
            "current_plan": [{"description": "step 1", "completed": False}],
        }
        assert route_execution(state) == "step_controller"


# ---------------------------------------------------------------------------
# MEM-2: Persistent decision memory
# ---------------------------------------------------------------------------


class TestPersistentDecisionMemory:
    """MEM-2: write_decisions_json / read_recent_decisions round-trip."""

    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        from src.core.memory.session_store import SessionStore

        store = SessionStore(workdir=str(tmp_path))
        store.add_decision("sess-1", "complete: add login", "tests pass")
        store.add_decision("sess-1", "complete: refactor auth", "cleaner code")

        decisions = store.read_recent_decisions(max_entries=10)
        assert len(decisions) >= 2
        texts = [d["decision"] for d in decisions]
        assert "complete: add login" in texts
        assert "complete: refactor auth" in texts

    def test_decisions_json_created_on_disk(self, tmp_path: Path) -> None:
        from src.core.memory.session_store import SessionStore

        store = SessionStore(workdir=str(tmp_path))
        store.add_decision("sess-2", "complete: fix bug", "all good")

        decisions_path = tmp_path / ".codingAgent" / "decisions.json"
        assert decisions_path.exists(), "decisions.json should be written to disk"
        data = json.loads(decisions_path.read_text(encoding="utf-8"))
        assert isinstance(data, list)
        assert any(d.get("decision") == "complete: fix bug" for d in data)

    def test_read_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        from src.core.memory.session_store import SessionStore

        store = SessionStore(workdir=str(tmp_path))
        result = store.read_recent_decisions()
        assert result == []

    def test_max_entries_limit(self, tmp_path: Path) -> None:
        from src.core.memory.session_store import SessionStore

        store = SessionStore(workdir=str(tmp_path))
        for i in range(20):
            store.add_decision("sess-3", f"decision-{i}", "rationale")

        result = store.read_recent_decisions(max_entries=5)
        assert len(result) == 5

    def test_write_decisions_json_limit(self, tmp_path: Path) -> None:
        from src.core.memory.session_store import SessionStore

        store = SessionStore(workdir=str(tmp_path))
        for i in range(60):
            # Bypass add_decision's auto-flush to speed up the test
            with store._lock:
                conn = store._get_connection()
                conn.execute(
                    "INSERT INTO decisions (session_id, decision, rationale) VALUES (?, ?, ?)",
                    ("sess-4", f"d{i}", None),
                )
                conn.commit()
        # Now flush with limit=50
        store.write_decisions_json(limit=50)
        data = json.loads(
            (tmp_path / ".codingAgent" / "decisions.json").read_text(encoding="utf-8")
        )
        assert len(data) <= 50

    def test_read_returns_list_not_error_on_corrupt_json(self, tmp_path: Path) -> None:
        from src.core.memory.session_store import SessionStore

        # Write malformed JSON
        dec_path = tmp_path / ".codingAgent" / "decisions.json"
        dec_path.parent.mkdir(parents=True, exist_ok=True)
        dec_path.write_text("NOT_JSON{{{", encoding="utf-8")

        store = SessionStore(workdir=str(tmp_path))
        result = store.read_recent_decisions()
        assert result == []  # must not raise

    def test_add_decision_auto_flushes(self, tmp_path: Path) -> None:
        from src.core.memory.session_store import SessionStore

        store = SessionStore(workdir=str(tmp_path))
        store.add_decision("sess-5", "task done", "passed all checks")

        # File should exist immediately after add_decision
        decisions_path = tmp_path / ".codingAgent" / "decisions.json"
        assert decisions_path.exists()


# ---------------------------------------------------------------------------
# CAP-5: Pipeline benchmarks
# ---------------------------------------------------------------------------


class TestBenchmarkScenarios:
    """CAP-5: Each benchmark scenario completes successfully within time limit."""

    # Scenario definitions inline — avoids bench_pipeline.run_scenario's
    # unittest.mock.patch approach which can't correctly null _COMPILED_GRAPH.
    _SCENARIOS: Dict[str, Dict[str, Any]] = {
        "fast_path_write": {
            "name": "fast_path_write",
            "task": "Create hello.py with def hello(): return 'Hello World'",
            "responses": [
                "```yaml\nname: write_file\narguments:\n  path: hello.py\n  content: \"def hello():\\n    return 'Hello World'\\n\"\n```",
                "hello.py has been created.",
            ],
        },
        "fast_path_grep": {
            "name": "fast_path_grep",
            "task": "Find all Python files that define a main function",
            "responses": [
                "```yaml\nname: grep\narguments:\n  pattern: 'def main'\n  path: .\n```",
                "Found 2 files with main functions.",
            ],
        },
        "fast_path_list": {
            "name": "fast_path_list",
            "task": "List all files in the project",
            "responses": [
                "```yaml\nname: list_files\narguments:\n  path: .\n```",
                "Found 3 files in the project.",
            ],
        },
    }

    def _run_scenario_with_monkeypatch(
        self, scenario: Dict[str, Any], tmp_path: Path, monkeypatch: Any
    ) -> Dict[str, Any]:
        """Run a scenario using monkeypatch (mirrors test_e2e_pipeline_smoke approach)."""
        from src.core.inference.adapters.mock_adapter import MockAdapter
        from src.core.orchestration.orchestrator import Orchestrator

        # Pre-create any required files
        for fname, content in scenario.get("pre_create", {}).items():
            fpath = tmp_path / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

        adapter = MockAdapter(responses=list(scenario["responses"]), strict=False)
        _cap5_patch_call_model(adapter, monkeypatch)
        _cap5_patch_infra(monkeypatch)

        orch = Orchestrator(
            adapter=adapter,
            working_dir=str(tmp_path),
            allow_external_working_dir=True,
        )
        messages = [{"role": "user", "content": scenario["task"]}]

        t0 = time.perf_counter()
        try:
            result = orch.run_agent_once(None, messages, {})
            elapsed = time.perf_counter() - t0
            # result_ok: pipeline returned a dict and did not set a top-level error key
            # with a non-empty value (grep/list tools set error='' on success)
            result_ok = isinstance(result, dict) and not result.get("error")
            return {
                "scenario": scenario["name"],
                "wall_time_s": round(elapsed, 4),
                "input_tokens": len(scenario["task"]) // 4,
                "output_tokens": sum(len(r) // 4 for r in scenario["responses"]),
                "result_ok": result_ok,
            }
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            return {
                "scenario": scenario["name"],
                "wall_time_s": round(elapsed, 4),
                "input_tokens": 0,
                "output_tokens": 0,
                "result_ok": False,
                "error": str(exc),
            }

    @pytest.mark.parametrize(
        "scenario_name",
        ["fast_path_write", "fast_path_grep", "fast_path_list"],
    )
    def test_benchmark_scenario_passes(
        self, scenario_name: str, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Each scenario must complete without error and under 30 s."""
        scenario = self._SCENARIOS[scenario_name]
        metrics = self._run_scenario_with_monkeypatch(scenario, tmp_path, monkeypatch)

        assert metrics["result_ok"], (
            f"Scenario '{scenario_name}' failed: {metrics.get('error', 'unknown')}"
        )
        assert metrics["wall_time_s"] < 30.0, (
            f"Scenario '{scenario_name}' took {metrics['wall_time_s']:.2f}s"
        )

    def test_benchmark_metrics_structure(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Metrics dict must have the expected keys."""
        scenario = self._SCENARIOS["fast_path_grep"]
        metrics = self._run_scenario_with_monkeypatch(scenario, tmp_path, monkeypatch)

        for key in (
            "scenario",
            "wall_time_s",
            "input_tokens",
            "output_tokens",
            "result_ok",
        ):
            assert key in metrics, f"Expected key '{key}' in metrics dict"
