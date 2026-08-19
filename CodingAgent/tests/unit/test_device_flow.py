"""Tests for generic device-flow abstractions (Gap 7)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from src.core.auth.device_flow import (
    AuthCancelled,
    DeviceCodeRequest,
    DeviceCodeResponse,
    DeviceFlowProvider,
    TokenResult,
    interruptible_sleep,
)
from src.core.inference.adapters.github_copilot_auth import (
    GITHUB_CLIENT_ID,
    GitHubDeviceFlow,
)


class _DummyProvider(DeviceFlowProvider):
    """Minimal concrete provider for testing base-class helpers."""

    def __init__(self) -> None:
        self._token: TokenResult | None = None

    def request_device_code(self, req: DeviceCodeRequest) -> DeviceCodeResponse:
        return DeviceCodeResponse(
            device_code="dummy",
            user_code="CODE",
            verification_uri="https://example.test/device",
            interval=1,
            domain=req.domain,
        )

    def poll_for_token(
        self,
        dcr: DeviceCodeResponse,
        cancel_event: threading.Event | None = None,
    ) -> TokenResult:
        if cancel_event and cancel_event.is_set():
            raise AuthCancelled("cancelled")
        return TokenResult(access_token=f"tok-{dcr.domain}")

    def save_token(self, token: TokenResult) -> None:
        self._token = token

    def load_token(self) -> str | None:
        return self._token.access_token if self._token else None

    def clear_token(self) -> bool:
        self._token = None
        return True


def _mock_response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    return resp


class TestDeviceFlowAbstractions:
    def test_abc_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            DeviceFlowProvider()  # type: ignore[abstract]

    def test_dataclass_defaults(self):
        req = DeviceCodeRequest(client_id="cid", scope="read:user")
        dcr = DeviceCodeResponse(
            device_code="dev",
            user_code="code",
            verification_uri="https://github.com/login/device",
            interval=5,
        )
        tok = TokenResult(access_token="ghu_abc")

        assert req.domain == "github.com"
        assert dcr.expires_in == 900
        assert dcr.domain == "github.com"
        assert tok.refresh_token is None
        assert tok.expires_in == 0

    def test_interruptible_sleep_raises_when_cancelled(self):
        event = threading.Event()
        event.set()
        with pytest.raises(AuthCancelled):
            interruptible_sleep(1.0, event)

    def test_base_helper_is_authenticated_uses_load_token(self):
        provider = _DummyProvider()
        assert provider.is_authenticated() is False
        provider.save_token(TokenResult(access_token="abc"))
        assert provider.is_authenticated() is True


class TestGitHubDeviceFlowProvider:
    def test_provider_is_device_flow_provider(self):
        assert isinstance(GitHubDeviceFlow(), DeviceFlowProvider)

    def test_request_device_code_uses_request_object(self):
        response = _mock_response(
            {
                "device_code": "dev123",
                "user_code": "ABCD-1234",
                "verification_uri": "https://github.com/login/device",
                "interval": 5,
                "expires_in": 900,
            }
        )
        provider = GitHubDeviceFlow()
        req = DeviceCodeRequest(
            client_id=GITHUB_CLIENT_ID,
            scope="read:user",
            domain="github.com",
        )

        with patch("requests.post", return_value=response) as mock_post:
            result = provider.request_device_code(req)

        assert result.device_code == "dev123"
        body = mock_post.call_args[1]["json"]
        assert body["client_id"] == GITHUB_CLIENT_ID
        assert body["scope"] == "read:user"

    def test_poll_for_token_success(self):
        provider = GitHubDeviceFlow(poll_timeout=60)
        dcr = DeviceCodeResponse(
            device_code="dev_code",
            user_code="unused",
            verification_uri="unused",
            interval=1,
            domain="github.com",
        )
        response = _mock_response({"access_token": "ghu_tok123"})

        with patch("requests.post", return_value=response):
            result = provider.poll_for_token(dcr)

        assert isinstance(result, TokenResult)
        assert result.access_token == "ghu_tok123"

    def test_poll_for_token_cancelled_before_http(self):
        provider = GitHubDeviceFlow(poll_timeout=60)
        dcr = DeviceCodeResponse(
            device_code="dev_code",
            user_code="unused",
            verification_uri="unused",
            interval=1,
            domain="github.com",
        )
        cancel = threading.Event()
        cancel.set()

        with patch("requests.post") as mock_post:
            with pytest.raises(AuthCancelled):
                provider.poll_for_token(dcr, cancel_event=cancel)

        mock_post.assert_not_called()

    def test_provider_save_load_clear_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CODINGAGENT_PREFS", str(tmp_path / "prefs.json"))
        provider = GitHubDeviceFlow()

        provider.save_token(TokenResult(access_token="ghu_roundtrip"))
        assert provider.load_token() == "ghu_roundtrip"
        assert provider.is_authenticated() is True

        assert provider.clear_token() is True
        assert provider.load_token() in (None, "")
