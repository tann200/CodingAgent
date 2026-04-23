import time
from src.core.orchestration import orchestrator_bootstrap as ob
from src.core.scheduler import worker as sched


def teardown_function(_):
    try:
        sched.stop_scheduler()
    except Exception:
        pass
    try:
        sched.clear_jobs()
    except Exception:
        pass


def test_scheduler_job_registration_respects_config(monkeypatch):
    # Ensure clean state
    sched.clear_jobs()

    # Fake orchestrator with minimal attributes
    class FakeOrch:
        def __init__(self):
            from src.core.orchestration.event_bus import get_event_bus

            self.event_bus = get_event_bus()
            self.working_dir = None
            self.lifecycle_manager = type(
                "L", (), {"on_shutdown": lambda *a, **k: None}
            )()

    fo = FakeOrch()

    # Case 1: disable periodic_distill_request via config
    monkeypatch.setattr(
        "src.core.config_loader.get",
        lambda k, d=None: {"periodic_distill_request": {"enabled": False}}
        if k == "scheduler_jobs"
        else d,
    )

    # Prevent starting actual scheduler thread for this test by stubbing start_scheduler
    original_start = sched.start_scheduler
    monkeypatch.setattr(
        "src.core.scheduler.worker.start_scheduler", lambda *a, **k: None
    )

    try:
        ob._init_scheduler(fo)
        jobs = sched.list_jobs()
        assert "periodic_distill_request" not in jobs
    finally:
        # restore
        monkeypatch.setattr("src.core.scheduler.worker.start_scheduler", original_start)
        sched.clear_jobs()

    # Case 2: enable with custom interval
    monkeypatch.setattr(
        "src.core.config_loader.get",
        lambda k, d=None: {
            "periodic_distill_request": {"enabled": True, "interval": 123}
        }
        if k == "scheduler_jobs"
        else d,
    )
    monkeypatch.setattr(
        "src.core.scheduler.worker.start_scheduler", lambda *a, **k: None
    )
    try:
        ob._init_scheduler(fo)
        jobs = sched.list_jobs()
        assert "periodic_distill_request" in jobs
        assert int(jobs["periodic_distill_request"]["interval"]) == 123
    finally:
        # cleanup
        sched.clear_jobs()
