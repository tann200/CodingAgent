# TUI & EventBus Audit Report

**Date:** 2026-03-26
**Scope:** `src/ui/app.py`, `src/ui/textual_app_impl.py`, `src/ui/views/main_view.py`, `src/core/orchestration/event_bus.py`, `src/core/orchestration/orchestrator.py`
**Objective:** Identify gaps, concurrency bugs, and UI/core coupling violations
**Result:** 9 findings — all resolved. Test suite: **1502 passed, 4 skipped, 0 failed**

---

## Summary of Findings

| ID | Severity | Component | Title | Status |
|----|----------|-----------|-------|--------|
| EB-1 | CRITICAL | `app.py` / `event_bus.py` | EventBus singleton split — TUI and orchestrator on different buses | FIXED |
| EB-2 | CRITICAL | `textual_app_impl.py` | Lock-free `_agent_running = True` in `on_input_submitted` | FIXED |
| EB-3 | HIGH | `textual_app_impl.py` | Lock-free `_agent_running = False` in `on_agent_result` | FIXED |
| EB-4 | HIGH | `textual_app_impl.py` | Lock-free `_agent_running = False` in interrupt handlers | FIXED |
| EB-5 | HIGH | `textual_app_impl.py` | Blocking file I/O in EventBus callback `_on_session_new` | FIXED |
| EB-6 | HIGH | `event_bus.py` | Silent exception swallow in `EventBus.publish()` | FIXED |
| EB-7 | MEDIUM | `main_view.py` | `MainViewController` has no `cleanup()` — 18 subscriptions never unsubscribed | FIXED |
| EB-8 | MEDIUM | `textual_app_impl.py` / `orchestrator.py` | TUI mutates orchestrator private fields directly | FIXED |
| EB-9 | LOW | `textual_app_impl.py` | Redundant `_session_read_files = set()` after `start_new_task()` | FIXED |

---

## Detailed Findings and Fixes

### EB-1 — EventBus Singleton Split (CRITICAL)

**File:** `src/ui/app.py:47`, `src/core/orchestration/event_bus.py`

**Description:**
`CodingAgentApp.__init__` creates `self.event_bus = EventBus()` and wires it to `ProviderManager`. The orchestrator picks up this bus and publishes all events to it.

Meanwhile, `textual_app_impl.py` uses `get_event_bus()` to subscribe its UI handlers. `get_event_bus()` lazily creates a module-level `_default_bus` singleton — a **completely separate** `EventBus` instance. Result: orchestrator events arrive on bus A, TUI handlers are on bus B. Every orchestrator event is silently dropped by the TUI.

**Root cause:** No code path in `CodingAgentApp.__init__` ever sets `_default_bus` to point at the application bus.

**Fix:** After creating `self.event_bus`, assign it to the module-level singleton:

```python
# src/ui/app.py
import src.core.orchestration.event_bus as _event_bus_module
...
self.event_bus = EventBus()
# Wire get_event_bus() singleton to this instance so TUI handlers and
# the orchestrator share the same bus (singleton split fix).
_event_bus_module._default_bus = self.event_bus
```

---

### EB-2 — Lock-free `_agent_running = True` in `on_input_submitted` (CRITICAL)

**File:** `src/ui/textual_app_impl.py:971`

**Description:**
The base class `send_prompt()` correctly acquires `_agent_lock` before setting `_agent_running = True`. The Textual subclass `on_input_submitted()` sets `self._agent_running = True` at line 971 **without the lock**. If `send_prompt()` is called concurrently (e.g. from a test harness or a race between input submission and a background callback), both code paths can enter `_run_agent` simultaneously, spawning two agent threads for the same task.

**Fix:** Acquire the lock and guard against re-entry:

```python
with self._agent_lock:
    if self._agent_running:
        guilogger.warning(
            "on_input_submitted: agent already running, ignoring duplicate submission"
        )
        return
    self._agent_running = True
```

---

### EB-3 — Lock-free `_agent_running = False` in `on_agent_result` (HIGH)

**File:** `src/ui/textual_app_impl.py:1001`

**Description:**
`on_agent_result()` (called from the background agent thread via `call_from_thread`) wrote `self._agent_running = False` without `_agent_lock`. This is both racy and redundant — the `finally` block in `_run_agent` is the authoritative place to clear the flag.

**Fix:** Remove the redundant assignment. The `finally` block in `_run_agent` handles the flag correctly under the lock.

---

### EB-4 — Lock-free `_agent_running = False` in Interrupt Handlers (HIGH)

**File:** `src/ui/textual_app_impl.py:1681, 1693`

**Description:**
`action_interrupt_agent()` (line 1681) and `action_force_interrupt_agent()` (line 1693) both write `self._agent_running = False` without acquiring `_agent_lock`. These handlers are invoked on the Textual main thread while `_run_agent` runs on a daemon thread, creating a TOCTOU window.

**Fix:** Wrap both assignments:

```python
# action_interrupt_agent
with self._agent_lock:
    self._agent_running = False

# action_force_interrupt_agent
with self._agent_lock:
    running = self._agent_running
    if running:
        self._agent_running = False
```

---

### EB-5 — Blocking File I/O in EventBus Callback (HIGH)

**File:** `src/ui/textual_app_impl.py:1261-1263` (`_on_session_new`)

**Description:**
The `session.new` event handler `_on_session_new()` calls `task_state_path.write_text(...)` synchronously. EventBus delivers callbacks **on the publisher's thread** — in this case the agent or UI thread. A slow or contended filesystem write blocks that thread for the duration, stalling either the agent loop or UI event processing.

**Fix:** Offload the write to a daemon thread:

```python
def _write_task_state(p=task_state_path):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Current Task\n\n# Completed Steps\n\n# Next Step\n")
    except Exception:
        pass
threading.Thread(target=_write_task_state, daemon=True).start()
```

---

### EB-6 — Silent Exception Swallow in `EventBus.publish()` (HIGH)

**File:** `src/core/orchestration/event_bus.py:116`

**Description:**
The `publish()` loop catches every subscriber exception and calls `continue` with no logging whatsoever. A buggy subscriber silently fails with no trace in any log, making it extremely difficult to diagnose TUI state divergence or missing updates.

**Fix:** Log at DEBUG before continuing:

```python
except Exception as _exc:
    _logger.debug(
        "EventBus: subscriber %r raised on event %r: %s",
        cb, event_name, _exc,
    )
    continue
```

---

### EB-7 — `MainViewController` Subscriptions Never Unsubscribed (MEDIUM)

**File:** `src/ui/views/main_view.py:52-76`

**Description:**
`MainViewController.__init__` registers 18 event handlers via `event_bus.subscribe()`. There is no `cleanup()` or `close()` method. In tests or multi-session flows where a controller is replaced, all 18 callbacks remain registered on the bus, leaking memory and potentially delivering events to a stale controller.

**Fix:** Track subscriptions and expose `cleanup()`:

```python
def _subscribe(self, event_name: str, callback) -> None:
    self.event_bus.subscribe(event_name, callback)
    self._subscriptions.append((event_name, callback))

def cleanup(self) -> None:
    for event_name, callback in self._subscriptions:
        self.event_bus.unsubscribe(event_name, callback)
    self._subscriptions.clear()
```

---

### EB-8 — TUI Mutates Orchestrator Private Fields Directly (MEDIUM)

**File:** `src/ui/textual_app_impl.py:1729-1748` (`_restore_state_for_continue`)

**Description:**
The TUI's `_restore_state_for_continue` wrote directly to `orchestrator.msg_mgr.messages`, `orchestrator._session_read_files`, and `orchestrator._last_agent_state` — all private implementation details. This couples the UI to the orchestrator's internal structure and bypasses any invariants the orchestrator may enforce when these fields change.

**Fix:** Add a `restore_continue_state(state: dict)` public method to `Orchestrator` that encapsulates the restoration logic. The TUI delegates:

```python
# textual_app_impl.py
def _restore_state_for_continue(self) -> bool:
    if not self._continue_state:
        return False
    try:
        if self.orchestrator and hasattr(self.orchestrator, "restore_continue_state"):
            self.orchestrator.restore_continue_state(self._continue_state)
            return True
    except Exception as e:
        guilogger.error(f"Failed to restore state: {e}")
    return False
```

```python
# orchestrator.py
def restore_continue_state(self, state: dict) -> None:
    """Restore saved conversation state for the /continue workflow."""
    if hasattr(self, "msg_mgr") and "history" in state:
        if state["history"] and hasattr(self.msg_mgr, "messages"):
            self.msg_mgr.messages = list(state["history"])
    if "session_read_files" in state:
        self._session_read_files = set(state["session_read_files"])
    last_state = getattr(self, "_last_agent_state", None) or {}
    for key in ("current_plan", "current_step", "working_dir", "step_retry_counts"):
        val = state.get(key)
        if val is not None:
            last_state[key] = val
    self._last_agent_state = last_state
```

---

### EB-9 — Redundant `_session_read_files` Clear in `_do_new_session` (LOW)

**File:** `src/ui/textual_app_impl.py:1792-1793`

**Description:**
`_do_new_session` called `orch._session_read_files = set()` immediately after calling `orch.start_new_task()`. `start_new_task()` already resets `_session_read_files` (and all other per-task state) at line 2429 of `orchestrator.py`. The redundant assignment is dead code that also bypasses the method boundary.

**Fix:** Remove the redundant line; rely on `start_new_task()` as the single point of reset.

---

## Architecture Validation: UI/Core Separation

The following concerns were verified and found acceptable:

- **`session_lifecycle` imported lazily inside `_load_available_snapshots`** — acceptable; imports happen at method call time, not at class construction.
- **`plan_mode.py` / `preview_service.py`** — clean implementations with no TUI coupling.
- **`on_unmount` subscription cleanup (H7 pattern)** — correctly implemented in the main subscription block at lines 572-596 of `textual_app_impl.py`.

After fix EB-8, the remaining cases of orchestrator access from the TUI all go through public methods (`start_new_task`, `run_agent_once`, `cancel`, `restore_continue_state`). The separation boundary is now enforced.

---

## Test Results

```
1502 passed, 4 skipped, 0 failed
```

Prior baseline: **1459 passed** (vol9 final)
Delta: +43 tests (all pre-existing, not new regressions — count increase is from other accumulated tests in the session)
