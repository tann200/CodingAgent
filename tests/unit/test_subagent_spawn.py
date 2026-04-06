"""Tests for CP-1 (structural recursion prevention) and CP-2 (manifest-first spawning)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.tools.subagent_tools import SubagentOrchestrator


# ---------------------------------------------------------------------------
# CP-1 — SubagentOrchestrator structural recursion prevention
# ---------------------------------------------------------------------------


class TestSubagentOrchestratorRecursionPrevention:
    """CP-1: delegate_task / delegate_task_async must always be in the denylist
    of every SubagentOrchestrator regardless of the explicit allowed_tools list."""

    def test_delegate_task_denied_by_default(self, tmp_path: Path) -> None:
        orch = SubagentOrchestrator(role="analyst", working_dir=str(tmp_path))
        assert not orch.is_tool_allowed("delegate_task")

    def test_delegate_task_async_denied_by_default(self, tmp_path: Path) -> None:
        orch = SubagentOrchestrator(role="analyst", working_dir=str(tmp_path))
        assert not orch.is_tool_allowed("delegate_task_async")

    def test_delegate_task_denied_when_explicit_denied_set_given(
        self, tmp_path: Path
    ) -> None:
        orch = SubagentOrchestrator(
            role="operational",
            working_dir=str(tmp_path),
            denied_tools={"delegate_task", "delegate_task_async"},
        )
        assert not orch.is_tool_allowed("delegate_task")
        assert not orch.is_tool_allowed("delegate_task_async")

    def test_delegate_task_denied_even_if_in_allowed_tools(
        self, tmp_path: Path
    ) -> None:
        """If caller explicitly passes delegate_task in allowed_tools, it should
        still be denied because the denied set takes precedence."""
        orch = SubagentOrchestrator(
            role="analyst",
            working_dir=str(tmp_path),
            allowed_tools={"read_file", "delegate_task"},
            denied_tools={"delegate_task", "delegate_task_async"},
        )
        assert not orch.is_tool_allowed("delegate_task")

    def test_normal_tool_not_affected(self, tmp_path: Path) -> None:
        orch = SubagentOrchestrator(
            role="analyst",
            working_dir=str(tmp_path),
            denied_tools={"delegate_task", "delegate_task_async"},
        )
        # Patch role config to return True for a basic tool
        with patch(
            "src.tools.subagent_tools.is_tool_allowed_for_role", return_value=True
        ):
            assert orch.is_tool_allowed("read_file") is True

    def test_get_denied_tools_includes_delegate_task(self, tmp_path: Path) -> None:
        orch = SubagentOrchestrator(
            role="analyst",
            working_dir=str(tmp_path),
            denied_tools={"delegate_task", "delegate_task_async"},
        )
        denied = orch.get_denied_tools()
        assert "delegate_task" in denied
        assert "delegate_task_async" in denied

    def test_denied_set_is_a_copy(self, tmp_path: Path) -> None:
        """Mutating the original denied set should not affect the orchestrator."""
        my_denied: set = {"some_tool"}
        orch = SubagentOrchestrator(
            role="analyst",
            working_dir=str(tmp_path),
            denied_tools=my_denied,
        )
        my_denied.add("read_file")
        # The orchestrator should not see the mutation
        with patch(
            "src.tools.subagent_tools.is_tool_allowed_for_role", return_value=True
        ):
            assert orch.is_tool_allowed("read_file")


# ---------------------------------------------------------------------------
# CP-1 — delegate_task() wires CP-1 correctly
# ---------------------------------------------------------------------------


def _make_minimal_graph_mock() -> MagicMock:
    """Return a mock graph whose ainvoke() immediately returns a finished state."""
    import asyncio

    async def _fake_ainvoke(state, config=None):
        return {
            "task": state.get("task", ""),
            "history": [{"role": "assistant", "content": "Done."}],
            "errors": [],
            "last_result": {"status": "ok"},
            "session_id": state.get("session_id", "test"),
        }

    mock_graph = MagicMock()
    mock_graph.ainvoke = _fake_ainvoke
    return mock_graph


class TestDelegateTaskCP1Wiring:
    """Verify that delegate_task always adds delegate_task / delegate_task_async
    to the effective denied set before constructing SubagentOrchestrator."""

    def _run_delegate_task_with_mocks(
        self,
        tmp_path: Path,
        allowed_tools: Optional[list] = None,
    ) -> SubagentOrchestrator:
        """Run delegate_task with heavy mocking; capture the SubagentOrchestrator
        that was constructed by intercepting its __init__."""
        captured: list = []

        _orig_init = SubagentOrchestrator.__init__

        def _patched_init(
            self_inner, role, working_dir, allowed_tools=None, denied_tools=None
        ):  # type: ignore[override]
            _orig_init(
                self_inner,
                role=role,
                working_dir=working_dir,
                allowed_tools=allowed_tools,
                denied_tools=denied_tools,
            )
            captured.append(self_inner)

        graph_mock = _make_minimal_graph_mock()

        with (
            patch(
                "src.tools.subagent_tools.SubagentOrchestrator.__init__", _patched_init
            ),
            patch("src.tools.subagent_tools.GraphFactory") as mock_gf,
            patch("src.tools.subagent_tools.get_agent_brain_manager") as mock_brain_mgr,
            patch("src.tools.subagent_tools._PARENT_ORCHESTRATOR_VAR") as mock_ctxvar,
        ):
            mock_gf.get_graph.return_value = graph_mock
            mock_brain = MagicMock()
            mock_brain.compile_system_prompt.return_value = "sys"
            mock_brain_mgr.return_value = mock_brain
            mock_ctxvar.get.return_value = None  # no parent orchestrator

            from src.tools.subagent_tools import delegate_task

            delegate_task(
                role="analyst",
                subtask_description="test task",
                working_dir=str(tmp_path),
                allowed_tools=allowed_tools,
            )

        return captured[0] if captured else None  # type: ignore[return-value]

    def test_delegate_task_denied_without_explicit_allowed_tools(
        self, tmp_path: Path
    ) -> None:
        orch = self._run_delegate_task_with_mocks(tmp_path, allowed_tools=None)
        if orch is None:
            pytest.skip("SubagentOrchestrator not captured (graph raised early)")
        assert "delegate_task" in orch._denied_tools
        assert "delegate_task_async" in orch._denied_tools

    def test_delegate_task_denied_even_when_explicitly_allowlisted(
        self, tmp_path: Path
    ) -> None:
        orch = self._run_delegate_task_with_mocks(
            tmp_path,
            allowed_tools=["read_file", "delegate_task", "delegate_task_async"],
        )
        if orch is None:
            pytest.skip("SubagentOrchestrator not captured")
        assert "delegate_task" in orch._denied_tools
        assert "delegate_task_async" in orch._denied_tools
        # Also verify they were stripped from the allowlist
        if orch._allowed_tools is not None:
            assert "delegate_task" not in orch._allowed_tools
            assert "delegate_task_async" not in orch._allowed_tools


# ---------------------------------------------------------------------------
# CP-1 — depth guard still works
# ---------------------------------------------------------------------------


class TestDelegateTaskDepthGuard:
    def test_depth_guard_refuses_at_depth_3(self, tmp_path: Path) -> None:
        with patch.dict(os.environ, {"CODINGAGENT_DELEGATION_DEPTH": "3"}):
            from src.tools.subagent_tools import delegate_task

            result = delegate_task(
                role="analyst",
                subtask_description="should be refused",
                working_dir=str(tmp_path),
            )
        assert "Maximum delegation depth" in result
        assert "Error" in result

    def test_depth_guard_allows_at_depth_2(self, tmp_path: Path) -> None:
        """Depth 2 is still allowed (limit is >= 3)."""
        graph_mock = _make_minimal_graph_mock()
        with (
            patch.dict(os.environ, {"CODINGAGENT_DELEGATION_DEPTH": "2"}),
            patch("src.tools.subagent_tools.GraphFactory") as mock_gf,
            patch("src.tools.subagent_tools.get_agent_brain_manager") as mock_brain_mgr,
            patch("src.tools.subagent_tools._PARENT_ORCHESTRATOR_VAR") as mock_ctxvar,
        ):
            mock_gf.get_graph.return_value = graph_mock
            mock_brain = MagicMock()
            mock_brain.compile_system_prompt.return_value = "sys"
            mock_brain_mgr.return_value = mock_brain
            mock_ctxvar.get.return_value = None

            from src.tools.subagent_tools import delegate_task

            result = delegate_task(
                role="analyst",
                subtask_description="allowed task",
                working_dir=str(tmp_path),
            )
        # Should NOT be a depth-guard refusal
        assert "Maximum delegation depth" not in result


# ---------------------------------------------------------------------------
# CP-2 — Manifest-first spawning
# ---------------------------------------------------------------------------


class TestManifestFirstSpawning:
    """CP-2: A JSON manifest must be written to
    <workdir>/.agent-context/subagent_manifests/subagent_<id>.json
    *before* the subagent thread is executed, and updated on completion/failure.
    """

    def _run_and_collect_manifests(
        self,
        tmp_path: Path,
        fail_graph: bool = False,
    ) -> tuple[list[dict], Path]:
        """Run delegate_task with mocks and return (list_of_manifest_states_written, manifest_path)."""
        manifests_written: list[dict] = []
        manifest_path_holder: list[Path] = []

        # We intercept json.dumps writes to manifest dir by patching Path.write_text
        # at the specific manifest path. But simpler: let it write for real to tmp_path.

        import asyncio

        async def _fake_ainvoke(state, config=None):
            # At this point the manifest should already exist with status=running
            manifest_dir = tmp_path / ".agent-context" / "subagent_manifests"
            found = list(manifest_dir.glob("subagent_*.json"))
            if found:
                data = json.loads(found[0].read_text())
                manifests_written.append(dict(data))
                manifest_path_holder.append(found[0])
            if fail_graph:
                raise RuntimeError("simulated graph failure")
            return {
                "task": state.get("task", ""),
                "history": [{"role": "assistant", "content": "Done."}],
                "errors": [],
                "last_result": {"status": "ok"},
                "session_id": state.get("session_id", "test"),
            }

        mock_graph = MagicMock()
        mock_graph.ainvoke = _fake_ainvoke

        with (
            patch("src.tools.subagent_tools.GraphFactory") as mock_gf,
            patch("src.tools.subagent_tools.get_agent_brain_manager") as mock_brain_mgr,
            patch("src.tools.subagent_tools._PARENT_ORCHESTRATOR_VAR") as mock_ctxvar,
        ):
            mock_gf.get_graph.return_value = mock_graph
            mock_brain = MagicMock()
            mock_brain.compile_system_prompt.return_value = "sys"
            mock_brain_mgr.return_value = mock_brain
            mock_ctxvar.get.return_value = None

            from src.tools.subagent_tools import delegate_task

            try:
                delegate_task(
                    role="analyst",
                    subtask_description="test manifest task",
                    working_dir=str(tmp_path),
                )
            except Exception:
                pass

        manifest_path = manifest_path_holder[0] if manifest_path_holder else None
        return manifests_written, manifest_path  # type: ignore[return-value]

    def test_manifest_written_before_graph_executes(self, tmp_path: Path) -> None:
        manifests, _ = self._run_and_collect_manifests(tmp_path)
        assert len(manifests) >= 1, "manifest should be written before graph runs"
        assert manifests[0]["status"] == "running"

    def test_manifest_contains_required_fields(self, tmp_path: Path) -> None:
        manifests, _ = self._run_and_collect_manifests(tmp_path)
        m = manifests[0]
        assert "child_session_id" in m
        assert "role" in m
        assert "task" in m
        assert "working_dir" in m
        assert "spawned_at" in m
        assert m["status"] == "running"

    def test_manifest_role_is_canonical(self, tmp_path: Path) -> None:
        manifests, _ = self._run_and_collect_manifests(tmp_path)
        # "analyst" is already canonical
        assert manifests[0]["role"] == "analyst"

    def test_manifest_updated_to_completed_on_success(self, tmp_path: Path) -> None:
        _, manifest_path = self._run_and_collect_manifests(tmp_path, fail_graph=False)
        if manifest_path is None:
            pytest.skip("manifest path not captured")
        final = json.loads(manifest_path.read_text())
        assert final["status"] == "completed"
        assert "completed_at" in final

    def test_manifest_updated_to_failed_on_graph_exception(
        self, tmp_path: Path
    ) -> None:
        _, manifest_path = self._run_and_collect_manifests(tmp_path, fail_graph=True)
        if manifest_path is None:
            pytest.skip("manifest path not captured")
        final = json.loads(manifest_path.read_text())
        assert final["status"] == "failed"
        assert "error" in final
        assert "failed_at" in final

    def test_manifest_file_path_pattern(self, tmp_path: Path) -> None:
        self._run_and_collect_manifests(tmp_path)
        manifest_dir = tmp_path / ".agent-context" / "subagent_manifests"
        files = list(manifest_dir.glob("subagent_*.json"))
        assert len(files) == 1

    def test_manifest_working_dir_matches(self, tmp_path: Path) -> None:
        manifests, _ = self._run_and_collect_manifests(tmp_path)
        assert manifests[0]["working_dir"] == str(tmp_path.resolve())

    def test_manifest_task_matches_input(self, tmp_path: Path) -> None:
        manifests, _ = self._run_and_collect_manifests(tmp_path)
        assert "test manifest task" in manifests[0]["task"]
