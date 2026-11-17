"""Pure helpers for server auth and env-driven SSE configuration."""

from __future__ import annotations

import base64
import hmac
import ipaddress
import os
from urllib.parse import urlparse
from typing import Mapping, Optional, Tuple



_LOOPBACK_HOSTNAMES = {"localhost", "ip6-localhost"}


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
    """Reject unauthenticated binds that expose agent tooling off-machine."""
    if not is_loopback_bind(host) and not admin_token:
        raise RuntimeError(
            "Refusing unauthenticated non-loopback bind. Set "
            "CODINGAGENT_ADMIN_TOKEN or bind the server to 127.0.0.1/::1."
        )


def read_sse_adapter_settings(environ: Optional[Mapping[str, str]] = None) -> Tuple[int, int, str]:
    """Read queue size, keepalive, and drop policy from the environment."""
    env = environ or os.environ
    # Support both CODINGAGENT_ (new) and CODING_AGENT_ (legacy) prefixes.
    # When a plain Mapping is passed we must check both keys manually.
    def _get(new_key: str, old_key: str, default: str) -> str:
        return env.get(new_key) or env.get(old_key) or default  # type: ignore[return-value]

    try:
        queue_max_size = int(_get("CODINGAGENT_SSE_QUEUE_MAX", "CODING_AGENT_SSE_QUEUE_MAX", "100"))
    except Exception:
        queue_max_size = 100
    try:
        keepalive_interval = int(_get("CODINGAGENT_SSE_KEEPALIVE", "CODING_AGENT_SSE_KEEPALIVE", "15"))
    except Exception:
        keepalive_interval = 15
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
    """Return True when the request contains valid Basic auth credentials."""
    header = headers.get("Authorization") or headers.get("authorization")
    if not header or not header.startswith("Basic "):
        return False
    b64 = header.split(" ", 1)[1]
    try:
        decoded = base64.b64decode(b64).decode("utf-8")
    except Exception:
        return False
    return hmac.compare_digest(decoded, expected_credentials)


def validate_websocket_origin(
    headers: Mapping[str, str],
    allowed_origins_env: Optional[str] = None,
) -> bool:
    """Validate WebSocket Origin header against loopback or configured allowlist."""
    origin = headers.get("origin") or headers.get("Origin")
    if not origin:
        # Non-browser CLI/SDK WebSocket clients omit Origin.
        return True
    try:
        parsed = urlparse(origin.strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    if is_loopback_bind(parsed.hostname):
        return True
    raw_allowed = allowed_origins_env if allowed_origins_env is not None else (
        os.environ.get("CODINGAGENT_ALLOWED_ORIGINS")
        or os.environ.get("CODING_AGENT_ALLOWED_ORIGINS")
        or ""
    )
    allowed = {o.strip().rstrip("/") for o in raw_allowed.split(",") if o.strip()}
    return origin.strip().rstrip("/") in allowed
