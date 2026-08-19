"""Generic OAuth Device Flow (RFC 8628) abstraction.

This module provides:

* Shared **data-classes** (``DeviceCodeRequest``, ``DeviceCodeResponse``,
  ``TokenResult``) that any device-flow provider can use.
* Shared **exceptions** (``DeviceCodeExpired``, ``AuthCancelled``) that are
  raised by all concrete providers.
* A **utility function** ``interruptible_sleep()`` for cancellable polling
  loops.
* An **abstract base class** ``DeviceFlowProvider`` that standardises the
  contract: any provider (GitHub, GitLab, Azure AD, …) sub-classes it and
  implements the four abstract methods.

Usage example
-------------
::

    import threading
    from src.core.auth.device_flow import DeviceFlowProvider

    class MyProvider(DeviceFlowProvider):
        def request_device_code(self, req):  ...
        def poll_for_token(self, dcr, cancel_event):  ...
        def save_token(self, token):  ...
        def load_token(self):  ...

    provider = MyProvider()
    req = DeviceCodeRequest(client_id="...", scope="...", domain="example.com")
    dcr = provider.request_device_code(req)
    print(f"Visit {dcr.verification_uri} and enter: {dcr.user_code}")
    cancel = threading.Event()
    result = provider.poll_for_token(dcr, cancel)
    provider.save_token(result)
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# ── Exceptions ────────────────────────────────────────────────────────────────


class DeviceCodeExpired(Exception):
    """Raised when the device code expires before the user authorises."""


class AuthCancelled(Exception):
    """Raised when the user explicitly cancels or denies the authorisation."""


# ── Data-classes ──────────────────────────────────────────────────────────────


@dataclass
class DeviceCodeRequest:
    """Parameters required to initiate the device-authorisation request.

    Attributes
    ----------
    client_id:
        The OAuth application's client ID.
    scope:
        Space-separated list of OAuth scopes to request.
    domain:
        The authorisation server domain (e.g. ``"github.com"``).
        Concrete providers use this to build the endpoint URLs.
    extra:
        Provider-specific extra parameters (e.g. ``resource``, ``tenant``).
    """

    client_id: str
    scope: str
    domain: str = "github.com"
    extra: dict = field(default_factory=dict)


@dataclass
class DeviceCodeResponse:
    """Response from the device-authorisation endpoint.

    Attributes
    ----------
    device_code:
        Opaque code that the client presents when polling for a token.
    user_code:
        Short human-readable code that the user enters at ``verification_uri``.
    verification_uri:
        URL the user should open in a browser.
    interval:
        Minimum number of seconds between polling requests.
    expires_in:
        Seconds until *device_code* expires (default 900 = 15 min).
    domain:
        The domain from which this code was obtained; passed back to the
        polling endpoint.
    """

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int = 900
    domain: str = "github.com"


@dataclass
class TokenResult:
    """Result returned by a completed device-authorisation flow.

    Attributes
    ----------
    access_token:
        The bearer token for API calls.
    refresh_token:
        Long-lived refresh token (``None`` for providers that don't issue one).
    expires_in:
        Seconds until *access_token* expires; ``0`` means "no expiry".
    refresh_token_expires_in:
        Seconds until *refresh_token* expires; ``0`` means "no expiry".
    """

    access_token: str
    refresh_token: Optional[str] = None
    expires_in: int = 0
    refresh_token_expires_in: int = 0


# ── Utility ───────────────────────────────────────────────────────────────────

_SLEEP_CHUNK = 0.5  # seconds — granularity of cancel-check during sleep


def interruptible_sleep(
    total: float,
    cancel_event: "threading.Event",
    chunk: float = _SLEEP_CHUNK,
) -> None:
    """Sleep for *total* seconds, waking every *chunk* seconds to check *cancel_event*.

    Parameters
    ----------
    total:
        Total duration to sleep, in seconds.
    cancel_event:
        A :class:`threading.Event` that, when set, causes the function to
        raise :exc:`AuthCancelled` immediately.
    chunk:
        How often (in seconds) to check *cancel_event*.  Smaller values give
        faster cancellation response but more CPU wakeups.

    Raises
    ------
    AuthCancelled
        Immediately when *cancel_event* becomes set.
    """
    slept = 0.0
    while slept < total:
        if cancel_event.is_set():
            raise AuthCancelled("Login cancelled by user.")
        step = min(chunk, total - slept)
        time.sleep(step)
        slept += step


# ── Abstract base class ───────────────────────────────────────────────────────


class DeviceFlowProvider(ABC):
    """Abstract base class for RFC 8628 OAuth device-flow providers.

    Sub-class this to implement a concrete provider (e.g. GitHub, GitLab, Azure
    AD).  The four abstract methods map directly onto the four phases of the
    device-authorisation flow:

    1. :meth:`request_device_code` — POST to the device-authorisation endpoint.
    2. :meth:`poll_for_token`      — Poll the token endpoint until approved.
    3. :meth:`save_token`          — Persist the obtained token.
    4. :meth:`load_token`          — Load (and optionally refresh) a stored token.

    A minimal concrete implementation::

        class SimpleProvider(DeviceFlowProvider):
            _token: Optional[TokenResult] = None

            def request_device_code(self, req):
                # ... HTTP call ...
                return DeviceCodeResponse(...)

            def poll_for_token(self, dcr, cancel_event=None):
                # ... polling loop ...
                return TokenResult(access_token="...")

            def save_token(self, token):
                self._token = token

            def load_token(self):
                return self._token.access_token if self._token else None
    """

    # ── abstract interface ─────────────────────────────────────────────────

    @abstractmethod
    def request_device_code(self, req: DeviceCodeRequest) -> DeviceCodeResponse:
        """POST to the device-authorisation endpoint and return the codes.

        Parameters
        ----------
        req:
            All parameters needed to build the authorisation request.

        Returns
        -------
        DeviceCodeResponse
            The device code, user code, and polling metadata.

        Raises
        ------
        requests.HTTPError
            If the server returns a non-2xx status.
        ValueError
            If the response is missing required fields.
        """

    @abstractmethod
    def poll_for_token(
        self,
        dcr: DeviceCodeResponse,
        cancel_event: Optional["threading.Event"] = None,
    ) -> TokenResult:
        """Poll the token endpoint until the user approves (or an error occurs).

        Parameters
        ----------
        dcr:
            The :class:`DeviceCodeResponse` from :meth:`request_device_code`.
        cancel_event:
            Optional :class:`threading.Event`; when set the poll loop raises
            :exc:`AuthCancelled`.

        Returns
        -------
        TokenResult
            The access (and optional refresh) token.

        Raises
        ------
        DeviceCodeExpired
            If the device code expires before the user authorises.
        AuthCancelled
            If the user denies access or *cancel_event* is set.
        TimeoutError
            If the poll loop exhausts its deadline.
        """

    @abstractmethod
    def save_token(self, token: TokenResult) -> None:
        """Persist *token* to durable storage.

        Parameters
        ----------
        token:
            The :class:`TokenResult` to store.
        """

    @abstractmethod
    def load_token(self) -> Optional[str]:
        """Load the stored access token, refreshing if near expiry.

        Returns
        -------
        str or None
            The access token string, or ``None`` if no token is stored.
        """

    # ── optional helpers (may be overridden) ──────────────────────────────

    def is_authenticated(self) -> bool:
        """Return ``True`` if a non-empty access token is currently stored.

        The default implementation calls :meth:`load_token` and checks that
        the result is a non-empty string.  Concrete providers may override
        this (e.g. to add a network validity check).
        """
        tok = self.load_token()
        return bool(tok and tok.strip())

    def clear_token(self) -> bool:
        """Remove the stored token (logout).

        The base implementation is a no-op that returns ``False``.  Concrete
        providers should override this to delete the token from their storage
        backend.

        Returns
        -------
        bool
            ``True`` on success, ``False`` if the token could not be removed.
        """
        return False  # pragma: no cover
