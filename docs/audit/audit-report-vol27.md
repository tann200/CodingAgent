# Audit Report — Vol27

**Date:** 2026-04-10
**Prior cycle:** Vol26 (2026-04-10) — `needs_clarification` routing, subagent cost rollup, task reset (3422 tests passing)
**Scope:** Post-Vol26 deep audit — pipeline correctness, state management, cache invalidation.

---

## Summary

| ID | Severity | Status | File(s) | Description |
|----|----------|--------|---------|-------------|
| CF-1 | High | FIXED | `debug_node.py` | `model = "None"` (string) sent to adapter when `adapter.models` is empty — same bug fixed in `perception_node` not backported |
| CF-2 | Medium | FIXED | `verification_node.py` | Deletion failure path returns without `verification_passed=False` — `evaluation_node` incorrectly marks task complete |
| UP-1 | Low | FIXED | `planning_node.py` | Successful LLM-generated plan doesn't set `task_decomposed=True` — forces LLM replan on every replan cycle |
| UP-2 | Low | FIXED | `analysis_node.py`, `task_lifecycle.py` | `_REPO_SUMMARY_CACHE` never cleared on `start_new_task` — stale repo summary served across tasks |

---

## Findings

### CF-1 — `debug_node` sends `model="None"` (string) to adapter ✅ FIXED

**File:** `src/core/orchestration/graph/nodes/debug_node.py:167–168`

**Issue:** `debug_node` initialises `provider = "None"` and `model = "None"` (Python string literals) before conditionally populating them from the adapter. When `orchestrator.adapter` exists but `orchestrator.adapter.models` is empty or falsy (e.g., during startup or with partially initialised adapters), the `model` variable stays as the string `"None"`. This string is then passed to `call_model(messages, provider=provider, model=model, ...)`.

The adapter layer in `openai_compat_adapter.py` does not special-case the string `"None"`, so it is forwarded as the literal model identifier to the upstream LLM API. Responses arrive for the wrong model (or an API error is returned if `"None"` is not a valid model name). The same bug was discovered and fixed in `perception_node.py` (lines 662–663 now use `model = None`), but was not backported to `debug_node`.

**Affected path:** Any debug cycle where the adapter's `models` list is empty.

**Fix:** Change `provider = "None"` → `provider = None` and `model = "None"` → `model = None` in `debug_node.py`. The `call_model` function already handles `None` by falling back to the active provider default.

---

### CF-2 — Deletion failure silently treated as verification-passed ✅ FIXED

**File:** `src/core/orchestration/graph/nodes/verification_node.py:69–75`

**Issue:** When `delete_file` is called and the file still exists after the tool returns, `verification_node` returns early with:

```python
return {
    "verification_result": {
        "deletion_verification": "FAILED",
        "error": f"File still exists: {deleted_path}",
        "path": deleted_path,
    }
}
```

This return dict does **not** include `"verification_passed": False`. The evaluation_node then:

1. Reads `_state_vp = state.get("verification_passed")` → `None` (field absent from this return)
2. Falls into the recompute branch and iterates over `("tests", "linter", "syntax", "js_tests", "ts_check", "eslint")`
3. None of these keys are present in the result dict (only `deletion_verification` is)
4. `verification_passed = True` — the failed deletion is silently ignored
5. `evaluation_node` returns `evaluation_result = "complete"` — task marked done despite the failure

The successful deletion path (lines 80–85) has the same omission but is harmless: recomputation also finds no failures and correctly concludes `verification_passed = True`.

**Fix:** Add `"verification_passed": False` to the failed-deletion return dict.

---

### UP-1 — `planning_node` LLM path doesn't set `task_decomposed=True` ✅ FIXED

**File:** `src/core/orchestration/graph/nodes/planning_node.py` (~line 575)

**Issue:** The successful LLM-based DAG generation return dict is missing `"task_decomposed": True`:

```python
return {
    "current_plan": steps,
    "current_step": 0,
    "plan_dag": {"steps": steps},
    "execution_waves": waves,
    "current_wave": 0,
    "plan_attempts": plan_attempts,
    "plan_mode_approved": None,
    "affected_files": _extract_affected_files(steps),
    # ← task_decomposed missing
}
```

Without this flag, if `planning_node` is re-entered (e.g., replan cycle or plan_validator rejection), the guard at line 171:
```python
if task_decomposed and current_plan and current_step < len(current_plan):
    return fast-path using existing plan
```
is never taken. The node skips past the existing plan, falls through to the LLM generation block, and issues a redundant LLM call to regenerate the same plan. The fallback path at the bottom (lines 609–622) also omits `task_decomposed=True`, compounding the issue for multi-replan scenarios.

The plan-resume path (line 153) correctly sets `"task_decomposed": True` — demonstrating the intent.

**Fix:** Add `"task_decomposed": True` to both the LLM-success return (line ~575) and the fallback pass-through return (line ~609).

---

### UP-2 — `_REPO_SUMMARY_CACHE` not cleared on `start_new_task` ✅ FIXED

**File:** `src/core/orchestration/graph/nodes/analysis_node.py:26–187`, `src/core/orchestration/task_lifecycle.py`

**Issue:** `analysis_node.py` defines `_REPO_SUMMARY_CACHE` (a process-global dict keyed by resolved working directory path) and a `clear_repo_summary_cache()` utility function (PERF-1 comment). However, `start_new_task_impl()` in `task_lifecycle.py` only calls `ContextBuilder.clear_cache()` — it does **not** call `clear_repo_summary_cache()`.

Consequence: across tasks in the same process, `analysis_node` serves the stale repo summary from the first task. If task 1 adds or removes files and task 2 then runs, task 2's analysis phase receives an outdated project structure. The issue is subtle: a user seeing the agent miss a newly-created file might not suspect a stale cache.

The `_INDEXED_DIRS` cache (for `index_repository`) has proper mtime-based invalidation. `_REPO_SUMMARY_CACHE` has no such guard.

**Fix:** Call `clear_repo_summary_cache(working_dir=str(orch.working_dir))` in `start_new_task_impl()` after the `ContextBuilder.clear_cache()` call. The per-directory eviction (rather than full clear) preserves multi-project cache efficiency.

---

## Test Coverage

**File:** `tests/unit/test_audit_vol27.py` (24 tests)

| Class | Tests |
|-------|-------|
| `TestCF1DebugNodeModelString` | 6 |
| `TestCF2DeletionVerificationPassed` | 6 |
| `TestUP1TaskDecomposedFlag` | 6 |
| `TestUP2RepoCacheClear` | 6 |

---

## Baseline Metrics

| Metric | Vol26 (closed) | Vol27 (closed) |
|--------|----------------|----------------|
| Tests passed | 3422 | **3476** |
| Tests failed | 0 | **0** |
| Tests skipped | 4 | **4** |
| New tests | — | **+24** |
| Open findings | 0 | **0** |
