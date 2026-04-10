# Audit Report — Vol21

**Date:** 2026-04-09
**Auditor:** OpenCode agent (claude-sonnet-4.6)
**Scope:** Full-spectrum architectural audit — all 15 required categories.
**Baseline:** 3206 passed, 4 skipped, 0 failed, 4 warnings. Test collection: 3210 tests.
**Previous audit:** Vol20 — all 4 open items now fully resolved (ARCH-1 confirmed, RA-3 confirmed, OE-1 confirmed non-removable, UX-1 wired into `instruction_loader.py`).

---

## Executive Summary

Vol21 found **4 High-severity findings** (3 missing LLM timeout guards + 1 latent coroutine bug) and **2 Medium-severity findings** (GraphFactory downgrade + 13 missing `pytestmark`). All 5 Phase 1 items have been fixed in the same session.

Post-fix baseline: **3206 passed, 4 skipped, 0 failed** — no regressions.

---

## 1. Architecture

### ARCH-1 (ToolRegistry) ✅ CONFIRMED CLOSED

`tool_registry.py` contains `_TOOL_TIMEOUTS`, `DEFAULT_TOOL_TIMEOUT`, and `get_tool_timeout()`. Delegation chain confirmed: `orchestrator.py._get_tool_timeout()` → `orchestrator_helpers._get_tool_timeout_impl()` → `tool_registry.get_tool_timeout()`. No duplicate map in `orchestrator_bootstrap.py`.

### ARCH-VOL21-1 — GraphFactory Subgraphs Significantly Downgraded (Medium)

**File:** `src/core/orchestration/graph_factory.py`

`GraphFactory.get_graph()` builds 3–4 node subgraphs (perception → planning → execution → memory_sync). The main pipeline in `builder.py` has 14 nodes including `analysis`, `analyst_delegation`, `plan_validator`, `step_controller`, `replan`, `debug`, `evaluation`, `wait_for_user`, and more.

Any subagent task routed through `graph_factory.py` (e.g. when `delegate_task` spawns a role-specific graph) silently runs without plan validation, debug loops, evaluation, step retry logic, or wait-for-user gates.

**Recommendation (Phase 2):** Update `GraphFactory.get_graph()` to delegate to `_get_compiled_graph()` from `builder.py`, or at minimum route through the plan-validator and step-controller nodes.

### ARCH-VOL21-2 — orchestrator_bootstrap.py Phase 3 Extraction Not Started (Medium, carry-forward)

`orchestrator_bootstrap.py` remains a 446-line monolithic function. Vol20 Phase 3 specified extracting `ScopeGuard` and `OrchestratorCore`. Unchanged — no regression.

---

## 2. Security

### SEC-1 — WorkspaceGuard ✅ CONFIRMED CLOSED

All 6 write tools check `guard_operation()`. Unchanged.

### SEC-2 — Write-Tool Enforcement Sets ✅ CONFIRMED CLOSED

`WRITE_TOOLS_REQUIRING_READ` and `MODIFYING_TOOLS` remain consistent. Unchanged.

### SEC-3/SEC-4 ✅ CONFIRMED CLOSED

Both closed in Vol20. Unchanged.

---

## 3. Workflow Reliability

### WF-5 ✅ CONFIRMED CLOSED

All 3 previously-guarded nodes remain guarded (`perception_node`, `planning_node`, `execution_node`).

### WF-VOL21-1 — `replan_node.py` Missing LLM Timeout Guard ✅ CLOSED (this session)

**File:** `src/core/orchestration/graph/nodes/replan_node.py`

Replaced bare `await call_model(messages, ...)` with `asyncio.create_task` + deadline-based polling loop, reading `max_llm_wait_seconds` from `project_settings.py`. Cancel-event check and deadline guard both added, matching the `planning_node.py` pattern exactly.

### WF-VOL21-2 — `debug_node.py` Polling Loop Has No Hard Deadline ✅ CLOSED (this session)

**File:** `src/core/orchestration/graph/nodes/debug_node.py`

Added `_debug_deadline = loop.time() + _debug_llm_timeout` before the existing poll loop and `elif loop.time() >= _debug_deadline: cancel + return timeout error` inside the loop. Reads `max_llm_wait_seconds` from project settings; default 120 s.

### WF-VOL21-3 — `evaluation_node.py` Semantic Eval Missing Timeout Guard ✅ CLOSED (this session)

**File:** `src/core/orchestration/graph/nodes/evaluation_node.py`

Wrapped bare `await _call_model(_verdict_prompt, model=None)` with `asyncio.wait_for(..., timeout=_eval_timeout)`, reading `max_llm_wait_seconds` from project settings; default 120 s. Added `import asyncio` at the top of the file.

---

## 4. New Bug Findings

### BUG-VOL21-1 — `HubAndSpokeCoordinator.run_next()` Coroutine Called From Thread Pool ✅ CLOSED (this session)

**File:** `src/core/orchestration/graph_factory.py`

Replaced `executor.submit(agent["graph"].ainvoke, state_dict, config_dict)` with `executor.submit(asyncio.run, agent["graph"].ainvoke(state_dict, config_dict))`. The coroutine is now driven to completion inside the worker thread via `asyncio.run`. Note: `HubAndSpokeCoordinator` remains a dead class with no live production callers — the fix closes the silent data-loss bug for when/if it gains callers.

---

## 5. Memory System

### MEM-1/MEM-2/MEM-3 ✅ CONFIRMED CLOSED

All unchanged. `context_builder.py` correctly skips empty TASK_STATE blocks. Cache invalidation confirmed.

---

## 6. Test Suite Health

| Suite | Command | Result |
|-------|---------|--------|
| Unit | `pytest tests/unit --timeout=10` | **3206 passed, 4 skipped, 0 failed** |
| Non-live integration | `pytest tests/integration -m "not lmstudio and not ollama and not integration" --timeout=30` | **142 passed, 27 deselected, 0 failed** |

### TEST-VOL21-1 — 13 Integration Test Files Missing `pytestmark` ✅ CLOSED (this session)

Added `pytestmark = pytest.mark.integration` to all 13 files. `test_lmstudio_end_to_end.py` was marked `[integration, lmstudio]`; `test_ollama_adapter_integration.py` was marked `[integration, ollama]`.

| File | Status |
|------|--------|
| `test_agent_loop_plaintext_tools.py` | ✅ Fixed |
| `test_delegation_mock.py` | ✅ Fixed |
| `test_e2e_pipeline_smoke.py` | ✅ Fixed |
| `test_langgraph_orchestrator.py` | ✅ Fixed |
| `test_lmstudio_end_to_end.py` | ✅ Fixed |
| `test_loop_prevention.py` | ✅ Fixed |
| `test_mock_adapter_integration.py` | ✅ Fixed |
| `test_ollama_adapter_integration.py` | ✅ Fixed |
| `test_phase3_findings.py` | ✅ Fixed |
| `test_phase4_findings.py` | ✅ Fixed |
| `test_pipeline_mock.py` | ✅ Fixed |
| `test_prsw_execution.py` | ✅ Fixed |
| `test_scenario_smoke.py` | ✅ Fixed |

---

## 7. Performance

### RA-3 ✅ CONFIRMED CLOSED

`analysis_node._build_lightweight_test_map()` generates `test_map` for both simple (fast-path, lines 99–137, 227–243) and complex tasks (lines 440–521). `planning_node` injects the map into the planning prompt. No gaps.

### PERF-1/PERF-2 ✅ CONFIRMED CLOSED

Per-session TTL cache clear and `enable_semantic_evaluation` opt-in are both confirmed in place.

---

## 8. UX / Observability

### UX-1 ✅ CLOSED (Vol21 session)

`get_lsp_status_notice()` is now wired into `instruction_loader.py` (lines 216–224). When LSP is enabled but no language server is running, the notice appears in the system prompt on every turn.

### UX-2/UX-3 ✅ CONFIRMED CLOSED

`--dry-run` and `--validate-config` flags confirmed present in `src/main.py`.

---

## 9. Open Item Summary

| Priority | ID | Severity | Description | File(s) | Status |
|----------|----|----------|-------------|---------|--------|
| 1 | WF-VOL21-1 | High | Missing LLM timeout guard in `replan_node` | `replan_node.py` | ✅ CLOSED |
| 2 | WF-VOL21-2 | High | `debug_node` polling loop has no deadline counter | `debug_node.py` | ✅ CLOSED |
| 3 | WF-VOL21-3 | High | Semantic eval `await _call_model` unguarded | `evaluation_node.py` | ✅ CLOSED |
| 4 | BUG-VOL21-1 | High | `HubAndSpokeCoordinator.run_next()` — coroutine called from thread pool | `graph_factory.py` | ✅ CLOSED |
| 5 | TEST-VOL21-1 | Medium | 13 integration test files missing `pytestmark = pytest.mark.integration` | `tests/integration/*.py` | ✅ CLOSED |
| 6 | ARCH-VOL21-1 | Medium | GraphFactory subgraphs (3–4 nodes) vs main pipeline (14 nodes) | `graph_factory.py` | Open — Phase 2 |
| 7 | ARCH-VOL21-2 | Medium | `orchestrator_bootstrap.py` Phase 3 extraction not started | `orchestrator_bootstrap.py` | Open — Phase 3 |
| 8 | OE-VOL21-1 | Low | 5 unwired router functions have unit tests but don't run in production graph | `builder.py` | Open — deferred |
| 9 | OE-VOL21-2 | Low | `HubAndSpokeCoordinator` — no live callers | `graph_factory.py` | Open — Phase 3 |

---

## 10. Engineering Roadmap

### Phase 1 — Correctness ✅ ALL CLOSED

| Task | Location | Status |
|------|----------|--------|
| Add deadline guard to `replan_node` (WF-VOL21-1) | `replan_node.py` | ✅ Done |
| Add deadline guard to `debug_node` (WF-VOL21-2) | `debug_node.py` | ✅ Done |
| Wrap `evaluation_node` semantic eval call (WF-VOL21-3) | `evaluation_node.py` | ✅ Done |
| Fix `HubAndSpokeCoordinator.run_next()` (BUG-VOL21-1) | `graph_factory.py` | ✅ Done |
| Add `pytestmark = pytest.mark.integration` to 13 files (TEST-VOL21-1) | `tests/integration/*.py` | ✅ Done |

### Phase 2 — Architecture (Estimated: 1 week)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| GraphFactory: delegate to main compiled graph or upgrade to 14-node pipeline (ARCH-VOL21-1) | `graph_factory.py` | Medium | Subagent tasks get full plan-validate/debug/eval pipeline |

### Phase 3 — Advanced (Estimated: 2+ weeks)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Extract `ScopeGuard`, `OrchestratorCore` from `orchestrator_bootstrap.py` (ARCH-VOL21-2) | `orchestrator_bootstrap.py` | High | Reduces to <300-line file; clean separation of concerns |
| Remove `HubAndSpokeCoordinator` when ARCH-VOL21-1 is resolved (OE-VOL21-2) | `graph_factory.py` | Low | Dead code removal |

---

## Baseline Metrics

| Metric | Vol19 | Vol20 | Vol21 (open) | Vol21 (closed) |
|--------|-------|-------|--------------|----------------|
| Tests collected | 3136 | 3210 | 3210 | **3210** |
| Tests passed | 3110 | 3206 | 3206 | **3206** |
| Tests failed | 9 | 0 | 0 | **0** |
| Tests skipped | 17 | 4 | 4 | **4** |
| Pyright errors | 1 | 0 | 0 | **0** |
| `orchestrator.py` lines | 4092 | 375 | 375 | **375** |
| Open items | 10 | 0 | 9 | **4** (Phase 2/3 only) |
| Critical-severity items | 0 | 0 | 0 | **0** |
| High-severity items | 0 | 0 | 4 | **0** (all closed) |

---

## 1. Architecture

### ARCH-1 (ToolRegistry) ✅ CONFIRMED CLOSED

`tool_registry.py` contains `_TOOL_TIMEOUTS`, `DEFAULT_TOOL_TIMEOUT`, and `get_tool_timeout()`. Delegation chain confirmed: `orchestrator.py._get_tool_timeout()` → `orchestrator_helpers._get_tool_timeout_impl()` → `tool_registry.get_tool_timeout()`. No duplicate map in `orchestrator_bootstrap.py`.

### ARCH-VOL21-1 — GraphFactory Subgraphs Significantly Downgraded (Medium)

**File:** `src/core/orchestration/graph_factory.py`

`GraphFactory.get_graph()` builds 3–4 node subgraphs (perception → planning → execution → memory_sync). The main pipeline in `builder.py` has 14 nodes including `analysis`, `analyst_delegation`, `plan_validator`, `step_controller`, `replan`, `debug`, `evaluation`, `wait_for_user`, and more.

Any subagent task routed through `graph_factory.py` (e.g. when `delegate_task` spawns a role-specific graph) silently runs without plan validation, debug loops, evaluation, step retry logic, or wait-for-user gates.

**Recommendation (Phase 2):** Update `GraphFactory.get_graph()` to delegate to `_get_compiled_graph()` from `builder.py`, or at minimum route through the plan-validator and step-controller nodes.

### ARCH-VOL21-2 — orchestrator_bootstrap.py Phase 3 Extraction Not Started (Medium, carry-forward)

`orchestrator_bootstrap.py` remains a 446-line monolithic function. Vol20 Phase 3 specified extracting `ScopeGuard` and `OrchestratorCore`. Unchanged — no regression.

---

## 2. Security

### SEC-1 — WorkspaceGuard ✅ CONFIRMED CLOSED

All 6 write tools check `guard_operation()`. Unchanged.

### SEC-2 — Write-Tool Enforcement Sets ✅ CONFIRMED CLOSED

`WRITE_TOOLS_REQUIRING_READ` and `MODIFYING_TOOLS` remain consistent. Unchanged.

### SEC-3/SEC-4 ✅ CONFIRMED CLOSED

Both closed in Vol20. Unchanged.

---

## 3. Workflow Reliability

### WF-5 ✅ CONFIRMED CLOSED

All 3 previously-guarded nodes remain guarded (`perception_node`, `planning_node`, `execution_node`).

### WF-VOL21-1 — `replan_node.py` Missing LLM Timeout Guard (High) — NEW

**File:** `src/core/orchestration/graph/nodes/replan_node.py:87`

```python
resp = await call_model(messages, stream=False, format_json=False)
```

A bare `await` with no `asyncio.wait_for`, no polling loop, and no deadline counter. Every other LLM-calling production node wraps its call in a deadline-based poll guard reading `max_llm_wait_seconds` from `project_settings.py`. If the LLM hangs during a replan, the agent hangs indefinitely with no way to timeout.

**Fix:** Wrap in the same `asyncio.create_task` + deadline-based polling pattern used by `planning_node.py`.

### WF-VOL21-2 — `debug_node.py` Polling Loop Has No Hard Deadline (High) — NEW

**File:** `src/core/orchestration/graph/nodes/debug_node.py:180–203`

Uses `asyncio.create_task` + a `cancel_event.is_set()` poll, but has no elapsed-time deadline counter. If `cancel_event` is never set (e.g. headless / background execution), the node polls forever. The deadline guard pattern is: read `max_llm_wait_seconds` from settings, compute `_deadline = loop.time() + _timeout`, and add `elif loop.time() > _deadline: break` to the poll loop — already done in `perception_node.py` and `planning_node.py`.

**Fix:** Add `_debug_deadline = loop.time() + _debug_llm_timeout` and `elif loop.time() > _debug_deadline: break` to the poll loop.

### WF-VOL21-3 — `evaluation_node.py` Semantic Eval Missing Timeout Guard (High) — NEW

**File:** `src/core/orchestration/graph/nodes/evaluation_node.py:176`

```python
_resp = await _call_model(_verdict_prompt, model=None)
```

The semantic evaluation path (`enable_semantic_evaluation=True`) issues a bare `await` with zero timeout protection. This path is disabled by default (`enable_semantic_evaluation: bool = False` in `project_settings.py`), so there is no live regression. But when enabled, a hung LLM provider will stall evaluation indefinitely.

**Fix:** Same `asyncio.wait_for` or deadline-based polling pattern as `execution_node.py`.

---

## 4. New Bug Findings

### BUG-VOL21-1 — `HubAndSpokeCoordinator.run_next()` Coroutine Called From Thread Pool (High) — NEW

**File:** `src/core/orchestration/graph_factory.py:191–202`

```python
with concurrent.futures.ThreadPoolExecutor() as executor:
    future = executor.submit(
        agent["graph"].ainvoke,
        {... state dict ...},
        {... config ...},
    )
    result = future.result()
```

`agent["graph"].ainvoke` is a coroutine function. `executor.submit(coroutine_fn, args...)` submits the callable to a thread pool. When the thread calls `ainvoke(state, config)`, it gets back a coroutine object that is never awaited and immediately garbage-collected. `future.result()` returns the coroutine object (not the actual result), silently producing wrong output or `None`.

**Severity:** High (silent data loss). However, `HubAndSpokeCoordinator` has no production callers — `subagent_tools.py` uses only `GraphFactory.get_graph()`. Risk is confined to this dead class.

**Fix:** Replace `executor.submit(ainvoke, ...)` with `asyncio.run(ainvoke(...))` inside the submitted callable:
```python
future = executor.submit(asyncio.run, agent["graph"].ainvoke(state, config))
```
Or restructure `run_next()` as an async method.

---

## 5. Memory System

### MEM-1/MEM-2/MEM-3 ✅ CONFIRMED CLOSED

All unchanged. `context_builder.py` correctly skips empty TASK_STATE blocks. Cache invalidation confirmed.

---

## 6. Test Suite Health

| Suite | Command | Result |
|-------|---------|--------|
| Unit | `pytest tests/unit --timeout=10` | **3206 passed, 4 skipped, 0 failed** |
| Non-live integration | `pytest tests/integration -m "not lmstudio and not ollama and not integration" --timeout=30` | **142 passed, 27 deselected, 0 failed** |

### TEST-VOL21-1 — 13 Integration Test Files Missing `pytestmark = pytest.mark.integration` (Medium) — NEW

The following 13 test files are under `tests/integration/` but do not have `pytestmark = pytest.mark.integration`:

| File |
|------|
| `test_agent_loop_plaintext_tools.py` |
| `test_delegation_mock.py` |
| `test_e2e_pipeline_smoke.py` |
| `test_langgraph_orchestrator.py` |
| `test_lmstudio_end_to_end.py` |
| `test_loop_prevention.py` |
| `test_mock_adapter_integration.py` |
| `test_ollama_adapter_integration.py` |
| `test_phase3_findings.py` |
| `test_phase4_findings.py` |
| `test_pipeline_mock.py` |
| `test_prsw_execution.py` |
| `test_scenario_smoke.py` |

`-m "not integration"` does **not** exclude these tests (they run anyway in the non-live integration suite). `-m integration` does **not** select them. This means there is no CLI-level mechanism to run "only pure integration tests" or "exclude all integration tests" correctly.

**Fix:** Add `pytestmark = pytest.mark.integration` at the top of each file (below imports).

---

## 7. Performance

### RA-3 ✅ CONFIRMED CLOSED

`analysis_node._build_lightweight_test_map()` generates `test_map` for both simple (fast-path, lines 99–137, 227–243) and complex tasks (lines 440–521). `planning_node` injects the map into the planning prompt. No gaps.

### PERF-1/PERF-2 ✅ CONFIRMED CLOSED

Per-session TTL cache clear and `enable_semantic_evaluation` opt-in are both confirmed in place.

---

## 8. UX / Observability

### UX-1 ✅ CLOSED (this session)

`get_lsp_status_notice()` is now wired into `instruction_loader.py` (lines 216–224). When LSP is enabled but no language server is running, the notice appears in the system prompt on every turn.

### UX-2/UX-3 ✅ CONFIRMED CLOSED

`--dry-run` and `--validate-config` flags confirmed present in `src/main.py`.

---

## 9. Open Item Summary

| Priority | ID | Severity | Description | File(s) | Complexity |
|----------|----|----------|-------------|---------|------------|
| 1 | WF-VOL21-1 | High | Missing LLM timeout guard in `replan_node` | `replan_node.py:87` | Low |
| 2 | WF-VOL21-2 | High | `debug_node` polling loop has no deadline counter | `debug_node.py:180–203` | Low |
| 3 | WF-VOL21-3 | High | Semantic eval `await _call_model` unguarded | `evaluation_node.py:176` | Low |
| 4 | BUG-VOL21-1 | High | `HubAndSpokeCoordinator.run_next()` — coroutine called from thread pool | `graph_factory.py:191` | Low |
| 5 | TEST-VOL21-1 | Medium | 13 integration test files missing `pytestmark = pytest.mark.integration` | `tests/integration/*.py` (13 files) | Low |
| 6 | ARCH-VOL21-1 | Medium | GraphFactory subgraphs (3–4 nodes) vs main pipeline (14 nodes) | `graph_factory.py` | Medium |
| 7 | ARCH-VOL21-2 | Medium | `orchestrator_bootstrap.py` Phase 3 extraction not started | `orchestrator_bootstrap.py` | High |
| 8 | OE-VOL21-1 | Low | 5 unwired router functions have unit tests but don't run in production graph | `builder.py` | Low |
| 9 | OE-VOL21-2 | Low | `HubAndSpokeCoordinator` — no live callers, broken by BUG-VOL21-1 | `graph_factory.py` | Low |

---

## 10. Engineering Roadmap

### Phase 1 — Correctness (Estimated: 1 day)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Add deadline guard to `replan_node` (WF-VOL21-1) | `replan_node.py:87` | Low | Prevents indefinite hang on slow/stuck LLM |
| Add deadline guard to `debug_node` (WF-VOL21-2) | `debug_node.py:180` | Low | Same |
| Wrap `evaluation_node` semantic eval call (WF-VOL21-3) | `evaluation_node.py:176` | Low | Prevents hang when semantic eval enabled |
| Fix `HubAndSpokeCoordinator.run_next()` (BUG-VOL21-1) | `graph_factory.py:191` | Low | Fixes silent result-loss bug (no live impact) |
| Add `pytestmark = pytest.mark.integration` to 13 files (TEST-VOL21-1) | `tests/integration/*.py` | Low | Correct mark-based test filtering |

### Phase 2 — Architecture (Estimated: 1 week)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| GraphFactory: delegate to main compiled graph or upgrade to 14-node pipeline (ARCH-VOL21-1) | `graph_factory.py` | Medium | Subagent tasks get full plan-validate/debug/eval pipeline |

### Phase 3 — Advanced (Estimated: 2+ weeks)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Extract `ScopeGuard`, `OrchestratorCore` from `orchestrator_bootstrap.py` (ARCH-VOL21-2) | `orchestrator_bootstrap.py` | High | Reduces to <300-line file; clean separation of concerns |
| Remove `HubAndSpokeCoordinator` when ARCH-VOL21-1 is resolved (OE-VOL21-2) | `graph_factory.py` | Low | Dead code removal |

---

## Baseline Metrics

| Metric | Vol19 | Vol20 | Vol21 |
|--------|-------|-------|-------|
| Tests collected | 3136 | 3210 | **3210** |
| Tests passed | 3110 | 3206 | **3206** |
| Tests failed | 9 | 0 | **0** |
| Tests skipped | 17 | 4 | **4** |
| Pyright errors | 1 | 0 | **0** |
| `orchestrator.py` lines | 4092 | 375 | **375** |
| Open items | 10 | 0 | **9** (all new findings) |
| Critical-severity items | 0 | 0 | **0** |
| High-severity items | 0 | 0 | **4** (timeout guards + latent bug) |
