"""tests/unit/test_todo_list_widget.py — TASK-TUI-4

Tests for tui/src/ui/components/todo_list.py — TodoListWidget.
Verifies item rendering for all four statuses, update flow, and filtering.
"""

from __future__ import annotations

import pytest

from tui.src.ui.components.todo_list import TodoListWidget, _STATUS_STYLES


# ---------------------------------------------------------------------------
# Status icons present in _STATUS_STYLES
# ---------------------------------------------------------------------------


def test_status_styles_cover_all_states() -> None:
    for status in ("pending", "in_progress", "completed", "cancelled"):
        assert status in _STATUS_STYLES


# ---------------------------------------------------------------------------
# _render_item: correct icons per status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,expected_icon", [
    ("pending",     "o"),
    ("in_progress", "@"),
    ("completed",   "v"),
    ("cancelled",   "x"),
])
def test_render_item_icon(status: str, expected_icon: str) -> None:
    item = {"description": "Do something", "status": status}
    rendered = TodoListWidget._render_item(item)
    assert expected_icon in rendered


def test_render_item_includes_description() -> None:
    item = {"description": "Write unit tests", "status": "pending"}
    rendered = TodoListWidget._render_item(item)
    assert "Write unit tests" in rendered


def test_render_item_in_progress_is_bold() -> None:
    item = {"description": "Active task", "status": "in_progress"}
    rendered = TodoListWidget._render_item(item)
    assert "bold" in rendered


def test_render_item_completed_is_dim() -> None:
    item = {"description": "Done task", "status": "completed"}
    rendered = TodoListWidget._render_item(item)
    assert "dim" in rendered


def test_render_item_cancelled_is_strike() -> None:
    item = {"description": "Dropped task", "status": "cancelled"}
    rendered = TodoListWidget._render_item(item)
    assert "strike" in rendered


def test_render_item_unknown_status_falls_back() -> None:
    item = {"description": "Mystery", "status": "weird_status"}
    rendered = TodoListWidget._render_item(item)
    assert "Mystery" in rendered


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_widget_initial_items() -> None:
    items = [
        {"id": "1", "description": "Task A", "status": "pending"},
        {"id": "2", "description": "Task B", "status": "completed"},
    ]
    widget = TodoListWidget(items=items)
    assert len(widget.items) == 2


def test_widget_empty_construction() -> None:
    widget = TodoListWidget()
    assert widget.items == []


def test_widget_session_id_stored() -> None:
    widget = TodoListWidget(session_id="sess-123")
    assert widget.session_id == "sess-123"


# ---------------------------------------------------------------------------
# update_items: replace and query
# ---------------------------------------------------------------------------


def test_update_items_replaces_list() -> None:
    widget = TodoListWidget(items=[{"description": "Old", "status": "pending"}])
    new_items = [
        {"description": "New A", "status": "in_progress"},
        {"description": "New B", "status": "completed"},
    ]
    widget.update_items(new_items)
    assert len(widget.items) == 2
    assert widget.items[0]["description"] == "New A"


def test_update_items_sequential_events() -> None:
    """Two sequential manage_todo events → widget shows latest state only."""
    widget = TodoListWidget()
    first = [{"description": "Step 1", "status": "pending"}]
    second = [
        {"description": "Step 1", "status": "completed"},
        {"description": "Step 2", "status": "in_progress"},
    ]
    widget.update_items(first)
    widget.update_items(second)
    assert len(widget.items) == 2
    assert widget.items[0]["status"] == "completed"
    assert widget.items[1]["status"] == "in_progress"


def test_update_items_empty_clears_list() -> None:
    widget = TodoListWidget(items=[{"description": "x", "status": "pending"}])
    widget.update_items([])
    assert widget.items == []


# ---------------------------------------------------------------------------
# items_by_status filtering
# ---------------------------------------------------------------------------


def test_items_by_status_pending() -> None:
    widget = TodoListWidget(items=[
        {"description": "A", "status": "pending"},
        {"description": "B", "status": "completed"},
        {"description": "C", "status": "pending"},
    ])
    pending = widget.items_by_status("pending")
    assert len(pending) == 2
    assert all(i["status"] == "pending" for i in pending)


def test_items_by_status_empty_when_none_match() -> None:
    widget = TodoListWidget(items=[{"description": "A", "status": "pending"}])
    assert widget.items_by_status("in_progress") == []


# ---------------------------------------------------------------------------
# items property returns a copy
# ---------------------------------------------------------------------------


def test_items_property_returns_copy() -> None:
    orig = [{"description": "A", "status": "pending"}]
    widget = TodoListWidget(items=orig)
    copy = widget.items
    copy.append({"description": "B", "status": "pending"})
    assert len(widget.items) == 1


# ---------------------------------------------------------------------------
# All four status icons render in a single widget
# ---------------------------------------------------------------------------


def test_all_four_statuses_render() -> None:
    items = [
        {"description": "Pending task",    "status": "pending"},
        {"description": "Active task",     "status": "in_progress"},
        {"description": "Completed task",  "status": "completed"},
        {"description": "Cancelled task",  "status": "cancelled"},
    ]
    widget = TodoListWidget(items=items)
    rendered = [TodoListWidget._render_item(i) for i in items]
    # Each rendered string must contain the description
    for item, text in zip(items, rendered):
        assert item["description"] in text
