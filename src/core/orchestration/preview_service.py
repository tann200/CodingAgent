"""
Preview Mode service for diff before executing file changes.
Uses asyncio.Event for proper LangGraph suspension.
"""

import asyncio
import difflib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Literal

logger = logging.getLogger(__name__)


@dataclass
class DiffPreview:
    preview_id: str
    tool_name: str
    args: Dict[str, Any]
    old_content: Optional[str] = None
    new_content: Optional[str] = None
    diff: str = ""
    file_path: str = ""
    status: Literal["pending", "confirmed", "rejected"] = "pending"
    created_at: datetime = field(default_factory=datetime.now)
    # CF-4: Do NOT create asyncio.Event at field-default time — there may be no running
    # event loop at construction time (e.g. during Textual startup before asyncio.run).
    # The event is created lazily inside generate_preview() while a coroutine is running.
    confirmed_event: Optional[asyncio.Event] = field(default=None)


class PreviewService:
    """
    Generates and manages diff previews for Preview Mode.

    CRITICAL: Uses asyncio.Event for proper LangGraph suspension.
    """

    _instance = None

    def __init__(self, workdir: str = ""):
        self.workdir = Path(workdir) if workdir else Path(".")
        self.pending_previews: Dict[str, DiffPreview] = {}

    @classmethod
    def get_instance(cls, workdir: str = "") -> "PreviewService":
        if cls._instance is None:
            cls._instance = cls(workdir=workdir)
        return cls._instance

    def generate_preview(
        self,
        tool_name: str,
        args: Dict[str, Any],
        old_content: Optional[str] = None,
        new_content: Optional[str] = None,
    ) -> DiffPreview:
        """Generate diff preview for a tool action."""
        preview_id = str(uuid.uuid4())[:8]

        file_path = args.get("path", "")
        diff = ""
        if old_content is not None and new_content is not None:
            diff = self._compute_diff(old_content, new_content, file_path)

        preview = DiffPreview(
            preview_id=preview_id,
            tool_name=tool_name,
            args=args,
            old_content=old_content,
            new_content=new_content,
            diff=diff,
            file_path=file_path,
            status="pending",
        )

        # CF-4: Create the asyncio.Event lazily here, inside a running coroutine context,
        # so it is bound to the correct event loop.
        preview.confirmed_event = asyncio.Event()
        self.pending_previews[preview_id] = preview
        logger.info(f"PreviewService: generated preview {preview_id} for {tool_name}")
        return preview

    def _compute_diff(self, old: str, new: str, file_path: str = "") -> str:
        """Compute unified diff between two strings."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        label = file_path or str(self.workdir)
        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile="a/" + label,
                tofile="b/" + label,
                lineterm="",
            )
        )
        return "".join(diff)

    def get_preview(self, preview_id: str) -> Optional[DiffPreview]:
        """Get preview by ID."""
        return self.pending_previews.get(preview_id)

    async def wait_for_confirmation(self, preview_id: str) -> bool:
        """Async wait for user confirmation."""
        preview = self.get_preview(preview_id)
        if not preview:
            return False
        # CF-4: guard against missing event (should not happen after generate_preview fix)
        if preview.confirmed_event is None:
            logger.warning(f"PreviewService: preview {preview_id} has no event — auto-confirming")
            return True

        await preview.confirmed_event.wait()
        return preview.status == "confirmed"

    def confirm(self, preview_id: str) -> bool:
        """User confirmed preview - sets the event to unblock graph."""
        preview = self.get_preview(preview_id)
        if not preview:
            return False

        preview.status = "confirmed"
        if preview.confirmed_event is not None:
            preview.confirmed_event.set()
        logger.info(f"PreviewService: preview {preview_id} confirmed")
        return True

    def reject(self, preview_id: str):
        """User rejected preview - sets the event to unblock graph."""
        preview = self.get_preview(preview_id)
        if not preview:
            return

        preview.status = "rejected"
        if preview.confirmed_event is not None:
            preview.confirmed_event.set()
        logger.info(f"PreviewService: preview {preview_id} rejected")

    def clear_preview(self, preview_id: str):
        """Clear a preview from the pending list."""
        if preview_id in self.pending_previews:
            del self.pending_previews[preview_id]


def get_preview_service(workdir: str = "") -> PreviewService:
    """Get the global preview service instance."""
    return PreviewService.get_instance(workdir=workdir)
