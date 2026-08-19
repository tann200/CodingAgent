from src.tools.todo_tools import (
    notify_rbw,
    get_rbw_metrics,
    reset_rbw_metrics,
)


class BadOrchestrator:
    @property
    def _session_read_files(self):
        class BadSet:
            def add(self, *args, **kwargs):
                raise RuntimeError("intentional failure")

        return BadSet()


def test_notify_rbw_increments_metrics(tmp_path):
    reset_rbw_metrics()
    workdir = tmp_path / "repo"
    workdir.mkdir()

    # Case 1: explicit orchestrator that fails to accept updates
    bad = BadOrchestrator()
    notify_rbw(str(workdir), orchestrator=bad)
    metrics = get_rbw_metrics()
    assert metrics["rbw_notify_attempts"] >= 1
    assert metrics["rbw_notify_failures"] >= 1

    # Case 2: fallback path (no orchestrator); this will attempt ContextBuilder
    notify_rbw(str(workdir), orchestrator=None)
    metrics = get_rbw_metrics()
    assert metrics["rbw_notify_attempts"] >= 2
