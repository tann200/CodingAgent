"""STAB-06: endpoint exposure & authentication policy contract tests.

Asserts that every route on the FastAPI app has a documented exposure/auth
policy (the STAB-06 acceptance criterion) and that the key policy decisions are
exactly as documented in docs/PRODUCTION_DEPLOYMENT.md.
"""

import pytest

from src.server.app import app
from src.server.exposure_policy import (
    AUTH_ADMIN_TOKEN,
    AUTH_METRICS_BASIC,
    AUTH_NONE,
    ENDPOINT_POLICIES,
    unregistered_routes,
)


def _policy(method: str, path: str):
    for p in ENDPOINT_POLICIES:
        if p.method == method and p.path == path:
            return p
    raise AssertionError(f"no documented policy for {method} {path}")


def test_health_is_always_public():
    p = _policy("GET", "/health")
    assert p.auth_kind == AUTH_NONE
    assert p.public_when_unset is True


def test_metrics_uses_optional_basic_auth_never_admin_token():
    p = _policy("GET", "/metrics")
    assert p.auth_kind == AUTH_METRICS_BASIC
    assert p.public_when_unset is True


def test_session_and_schedulers_are_admin_token_scoped():
    for method, path in [
        ("POST", "/session"),
        ("GET", "/scheduler/jobs"),
        ("POST", "/scheduler/jobs/clear"),
        ("GET", "/task/{task_id}"),
        ("GET", "/tasks"),
    ]:
        assert _policy(method, path).auth_kind == AUTH_ADMIN_TOKEN


def test_sse_and_websocket_require_admin_token_header():
    assert _policy("GET", "/session/{session_id}/events").auth_kind == AUTH_ADMIN_TOKEN
    assert _policy("WS", "/ws/session/{session_id}").auth_kind == AUTH_ADMIN_TOKEN


def test_every_endpoint_has_a_documented_policy():
    """Acceptance: every app route maps to a documented exposure/auth policy."""
    undocumented = unregistered_routes(app.routes)
    assert undocumented == [], (
        "Endpoints present on the FastAPI app are missing from the STAB-06 "
        "exposure policy registry (src/server/exposure_policy.py):\n"
        + "\n".join(undocumented)
    )


def test_policy_registry_has_no_duplicate_method_path_pairs():
    seen = [f"{p.method} {p.path}" for p in ENDPOINT_POLICIES]
    assert len(seen) == len(set(seen))
