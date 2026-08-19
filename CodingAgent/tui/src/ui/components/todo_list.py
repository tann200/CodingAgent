"""todo_list.py — TASK-TUI-4

Rendered todo list widget for ``manage_todo`` tool calls.

Renders each item with a status icon:
  ○ pending
  ● in_progress  (bold)
  ✓ completed    (muted)
  ✗ cancelled    (strikethrough)

Usage::

    widget = TodoListWidget(items=[
        {"id": "1", "description": "Write tests", "status": "pending"},
        {"id": "2", "description": "Run CI",      "status": "completed"},
    ])
    # Later, update in place:
    widget.update_items(new_items)
"""

from __future__ import annotations

from typing import Any, Dict, List

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Label, Static

# Status → (icon, Rich markup style)
_STATUS_STYLES: Dict[str, tuple[str, str]] = {
    "pending":     ("o", ""),
    "in_progress": ("@", "bold"),
    "completed":   ("v", "dim"),
    "cancelled":   ("x", "strike dim"),
}

_DEFAULT_STYLE = ("?", "")


class TodoListWidget(Widget):
    """Renders a list of todo items with status icons."""

    DEFAULT_CSS = """
    TodoListWidget {
        padding: 0 1;
        margin-bottom: 1;
    }
    TodoListWidget > .todo-header {
        color: #a78bfa;
        text-style: bold;
    }
    TodoListWidget > .todo-item {
        color: #d4d4d8;
    }
    """

    def __init__(
        self,
        items: List[Dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._items: List[Dict[str, Any]] = items or []
        self.session_id = session_id

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Label("Todos", classes="todo-header")
        for item in self._items:
            yield Static(self._render_item(item), classes="todo-item", markup=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_items(self, items: List[Dict[str, Any]]) -> None:
        """Replace the current item list and re-render."""
        self._items = list(items)
        if self.is_mounted:
            self._rebuild()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_item(item: Dict[str, Any]) -> str:
        status = str(item.get("status", "pending")).lower()
        icon, style = _STATUS_STYLES.get(status, _DEFAULT_STYLE)
        desc = str(item.get("description", "")).strip()
        if style:
            return f"[{style}]{icon} {desc}[/]"
        return f"{icon} {desc}"

    def _rebuild(self) -> None:
        """Remove and re-mount all item widgets."""
        for w in self.query(".todo-item"):
            w.remove()
        for item in self._items:
            self.mount(Static(self._render_item(item), classes="todo-item", markup=True))

    # ------------------------------------------------------------------
    # Read-only accessors (for tests)
    # ------------------------------------------------------------------

    @property
    def items(self) -> List[Dict[str, Any]]:
        return list(self._items)

    def items_by_status(self, status: str) -> List[Dict[str, Any]]:
        return [i for i in self._items if i.get("status") == status]
