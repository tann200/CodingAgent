from __future__ import annotations
from src.ui.bus import StatusUpdate
from src.ui.logging import get_logger

logger = get_logger("controller")


class WorkflowController:
    def __init__(self, app):
        self.app = app
        self._running_tasks: set = set()

    def start_agent_worker(self, user_prompt: str):
        logger.info(f"Agent worker started: {user_prompt[:80]}")
        self.app.post_message(StatusUpdate(message=f"Processing: {user_prompt[:60]}..."))

    def cancel_all(self) -> None:
        for task in self._running_tasks:
            if not task.done():
                task.cancel()
        cancelled = len(self._running_tasks)
        self._running_tasks.clear()
        if cancelled:
            logger.info(f"Cancelled {cancelled} running tasks")
