# Code Quality Audit — Volume 44

**Date:** 2026-05-02
**Files audited:** src/core/memory/_write_retry_utils.py, src/core/memory/auto_compactor.py, src/core/memory/distiller.py, src/core/memory/file_lock.py, src/core/memory/frozen_snapshot.py, src/core/memory/jsonl_session_store.py, src/core/memory/session_store.py, src/core/memory/sqlite_session_store.py, src/core/context/context_builder.py, src/core/context/context_controller.py, src/core/context/instruction_files.py, src/core/orchestration/graph/nodes/analysis_node.py, src/core/orchestration/graph/nodes/debug_node.py, src/core/orchestration/graph/nodes/delegation_node.py, src/core/orchestration/graph/nodes/execution_node.py, src/core/orchestration/graph/nodes/planning_node.py, src/core/orchestration/graph/nodes/verification_node.py, src/core/orchestration/graph/state.py
**Findings:** 12 (F-58 retracted — variable is read at line 1305)

---

## Summary Table

| ID | Severity | Category | File |
|----|----------|----------|------|
| F-57 | High | InlineImport | src/core/memory/distiller.py:46 |
| F-58 | ~~High~~ | ~~DeadCode~~ | ~~src/core/orchestration/graph/nodes/execution_node.py:1171,1183~~ | **RETRACTED** — `plan_progress_event` is read at line 1305 via `**plan_progress_event` dict-unpack into the return state. |
| F-59 | High | InlineImport | src/core/orchestration/graph/nodes/execution_node.py:1194 |
| F-60 | High | DeadCode | src/core/memory/session_store.py:21-26 |
| F-61 | Medium | InlineImport | src/core/memory/distiller.py:70,75,136,241,265,387,421,448,524,543,613,667 |
| F-62 | Medium | DuplicateCode | src/core/memory/distiller.py:554-588 vs 627-656 |
| F-63 | Medium | MagicLiteral | src/core/context/context_controller.py:29-32 |
| F-64 | Medium | InlineImport | src/core/memory/session_store.py:104,108,131,378,448,501,515 |
| F-65 | Medium | InlineImport | src/core/memory/_write_retry_utils.py:93,119,144 |
| F-66 | Medium | MagicLiteral | src/core/memory/distiller.py:110 |
| F-67 | Low | MagicLiteral | src/core/orchestration/graph/nodes/delegation_node.py:41 |
| F-68 | Low | UnnecessaryComplexity | src/core/memory/session_store.py:263-330 |
| F-69 | Low | InconsistentPattern | src/core/memory/distiller.py:387 vs src/core/memory/_write_retry_utils.py:74 |

**Totals:** 3 High · 5 Medium · 2 Low

---

## Findings

### F-57 — Inline import of tokenizer inside hot path

**Severity:** High
**Category:** InlineImport
**File:** src/core/memory/distiller.py:46

**Description:**
`_estimate_tokens()` is called on every invocation of `distill_context()` and (potentially) `compact_messages_to_prose()`. Each call executes:
```python
from src.core.inference.tokenizer import count_messages_tokens
```
inside the function body. Because Python caches modules after first import, the lookup overhead is minor, but it suppresses static analysis (the import is invisible to linters and type checkers), makes the dependency graph opaque, and violates the module's own pattern of top-level imports. More critically, if `count_messages_tokens` raises on import, the bare `except Exception` silently falls back to the char heuristic — masking a misconfiguration that produces systematically wrong token counts, which in turn causes compaction to trigger too early or never.

**Fix:** Move the import to the module top level and let the `ImportError` propagate on startup so misconfigurations are caught immediately:
```python
try:
    from src.core.inference.tokenizer import count_messages_tokens as _count_tokens
except ImportError:
    _count_tokens = None

def _estimate_tokens(messages):
    if _count_tokens is not None:
        return _count_tokens(messages)
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    return max(1, int(total_chars / 3.5))
```

---

### F-58 — Dead assignment: `plan_progress_event` computed but never consumed

**Severity:** High
**Category:** DeadCode
**File:** src/core/orchestration/graph/nodes/execution_node.py:1171,1183

**Description:**
`plan_progress_event` is assigned at line 1171 (`plan_progress_event = {}`), then conditionally overwritten at line 1183 (`plan_progress_event = {"plan_progress": progress_payload}`). After line 1183 the variable is never read, referenced in a return dict, or passed anywhere. The actual side-effect (publishing to `orchestrator.event_bus`) happens in lines 1185-1188 independently. The dict assignment is pure dead code: it wastes a dict construction and creates a misleading implication that the result is used.

**Fix:** Remove both assignments to `plan_progress_event`. The event-bus publish and TODO check-off logic below it are the real work and should remain.

---

### F-59 — Inline import of `manage_todo` inside plan-step loop

**Severity:** High
**Category:** InlineImport
**File:** src/core/orchestration/graph/nodes/execution_node.py:1194

**Description:**
```python
from src.tools.todo_tools import manage_todo
```
is placed inside the execution hot path (inside a conditional that runs on every completed plan step). This is both a performance concern — module attribute lookup bypasses the fast-path module cache on some Python builds — and an architectural smell: the dependency is invisible at import time, preventing static checks and circular-import detection. The same import is available at the module top level in other node files.

**Fix:** Move to the module-level imports at the top of `execution_node.py`:
```python
from src.tools.todo_tools import manage_todo
```

---

### F-60 — Unused imports in compatibility shim (`session_store.py`)

**Severity:** High
**Category:** DeadCode
**File:** src/core/memory/session_store.py:21-26

**Description:**
The compatibility shim imports `inspect`, `sqlite3`, `tempfile`, and `shutil` at the module top level (lines 21-26). Of these:

- `inspect` — used only in `__getattr__` to inspect method signatures (line 237). Legitimate use.
- `sqlite3` — used only in `_get_connection()` (line 286) for the *fallback* sqlite connection path that is only exercised when the underlying store does not provide `_get_connection`. In the default sqlite backend path, this is dead — `SqliteSessionStore._get_connection` is always invoked instead.
- `tempfile` and `shutil` — used in `write_decisions_json` and `_write_with_retry` fallback paths. Legitimate use.

The `sqlite3` module-level import is the problematic one: it pulls in a C extension for a code path that is never exercised when the configured backend is sqlite (the overwhelming default), and its presence misleads readers into thinking the shim itself contains sqlite logic rather than delegating.

**Fix:** Move `sqlite3` import inside `_get_connection()` where it is actually needed for the fallback path:
```python
def _get_connection(self) -> "sqlite3.Connection":
    import sqlite3
    ...
```

---

### F-61 — Pervasive inline imports throughout `distiller.py`

**Severity:** Medium
**Category:** InlineImport
**File:** src/core/memory/distiller.py:70,75,136,241,265,387,421,448,524,543,613,667

**Description:**
`distiller.py` uses inline (deferred) imports for nearly all of its dependencies:
- Line 70: `from src.core.inference.llm_manager import call_model`
- Line 75: `from src.core.config_loader import get_small_model as _gsm`
- Line 136: `from src.core.inference.thinking_utils import strip_thinking`
- Line 241: `from src.core.config_loader import get as _cfg_get`
- Line 265: `from src.tools.tools_config import agent_context_path`
- Line 387: `from src.core.inference.thinking_utils import budget_max_tokens, get_active_model_id`
- Lines 421, 524: `from src.tools.tools_config import agent_context_path` (repeated)
- Lines 448, 543, 613: `from src.tools.todo_tools import _load_todo_json` / `from src.core.io_utils import atomic_write_json` (repeated)
- Line 667: `from src.core.indexing.vector_store import VectorStore`

This pattern was introduced to break circular imports, but it makes the module's dependency graph opaque, prevents `mypy` / `ruff` from seeing the imports, and causes module re-resolution on every call to `distill_context`. Several of these — `call_model`, `strip_thinking`, `budget_max_tokens`, `agent_context_path` — are called on every invocation.

**Fix:** Audit the actual circular import chain. In practice `distiller.py` → `llm_manager` → `distiller.py` is unlikely. Move stable, unconditionally-needed imports (`call_model`, `strip_thinking`, `budget_max_tokens`, `get_active_model_id`, `agent_context_path`) to module level. Keep only genuinely optional or circular-breaking imports inline.

---

### F-62 — Duplicate atomic-write fallback blocks in `distiller.py`

**Severity:** Medium
**Category:** DuplicateCode
**File:** src/core/memory/distiller.py:554-588 vs 627-656

**Description:**
The pattern:
```python
try:
    from src.core.io_utils import atomic_write_json
    ok = atomic_write_json(path, data, logger=logger)
    if not ok:
        fd, tmp_path = tempfile.mkstemp(...)
        ...
except Exception:
    fd, tmp_path = tempfile.mkstemp(...)
    ...
```
appears **four times** in `distiller.py` (for `compaction_checkpoint.md`, `TASK_STATE.md`, `repo_memory.json`, and `file_summaries.json`). The module already imports `tempfile`, `os`, and `shutil` at the top level, and `_write_retry_utils.py` exists precisely to provide `atomic_write_json` as a top-level utility. The duplication means any fix to the write strategy must be applied in four places.

**Fix:** Extract a module-level helper (or import `_write_retry_utils.atomic_write_json` directly) and replace all four occurrences with a single `_safe_write_json(path, data)` call.

---

### F-63 — Magic budget percentages in `ContextController.__init__`

**Severity:** Medium
**Category:** MagicLiteral
**File:** src/core/context/context_controller.py:29-32

**Description:**
```python
self._context_budget = {
    "relevant_files": math.ceil(0.08 * max_tokens),
    "bugs_found":     math.ceil(0.05 * max_tokens),
    "research":       math.ceil(0.06 * max_tokens),
    "other":          math.ceil(0.03 * max_tokens),
}
```
The four fractions `0.08`, `0.05`, `0.06`, `0.03` are inline magic literals. They are the core budget allocation policy for context management — a change to any value requires knowing where to find it. They also sum to `0.22` (22 %), leaving 78 % unaccounted for, which will confuse future maintainers. The class already defines named constants for `DEFAULT_MAX_TOKENS`, `LARGE_FILE_THRESHOLD`, and `SUMMARY_TARGET_LINES`.

**Fix:** Promote the fractions to named class-level constants:
```python
_BUDGET_RELEVANT_FILES = 0.08
_BUDGET_BUGS_FOUND     = 0.05
_BUDGET_RESEARCH       = 0.06
_BUDGET_OTHER          = 0.03
```

---

### F-64 — Repeated inline imports inside `session_store.py` functions

**Severity:** Medium
**Category:** InlineImport
**File:** src/core/memory/session_store.py:104,108,131,378,448,501,515

**Description:**
`get_session_store()` imports `SqliteSessionStore` and `JsonlSessionStore` again inside the local `_instantiate_raw` helper (lines 104, 108) even though they are already imported at module level (lines 37-39). The function-level import was added to support test-time monkeypatching, but the module-level import already achieves this with `importlib.reload` or `unittest.mock.patch`. Additionally `write_decisions_json` (line 378) and `_write_with_retry` (lines 448, 501, 515) each inline-import `traceback`, `time`, and `src.core.io_utils.atomic_write_json` — all of which are available (or should be available) at module level.

**Fix:** Remove the redundant in-function imports of `SqliteSessionStore` and `JsonlSessionStore`. Move `traceback`, `time`, and `atomic_write_json` to module level.

---

### F-65 — Inline `json` imports inside `_write_retry_utils.atomic_write_json`

**Severity:** Medium
**Category:** InlineImport
**File:** src/core/memory/_write_retry_utils.py:119,144

**Description:**
```python
import json
json.dump(data, f, indent=2)
```
appears twice inside the `atomic_write_json` function body (lines 119 and 144). `json` is a stdlib module; there is no circular-import risk. The inline import exists due to copy-paste from another module. All other imports in the file are at module level.

**Fix:** Add `import json` to the top of the file and remove the two inline occurrences.

---

### F-66 — Magic timeout literal in `distiller._call_llm_sync`

**Severity:** Medium
**Category:** MagicLiteral
**File:** src/core/memory/distiller.py:110

**Description:**
```python
resp = future.result(timeout=120)
```
The `120`-second hard timeout for the LLM thread-executor call is an inline magic literal. This is the single most consequential timeout in the distillation subsystem — if the LLM hangs, the compaction path will block a thread for 2 minutes before giving up. The value is undocumented and not configurable.

**Fix:** Define a named constant and make it config-readable:
```python
_DISTILLER_LLM_TIMEOUT_SECONDS = 120  # configurable via distiller_llm_timeout_seconds

# In _call_llm_sync:
_timeout = _DISTILLER_LLM_TIMEOUT_SECONDS
try:
    from src.core.config_loader import get as _cfg_get
    _timeout = int(_cfg_get("distiller_llm_timeout_seconds", _DISTILLER_LLM_TIMEOUT_SECONDS) or _DISTILLER_LLM_TIMEOUT_SECONDS)
except Exception:
    pass
resp = future.result(timeout=_timeout)
```

---

### F-67 — Magic write-lock timeout in `delegation_node.py`

**Severity:** Low
**Category:** MagicLiteral
**File:** src/core/orchestration/graph/nodes/delegation_node.py:41

**Description:**
```python
success = await lock_manager.acquire_write_async(f, agent_id, timeout=30.0)
```
The 30-second write-lock acquisition timeout is an inline float. The value is reasonable but undocumented and not configurable. The file already defines `_MAX_DELEGATION_DEPTH = 3` as a named constant for the analogous recursion-depth limit.

**Fix:** Add a module-level constant:
```python
_WRITE_LOCK_TIMEOUT_SECONDS = 30.0
```
and reference it in the `acquire_write_async` call.

---

### F-68 — Unnecessary in-band sqlite fallback in `SessionStore._get_connection`

**Severity:** Low
**Category:** UnnecessaryComplexity
**File:** src/core/memory/session_store.py:263-330

**Description:**
`SessionStore._get_connection` (lines 263-330) implements a full per-thread sqlite connection cache — including WAL mode, busy timeout, schema creation, and thread-registry integration — as a *fallback* for when the underlying store does not provide `_get_connection`. In practice, the default backend is always `SqliteSessionStore`, which does provide `_get_connection`, so this fallback code never runs in production. It adds ~70 lines of complexity to a compatibility shim, creates a second sqlite connection pool that competes with the real one, and silently creates a second `session.db` file under the agent context directory.

**Fix:** Replace the fallback with a clear error:
```python
def _get_connection(self):
    if hasattr(self._store, "_get_connection"):
        return self._store._get_connection()
    raise RuntimeError(
        f"Underlying store {type(self._store).__name__} does not support _get_connection"
    )
```
Tests that rely on this path should be rewritten to use `SqliteSessionStore` directly.

---

### F-69 — Duplicate `atomic_write_json` implementations

**Severity:** Low
**Category:** InconsistentPattern
**File:** src/core/memory/_write_retry_utils.py:74 vs src/core/memory/distiller.py (inline), src/core/memory/session_store.py (inline)

**Description:**
`_write_retry_utils.py` defines `atomic_write_json` as a module-level utility (line 74). `distiller.py` and `session_store.py` each inline the same try-`atomic_write_json`-fallback-to-mkstemp pattern rather than importing and calling `_write_retry_utils.atomic_write_json`. The utility function in `_write_retry_utils.py` itself also delegates to `src.core.io_utils.atomic_write_json` if available — adding a third implementation in the same chain. This three-layer delegation pattern means a bug in atomic writes must be investigated in up to three places.

**Fix:** Consolidate on a single canonical implementation in `src.core.io_utils`. Have `_write_retry_utils.atomic_write_json` simply re-export `src.core.io_utils.atomic_write_json` (no wrapper). Remove all inline copy-paste occurrences in `distiller.py` and `session_store.py` by calling the canonical import directly.

---

## Totals by Severity

| Severity | Count |
|----------|-------|
| High | 3 |
| Medium | 5 |
| Low | 2 |
| **Total** | **13** |

## Totals by Category

| Category | Count |
|----------|-------|
| InlineImport | 5 |
| DeadCode | 2 |
| MagicLiteral | 3 |
| DuplicateCode | 1 |
| UnnecessaryComplexity | 1 |
| InconsistentPattern | 1 |
