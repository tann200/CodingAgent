# Audit Report — Vol22

**Date:** 2026-04-09
**Auditor:** OpenCode agent (claude-sonnet-4.6)
**Scope:** Full-spectrum architectural audit — all 15 required categories.
**Baseline:** 3206 passed, 4 skipped, 0 failed, 4 warnings. Test collection: 3210 tests.
**Previous audit:** Vol21 — all 5 Phase 1 items closed (WF-VOL21-1/2/3, BUG-VOL21-1, TEST-VOL21-1). 4 Phase 2/3 items remain open (ARCH-VOL21-1/2, OE-VOL21-1/2).

---

## Executive Summary

Vol22 found **0 Critical, 0 High**, 2 Medium, 3 Low severity findings. All 5 Vol22 items have been fixed in the same session.

Post-fix baseline: **3206 passed, 4 skipped, 0 failed** — no regressions.

---

## 1. Architecture

### ARCH-VOL21-1 — GraphFactory Subgraphs Significantly Downgraded (Medium, carry-forward)

**File:** `src/core/orchestration/graph_factory.py`

`GraphFactory.get_graph()` builds 3–4 node subgraphs vs the 14-node main pipeline. Subagent tasks routed through `graph_factory.py` get no plan validation, debug loops, evaluation, step-retry, or wait-for-user gates.

**Recommendation (Phase 2):** Route `GraphFactory.get_graph()` through `_get_compiled_graph()` from `builder.py`.

### ARCH-VOL21-2 — orchestrator_bootstrap.py Phase 3 Extraction Not Started (Medium, carry-forward)

`orchestrator_bootstrap.py` remains a 446-line monolithic function. Unchanged — no regression.

---

## 2. Security

### SEC-1 — WorkspaceGuard ✅ CONFIRMED CLOSED

### SEC-2 — Write-Tool Enforcement Sets ✅ CONFIRMED CLOSED

### SEC-3/SEC-4 ✅ CONFIRMED CLOSED

---

## 3. Workflow Reliability

### WF-5/WF-VOL21-1/2/3 ✅ ALL CONFIRMED CLOSED

All 5 previously guarded nodes (`perception_node`, `planning_node`, `execution_node`, `replan_node`, `debug_node`) confirmed guarded. `evaluation_node` semantic eval wrapped with `asyncio.wait_for`.

### WF-VOL22-1 — `analyst_delegation_node.py` Uses Thread-Timeout, Not `asyncio.wait_for` ✅ CLOSED (this session)

**File:** `src/core/orchestration/graph/nodes/analyst_delegation_node.py`

Added `asyncio.wait_for(..., timeout=_analyst_timeout)` around `delegate_task_async(...)`. Timeout reads `max_llm_wait_seconds × 2.5` from project settings to allow for the full subagent round-trip; default 300 s. Added `import asyncio` at top of file.

---

## 4. Failure Handling

### BUG-VOL21-1 ✅ CONFIRMED CLOSED

### BUG-VOL22-1 — `/compact` Failure Invisible to User ✅ CLOSED (this session)

**Files:** `src/core/orchestration/orchestrator_helpers.py`, `tui/src/ui/app.py`

- `compact_context_impl`: on exception, now publishes `context.compact.failed` event to the event bus with the error message before returning `False`.
- `app.py` `/compact` handler: the `False` branch now shows a bold yellow warning message in the chat view and fires a `notify(..., severity="warning")` toast, instead of the previous misleading "Context compacted (mock)." message.

### BUG-VOL22-3 — Working Dir Creation Failure Not Surfaced to Caller ✅ CLOSED (this session)

**File:** `src/core/orchestration/orchestrator_helpers.py` (`_ensure_working_dir_impl`)

On exception, now sets `orch._working_dir_unavailable = True` and publishes `working_dir.unavailable` event to the event bus. Does not re-raise (that would crash `__init__`), but the flag and event give callers a clear signal to short-circuit file tool dispatch.

---

## 5. Tool System

### Tool Timeouts ✅ CONFIRMED CLOSED (ARCH-1)

### BUG-VOL22-2 — `patch_tools.py` Missing User-Facing Argument Validation ✅ CLOSED (this session)

**File:** `src/tools/patch_tools.py`

Changed `workdir: Path` to `workdir: Optional[Path] = None` on both `generate_patch` and `apply_patch`. Added `if workdir is None: workdir = Path.cwd()` guard immediately after each function signature, matching the `_edit_tools.py` / `_file_io.py` pattern. Also added `Optional` to the `from typing import` line.

---

## 6. Repository Awareness

### RA-3 ✅ CONFIRMED CLOSED

### RA-VOL22-1 — Fast-Path Planning Reads Potentially Absent `repo_index.json` ✅ CLOSED (this session)

**File:** `src/core/indexing/repo_indexer.py`

Added `import logging` and `logger = logging.getLogger(__name__)`. In `get_symbols_for_task()`, replaced the silent `return []` when `repo_index.json` is absent with a `logger.debug(...)` that identifies the path and instructs developers to run `analysis_node` or `index_repository()` to build it.

---

## 7. Memory System

### MEM-1/MEM-2/MEM-3 ✅ CONFIRMED CLOSED

---

## 8. Test Suite Health

### TEST-VOL21-1 ✅ CONFIRMED CLOSED

| Suite | Command | Result |
|-------|---------|--------|
| Unit | `pytest tests/unit --timeout=10` | **3206 passed, 4 skipped, 0 failed** |
| Non-live integration | `pytest tests/integration -m "not lmstudio and not ollama and not integration" --timeout=30` | **142 passed, 27 deselected, 0 failed** |

---

## 9. Performance

### PERF-1/PERF-2 ✅ CONFIRMED CLOSED

---

## 10. UX / Observability

### UX-1/2/3 ✅ CONFIRMED CLOSED

---

## 11. Open Item Summary

| Priority | ID | Severity | Description | File(s) | Status |
|----------|----|----------|-------------|---------|--------|
| 1 | WF-VOL22-1 | Low | `analyst_delegation_node` uses thread-timeout, not `asyncio.wait_for` | `analyst_delegation_node.py` | ✅ CLOSED |
| 2 | BUG-VOL22-1 | Medium | `/compact` failure invisible to user | `orchestrator_helpers.py`, `app.py` | ✅ CLOSED |
| 3 | BUG-VOL22-2 | Medium | `patch_tools.py` missing user-facing arg validation on `workdir` | `patch_tools.py` | ✅ CLOSED |
| 4 | RA-VOL22-1 | Low | Fast-path planning silently returns `[]` when `repo_index.json` absent | `repo_indexer.py` | ✅ CLOSED |
| 5 | BUG-VOL22-3 | Low | Working dir creation failure not surfaced to caller | `orchestrator_helpers.py` | ✅ CLOSED |
| 6 | ARCH-VOL21-1 | Medium | GraphFactory subgraphs (3–4 nodes) vs main pipeline (14 nodes) | `graph_factory.py` | Open — Phase 2 |
| 7 | ARCH-VOL21-2 | Medium | `orchestrator_bootstrap.py` Phase 3 extraction not started | `orchestrator_bootstrap.py` | Open — Phase 3 |
| 8 | OE-VOL21-1 | Low | 5 unwired router functions have unit tests but don't run in production graph | `builder.py` | Open — deferred |
| 9 | OE-VOL21-2 | Low | `HubAndSpokeCoordinator` — no live callers | `graph_factory.py` | Open — Phase 3 |

---

## 12. Engineering Roadmap

### Phase 1 — Correctness ✅ ALL CLOSED

| Task | Location | Status |
|------|----------|--------|
| Wrap `analyst_delegation_node` call in `asyncio.wait_for` (WF-VOL22-1) | `analyst_delegation_node.py` | ✅ Done |
| Surface `/compact` failure to user via event bus (BUG-VOL22-1) | `orchestrator_helpers.py`, `app.py` | ✅ Done |
| Add `workdir: Optional[Path] = None` guard to `patch_tools.py` (BUG-VOL22-2) | `patch_tools.py` | ✅ Done |
| Add `logger.debug` when `repo_index.json` absent (RA-VOL22-1) | `repo_indexer.py` | ✅ Done |
| Set `_working_dir_unavailable` flag + publish event (BUG-VOL22-3) | `orchestrator_helpers.py` | ✅ Done |

### Phase 2 — Architecture (Estimated: 1 week)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| GraphFactory: delegate to main compiled graph (ARCH-VOL21-1) | `graph_factory.py` | Medium | Subagent tasks get full plan-validate/debug/eval pipeline |

### Phase 3 — Advanced (Estimated: 2+ weeks)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Extract `ScopeGuard`, `OrchestratorCore` from `orchestrator_bootstrap.py` (ARCH-VOL21-2) | `orchestrator_bootstrap.py` | High | Reduces to <300-line file; clean separation of concerns |
| Remove `HubAndSpokeCoordinator` when ARCH-VOL21-1 resolved (OE-VOL21-2) | `graph_factory.py` | Low | Dead code removal |

---

## Baseline Metrics

| Metric | Vol21 (closed) | Vol22 (open) | Vol22 (closed) |
|--------|----------------|--------------|----------------|
| Tests collected | 3210 | 3210 | **3210** |
| Tests passed | 3206 | 3206 | **3206** |
| Tests failed | 0 | 0 | **0** |
| Tests skipped | 4 | 4 | **4** |
| Pyright errors | 0 | 0 | **0** |
| `orchestrator.py` lines | 375 | 375 | **375** |
| Open items | 4 (Phase 2/3) | 9 | **4** (Phase 2/3 only) |
| Critical-severity items | 0 | 0 | **0** |
| High-severity items | 0 | 0 | **0** |
| Medium-severity items | 0 | 2 | **0** (all closed) |
| Low-severity items | 0 | 3 | **0** (all closed) |


