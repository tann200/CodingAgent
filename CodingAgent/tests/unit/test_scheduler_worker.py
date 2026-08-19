import time
from src.core.orchestration.event_bus import get_event_bus


def test_scheduler_runs_job_and_heartbeat(monkeypatch):
    bus = get_event_bus()
    # simple counter captured by closure
    state = {"ran": 0, "heartbeats": 0}

    def job():
        state["ran"] += 1

    def on_hb(payload):
        state["heartbeats"] += 1

    bus.subscribe("scheduler.heartbeat", on_hb)

    # Start scheduler with a short heartbeat and register job with short interval
    from src.core.scheduler import worker as sched

    # Ensure clean state from any previous test runs
    try:
        sched.stop_scheduler()
    except Exception:
        pass
    try:
        sched._JOBS.clear()
    except Exception:
        pass

    sched.register_job("test_job", job, interval_seconds=1)
    sched.start_scheduler(bus, heartbeat_interval=1)

    # Wait up to 3 seconds for job to run a couple times
    time.sleep(2.5)

    try:
        assert state["ran"] >= 1
        assert state["heartbeats"] >= 1
    finally:
        sched.stop_scheduler()
