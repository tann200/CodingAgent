# Subagent TUI Visibility — Implementation Plan

**Goal:** Make the TUI aware of subagent spawning and completion, mirroring the
OpenCode `⠦ Explore Task` / `└ 42 toolcalls · 1m 23s` UX pattern.

**Background:** CodingAgent's `delegate_task` (`src/tools/subagent_tools.py`) spawns
child agents in a background thread.  The TUI (`tui/src/ui/`) is completely blind
to these events: `tui/src/ui/core_bridge.py:setup_subscriptions` (lines 221–292)
subscribes to zero delegation-related topics, and no `delegation.start` event is
published anywhere.

---

## Task List

### SUBAGENT-VIS-1 — Publish `delegation.start` / `delegation.finish` events

**File:** `src/tools/subagent_tools.py`

Publish two new EventBus topics so downstream subscribers (TUI, tests) can react
to the full lifecycle of each subagent spawn.

**Changes:**

1. After `_manifest_path.write_text(...)` (line 512, inside the manifest try/except
   block), add a `delegation.start` publish:

   ```python
   # Notify TUI that a subagent is starting
   try:
       if parent_orchestrator is not None:
           _pbus = getattr(parent_orchestrator, "event_bus", None)
           if _pbus is not None:
               _pbus.publish(
                   "delegation.start",
                   {
                       "child_session_id": child_session_id,
                       "parent_session_id": parent_session_id,
                       "role": canonical_role,
                       "task": subtask_description[:120],
                   },
               )
   except Exception:
       pass
   ```

2. After manifest update to `"completed"` (line ~542, after `_manifest_path.write_text`):

   ```python
   # Notify TUI that subagent finished successfully
   try:
       if parent_orchestrator is not None:
           _pbus = getattr(parent_orchestrator, "event_bus", None)
           if _pbus is not None:
               _pbus.publish(
                   "delegation.finish",
                   {
                       "child_session_id": child_session_id,
                       "role": canonical_role,
                       "ok": True,
                   },
               )
   except Exception:
       pass
   ```

3. After manifest update to `"failed"` (line ~558, inside `except _subagent_err`):

   ```python
   # Notify TUI that subagent failed
   try:
       if parent_orchestrator is not None:
           _pbus = getattr(parent_orchestrator, "event_bus", None)
           if _pbus is not None:
               _pbus.publish(
                   "delegation.finish",
                   {
                       "child_session_id": child_session_id,
                       "role": canonical_role,
                       "ok": False,
                   },
               )
   except Exception:
       pass
   ```

**Why `parent_orchestrator.event_bus`?** The parent_orchestrator is already
available at this scope (line 352 via `_PARENT_ORCHESTRATOR_VAR.get(None)`), and
`event_bus` is the established pattern used throughout `delegation_node.py` (line
388) and `inference_loop.py` (line 148).

---

### SUBAGENT-VIS-2 — Add `SubagentStartEvent` / `SubagentFinishEvent` to bus

**File:** `tui/src/ui/bus.py`

Add two new Textual `Message` subclasses after `AgentRunningEvent` (line 355).

```python
class SubagentStartEvent(Message):
    """delegation.start — a subagent was spawned."""

    def __init__(
        self,
        child_session_id: str,
        role: str,
        task: str,
        parent_session_id: str | None = None,
    ) -> None:
        self.child_session_id = child_session_id
        self.role = role
        self.task = task
        self.parent_session_id = parent_session_id
        super().__init__()


class SubagentFinishEvent(Message):
    """delegation.finish — a subagent completed or failed."""

    def __init__(self, child_session_id: str, role: str, ok: bool) -> None:
        self.child_session_id = child_session_id
        self.role = role
        self.ok = ok
        super().__init__()
```

---

### SUBAGENT-VIS-3 — Wire bridge in `core_bridge.py`

**File:** `tui/src/ui/core_bridge.py`

Three sub-changes:

**3a.** Add to `_EVENT_MAP` (after line 68):

```python
"delegation.start": "delegation.start",
"delegation.finish": "delegation.finish",
```

**3b.** Add subscriptions after line 292 (after the `agent.message` subscription):

```python
self._subscribe("delegation.start", self._on_delegation_start)
self._subscribe("delegation.finish", self._on_delegation_finish)
```

**3c.** Add handler methods after `_on_tool_error` (after line 729):

```python
def _on_delegation_start(self, payload: dict) -> None:
    from src.ui.bus import SubagentStartEvent

    self._post(
        SubagentStartEvent(
            child_session_id=payload.get("child_session_id", ""),
            role=payload.get("role", "unknown"),
            task=payload.get("task", ""),
            parent_session_id=payload.get("parent_session_id"),
        )
    )

def _on_delegation_finish(self, payload: dict) -> None:
    from src.ui.bus import SubagentFinishEvent

    self._post(
        SubagentFinishEvent(
            child_session_id=payload.get("child_session_id", ""),
            role=payload.get("role", "unknown"),
            ok=payload.get("ok", True),
        )
    )
```

---

### SUBAGENT-VIS-4 — Add sidebar section and event handlers in `app.py`

**File:** `tui/src/ui/app.py`

**4a.** Add `_subagent_widgets` dict to `__init__` alongside `_tool_widgets` (line 301):

```python
self._subagent_widgets: dict[str, "SubagentProgress"] = {}
```

**4b.** Add sidebar section after `LAST TOOL` / `#sb_tool_activity` (after line 365):

```python
# ── Active subagents ───────────────────────────────────────────
yield Label("SUBAGENTS", classes="sb_title")
yield Static("none", id="sb_subagent_status")
```

**4c.** Add event handlers after `handle_tool_error` (after line 1094):

```python
@on(SubagentStartEvent)
def handle_subagent_start(self, event: SubagentStartEvent) -> None:
    logger.info(f"Subagent start: {event.role}  id={event.child_session_id}")
    widget = SubagentProgress(event.role, event.task)
    self._subagent_widgets[event.child_session_id] = widget
    try:
        active = len(self._subagent_widgets)
        self.query_one("#sb_subagent_status", Static).update(
            f"{active} running"
        )
    except Exception:
        pass
    self._sched_chat_widget(widget)

@on(SubagentFinishEvent)
def handle_subagent_finish(self, event: SubagentFinishEvent) -> None:
    logger.info(f"Subagent finish: {event.role}  ok={event.ok}  id={event.child_session_id}")
    widget = self._subagent_widgets.pop(event.child_session_id, None)
    if widget is not None:
        self.call_later(lambda w=widget, ok=event.ok: w.finish(ok))
    try:
        active = len(self._subagent_widgets)
        label = f"{active} running" if active else "none"
        self.query_one("#sb_subagent_status", Static).update(label)
    except Exception:
        pass
```

**4d.** Add `SubagentStartEvent`, `SubagentFinishEvent` to the `from .bus import (` block (after line 80).

**4e.** Add `SubagentProgress` to the `from .components import (` block (after line 33).

---

### SUBAGENT-VIS-5 — Create `SubagentProgress` spinner component

**File:** `tui/src/ui/components/subagent_progress.py` *(new file)*

Animated spinner widget inspired by OpenCode's `⠦ Explore Task — description`
inline spinner:

```python
from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static


_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class SubagentProgress(Static):
    """Animated spinner for a running subagent.

    Shows: ⠦ <role> — <task preview>
    Stops: ✓/✗ <role> — <task preview>
    """

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
    """

    def __init__(self, role: str, task: str) -> None:
        self._role = role
        self._task_preview = task[:80].replace("\n", " ")
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
        return (
            f"[bold #a78bfa]{frame} {self._role}[/] — {self._task_preview}"
        )

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
        self.update(
            f"[bold {color}]{icon} {self._role}[/] — {self._task_preview}"
        )
```

**File:** `tui/src/ui/components/__init__.py`

Add export:

```python
from .subagent_progress import SubagentProgress
# and add "SubagentProgress" to __all__
```

---

### SUBAGENT-VIS-6 — CSS rules and final imports

**File:** `tui/src/ui/styles/app.tcss`

Add CSS for the new sidebar section and subagent messages:

```css
#sb_subagent_status {
    color: #a78bfa;
}

.subagent_msg {
    color: #a78bfa;
    padding: 0 1;
}
```

---

## Test Plan

After all 6 tasks:

1. `python -m pytest tests/unit --timeout=10` — zero regressions
2. Manual smoke test: run a task that triggers `delegate_task`; verify the TUI
   sidebar shows `SUBAGENTS  1 running` and the chat log shows the animated spinner,
   then switches to `✓ <role>` on completion.

---

## File Map

| File | Change |
|------|--------|
| `src/tools/subagent_tools.py` | Publish `delegation.start` (line ~512) and `delegation.finish` (lines ~542, ~558) |
| `tui/src/ui/bus.py` | Add `SubagentStartEvent`, `SubagentFinishEvent` (after line 355) |
| `tui/src/ui/core_bridge.py` | `_EVENT_MAP` entries (line 68), subscriptions (after 292), handlers (after 729) |
| `tui/src/ui/app.py` | `_subagent_widgets` dict (line 301), sidebar section (after 365), event handlers (after 1094), bus/component imports |
| `tui/src/ui/components/subagent_progress.py` | New file — `SubagentProgress` spinner |
| `tui/src/ui/components/__init__.py` | Export `SubagentProgress` |
| `tui/src/ui/styles/app.tcss` | `#sb_subagent_status` and `.subagent_msg` CSS rules |
