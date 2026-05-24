from textual.message import Message
from typing import Dict, Any, Optional


class PaletteCommand(Message):
    def __init__(self, command_id: str) -> None:
        self.command_id = command_id
        super().__init__()


class ConnectProvider(Message):
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        super().__init__()


class UpdateSettings(Message):
    def __init__(self, updates: Optional[Dict[str, Any]] = None) -> None:
        self.updates = updates or {}
        super().__init__()


class SlashCommand(Message):
    def __init__(self, command: str, args: str = "") -> None:
        self.command = command
        self.args = args
        super().__init__()


class AgentInterrupt(Message):
    def __init__(self) -> None:
        super().__init__()


class ConsoleLogLine(Message):
    def __init__(self, line: str, level: str = "INFO") -> None:
        self.line = line
        self.level = level
        super().__init__()


class RequestSystemSettings(Message):
    """UI asks the backend to send the current UserPrefs and Providers."""

    def __init__(self) -> None:
        super().__init__()


class SaveProviderCredentials(Message):
    """UI tells the backend to save an API key for a provider."""

    def __init__(
        self, provider_id: str, api_key: str, base_url: Optional[str] = None
    ) -> None:
        self.provider_id = provider_id
        self.api_key = api_key
        self.base_url = base_url
        super().__init__()


class UpdateRoleModel(Message):
    """UI tells the backend that the user changed the model for a specific role."""

    def __init__(self, role: str, model_id: str, provider_id: str = "") -> None:
        self.role = role
        self.model_id = model_id
        self.provider_id = provider_id  # canonical provider key, e.g. "lm_studio"
        super().__init__()


# ── New spec-compliant UI→backend events ─────────────────────────────────────


class PlanApproved(Message):
    """User approved the pending plan."""

    def __init__(self) -> None:
        super().__init__()


class PlanRejected(Message):
    """User rejected the pending plan."""

    def __init__(self) -> None:
        super().__init__()


class BashApproved(Message):
    """User approved a tier-3 bash command."""

    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id
        super().__init__()


class BashDenied(Message):
    """User denied a tier-3 bash command."""

    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id
        super().__init__()


class ToolPermissionApproved(Message):
    """User approved a tool permission request."""

    def __init__(self, tool: str = "", tool_id: str = "") -> None:
        self.tool = tool
        self.tool_id = tool_id
        super().__init__()


class ToolPermissionDenied(Message):
    """User denied a tool permission request."""

    def __init__(self, tool: str = "", tool_id: str = "") -> None:
        self.tool = tool
        self.tool_id = tool_id
        super().__init__()


class StartGithubDeviceFlow(Message):
    """User pressed 'Login with GitHub Copilot' in Settings.

    The app responds by calling start_device_flow(), showing the
    OAuthDeviceFlowScreen modal, and launching background polling.

    Parameters
    ----------
    enterprise_url:
        Optional GitHub Enterprise URL or bare domain (e.g. ``company.ghe.com``).
        When None (default) the public github.com endpoints are used.
    """

    def __init__(self, enterprise_url: Optional[str] = None) -> None:
        self.enterprise_url = enterprise_url
        super().__init__()
