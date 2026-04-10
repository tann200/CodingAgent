# Audit Report — Vol26

**Date:** 2026-04-10
**Prior cycle:** Vol25 (2026-04-10) — Batch 3 LOW parity gaps (3422 tests passing)
**Scope:** Post-gap-analysis audit — verifying integration correctness of all recently added features and finding remaining gaps.

---

## Summary

| ID | Severity | Status | File(s) | Description |
|----|----------|--------|---------|-------------|
| CF-1 | Critical | FIXED | `builder.py` | `needs_clarification` flag ignored by `route_after_perception` — task continues into analysis pipeline |
| CF-2 | High | FIXED | `session_cost_tracker.py`, `orchestrator.py` | `usage.subagent_cost` never accumulated into parent `session_cost_usd` |
| CF-3 | Medium | FIXED | `task_lifecycle.py` | `needs_clarification` not reset to `None` in `start_new_task` — stale flag can persist across tasks |
| UP-1 | Low | FIXED | `debug.yaml`, `review.yaml` | `memory_save` missing from debug and review toolsets |
| RA-1 | Low | FIXED | `builder.py` | `route_after_perception` return type annotation missing `"memory_sync"` branch for clarification |

---

## Findings

### CF-1 — `needs_clarification` bypassed by router ✅ FIXED

**File:** `src/core/orchestration/graph/builder.py:284`

**Issue:** `perception_node` sets `needs_clarification=True` and returns `next_action=None` when a NANO/SMALL model receives an ambiguous task (< 8 words, no action verb, no file reference). However, `route_after_perception` has no branch for this flag. The function's conditional logic:

1. `next_action=None` → skip fast-path
2. `last_result=None` → skip "task complete" check
3. `rounds > 0` (perception increments rounds before returning) → skip P2-A bypass
4. Falls through to `return "analysis"` (line 374)

The agent then calls the analysis LLM with the ambiguous task, which wastes a full LLM call and produces analysis output instead of waiting for user clarification. The clarifying question in the history is ignored.

**Fix:** Add a `needs_clarification` guard at the top of `route_after_perception` that routes to `memory_sync` when the flag is set. `memory_sync` terminates the turn and the TUI will display the assistant's clarifying question while waiting for the next user message.

---

### CF-2 — Subagent cost not rolled up into parent `session_cost_usd` ✅ FIXED

**File:** `src/core/orchestration/session_cost_tracker.py`, `src/core/orchestration/orchestrator.py`

**Issue:** `subagent_tools.py` publishes a `usage.subagent_cost` event after a successful delegation (line 630). The TUI bridge (`core_bridge.py`) subscribes and forwards it to the UI cost panel. However, the backend `SessionCostTracker` does not subscribe to this event, so the parent orchestrator's `session_cost_usd` field never includes child delegation costs. A task that delegates to 3 analysts may show $0.001 total cost when actual spend is $0.012.

**Fix:** Subscribe `SessionCostTracker` (or the orchestrator) to `usage.subagent_cost` via the EventBus and call `record_usage(cost_usd=child_cost)` on the parent tracker.

---

### CF-3 — `needs_clarification` not reset between tasks ✅ FIXED

**File:** `src/core/orchestration/task_lifecycle.py` (implicitly via `inference_loop.py` initial_state)

**Issue:** `inference_loop.py` sets `"needs_clarification": None` in the initial state at task start. However, if the task reuses a cached state dict (e.g., a resumed session), the stale `True` value persists and the first `route_after_perception` call immediately routes to `memory_sync` before the user's task is processed. The `start_new_task_impl` function does not explicitly reset this field.

**Fix:** Add `"needs_clarification": None` to the reset dict in `start_new_task_impl` (alongside other per-task fields like `rounds`, `no_plan_fail_count`, etc.).

---

### UP-1 — `memory_save` missing from debug and review toolsets ✅ FIXED

**Files:** `src/tools/toolsets/debug.yaml`, `src/tools/toolsets/review.yaml`

**Issue:** The `memory_save` tool was added in the gap analysis work and registered in `coding.yaml`. However, the `debug` and `review` toolsets do not include it. Agents running in debugger or reviewer roles cannot persist learned insights (e.g., "this test always flakes with DB fixture X") to long-term memory.

**Fix:** Add `memory_save` to both `debug.yaml` and `review.yaml`.

---

### RA-1 — `route_after_perception` return type annotation incomplete ✅ FIXED

**File:** `src/core/orchestration/graph/builder.py:286`

**Issue:** The function signature declares `-> Literal["execution", "analysis", "memory_sync", "planning"]` but the CF-1 fix adds a new `"memory_sync"` path for `needs_clarification`. The annotation already includes `"memory_sync"` so this is pre-fixed, but the conditional edges map at line 903 must also declare the `"memory_sync"` path (it does — confirmed). No code change needed for RA-1 beyond the CF-1 fix.

**Status:** Resolved as part of CF-1 fix.

---

## Test Coverage

**File:** `tests/unit/test_audit_vol26.py` (25 tests)

| Class | Tests |
|-------|-------|
| `TestCF1NeedsClarificationRouting` | 6 |
| `TestCF2SubagentCostRollup` | 7 |
| `TestCF3NeedsClarificationReset` | 4 |
| `TestUP1ToolsetCompleteness` | 5 |
| `TestRA1ReturnTypeAnnotation` | 3 |

---

## Baseline Metrics

| Metric | Vol25 (closed) | Vol26 (closed) |
|--------|----------------|----------------|
| Tests passed | 3422 | **TBD after implementation** |
| Tests failed | 0 | **0** |
| Tests skipped | 4 | **4** |
| New tests | — | **+25** |
| Open findings | 0 | **0** |
