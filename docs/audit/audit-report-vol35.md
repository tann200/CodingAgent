# Vol35 Audit Report

**Date:** 2026-05-01
**Scope:** Full `src/` codebase — DRY, correctness, and Python best-practice findings

---

## Summary

| Severity | ID | Finding | Status |
|----------|----|---------|--------|
| Critical | C-1 | Dual tool registries (`registry.py` + `tool_registry.py`) with incompatible key schemas | Deferred — high-risk, needs test coverage first |
| Critical | C-2 | Schema generation duplicated across two files | Deferred — high-risk |
| Critical | C-3 | Permission gate logic duplicated across `permission_gateway.py` + `tool_execution_service.py` | Deferred — high-risk |
| High | C-4 | Inline `mkstemp` fallback blocks in `orchestrator_helpers.py` (3 occurrences) | **Fixed** |
| High | H-2 | `_safe_resolve` thin wrapper duplicated in `_file_io.py` and `_edit_tools.py` | **Fixed** |
| High | H-3 | `_verify_new_content` / `_verify_write_candidate` duplicated verify logic | **Fixed** |
| High | H-4 | `_PERM_ORDER` duplicated in `permission_gateway.py` and `tool_execution_service.py` | **Fixed** |
| High | H-5 | `PermissionTable` opened/closed a new SQLite connection per call (thread-unsafe) | **Fixed** |
| High | H-6 | `_RECOVERY_CAPS` defined inside two functions in `builder.py` | **Fixed** |
| High | H-7 | 14 async lambda wrappers in `compile_agent_graph()` adding pure overhead | **Fixed** |
| Medium | M-1 | Telemetry publish block copy-pasted twice in `adapter_wrappers.py` | **Fixed** |
| Medium | M-2 | Unreachable dead code after `return []` in `session_store.read_recent_decisions` | **Fixed** |
| Medium | M-4 | Self-assignment re-exports in `orchestrator.py` (`X = X`) | **Fixed** |
| Medium | M-5 | `# noqa: F401` suppression on re-exports instead of `__all__` | **Fixed** |
| Medium | M-6 | Silent `except Exception: pass` fail-open in permission gate methods | **Fixed** |
| Medium | M-7 | Inline `_fallback` timeout dict copy in `orchestrator_helpers.py` (diverged from `tool_registry`) | **Fixed** |
| Medium | M-8 | Backward-compat alias wrappers in `builder.py` | No action — intentional, documented |

---

## Fixed Findings — Detail

### C-4 — Inline mkstemp fallbacks removed (`orchestrator_helpers.py`)
All three occurrences of inline `tempfile.mkstemp` + manual write + `os.replace` blocks replaced with calls to `atomic_write_json` from `src/core/io_utils.py`. The authoritative implementation (including its own fallback) now lives in one place.

### H-2 / H-3 — `_safe_resolve` and verify helpers consolidated
- `safe_resolve` in `src/tools/_path_utils.py` now accepts `None` workdir (falls back to `Path.cwd()`), eliminating the need for thin wrappers in each tool module.
- `_verify_new_content` (`_edit_tools.py`) and `_verify_write_candidate` (`_file_io.py`) replaced by shared `verify_candidate_content()` in new module `src/tools/_lint_verify.py`.
- Both tool modules now import from `_lint_verify`; the duplicate implementations are removed.
- Unused `re` import removed from `_file_io.py`.

### H-4 — `PERM_ORDER` canonicalised in `tool_constants.py`
Local `_PERM_ORDER` dicts removed from `permission_gateway.py` and `tool_execution_service.py`. Both now import `PERM_ORDER` from `src/core/orchestration/tool_constants.py` (a leaf module with no internal imports, avoiding circular deps).

### H-5 — Thread-local SQLite connections in `PermissionTable`
`_connect()` removed; replaced by `_get_connection()` using `threading.local`. WAL mode enabled at connection creation. Per-call `conn.close()` removed — connections are reused within each thread for the lifetime of the process.

### H-6 — `_RECOVERY_CAPS` hoisted to module level (`builder.py`)
The constant was defined identically inside both `should_after_debug` and `should_after_replan`. Moved to module scope and both functions reference the shared constant.

### H-7 — Async lambda wrappers removed (`builder.py`)
14 `async lambda state: await node_fn(state)` wrappers removed from `compile_agent_graph()`. LangGraph accepts coroutine functions directly; the wrappers were pure overhead. `wait_for_user_node` import moved to module level.

### M-1 — Telemetry publish extracted to `_publish_telemetry` (`adapter_wrappers.py`)
The 20-line telemetry block was copy-pasted twice inside `generate()`. Extracted into a private `_publish_telemetry(out)` method on `AdapterWrapper`; both call sites replaced with a single method call.

### M-2 — Unreachable code fixed (`session_store.py`)
`read_recent_decisions` had a `return []` inside the `except` block followed by unreachable delegation logic. Restructured so the `except` falls through to the delegate check and final `return []`.

### M-4 / M-5 — Re-exports cleaned up (`orchestrator.py`)
Self-assignments (`PERMISSION_REQUIRED_TOOLS = PERMISSION_REQUIRED_TOOLS`, `_write_permission_audit as _write_permission_audit`) and `# noqa: F401` suppressions replaced with a proper `__all__` list. All names are now imported once and declared in `__all__` for explicit re-export.

### M-6 — Fail-open exceptions now logged
Three `except Exception: pass` blocks in `tool_execution_service.py` (`_check_permission_mode`, `_check_explore_mode`, `_check_plan_mode`) and one in `permission_gateway.py` now log a `WARNING` with the exception message before failing open. Operational failures will appear in logs instead of being silently swallowed.

### M-7 — Inline fallback timeout dict removed (`orchestrator_helpers.py`)
The `_fallback` dict (14 entries) was a stale copy of data owned by `tool_registry`. Replaced with a single `logger.warning` + `return 30` (the safe default). `tool_registry.get_tool_timeout` remains the authoritative source.

---

## Deferred Findings

### C-1 — Dual tool registries
`src/tools/registry.py` uses key `"func"`; `src/core/orchestration/tool_registry.py` uses key `"fn"`. Mixing them silently yields `None`. Merging requires careful test coverage to avoid breaking tool dispatch across all execution paths. Deferred to a dedicated session.

### C-2 — Schema generation duplication
Two files independently generate JSON tool schemas. Merging requires understanding all call sites. Deferred.

### C-3 — Permission gate duplication
`permission_gateway.py` and `tool_execution_service.py` both contain partial permission logic. Consolidation is a significant refactor touching the security-critical path. Deferred.
