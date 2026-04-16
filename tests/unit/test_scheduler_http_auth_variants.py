from fastapi.testclient import TestClient
from src.server.app import app, register_event_bus
from src.core.orchestration.event_bus import get_event_bus
from src.core.scheduler import worker as sched


def setup_function(_):
    try:
        sched.stop_scheduler()
    except Exception:
        pass
    try:
        sched.clear_jobs()
    except Exception:
        pass


def test_x_codingagent_token_header_auth(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "htoken")
    with TestClient(app) as client:
        register_event_bus(get_event_bus())

        # Register a factory so the endpoint has content
        sched.register_job_factory("foo", lambda: None, 60)

        # No auth -> 401
        r = client.get("/scheduler/jobs")
        assert r.status_code == 401

        # X-CodingAgent-Token header works
        r = client.get("/scheduler/jobs", headers={"X-CodingAgent-Token": "htoken"})
        assert r.status_code == 200

        # Metrics endpoint should include auth counters
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "codingagent_admin_auth_total" in metrics.text
