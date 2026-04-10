"""
tui/src/ui/features/oauth/screen.py — GitHub Copilot OAuth device-flow modal.

Mirrors OpenCode's dialog-provider.tsx AutoMethod + PromptsMethod:

  Step 1 (DeploymentSelectScreen):
    Ask user whether they want github.com or GitHub Enterprise.
    If enterprise, prompt for the GHE domain.
    On confirm → fires StartGithubDeviceFlow(enterprise_url=...) to app.

  Step 2 (OAuthDeviceFlowScreen):
    Shows verification_uri + user_code, "Open in Browser" button, spinner.
    Dismisses on DeviceFlowCompleteEvent / DeviceFlowErrorEvent.

The screens do NOT do the polling themselves — polling runs in a background
thread managed by AgentApp, keeping Textual's async loop free.
"""

from __future__ import annotations

import webbrowser
from typing import Optional

from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    LoadingIndicator,
    RadioButton,
    RadioSet,
    Static,
)
from textual.containers import Vertical, Horizontal
from textual import on

from ...logging import get_logger

logger = get_logger("oauth")


# ── Step 1: Deployment type selection ────────────────────────────────────────


class DeploymentSelectScreen(ModalScreen):
    """Ask whether to use github.com or GitHub Enterprise.

    Mirrors OpenCode's copilot.ts prompts[] — a 'select' for deploymentType
    followed by an optional 'text' prompt for the enterpriseUrl.

    On confirmation fires ``StartGithubDeviceFlow`` with the resolved
    ``enterprise_url`` (None for github.com).
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    DeploymentSelectScreen {
        align: center middle;
    }

    #deploy_box {
        width: 72;
        padding: 2 3;
        border: round $primary;
        background: $surface;
    }

    #deploy_title {
        text-style: bold;
        margin-bottom: 1;
    }

    #deploy_subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }

    #deploy_radio {
        margin-bottom: 1;
    }

    #deploy_ghe_label {
        color: $text-muted;
        margin-top: 1;
    }

    #deploy_ghe_input {
        margin-bottom: 1;
    }

    #deploy_ghe_error {
        color: $error;
        height: 1;
    }

    #deploy_actions {
        margin-top: 1;
        align-horizontal: right;
    }

    #deploy_confirm_btn {
        margin-right: 1;
    }
    """

    def compose(self):
        with Vertical(id="deploy_box"):
            yield Label("Login with GitHub Copilot", id="deploy_title")
            yield Label("Select GitHub deployment type:", id="deploy_subtitle")
            with RadioSet(id="deploy_radio"):
                yield RadioButton(
                    "GitHub.com  (Public)", id="radio_github_com", value=True
                )
                yield RadioButton(
                    "GitHub Enterprise  (Data residency or self-hosted)", id="radio_ghe"
                )
            yield Label(
                "Enter your GitHub Enterprise URL or domain:", id="deploy_ghe_label"
            )
            yield Input(
                placeholder="company.ghe.com  or  https://company.ghe.com",
                id="deploy_ghe_input",
            )
            yield Static("", id="deploy_ghe_error")
            with Horizontal(id="deploy_actions"):
                yield Button("Continue", id="deploy_confirm_btn", variant="primary")
                yield Button("Cancel", id="deploy_cancel_btn")

    def on_mount(self) -> None:
        # Hide GHE URL field initially
        self._set_ghe_visible(False)

    # ── Radio toggle ─────────────────────────────────────────────────────────

    @on(RadioSet.Changed)
    def on_radio_changed(self, event: RadioSet.Changed) -> None:
        is_ghe = event.index == 1
        self._set_ghe_visible(is_ghe)

    def _set_ghe_visible(self, visible: bool) -> None:
        try:
            self.query_one("#deploy_ghe_label").display = visible
            self.query_one("#deploy_ghe_input").display = visible
            self.query_one("#deploy_ghe_error").display = visible
        except Exception:
            pass

    # ── Buttons ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#deploy_confirm_btn")
    def handle_confirm(self) -> None:
        from ...events import StartGithubDeviceFlow

        # Determine which radio is selected
        try:
            rs = self.query_one("#deploy_radio", RadioSet)
            is_enterprise = rs.pressed_index == 1
        except Exception:
            is_enterprise = False

        enterprise_url: Optional[str] = None
        if is_enterprise:
            try:
                raw = self.query_one("#deploy_ghe_input", Input).value.strip()
            except Exception:
                raw = ""
            if not raw:
                try:
                    self.query_one("#deploy_ghe_error", Static).update(
                        "URL or domain is required"
                    )
                except Exception:
                    pass
                return
            # Basic validation — must look like a hostname or URL
            if "." not in raw.replace("http://", "").replace("https://", ""):
                try:
                    self.query_one("#deploy_ghe_error", Static).update(
                        "Please enter a valid URL (e.g. company.ghe.com)"
                    )
                except Exception:
                    pass
                return
            enterprise_url = raw

        # Dismiss this screen and kick off the device flow
        self.dismiss()
        self.app.post_message(StartGithubDeviceFlow(enterprise_url=enterprise_url))

    @on(Button.Pressed, "#deploy_cancel_btn")
    def handle_cancel_btn(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss()


# ── Step 2: Device code display + waiting spinner ─────────────────────────────


class OAuthDeviceFlowScreen(ModalScreen):
    """Modal shown while the GitHub Copilot device-flow is in progress.

    Parameters
    ----------
    user_code:
        The 8-character code the user must enter at *verification_uri*.
    verification_uri:
        The GitHub page the user must visit (usually
        ``https://github.com/login/device``).
    expires_in:
        Seconds until the device code expires (shown as advisory text).
    """

    BINDINGS = [("escape", "cancel_flow", "Cancel")]

    DEFAULT_CSS = """
    OAuthDeviceFlowScreen {
        align: center middle;
    }

    #oauth_box {
        width: 70;
        padding: 2 3;
        border: round $primary;
        background: $surface;
    }

    #oauth_title {
        text-style: bold;
        margin-bottom: 1;
    }

    #oauth_code_label {
        color: $text-muted;
        margin-top: 1;
    }

    #oauth_code {
        text-style: bold;
        color: $success;
        text-align: center;
        margin-bottom: 1;
    }

    #oauth_link_label {
        color: $text-muted;
        margin-bottom: 1;
    }

    #oauth_link {
        color: $primary;
        text-style: underline;
    }

    #oauth_waiting_row {
        margin-top: 1;
        height: 3;
    }

    #oauth_waiting_text {
        color: $text-muted;
        padding-top: 1;
    }

    #oauth_actions {
        margin-top: 2;
        align-horizontal: right;
    }

    #oauth_open_btn {
        margin-right: 1;
    }
    """

    def __init__(
        self,
        user_code: str,
        verification_uri: str,
        expires_in: int = 900,
    ) -> None:
        super().__init__()
        self._user_code = user_code
        self._verification_uri = verification_uri
        self._expires_in = expires_in
        self._done = False  # guard against double-dismiss

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self):
        minutes = self._expires_in // 60
        with Vertical(id="oauth_box"):
            yield Label("Login with GitHub Copilot", id="oauth_title")
            yield Label(
                "1. Visit the URL below (or press Open in Browser):",
                id="oauth_link_label",
            )
            yield Static(self._verification_uri, id="oauth_link", markup=False)
            yield Label(
                "2. Enter this one-time code on that page:",
                id="oauth_code_label",
            )
            yield Static(self._user_code, id="oauth_code", markup=False)
            yield Label(
                f"   (code expires in ~{minutes} min — do not close this dialog)",
                id="oauth_expiry",
            )
            with Horizontal(id="oauth_waiting_row"):
                yield LoadingIndicator()
                yield Static("  Waiting for authorization…", id="oauth_waiting_text")
            with Horizontal(id="oauth_actions"):
                yield Button("Open in Browser", id="oauth_open_btn", variant="primary")
                yield Button("Cancel", id="oauth_cancel_btn", variant="default")

    # ── Events ────────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#oauth_open_btn")
    def handle_open_browser(self) -> None:
        try:
            webbrowser.open(self._verification_uri)
        except Exception as exc:
            logger.warning(f"Could not open browser: {exc}")

    @on(Button.Pressed, "#oauth_cancel_btn")
    def handle_cancel(self) -> None:
        self.action_cancel_flow()

    def action_cancel_flow(self) -> None:
        """User cancelled — post a cancellation event so polling stops."""
        if self._done:
            return
        self._done = True
        try:
            from ...bus import DeviceFlowErrorEvent

            self.app.post_message(DeviceFlowErrorEvent(reason="cancelled"))
        except Exception:
            pass
        self.dismiss()

    def complete(self) -> None:
        """Called by AgentApp when DeviceFlowCompleteEvent arrives."""
        if self._done:
            return
        self._done = True
        self.dismiss()

    def fail(self, reason: str) -> None:
        """Called by AgentApp when DeviceFlowErrorEvent arrives."""
        if self._done:
            return
        self._done = True
        # Update the waiting text BEFORE removing its parent row, so the
        # query can still find the widget.  The modal is dismissed immediately
        # after, so the visual update is only briefly visible.
        try:
            self.query_one("#oauth_waiting_text", Static).update(
                f"[bold red]Failed:[/] {reason}"
            )
        except Exception:
            pass
        self.dismiss()
