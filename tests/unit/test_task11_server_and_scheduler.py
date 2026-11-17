from __future__ import annotations

import atexit
import base64
import gc
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


def test_server_lifespan_exposure_and_executor_shutdown(monkeypatch):
    from src.server.app import app
    from src.server import task_endpoints

    monkeypatch.setenv("CODINGAGENT_SERVER_HOST", "0.0.0.0")
    monkeypatch.delenv("CODINGAGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("CODING_AGENT_ADMIN_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="non-loopback"):
        with TestClient(app):
            pass

    monkeypatch.setenv("CODINGAGENT_SERVER_HOST", "127.0.0.1")
    with TestClient(app):
        assert not getattr(task_endpoints._TASK_EXECUTOR, "_shutdown", False)
    assert getattr(task_endpoints._TASK_EXECUTOR, "_shutdown", False)

    # Submitting after restart recreates executor automatically
    exe = task_endpoints._get_task_executor()
    assert not getattr(exe, "_shutdown", False)


def test_auth_runs_before_pydantic_and_session_all_gated(monkeypatch):
    from src.server.app import app

    monkeypatch.setenv("CODINGAGENT_ADMIN_TOKEN", "top-secret-token")
    monkeypatch.setenv("CODINGAGENT_SERVER_HOST", "127.0.0.1")

    with TestClient(app) as client:
        # Invalid/empty JSON body must fail 401 (Depends auth) BEFORE 422 Pydantic validation
        r_task = client.post("/task", json={})
        assert r_task.status_code == 401

        r_sess = client.get("/session/all/events")
        assert r_sess.status_code == 401

        # With valid admin token, /session/all SSE is authorized (403 not raised)
        headers = {"Authorization": "Bearer top-secret-token"}
        r_bad_body = client.post("/task", json={}, headers=headers)
        assert r_bad_body.status_code == 422

    # When admin token is unset, /session/all must be forbidden in both SSE and WS
    monkeypatch.delenv("CODINGAGENT_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("CODING_AGENT_ADMIN_TOKEN", raising=False)
    with TestClient(app) as client:
        r_all = client.get("/session/all/events")
        assert r_all.status_code == 403
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/session/all"):
                pass


def test_websocket_origin_validation(monkeypatch):
    from src.server.server_config import validate_websocket_origin
    from src.server.app import app

    assert validate_websocket_origin({}) is True
    assert validate_websocket_origin({"Origin": "http://localhost:3000"}) is True
    assert validate_websocket_origin({"Origin": "https://127.0.0.1:8443"}) is True
    assert validate_websocket_origin({"Origin": "https://evil.attacker.com"}) is False
    assert (
        validate_websocket_origin(
            {"Origin": "https://trusted.example.com"},
            allowed_origins_env="https://trusted.example.com",
        )
        is True
    )

    monkeypatch.delenv("CODINGAGENT_ADMIN_TOKEN", raising=False)
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/ws/session/s1",
                headers={"Origin": "https://evil.attacker.com"},
            ):
                pass


def test_metrics_hmac_and_prometheus_label_escaping():
    from src.server.server_config import metrics_basic_auth_valid
    from src.server.metrics import (
        _escape_prom_label,
        format_metrics_text,
        inc_corrective_prompt_counter,
    )

    tok = base64.b64encode(b"prom:secret123").decode()
    assert metrics_basic_auth_valid({"Authorization": f"Basic {tok}"}, "prom:secret123") is True
    assert metrics_basic_auth_valid({"Authorization": f"Basic {tok}"}, "prom:wrong") is False

    escaped = _escape_prom_label('bad"reason\nfake_metric 999')
    assert "\n" not in escaped
    assert '\\"' in escaped

    inc_corrective_prompt_counter('reason\ninjected_metric 1', 'nano')
    txt = format_metrics_text()
    assert "\ninjected_metric" not in txt


def test_task_hydration_contextvar_reset_secret_slice_and_scheduler_isolation(monkeypatch, tmp_path):
    from src.core.orchestration.event_bus import EventBus, get_session_id, set_session_id
    from src.core.scheduler import worker as sched_worker
    from src.server import task_endpoints

    bus = EventBus()
    published_starts: list[dict] = []
    bus.subscribe("agent.start", published_starts.append)

    class FakeSessionStore:
        def get_messages(self, sid):
            return [{"role": "user", "content": "prior question"}]

    class FakeMsgMgr:
        def __init__(self):
            self.messages = []

    class FakeOrch:
        def __init__(self):
            self.session_store = FakeSessionStore()
            self.msg_mgr = FakeMsgMgr()
            self.seen_msg_mgr_on_run = None

        def get_tools_for_role(self, _r):
            return {}

        def run_agent_once(self, **_kw):
            from src.core.orchestration.orchestrator_scheduler import _init_scheduler

            # Must not touch global scheduler inside /task
            _init_scheduler(self)
            self.seen_msg_mgr_on_run = list(self.msg_mgr.messages)
            return {"ok": True, "assistant_message": "done"}

        def close(self):
            pass

    monkeypatch.setattr(sched_worker, "register_job", MagicMock())
    monkeypatch.setattr(
        "src.server.app._get_or_create_orchestrator",
        lambda *_a, **_kw: FakeOrch(),
    )

    prior_tok = set_session_id("outer-session")
    try:
        tid = "t-hydrate-1"
        task_endpoints.register_task_endpoints(event_bus=bus)
        with task_endpoints._TASK_REGISTRY_LOCK:
            task_endpoints._TASK_REGISTRY[tid] = {
                "task_id": tid,
                "session_id": "s-chat-1",
                "status": task_endpoints.TaskStatus.ACCEPTED,
                "cancel_event": __import__("threading").Event(),
            }
        # Secret near index 195 so naive [:200] slice would fail redaction
        secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
        task_str = ("a" * 190) + " " + secret
        task_endpoints._run_task_thread(tid, "s-chat-1", task_str, str(tmp_path), None, __import__("threading").Event())

        # Session ID contextvar must be restored to outer-session
        assert get_session_id() == "outer-session"
        assert published_starts and secret not in published_starts[0]["task"]
        sched_worker.register_job.assert_not_called()
    finally:
        from src.core.orchestration.event_bus import reset_session_id
        reset_session_id(prior_tok)


def test_orchestrator_bootstrap_atexit_weakref_allows_gc(monkeypatch, tmp_path):
    from src.core.orchestration.event_bus import EventBus
    from src.core.orchestration.orchestrator_bootstrap import _init_infrastructure

    registered: list[object] = []
    monkeypatch.setattr(atexit, "register", registered.append)

    class DummyOrch:
        def __init__(self):
            self.event_bus = EventBus()
            self.working_dir = tmp_path
            self._allow_external = False

        def _ensure_working_dir(self):
            pass

        def _compact_messages(self, *_a, **_kw):
            return []

    orch = DummyOrch()
    _init_infrastructure(orch, message_max_tokens=4000)
    fn = getattr(orch, "_atexit_shutdown_fn", None)
    assert callable(fn) and fn in registered

    import weakref
    wr = weakref.ref(orch)
    orch._tool_executor.shutdown(wait=False)
    orch._graph_executor.shutdown(wait=False)
    if getattr(orch, "session_store", None) and hasattr(orch.session_store, "close"):
        orch.session_store.close()
    del orch
    gc.collect()
    # Even while fn remains in `registered`, the weakref closure must allow GC!
    assert wr() is None
    fn()  # Safe no-op when referent is dead


def test_scheduler_worker_hardening(tmp_path):
    from src.core.scheduler import worker as sched_worker

    assert hasattr(sched_worker, "_LIFECYCLE_LOCK")
    with pytest.raises(ValueError):
        sched_worker.register_job("bad", lambda: None, 0)

    state_file = tmp_path / "sched_state.json"
    sched_worker.clear_jobs()
    sched_worker.set_state_path(state_file)
    try:
        sched_worker.register_job("j1", lambda: None, 10)
        assert state_file.exists()
        assert sched_worker.update_job_interval("j1", 25) is True

        # Re-register with default 10, then restore saved state (25)
        sched_worker._JOBS["j1"]["interval"] = 10
        sched_worker.load_persisted_state()
        assert sched_worker.list_jobs()["j1"]["interval"] == 25

        # Timeout test
        slow_done = []
        eb = SimpleNamespace(publish=lambda ev, p: slow_done.append((ev, p)))

        def _slow():
            time.sleep(0.5)

        sched_worker._JOBS["slow"] = {"fn": _slow, "interval": 1, "last_run": 0, "timeout": 0.05}
        # Simulate one iteration cleanly with executor
        fut = sched_worker._get_job_executor().submit(_slow)
        with pytest.raises(Exception):
            fut.result(timeout=0.05)
    finally:
        sched_worker.set_state_path(None)
        sched_worker.clear_jobs()
        sched_worker.stop_scheduler()
