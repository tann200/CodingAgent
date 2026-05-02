# Code Quality Audit — Volume 43

**Date:** 2026-05-02
**Files audited:** src/tools/_tool.py, src/tools/_file_io.py, src/tools/_bash_exec.py, src/tools/_edit_tools.py, src/tools/_registry.py, src/tools/file_tools.py, src/tools/git_tools.py, src/tools/memory_tools.py, src/tools/state_tools.py, src/tools/ast_tools.py, src/tools/symbol_reader.py, src/tools/guardrails.py, src/tools/formatter.py, src/tools/lsp_tools.py, src/tools/interaction_tools.py
**Findings:** 14

---

## Summary Table

| ID | Severity | Category | File |
|----|----------|----------|------|
| F-43 | High | DeadCode | src/tools/memory_tools.py:139-140 |
| F-44 | High | MissingImport | src/tools/lsp_tools.py:63 |
| F-45 | High | InconsistentPattern | src/tools/lsp_tools.py:58-63 |
| F-46 | High | DuplicateCode | src/tools/_bash_exec.py:108-205 |
| F-47 | Medium | DuplicateCode | src/tools/_edit_tools.py:40-52 vs _file_io.py:41-47 |
| F-48 | Medium | InlineImport | src/tools/_file_io.py:58,126,206,213,221,291,307 |
| F-49 | Medium | InlineImport | src/tools/_bash_exec.py:228-234,370,457,540 |
| F-50 | Medium | MagicLiteral | src/tools/_file_io.py:602 |
| F-51 | Medium | InlineImport | src/tools/interaction_tools.py:63,152,231 |
| F-52 | Medium | DeadCode | src/tools/_file_io.py:555-558 |
| F-53 | Medium | DuplicateCode | src/tools/ast_tools.py vs symbol_reader.py |
| F-54 | Low | DuplicateCode | src/tools/_bash_exec.py:456-466 vs 480-531 |
| F-55 | Low | UnnecessaryComplexity | src/tools/_tool.py:136-178 |
| F-56 | Low | InlineImport | src/tools/formatter.py:32 |

**Totals:** 3 High · 7 Medium · 4 Low · 0 Info

---

## Findings

### F-43 — Duplicate _MEMORY_FILE assignment (dead code)

**Severity:** High
**Category:** DeadCode
**File:** src/tools/memory_tools.py:59 and 139-140

**Description:**
`_MEMORY_FILE = get_memory_path()` is assigned twice — once at line 59 and again at lines 139-140. The second assignment (lines 139-140) is dead code that shadows the first and runs at import time, causing a redundant filesystem call. The comment "Memory file and limits" preceding line 140 is also duplicated from line 58.

**Fix:** Remove lines 139-140 entirely. The module-level `_MEMORY_FILE` at line 59 is the authoritative definition.

---

### F-44 — SyntaxError in _err() helper (missing comma)

**Severity:** High
**Category:** MissingImport
**File:** src/tools/lsp_tools.py:63

**Description:**
The `_err()` function is missing a comma between dict entries:
```python
return {"ok": False "output": msg}  # SyntaxError: missing comma
```
This causes a `SyntaxError` and prevents the module from loading. The `_ok()` function (line 59) correctly uses `{"ok": True, "output": output}` with a comma.

**Fix:** Add the missing comma:
```python
return {"ok": False, "output": msg}
```

---

### F-45 — Inconsistent return format in LSP tools

**Severity:** High
**Category:** InconsistentPattern
**File:** src/tools/lsp_tools.py:58-63, 70-146

**Description:**
LSP tools use `{"ok": True/False, "output": ...}` as their return format, while every other tool in the codebase uses `{"status": "ok"/"error", ...}` (defined in `_tool.py:254-261`). This inconsistency means:
1. LSP tool results are not compatible with the standard `result["status"]` checks used throughout the codebase.
2. The `_ok()`/`_err()` helpers duplicate the purpose of `ok()`/`err()` in `_tool.py` but with different keys.

**Fix:** Replace `_ok()`/`_err()` with the standard `ok()`/`err()` from `_tool.py`, or refactor LSP tools to return `{"status": "ok", "output": ...}` to match the codebase convention.

---

### F-46 — _DESTRUCTIVE_CMD_PATTERNS rebuilt on every call

**Severity:** High
**Category:** DuplicateCode
**File:** src/tools/_bash_exec.py:116-149

**Description:**
`_DESTRUCTIVE_CMD_PATTERNS` is a large list literal (25+ entries) defined **inside** `_check_shell_flags()` and rebuilt on every call. This is wasteful and makes the function 90+ lines long. The list is a constant that should be hoisted to module level (like `DANGEROUS_PATTERNS` in `_security.py`).

**Fix:** Move `_DESTRUCTIVE_CMD_PATTERNS` to module level as a constant (e.g. `_DESTRUCTIVE_CMD_PATTERNS = [...]`), and reference it inside the function.

---

### F-47 — Duplicated WorkspaceGuard fallback stubs

**Severity:** Medium
**Category:** DuplicateCode
**File:** src/tools/_edit_tools.py:40-52 vs src/tools/_file_io.py:41-47

**Description:**
Both files define their own `WorkspaceGuard` fallback stub when `_workspace_guard` is unavailable. The two stubs are inconsistent:
- `_file_io.py` stub has `is_protected(self, path) -> bool`
- `_edit_tools.py` stub is a context manager with `__enter__`/`__exit__`

Since both files import from `src.tools._workspace_guard`, the fallback should live in one place (e.g., in `_workspace_guard.py` itself, or a shared `_workspace_guard_stub.py`).

**Fix:** Move the fallback stub to a shared location. If the two stubs serve different purposes, document why; otherwise unify them.

---

### F-48 — Repeated inline stdlib imports in _file_io.py

**Severity:** Medium
**Category:** InlineImport
**File:** src/tools/_file_io.py:58,126,206,213,221,291,307

**Description:**
Multiple stdlib and internal imports are done inline inside `write_file()` that should be at module level:
- Line 58: `import fnmatch` (stdlib, no reason to import inline)
- Line 126: `import difflib` (stdlib, also used in other functions)
- Lines 206,213,221,291,307: `from src.tools.tools_config import ...`, `from src.core... import ...` — these are repeated across multiple tool functions in this file

The `difflib` import at line 126 is particularly notable since `difflib` is also used in `_edit_tools.py` and should be a top-of-file import in both files.

**Fix:** Hoist `import difflib`, `import fnmatch` to module level. For internal imports that are repeated, consider whether they can be top-level or at least imported once in the function that uses them most.

---

### F-49 — Repeated inline imports in _bash_exec.py

**Severity:** Medium
**Category:** InlineImport
**File:** src/tools/_bash_exec.py:228-234,370,457,540

**Description:**
`bash()` and `bash_readonly()` contain multiple inline imports of stdlib modules that should be at module level:
- Line 228: `import logging as _logging` (already imported as `_logger` at line 30)
- Line 229: `import subprocess` (should be top-level, also used in `check_background_task`)
- Line 230: `import shlex` (should be top-level, used in both `bash()` and `bash_readonly()`)
- Line 231: `import re as _re` (should be top-level, used in both functions)

Additionally, `from src.tools.bash_security import ...` is imported inline at lines 252 and 458 (identical import in both `bash()` and `bash_readonly()`).

**Fix:** Move stdlib imports (`subprocess`, `shlex`, `re`) to module level. For optional imports like `run_sandboxed` and `bash_security`, the inline pattern is somewhat justified but could be documented.

---

### F-50 — Magic literal LIMIT=500 in glob()

**Severity:** Medium
**Category:** MagicLiteral
**File:** src/tools/_file_io.py:602

**Description:**
The `glob()` function defines `LIMIT = 500` as a local constant inside the function body. This is a magic literal that should be a module-level constant (like `_READ_FILE_MAX_CHARS`, `_WRITE_HARD_LINE_LIMIT`, etc. in the same file). This makes it inconsistent with the file's own conventions and hard to tune.

**Fix:** Move to module level:
```python
_GLOB_RESULT_LIMIT = 500
```
And reference it in `glob()`.

---

### F-51 — Repeated get_event_bus import in interaction_tools.py

**Severity:** Medium
**Category:** InlineImport
**File:** src/tools/interaction_tools.py:63,152,231

**Description:**
`from src.core.orchestration.event_bus import get_event_bus` is imported inline three times in the same module (lines 63, 152, 231), once in each tool function. This is unnecessary repetition.

**Fix:** Hoist to module level:
```python
from src.core.orchestration.event_bus import get_event_bus
```
(Leading underscore prefix not needed since it's a module-level import.)

---

### F-52 — sandbox_info() is dead code

**Severity:** Medium
**Category:** DeadCode
**File:** src/tools/_file_io.py:555-558

**Description:**
`sandbox_info()` is defined without a `@tool` decorator and is not exported via `file_tools.py`'s `_IMPORT_MAP`. It appears to be unused dead code. The function also returns an inconsistent format (`{"workdir": ...}`) without a `"status"` key, unlike every other tool in the file.

**Fix:** Remove `sandbox_info()` if it's not needed. If it's intended to be a tool, add the `@tool` decorator and export it via `file_tools.py`.

---

### F-53 — Duplicated AST symbol extraction (ast_tools.py vs symbol_reader.py)

**Severity:** Medium
**Category:** DuplicateCode
**File:** src/tools/ast_tools.py vs src/tools/symbol_reader.py

**Description:**
Both files implement AST-based symbol extraction from Python files with significant overlap:
- `ast_tools.py:ast_list_symbols()` and `symbol_reader.py:parse_symbols()` both walk AST to find FunctionDef, ClassDef, and Assign nodes.
- `symbol_reader.py` appears to be an older/alternative implementation that is not decorated with `@tool` and doesn't follow the same patterns (no `PermissionKind`, no `workdir` resolution via `_safe_resolve` in all methods).

The `symbol_reader.py` module also has `read_function()` and `read_class()` (lines 110-116) which are trivial one-line wrappers around `read_symbol()`.

**Fix:** Consolidate into `ast_tools.py`. Either remove `symbol_reader.py` or refactor it to use `ast_tools` internally. The `SymbolReader` class wrapper doesn't add value since the tools already take `path` and `workdir` parameters directly.

---

### F-54 — Massive code duplication between bash() and bash_readonly()

**Severity:** Low
**Category:** DuplicateCode
**File:** src/tools/_bash_exec.py:208-423 vs 425-593

**Description:**
`bash()` (lines 208-423) and `bash_readonly()` (lines 425-593) share nearly identical implementations of:
- Gate 1: Shell-operator / metacharacter block (lines 236-246 vs 447-454)
- Gate 2: AST-level bash security analysis (lines 251-261 vs 456-467)
- Gate 3: Restricted-command check (lines 276-289 vs 481-487)
- Gate 4: Archive/inplace-edit flag check (lines 300-303 vs 530-533)
- Gate 5: Tier allowlist / SAFE_COMMANDS check (lines 321-340 vs 489-495)
- Timeout/error handling (lines 399-422 vs 570-593)

About 80+ lines are near-identical between the two functions.

**Fix:** Extract shared gate checks into helper functions that both `bash()` and `bash_readonly()` call. For example:
```python
def _run_bash_with_gates(cmd_parts, first_cmd, cmd_lower, workdir, timeout, readonly=False):
    # Run gates 1-5
    # Execute via sandbox
    # Handle errors
```
This would significantly reduce duplication.

---

### F-55 — Deeply nested try/except in to_openai_schema()

**Severity:** Low
**Category:** UnnecessaryComplexity
**File:** src/tools/_tool.py:136-178

**Description:**
The `to_openai_schema()` method has 4 levels of nested try/except blocks (lines 136-178) for dynamically populating parameter enums. The logic for `load_skill`, `delegate_task`, and toolset enums is three separate blocks that follow the same pattern.

**Fix:** Extract each dynamic enum population into a separate helper method:
```python
def _populate_dynamic_enums(self, params):
    self._populate_skill_enum(params)
    self._populate_delegate_enum(params)
    self._populate_toolset_enum(params)
```
This would make `to_openai_schema()` easier to follow and each helper easier to test.

---

### F-56 — yaml imported inline in formatter.py

**Severity:** Low
**Category:** InlineImport
**File:** src/tools/formatter.py:32

**Description:**
`import yaml` is done inline inside `_load_config()` (line 32). While `yaml` is an optional dependency, the pattern used here is inconsistent with how other optional imports are handled in the codebase (e.g., guarded by a top-level try/except with a fallback).

**Fix:** Move to a top-level conditional import:
```python
try:
    import yaml
except ImportError:
    yaml = None
```
Then check `if yaml is not None` in `_load_config()`. This makes the optional dependency explicit and avoids importing on every call to `_load_config()`.

