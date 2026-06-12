# Fast-Path Stabilization Plan

Analysis and step-by-step plan for eliminating regression bugs in the async event-driven AI coding agent. Written for the Senior Python Systems Architect & Debugging Specialist role.

---

## Diagnostics Report

### 1. MessageBus Race Conditions & Event Ordering

**Highest-risk files:**
- `src/core/messaging/bus.py:125-143` — `_dispatch`: creates independent `run_in_executor` per event, no ordering guarantee
- `src/core/messaging/bus.py:231-274` — `_deliver_to_handlers`: runs in arbitrary executor thread
- `tui/src/ui/_bridge_subscriptions.py:151-167` — `_DictBridgeAdapter`: handler receives events with no sequential guarantee

**Bug pattern:** The `_dispatch` method spawns a new `asyncio.Task` per event, each submitting `run_in_executor` to a thread pool. When two events queue rapidly (e.g., `ToolExecuteStart` at `tool_execution_pipeline.py:965` then `ToolExecuteFinish` at `:841`), they become separate executor tasks that can **run in any order**. The TUI can receive `ToolExecuteFinish` before `ToolExecuteStart`, corrupting its tool-call display state.

No sequence number, no per-subscriber ordering queue, no cooperative yielding — the architecture assumes sequential delivery but provides none.

**Specific high-risk sequence:** `StepStart`→`StepFinish` events emitted at `execution_tool.py:63` and `:94` — both fire on the same executor thread, but become separate `_dispatch` tasks in the event loop. With 4 worker threads, background TUI events (`TokenBudget`, `LogEntry`) can interleave and reorder the visual step tracking.

---

### 2. Zombie Locks in PRSW (FileLockManager)

**Highest-risk files:**
- `src/core/orchestration/graph/nodes/execution_node.py:196-207` — `finally` block
- `src/core/orchestration/graph/nodes/execution_node.py:421-435` — `CancelledError` propagation path
- `src/core/orchestration/file_lock_manager.py:116-138` — polling loop
- `src/core/orchestration/file_lock_manager.py:50-59` — singleton never resets

**Zombie lock scenario A — Hanging executor thread:** `_execute_tool_with_locks` at `execution_node.py:143` acquires locks then delegates to `_dispatch_tool` at `:178`. For MEDIUM+ tiers, this uses `run_with_correlation(loop, None, orch.execute_tool, action)`. If the executor thread hangs (NFS stall, deadlock in `fcntl.flock`), the `await` never resolves, `finally` at `:196` never runs, and the write lock is **permanently held** for the process lifetime.

**Zombie lock scenario B — Singleton leak:** `FileLockManager.get_instance()` at `file_lock_manager.py:50` uses a class-level singleton. `orchestrator_bootstrap.py:134` creates a new `FileLockManager(workdir, cancel_event)` per initialization, but `__init__` on an already-initialized singleton does nothing — the old `_cancel_event` and lock state persist. TUI reconnection or hot-reload creates a fresh orchestrator with a stale lock manager that still holds old locks.

**Zombie lock scenario C — Cancel corruption:** `_execute_tool_with_locks` line 207 calls `lock_manager.reset_cancel()` unconditionally after every write-tool finally block. `reset_cancel()` at `:159` calls `self._cancel_event.clear()`. If a **different** coroutine set the cancel event to abort its own lock wait, this `clear()` silently un-cancels it, creating a livelock where the cancelled operation now spins forever.

---

### 3. Lazy Import & Circular Dependency Crashes

**Highest-risk files:**
- `src/core/messaging/__init__.py:140-146` — `__getattr__` re-export
- `src/core/orchestration/event_bus.py:182-389` — `_get_event_name_map()` — massive inline import
- `src/core/orchestration/event_bus.py:392-417` — `_build_typed_event` silent `None` returns
- `tui/src/ui/_bridge_subscriptions.py:28-92` — silent `except Exception: pass`

**Bug pattern:** `_get_event_name_map()` at `event_bus.py:188-284` does **97 eager imports** inside a function body. If any single event type class is renamed, removed, or has an import error, the function raises. Since it's guarded by `_EVENT_MAP_LOCK` and writes to global `_EVENT_NAME_TO_TYPED` (only on success), the **entire event mapping is lost** — all 90+ string-based events silently stop producing typed events. The TUI goes blind, logging only at `_logger.debug` level (`event_bus.py:416`).

The circular chain is:
```
messaging/__init__ ──→ bus.py ←── event_bus.py:get_typed_bus (lazy local import)
                    ──→ event_types.py ←── event_bus.py:41 (eager import)
                    ──→ __getattr__ ──→ event_bus.py:get_typed_bus
```

The `__getattr__` at `messaging/__init__.py:140` works only if `src.core.orchestration.event_bus` is already in `sys.modules`. If import order changes during refactoring (e.g., a new file imports from `messaging` before `event_bus` is loaded), `get_typed_bus()` becomes `AttributeError` at module import time.

---

## Fast-Path Isolation Plan

### Phase 0: Freeze Complex Nodes

**Action:** In `src/core/orchestration/graph/builder.py`, create a compile-time switch that **statically removes** the complex cognitive nodes from the graph. Do not touch their source code — they are frozen for inspection.

**Files to modify:**

| File | Change |
|------|--------|
| `builder.py:123-136` | Add `_USE_FULL_GRAPH = False` flag. When `False`, skip adding `analysis_node`, `planning_node`, `plan_validator_node`, `replan_node`, `delegation_node`, `analyst_delegation_node`, `wait_for_user_node`, `debug_node`, `step_controller_node` |
| `builder.py:146-155` | Rewire `perception` conditional edges: route `"analysis"` → `"execution"` (bypass), route `"planning"` → `"execution"` (bypass). Only `"execution"`, `"memory_sync"` remain |
| `builder.py:193-206` | Simplify `execution` routing: remove `"replan"`, `"analysis"`, `"wait_for_user"` routes. Only `"step_controller"` → redirect to `"verification"`, `"perception"`, `"memory_sync"` remain |
| `builder.py:254-263` | Evaluation: remove `"step_controller"`, `"debug"` routes. Only `"memory_sync"` and `"end"` remain |
| `builder.py:268-272` | Remove debug node entirely |
| `builder.py:274-278` | `memory_sync`: remove `"delegation"` route. Only `"perception"` and `"end"` remain |

**Resulting graph topology after freeze:**

```
perception ──→ execution ──→ verification ──→ evaluation ──→ memory_sync ──→ END
    ↑                                                              │
    └──────────────────────────────────────────────────────────────┘
```

6 nodes instead of 16. Pure Fast-Path.

---

### Phase 1: Fix MessageBus Ordering for Fast-Path Events

**Goal:** Guarantee that critical UI events (`StepStart`/`StepFinish`, `ToolExecuteStart`/`ToolExecuteFinish`) are delivered in publication order.

**Step 1.1 — Per-subscriber sequential dispatch queue**

Modify `bus.py:_dispatch` to accept a `sequenced=True` parameter. When `sequenced`, the task does not immediately run handlers. Instead, it appends to an **ordered per-event-type deque**, and a single sequential dispatcher coroutine processes them in FIFO order.

**Files to modify:**

| File | Change |
|------|--------|
| `bus.py:125-143` | Add `_dispatch_sequenced(item)` method. Uses `asyncio.Queue` per event type category (TOOL, STEP, SESSION, etc.). One consumer task per queue processes items sequentially via `run_in_executor` |
| `bus.py:115` | Bridge: for tool/step events, call `_dispatch_sequenced` instead of `_dispatch` |
| `bus.py:231-274` | No change needed — `_deliver_to_handlers` already thread-safe |

**Step 1.2 — Categorize event types**

**File:** `src/core/messaging/event_types.py` base class or `events.py`

Add a `sequenced: bool = False` field to `Event` base class. Set `sequenced=True` on:
- `ToolExecuteStart`, `ToolExecuteFinish`, `ToolExecuteError`
- `StepStart`, `StepFinish`
- `DelegationStart`, `DelegationFinish`

**Step 1.3 — Bridge handler for sequenced events**

**File:** `tui/src/ui/_bridge_subscriptions.py`

Not needed — the MessageBus guarantees per-category ordering from Step 1.1. The TUI bridge handler already receives events synchronously within `_deliver_to_handlers`.

---

### Phase 2: Fix Zombie Locks

**Step 2.1 — Add lock acquisition timeout and automatic release guard**

**File:** `src/core/orchestration/file_lock_manager.py`

Add a `_lock_release_guard` asyncio task per write lock acquisition. If the tool execution takes longer than the lock timeout (30s), the guard **force-releases** the lock and logs a critical error.

Add to `acquire_write_async` (after acquiring the lock):
```python
self._lock_release_guard = asyncio.create_task(
    self._auto_release_after_timeout(path, agent_id, timeout)
)
```

Add new method:
```python
async def _auto_release_after_timeout(self, path: str, agent_id: str, timeout: float):
    await asyncio.sleep(timeout)
    async with self._async_lock:
        if self._write_lock and self._write_lock.path == path:
            logger.critical("AUTO-RELEASING zombie write lock for %s (owner: %s)", path, agent_id)
            self._write_lock = None
```

**Step 2.2 — Fix singleton reset on orchestrator re-init**

**File:** `src/core/orchestration/file_lock_manager.py:50-59`

Add a `reset_instance()` classmethod that replaces the singleton:

```python
@classmethod
def reset_instance(cls, workdir: str = "", cancel_event: Optional[asyncio.Event] = None) -> "FileLockManager":
    with cls._instance_lock:
        old = cls._instance
        if old is not None:
            old.cancel()  # Signal all waiting acquires to abort
        cls._instance = cls(workdir=workdir or ".", cancel_event=cancel_event)
    if old is not None:
        old.shutdown(timeout=5.0)  # Let pending operations clean up
    return cls._instance
```

**File:** `src/core/orchestration/orchestrator_bootstrap.py:131-137`

Replace `FileLockManager(...)` with `FileLockManager.reset_instance(...)`.

**Step 2.3 — Fix `reset_cancel()` to be idempotent**

**File:** `src/core/orchestration/file_lock_manager.py:159-161`

Change to only clear when the cancel was set by *this* lock operation (track owner):

```python
def reset_cancel(self, owner: Optional[str] = None):
    if owner is None or owner == self._cancel_owner:
        self._cancel_event.clear()
        self._cancel_owner = None
```

Add `_cancel_owner: Optional[str] = None` to `__init__`.

**Step 2.4 — Add process-exit lock cleanup**

**File:** `src/core/orchestration/file_lock_manager.py`

Register an `atexit` handler that releases all held locks:

```python
import atexit

@classmethod
def _install_atexit_cleanup(cls):
    if not cls._atexit_installed:
        atexit.register(cls._release_all_locks)
        cls._atexit_installed = True
```

---

### Phase 3: Fortify Fast-Path Event Mapping

**Step 3.1 — Break the bulk import into a per-event-type lazy loader**

**File:** `src/core/orchestration/event_bus.py:182-389`

Replace the monolithic `_get_event_name_map()` with per-event-type lazy loading. Each call to `_build_typed_event` loads only its own event class via `importlib.import_module` + `getattr`. A failed import for `"tool.execute.start"` does not break `"session.created"`.

**Step 3.2 — Add `_build_typed_event` failure telemetry**

**File:** `src/core/orchestration/event_bus.py:413-416`

Change `_logger.debug` to `_logger.warning` when `cls(**mapped)` fails, including the event class name and the mapped fields. This makes silent drops audible in normal log output.

**Step 3.3 — Add bridge subscription health-check**

**File:** `tui/src/ui/_bridge_subscriptions.py:28-92`

Replace the blanket `except Exception: pass` with a per-symbol try/except that logs each failure at `WARNING` level:

```python
_IMPORT_FAILURES = []
try:
    from src.core.messaging import AgentMessage
except Exception as e:
    _IMPORT_FAILURES.append(("AgentMessage", e))

if _IMPORT_FAILURES:
    logger.warning("Bridge: %d event types failed to import", len(_IMPORT_FAILURES))
    for name, exc in _IMPORT_FAILURES:
        logger.warning("Bridge: %s import failed: %s", name, exc)
```

---

### Phase 4: Contract Tests for Fast-Path

**Step 4.1 — Lock-release contract test**

New test file `tests/unit/test_fast_path_locks.py`:

| Test | What it verifies |
|------|-----------------|
| `test_write_lock_released_on_hanging_tool_executor` | Timeout guard releases lock when tool hangs |
| `test_write_lock_released_on_cancelled_error` | `CancelledError` propagation still fires `finally` |
| `test_singleton_reset_clears_old_locks` | `reset_instance()` abandons old lock state |
| `test_concurrent_cancel_reset_no_race` | `cancel_owner` prevents cross-coroutine cancel clearing |

**Step 4.2 — Event ordering contract test**

New test file `tests/unit/test_fast_path_event_ordering.py`:

| Test | What it verifies |
|------|-----------------|
| `test_tool_events_delivered_in_order` | Publish TStart, TFinish → handler sees TStart then TFinish |
| `test_step_events_delivered_in_order` | Publish SStart, SFinish → handler sees SStart then SFinish |
| `test_interleaved_categories_no_block` | Tool events and UI events don't block each other |

---

### Execution Order

```
Phase 0 → Freeze graph (builder.py)                    [1 file, ~20 lines changed]
  ↓
Phase 1 → Bus ordering (bus.py, event_types.py)        [2 files, ~60 lines changed]
  ↓
Phase 2 → Lock hardening (file_lock_manager.py,        [3 files, ~80 lines changed]
           orchestrator_bootstrap.py)
  ↓
Phase 3 → Import safety (event_bus.py,                 [3 files, ~100 lines changed]
           _bridge_subscriptions.py)
  ↓
Phase 4 → Contract tests                               [2 new files, ~200 lines of tests]
  ↓
Run Fast-Path integration test → Verify functional
  ↓
Gradually re-enable frozen nodes one-at-a-time:
  perception → execution → verification → evaluation → memory_sync ✓
  └→ step_controller       (Phase 5a)
  └→ debug                 (Phase 5b)
  └→ analysis              (Phase 5c)
  └→ planning              (Phase 5d)
  └→ plan_validator        (Phase 5e)
  └→ replan                (Phase 5f)
  └→ wait_for_user         (Phase 5g)
  └→ delegation            (Phase 5h)
```

Each re-enabled node must pass the event-ordering and lock-release contract tests from Phase 4 before the next node is re-enabled.
