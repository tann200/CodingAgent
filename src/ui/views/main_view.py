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


class MainViewController:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.state = MainViewState()
        self.dashboard = DashboardState()
        # subscribe to orchestrator/provider events
        self.event_bus.subscribe("orchestrator.startup", self._on_startup)
        self.event_bus.subscribe("provider.status.changed", self._on_provider_status)
        self.event_bus.subscribe("ui.notification", self._on_notification)
        # subscribe to file events
        self.event_bus.subscribe("file.modified", self._on_file_modified)
        self.event_bus.subscribe("file.deleted", self._on_file_deleted)
        # subscribe to tool events
        self.event_bus.subscribe("tool.execute.start", self._on_tool_start)
        self.event_bus.subscribe("tool.execute.finish", self._on_tool_finish)
        self.event_bus.subscribe("tool.execute.error", self._on_tool_error)
        # subscribe to plan events
        self.event_bus.subscribe("plan.progress", self._on_plan_progress)
        # subscribe to verification events
        self.event_bus.subscribe("verification.complete", self._on_verification)

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
            tool = payload.get("tool", "unknown")
            args = payload.get("args", {})
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
            tool = payload.get("tool", "unknown")
            ok = payload.get("ok", True)
            # Update last activity for this tool
            for activity in reversed(self.dashboard.tool_activity):
                if activity.get("tool") == tool and activity.get("status") == "running":
                    activity["status"] = "ok" if ok else "error"
                    activity["timestamp"] = datetime.now().isoformat()
                    break

    def _on_tool_error(self, payload: Dict[str, Any]):
        if isinstance(payload, dict):
            tool = payload.get("tool", "unknown")
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
        }
