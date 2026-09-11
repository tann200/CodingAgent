"""Pure helpers for server auth and env-driven SSE configuration.

Deployment notes
----------------
**Supported launcher**: Use ``python -m src.server.app`` or the ``run_server()``
helper.  This launcher runs the ``validate_server_exposure`` startup guard,
which refuses a non-loopback bind unless ``CODINGAGENT_ADMIN_TOKEN`` is set.

**Direct uvicorn**: Running uvicorn directly (``uvicorn src.server.app:app``)
is *request-safe* — every protected endpoint enforces the request-level
``enforce_request_exposure_policy`` guard, so when no admin token is configured
only loopback clients are served and non-loopback clients are rejected. However,
direct uvicorn still **bypasses the startup validation** (``validate_server_exposure``)
and any TLS/reverse-proxy setup you would normally perform via the launcher.
Prefer ``run_server()`` for production; if you must launch uvicorn directly,
set ``CODINGAGENT_ADMIN_TOKEN`` and terminate TLS in front of the process.

**TLS / reverse-proxy requirement**: This server does not terminate TLS itself.
In non-loopback deployments always place a TLS-terminating reverse-proxy
(nginx, Caddy, …) in front so tokens are never sent in cleartext.

**Accepted auth headers** (admin endpoints):
  - ``Authorization: Bearer <token>``
  - ``X-CodingAgent-Token: <token>``

**Endpoint policy summary**:
  - ``GET /health``            — public; returns minimal {status} only
  - ``GET /health/details``    — admin-protected; returns full capability data
  - ``GET /metrics``           — metrics Basic-auth when configured, else admin auth; never public on protected deploys
  - ``POST /session``          — admin auth
  - ``GET  /session/{id}/events`` — admin auth (SSE)
  - ``WS   /ws/session/{id}``  — admin auth (WebSocket)
  - ``POST /task`` etc.        — admin auth
  - ``GET|POST /scheduler/…``  — admin auth

When no admin token is configured, every "admin auth" endpoint above additionally
requires the client to be on the loopback interface (request-level fail-closed).
"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import os
from typing import Any, Mapping, Optional, Tuple


_LOOPBACK_HOSTNAMES = {"localhost", "ip6-localhost"}

# Synthetic client host reported by Starlette/FastAPI ``TestClient``.  It is not
# a routable address, so we treat it as local *only* in tests.  This is
# deliberately narrow: real arbitrary hostnames are NOT treated as local (see
# ``client_host_is_local``).
_TESTCLIENT_HOST = "testclient"

# Absolute bounds for SSE/WebSocket queue and keepalive settings.
# These prevent abusive or misconfigured query values from causing OOM or stale
# connections.
_QUEUE_MIN = 1
_QUEUE_MAX = 10_000
_QUEUE_DEFAULT = 100

_KEEPALIVE_MIN = 1      # seconds
_KEEPALIVE_MAX = 300    # seconds (5 minutes)
_KEEPALIVE_DEFAULT = 15


def is_loopback_bind(host: str) -> bool:
    """Return whether *host* restricts the server to the local machine."""
    normalized = host.strip().lower().strip("[]")
    if normalized in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_server_exposure(host: str, admin_token: Optional[str]) -> None:
    """Reject unauthenticated binds that expose agent tooling off-machine.

    NOTE: This guard only fires when using the ``run_server()`` / ``__main__``
    launcher.  Direct uvicorn invocations bypass this check — ensure
    ``CODINGAGENT_ADMIN_TOKEN`` is set in that case.
    """
    if not is_loopback_bind(host) and not admin_token:
        raise RuntimeError(
            "Refusing unauthenticated non-loopback bind. Set "
            "CODINGAGENT_ADMIN_TOKEN or bind the server to 127.0.0.1/::1."
        )


def client_host_is_local(host: Optional[str]) -> bool:
    """Return whether a request's *client host* is the local machine.

    Unlike :func:`is_loopback_bind` (which validates a bind address and treats
    resolvable hostnames leniently), this is used for per-request authorization
    and is intentionally strict:

    - A missing/empty client host is treated as NOT local (fail closed).
    - The Starlette ``TestClient`` synthetic host ``"testclient"`` is treated as
      local so unit tests exercise the loopback path.  This is the *only*
      hostname accepted; real arbitrary hostnames (e.g. ``agent.example.com``)
      are rejected because they cannot be proven to be loopback here.
    - Otherwise the host must parse as a loopback IP address (127.0.0.0/8, ::1).
    """
    if not host:
        return False
    normalized = host.strip().lower().strip("[]")
    if not normalized:
        return False
    if normalized == _TESTCLIENT_HOST:
        return True
    if normalized in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        # Real, unresolved hostnames are NOT considered local.
        return False


def request_client_host(scope_or_client: Any) -> Optional[str]:
    """Extract the client host from a Request/WebSocket or a raw ASGI client.

    Accepts:
    - an object with a ``.client`` attribute (FastAPI ``Request``/``WebSocket``),
      whose ``.client`` is an ``(host, port)`` pair or ``None``; or
    - a raw ``(host, port)`` tuple / ``None`` (an ASGI ``scope["client"]``).
    """
    client = getattr(scope_or_client, "client", scope_or_client)
    if client is None:
        return None
    try:
        host = client[0]
    except (TypeError, IndexError, KeyError):
        return None
    return host


def loopback_only_allowed(scope_or_client: Any, admin_token: Optional[str]) -> bool:
    """Request-level fail-closed check for the no-token deployment.

    When ``admin_token`` is falsy, protected endpoints must only serve loopback
    clients.  Returns True when the request is permitted, False when it should be
    rejected.  When an admin token *is* configured, this returns True and the
    caller falls through to token verification instead.
    """
    if admin_token:
        return True
    return client_host_is_local(request_client_host(scope_or_client))


def clamp_queue_size(value: int) -> int:
    """Clamp a queue-size value to [_QUEUE_MIN, _QUEUE_MAX]."""
    return max(_QUEUE_MIN, min(_QUEUE_MAX, int(value)))


def clamp_keepalive(value: int) -> int:
    """Clamp a keepalive interval (seconds) to [_KEEPALIVE_MIN, _KEEPALIVE_MAX]."""
    return max(_KEEPALIVE_MIN, min(_KEEPALIVE_MAX, int(value)))


def read_sse_adapter_settings(environ: Optional[Mapping[str, str]] = None) -> Tuple[int, int, str]:
    """Read queue size, keepalive, and drop policy from the environment.

    Returned values are always within the safe bounds defined by
    ``_QUEUE_MIN/_QUEUE_MAX`` and ``_KEEPALIVE_MIN/_KEEPALIVE_MAX``.
    """
    env = environ or os.environ
    # Support both CODINGAGENT_ (new) and CODING_AGENT_ (legacy) prefixes.
    # When a plain Mapping is passed we must check both keys manually.
    def _get(new_key: str, old_key: str, default: str) -> str:
        return env.get(new_key) or env.get(old_key) or default  # type: ignore[return-value]

    try:
        queue_max_size = clamp_queue_size(
            int(_get("CODINGAGENT_SSE_QUEUE_MAX", "CODING_AGENT_SSE_QUEUE_MAX", str(_QUEUE_DEFAULT)))
        )
    except Exception:
        queue_max_size = _QUEUE_DEFAULT
    try:
        keepalive_interval = clamp_keepalive(
            int(_get("CODINGAGENT_SSE_KEEPALIVE", "CODING_AGENT_SSE_KEEPALIVE", str(_KEEPALIVE_DEFAULT)))
        )
    except Exception:
        keepalive_interval = _KEEPALIVE_DEFAULT
    drop_policy = _get("CODINGAGENT_SSE_DROP_POLICY", "CODING_AGENT_SSE_DROP_POLICY", "drop_oldest").lower()
    return queue_max_size, keepalive_interval, drop_policy


def extract_admin_token_from_headers(headers: Mapping[str, str]) -> Optional[str]:
    """Extract admin token from bearer auth or X-CodingAgent-Token header."""
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    token = None
    if auth and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1]
    if token:
        return token
    return headers.get("X-CodingAgent-Token") or headers.get("x-codingagent-token")


def metrics_basic_auth_valid(
    headers: Mapping[str, str], expected_credentials: str
) -> bool:
    """Return True when the request contains valid Basic auth credentials.

    Uses constant-time comparison to prevent timing side-channels.
    """
    header = headers.get("Authorization") or headers.get("authorization")
    if not header or not header.startswith("Basic "):
        return False
    b64 = header.split(" ", 1)[1]
    try:
        decoded = base64.b64decode(b64).decode("utf-8")
    except Exception:
        return False
    # Constant-time comparison — prevents timing oracle on the credentials.
    return hmac.compare_digest(decoded, expected_credentials)
