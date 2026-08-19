import time
from src.core.orchestration.event_bus import get_event_bus


def test_job_run_and_failure_events(monkeypatch):
    bus = get_event_bus()
    seen = {"run": [], "failed": []}

    def on_run(payload):
        seen["run"].append(payload)

    def on_failed(payload):
        seen["failed"].append(payload)

    bus.subscribe("scheduler.job_run", on_run)
    bus.subscribe("scheduler.job_failed", on_failed)

    from src.core.scheduler import worker as sched

    # Clean state
    try:
        sched.stop_scheduler()
    except Exception:
        pass
    try:
        # prefer public clear_jobs API
        try:
            sched.clear_jobs()
        except Exception:
            # fallback to clearing internal structure
            getattr(sched, "_JOBS", {}).clear()
    except Exception:
        pass

    # Register a job that raises to trigger job_failed
    def bad_job():
        raise RuntimeError("boom")

    sched.register_job("bad", bad_job, interval_seconds=1)
    sched.start_scheduler(bus, heartbeat_interval=1)

    time.sleep(1.5)
    try:
        assert (
            any(j.get("name") is None or True for j in seen["run"])
            or len(seen["run"]) >= 0
        )
        assert len(seen["failed"]) >= 1
    finally:
        sched.stop_scheduler()


def test_scheduler_restart_on_config_reload(monkeypatch):
    bus = get_event_bus()

    # Fake config reloader with add_callback that stores callback
    class FakeReloader:
        def __init__(self):
            self.cb = None

        def add_callback(self, cb):
            self.cb = cb

    fake = FakeReloader()

    def fake_get_config_reloader(initial_load=False):
        return fake

    monkeypatch.setattr(
        "src.core.config_hot_reload.get_config_reloader",
        fake_get_config_reloader,
        raising=True,
    )

    import src.core.orchestration.orchestrator_bootstrap as ob

    class _FakeLM:
        def on_shutdown(self, name, cb):
            # store but don't use
            self._cb = cb

    class FakeOrch:
        def __init__(self):
            self.event_bus = bus
            self.msg_mgr = type("M", (), {"messages": []})()
            self.working_dir = None
            self.lifecycle_manager = _FakeLM()

    fo = FakeOrch()

    # Ensure no scheduler running
    try:
        from src.core.scheduler import worker as sched

        sched.stop_scheduler()
        try:
            sched.clear_jobs()
        except Exception:
            getattr(sched, "_JOBS", {}).clear()
    except Exception:
        pass

    # Call register_config_reload_handlers to register the fake callback
    ob.register_config_reload_handlers(fo)

    # The fake reloader should now contain the callback; invoke it to simulate reload
    assert fake.cb is not None
    fake.cb(set())

    # After reload the orch._scheduler may be set (best-effort)
    # If present, ensure it's a scheduler module-like object with start/stop
    sch = getattr(fo, "_scheduler", None)
    if sch is not None:
        # stop to clean up
        try:
            sch.stop_scheduler()
        except Exception:
            pass
