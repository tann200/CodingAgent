"""Main view controller for the TUI with dashboard widgets.

This module exposes a `MainViewController` class that will be used by the
Textual application to build the layout. For tests it can be instantiated and
inspected without rendering.

Includes dashboard widgets that subscribe to EventBus for live data:
- ModifiedFilesPanel: Shows files that have been edited
- TaskProgressPanel: Shows plan execution progress
- ToolActivityPanel: Shows recent tool activity
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime

from src.core.orchestration.event_bus import EventBus


@dataclass
class MainViewState:
    status: str = "idle"
    active_provider: Optional[str] = None
    last_notification: Optional[str] = None
    notification_level: Optional[str] = None


@dataclass
class DashboardState:
    modified_files: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    tool_activity: List[Dict[str, Any]] = field(default_factory=list)
    plan_progress: Dict[str, Any] = field(default_factory=dict)
    verification_status: Optional[str] = None
    last_tool_result: Optional[str] = None  # formatted result of last tool call
    token_budget: Dict[str, Any] = field(
        default_factory=dict
    )  # Phase 4: token budget display
    preview_pending: Optional[str] = None  # Phase 3: preview mode status
    available_snapshots: List[Dict[str, Any]] = field(
        default_factory=list
    )  # Session resume


class MainViewController:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.state = MainViewState()
        self.dashboard = DashboardState()
        self._subscriptions: list = []
        # subscribe to orchestrator/provider events
        self._subscribe("orchestrator.startup", self._on_startup)
        self._subscribe("provider.status.changed", self._on_provider_status)
        self._subscribe("ui.notification", self._on_notification)
        # subscribe to file events
        self._subscribe("file.modified", self._on_file_modified)
        self._subscribe("file.deleted", self._on_file_deleted)
        # subscribe to tool events
        self._subscribe("tool.execute.start", self._on_tool_start)
        self._subscribe("tool.execute.finish", self._on_tool_finish)
        self._subscribe("tool.execute.error", self._on_tool_error)
        # subscribe to plan events
        self._subscribe("plan.progress", self._on_plan_progress)
        # subscribe to verification events
        self._subscribe("verification.complete", self._on_verification)
        # Phase 4: subscribe to token budget events
        self._subscribe("token.budget.update", self._on_token_budget)
        self._subscribe("token.budget.warning", self._on_token_warning)
        # Phase 3: subscribe to preview mode events
        self._subscribe("preview.pending", self._on_preview_pending)
        self._subscribe("preview.confirmed", self._on_preview_confirmed)
        self._subscribe("preview.rejected", self._on_preview_rejected)
        # Session lifecycle events
        self._subscribe("session.registered", self._on_session_registered)
        self._subscribe("session.unregistered", self._on_session_unregistered)
        self._subscribe("session.health_alert", self._on_session_health_alert)

    def _subscribe(self, event_name: str, callback) -> None:
        """Subscribe and track for later cleanup."""
        self.event_bus.subscribe(event_name, callback)
        self._subscriptions.append((event_name, callback))

    def cleanup(self) -> None:
        """Unsubscribe all event handlers to prevent callback leaks."""
        for event_name, callback in self._subscriptions:
            self.event_bus.unsubscribe(event_name, callback)
        self._subscriptions.clear()

    def _on_startup(self, payload):
        self.state.status = "started"

    def _on_provider_status(self, payload):
        if isinstance(payload, dict):
            self.state.active_provider = payload.get("provider")

    def _on_notification(self, payload):
        if isinstance(payload, dict):
            self.state.last_notification = payload.get("message")
            self.state.notification_level = payload.get("level", "info")

    def _on_file_modified(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            path = payload.get("path", "")
            tool = payload.get("tool", "unknown")
            self.dashboard.modified_files[path] = {
                "tool": tool,
                "timestamp": datetime.now().isoformat(),
                "action": "modified",
            }

    def _on_file_deleted(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            path = payload.get("path", "")
            self.dashboard.modified_files[path] = {
                "tool": "delete_file",
                "timestamp": datetime.now().isoformat(),
                "action": "deleted",
            }

    def _on_tool_start(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            # GAP 2: ACP schema uses "title", legacy uses "tool"
            tool = payload.get("title") or payload.get("tool", "unknown")
            args = payload.get("rawInput") or payload.get("args", {})
            self.dashboard.tool_activity.append(
                {
                    "tool": tool,
                    "args": args,
                    "status": "running",
                    "timestamp": datetime.now().isoformat(),
                }
            )
            # Keep only last 10 activities
            if len(self.dashboard.tool_activity) > 10:
                self.dashboard.tool_activity = self.dashboard.tool_activity[-10:]

    def _on_tool_finish(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            # GAP 2: ACP schema uses "title", legacy uses "tool"
            tool = payload.get("title") or payload.get("tool", "unknown")
            # ACP schema uses "status", legacy uses "ok"
            status = payload.get("status", "completed")
            ok = status == "completed" if status else payload.get("ok", True)
            # Capture formatted result for display
            result_formatted = payload.get("result_formatted")
            if not result_formatted:
                # Extract from ACP content field
                content = payload.get("content", [])
                if content and isinstance(content, list):
                    result_formatted = content[0].get("text", "")
            if result_formatted:
                self.dashboard.last_tool_result = result_formatted
            # Update last activity for this tool
            for activity in reversed(self.dashboard.tool_activity):
                if activity.get("tool") == tool and activity.get("status") == "running":
                    activity["status"] = "ok" if ok else "error"
                    activity["timestamp"] = datetime.now().isoformat()
                    if result_formatted:
                        activity["result"] = result_formatted
                    break

    def _on_tool_error(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            # GAP 2: ACP schema uses "title", legacy uses "tool"
            tool = payload.get("title") or payload.get("tool", "unknown")
            # ACP schema uses "content[0].text", legacy uses "error"
            error = ""
            content = payload.get("content", [])
            if content and isinstance(content, list):
                error = content[0].get("text", "")
            if not error:
                error = payload.get("error", "")
            # Update last activity for this tool
            for activity in reversed(self.dashboard.tool_activity):
                if activity.get("tool") == tool and activity.get("status") == "running":
                    activity["status"] = "error"
                    activity["error"] = error
                    activity["timestamp"] = datetime.now().isoformat()
                    break

    def _on_plan_progress(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.plan_progress = payload

    def _on_verification(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.verification_status = payload.get("status", "unknown")

    # Phase 4: Token budget event handlers
    def _on_token_budget(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.token_budget = {
                "used_tokens": payload.get("used_tokens", 0),
                "max_tokens": payload.get("max_tokens", 6000),
                "usage_ratio": payload.get("usage_ratio", 0),
                "session_id": payload.get("session_id", "default"),
            }

    def _on_token_warning(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.token_budget["warning"] = True
            self.dashboard.token_budget["message"] = payload.get(
                "message", "Token budget warning"
            )

    # Phase 3: Preview mode event handlers
    def _on_preview_pending(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.preview_pending = payload.get("preview_id")

    def _on_preview_confirmed(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.preview_pending = None

    def _on_preview_rejected(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.preview_pending = None

    def _on_session_registered(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.available_snapshots = self._load_available_snapshots()

    def _on_session_unregistered(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            self.dashboard.available_snapshots = self._load_available_snapshots()

    def _on_session_health_alert(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            message = payload.get("message", "Session health alert")
            self.state.last_notification = f"Session Alert: {message}"
            self.state.notification_level = "warning"

    def _load_available_snapshots(self) -> List[Dict[str, Any]]:
        """Load available session snapshots for resume."""
        try:
            from src.core.orchestration.session_lifecycle import (
                get_session_lifecycle_manager,
            )
            from pathlib import Path

            lifecycle = get_session_lifecycle_manager(str(Path.cwd()))
            return lifecycle.list_snapshots()
        except Exception:
            return []

    def refresh_available_sessions(self) -> List[Dict[str, Any]]:
        """Refresh and return list of available sessions for resume."""
        self.dashboard.available_snapshots = self._load_available_snapshots()
        return self.dashboard.available_snapshots

    def get_status(self) -> str:
        return self.state.status

    def get_dashboard_summary(self) -> Dict[str, Any]:
        """Get a summary of dashboard state for UI display."""
        return {
            "modified_files_count": len(self.dashboard.modified_files),
            "modified_files": list(self.dashboard.modified_files.keys()),
            "recent_activities": self.dashboard.tool_activity[-5:],
            "plan_progress": self.dashboard.plan_progress,
            "verification_status": self.dashboard.verification_status,
            "last_tool_result": self.dashboard.last_tool_result,
            "token_budget": self.dashboard.token_budget,
            "preview_pending": self.dashboard.preview_pending,
            "available_snapshots": self.dashboard.available_snapshots,
        }

    def get_token_budget_display(self) -> str:
        """Get formatted token budget display string."""
        budget = self.dashboard.token_budget
        if not budget:
            return ""
        ratio = budget.get("usage_ratio", 0)
        used = budget.get("used_tokens", 0)
        max_tok = budget.get("max_tokens", 6000)
        if budget.get("warning"):
            return f"Token Budget: {ratio:.0%} ({used}/{max_tok}) - WARNING"
        return f"Token Budget: {ratio:.0%} ({used}/{max_tok})"
