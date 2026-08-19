
from fastapi.testclient import TestClient

from src.core.orchestration.event_bus import EventBus
from src.server.app import app, register_event_bus


def test_sse_integration_client_receives_corrective_prompt():
    bus = EventBus()

    # Start the TestClient context (runs lifespan)
    with TestClient(app) as client:
        # Register our EventBus so the server uses it
        register_event_bus(bus)

        # Publish a corrective prompt event and assert metrics changed
        payload = {
            "session_id": "s1",
            "attempt": 1,
            "reason": "empty_response",
            "truncated_yaml": False,
            "model_tier": "NANO",
        }
        bus.publish("perception.corrective_prompt", payload)

        # Query /metrics to ensure the corrective-prompt counter incremented
        resp = client.get("/metrics")
        assert resp.status_code == 200
        text = resp.text
        assert "codingagent_corrective_prompts_total" in text
        assert 'reason="empty_response"' in text
