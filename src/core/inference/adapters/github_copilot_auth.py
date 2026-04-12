"""GitHub Copilot OAuth Device Flow authentication.

End-to-end flow
---------------
1. POST github.com/login/device/code  (client_id = Iv23lisS4MOAlyhbW9Oa, scope = read:user)
2. Display verification_uri + user_code to the user
3. Poll github.com/login/oauth/access_token until the user approves in the browser
4. Store token in  ~/.local/share/codingagent/auth.json  as:
     {"github-copilot": {"type": "oauth", "refresh": "<refresh_token>", "access": "<access_token>",
                          "expires": <unix_epoch_seconds>, "refresh_expires": <unix_epoch_seconds>}}
   File is written with permissions 0o600.
5. Use the access_token directly as  Authorization: Bearer <token>  on every Copilot API call

Token expiry and refresh (GitHub App tokens)
--------------------------------------------
GitHub Apps issue short-lived user access tokens (ghu_ prefix, ~8 h) together with a
long-lived refresh token (ghr_ prefix, ~6 months).  load_token() checks whether the
access token expires within TOKEN_REFRESH_MARGIN seconds and, if so, automatically
exchanges the refresh token for a new access + refresh token pair before returning.
This means the app never sends an expired token to the Copilot API.

Classic OAuth App tokens (gho_ prefix) have no expiry; expires=0 means "never expires"
and the refresh path is skipped.

GitHub Enterprise is also supported: pass enterprise_url to start_device_flow() and
the device-code / polling requests are routed to that domain instead of github.com.
The enterpriseUrl is stored in the auth.json entry and used to build the Copilot
base URL (https://copilot-api.<domain>).

Architecture note
-----------------
``GitHubDeviceFlow`` is the concrete implementation of the generic
``src.core.auth.device_flow.DeviceFlowProvider`` ABC.  All module-level
functions (``start_device_flow``, ``poll_for_token``, ``save_token``,
``load_token``, ``clear_token``, ``is_authenticated``) are kept as thin
backward-compat wrappers that delegate to a shared ``_default_provider``
singleton so existing callers do not need to be updated.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional, Union

import requests

from src.core.auth.device_flow import (
    AuthCancelled,
    DeviceCodeExpired,
    DeviceCodeRequest,
    DeviceCodeResponse,
    DeviceFlowProvider,
    TokenResult,
    interruptible_sleep,
)

_logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

CLIENT_ID = "Iv23lisS4MOAlyhbW9Oa"  # CodingAgent GitHub OAuth App client ID
GITHUB_CLIENT_ID = CLIENT_ID  # backwards-compat alias (used by tests)
GITHUB_SCOPE = "read:user"
_USER_AGENT = "CodingAgent/1.0"
_POLL_SAFETY_MARGIN = (
    3  # extra seconds added to each polling interval (3 s, same as OpenCode)
)
_PROVIDER_KEY = "github-copilot"  # key in auth.json — uses hyphen (OpenCode convention)
TOKEN_REFRESH_MARGIN = (
    300  # refresh token if it expires within this many seconds (5 min)
)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _normalize_domain(url: str) -> str:
    """Strip scheme and trailing slash from a URL/domain string."""
    return url.replace("https://", "").replace("http://", "").rstrip("/")


def _get_urls(domain: str) -> tuple[str, str]:
    """Return (device_code_url, access_token_url) for the given domain."""
    return (
        f"https://{domain}/login/device/code",
        f"https://{domain}/login/oauth/access_token",
    )


# ── Auth.json storage (OpenCode-compatible) ───────────────────────────────────


def _auth_json_path() -> Path:
    """Return the path to auth.json.

    Primary location:
        $XDG_DATA_HOME/codingagent/auth.json
        (default: ~/.local/share/codingagent/auth.json)

    Migration: if the new path does not exist but the old OpenCode-compatible
    path ($XDG_DATA_HOME/opencode/auth.json) does, the old file is copied to
    the new location so existing authenticated users are not forced to re-login.

    Test override: if $CODINGAGENT_PREFS is set (tests monkeypatch this to a
    tmp file), we store auth in a sibling file auth.json in the same directory
    so tests remain isolated without polluting the real store.
    """
    test_prefs = os.environ.get("CODINGAGENT_PREFS")
    if test_prefs:
        return Path(test_prefs).parent / "auth.json"
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    new_path = Path(xdg_data) / "codingagent" / "auth.json"
    if not new_path.exists():
        old_path = Path(xdg_data) / "opencode" / "auth.json"
        if old_path.exists():
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                import shutil

                shutil.copy2(old_path, new_path)
                new_path.chmod(0o600)
                _logger.info("Migrated auth.json from %s to %s", old_path, new_path)
            except Exception as exc:  # pragma: no cover
                _logger.warning("Could not migrate auth.json: %s", exc)
    return new_path


def _read_auth_json() -> dict:
    path = _auth_json_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_auth_json(data: dict) -> None:
    path = _auth_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # CP-10: use tempfile.mkstemp for a unique temp filename so concurrent
    # writes in different threads do not clobber each other's temp file.
    fd, tmp_str = tempfile.mkstemp(dir=str(path.parent), prefix=".auth_", suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.chmod(tmp_str, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ── GitHubDeviceFlow — concrete DeviceFlowProvider ───────────────────────────


class GitHubDeviceFlow(DeviceFlowProvider):
    """Concrete RFC 8628 device-flow implementation for GitHub (and GHE).

    Parameters
    ----------
    enterprise_url:
        Optional GitHub Enterprise base URL or bare domain
        (e.g. ``"company.ghe.com"`` or ``"https://company.ghe.com"``).
        When ``None``, uses ``github.com``.
    client_id:
        OAuth App client ID.  Defaults to :data:`CLIENT_ID`.
    scope:
        OAuth scope string.  Defaults to :data:`GITHUB_SCOPE`.
    poll_timeout:
        Maximum seconds to wait for user approval in :meth:`poll_for_token`.
    """

    def __init__(
        self,
        enterprise_url: Optional[str] = None,
        client_id: str = CLIENT_ID,
        scope: str = GITHUB_SCOPE,
        poll_timeout: float = 900.0,
    ) -> None:
        self._domain = (
            _normalize_domain(enterprise_url) if enterprise_url else "github.com"
        )
        self._enterprise_url: Optional[str] = (
            _normalize_domain(enterprise_url) if enterprise_url else None
        )
        self._client_id = client_id
        self._scope = scope
        self._poll_timeout = poll_timeout

    # ── DeviceFlowProvider interface ──────────────────────────────────────

    def request_device_code(self, req: DeviceCodeRequest) -> DeviceCodeResponse:
        """POST /login/device/code and return device + user codes.

        *req.domain* and *req.client_id* / *req.scope* override the values
        passed to ``__init__`` when they differ, allowing the same instance to
        be used for multiple domains if needed.

        Raises
        ------
        requests.HTTPError
            Non-2xx response from GitHub.
        ValueError
            Response missing required fields.
        """
        domain = req.domain or self._domain
        device_code_url, _ = _get_urls(domain)
        resp = requests.post(
            device_code_url,
            json={
                "client_id": req.client_id or self._client_id,
                "scope": req.scope or self._scope,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for field_name in ("device_code", "user_code", "verification_uri", "interval"):
            if field_name not in data:
                raise ValueError(
                    f"GitHub device flow: missing field '{field_name}' in response"
                )
        return DeviceCodeResponse(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_uri=data["verification_uri"],
            interval=int(data["interval"]),
            expires_in=int(data.get("expires_in", 900)),
            domain=domain,
        )

    def poll_for_token(
        self,
        dcr: DeviceCodeResponse,
        cancel_event: Optional["threading.Event"] = None,
    ) -> TokenResult:
        """Block-poll POST /login/oauth/access_token until the user approves.

        Implements RFC 8628 error handling (identical to OpenCode's copilot.ts):
          authorization_pending  → wait interval + safety margin, retry
          slow_down              → reset to original_interval + 5 s (CP-09), retry
          expired_token          → raise DeviceCodeExpired
          access_denied          → raise AuthCancelled

        *cancel_event* — optional threading.Event; if set(), the loop raises
        AuthCancelled immediately (used by the TUI Cancel button).

        CP-05: Poll first, sleep after (matches OpenCode copilot.ts behaviour).
        """
        if cancel_event is None:
            cancel_event = threading.Event()

        domain = dcr.domain or self._domain
        _, access_token_url = _get_urls(domain)
        deadline = time.monotonic() + self._poll_timeout
        original_interval = dcr.interval
        current_interval = dcr.interval
        client_id = self._client_id

        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise AuthCancelled("Login cancelled by user.")

            # CP-05: issue the poll request BEFORE sleeping so the first attempt
            # fires immediately (matches OpenCode copilot.ts).
            try:
                resp = requests.post(
                    access_token_url,
                    json={
                        "client_id": client_id,
                        "device_code": dcr.device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": _USER_AGENT,
                    },
                    timeout=15,
                )
                resp.raise_for_status()
            except requests.RequestException as e:
                _logger.warning("github_copilot_auth: poll request failed: %s", e)
                interruptible_sleep(
                    current_interval + _POLL_SAFETY_MARGIN, cancel_event
                )
                continue

            data = resp.json()

            if "access_token" in data:
                return TokenResult(
                    access_token=data["access_token"],
                    refresh_token=data.get("refresh_token") or None,
                    expires_in=int(data.get("expires_in") or 0),
                    refresh_token_expires_in=int(
                        data.get("refresh_token_expires_in") or 0
                    ),
                )

            error = data.get("error", "")
            if error == "authorization_pending":
                interruptible_sleep(
                    current_interval + _POLL_SAFETY_MARGIN, cancel_event
                )
                continue
            elif error == "slow_down":
                # RFC 8628 §3.5: use server-provided interval if present, else
                # reset to original_interval + 5 (CP-09 — do not compound).
                server_interval = data.get("interval")
                if (
                    server_interval
                    and isinstance(server_interval, (int, float))
                    and server_interval > 0
                ):
                    current_interval = int(server_interval)
                else:
                    current_interval = original_interval + 5
                interruptible_sleep(
                    current_interval + _POLL_SAFETY_MARGIN, cancel_event
                )
                continue
            elif error in ("expired_token", "device_flow_unauthorized"):
                raise DeviceCodeExpired(
                    "GitHub device code expired before user authorized."
                )
            elif error == "access_denied":
                raise AuthCancelled("GitHub authorization was denied by the user.")
            else:
                _logger.warning(
                    "github_copilot_auth: unexpected poll response: %s", data
                )
                continue

        raise TimeoutError(
            f"GitHub Copilot login timed out after {self._poll_timeout:.0f}s."
        )

    def save_token(  # type: ignore[override]
        self,
        token: "Union[TokenResult, str]",
        enterprise_url: Optional[str] = None,
    ) -> None:
        """Write the GitHub OAuth token to auth.json under key 'github-copilot'.

        Accepts either a TokenResult (from poll_for_token) or a bare string
        (legacy / CLI usage).

        The ``enterprise_url`` parameter overrides the one set at construction
        time, allowing callers to save tokens for different GHE instances.
        """
        ent = enterprise_url or self._enterprise_url
        _save_token_impl(token, enterprise_url=ent)

    def load_token(self) -> Optional[str]:
        """Return the stored GitHub OAuth access token, refreshing if near expiry."""
        return _load_token_impl()

    def clear_token(self) -> bool:
        """Remove the GitHub Copilot entry from auth.json (logout)."""
        return _clear_token_impl()

    def load_enterprise_url(self) -> Optional[str]:
        """Return the stored enterpriseUrl if the token was for a GHE instance."""
        return load_enterprise_url()

    def refresh_access_token(
        self,
        refresh_token: str,
        domain: Optional[str] = None,
    ) -> TokenResult:
        """Exchange a refresh token for a new access + refresh token pair."""
        return _refresh_access_token_impl(refresh_token, domain=domain or self._domain)


# ── Shared implementation functions (used by both the class and the module API) ─


def _save_token_impl(
    token: "Union[TokenResult, str]",
    enterprise_url: Optional[str] = None,
) -> None:
    now = int(time.time())
    if isinstance(token, TokenResult):
        access = token.access_token
        refresh = token.refresh_token or token.access_token
        expires = (now + token.expires_in) if token.expires_in > 0 else 0
        refresh_expires = (
            (now + token.refresh_token_expires_in)
            if token.refresh_token_expires_in > 0
            else 0
        )
    else:
        # Plain string — treat as a non-expiring Classic OAuth token
        access = token
        refresh = token
        expires = 0
        refresh_expires = 0

    data = _read_auth_json()
    entry: dict = {
        "type": "oauth",
        "refresh": refresh,
        "access": access,
        "expires": expires,
        "refresh_expires": refresh_expires,
    }
    if enterprise_url:
        entry["enterpriseUrl"] = _normalize_domain(enterprise_url)
    data[_PROVIDER_KEY] = entry
    _write_auth_json(data)
    _logger.info("github_copilot_auth: token saved to %s", _auth_json_path())


def _refresh_access_token_impl(
    refresh_token: str,
    domain: str = "github.com",
) -> TokenResult:
    """Exchange a refresh token for a new access + refresh token pair."""
    _, access_token_url = _get_urls(domain)
    resp = requests.post(
        access_token_url,
        json={
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(
            f"github_copilot_auth: refresh response missing access_token: {data}"
        )
    return TokenResult(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or None,
        expires_in=int(data.get("expires_in") or 0),
        refresh_token_expires_in=int(data.get("refresh_token_expires_in") or 0),
    )


def _load_token_impl() -> Optional[str]:
    try:
        data = _read_auth_json()
        entry = data.get(_PROVIDER_KEY)
        if not entry or not isinstance(entry, dict):
            return None

        access = entry.get("access") or entry.get("refresh") or None
        if not access:
            return None

        expires = entry.get("expires", 0)
        # expires == 0 → no expiry (Classic OAuth App gho_ token)
        if expires == 0:
            return access

        now = int(time.time())
        if now + TOKEN_REFRESH_MARGIN < expires:
            # Token is still fresh — return it directly
            return access

        # Token is expiring soon — attempt a refresh
        refresh_tok = entry.get("refresh")
        if not refresh_tok or refresh_tok == access:
            # No separate refresh token available (legacy plain-string storage or
            # Classic OAuth App); return what we have and let the 401 handler
            # clear it if truly expired.
            _logger.debug(
                "github_copilot_auth: access token near expiry but no refresh token available"
            )
            return access

        domain = entry.get("enterpriseUrl") or "github.com"
        enterprise_url = entry.get("enterpriseUrl") or None
        _logger.info(
            "github_copilot_auth: access token expires in %ds — refreshing",
            max(0, expires - now),
        )
        try:
            # Use the public wrapper so tests (and callers) can monkeypatch
            # refresh behaviour via github_copilot_auth.refresh_access_token.
            new_result = refresh_access_token(refresh_tok, domain=domain)
            _save_token_impl(new_result, enterprise_url=enterprise_url)
            return new_result.access_token
        except Exception as exc:
            _logger.warning(
                "github_copilot_auth: token refresh failed (%s) — using existing token",
                exc,
            )
            return access
    except Exception:
        return None


def _clear_token_impl() -> bool:
    try:
        data = _read_auth_json()
        data.pop(_PROVIDER_KEY, None)
        _write_auth_json(data)
        _logger.info("github_copilot_auth: token cleared")
        return True
    except Exception as e:
        _logger.warning("github_copilot_auth: clear_token failed: %s", e)
        return False


# ── Default provider singleton ────────────────────────────────────────────────

_default_provider = GitHubDeviceFlow()


# ── Backward-compat module-level API ─────────────────────────────────────────
# All callers that use the old flat functions continue to work unchanged.


def start_device_flow(enterprise_url: Optional[str] = None) -> DeviceCodeResponse:
    """POST /login/device/code and return device + user codes.

    Parameters
    ----------
    enterprise_url:
        Optional GitHub Enterprise URL or bare domain (e.g. ``company.ghe.com``
        or ``https://company.ghe.com``).  When None, uses github.com.

    Raises:
        requests.HTTPError   — non-2xx from GitHub
        ValueError           — response missing required fields
    """
    domain = _normalize_domain(enterprise_url) if enterprise_url else "github.com"
    provider = GitHubDeviceFlow(enterprise_url=enterprise_url)
    req = DeviceCodeRequest(
        client_id=CLIENT_ID,
        scope=GITHUB_SCOPE,
        domain=domain,
    )
    return provider.request_device_code(req)


def _interruptible_sleep(total: float, cancel_event: "threading.Event") -> None:
    """Sleep for *total* seconds in 0.5 s chunks, aborting if cancel_event is set.

    .. deprecated::
        Use :func:`src.core.auth.device_flow.interruptible_sleep` directly.
    """
    interruptible_sleep(total, cancel_event)


def poll_for_token(
    device_code: str,
    interval: int,
    domain: str = "github.com",
    timeout: float = 900.0,
    cancel_event: Optional["threading.Event"] = None,
) -> "TokenResult":
    """Block-poll POST /login/oauth/access_token until the user approves.

    Backward-compat wrapper — delegates to :class:`GitHubDeviceFlow`.
    """
    provider = GitHubDeviceFlow(poll_timeout=timeout)
    dcr = DeviceCodeResponse(
        device_code=device_code,
        user_code="",  # not needed for polling
        verification_uri="",  # not needed for polling
        interval=interval,
        domain=domain,
    )
    return provider.poll_for_token(dcr, cancel_event=cancel_event)


def save_token(
    token: "Union[TokenResult, str]",
    enterprise_url: Optional[str] = None,
) -> None:
    """Write the GitHub OAuth token to auth.json under key 'github-copilot'.

    Backward-compat wrapper — delegates to :func:`_save_token_impl`.
    """
    _save_token_impl(token, enterprise_url=enterprise_url)


def refresh_access_token(
    refresh_token: str,
    domain: str = "github.com",
) -> "TokenResult":
    """Exchange a refresh token for a new access + refresh token pair.

    Backward-compat wrapper — delegates to :func:`_refresh_access_token_impl`.
    """
    return _refresh_access_token_impl(refresh_token, domain=domain)


def load_token() -> Optional[str]:
    """Return the stored GitHub OAuth access token, refreshing if near expiry.

    Backward-compat wrapper — delegates to :func:`_load_token_impl`.
    """
    return _load_token_impl()


def load_enterprise_url() -> Optional[str]:
    """Return the stored enterpriseUrl if the token was for a GHE instance."""
    try:
        data = _read_auth_json()
        entry = data.get(_PROVIDER_KEY)
        if entry and isinstance(entry, dict):
            return entry.get("enterpriseUrl") or None
    except Exception:
        pass
    return None


def clear_token() -> bool:
    """Remove the GitHub Copilot entry from auth.json (logout).

    Backward-compat wrapper — delegates to :func:`_clear_token_impl`.
    """
    return _clear_token_impl()


def is_authenticated() -> bool:
    """Return True if a non-empty GitHub token is stored (no network call)."""
    return _default_provider.is_authenticated()


# ── Re-export exceptions for callers that import them from this module ─────────
# DeviceCodeExpired and AuthCancelled are now defined in device_flow.py but are
# re-exported here so existing ``from ... import DeviceCodeExpired`` statements
# continue to work without modification.
__all__ = [
    # classes
    "GitHubDeviceFlow",
    # data-classes (re-exported from device_flow)
    "DeviceCodeRequest",
    "DeviceCodeResponse",
    "TokenResult",
    # exceptions (re-exported from device_flow)
    "AuthCancelled",
    "DeviceCodeExpired",
    # constants
    "CLIENT_ID",
    "GITHUB_CLIENT_ID",
    "GITHUB_SCOPE",
    "TOKEN_REFRESH_MARGIN",
    # module-level backward-compat functions
    "start_device_flow",
    "poll_for_token",
    "save_token",
    "load_token",
    "load_enterprise_url",
    "refresh_access_token",
    "clear_token",
    "is_authenticated",
]
