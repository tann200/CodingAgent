# Codebase Fragility Analysis

## Overview

Analysis of `src/` (core, inference, tools, memory, orchestration) and `tui/src/ui/` layers. Found **~1,200 bare `except Exception`** patterns, **5 critical data-integrity risks**, **3 deadlock-prone lock patterns**, and **2 nested event-loop hazards**.

---

## Priority 1: Critical Data Integrity Risks

### FRAG-1: `revert_session` TOCTOU — Can truncate wrong file or crash

**File:** `src/core/memory/jsonl_session_store.py:376-399`

```python
# Line 376: check OUTSIDE the lock
if not target_file.exists():
    return

with self._session_lock(session_id):
    with target_file.open("r+b") as f:  # Line 385: open OUTSIDE the check
        f.truncate(target_offset)       # Line 386: can race with delete/create
```

**Deep Analysis:**

The window between `target_file.exists()` (line 376) and `target_file.open("r+b")` (line 385) is the TOCTOU race. During this window:
1. Another process could DELETE `target_file` and CREATE a NEW file at the same path
2. `open("r+b")` would open the NEW file and `truncate()` would zero it out
3. Or the file could be deleted BETWEEN check and open, causing `FileNotFoundError`

The `active` file check at line 391-398 has a similar issue: `active.resolve() != target_file.resolve()` checks resolved paths, but the subsequent `active.open("w")` at line 398 happens on a path that could have changed.

**Fix Plan:**

```python
with self._session_lock(session_id):
    try:
        with target_file.open("r+b") as f:
            f.truncate(target_offset)
    except FileNotFoundError:
        logger.debug("revert_session: target file gone, no-op")
        return
    # Similar for active file handling
```

---

### FRAG-2: Snapshot save silently succeeds even if transaction fails

**File:** `src/core/memory/sqlite_store_collaborators.py:440-453`

```python
wconn.execute("INSERT INTO session_snapshot_rows ...")
wconn.commit()           # Line 447 — commit can fail silently
except Exception:
    try:
        wconn.rollback()
    except Exception:
        pass
    return None          # Returns None, caller sees "failed" — correct
```

But INSIDE the insertion loop at lines 440-444:

```python
for tbl, serialised in data.items():
    wconn.execute("INSERT INTO session_snapshot_rows ...")
    # ^ If this raises, loop aborts. Commit at 447 runs.
    # But if commit() itself fails, rollback at 449-452 runs but
    # the function returns None (correct). However, there's no
    # logging of WHAT failed.
```

**Deep Analysis:**

The `None` return correctly signals failure to the caller. But:
1. **No logging** of WHY it failed (commit? insert?)
2. **BEGIN/COMMIT transaction** at the outer level (around line 436-448) uses implicit transaction. If `wconn.commit()` fails, the function returns `None` but the `except` at 448 catches ALL exceptions including `sqlite3.IntegrityError` (duplicate snap_id) which is a **logic bug, not a transient error**.

The deeper issue: the code uses implicit transactions. If `wconn.commit()` raises `sqlite3.OperationalError` (disk full, permission denied), the data is NOT durable but the caller has no way to distinguish "disk full" from "duplicate snap_id".

**Fix Plan:**

```python
except Exception as _exc:
    try:
        wconn.rollback()
    except Exception:
        pass
    logger.error("SnapshotManager.save_snapshot: %s failed: %s", 
                 session_id, _exc)
    return None  # Ensure caller can distinguish from success
```

Also add specific handling for transient vs permanent errors:

```python
except sqlite3.IntegrityError:
    logger.warning("Duplicate snapshot %s/%s", session_id, snap_id)
    return None
except sqlite3.OperationalError as _oe:
    logger.error("Snapshot save failed (transient): %s", _oe)
    return None
```

---

### FRUG-3: `publish_files_changed` — silently drops ALL event bus failures

**File:** `src/core/orchestration/session_manager.py:292-301`

```python
self.event_bus.publish(
    "session.files_changed",
    {"files": changes, "workdir": str(workdir_path), ...},
)
# Line 300-301: ENTIRE publish wrapped in bare except: pass
except Exception:
    pass  # User sees no sidebar update, no indication why
```

**Deep Analysis:**

`event_bus.publish()` can fail for many reasons:
- Event bus not initialized → `AttributeError`
- Subscriber raises → propagates to publisher
- Deadlock in subscriber callback → blocks publish

The `pass` means the user sees **no file tree updates** with **no error message**. This is a UX failure that hides production issues.

**Fix Plan:**

```python
try:
    self.event_bus.publish("session.files_changed", {...})
except Exception as _exc:
    logger.warning("session.files_changed event dropped: %s", _exc)
    # Consider a fallback: queue for retry or surface to UI
```

---

### FRAG-4: Unbounded `_read_all_records` — OOM on large sessions

**File:** `src/core/memory/jsonl_session_store.py:242-279`

```python
def _read_all_records(self, session_id: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for fpath in self._session_files(session_id):  # ALL rotated files
        with locked_file(fpath, mode="r") as f:
            for raw_line in f:           # NO limit on lines read
                records.append(json.loads(line))
    return records
```

**Deep Analysis:**

`_session_files(session_id)` returns ALL rotated files. A session with:
- 5 rotations × 100MB each = 500MB loaded into memory
- Each record parsed as JSON = additional memory overhead
- No pagination, no limit, no streaming

Callers:
- `get_session_history()` → loads ALL records
- `get_messages_since()` → still calls `_read_all_records`, filters in-memory
- `export_session()` → same issue

**Fix Plan:**

Add pagination support:

```python
def _read_records_page(
    self, session_id: str, limit: int = 1000, offset: int = 0
) -> Tuple[List[Dict[str, Any]], bool]:
    """Read records with pagination. Returns (records, has_more)."""
    records = []
    skipped = 0
    has_more = False
    for fpath in self._session_files(session_id):
        with locked_file(fpath, mode="r") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                skipped += 1
                if skipped <= offset:
                    continue
                if len(records) >= limit:
                    has_more = True
                    return records, has_more
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records, has_more
```

Or for streaming consumption:

```python
def iter_records(self, session_id: str):
    """Yield records lazily without loading all into memory."""
    for fpath in self._session_files(session_id):
        with locked_file(fpath, mode="r") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
```

---

### FRAG-5: `_session_lock` dict accumulates unbounded — memory leak

**File:** `src/core/memory/jsonl_session_store.py:110`

```python
# Module-level
_SESSION_LOCKS: Dict[str, threading.Lock] = {}

def _session_lock(self, session_id: str) -> threading.Lock:
    if session_id not in _SESSION_LOCKS:
        _SESSION_LOCKS[session_id] = threading.Lock()
    return _SESSION_LOCKS[session_id]
```

**Deep Analysis:**

Every unique `session_id` creates a `threading.Lock` stored forever in `_SESSION_LOCKS`. Sessions that are deleted/archived still have their locks in memory. For long-running servers with many ephemeral sessions (e.g., per-task sessions), this is a **memory leak**.

**Fix Plan:**

Use `WeakValueDictionary` so locks are garbage-collected when session objects are gone, OR add an explicit cleanup method:

```python
from weakref import WeakValueDictionary

_SESSION_LOCKS: WeakValueDictionary[str, threading.Lock] = WeakValueDictionary()

# OR add explicit cleanup:
def _release_session_lock(self, session_id: str) -> None:
    """Call when session is fully archived/deleted."""
    _SESSION_LOCKS.pop(session_id, None)
```

---

## Priority 2: Async/Concurrency Hazards

### FRAG-6: `asyncio.run()` in thread pool — nested event loop risk

**File:** `src/core/orchestration/inference_loop_rounds.py:13-23`

```python
def _run_graph_round_sync(graph, orch, state_to_run):
    return asyncio.run(  # Creates NEW event loop
        graph.ainvoke(state_to_run, {...})
    )

def _execute_graph_round(...):
    try:
        asyncio.get_running_loop()  # Check: are we in an event loop?
        _ctx = _cv.copy_context()
        future = graph_executor.submit(  # Submit to thread pool
            _ctx.run, _run_graph_round_sync, graph, orch, current_state
        )
        return future.result()
    except RuntimeError:  # No event loop running
        return _run_graph_round_sync(graph, orch, current_state)  # Direct call
```

**Deep Analysis:**

`_execute_graph_round` is called from a **thread pool** (`graph_executor.submit`). When it calls `asyncio.get_running_loop()` on line 35:
- If there IS a running loop → submits `_run_graph_round_sync` to the thread pool (which then creates a nested `asyncio.run()`)
- If there is NO running loop → calls `_run_graph_round_sync` directly (which creates its own loop via `asyncio.run()`)

The nested `asyncio.run()` case is problematic:
1. `asyncio.run()` creates a **new event loop** on each call
2. The LangGraph `ainvoke()` may use `asyncio.create_task()` internally
3. These tasks are tied to the NEW loop, not the outer loop
4. If the outer code tries to `await` something from those tasks, it deadlocks

However, looking at the code flow: `_run_graph_round_sync` is submitted to `graph_executor` (a `ProcessPoolExecutor` or `ThreadPoolExecutor`). The `.result()` call blocks until complete. The nested `asyncio.run()` completes fully before returning. So **this is actually safe** for this specific pattern because:
- The nested loop is fully contained within the thread pool task
- Nothing from the outer loop is awaited within the nested context

But the pattern is **fragile**: if someone later adds a `.result()` followed by an `await` in the outer code, they'll hit the nested loop issue.

**Fix Plan:**

Document the constraint and use a safer pattern:

```python
def _execute_graph_round(...):
    """Execute graph round. Must NOT be called from within an active event loop."""
    try:
        asyncio.get_running_loop()
        raise RuntimeError(
            "_execute_graph_round must not be called from within an asyncio event loop. "
            "Use await _execute_graph_round_async() instead."
        )
    except RuntimeError:
        # No running loop — safe to call directly
        return _run_graph_round_sync(graph, orch, current_state)
```

Or better: refactor to pass the graph execution into the thread pool without `asyncio.run()`:

```python
def _run_graph_round_sync(graph, orch, state_to_run):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            graph.ainvoke(state_to_run, {...})
        )
    finally:
        loop.close()
```

---

### FRAG-7: Lock ordering — AB-BA deadlock risk in `AgentSessionManager`

**File:** `src/core/orchestration/agent_session_manager.py:158-206`

```python
def update_session_state(...):
    with self._state_lock:           # Lock #1
        ...
        with self._sessions_lock:    # Lock #2 (nested inside #1)
            state.active_agents = {...}

def get_session_state():
    with self._state_lock:           # Lock #1
        state = self._current_session_state
        with self._sessions_lock:    # Lock #2 (nested inside #1)
            state.active_agents = {...}

    # IMPORTANT: flush_pending_p2p() called OUTSIDE both locks
    state.pending_p2p = self.flush_pending_p2p()  # Acquires _p2p_lock
```

**Deep Analysis:**

The comment at lines 194-205 explicitly acknowledges the deadlock risk. The **mitigation** is correct but fragile:

Lock acquisition order in this file:
- `update_session_state`: `_state_lock` → `_sessions_lock`
- `get_session_state`: `_state_lock` → `_sessions_lock`
- `buffer_p2p_message`: `_p2p_lock` only
- `flush_pending_p2p`: `_p2p_lock` only

**AB-BA scenario:** If a thread holds `_p2p_lock` and then tries to acquire `_state_lock`, and another thread holds `_state_lock` and tries to acquire `_p2p_lock`, we deadlock. The current code avoids this by calling `flush_pending_p2p()` **outside** the lock. But:

1. If any other method calls `flush_pending_p2p()` while holding `_state_lock`, it deadlocks
2. The constraint is not enforced — future developers must remember this

**Fix Plan:**

Create a lock hierarchy and enforce it via a lint rule or a wrapper:

```python
# In agent_session_manager.py
LOCK_ORDER = ("_p2p_lock", "_state_lock", "_sessions_lock")

def _acquire_locks(self, *lock_names):
    """Acquire locks in strict order. Raises if order would cause deadlock."""
    acquired = []
    for name in lock_names:
        lock = getattr(self, name)
        lock.acquire()
        acquired.append(lock)
    return acquired

def _release_locks(self, locks):
    for lock in reversed(locks):
        lock.release()
```

Or consolidate to a single lock:

```python
# Simpler: one lock for all session state
_session_state_lock = threading.RLock()

def update_session_state(...):
    with self._session_state_lock:
        with self._sessions_lock:  # sessions accessed here
            state.active_agents = {...}
        # flush_pending_p2p() can safely be called here since
        # we don't acquire _p2p_lock — it uses its own lock-free approach
```

---

### FRAG-8: `self.app._save_session_snapshot()` in background finally

**File:** `tui/src/ui/_bridge_agent.py:150-158`

```python
finally:
    with self._agent_lock:
        self._agent_running = False
    self._post(AgentRunningEvent(running=False))
    try:
        self.app._save_session_snapshot()  # Direct access! No guard!
    except Exception as _snap_err:
        logger.debug(...)
```

**Deep Analysis:**

`_run_agent` runs in a **background thread** (ThreadPoolExecutor). The `finally` block executes when the agent run completes. At this point:

1. `self.app` could be `None` (if app was destroyed)
2. `self.app` could be a **different app instance** (if app was replaced)
3. `_save_session_snapshot()` could block or fail while the UI is shutting down

The `try/except` catches exceptions, but if `self.app` is `None`, the `AttributeError` propagates... wait, no — the `try/except` catches `AttributeError` too since it catches `Exception`. So it won't crash. But it will log at DEBUG level only.

The deeper issue: if the app is shutting down, `_save_session_snapshot()` might save stale or incomplete state, or it might access resources that are already cleaned up.

**Fix Plan:**

Use `_schedule_callback` to marshal back to the UI thread:

```python
finally:
    with self._agent_lock:
        self._agent_running = False
    self._post(AgentRunningEvent(running=False))
    self._schedule_callback(self.app._save_session_snapshot)
```

This ensures:
1. The callback runs on the UI thread where `self.app` is valid
2. If app is shutting down, `_schedule_callback` will fail gracefully and log

---

### FRAG-9: `call_from_thread` silent failure

**File:** `tui/src/ui/core_bridge.py` (check tui path)

```python
def _schedule_callback(self, fn, *args):
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon(fn, *args)
    except RuntimeError:
        try:
            self.app.call_from_thread(fn, *args)
        except Exception as e:
            logger.debug("callback failed: %s", e)  # Silent at DEBUG!
```

**Deep Analysis:**

If BOTH `call_soon` AND `call_from_thread` fail, the callback is **permanently dropped** with only a DEBUG log. For critical UI events like `AgentFinalResponse`, `ToolCallStartEvent`, etc., this means:

1. UI stops updating for that event type
2. User sees stale state
3. No indication of failure at WARNING/ERROR level

**Fix Plan:**

```python
def _schedule_callback(self, fn, *args):
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon(fn, *args)
    except RuntimeError:
        try:
            self.app.call_from_thread(fn, *args)
        except Exception as e:
            logger.warning(
                "UI callback %s(%s) dropped: %s — UI may be desynced",
                fn.__name__, args, e
            )
            # Consider: queue for retry or surface error to user
```

---

## Priority 3: Pervasive Bare `except` Issues

### FRAG-10: ~1,200 bare `except Exception` patterns

**Distribution:**
- Orchestration: ~833 patterns
- Inference: ~231 patterns  
- Memory: ~174 patterns

**Fix Plan:**

Categorize and fix systematically:

**Category A — Errors that should propagate (logic bugs, config issues):**

```python
# BEFORE
try:
    config = json.loads(content)
except Exception:
    return {}

# AFTER
try:
    config = json.loads(content)
except json.JSONDecodeError as e:
    logger.warning("Invalid config JSON: %s", e)
    return {}
except PermissionError:
    logger.error("Cannot read config file: %s", path)
    raise
```

**Category B — Errors that are truly non-fatal (best-effort operations):**

```python
# BEFORE
try:
    self.event_bus.publish(...)
except Exception:
    pass

# AFTER
try:
    self.event_bus.publish(...)
except Exception as e:
    logger.debug("event_bus.publish non-fatal failure: %s", e)
    # Consider: queue for retry or surface to monitoring
```

**Category C — Errors that need user notification:**

```python
# BEFORE
try:
    write_file(path, content)
except Exception:
    pass  # User sees no error

# AFTER
try:
    write_file(path, content)
except PermissionError:
    raise ToolError(f"Permission denied: {path}")
except OSError as e:
    raise ToolError(f"File write failed: {e}")
```

**Fix automation approach:**

```bash
# Find all bare except in a file for prioritization
rg "except Exception:" --type py -n | wc -l
rg "except Exception:" --type py -n src/core/memory/ | sort -t: -k2 -n | tail -20
```

---

### FRAG-11: Ollama adapter — no retry, no backoff, short timeouts

**File:** `src/core/inference/adapters/ollama_adapter.py`

```python
requests.get(model_url, timeout=10)    # 10s too short for local VRAM loading
requests.post(chat_url, timeout=20)    # 20s too short for streaming
requests.post(generate_url, timeout=120) # Has timeout but NO retry logic
```

**Deep Analysis:**

Compare to `openai_compat_adapter.py` which has 3 retries with exponential backoff. Ollama adapter has:
- No retry on connection error
- No retry on 429 rate limit
- No retry on 500/502/503/504 errors
- Timeout of 10s for model listing (too short for VRAM loading)
- Timeout of 20s for `get_model_info` (too short for VRAM loading)

For a **local** service like Ollama, transient failures are rare but VRAM loading can take 30-60 seconds. The short timeouts cause false "not reachable" errors.

**Fix Plan:**

```python
def _post_with_retry(self, url, payload, timeout=60, max_retries=3):
    """POST with retry and backoff, suitable for local Ollama service."""
    for attempt in range(max_retries):
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning("Ollama %s failed (attempt %d/%d), retrying in %ds",
                               url, attempt + 1, max_retries, wait)
                time.sleep(wait)
                continue
            return r
        except requests.exceptions.ConnectionError as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    return r
```

---

### FRAG-12: OpenAI retry — no max delay cap, deterministic jitter

**File:** `src/core/inference/adapters/openai_compat_adapter.py:420-440`

```python
_retry_wait = 2**_attempt  # 1s, 2s, 4s

if r is not None and r.status_code == 429:
    for _hdr in ("retry-after", ...):
        _hval = r.headers.get(_hdr)
        if _hval:
            _retry_wait = min(float(_hval), 30.0)  # Cap at 30s — GOOD
            break

time.sleep(_retry_wait)  # Uses time.time() % 1.0 for jitter seed
```

**Issues:**

1. **Jitter seed uses `time.time() % 1.0`** — deterministic. Multiple clients hitting the server at the same timestamp get the same jitter, causing thundering herd.
2. **The `min(float(_hval), 30.0)` cap is good** — BUT only applied when `retry-after` header is present. Without it, backoff is just `2^attempt` (1s, 2s, 4s) with no cap.
3. **Rate-limited 429 without `retry-after`**: backoff is 1s, 2s, 4s. API may have 60s rate limit window → unnecessary failures.

**Fix Plan:**

```python
import random
import time

# Replace time.time() % 1.0 jitter with random
jitter = random.random()  # Proper random jitter 0-1s
_retry_wait = min(2**_attempt + jitter, 30.0)

# If 429 without retry-after, use larger backoff
if r is not None and r.status_code == 429:
    if not retry_after_header_found:
        _retry_wait = max(_retry_wait, 5.0)  # At least 5s for 429 without retry-after
```

---

### FRAG-13: URI path traversal in LSP tools

**File:** `src/tools/lsp_tools.py:301-304`

```python
if file_uri.startswith("file://"):
    file_path = Path(unquote(file_uri[7:]))  # Decode first!
else:
    file_path = Path(unquote(file_uri))

# NO workspace boundary check after decoding!
if file_path.is_file():
    content = file_path.read_text(...)
```

**Deep Analysis:**

`unquote()` decodes percent-encoded characters. A malicious LSP server could send:
- `file:///../../../etc/passwd` → decoded: `../../etc/passwd`
- `file:///proc/self/environ` → read environment variables

Even though the LSP server is "trusted" in the threat model, a compromised or malicious LSP server could exfiltrate:
- SSH keys from `.ssh/`
- API tokens from environment
- Config files with credentials

**Fix Plan:**

```python
import urllib.parse

if file_uri.startswith("file://"):
    file_path = Path(urllib.parse.unquote(file_uri[7:]))
else:
    file_path = Path(urllib.parse.unquote(file_uri))

# Resolve and validate workspace boundary
try:
    resolved = file_path.resolve()
except (OSError, ValueError):
    return {"error": "Invalid path", ...}

workdir = Path(working_dir).resolve()
if not resolved.is_relative_to(workdir):
    return {"error": "Path outside workspace", ...}

if not resolved.is_file():
    return {"error": "Not a file", ...}
```

---

### FRAG-14: `conceal_sensitive` default — credentials visible in TUI

**File:** `tui/src/ui/settings.py:69`

```python
"conceal_sensitive": False,  # API keys visible by default!
```

**Status:** Fixed earlier in this session (set to `True`).

---

### FRAG-15: Lambda capture of `self.app` unsafely

**File:** `tui/src/ui/_bridge_agent.py:143`

```python
lambda r=_r: setattr(self.app, "_continue_state", r)
```

**Status:** Fixed earlier in this session (captures via default arg `_app=self.app`).

---

### FRAG-16: StreamView flush race

**File:** `tui/src/ui/components/stream_view.py:14-19`

**Status:** Fixed earlier in this session (added `threading.Lock`).

---

## Priority 4: Design Issues

### FRAG-17: State machine not enforced

**File:** `src/core/orchestration/graph/state.py`

`AgentState` is a plain `TypedDict`. Invalid transitions (e.g., `waiting_for_user` → `executing` without proper guard) are silently allowed.

**Fix Plan:**

Add a state transition validator:

```python
VALID_TRANSITIONS = {
    "planning": {"executing", "waiting_for_user", "analysis"},
    "executing": {"planning", "waiting_for_user", "done", "error"},
    "waiting_for_user": {"planning", "executing", "done"},
    # ...
}

def validate_transition(from_state: str, to_state: str) -> None:
    if to_state not in VALID_TRANSITIONS.get(from_state, set()):
        logger.warning(
            "Unexpected state transition: %s → %s",
            from_state, to_state
        )
```

---

### FRAG-18: Threading locks in async contexts

**File:** `src/core/orchestration/agent_session_manager.py:113-117`

Three `threading.Lock()` instances used in code that processes async events. `Lock.acquire()` is **blocking** — if held for long operations, it blocks the event loop thread.

**Fix Plan:**

Audit all lock usages in async contexts:
- If lock is held across `await` points → convert to `asyncio.Lock`
- If lock is held only for brief critical sections without `await` → threading lock is OK
- If lock is used from both sync and async code → keep threading lock but document the constraint

---

## Fix Priority Order

| Priority | Issue | Files | Effort | Risk of Fix |
|----------|-------|-------|--------|-------------|
| P1 | FRAG-3 silent event bus failure | `session_manager.py:300-301` | Low | Very safe |
| P1 | FRAG-8 self.app in background finally | `_bridge_agent.py:157` | Low | Safe |
| P1 | FRAG-9 call_from_thread silent failure | `core_bridge.py:263-274` | Low | Safe |
| P1 | FRAG-10 Bare except patterns | 1,200+ files | High | Moderate — need careful per-case analysis |
| P2 | FRAG-4 Unbounded file reads | `jsonl_session_store.py:242` | Medium | Safe — add pagination |
| P2 | FRAG-11 Ollama no retry | `ollama_adapter.py` | Medium | Safe — add retry wrapper |
| P2 | FRAG-12 OpenAI retry no jitter | `openai_compat_adapter.py:420` | Low | Safe |
| P2 | FRAG-13 LSP URI path traversal | `lsp_tools.py:301` | Low | Safe |
| P2 | FRAG-5 Lock dict memory leak | `jsonl_session_store.py:110` | Low | Safe — use WeakValueDictionary |
| P3 | FRAG-1 revert_session TOCTOU | `jsonl_session_store.py:376` | Medium | Safe — wrap in try/except |
| P3 | FRAG-2 Snapshot transaction silent | `sqlite_store_collaborators.py:440` | Low | Safe — add logging |
| P3 | FRAG-6 asyncio.run nested loop | `inference_loop_rounds.py:15` | Medium | Moderate — needs design review |
| P3 | FRAG-7 Lock ordering | `agent_session_manager.py:158` | High | Moderate — refactoring |
| P3 | FRAG-14-16 (already fixed) | Various | Done | — |

---

## Quick Wins (Low Effort, High Impact)

1. **`session_manager.py:300-301`** — add `logger.warning` instead of bare `pass`
2. **`_bridge_agent.py:157`** — use `_schedule_callback` instead of direct `self.app` access
3. **`openai_compat_adapter.py:420`** — add `random.random()` jitter
4. **`ollama_adapter.py`** — add `_post_with_retry` wrapper (30s timeout, 3 retries)
5. **`sqlite_store_collaborators.py:444`** — add logging for snapshot save failures

## Major Refactors (High Effort, High Impact)

1. **FRAG-4** — Add pagination to `_read_all_records` and related methods
2. **FRAG-6** — Refactor `asyncio.run()` in thread pool to use `loop.run_until_complete()` on a dedicated loop
3. **FRAG-7** — Consolidate `AgentSessionManager` locks or create lock hierarchy enforcement
4. **FRAG-10** — Systematic replacement of bare `except` with specific exception types across 1,200+ patterns