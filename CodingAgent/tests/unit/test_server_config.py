import base64

from src.server.server_config import (
    extract_admin_token_from_headers,
    is_loopback_bind,
    metrics_basic_auth_valid,
    read_sse_adapter_settings,
    validate_server_exposure,
)
import pytest


def test_read_sse_adapter_settings_parses_env_and_falls_back_on_bad_values():
    queue_max, keepalive, drop_policy = read_sse_adapter_settings(
        {
            "CODING_AGENT_SSE_QUEUE_MAX": "bad",
            "CODING_AGENT_SSE_KEEPALIVE": "also-bad",
            "CODING_AGENT_SSE_DROP_POLICY": "DROP_NEW",
        }
    )
    assert queue_max == 100
    assert keepalive == 15
    assert drop_policy == "drop_new"


def test_extract_admin_token_from_headers_supports_bearer_and_custom_header():
    assert (
        extract_admin_token_from_headers({"Authorization": "Bearer secret"})
        == "secret"
    )
    assert (
        extract_admin_token_from_headers({"X-CodingAgent-Token": "secret2"})
        == "secret2"
    )
    assert extract_admin_token_from_headers({}) is None


def test_metrics_basic_auth_valid_accepts_valid_header_and_rejects_bad_inputs():
    good = base64.b64encode(b"user:pass").decode("ascii")
    bad = base64.b64encode(b"bad:creds").decode("ascii")

    assert metrics_basic_auth_valid({"Authorization": f"Basic {good}"}, "user:pass")
    assert not metrics_basic_auth_valid(
        {"Authorization": f"Basic {bad}"}, "user:pass"
    )
    assert not metrics_basic_auth_valid({"Authorization": "Bearer secret"}, "user:pass")
    assert not metrics_basic_auth_valid({}, "user:pass")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.2.3.4", "::1", "[::1]"])
def test_loopback_binds_are_recognized(host):
    assert is_loopback_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "agent.example"])
def test_non_loopback_binds_require_admin_token(host):
    with pytest.raises(RuntimeError, match="unauthenticated non-loopback bind"):
        validate_server_exposure(host, None)
    validate_server_exposure(host, "configured")
