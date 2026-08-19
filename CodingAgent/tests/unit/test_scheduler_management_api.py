import time
from src.core.scheduler import worker as sched


def teardown_function(_):
    # Ensure global scheduler state is clean between tests
    try:
        sched.stop_scheduler()
    except Exception:
        pass
    try:
        sched.clear_jobs()
    except Exception:
        pass


def test_register_list_unregister_clear_jobs():
    # Start fresh
    sched.clear_jobs()

    called = {"a": 0, "b": 0}

    def job_a():
        called["a"] += 1

    def job_b():
        called["b"] += 1

    # Register two jobs with short intervals
    sched.register_job("job_a", job_a, 1)
    sched.register_job("job_b", job_b, 2)

    jobs = sched.list_jobs()
    assert "job_a" in jobs and "job_b" in jobs

    # Start the scheduler and let it run a couple of heartbeats
    sched.start_scheduler(None, heartbeat_interval=0.5)
    time.sleep(1.2)

    # Jobs should have run at least once
    assert called["a"] >= 1

    # Unregister job_b and ensure it is removed
    removed = sched.unregister_job("job_b")
    assert removed is True
    jobs = sched.list_jobs()
    assert "job_b" not in jobs

    # Clear remaining jobs
    sched.clear_jobs()
    jobs = sched.list_jobs()
    assert jobs == {}
