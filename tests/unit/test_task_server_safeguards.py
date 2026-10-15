"""Unit tests for task server safeguards: workspace allowlists, bounded admission, cancellation-before-start, and session identity (Audit Phase 1 Item 4 / Section 4)."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.server.app import app
from src.server.task_endpoints import (
    TaskStatus,
    _TASK_REGISTRY,
    _TASK_REGISTRY_LOCK,
    get_allowed_workspace_roots,
    set_allowed_workspace_roots,
)


@pytest.fixture
def test_client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_server_state():
    with _TASK_REGISTRY_LOCK:
        _TASK_REGISTRY.clear()
    set_allowed_workspace_roots(None)
    yield
    with _TASK_REGISTRY_LOCK:
        _TASK_REGISTRY.clear()
    set_allowed_workspace_roots(None)


class TestTaskWorkspaceRoots:
    """Test workspace root validation and denial of arbitrary host directories."""

    def test_denies_directory_outside_approved_roots(self, tmp_path, test_client):
        approved_root = tmp_path / "allowed_workspace"
        approved_root.mkdir()
        outside_dir = tmp_path / "outside_workspace"
        outside_dir.mkdir()

        set_allowed_workspace_roots([approved_root])

        resp = test_client.post(
            "/task",
            json={
                "task": "do something",
                "working_dir": str(outside_dir),
            },
        )
        assert resp.status_code == 403
        assert "not within an approved workspace root" in resp.json()["detail"]

    def test_denies_etc_directory(self, tmp_path, test_client):
        approved_root = tmp_path / "allowed_workspace"
        approved_root.mkdir()
        set_allowed_workspace_roots([approved_root])

        etc_path = Path("/etc").resolve()
        if etc_path.exists() and etc_path.is_dir():
            resp = test_client.post(
                "/task",
                json={
                    "task": "read etc",
                    "working_dir": str(etc_path),
                },
            )
            assert resp.status_code == 403
            assert "not within an approved workspace root" in resp.json()["detail"]

    def test_allows_directory_within_approved_roots(self, tmp_path, test_client):
        approved_root = tmp_path / "allowed_workspace"
        child_dir = approved_root / "project_a"
        child_dir.mkdir(parents=True)

        set_allowed_workspace_roots([approved_root])

        dummy_orch = MagicMock()
        dummy_orch.get_tools_for_role.return_value = {}
        dummy_orch.run_agent_once.return_value = {"ok": True, "assistant_message": "done"}

        with patch("src.server.app._get_or_create_orchestrator", return_value=dummy_orch):
            resp = test_client.post(
                "/task",
                json={
                    "task": "safe work",
                    "working_dir": str(child_dir),
                },
            )
        assert resp.status_code == 202
        assert resp.json()["status"] == "accepted"

    def test_defaults_to_cwd_when_no_env_or_explicit_roots(self):
        set_allowed_workspace_roots(None)
        roots = get_allowed_workspace_roots()
        assert len(roots) >= 1
        assert Path.cwd().resolve() in roots


class TestTaskBoundedAdmission:
    """Test server capacity and bounded queue rejection (HTTP 503)."""

    def test_rejects_when_queue_limit_exceeded(self, monkeypatch, test_client):
        import src.server.task_endpoints as task_ep

        # Set max queue limit to 2
        monkeypatch.setattr(task_ep, "_TASK_MAX_QUEUE", 2)

        # Pre-fill registry with 2 accepted tasks waiting in queue
        with _TASK_REGISTRY_LOCK:
            for i in range(2):
                _TASK_REGISTRY[f"task-{i}"] = {
                    "task_id": f"task-{i}",
                    "session_id": f"session-{i}",
                    "status": TaskStatus.ACCEPTED,
                    "result": None,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                    "cancel_event": threading.Event(),
                }

        # 3rd submission must be rejected with 503
        resp = test_client.post("/task", json={"task": "excess task"})
        assert resp.status_code == 503
        assert "server overloaded" in resp.json()["detail"].lower()


class TestTaskCancellationBeforeStart:
    """Test that cancelled queued tasks do not construct orchestrators or execute."""

    def test_cancelled_queued_task_aborts_without_calling_orchestrator(self, test_client):
        import src.server.task_endpoints as task_ep

        task1_started = threading.Event()
        task1_block = threading.Event()

        def block_run(*args, **kwargs):
            task1_started.set()
            task1_block.wait(timeout=5)
            return {"ok": True, "assistant_message": "task1 done"}

        orch1 = MagicMock()
        orch1.get_tools_for_role.return_value = {}
        orch1.run_agent_once.side_effect = block_run

        mock_orch2_factory = MagicMock()

        def orch_factory(working_dir, model):
            if not task1_started.is_set():
                return orch1
            return mock_orch2_factory(working_dir, model)

        # Saturate executor with max_workers tasks or submit task2 while task1 blocks
        with patch.object(task_ep, "_TASK_EXECUTOR", ThreadPoolExecutor(max_workers=1)):
            with patch("src.server.app._get_or_create_orchestrator", side_effect=orch_factory):
                resp1 = test_client.post("/task", json={"task": "task 1"})
                assert resp1.status_code == 202
                # Wait until task 1 has actually started running
                assert task1_started.wait(timeout=2)

                # Now submit task 2 — it MUST wait in ACCEPTED queue since max_workers=1 is busy
                resp2 = test_client.post("/task", json={"task": "task 2"})
                assert resp2.status_code == 202
                task2_id = resp2.json()["task_id"]

                # Verify task 2 is in ACCEPTED status
                status2 = test_client.get(f"/task/{task2_id}")
                assert status2.json()["status"] == "accepted"

                # Cancel task 2 while queued
                cancel_resp = test_client.post(f"/task/{task2_id}/cancel")
                assert cancel_resp.status_code == 200

                # Unblock task 1 so executor can proceed to task 2
                task1_block.set()
                time.sleep(0.3)

                # Task 2 status should be cancelled
                status2_after = test_client.get(f"/task/{task2_id}")
                assert status2_after.json()["status"] == "cancelled"

                # Orchestrator for task 2 was never created
                mock_orch2_factory.assert_not_called()


class TestTaskSessionPropagation:
    """Test session identity propagation from HTTP requests to orchestrator."""

    def test_session_id_propagates_to_orchestrator(self, test_client):
        dummy_orch = MagicMock()
        dummy_orch.get_tools_for_role.return_value = {}
        dummy_orch.run_agent_once.return_value = {"ok": True, "assistant_message": "done"}
        dummy_orch.session_mgr = MagicMock()

        custom_session_id = "test-custom-session-12345"

        with patch("src.server.app._get_or_create_orchestrator", return_value=dummy_orch):
            resp = test_client.post(
                "/task",
                json={
                    "task": "propagate session",
                    "session_id": custom_session_id,
                },
            )
            assert resp.status_code == 202
            time.sleep(0.3)

        assert dummy_orch._current_task_id == custom_session_id
        assert dummy_orch.session_id == custom_session_id
        assert dummy_orch.session_mgr.task_id == custom_session_id
        dummy_orch.session_mgr.update_session_state.assert_called_with(session_id=custom_session_id)
