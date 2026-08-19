"""Authentication package.

Provides a generic device-flow OAuth ABC (``device_flow.DeviceFlowProvider``)
and the GitHub Copilot concrete implementation
(``github_copilot_auth.GitHubDeviceFlow``).

Quick start
-----------
::

    from src.core.auth.device_flow import (
        DeviceFlowProvider,
        DeviceCodeRequest,
        DeviceCodeResponse,
        TokenResult,
    )
    from src.core.inference.adapters.github_copilot_auth import GitHubDeviceFlow
"""

from src.core.auth.device_flow import (  # noqa: F401
    AuthCancelled,
    DeviceCodeExpired,
    DeviceCodeRequest,
    DeviceCodeResponse,
    DeviceFlowProvider,
    TokenResult,
)

__all__ = [
    "AuthCancelled",
    "DeviceCodeExpired",
    "DeviceCodeRequest",
    "DeviceCodeResponse",
    "DeviceFlowProvider",
    "TokenResult",
]
