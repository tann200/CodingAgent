"""Pure helpers for server auth and env-driven SSE configuration."""

from __future__ import annotations

import base64
import os
from typing import Mapping, Optional, Tuple


def read_sse_adapter_settings(environ: Optional[Mapping[str, str]] = None) -> Tuple[int, int, str]:
    """Read queue size, keepalive, and drop policy from the environment."""
    env = environ or os.environ
    try:
        queue_max_size = int(env.get("CODING_AGENT_SSE_QUEUE_MAX", "100"))
    except Exception:
        queue_max_size = 100
    try:
        keepalive_interval = int(env.get("CODING_AGENT_SSE_KEEPALIVE", "15"))
    except Exception:
        keepalive_interval = 15
    drop_policy = env.get("CODING_AGENT_SSE_DROP_POLICY", "drop_oldest").lower()
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
    return decoded == expected_credentials
