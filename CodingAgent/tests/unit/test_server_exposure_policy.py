"""Focused tests for server exposure-policy tranche (Task #6).

Covers:
- Endpoint auth matrix (health public vs details admin, metrics fallback)
- Startup bind guard (validate_server_exposure)
- Metrics/health behavior
- Queue and keepalive bounds (clamp helpers + websocket_handler)
- Constant-time metrics Basic-auth comparison
"""

from __future__ import annotations

import base64
import hmac

import pytest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.server.server_config import (
    clamp_keepalive,
    clamp_queue_size,
    is_loopback_bind,
    metrics_basic_auth_valid,
    read_sse_adapter_settings,
    validate_server_exposure,
    _QUEUE_MIN,
    _QUEUE_MAX,
    _KEEPALIVE_MIN,
    _KEEPALIVE_MAX,
)


# ---------------------------------------------------------------------------
# Startup bind guard
# ---------------------------------------------------------------------------

class TestValidateServerExposure:
    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]", "127.2.3.4"])
    def test_loopback_without_token_is_allowed(self, host):
        # Should not raise
        validate_server_exposure(host, None)
        validate_server_exposure(host, "")

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "10.0.0.1", "agent.example.com"])
    def test_non_loopback_without_token_raises(self, host):
        with pytest.raises(RuntimeError, match="unauthenticated non-loopback bind"):
            validate_server_exposure(host, None)

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.1"])
    def test_non_loopback_with_token_is_allowed(self, host):
        validate_server_exposure(host, "mytoken")


# ---------------------------------------------------------------------------
# Queue and keepalive clamping
# ---------------------------------------------------------------------------

class TestClampHelpers:
    def test_clamp_queue_size_within_bounds(self):
        assert clamp_queue_size(100) == 100
        assert clamp_queue_size(1) == 1
        assert clamp_queue_size(10_000) == 10_000

    def test_clamp_queue_size_below_min(self):
        assert clamp_queue_size(0) == _QUEUE_MIN
        assert clamp_queue_size(-500) == _QUEUE_MIN

    def test_clamp_queue_size_above_max(self):
        assert clamp_queue_size(999_999) == _QUEUE_MAX
        assert clamp_queue_size(_QUEUE_MAX + 1) == _QUEUE_MAX

    def test_clamp_keepalive_within_bounds(self):
        assert clamp_keepalive(15) == 15
        assert clamp_keepalive(1) == 1
        assert clamp_keepalive(300) == 300

    def test_clamp_keepalive_below_min(self):
        assert clamp_keepalive(0) == _KEEPALIVE_MIN
        assert clamp_keepalive(-10) == _KEEPALIVE_MIN

    def test_clamp_keepalive_above_max(self):
        assert clamp_keepalive(999) == _KEEPALIVE_MAX
        assert clamp_keepalive(_KEEPALIVE_MAX + 1) == _KEEPALIVE_MAX

    def test_read_sse_adapter_settings_clamps_extreme_values(self):
        """Env values outside bounds are silently clamped."""
        qms, ka, dp = read_sse_adapter_settings({
            "CODINGAGENT_SSE_QUEUE_MAX": "99999999",
            "CODINGAGENT_SSE_KEEPALIVE": "99999",
            "CODINGAGENT_SSE_DROP_POLICY": "drop_new",
        })
        assert qms == _QUEUE_MAX
        assert ka == _KEEPALIVE_MAX
        assert dp == "drop_new"

    def test_read_sse_adapter_settings_clamps_zero_queue(self):
        qms, ka, _ = read_sse_adapter_settings({
            "CODINGAGENT_SSE_QUEUE_MAX": "0",
            "CODINGAGENT_SSE_KEEPALIVE": "0",
        })
        assert qms == _QUEUE_MIN
        assert ka == _KEEPALIVE_MIN


# ---------------------------------------------------------------------------
# Metrics Basic-auth constant-time comparison
# ---------------------------------------------------------------------------

class TestMetricsBasicAuthConstantTime:
    def _make_header(self, creds: str) -> dict:
        b64 = base64.b64encode(creds.encode()).decode("ascii")
        return {"Authorization": f"Basic {b64}"}

    def test_valid_creds_accepted(self):
        assert metrics_basic_auth_valid(self._make_header("user:pass"), "user:pass")

    def test_invalid_creds_rejected(self):
        assert not metrics_basic_auth_valid(self._make_header("bad:creds"), "user:pass")

    def test_empty_header_rejected(self):
        assert not metrics_basic_auth_valid({}, "user:pass")

    def test_bearer_token_rejected(self):
        assert not metrics_basic_auth_valid({"Authorization": "Bearer mytoken"}, "user:pass")

    def test_uses_hmac_compare_digest(self):
        """Verify the implementation calls hmac.compare_digest (constant-time)."""
        # We spy on hmac.compare_digest to confirm it's invoked.
        with patch("src.server.server_config.hmac.compare_digest", wraps=hmac.compare_digest) as spy:
            metrics_basic_auth_valid(self._make_header("user:pass"), "user:pass")
            assert spy.called


# ---------------------------------------------------------------------------
# HTTP endpoint auth matrix
# ---------------------------------------------------------------------------

@pytest.fixture
def test_client():
    from src.server.app import app
    return TestClient(app)


class TestHealthEndpointPolicy:
    def test_health_is_public_returns_minimal_status(self, test_client):
        """/health must be reachable without auth and return only {status}."""
        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "healthy"}, f"Unexpected keys: {body.keys()}"

    def test_health_no_capabilities_key(self, test_client):
        """/health must NOT expose capability data."""
        resp = test_client.get("/health")
        assert "capabilities" not in resp.json()

    def test_health_still_public_when_token_configured(self, monkeypatch, test_client):
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "secret")
        resp = test_client.get("/health")
        assert resp.status_code == 200

    def test_health_details_requires_admin_when_token_configured(self, monkeypatch, test_client):
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "secret")
        resp = test_client.get("/health/details")
        assert resp.status_code == 401

    def test_health_details_allowed_with_valid_token(self, monkeypatch, test_client):
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "secret")
        resp = test_client.get(
            "/health/details",
            headers={"Authorization": "Bearer secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "capabilities" in body

    def test_health_details_public_when_no_token_configured(self, monkeypatch, test_client):
        monkeypatch.delenv("CODING_AGENT_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("CODINGAGENT_ADMIN_TOKEN", raising=False)
        resp = test_client.get("/health/details")
        assert resp.status_code == 200
        assert "capabilities" in resp.json()


class TestMetricsEndpointPolicy:
    """Metrics must never be public on a token-protected deployment."""

    def test_metrics_open_when_neither_auth_configured(self, monkeypatch, test_client):
        monkeypatch.delenv("CODING_AGENT_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("CODINGAGENT_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("CODING_AGENT_METRICS_AUTH", raising=False)
        monkeypatch.delenv("CODINGAGENT_METRICS_AUTH", raising=False)
        resp = test_client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_uses_basic_auth_when_configured(self, monkeypatch, test_client):
        monkeypatch.delenv("CODING_AGENT_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("CODINGAGENT_ADMIN_TOKEN", raising=False)
        monkeypatch.setenv("CODING_AGENT_METRICS_AUTH", "user:pass")

        # No auth → 401
        assert test_client.get("/metrics").status_code == 401

        # Wrong creds → 401
        bad = base64.b64encode(b"bad:creds").decode()
        assert test_client.get("/metrics", headers={"Authorization": f"Basic {bad}"}).status_code == 401

        # Correct creds → 200
        good = base64.b64encode(b"user:pass").decode()
        assert test_client.get("/metrics", headers={"Authorization": f"Basic {good}"}).status_code == 200

    def test_metrics_falls_back_to_admin_auth_when_no_basic_auth(self, monkeypatch, test_client):
        """No METRICS_AUTH + ADMIN_TOKEN set → must require admin bearer."""
        monkeypatch.delenv("CODING_AGENT_METRICS_AUTH", raising=False)
        monkeypatch.delenv("CODINGAGENT_METRICS_AUTH", raising=False)
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "mytoken")

        # No auth → 401
        assert test_client.get("/metrics").status_code == 401

        # Wrong token → 401
        assert test_client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401

        # Correct token → 200
        assert test_client.get("/metrics", headers={"Authorization": "Bearer mytoken"}).status_code == 200

    def test_basic_auth_takes_priority_over_admin_auth(self, monkeypatch, test_client):
        """When both METRICS_AUTH and ADMIN_TOKEN are set, Basic auth is used."""
        monkeypatch.setenv("CODING_AGENT_METRICS_AUTH", "muser:mpass")
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "admintoken")

        # Admin bearer without Basic auth → 401 (Basic auth takes precedence)
        assert test_client.get("/metrics", headers={"Authorization": "Bearer admintoken"}).status_code == 401

        # Correct Basic auth → 200
        good = base64.b64encode(b"muser:mpass").decode()
        assert test_client.get("/metrics", headers={"Authorization": f"Basic {good}"}).status_code == 200


class TestSSEEndpointAuthMatrix:
    def test_sse_requires_token_when_configured(self, monkeypatch, test_client):
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "ssetoken")
        resp = test_client.get("/session/s1/events")
        assert resp.status_code == 401

    @patch("src.server.app.sse_adapter")
    def test_sse_allows_valid_bearer_token(self, mock_adapter, monkeypatch, test_client):
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "ssetoken")

        async def _gen():
            yield 'data: {"event":"test","data":{}}\n\n'

        mock_adapter.event_generator.return_value = _gen()
        resp = test_client.get(
            "/session/s1/events",
            headers={"Authorization": "Bearer ssetoken"},
        )
        assert resp.status_code == 200

    def test_sse_open_when_no_token_configured(self, monkeypatch):
        monkeypatch.delenv("CODING_AGENT_ADMIN_TOKEN", raising=False)
        monkeypatch.delenv("CODINGAGENT_ADMIN_TOKEN", raising=False)

        from src.server.app import app

        async def _gen():
            yield 'data: {"event":"test","data":{}}\n\n'

        with patch("src.server.app.sse_adapter") as mock_adapter:
            mock_adapter.event_generator.return_value = _gen()
            client = TestClient(app)
            resp = client.get("/session/s1/events")
            assert resp.status_code == 200


class TestSessionEndpointAuthMatrix:
    def test_post_session_requires_token_when_configured(self, monkeypatch, test_client):
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "tok")
        assert test_client.post("/session", json={}).status_code == 401

    def test_post_session_allows_valid_token(self, monkeypatch, test_client):
        monkeypatch.setenv("CODING_AGENT_ADMIN_TOKEN", "tok")
        resp = test_client.post(
            "/session", json={}, headers={"Authorization": "Bearer tok"}
        )
        assert resp.status_code == 200
