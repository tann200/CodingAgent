"""Single source of truth for HTTP/SSE/WebSocket endpoint exposure + auth.

STAB-06: every endpoint the FastAPI app exposes must have a documented exposure
and authentication policy. The production deployment guide
(docs/PRODUCTION_DEPLOYMENT.md) and the contract tests
(tests/unit/test_exposure_policy.py) both derive from this registry, so adding
an endpoint without recording its policy here fails the coverage contract test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Tuple

# Authentication kinds -----------------------------------------------------
AUTH_ADMIN_TOKEN = "admin_token"  # CODINGAGENT_ADMIN_TOKEN; Bearer or X-CodingAgent-Token header
AUTH_METRICS_BASIC = "metrics_basic"  # CODINGAGENT_METRICS_AUTH; HTTP Basic; public if unset
AUTH_NONE = "none"  # always public

# Governing environment variables (canonical name shown; legacy alias supported)
ADMIN_TOKEN_ENV = "CODINGAGENT_ADMIN_TOKEN"
METRICS_AUTH_ENV = "CODINGAGENT_METRICS_AUTH"

# FastAPI-generated framework routes that are dev helpers, not app endpoints.
# They must not be reachable on a production deployment (disable via the app's
# docs_url/redoc_url/openapi_url / a reverse proxy ACL). Filtered by prefix
# because the Swagger UI registers helper routes under /docs/ (e.g. the
# /docs/oauth2-redirect endpoint).
FRAMEWORK_ROUTE_PREFIXES = ("/openapi.json", "/docs", "/redoc")


@dataclass(frozen=True)
class EndpointPolicy:
    method: str
    path: str
    auth_kind: str
    public_when_unset: bool
    note: str


# Policy for every endpoint. `method` is the HTTP verb, or "WS" for the
# WebSocket handshake. `public_when_unset` is True when the endpoint is open if
# its governing env var is not configured (the local, loopback-only default).
ENDPOINT_POLICIES: Tuple[EndpointPolicy, ...] = (
    EndpointPolicy(
        "GET",
        "/health",
        AUTH_NONE,
        True,
        "Liveness/readiness plus optional-feature capability flags; always public for "
        "load-balancer and orchestrator probes.",
    ),
    EndpointPolicy(
        "GET",
        "/metrics",
        AUTH_METRICS_BASIC,
        True,
        "Prometheus text-format metrics; public unless CODINGAGENT_METRICS_AUTH is set, "
        "then requires HTTP Basic credentials.",
    ),
    EndpointPolicy(
        "POST",
        "/session",
        AUTH_ADMIN_TOKEN,
        True,
        "Create a session; open when the admin token is unset.",
    ),
    EndpointPolicy(
        "GET",
        "/session/{session_id}/events",
        AUTH_ADMIN_TOKEN,
        True,
        "SSE event stream; header token only (Bearer or X-CodingAgent-Token). Query-string "
        "tokens are never accepted.",
    ),
    EndpointPolicy(
        "WS",
        "/ws/session/{session_id}",
        AUTH_ADMIN_TOKEN,
        True,
        "WebSocket event stream; header token only (Bearer or X-CodingAgent-Token) verified "
        "at handshake. Query-string tokens are never accepted.",
    ),
    EndpointPolicy(
        "GET", "/scheduler/jobs", AUTH_ADMIN_TOKEN, True, "List registered scheduler jobs."
    ),
    EndpointPolicy(
        "POST",
        "/scheduler/jobs/{name}/unregister",
        AUTH_ADMIN_TOKEN,
        True,
        "Unregister a scheduler job.",
    ),
    EndpointPolicy(
        "POST", "/scheduler/jobs/clear", AUTH_ADMIN_TOKEN, True, "Clear all scheduler jobs."
    ),
    EndpointPolicy(
        "POST",
        "/scheduler/jobs/{name}/enable",
        AUTH_ADMIN_TOKEN,
        True,
        "Enable a scheduler job registered via a factory.",
    ),
    EndpointPolicy(
        "POST",
        "/scheduler/jobs/{name}/interval",
        AUTH_ADMIN_TOKEN,
        True,
        "Update a scheduler job interval.",
    ),
    EndpointPolicy(
        "POST", "/task", AUTH_ADMIN_TOKEN, True, "Submit an asynchronous task."
    ),
    EndpointPolicy(
        "GET", "/task/{task_id}", AUTH_ADMIN_TOKEN, True, "Query an asynchronous task's status."
    ),
    EndpointPolicy(
        "POST",
        "/task/{task_id}/cancel",
        AUTH_ADMIN_TOKEN,
        True,
        "Request cancellation of an asynchronous task.",
    ),
    EndpointPolicy(
        "GET", "/tasks", AUTH_ADMIN_TOKEN, True, "List recent asynchronous tasks."
    ),
)


def unregistered_routes(app_routes: Iterable[object]) -> List[str]:
    """Return (METHOD path) signatures present on the app but undocumented.

    Iterates over FastAPI/Starlette route objects and reports any route that is
    not covered by ENDPOINT_POLICIES. Framework-generated dev routes
    (OpenAPI/docs/redoc) are excluded here and instead must be disabled in
    production. Used by the STAB-06 contract test so every endpoint has a
    documented policy.
    """
    documented = {(p.method, p.path) for p in ENDPOINT_POLICIES}
    missing: List[str] = []
    for route in app_routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        if path.startswith(FRAMEWORK_ROUTE_PREFIXES):
            continue
        methods = getattr(route, "methods", None)
        if methods is None:
            methods = {"WEBSOCKET"}
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            norm = "WS" if method == "WEBSOCKET" else method
            if (norm, path) not in documented:
                missing.append(f"{norm} {path}")
    return sorted(missing)
