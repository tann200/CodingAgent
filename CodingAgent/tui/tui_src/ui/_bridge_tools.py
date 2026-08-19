"""BridgeToolsMixin — tool execution, plan, and permission event handlers.

Contains: _on_tool_start, _on_tool_finish, _on_tool_error, _on_delegation_start,
_on_delegation_finish, _on_diff_preview, _on_file_modified, _on_file_deleted,
_on_plan_progress, _on_plan_requested, _on_mcp_server_status,
_on_tool_permission_required, _on_spawn_permission_required,
_on_doom_loop_detected, _on_agent_message, approve_plan, reject_plan,
bash_approved, bash_denied, confirm_file_preview, reject_file_preview.
"""

from __future__ import annotations


from src.core.messaging.event_types import BashApprovalDenied, BashApprovalGranted, PreviewConfirmed, PreviewRejected
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from ._bridge_protocol import AgentBridgeProtocol

TIER3_PREFIXES = (
    "pip ",
    "pip3 ",
    "curl ",
    "wget ",
    "npm install",
    "npm i ",
    "cargo install",
    "go install",
    "go get",
    "apt ",
    "apt-get ",
    "yum ",
    "dnf ",
    "brew ",
    "sudo ",
    "su ",
    "chmod ",
    "chown ",
    "rm ",
    "del ",
)


class BridgeToolsMixin(AgentBridgeProtocol):
    """Mixin providing tool, plan, file and permission event handlers."""

    def _on_tool_start(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import ToolCallStartEvent, BashApprovalEvent

        tool_name = payload.get("title") or payload.get("tool", "unknown")
        tool_args = payload.get("rawInput") or payload.get("args", {})
        tool_id = payload.get("tool_call_id") or payload.get("toolCallId", "")
        if not isinstance(tool_args, dict):
            tool_args = {}

        # §16.1 — bash tier-3 gate
        if tool_name == "bash":
            cmd = tool_args.get("command", "").lower().strip()
            if any(cmd.startswith(p) for p in TIER3_PREFIXES):
                self._post(
                    BashApprovalEvent(
                        tool_id=tool_id, command=tool_args.get("command", "")
                    )
                )
                return

        self._post(
            ToolCallStartEvent(
                tool_name=tool_name, tool_args=tool_args, tool_id=tool_id
            )
        )

    def _on_tool_finish(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import ToolCallFinishEvent

        tool_name = payload.get("title") or payload.get("tool", "unknown")
        tool_id = payload.get("tool_call_id") or payload.get("toolCallId", "")
        content = payload.get("content", [])
        result_text = ""
        if content and isinstance(content, list) and isinstance(content[0], dict):
            result_text = content[0].get("text", "")

        if not result_text:
            formatted = payload.get("result_formatted")
            if not formatted:
                formatted = payload.get("result")
            if formatted:
                result_text = str(formatted)

        if not result_text:
            raw_output = payload.get("rawOutput") or payload.get("raw_output")
            if raw_output is not None:
                if isinstance(raw_output, dict):
                    raw_result = raw_output.get("result") or raw_output.get("error")
                    if raw_result is None:
                        raw_result = raw_output
                    result_text = str(raw_result)
                else:
                    result_text = str(raw_output)

        if not result_text:
            result_text = "(no output)"
        ok = payload.get("ok", True)
        self._post(
            ToolCallFinishEvent(
                tool_name=tool_name, tool_id=tool_id, result_text=result_text, ok=ok
            )
        )

    def _on_tool_error(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import ToolCallErrorEvent

        tool_name = payload.get("title") or payload.get("tool", "unknown")
        tool_id = payload.get("tool_call_id") or payload.get("toolCallId", "")
        self._post(
            ToolCallErrorEvent(
                tool_name=tool_name,
                tool_id=tool_id,
                error=str(payload.get("error", "Unknown error")),
            )
        )

    # SUBAGENT-VIS-3: subagent lifecycle handlers

    def _on_delegation_start(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import SubagentStartEvent

        self._post(
            SubagentStartEvent(
                child_session_id=payload.get("child_session_id", ""),
                role=payload.get("role", "unknown"),
                task=payload.get("task", ""),
                parent_session_id=payload.get("parent_session_id"),
            )
        )

    def _on_delegation_finish(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import SubagentFinishEvent

        self._post(
            SubagentFinishEvent(
                child_session_id=payload.get("child_session_id", ""),
                role=payload.get("role", "unknown"),
                ok=payload.get("ok", True),
            )
        )

    def _on_diff_preview(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import DiffPreviewEvent

        self._post(
            DiffPreviewEvent(
                path=payload.get("path", ""),
                diff=payload.get("diff", ""),
                is_new_file=payload.get("is_new_file", False),
            )
        )

    def _on_file_modified(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import FileModifiedEvent

        self._post(FileModifiedEvent(file_path=payload.get("path", ""), diff=""))

    def _on_file_deleted(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import FileModifiedEvent

        self._post(
            FileModifiedEvent(file_path=f"[deleted] {payload.get('path', '')}", diff="")
        )

    def _on_plan_progress(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import PlanProgressEvent

        # Accept both ACP and legacy schemas (§12.3).
        # Use explicit int() coercion with a guaranteed int fallback so pyright
        # knows step/total are always int (payload values are Any/Unknown).
        _raw_step = (
            payload.get("currentStep")
            if payload.get("currentStep") is not None
            else payload.get("step")
        )
        _raw_total = (
            payload.get("totalSteps")
            if payload.get("totalSteps") is not None
            else payload.get("total")
        )
        step: int = int(_raw_step) if _raw_step is not None else 0
        total: int = int(_raw_total) if _raw_total is not None else 0
        desc = payload.get("stepDescription") or payload.get("description", "")
        self._post(PlanProgressEvent(step=step, total=total, description=desc))

    def _on_plan_requested(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import PlanRequestedEvent

        self._post(PlanRequestedEvent(plan_text=payload.get("plan") or payload.get("plan_text", "")))

    def _on_mcp_server_status(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import McpServerStatusEvent

        self._post(
            McpServerStatusEvent(
                running=payload.get("running", False),
                count=payload.get("count", 0),
                server_names=payload.get("server_names", []),
                has_error=payload.get("has_error", False),
            )
        )

    def _on_tool_permission_required(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import ToolPermissionEvent

        self._post(
            ToolPermissionEvent(
                tool=payload.get("tool", "unknown"),
                args=payload.get("args", {}),
                tool_id=payload.get("tool_id", ""),
            )
        )

    def _on_spawn_permission_required(self, payload: dict) -> None:
        from tui.tui_src.ui.bus import SpawnPermissionEvent

        self._post(
            SpawnPermissionEvent(
                tool=payload.get("tool", "unknown"),
                role=payload.get("role", ""),
                task=payload.get("task", ""),
                tool_id=payload.get("tool_id", ""),
            )
        )

    def _on_doom_loop_detected(self, payload: dict) -> None:
        """PERM-W3: forward doom-loop detection to TUI for user confirmation."""
        from tui.tui_src.ui.bus import DoomLoopEvent

        self._post(
            DoomLoopEvent(
                tool_name=str(payload.get("tool") or payload.get("tool_name", "")),
                fingerprint=str(payload.get("fingerprint", "")),
                count=int(payload.get("count", 3)),
                tool_id=str(payload.get("tool_id", "")),
            )
        )

    def _on_agent_message(self, payload: dict) -> None:
        """CP-15: route send_user_message bus events to the chat panel.

        ``send_user_message`` publishes ``agent.message`` with keys:
          message, attachments, status ("normal" | "proactive").
        Route as AgentFinalResponse so the chat panel renders it immediately.
        """
        from tui.tui_src.ui.bus import AgentFinalResponse

        text = payload.get("message", "")
        if text:
            self._post(AgentFinalResponse(content=text))

    # ── Plan approval ─────────────────────────────────────────────────────

    def approve_plan(self) -> None:
        if self._orchestrator:
            self._orchestrator.approve_plan()
        self._bus.publish_typed(PreviewConfirmed(preview_id="plan"))

    def reject_plan(self) -> None:
        if self._orchestrator:
            self._orchestrator.reject_plan()
        self._bus.publish_typed(PreviewRejected(preview_id="plan"))

    def bash_approved(self, tool_id: str) -> None:
        # TUI-03: publish bash.approval_granted so the backend gate resolves
        self._bus.publish_typed(BashApprovalGranted(tool_id=tool_id))
        # Legacy preview event for backwards-compat
        self._bus.publish_typed(PreviewConfirmed(preview_id=tool_id))

    def bash_denied(self, tool_id: str) -> None:
        # TUI-03: publish bash.approval_denied so the backend gate releases
        self._bus.publish_typed(BashApprovalDenied(tool_id=tool_id))
        # Legacy preview event for backwards-compat
        self._bus.publish_typed(PreviewRejected(preview_id=tool_id))

    def confirm_file_preview(self, path: str) -> None:
        """User accepted the diff preview for a file write — resolve the gate.

        Publishes ``preview.confirmed`` with a ``path`` key so that
        ``PreviewCoordinator._on_confirmed`` can resolve the threading.Event
        gate registered in ``file_tools.register_preview_gate()``.
        """
        self._bus.publish_typed(PreviewConfirmed(path=path))

    def reject_file_preview(self, path: str) -> None:
        """User rejected the diff preview for a file write — resolve the gate.

        Publishes ``preview.rejected`` with a ``path`` key so that
        ``PreviewCoordinator._on_rejected`` can set the rejected flag in
        ``file_tools._preview_rejected`` and unblock the gate.
        """
        self._bus.publish_typed(PreviewRejected(path=path))
