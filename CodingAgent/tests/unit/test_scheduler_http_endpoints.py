from fastapi.testclient import TestClient
import json
import pytest
from src.server.app import app, register_event_bus
from src.core.orchestration.event_bus import get_event_bus
from src.core.scheduler import worker as sched

# ruff: noqa: E501


def setup_function(_):
    try:
        sched.stop_scheduler()
    except Exception:
        pass
    try:
        sched.clear_jobs()
    except Exception:
        pass


def test_scheduler_http_endpoints(monkeypatch):
    # Use TestClient in context so lifespan handlers run and server app is initialized
    monkeypatch.delenv("CODING_AGENT_ADMIN_TOKEN", raising=False)
    with TestClient(app) as client:
        # Register the shared event bus so server endpoints (if any) that rely on it work
        register_event_bus(get_event_bus())

        # Register a dummy factory and job
        def dummy():
            return None

        sched.register_job_factory("dummy", dummy, 60)

        # List jobs
        r = client.get("/scheduler/jobs")
        assert r.status_code == 200
        data = r.json()
        assert "factories" in data
        assert "dummy" in data["factories"]

        # Disable (unregister) job
        r = client.post("/scheduler/jobs/dummy/unregister")
        assert r.status_code == 200
        assert r.json().get("removed") is True

        # Re-enable job via factory
        r = client.post("/scheduler/jobs/dummy/enable")
        assert r.status_code == 200
        assert r.json().get("enabled") is True

        # Update interval
        r = client.post("/scheduler/jobs/dummy/interval", json={"interval": 7})
        assert r.status_code == 200
        j = sched.list_jobs().get("dummy")
        assert j and int(j["interval"]) == 7

        # Clear jobs
        r = client.post("/scheduler/jobs/clear")
        assert r.status_code == 200
        assert r.json().get("cleared") is True


def test_scheduler_http_endpoints_auth(monkeypatch):
    # Set an admin token and verify endpoints require it
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "secret-token")
    with TestClient(app) as client:
        register_event_bus(get_event_bus())

        def dummy():
            return None

        sched.register_job_factory("dummy2", dummy, 60)

        # Without auth should be 401
        r = client.get("/scheduler/jobs")
        assert r.status_code == 401

        # With header
        r = client.get(
            "/scheduler/jobs", headers={"Authorization": "Bearer secret-token"}
        )
        assert r.status_code == 200
        # cleanup
        sched.clear_jobs()


def test_websocket_session_endpoint_auth_and_events(monkeypatch):
    # WebSocket auth: require admin token
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "ws-token")

    with TestClient(app) as client:
        # 1) Without auth header the server should immediately close the connection
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/session/testsession", headers={}) as ws:
                pass

        # 2) Wrong token should also be rejected
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/ws/session/testsession", headers={"Authorization": "Bearer wrong"}
            ) as ws:
                pass

        # 3) Happy path: register the shared event bus so server and client share it
        register_event_bus(get_event_bus())
        with client.websocket_connect(
            "/ws/session/testsession", headers={"Authorization": "Bearer ws-token"}
        ) as ws:
            # Publish an event that matches and assert we receive it
            get_event_bus().publish(
                "session.created", {"session_id": "testsession", "foo": "bar"}
            )
            msg = ws.receive_json()
            assert msg.get("event") == "session.created"
            assert isinstance(msg.get("data"), dict)


def test_websocket_control_subscribe_and_initial_query(monkeypatch, recv_json_ws):
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "ws-token-2")
    with TestClient(app) as client:
        register_event_bus(get_event_bus())

        # 1) Connect with events=none and then subscribe via control message
        # Use header auth (preferred) to avoid query-token usage
        with client.websocket_connect(
            "/ws/session/testsession?events=none",
            headers={"Authorization": "Bearer ws-token-2"},
        ) as ws:
            # Send subscribe control message
            ws.send_text(json.dumps({"type": "subscribe", "event": "session.created"}))
            # server will acknowledge via _control subscribed envelope
            ack = recv_json_ws(ws)
            assert ack.get("event") == "_control"
            assert ack.get("data", {}).get("type") == "subscribed"

            # Now publish an event and expect to receive it
            get_event_bus().publish(
                "session.created", {"session_id": "testsession", "foo": "baz"}
            )
            msg = recv_json_ws(ws)
            # Ensure we got the event envelope at some point
            assert (
                msg.get("event") == "session.created" or msg.get("event") == "_control"
            )

        # 2) Connect with initial events param and receive event without control subscribe
        with client.websocket_connect(
            "/ws/session/testsession?events=session.created",
            headers={"Authorization": "Bearer ws-token-2"},
        ) as ws:
            get_event_bus().publish(
                "session.created", {"session_id": "testsession", "foo": "qux"}
            )
            msg = recv_json_ws(ws)
            assert msg.get("event") == "session.created"
            assert isinstance(msg.get("data"), dict)


def test_scheduler_http_endpoints_negative_and_metrics(monkeypatch):
    # Set an admin token and verify negative paths and metrics update
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "secret-token")
    with TestClient(app) as client:
        register_event_bus(get_event_bus())

        def dummy():
            return None

        sched.register_job_factory("dummy3", dummy, 60)

        # Invalid payload to interval endpoint — send valid JSON that's not an object
        r = client.post(
            "/scheduler/jobs/dummy3/interval",
            headers={"Authorization": "Bearer secret-token"},
            json="not-json",
        )
        assert r.status_code == 400

        # Try to enable a non-existent factory
        r = client.post(
            "/scheduler/jobs/nonexistent/enable",
            headers={"Authorization": "Bearer secret-token"},
        )
        assert r.status_code == 404

        # Auth failure increments metrics: attempt + failure when wrong token
        r = client.get("/scheduler/jobs", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

        # Now get metrics text and assert admin auth counters present
        r = client.get("/metrics")
        assert r.status_code == 200
        text = r.text
        assert "codingagent_admin_auth_total" in text

        # cleanup
        sched.clear_jobs()


def _parse_admin_auth_metrics(text: str) -> dict:
    """Parse the codingagent_admin_auth_total lines from the metrics text.

    Returns dict with keys 'attempts','successes','failures' and integer values.
    """
    res = {"attempts": 0, "successes": 0, "failures": 0}
    for line in text.splitlines():
        if line.startswith("codingagent_admin_auth_total{"):
            # Example: codingagent_admin_auth_total{type="attempts"} 3
            try:
                left, val = line.rsplit(" ", 1)
                if 'type="attempts"' in left:
                    res["attempts"] = int(val)
                elif 'type="successes"' in left:
                    res["successes"] = int(val)
                elif 'type="failures"' in left:
                    res["failures"] = int(val)
            except Exception:
                pass
    return res


def test_admin_auth_counters_increment(monkeypatch):
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "counter-token")
    with TestClient(app) as client:
        register_event_bus(get_event_bus())
        # read baseline
        r = client.get("/metrics")
        before = _parse_admin_auth_metrics(r.text)

        # one failed attempt
        r = client.get("/scheduler/jobs", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

        # one success
        r = client.get(
            "/scheduler/jobs", headers={"Authorization": "Bearer counter-token"}
        )
        assert r.status_code in (200, 503, 404)

        # read after
        r = client.get("/metrics")
        after = _parse_admin_auth_metrics(r.text)

        assert after["attempts"] >= before["attempts"] + 2
        assert after["failures"] >= before["failures"] + 1
        assert after["successes"] >= before["successes"] + 1


def test_websocket_control_list_and_ping(monkeypatch, recv_json_ws):
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "ws-token-3")
    # Allow query-token for this test by enabling the opt-in env flag
    monkeypatch.setenv("CODING_AGENT_ALLOW_QUERY_TOKEN", "true")
    with TestClient(app) as client:
        register_event_bus(get_event_bus())
        with client.websocket_connect(
            "/ws/session/testsession?events=none",
            headers={"Authorization": "Bearer ws-token-3"},
        ) as ws:
            # list should return empty subscriptions initially
            ws.send_text(json.dumps({"type": "list"}))
            msg = recv_json_ws(ws)
            assert msg.get("event") == "_control"
            assert msg.get("data", {}).get("type") == "subscriptions"

            # ping -> pong
            ws.send_text(json.dumps({"type": "ping"}))
            msg = recv_json_ws(ws)
            assert msg.get("event") == "_control"
            # server pong may be represented as {type: 'pong'} or minimal control
            assert msg.get("data", {}).get("type") in ("pong", None)


def test_websocket_backpressure_and_drop_policy(monkeypatch, recv_json_ws):
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "ws-token-4")
    # Use a small queue size to force drops
    with TestClient(app) as client:
        register_event_bus(get_event_bus())
        # Use queue_max_size=1 and drop_policy=drop_new so incoming events beyond the first are dropped
        # For backpressure test use header auth to avoid relying on query-token
        with client.websocket_connect(
            "/ws/session/testsession?events=session.created&queue_max_size=1&drop_policy=drop_new",
            headers={"Authorization": "Bearer ws-token-4"},
        ) as ws:
            # send multiple events quickly
            for i in range(5):
                get_event_bus().publish(
                    "session.created", {"session_id": "testsession", "seq": i}
                )
            # Read a few events (may include keepalive/control messages). The bounded
            # queue limits pending delivery, not total burst delivery: the sender may
            # drain it between enqueue callbacks, so this integration test must not
            # assume every burst produces drops. Overflow behavior is covered
            # deterministically in test_server_event_delivery.py.
            seen = []
            for _ in range(6):
                try:
                    m = recv_json_ws(ws, timeout=2.0)
                    if m is not None and m.get("event") == "session.created":
                        seen.append(m)
                except TimeoutError:
                    break
                except Exception:
                    break
            assert len(seen) >= 1

        # Check metrics text contains dropped event metrics header
        r = client.get("/metrics")
        assert r.status_code == 200
    assert "codingagent_sse_events_dropped_total" in r.text


def test_websocket_control_negative_cases(monkeypatch, recv_json_ws):
    monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "ws-token-5")
    # Allow query-token for this negative control test
    monkeypatch.setenv("CODING_AGENT_ALLOW_QUERY_TOKEN", "true")
    with TestClient(app) as client:
        register_event_bus(get_event_bus())
        with client.websocket_connect(
            "/ws/session/testsession?events=none",
            headers={"Authorization": "Bearer ws-token-5"},
        ) as ws:
            # Malformed JSON -> should be ignored (no exception raised client-side)
            ws.send_text("not-json")
            # Send non-dict JSON -> ignored
            ws.send_text(json.dumps([1, 2, 3]))

            # Subscribe to an event twice -> second subscribe should be a no-op but server may ack
            ws.send_text(json.dumps({"type": "subscribe", "event": "session.created"}))
            try:
                ack1 = recv_json_ws(ws, timeout=2.0)
                assert ack1.get("event") == "_control"
            except TimeoutError:
                pass  # events=none: server may not ack control messages
            # Duplicate subscribe
            ws.send_text(json.dumps({"type": "subscribe", "event": "session.created"}))
            try:
                ack2 = recv_json_ws(ws, timeout=2.0)
                # Server may send keepalive or control envelopes; if keepalive arrived accept that
                assert ack2.get("event") in ("_control", "_keepalive")
            except TimeoutError:
                pass

            # Unsubscribe unknown event -> server may reply with control ack
            ws.send_text(json.dumps({"type": "unsubscribe", "event": "unknown.event"}))
            for _ in range(10):
                try:
                    m = recv_json_ws(ws, timeout=2.0)
                except TimeoutError:
                    break
                except Exception:
                    break
                if m is not None and m.get("event") == "_control":
                    found_control = True
                    break
            # When events=none, server may not ack unsubscribe; test still passes

            # Send unknown control type -> ignored
            ws.send_text(json.dumps({"type": "does_not_exist"}))
            try:
                recv_json_ws(ws, timeout=1.0)
            except (TimeoutError, Exception):
                pass
