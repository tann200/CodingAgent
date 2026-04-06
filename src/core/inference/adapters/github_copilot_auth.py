"""GitHub Copilot OAuth Device Flow authentication.

End-to-end flow
---------------
1. POST github.com/login/device/code  (client_id = Ov23li8tweQw6odWQebz, scope = read:user)
2. Display verification_uri + user_code to the user
3. Poll github.com/login/oauth/access_token until the user approves in the browser
4. Store the resulting access_token in UserPrefs
5. Use the token directly as  Authorization: Bearer <token>  on every Copilot API call

The token is the GitHub OAuth access token from the device flow.  It is long-lived
(no client-side expiry; valid until the user revokes it in GitHub settings).
No intermediate Copilot-specific token exchange is required.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

_logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

GITHUB_CLIENT_ID = "Ov23li8tweQw6odWQebz"   # GitHub OAuth app for Copilot — do not change
GITHUB_SCOPE = "read:user"
_DEVICE_CODE_URL = "https://github.com/login/device/code"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_AGENT = "CodingAgent/1.0"
_POLL_SAFETY_MARGIN = 3   # extra seconds added to each polling interval
_PREFS_PROVIDER_KEY = "github_copilot"
_PREFS_TOKEN_KEY = "github_token"


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class DeviceCodeResponse:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int           # seconds between poll attempts
    expires_in: int = 900   # seconds until device_code expires


# ── Device flow ───────────────────────────────────────────────────────────────

def start_device_flow() -> DeviceCodeResponse:
    """POST /login/device/code and return device + user codes.

    Raises:
        requests.HTTPError   — non-2xx from GitHub
        ValueError           — response missing required fields
    """
    resp = requests.post(
        _DEVICE_CODE_URL,
        json={"client_id": GITHUB_CLIENT_ID, "scope": GITHUB_SCOPE},
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    for field in ("device_code", "user_code", "verification_uri", "interval"):
        if field not in data:
            raise ValueError(f"GitHub device flow: missing field '{field}' in response")
    return DeviceCodeResponse(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        interval=int(data["interval"]),
        expires_in=int(data.get("expires_in", 900)),
    )


def poll_for_token(
    device_code: str,
    interval: int,
    timeout: float = 300.0,
    cancel_event: Optional["threading.Event"] = None,
) -> str:
    """Block-poll POST /login/oauth/access_token until the user approves.

    Implements RFC 8628 error handling:
      authorization_pending  → wait interval + safety margin, retry
      slow_down              → increment interval by 5s, retry
      expired_token          → raise DeviceCodeExpired
      access_denied          → raise AuthCancelled

    *cancel_event* — optional threading.Event; if set(), the loop raises
    AuthCancelled immediately (used by the TUI Cancel button).

    Returns the GitHub OAuth access_token on success.

    Raises:
        DeviceCodeExpired  — device code timed out before user authorized
        AuthCancelled      — user cancelled or explicitly denied
        TimeoutError       — poll loop exceeded *timeout* seconds
    """
    if cancel_event is None:
        cancel_event = threading.Event()

    deadline = time.monotonic() + timeout
    current_interval = interval

    while time.monotonic() < deadline:
        # Sleep in small increments so cancel_event is checked promptly
        sleep_total = current_interval + _POLL_SAFETY_MARGIN
        slept = 0.0
        while slept < sleep_total:
            if cancel_event.is_set():
                raise AuthCancelled("Login cancelled by user.")
            chunk = min(0.5, sleep_total - slept)
            time.sleep(chunk)
            slept += chunk

        if cancel_event.is_set():
            raise AuthCancelled("Login cancelled by user.")

        try:
            resp = requests.post(
                _TOKEN_URL,
                json={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            _logger.warning("github_copilot_auth: poll request failed: %s", e)
            continue

        data = resp.json()

        if "access_token" in data:
            return data["access_token"]

        error = data.get("error", "")
        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            current_interval += 5
            continue
        elif error == "expired_token":
            raise DeviceCodeExpired("GitHub device code expired before user authorized.")
        elif error == "access_denied":
            raise AuthCancelled("GitHub authorization was denied by the user.")
        else:
            _logger.warning("github_copilot_auth: unexpected poll response: %s", data)
            continue

    raise TimeoutError(f"GitHub Copilot login timed out after {timeout:.0f}s.")


# ── Token persistence ─────────────────────────────────────────────────────────

def save_token(token: str) -> None:
    """Write the GitHub OAuth token to UserPrefs under providers.github_copilot.github_token."""
    from src.core.user_prefs import UserPrefs
    prefs = UserPrefs.load()
    prefs.set_provider_setting(_PREFS_PROVIDER_KEY, _PREFS_TOKEN_KEY, token)
    prefs.save()
    _logger.info("github_copilot_auth: token saved to UserPrefs")


def load_token() -> Optional[str]:
    """Return the stored GitHub OAuth token, or None if not authenticated."""
    try:
        from src.core.user_prefs import UserPrefs
        prefs = UserPrefs.load()
        return prefs.get_provider_setting(_PREFS_PROVIDER_KEY, _PREFS_TOKEN_KEY) or None
    except Exception:
        return None


def clear_token() -> None:
    """Remove the stored token from UserPrefs (logout)."""
    try:
        from src.core.user_prefs import UserPrefs
        prefs = UserPrefs.load()
        prefs.set_provider_setting(_PREFS_PROVIDER_KEY, _PREFS_TOKEN_KEY, "")
        prefs.save()
        _logger.info("github_copilot_auth: token cleared")
    except Exception as e:
        _logger.warning("github_copilot_auth: clear_token failed: %s", e)


def is_authenticated() -> bool:
    """Return True if a non-empty GitHub token is stored (no network call)."""
    tok = load_token()
    return bool(tok and tok.strip())


# ── Exceptions ────────────────────────────────────────────────────────────────

class DeviceCodeExpired(Exception):
    """Raised when the GitHub device code expires before the user authorizes."""


class AuthCancelled(Exception):
    """Raised when the user explicitly denies the GitHub authorization request."""
