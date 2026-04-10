# file_tools.py Refactoring Plan

**File:** `src/tools/file_tools.py`
**Current size:** 1,837 lines
**Target:** No file exceeds ~400 lines; each module owns exactly one responsibility.

---

## Problem Statement

`file_tools.py` is the second-largest file in the codebase after `orchestrator.py` (now
refactored). It bundles five distinct responsibilities that have grown together over time:

1. **Diff preview gate** — threading events, publish/resolve, blocking gate state
   (`_publish_diff_preview`, `register_preview_gate`, `resolve_preview_gate`,
   `_pending_previews`, `_preview_rejected`, `_preview_gate_lock`)
2. **Bash execution** — two security-gated shell runners with tier-1/2/3 allowlists,
   background process support, output truncation
   (`bash`, `bash_readonly`, `check_background_task`, `_truncate_bash_output`,
   `_check_shell_flags`)
3. **File I/O** — read, write, list, delete, rename, chunked read, tail, binary read,
   create-directory
   (`read_file`, `write_file`, `list_dir`, `delete_file`, `rename_file`,
   `read_file_chunk`, `tail_log_file`, `create_directory`, `read_file_bytes`, `glob`)
4. **Edit tools** — patch-based edit, line-range edit, atomic string replacement,
   multi-edit, fuzzy matcher
   (`edit_file`, `edit_by_line_range`, `edit_file_atomic`, `multiedit`, `_fuzzy_find`)
5. **Shared plumbing** — module-level constants, `_safe_resolve` wrapper, `DEFAULT_WORKDIR`

---

## Guiding Principles

1. **Public API does not change** — all names currently importable from `file_tools` stay
   importable from `file_tools` via backward-compatible re-exports.
2. **No new circular imports** — new modules sit *below* `file_tools.py` in the dependency
   graph. `file_tools.py` imports from them; they never import back.
3. **`_safe_resolve` stays in `file_tools`** — tests import it directly as
   `from src.tools.file_tools import _safe_resolve`. Do not move it.
4. **`resolve_preview_gate` stays importable from `file_tools`** — two production call
   sites patch `src.tools.file_tools.resolve_preview_gate` and two tests patch the same.
   Re-export from `file_tools` after moving the implementation.
5. **Private constants stay re-exported from `file_tools`** — tests import
   `_BASH_STDOUT_MAX_TOKENS` and `_BASH_STDERR_MAX_TOKENS` directly from `file_tools`.
6. **Tests must stay green after every phase** — run the full suite after each phase;
   no phase lands with a regression.

---

## Responsibility Map (current file)

| Lines | Symbol(s) | Responsibility | Destination |
|---|---|---|---|
| 1–41 | imports, `WorkspaceGuard` fallback | shared plumbing | stays in `file_tools.py` |
| 44–93 | `_publish_diff_preview`, `register_preview_gate`, `resolve_preview_gate`, `_pending_previews`, `_preview_rejected`, `_preview_gate_lock` | diff preview gate | `_diff_gate.py` |
| 96–117 | `DEFAULT_WORKDIR`, `_BASH_*`, `_READ_FILE_*`, `_WRITE_*`, `_EDIT_*` | constants | stays in `file_tools.py` (re-exported from submodules as needed) |
| 120–178 | `_truncate_bash_output` | bash output handling | `_bash_exec.py` |
| 181–232 | `_check_shell_flags` | bash flag validation | `_bash_exec.py` |
| 235–290 | `_fuzzy_find` | fuzzy text matching | `_edit_tools.py` |
| 293–295 | `_safe_resolve` | path resolution wrapper | stays in `file_tools.py` |
| 298–472 | `write_file` | file I/O | `_file_io.py` |
| 475–547 | `read_file` | file I/O | `_file_io.py` |
| 550–571 | `_OS_JUNK`, `list_dir` | file I/O | `_file_io.py` |
| 574–635 | `delete_file` | file I/O | `_file_io.py` |
| 638–698 | `rename_file` | file I/O | `_file_io.py` |
| 701–702 | `sandbox_info` | utility | `_file_io.py` |
| 705–729 | `read_file_chunk` | file I/O | `_file_io.py` |
| 732–831 | `edit_file` | edit tools | `_edit_tools.py` |
| 834–1088 | `bash` | bash execution | `_bash_exec.py` |
| 1090–1255 | `bash_readonly` | bash execution | `_bash_exec.py` |
| 1258–1294 | `check_background_task` | bash execution | `_bash_exec.py` |
| 1297–1405 | `edit_by_line_range` | edit tools | `_edit_tools.py` |
| 1408–1457 | `glob` | file I/O | `_file_io.py` |
| 1460–1633 | `edit_file_atomic` | edit tools | `_edit_tools.py` |
| 1636–1761 | `multiedit` | edit tools | `_edit_tools.py` |
| 1764–1785 | `tail_log_file` | file I/O | `_file_io.py` |
| 1788–1799 | `create_directory` | file I/O | `_file_io.py` |
| 1802–1837 | `read_file_bytes` | file I/O | `_file_io.py` |

---

## Target Module Layout

```
src/tools/
├── file_tools.py          # ~120 lines — thin re-export hub + shared plumbing
│                          #   (_safe_resolve, DEFAULT_WORKDIR, constants, WorkspaceGuard)
├── _diff_gate.py          # ~80 lines  — diff preview publish/gate state
├── _bash_exec.py          # ~480 lines — bash(), bash_readonly(), check_background_task()
│                          #              + _truncate_bash_output, _check_shell_flags
├── _file_io.py            # ~580 lines — read/write/list/delete/rename/glob/tail/mkdir
└── _edit_tools.py         # ~560 lines — edit_file, edit_by_line_range, edit_file_atomic,
                           #              multiedit, _fuzzy_find
```

All public names continue to be importable from `src.tools.file_tools`.

---

## Known Test Constraints

These constraints must be respected. Violating any of them breaks tests.

### Direct private-symbol imports (must remain importable from `file_tools`)

| Symbol | Test file | Notes |
|---|---|---|
| `_safe_resolve` | `test_file_tools_misc.py:49`, `test_tool_fixes.py:256` | Must stay defined in `file_tools` |
| `_BASH_STDOUT_MAX_TOKENS` | `test_bash_fixes_regression.py:217` | Re-export from `file_tools` |
| `_BASH_STDERR_MAX_TOKENS` | `test_bash_fixes_regression.py:229` | Re-export from `file_tools` |

### Patch targets (must remain patchable at the `file_tools` namespace)

| Patch target | Test file | Resolution |
|---|---|---|
| `src.tools.file_tools.resolve_preview_gate` | `test_d10_services.py:235,243` | Keep re-exported name in `file_tools`; ensure `_diff_gate.py` also exports it so the implementation module can be patched in future |

### `inspect.getsource` checks

| Test | What it checks | Impact |
|---|---|---|
| `test_tool_fixes.py:256` | Source of `file_tools._safe_resolve` | `_safe_resolve` must stay in `file_tools.py` itself (not delegated away) |
| `test_tool_safety_node_caching_plan_contracts.py:509` | `"safe_resolve"` in source of `patch_tools.generate_patch` | Unrelated to this refactor |
| `test_bash_planning_threading_bug_documentation.py:529,538` | `"_safe_resolve"` in source of `multi_file_summary` / `generate_patch` | Unrelated to this refactor |

---

## Implementation Phases

### Phase 1 — Extract `_diff_gate.py` (~80 lines)

**Move:**
- `_pending_previews`, `_preview_rejected`, `_preview_gate_lock`
- `_publish_diff_preview`
- `register_preview_gate`
- `resolve_preview_gate`

**In `file_tools.py`:** replace with:
```python
from src.tools._diff_gate import (
    _publish_diff_preview,
    register_preview_gate,
    resolve_preview_gate,
    _pending_previews,
    _preview_rejected,
    _preview_gate_lock,
)
```

**Risk:** `test_d10_services.py` patches `src.tools.file_tools.resolve_preview_gate`.
The re-export in `file_tools` makes the patch land on the re-exported name, not the
implementation. This is fine as long as the re-export is a direct reference (not a
`from ... import *`). Verify after Phase 1.

**Lines saved in `file_tools.py`:** ~50

---

### Phase 2 — Extract `_bash_exec.py` (~480 lines)

**Move:**
- `_BASH_STDOUT_MAX`, `_BASH_STDOUT_MAX_TOKENS`, `_BASH_STDERR_MAX`, `_BASH_STDERR_MAX_TOKENS`
- `_truncate_bash_output`
- `_check_shell_flags`
- `bash`
- `bash_readonly`
- `check_background_task`

**In `file_tools.py`:** replace with:
```python
from src.tools._bash_exec import (
    bash,
    bash_readonly,
    check_background_task,
    _truncate_bash_output,
    _check_shell_flags,
    _BASH_STDOUT_MAX,
    _BASH_STDOUT_MAX_TOKENS,
    _BASH_STDERR_MAX,
    _BASH_STDERR_MAX_TOKENS,
)
```

**Risk:** `bash()` references `register_preview_gate` and `_preview_gate_lock` from the
current module namespace. After Phase 1, `_bash_exec.py` must import these from
`_diff_gate`. Double-check that `write_file` and `edit_file_atomic` in `_file_io.py` /
`_edit_tools.py` also import from `_diff_gate` (not from `file_tools`).

**Lines saved in `file_tools.py`:** ~450

---

### Phase 3 — Extract `_file_io.py` (~580 lines)

**Move:**
- `_OS_JUNK`
- `_READ_FILE_MAX_CHARS`, `_READ_FILE_MAX_LINE`
- `_WRITE_HARD_LINE_LIMIT`, `_WRITE_WARN_LINE_LIMIT`
- `write_file`
- `read_file`
- `list_dir`
- `delete_file`
- `rename_file`
- `sandbox_info`
- `read_file_chunk`
- `glob`
- `tail_log_file`
- `create_directory`
- `read_file_bytes`

**In `file_tools.py`:** re-export all public names. `write_file` imports
`_publish_diff_preview`, `register_preview_gate`, `_preview_gate_lock`, `_preview_rejected`
from `_diff_gate`; `_safe_resolve` from `file_tools` creates a circular import.

**Circular import solution:** `_file_io.py` imports `_safe_resolve` from
`src.tools._path_utils` directly (the underlying implementation) rather than from
`file_tools`. The `_safe_resolve` wrapper in `file_tools` remains the public symbol for
test compatibility.

**Lines saved in `file_tools.py`:** ~600

---

### Phase 4 — Extract `_edit_tools.py` (~560 lines)

**Move:**
- `_EDIT_NET_CHANGE_WARN`
- `_fuzzy_find`
- `edit_file`
- `edit_by_line_range`
- `edit_file_atomic`
- `multiedit`

**In `file_tools.py`:** re-export all public names.

**Same circular import solution as Phase 3:** use `src.tools._path_utils.safe_resolve`
directly inside `_edit_tools.py`.

**Lines saved in `file_tools.py`:** ~560

---

### Phase 5 — Trim `file_tools.py` to hub

After all four extractions, `file_tools.py` becomes a ~120-line re-export hub containing:

- `WorkspaceGuard` fallback + real import
- `DEFAULT_WORKDIR`
- `_safe_resolve` (thin wrapper — must stay here for test compatibility)
- All `from src.tools._diff_gate import ...`
- All `from src.tools._bash_exec import ...`
- All `from src.tools._file_io import ...`
- All `from src.tools._edit_tools import ...`
- `__all__` listing every re-exported public name

---

## Execution Rules

1. One phase at a time. After each phase: run tests, confirm green, then proceed.
2. Test command:
   ```
   python -m pytest tests/unit/ --timeout=20 --ignore=tests/unit/test_subagent_tools.py -p no:sugar 2>&1 | tail -3
   ```
3. Baseline: **3191 passed, 4 skipped, 0 failed**. Any regression blocks the phase.
4. `test_subagent_tools.py::TestDelegateTask::test_delegate_task_valid_roles` is a
   pre-existing failure — ignore it.
5. Pre-existing LSP errors in `tui/src/ui/core_bridge.py` are intentional — do not fix.
6. After Phase 1, verify the `resolve_preview_gate` patch target still works by running:
   ```
   python -m pytest tests/unit/test_d10_services.py -p no:sugar 2>&1 | tail -5
   ```
7. After Phase 2, verify bash token-cap tests still pass:
   ```
   python -m pytest tests/unit/test_bash_fixes_regression.py -p no:sugar 2>&1 | tail -5
   ```

---

## Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Circular import (`_file_io` → `file_tools._safe_resolve`) | High | Import from `_path_utils.safe_resolve` directly inside submodules |
| `resolve_preview_gate` patch lands on wrong namespace | Medium | Ensure `file_tools` re-export is a named binding, not a `*` import; run `test_d10_services.py` after Phase 1 |
| `inspect.getsource(file_tools._safe_resolve)` fails | Low | `_safe_resolve` stays defined in `file_tools.py` |
| `_BASH_STDOUT_MAX_TOKENS` / `_BASH_STDERR_MAX_TOKENS` import breaks | Low | Explicit re-exports in `file_tools.py` after Phase 2 |
| `bash()` references gate state from wrong module | Medium | After Phase 2, `_bash_exec.py` must import `_preview_gate_lock`, `_preview_rejected` from `_diff_gate` |

---

## Completion Criteria

- `src/tools/file_tools.py` ≤ 130 lines
- `src/tools/_diff_gate.py` ≤ 90 lines
- `src/tools/_bash_exec.py` ≤ 500 lines
- `src/tools/_file_io.py` ≤ 600 lines
- `src/tools/_edit_tools.py` ≤ 580 lines
- Full test suite: **3191 passed, 4 skipped, 0 failed**
- All public names importable from `src.tools.file_tools` without change
