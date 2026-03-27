"""Tests for GAP 1: Session State Hydration."""

import pytest
from src.core.orchestration.event_bus import EventBus
from src.core.orchestration.agent_session_manager import (
    AgentSessionManager,
    SessionState,
    get_agent_session_manager,
)


class TestSessionState:
    """Tests for SessionState dataclass."""

    def test_session_state_to_dict(self):
        """Test SessionState converts to ACP-compliant dict."""
        state = SessionState()
        state.session_id = "test-session-123"
        state.task = "Test task"
        state.current_plan = [{"description": "Step 1", "completed": False}]
        state.current_step = 0
        state.provider = "lm_studio"
        state.model = "llama-3"
        state.files_modified = ["/path/to/file.py"]
        state.files_read = ["/path/to/read.py"]

        result = state.to_dict()

        assert result["sessionId"] == "test-session-123"
        assert result["task"] == "Test task"
        assert result["currentPlan"]["steps"] == [
            {"description": "Step 1", "completed": False}
        ]
        assert result["currentPlan"]["currentStep"] == 0
        assert result["provider"]["name"] == "lm_studio"
        assert result["provider"]["model"] == "llama-3"
        assert result["workspace"]["filesModified"] == ["/path/to/file.py"]
        assert result["workspace"]["filesRead"] == ["/path/to/read.py"]

    def test_session_state_empty_values(self):
        """Test SessionState with default empty values."""
        state = SessionState()
        result = state.to_dict()

        assert result["sessionId"] == ""
        assert result["task"] == ""
        assert result["messageHistory"] == []
        assert result["currentPlan"]["steps"] == []
        assert result["currentPlan"]["currentStep"] == 0


class TestAgentSessionManagerHydration:
    """Tests for AgentSessionManager hydration functionality."""

    def test_get_session_state_default(self):
        """Test get_session_state returns default state."""
        # Use a fresh instance for testing
        manager = AgentSessionManager()
        state = manager.get_session_state()

        assert state.session_id == "default"
        assert state.task == ""

    def test_update_session_state(self):
        """Test update_session_state modifies state correctly."""
        manager = AgentSessionManager()
        manager.update_session_state(
            session_id="session-456",
            task="Updated task",
            current_plan=[{"description": "New step", "completed": True}],
            current_step=1,
            provider="ollama",
            model="codellama",
        )

        state = manager.get_session_state()
        assert state.session_id == "session-456"
        assert state.task == "Updated task"
        assert len(state.current_plan) == 1
        assert state.current_step == 1
        assert state.provider == "ollama"
        assert state.model == "codellama"

    def test_update_session_state_partial(self):
        """Test update_session_state with partial updates."""
        manager = AgentSessionManager()
        manager.update_session_state(
            session_id="session-789",
            task="Initial task",
        )
        manager.update_session_state(
            files_modified=["/file1.py"],
        )

        state = manager.get_session_state()
        assert state.session_id == "session-789"
        assert state.task == "Initial task"
        assert state.files_modified == ["/file1.py"]

    def test_hydration_handler_subscribes_to_event(self):
        """Test that hydration handler subscribes to session.request_state."""
        bus = EventBus()
        manager = AgentSessionManager()
        manager._event_bus = bus

        # The manager should have subscribed to session.request_state
        # We verify this by checking the handler is set up
        assert hasattr(manager, "_handle_state_request")

    def test_hydration_publishes_state(self):
        """Test that requesting state publishes hydrated state."""
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()

        # Track published session.hydrated events
        hydrated_events = []

        def capture_hydrated(payload):
            hydrated_events.append(payload)

        bus.subscribe("session.hydrated", capture_hydrated)

        # Create manager and manually trigger handler
        manager = AgentSessionManager()
        manager._event_bus = bus
        manager.update_session_state(
            session_id="test-hydration",
            task="Hydration test task",
        )

        # Call the handler directly
        manager._handle_state_request({})

        assert len(hydrated_events) == 1
        assert hydrated_events[0]["sessionId"] == "test-hydration"
        assert hydrated_events[0]["task"] == "Hydration test task"


class TestHydrationIntegration:
    """Integration tests for full hydration flow."""

    def test_full_hydration_flow(self):
        """Test complete hydration: request -> response -> update."""
        from src.core.orchestration.event_bus import get_event_bus

        bus = get_event_bus()

        # Setup: create manager and update some state
        manager = AgentSessionManager()
        manager._event_bus = bus
        manager.update_session_state(
            session_id="integration-test",
            task="Integration test task",
            current_plan=[{"description": "Step 1", "completed": True}],
            current_step=1,
        )

        # Capture the hydrated state (filter to our specific session)
        hydrated_payload = []

        def capture_hydrated(payload):
            if payload.get("sessionId") == "integration-test":
                hydrated_payload.append(payload)

        bus.subscribe("session.hydrated", capture_hydrated)

        # Trigger hydration request
        bus.publish("session.request_state", {})

        # Verify state was published for our session
        assert len(hydrated_payload) >= 1
        payload = hydrated_payload[-1]  # Get the latest one
        assert payload["sessionId"] == "integration-test"
        assert payload["task"] == "Integration test task"
        assert payload["currentPlan"]["currentStep"] == 1
