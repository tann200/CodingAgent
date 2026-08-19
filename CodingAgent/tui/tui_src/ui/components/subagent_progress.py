from __future__ import annotations

from textual.message import Message
from textual.widgets import Static


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class SubagentProgress(Static):
    """Animated spinner for a running subagent.

    Shows: ⠦ <role> — <task preview>   (while running)
    Shows: ✓/✗ <role> — <task preview>  (when finished; click to view session)

    Clicking the widget (once finished) posts SubagentProgress.Clicked so the
    app can open a detail screen for the child session.
    """

    class Clicked(Message):
        """User clicked a finished subagent widget — open its session detail."""

        def __init__(self, child_session_id: str, role: str, task: str) -> None:
            self.child_session_id = child_session_id
            self.role = role
            self.task = task
            super().__init__()

    DEFAULT_CSS = """
    SubagentProgress {
        color: #a78bfa;
    }
    SubagentProgress.finished_ok {
        color: #22c55e;
    }
    SubagentProgress.finished_err {
        color: #ff5555;
    }
    SubagentProgress.finished_ok:hover,
    SubagentProgress.finished_err:hover {
        text-style: underline;
    }
    """

    def __init__(self, role: str, task: str, child_session_id: str = "") -> None:
        self._role = role
        self._task_preview = task[:80].replace("\n", " ")
        self._child_session_id = child_session_id
        self._frame_idx = 0
        self._finished = False
        super().__init__(
            self._render_frame(),
            classes="tool_msg subagent_msg",
            markup=True,
        )

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick)

    def _render_frame(self) -> str:
        frame = _FRAMES[self._frame_idx % len(_FRAMES)]
        return f"[bold #a78bfa]{frame} {self._role}[/] — {self._task_preview}"

    def _tick(self) -> None:
        if self._finished:
            return
        self._frame_idx += 1
        self.update(self._render_frame())

    def finish(self, ok: bool) -> None:
        """Stop the spinner and display final status."""
        self._finished = True
        try:
            self._timer.stop()
        except Exception:
            pass
        icon = "✓" if ok else "✗"
        color = "#22c55e" if ok else "#ff5555"
        css_class = "finished_ok" if ok else "finished_err"
        self.remove_class("subagent_msg")
        self.add_class(css_class)
        hint = "  [dim](click to view)[/]" if self._child_session_id else ""
        self.update(
            f"[bold {color}]{icon} {self._role}[/] — {self._task_preview}{hint}"
        )

    def on_click(self) -> None:
        """Post Clicked message when the user clicks a finished subagent widget."""
        if self._finished and self._child_session_id:
            self.post_message(
                self.Clicked(
                    child_session_id=self._child_session_id,
                    role=self._role,
                    task=self._task_preview,
                )
            )
