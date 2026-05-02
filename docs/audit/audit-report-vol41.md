# Code Quality Audit — Vol. 41

**Date:** 2026-05-02
**Auditor:** OpenCode automated audit
**Scope:** 20 files across `src/tools/` and `src/core/orchestration/graph/nodes/`

---

## Files Audited

| # | File | Lines |
|---|------|-------|
| 1 | `src/tools/_bash_exec.py` | 680 |
| 2 | `src/tools/_edit_tools.py` | 654 |
| 3 | `src/tools/_file_io.py` | 731 |
| 4 | `src/tools/_registry.py` | 469 |
| 5 | `src/tools/_security.py` | 382 |
| 6 | `src/tools/bash_security.py` | 189 |
| 7 | `src/tools/batch_tools.py` | 164 |
| 8 | `src/tools/formatter.py` | 128 |
| 9 | `src/tools/git_tools.py` | 197 |
| 10 | `src/tools/guardrails.py` | 97 |
| 11 | `src/tools/lsp_tools.py` | 480 |
| 12 | `src/tools/memory_tools.py` | 242 |
| 13 | `src/tools/repo_analysis_tools.py` | 348 |
| 14 | `src/tools/repo_summary.py` | 352 |
| 15 | `src/tools/state_tools.py` | 343 |
| 16 | `src/tools/subagent_tools.py` | 1091 |
| 17 | `src/core/orchestration/graph/nodes/debug_node.py` | 377 |
| 18 | `src/core/orchestration/graph/nodes/execution_node.py` | 1310 |
| 19 | `src/core/orchestration/graph/nodes/perception_node.py` | 2079 |
| 20 | `src/core/orchestration/graph/nodes/planning_node.py` | 1007 |

**Total lines audited:** 10,320

---

## Scope & Exclusions

Findings are limited to actionable code-quality issues. The following categories are **excluded**:

- Pure style / formatting (spacing, naming conventions)
- Intentional circular-import guards (lazy imports inside try/except at module level to avoid import cycles — see note on `lsp_tools.py` below)
- Changes that would require significant API redesign

---

## Summary Table

| ID | Severity | Category | File(s) | Short Description |
|----|----------|----------|---------|-------------------|
| F-01 | Medium | DRY Violation | `_edit_tools.py`, `_file_io.py` | Duplicate `WorkspaceGuard` no-op stub |
| F-02 | Medium | DRY Violation | `repo_analysis_tools.py`, `repo_summary.py` | Duplicate directory-exclusion constant sets |
| F-03 | Low | DRY Violation | `_file_io.py` | `WorkspaceGuard` fallback is `pass` — stub is absent, no-op is invisible |
| F-04 | Low | Redundant Call | `state_tools.py` | `datetime.now()` called twice; timestamp can drift between calls |
| F-05 | Low | Dead Differentiation | `memory_tools.py` | `_USER_TIER_LIMITS` has identical `max_chars` across all three tiers |
| F-06 | Low | Inline Import | `planning_node.py` | `from datetime import datetime` inside function body (non-circular) |
| F-07 | Low | Inline Import | `planning_node.py` | Second `from datetime import datetime` inline import in same file |
| F-08 | Low | Missing Shared Constant | `repo_summary.py` | `_FW_EXCLUDE` and `_EXCLUDE_DIRS` both defined locally — same content |
| F-09 | Info | Redundant Wrapper | `subagent_tools.py` | `_atomic_write_json` wraps `src.core.io_utils.atomic_write_json`; callers in same package use the central function directly |

---

## Detailed Findings

---

### F-01 — Duplicate `WorkspaceGuard` no-op stub

**Severity:** Medium
**Category:** DRY Violation
**Files:**
- `src/tools/_edit_tools.py:35–51`
- `src/tools/_file_io.py:38–40` (partial — see F-03)

**Description:**
`_edit_tools.py` defines a complete inline no-op `WorkspaceGuard` fallback class (with `__init__`, `__enter__`, `__exit__`) inside an `except ImportError` block. The same fallback class should appear in `_file_io.py` but the except block there is just `pass`, meaning any code that calls `WorkspaceGuard()` in `_file_io.py` when the real module is absent will raise `NameError` at runtime. The stub in `_edit_tools.py` is the correct pattern; `_file_io.py` diverged.

**`_edit_tools.py` (correct pattern):**
```python
# lines 35–51
try:
    from src.tools._workspace_guard import WorkspaceGuard
except ImportError:
    import contextlib
    class WorkspaceGuard:
        """No-op stub used when _workspace_guard is unavailable."""
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
```

**`_file_io.py` (broken pattern):**
```python
# lines 38–41
try:
    from src.tools._workspace_guard import WorkspaceGuard
except ImportError:
    pass   # <-- WorkspaceGuard is now undefined if import fails
```

**Recommended Fix:**
Either (a) move the no-op stub to `src/tools/_workspace_guard.py` as the canonical fallback and ensure it is always importable, eliminating the need for duplicate stubs entirely; or (b) copy the full stub from `_edit_tools.py` into the `except ImportError` block in `_file_io.py` so both files are consistent and safe.

---

### F-02 — Duplicate directory-exclusion constant sets

**Severity:** Medium
**Category:** DRY Violation / Missing Shared Constant
**Files:**
- `src/tools/repo_analysis_tools.py:17–27`
- `src/tools/repo_summary.py:32` (`_FW_EXCLUDE`) and `repo_summary.py:73` (`_EXCLUDE_DIRS`)

**Description:**
`repo_analysis_tools.py` defines `_EXCLUDE_DIRS` as a module-level set of 10 well-known noise directories (`.venv`, `venv`, `__pycache__`, `.git`, `node_modules`, `.mypy_cache`, `.pytest_cache`, `dist`, `build`, `target`). `repo_summary.py` defines the same concept **twice** under two names — `_FW_EXCLUDE` (5 entries, line 32) and `_EXCLUDE_DIRS` (5 entries, line 73) — both omitting the extended entries present in `repo_analysis_tools.py`. The three definitions will drift independently as new noise directories are added.

**Recommended Fix:**
Extract the authoritative set to a shared constant, e.g. `src/tools/_constants.py`:
```python
REPO_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", "target",
})
```
Import `REPO_EXCLUDE_DIRS` in both `repo_analysis_tools.py` and `repo_summary.py` and remove all three local definitions.

---

### F-03 — `_file_io.py` `WorkspaceGuard` fallback is silent `pass`

**Severity:** Low
**Category:** Latent Bug / DRY Violation
**File:** `src/tools/_file_io.py:38–41`

**Description:**
As noted in F-01, the `except ImportError: pass` pattern leaves `WorkspaceGuard` undefined in `_file_io.py` when `_workspace_guard` is not installed. Two call-sites (`_file_io.py:106`, `_file_io.py:430`) instantiate `WorkspaceGuard()` unconditionally; these will raise `NameError` in any environment where `src.tools._workspace_guard` cannot be imported (e.g. early development, CI without optional deps). This is a latent runtime failure separate from the stylistic duplication noted in F-01.

**Recommended Fix:** Apply the full no-op stub shown in F-01, or gate the call-sites with `if WorkspaceGuard is not None`.

---

### F-04 — `datetime.now()` called twice in `create_state_checkpoint`

**Severity:** Low
**Category:** Redundant Call / Correctness
**File:** `src/tools/state_tools.py:44, 50`

**Description:**
```python
# line 44
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
# ...
# line 50
"created_at": datetime.now().isoformat(),
```
`datetime.now()` is called twice, 6 lines apart. In normal execution the difference is negligible, but the two values are semantically the same instant (the checkpoint creation time) and should be identical. Under load or system clock jitter the formatted `timestamp` used in the filename and the `created_at` ISO string in the payload could differ by a microsecond, making them non-trivially inconsistent.

**Recommended Fix:**
```python
_now = datetime.now()
timestamp = _now.strftime("%Y%m%d_%H%M%S_%f")
# ...
"created_at": _now.isoformat(),
```

---

### F-05 — `_USER_TIER_LIMITS` has identical values across all tiers

**Severity:** Low
**Category:** Dead Differentiation
**File:** `src/tools/memory_tools.py:47–51`

**Description:**
```python
_USER_TIER_LIMITS = {
    "lite":     {"max_chars": 1375},
    "standard": {"max_chars": 1375},
    "full":     {"max_chars": 1375},
}
```
All three tier entries have the same `max_chars` value. The lookup function `get_user_max_chars` (lines 132–134) always returns `1375` regardless of tier. The tier dictionary provides no actual differentiation. `_MEMORY_TIER_LIMITS` (lines 28–45) does differentiate tiers correctly; `_USER_TIER_LIMITS` appears to be an incomplete stub that was never populated.

**Recommended Fix:**
Either populate the tiers with intentionally different limits (e.g. `lite: 800`, `standard: 1375`, `full: 2200`) to match the intent implied by the tier system, or replace the dict with a single module-level constant `_USER_MAX_CHARS = 1375` and remove the dead branching logic in `get_user_max_chars`.

---

### F-06 / F-07 — Inline `datetime` imports inside function bodies in `planning_node.py`

**Severity:** Low
**Category:** Inline Import (non-circular)
**File:** `src/core/orchestration/graph/nodes/planning_node.py:39, 128`

**Description:**
```python
# line 39 — inside _plan_is_resumable()
from datetime import datetime as _dt

# line 128 — inside another function
from datetime import datetime
```
`datetime` is part of the Python standard library and carries no circular-import risk. Both imports belong at the top of the file alongside the other stdlib imports (lines 1–10). Inline stdlib imports obscure the module's dependency surface and add minor per-call overhead.

**Recommended Fix:**
Add `from datetime import datetime` to the top-level import block and remove both inline imports. Use `datetime` (or alias `_dt` if preferred throughout) consistently.

---

### F-08 — `repo_summary.py` defines two local exclusion sets instead of one

**Severity:** Low
**Category:** DRY Violation (internal)
**File:** `src/tools/repo_summary.py:32, 73`

**Description:**
Within a single file, `repo_summary.py` defines `_FW_EXCLUDE` at line 32 and `_EXCLUDE_DIRS` at line 73 with identical content (`{".venv", "venv", "__pycache__", ".git", "node_modules"}`). One definition is used by the framework-detection path and the other by the file-listing path. There is no semantic reason for two separate names with the same value.

**Recommended Fix:**
Define once at module level:
```python
_EXCLUDE_DIRS = frozenset({".venv", "venv", "__pycache__", ".git", "node_modules"})
```
and replace the `_FW_EXCLUDE` reference with `_EXCLUDE_DIRS`. Then apply F-02 to consolidate with `repo_analysis_tools.py`.

---

### F-09 — `subagent_tools._atomic_write_json` — NO ACTION NEEDED

**Severity:** Info
**File:** `src/tools/subagent_tools.py:48–90`

The wrapper is intentionally defensive: it defers the `import` to call-time so tests can
monkeypatch `io_utils` between runs, and carries a local `mkstemp+replace` fallback for
environments where `io_utils` is unavailable. This is the correct pattern for this module.
No fix required.

---

## Clean Files

The following audited files contained no actionable findings under the defined scope:

| File | Notes |
|------|-------|
| `src/tools/_bash_exec.py` | Clean. Security constants well-organised. |
| `src/tools/_registry.py` | Clean. `_BUILTIN_MODULES` list is well-maintained. |
| `src/tools/_security.py` | Clean. Constants clearly named and co-located. |
| `src/tools/bash_security.py` | Clean. AST risk analysis is self-contained. |
| `src/tools/batch_tools.py` | Clean. Short and focused. |
| `src/tools/formatter.py` | Inline `import yaml` is an intentional optional-dep guard — excluded per scope. |
| `src/tools/git_tools.py` | Clean. |
| `src/tools/guardrails.py` | Clean. |
| `src/tools/lsp_tools.py` | Inline `get_lsp_manager` import is a genuine circular-import guard — excluded per scope. |
| `src/core/orchestration/graph/nodes/debug_node.py` | Clean. `TYPE_GUIDANCE` is actively referenced at line 154. |
| `src/core/orchestration/graph/nodes/execution_node.py` | Plugin hook lazy import is a legitimate circular-import guard — excluded per scope. |
| `src/core/orchestration/graph/nodes/perception_node.py` | Plugin hook lazy import is a legitimate circular-import guard — excluded per scope. |

---

## Prioritised Action List

1. **F-01 / F-03** — Fix `_file_io.py` `WorkspaceGuard` fallback immediately; this is a latent `NameError` in non-standard environments.
2. **F-02 / F-08** — Consolidate exclude-dir constants into a shared location before the set diverges further.
3. **F-04** — Capture `datetime.now()` once in `create_state_checkpoint`.
4. **F-05** — Decide on actual tier limits for `_USER_TIER_LIMITS` or remove the dead structure.
5. **F-06 / F-07** — Move `datetime` imports to module level in `planning_node.py`.
6. **F-09** — Informational; address only if `io_utils.atomic_write_json` is confirmed stable.
