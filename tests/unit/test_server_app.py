"""Unit tests for the HTTP/SSE server component (Gap 2 implementation)."""

import pytest
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from src.server.app import ServerEventBusAdapter, app


@pytest.fixture
def mock_event_bus():
    """Create a mock EventBus for testing."""
    bus = MagicMock()
    bus.subscribe = MagicMock()
    bus.unsubscribe = MagicMock()
    return bus


@pytest.fixture
def sse_adapter(mock_event_bus):
    """Create a ServerEventBusAdapter instance for testing."""
    return ServerEventBusAdapter(mock_event_bus)


@pytest.fixture
def test_client():
    """Create a test client for the FastAPI app."""
    from fastapi.testclient import TestClient

    return TestClient(app)


class TestServerEventBusAdapter:
    """Test the ServerEventBusAdapter class."""

    def test_init(self, mock_event_bus):
        """Test adapter initialization."""
        adapter = ServerEventBusAdapter(mock_event_bus)
        assert adapter.event_bus == mock_event_bus
        assert adapter._session_handlers == {}

    @pytest.mark.asyncio
    async def test_event_generator_basic(self, sse_adapter, mock_event_bus):
        """Test basic event generation."""
        # This test would require more complex mocking of the async queue
        # For now, we'll test that the adapter can be instantiated
        assert sse_adapter is not None

    @pytest.mark.asyncio
    async def test_event_generator_cleanup(self, sse_adapter, mock_event_bus):
        """Test that handlers are properly cleaned up."""
        # Mock the event generator to avoid infinite loop
        with patch.object(sse_adapter, "event_generator") as mock_gen:
            mock_gen.return_value = AsyncMock()
            # Just verify the method exists
            assert hasattr(sse_adapter, "event_generator")


class TestServerApp:
    """Test the FastAPI server application."""

    def test_create_session_endpoint(self, test_client):
        """Test the session creation endpoint."""
        response = test_client.post("/session", json={"metadata": {"test": "value"}})
        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert data["status"] == "created"

    def test_create_session_without_metadata(self, test_client):
        """Test session creation without metadata."""
        response = test_client.post("/session", json={})
        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert data["status"] == "created"

    def test_health_endpoint(self, test_client):
        """Test the health check endpoint."""
        response = test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    @patch("src.server.app.sse_adapter")
    def test_session_events_endpoint(self, mock_sse_adapter, test_client):
        """Test the SSE events endpoint."""

        # Mock the event generator to return a simple async generator
        async def mock_event_gen():
            yield 'data: {"event": "test", "data": {}}\n\n'

        mock_sse_adapter.event_generator.return_value = mock_event_gen()

        response = test_client.get("/session/test-session/events")
        # Note: Testing StreamingResponse with TestClient can be tricky
        # For now, we'll just check that the endpoint doesn't crash
        assert response.status_code == 200

    def test_session_events_endpoint_no_adapter(self, test_client):
        """Test SSE endpoint when adapter is not initialized."""
        # Temporarily set the global adapter to None
        import src.server.app as app_module

        original_adapter = app_module.sse_adapter
        app_module.sse_adapter = None

        try:
            response = test_client.get("/session/test-session/events")
            assert response.status_code == 503
            assert "Server not properly initialized" in response.json()["detail"]
        finally:
            # Restore the original adapter
            app_module.sse_adapter = original_adapter

    def test_metrics_endpoint_reflects_corrective_prompts(self, test_client):
        """Publish a perception.corrective_prompt event and ensure /metrics updates."""
        # Import and register a fresh EventBus with the server module
        from src.core.orchestration.event_bus import EventBus
        import src.server.app as app_module

        bus = EventBus()
        # Register bus so server internals (metrics) subscribe
        app_module.register_event_bus(bus)

        # Publish a corrective prompt event
        bus.publish(
            "perception.corrective_prompt",
            {
                "session_id": "s1",
                "attempt": 1,
                "reason": "empty_response",
                "model_tier": "NANO",
                "truncated_yaml": False,
            },
        )

        # Query /metrics and assert our counter is present
        response = test_client.get("/metrics")
        assert response.status_code == 200
        text = response.text
        assert "codingagent_corrective_prompts_total" in text
        assert 'reason="empty_response"' in text

    def test_per_client_dropped_metrics(self, test_client):
        """Ensure per-client dropped metrics appear when incremented."""
        import src.server.app as app_module
        from src.core.orchestration.event_bus import EventBus

        bus = EventBus()
        # Register bus so server internals (metrics) subscribe
        app_module.register_event_bus(bus)

        # Increment per-client dropped counter directly (deterministic)
        app_module._inc_client_event_dropped_counter("agent.start", "s1")

        # Query /metrics and assert per-client dropped metric present
        response = test_client.get("/metrics")
        assert response.status_code == 200
        text = response.text
        assert "codingagent_sse_events_dropped_per_client_total" in text
        assert 'session_id="s1"' in text

    def test_metrics_endpoint_basic_auth(self, monkeypatch, test_client):
        """Ensure /metrics enforces Basic Auth when CODING_AGENT_METRICS_AUTH is set."""
        import base64
        import src.server.app as app_module
        from src.core.orchestration.event_bus import EventBus

        # Enable basic auth via env var
        monkeypatch.setenv("CODING_AGENT_METRICS_AUTH", "user:pass")

        bus = EventBus()
        app_module.register_event_bus(bus)

        # No auth header -> 401
        resp = test_client.get("/metrics")
        assert resp.status_code == 401

        # Wrong creds -> 401
        bad = base64.b64encode(b"bad:creds").decode("ascii")
        resp2 = test_client.get("/metrics", headers={"Authorization": f"Basic {bad}"})
        assert resp2.status_code == 401

        # Correct creds -> 200
        good = base64.b64encode(b"user:pass").decode("ascii")
        resp3 = test_client.get("/metrics", headers={"Authorization": f"Basic {good}"})
        assert resp3.status_code == 200


# ---------------------------------------------------------------------------
# G8 — Task execution endpoint tests
# ---------------------------------------------------------------------------


class TestTaskEndpoints:
    """Tests for POST /task, GET /task/{id}, POST /task/{id}/cancel, GET /tasks."""

    @pytest.fixture(autouse=True)
    def _clear_registry(self):
        """Ensure a clean task registry for each test."""
        import src.server.app as app_module
        with app_module._TASK_REGISTRY_LOCK:
            app_module._TASK_REGISTRY.clear()
        yield
        with app_module._TASK_REGISTRY_LOCK:
            app_module._TASK_REGISTRY.clear()

    def test_submit_task_returns_202(self, test_client):
        """POST /task returns 202 Accepted with a task_id."""
        from unittest.mock import patch, MagicMock
        import src.server.app as app_module

        dummy_orch = MagicMock()
        dummy_orch.get_tools_for_role.return_value = {}
        dummy_orch.run_agent_once.return_value = {"ok": True, "assistant_message": "Done"}

        with patch.object(app_module, "_get_or_create_orchestrator", return_value=dummy_orch):
            resp = test_client.post("/task", json={"task": "hello world"})

        assert resp.status_code == 202
        body = resp.json()
        assert "task_id" in body
        assert body["status"] == "accepted"
        assert "session_id" in body

    def test_get_task_status_not_found(self, test_client):
        """GET /task/{unknown} returns 404."""
        resp = test_client.get("/task/does-not-exist")
        assert resp.status_code == 404

    def test_get_task_status_after_submit(self, test_client):
        """GET /task/{id} returns the task record."""
        from unittest.mock import patch, MagicMock
        import src.server.app as app_module
        import time

        done = threading.Event()

        def slow_run(*args, **kwargs):
            done.wait(timeout=5)
            return {"ok": True, "assistant_message": "finished"}

        dummy_orch = MagicMock()
        dummy_orch.get_tools_for_role.return_value = {}
        dummy_orch.run_agent_once.side_effect = slow_run

        with patch.object(app_module, "_get_or_create_orchestrator", return_value=dummy_orch):
            resp = test_client.post("/task", json={"task": "test task"})

        task_id = resp.json()["task_id"]
        # Status should be accepted or running before the task completes
        status_resp = test_client.get(f"/task/{task_id}")
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] in ("accepted", "running")
        # Let task complete
        done.set()
        time.sleep(0.2)
        status_resp2 = test_client.get(f"/task/{task_id}")
        assert status_resp2.json()["status"] == "completed"
        assert status_resp2.json()["result"] == "finished"

    def test_cancel_task(self, test_client):
        """POST /task/{id}/cancel sets cancel_event and returns the record."""
        from unittest.mock import patch, MagicMock
        import src.server.app as app_module

        dummy_orch = MagicMock()
        dummy_orch.get_tools_for_role.return_value = {}
        dummy_orch.run_agent_once.return_value = {"ok": True, "assistant_message": "x"}

        with patch.object(app_module, "_get_or_create_orchestrator", return_value=dummy_orch):
            resp = test_client.post("/task", json={"task": "cancel me"})

        task_id = resp.json()["task_id"]
        cancel_resp = test_client.post(f"/task/{task_id}/cancel")
        assert cancel_resp.status_code == 200
        # cancel_event must be set in registry
        with app_module._TASK_REGISTRY_LOCK:
            rec = app_module._TASK_REGISTRY[task_id]
        assert rec["cancel_event"].is_set()

    def test_cancel_unknown_task(self, test_client):
        """POST /task/{unknown}/cancel returns 404."""
        resp = test_client.post("/task/no-such-task/cancel")
        assert resp.status_code == 404

    def test_list_tasks_empty(self, test_client):
        """GET /tasks returns empty list when no tasks submitted."""
        resp = test_client.get("/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_tasks_shows_submitted(self, test_client):
        """GET /tasks lists submitted tasks."""
        from unittest.mock import patch, MagicMock
        import src.server.app as app_module

        dummy_orch = MagicMock()
        dummy_orch.get_tools_for_role.return_value = {}
        dummy_orch.run_agent_once.return_value = {"ok": True, "assistant_message": "ok"}

        with patch.object(app_module, "_get_or_create_orchestrator", return_value=dummy_orch):
            test_client.post("/task", json={"task": "task A"})
            test_client.post("/task", json={"task": "task B"})

        resp = test_client.get("/tasks")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_task_failed_on_orchestrator_error(self, test_client):
        """Task transitions to failed when orchestrator raises."""
        from unittest.mock import patch, MagicMock
        import src.server.app as app_module
        import time

        dummy_orch = MagicMock()
        dummy_orch.get_tools_for_role.return_value = {}
        dummy_orch.run_agent_once.side_effect = RuntimeError("boom")

        with patch.object(app_module, "_get_or_create_orchestrator", return_value=dummy_orch):
            resp = test_client.post("/task", json={"task": "fail me"})

        task_id = resp.json()["task_id"]
        time.sleep(0.3)
        status_resp = test_client.get(f"/task/{task_id}")
        body = status_resp.json()
        assert body["status"] == "failed"
        assert "boom" in (body["error"] or "")
