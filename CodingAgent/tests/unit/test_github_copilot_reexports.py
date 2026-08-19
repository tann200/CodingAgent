"""Tests to assert backward-compatible re-exports in github_copilot_auth."""

from __future__ import annotations

from importlib import import_module


def test_reexports_present():
    m = import_module("src.core.inference.adapters.github_copilot_auth")
    # Verify a small set of expected re-exports/aliases
    for name in (
        "GitHubDeviceFlow",
        "DeviceCodeRequest",
        "DeviceCodeResponse",
        "TokenResult",
        "AuthCancelled",
        "DeviceCodeExpired",
        "CLIENT_ID",
        "GITHUB_CLIENT_ID",
        "GITHUB_SCOPE",
        "start_device_flow",
        "poll_for_token",
        "save_token",
        "load_token",
        "refresh_access_token",
        "clear_token",
        "is_authenticated",
    ):
        assert hasattr(m, name), f"Missing re-export: {name}"
