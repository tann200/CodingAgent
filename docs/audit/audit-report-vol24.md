# Audit Report — Vol24

**Date:** 2026-04-09
**Prior cycle:** Vol23 (2026-04-09)
**Scope:** Fresh scan across orchestration, inference, MCP server, and approval subsystems looking for new bugs not addressed in prior cycles.

---

## Summary

| ID | Severity | Status | File | Description |
|----|----------|--------|------|-------------|
| SEC-VOL24-1 | MEDIUM | FIXED | approval_gate.py:115 | `assert ev is not None` stripped by `python -O` |
| SEC-VOL24-2 | HIGH | FIXED | mcp_stdio_server.py:299 | Path traversal — `startswith(str(base))` missing `os.sep` |
| PERF-VOL24-1 | MEDIUM | FIXED | mcp_stdio_server.py:392 | New `ThreadPoolExecutor` per `sampling/create` call |
| PERF-VOL24-2 | LOW | FIXED | openai_compat_adapter.py:391 | `assert r is not None` stripped by `python -O` |
| AUDIT-VOL24-DEBUNKED | N/A | N/A | distiller.py | `shutdown(wait=False)` — intentional per SCAN-6, not a bug |
| AUDIT-VOL24-DEBUNKED | N/A | N/A | planning_node.py | `plan_attempts` not missing — returned in all paths |
| AUDIT-VOL24-DEBUNKED | N/A | N/A | execution_node.py | `finally` block is NOT bypassed by early `return` in Python |

---

## Findings

### SEC-VOL24-1 — `assert ev is not None` in approval_gate.py (MEDIUM)

**Location:** `src/core/orchestration/approval_gate.py:115`

**Issue:** The `assert` statement is silently removed when the interpreter runs with the `-O` (optimize) flag (`python -O`). In the fallback path of `ApprovalGate.wait_async()`, `ev.wait(timeout)` would then be called on `None`, raising an unguarded `AttributeError` with no context.

**Before:**
```python
ev = self._sync_event
assert ev is not None
try:
    await _asyncio.wait_for(...)
```

**After:**
```python
ev = self._sync_event
if ev is None:
    raise RuntimeError("ApprovalGate._sync_event is None in fallback path")
try:
    await _asyncio.wait_for(...)
```

---

### SEC-VOL24-2 — Path traversal in mcp_stdio_server.py (HIGH)

**Location:** `src/core/orchestration/mcp_stdio_server.py:299` (`resources/read` handler)

**Issue:** The path traversal guard used `str(target).startswith(str(base))`. Because Python string `startswith` matches any prefix, the path `/workdir-evil/secret.txt` would pass the check when `base = /workdir`, allowing reads outside the working directory.

**Before:**
```python
if str(target).startswith(str(base)) and target.is_file():
```

**After:**
```python
import os as _os
_base_prefix = str(base) + _os.sep
if (str(target) == str(base) or str(target).startswith(_base_prefix)) and target.is_file():
```

The fix appends the OS path separator so that `/workdir-evil` no longer matches `/workdir/`.

---

### PERF-VOL24-1 — ThreadPoolExecutor created per `sampling/create` call (MEDIUM)

**Location:** `src/core/orchestration/mcp_stdio_server.py:392`

**Issue:** Every `sampling/create` MCP request created a new `ThreadPoolExecutor(max_workers=1)` as a context manager. Thread creation and teardown on each request adds latency and wastes OS resources under load.

**Fix:** Added `self._sampling_executor = ThreadPoolExecutor(max_workers=1)` in `MCPStdioServer.__init__()` and replaced the per-call `with ThreadPoolExecutor(...) as _pool:` with `self._sampling_executor.submit(...)`.

---

### PERF-VOL24-2 — `assert r is not None` in openai_compat_adapter.py (LOW)

**Location:** `src/core/inference/adapters/openai_compat_adapter.py:391`

**Issue:** The surrounding comment correctly explains that `r` cannot be `None` at this point — the retry loop would have raised before reaching this line. However, the bare `assert` is stripped in optimized mode and provides no diagnostic value when it would fire. Consistent with SEC-VOL24-1.

**Before:**
```python
assert r is not None  # narrow type for pyright
```

**After:**
```python
if r is None:  # pragma: no cover — defensive; satisfies type narrowing
    raise RuntimeError("HTTP response is None after retry loop (unexpected)")
```

---

## Debunked / Reclassified Findings

### distiller.py `shutdown(wait=False)` — NOT A BUG
The subagent flagged `_pool.shutdown(wait=False)` in `distiller.py`. This is intentional per SCAN-6 (2026-03-22): the distiller pool is used for LLM calls that can block for seconds; `wait=False` + `future.cancel()` avoids hanging the entire agent when distillation times out. The comment in the source confirms this.

### planning_node.py `plan_attempts` — NOT MISSING
Subagent claimed `plan_attempts` was not returned in all paths. Direct code reading confirmed it is returned at every `return {...}` site in `planning_node`.

### execution_node.py early `return` bypasses `finally` — INCORRECT
Python's `finally` block always executes even on early `return` inside a `try`. The claim that `return` inside `try` skips `finally` is false.

---

## Test Coverage

Regression tests added in `tests/unit/test_audit_vol24.py`:
- `TestSecVol241ApprovalGateAssert` (2 assertions)
- `TestSecVol242McpPathTraversal` (2 tests)
- `TestPerfVol241McpExecutorReuse` (3 tests)
- `TestPerfVol242OpenAICompatAssert` (2 assertions)
