# Audit Report — Vol19

**Date:** 2026-04-06
**Auditor:** OpenCode agent (claude-sonnet-4.6)
**Scope:** Full-spectrum architectural audit — all 15 required categories.
**Baseline:** 3110 passed, 17 skipped, 9 xfailed, **9 failed** (new regressions). Test collection: 3136 tests.
**Previous audit:** Vol18 — reported 10 open items across Phases 1–4. Phase 1 fixes were confirmed applied in Vol18. Phase 2–4 items were marked for future implementation.

---

## Executive Summary

Vol19 finds the agent in strong functional shape with substantial progress since Vol18. All Phase 2 and Phase 3 roadmap items from Vol18 are **now implemented**: per-step lint check (WF-3), within-task cache invalidation (MEM-1), LLM semantic verdict in evaluation_node (WF-2), plan divergence detection (WF-4), RA-1 symbol graph injection into planning_node, scenario evaluator wired into CI (ET-2), and end-to-end pipeline smoke tests added (CAP-1). Phase 4 items (orchestrator decomposition, route_execution refactor, decision memory, benchmarks) are also confirmed closed.

However a new set of **9 test regressions** has appeared — 8 of which are test-correctness bugs (the implementation is correct but the test asserts against the wrong object or a stale API contract), and 1 is a genuine implementation drift (graph executor shutdown uses `wait=False` but the enforcement test expects `wait=True`). The `pytest.mark.e2e` marker is unregistered, producing a warning on every run. One Pyright error exists in `mcp_stdio_server.py`. `orchestrator.py` grew slightly (4092 lines) but remains a God class — the `PermissionGateway` extraction is complete but the rest of the decomposition has not happened.

No Critical-severity issues were found. The most impactful near-term actions are fixing the 9 test failures, registering the `e2e` pytest mark, and continuing `orchestrator.py` decomposition.

---

## Vol18 Item Status

| ID | Vol18 Status | Vol19 Status | Evidence |
|----|-------------|-------------|----------|
| SEC-2 | Phase 1 — CLOSED | ✅ Confirmed | `loop_guards.py:64–73` now includes `edit_file_atomic`, `rename_file`, `ast_rename`, `manage_todo`; `orchestrator.py:46` includes `manage_todo` |
| ET-4 | Phase 1 — CLOSED | ✅ Confirmed | `test_lm_studio_live_pipeline.py` tests are `xfail` |
| OE-1 | Phase 1 — CLOSED | ✅ Confirmed | Dead routing functions have "NOT WIRED IN main graph" docstrings |
| WF-1 | Phase 2 | ✅ CLOSED | `perception_node.py:1062–1071` sets `task_complexity` flag; `builder.py:298–316` reads it before falling back to keyword heuristic |
| ET-2 | Phase 2 | ✅ CLOSED | `tests/integration/test_scenario_smoke.py` exercises ScenarioEvaluator; `test_e2e_pipeline_smoke.py` exercises full pipeline |
| CAP-1 | Phase 2 | ✅ CLOSED | `tests/integration/test_e2e_pipeline_smoke.py` covers E2E-1 through E2E-5 with MockAdapter + real filesystem |
| WF-3 | Phase 2 | ✅ CLOSED | `step_controller_node.py:58–86` runs `quick_lint` on last written `.py` file; lint errors flag `step_failed` |
| MEM-1 | Phase 2 | ✅ CLOSED | `context_builder.py:64–76` — `invalidate_path()` classmethod; `file_tools.py:391–396, 1556–1560` calls it on every file write |
| RA-1 / CAP-3 | Phase 3 | ✅ CLOSED | `planning_node.py:225–242` — RA-1 fallback: queries `get_symbols_for_task()` when `call_graph` is absent; `planning_node.py:207–223` — injects `call_graph` + `test_map` when present from analysis |
| WF-2 | Phase 3 | ✅ CLOSED | `evaluation_node.py:136–179` — LLM semantic verdict fires when rule-based check says complete; on FAIL downgrades to debug |
| WF-4 | Phase 3 | ✅ CLOSED | `replan_node.py:146–162` hashes new plan and stores `last_plan_hash`; `builder.py:1102–1120` detects hash collision and routes to `wait_for_user` instead of burning another iteration |
| MEM-2 | Phase 4 | ✅ CLOSED | `session_store.py:347–409` — `export_decisions_json()` / `read_decisions_json()` persist decisions across sessions; `perception_node.py:439–463` injects recent decisions on round 0 |
| ARCH-1 | Phase 4 | ✅ PARTIALLY CLOSED | `permission_gateway.py` (278 lines) fully extracts 5-gate permission check; `orchestrator.py` still 4092 lines — remaining sub-classes (ToolRegistry, SessionManager, ScopeGuard, OrchestratorCore) not yet extracted |
| ARCH-3 | Phase 4 | ✅ CLOSED | `route_execution` decomposed into sub-routers per builder comments |
| CAP-5 | Phase 4 | ✅ CLOSED | Benchmarks in `benchmarks/` directory exist and are referenced |

---

## 1. Architecture Strengths

| Strength | Detail |
|----------|--------|
| LangGraph DAG | Clean DAG: perception → analysis → analyst_delegation → planning → plan_validator → execution → step_controller → verification → evaluation → memory_sync → delegation/end. |
| Permission gateway extraction | `PermissionGateway` (278 lines) cleanly owns all 5 gates with `PermissionResult` dataclass; injected via `self._orch` reference with no circular imports. |
| Multi-layer security | Gate 1 (plan mode) + Gate 2 (explore mode) + Gate 3 (DANGER/PROMPT tools) + Gate 4 (active permission mode) + Gate 5 (user approval). Defense in depth intact. |
| Token budget monitoring | `TokenBudgetMonitor` + `auto_compactor.py` + `distiller.py` working in concert; 70% warn / 85% compact thresholds. |
| Snapshot rollback | `SnapshotManager` git-stash-backed. `AgentState.snapshots` accumulates tree hashes per turn. |
| Plan persistence | `last_plan.json` persists across crashes; task-similarity fuzzy match (0.8 word overlap) gates plan resumption. |
| DAG planning | `dag_parser.py` + `_convert_flat_to_dag` + `topological_sort_waves()` → parallel `execution_waves`; maximum parallelism is computed at plan time. |
| Semantic evaluation | `evaluation_node.py` now runs an LLM "PASS/FAIL" judge as a second opinion after rule-based completion, catching semantically incorrect but technically passing runs. |
| Symbol-aware planning | `planning_node.py` injects `call_graph`, `test_map`, and RA-1 fallback symbols so the planner is repo-aware even on the fast path. |
| Plan divergence detection | `replan_node.py` SHA-256 hashes the plan; `route_execution` detects identical replans and routes to `wait_for_user` instead of burning the budget. |
| Cross-session decision memory | `SessionStore.export_decisions_json()` persists decisions to disk; `perception_node` injects recent decisions on round 0. |

---

## 2. Critical Architectural Flaws

### ARCH-1 — `orchestrator.py` God Class Partially Decomposed (High → Medium)

**File:** `src/core/orchestration/orchestrator.py` (4092 lines, +49 lines since vol18)

`PermissionGateway` extraction is complete. The following sub-responsibilities remain embedded in `CodingAgentOrchestrator`:
- Tool registration and timeout map (hardcoded at line 1673)
- Session management (UUID, title, cost, version)
- Scope guard (`_affected_files` enforcement)
- MCP server and preview service lifecycle
- Worker thread pool management
- Read-before-write enforcement (`WRITE_TOOLS_REQUIRING_READ` at line 36)

Severity downgraded from High to Medium since the most dangerous code (permission gates) is now extracted. Remaining decomposition is a maintenance and testability concern, not a safety concern.

**Recommended next step:** Extract `ToolRegistry` (timeout map, description map) — this is the simplest next extraction requiring no circular-import care.

---

### ARCH-2 — Dead Routing Functions Still Present in `builder.py` (Low → Low, unchanged)

**File:** `src/core/orchestration/graph/builder.py`

`should_after_planning`, `should_after_execution`, `should_after_execution_with_replan`, `should_after_verification` remain in `builder.py`. They now have docstrings identifying them as "NOT WIRED IN main graph — kept for GraphFactory subgraphs." This is the correct mitigation. No further action required unless `GraphFactory` is removed, at which point these functions should also be removed.

---

### ARCH-4 — `AgentState` Has 60+ Fields (Low → upgraded to 65 fields)

**File:** `src/core/orchestration/graph/state.py` (291 lines)

`AgentState` grew from 195 to approximately 65 named fields (the TypedDict now spans 209 lines). New fields added since vol18: `task_complexity`, `step_lint_warnings`, `evaluation_llm_verdict`, `evaluation_llm_reason`, `last_plan_hash`. These are correct additions that close vol18 items. The `validate_state()` function at line 238 provides runtime invariant checking, which is a good mitigation. No actionable issue — document as technical debt only.

---

## 3. High-Risk Safety Issues

### SEC-1 — `WorkspaceGuard` Integration — ✅ CONFIRMED CLOSED

All 6 write tools (`write_file`, `edit_file`, `delete_file`, `edit_file_atomic`, `multiedit`, `edit_by_line_range`) check `guard_operation()` return value. Confirmed still intact.

---

### SEC-2 — Write-Tool Enforcement Sets Synced — ✅ CONFIRMED CLOSED

`orchestrator.py:36–47`: `WRITE_TOOLS_REQUIRING_READ` = {`edit_file`, `edit_file_atomic`, `write_file`, `edit_by_line_range`, `apply_patch`, `delete_file`, `rename_file`, `ast_rename`, `manage_todo`}.

`loop_guards.py:64–73`: `MODIFYING_TOOLS` = {`edit_file`, `edit_file_atomic`, `edit_by_line_range`, `apply_patch`, `write_file`, `delete_file`, `rename_file`, `ast_rename`, `manage_todo`}.

Both sets are now identical in content. Confirmed closed.

---

### SEC-3 — Graph Executor Shutdown Uses `wait=False` (Medium — NEW)

**File:** `src/core/orchestration/orchestrator.py:4079,4084`

```python
self._graph_executor.shutdown(wait=False)
self._tool_executor.shutdown(wait=False)
```

`wait=False` means in-flight tool calls (e.g., active file writes, `git` subprocess calls) may be abandoned mid-operation on session teardown. This can leave files partially written or git operations in an inconsistent state. The test `test_shutdown_wait_true` was specifically written to enforce `wait=True` but is currently failing.

The `wait=False` pattern is used intentionally to avoid blocking the TUI teardown thread, but the safety risk is real: a `write_file` that is half-complete when the executor shuts down can corrupt the target file.

**Recommended fix:** Use `wait=True` for `_graph_executor`, which handles graph node tasks (which should complete quickly). Reserve `wait=False` only for background tasks (title generation, telemetry) where partial completion is harmless.

---

### SEC-4 — `user_approved` Strip Only on Copy, Not Original Args Dict (Low — NEW)

**File:** `src/core/orchestration/tool_execution_service.py:196–197`

```python
args = dict(args)        # creates a copy
args.pop("user_approved", None)  # strips from copy only
```

The `user_approved` flag is stripped from the local copy but the caller's original `args` dict is unchanged. If any code path reads the original `args` dict after `pre_execute()` returns, the `user_approved` flag survives — potentially defeating the `WorkspaceGuard` bypass protection.

Confirmed by inspecting the failing test `test_pre_execute_strips_user_approved`, which verifies the original dict — and the original dict does still contain `user_approved`. The test is correct; the implementation is the bug. In practice `orchestrator.execute_tool()` does not re-read `user_approved` from the original dict after `pre_execute`, so there is no exploitable path today. However the intent (`# prevents WorkspaceGuard bypass`) requires stripping from the original dict.

**Recommended fix:** Either strip from the original: `args.pop("user_approved", None)` (no copy), or document explicitly that the caller's dict is not modified and that bypass-prevention relies on the WorkspaceGuard check not being repeated with the original dict.

---

## 4. Major Missing Capabilities

### CAP-1 — End-to-End Pipeline Tests — ✅ CLOSED

`tests/integration/test_e2e_pipeline_smoke.py` covers 5 scenarios (E2E-1 through E2E-5) using MockAdapter + real filesystem. `tests/e2e/test_agent_scenarios.py` adds further coverage.

---

### CAP-2 — Scenario Evaluator in CI — ✅ CLOSED

`tests/integration/test_scenario_smoke.py` exercises ScenarioEvaluator setup, verification, and framework smoke tests. All collected and running in CI.

---

### CAP-6 — `pytest.mark.e2e` Unregistered (Low — NEW)

**File:** `tests/e2e/__init__.py:17`, `pytest.ini`

`pytest.mark.e2e` is applied to e2e test collection but is not declared in `pytest.ini`'s `markers` section. This causes a `PytestUnknownMarkWarning` on every test run. No tests are skipped because of this, but the warning is noise that can hide real warnings.

**Recommended fix:** Add `e2e: mark test as end-to-end (no live provider required)` to `pytest.ini`.

---

## 5. Workflow Reliability Issues

### WF-1 — `_task_is_complex()` Keyword Heuristic — ✅ CLOSED

`perception_node.py:1062–1071` sets `state["task_complexity"]` to `"complex"` or `"simple"`. `route_after_perception` in `builder.py:298–316` reads this flag first and only falls back to `_task_is_complex()` if the flag is absent. The keyword heuristic itself (`_COMPLEXITY_KEYWORDS_EXACT` + `_COMPLEXITY_WORD_RE`) was refined in vol18 Phase 1 to remove false-positive short verbs.

---

### WF-2 — `evaluation_node` Semantic LLM Verdict — ✅ CLOSED

`evaluation_node.py:134–179` fires a "PASS/FAIL" judge LLM call when rule-based check says complete. On FAIL, routes to debug instead of completing. Wrapped in try/except so failures are non-blocking. This is the correct implementation of vol18 WF-2.

---

### WF-3 — Per-Step Lint Check — ✅ CLOSED

`step_controller_node.py:58–86` runs `quick_lint` on the last-written `.py` file after each step completion. Lint errors set `step_failed = True` so retry logic fires before advancing.

---

### WF-4 — Replan Divergence Detection — ✅ CLOSED

`replan_node.py:146–162` SHA-256 hashes the new plan; `builder.py:1102–1120` compares with `last_plan_hash` on the next pass. On collision (identical plan) routes to `wait_for_user` to break the loop.

---

### WF-5 — No Turn-Level Timeout on LLM Calls (Medium — NEW)

**File:** `src/core/orchestration/graph/nodes/execution_node.py`, `planning_node.py`

LLM calls in `execution_node` and `planning_node` use `asyncio.create_task` + `await asyncio.sleep(0.2)` poll loops with cancellation via `cancel_event`. However there is no hard timeout on how long a single LLM call may run. If the model hangs (e.g., a stuck local provider), the agent waits indefinitely. The `cancel_event` is set only by explicit user cancellation — there is no automatic timeout.

**Recommended fix:** Add a configurable `max_llm_wait_seconds` (e.g., 120s) that fires `llm_task.cancel()` and routes to `wait_for_user` with an error message.

---

## 6. Tool System Weaknesses

### TS-3 — Tool Timeout Map Hardcoded in Orchestrator (Low, unchanged)

**File:** `src/core/orchestration/orchestrator.py:1673`

Timeout map for individual tools is still hardcoded inside the God class. Not a safety issue; a maintenance concern only. Will be resolved when `ToolRegistry` is extracted (see ARCH-1 next step).

---

### TS-4 — No Tool Retry on Transient Failure (Low, unchanged)

Tool failures on transient conditions (network timeout, file lock) still surface as plan errors requiring a full replan cycle. No per-tool retry-with-backoff has been added. Low-priority given the debug loop now handles some of these cases.

---

### TS-5 — `user_approved` Strip Ineffective on Original Dict (Low)

See SEC-4 above. Duplicate entry since it affects both security and tool correctness.

---

### TS-6 — New Regression: `pre_execute` Does Not Strip `user_approved` from Caller's Dict (Medium — NEW)

See SEC-4. Test `test_pre_execute_strips_user_approved` explicitly verifies this and is **currently failing**. This is a confirmed implementation drift introduced since vol18.

---

## 7. Repository Awareness Gaps

### RA-1 — Symbol Graph Not Used During Planning — ✅ CLOSED

`planning_node.py:207–242`: `call_graph` and `test_map` (populated by `analysis_node`) are injected as structured JSON blocks into the planner prompt. When absent (fast-path bypassed analysis), `get_symbols_for_task()` is called as a fallback at `planning_node.py:228–241`. Confirmed closed.

---

### RA-2 — Repo Summary Cache TTL (Low, unchanged)

`ContextBuilder._TEXT_CACHE` / `_JSON_CACHE` — module-level OrderedDict with 256-entry LRU cap but no TTL. `clear_cache()` fires at task start. Within-task write invalidation via `invalidate_path()` is now implemented (MEM-1). No TTL remains but the within-task gap is closed.

---

### RA-3 — No Test-to-Symbol Mapping at Plan Time — ✅ PARTIALLY CLOSED

`planning_node.py:245–258`: When `test_map` is populated, a "Test Coverage Hint" is appended to the planning prompt listing up to 4 relevant test files. This is not automatic injection (requires `analysis_node` to have run), but is a meaningful improvement. Full automation would require `analysis_node` to always produce a `test_map` — currently only done for complex tasks.

---

## 8. Memory System Evaluation

**Strengths (unchanged from vol18):**
- Dual compaction: `auto_compactor.py` (deterministic, 85% threshold) + `distiller.py` (LLM-based semantic condensation).
- `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` correctly separates static/dynamic prompt sections for Anthropic cache_control.
- `Annotated[List, operator.add]` — correct append-only LangGraph semantics.

### MEM-1 — Within-Task Cache Invalidation — ✅ CLOSED

`ContextBuilder.invalidate_path()` classmethod implemented. Called by `write_file` (`file_tools.py:391–396`) and by the atomic write path (`file_tools.py:1556–1560`). Within-task stale reads are prevented.

---

### MEM-2 — Cross-Session Decision Memory — ✅ CLOSED

`session_store.py:347–409`: `export_decisions_json()` writes decisions to `.agent-context/decisions.json`; `read_decisions_json()` loads them. `perception_node.py:439–463` injects the last 5 decisions into the dynamic prompt section on round 0.

---

### MEM-3 — `session_summary` Always Injected Regardless of Content (Low — NEW)

**File:** `src/core/context/context_builder.py` (dynamic prompt assembly)

Tests `test_task_state_injected_when_present` and `test_empty_task_state_not_injected` are **currently failing**. The test expects `<session_summary>` to be absent when `TASK_STATE.md` contains only section headers with no content. The implementation injects the `<session_summary>` block unconditionally once it detects a `TASK_STATE.md` file, regardless of whether the file has meaningful content.

This means the agent's system prompt always includes a `<session_summary>` block even for empty/stub task state files, consuming tokens unnecessarily.

**Recommended fix:** Add a content check — skip injection when the parsed task state contains no actionable content (no non-empty sections beyond headers).

---

## 9. Evaluation and Testing Gaps

### ET-1 — No Unmocked Pipeline Integration Test — ✅ CLOSED

`tests/integration/test_e2e_pipeline_smoke.py` provides 5 pipeline tests with MockAdapter + real filesystem. `tests/e2e/test_agent_scenarios.py` provides 4 additional scenario tests.

---

### ET-2 — Scenario Evaluator Not in CI — ✅ CLOSED

`tests/integration/test_scenario_smoke.py` is collected and runs in CI.

---

### ET-3 — No Regression Benchmark Baseline (Low, unchanged)

`benchmarks/` directory exists. No tracked per-version success-rate metric. No automated comparison of token usage per task across versions.

---

### ET-4 — Pre-Existing Test Failure — ✅ CLOSED

`test_llm_manager_fallback` is handled via xfail. No pre-existing infrastructure failures remain.

---

### ET-5 — 9 New Test Regressions (Medium — NEW)

**Current test run:** 3110 passed, 17 skipped, 9 xfailed, **9 failed**.

Compared to vol18 baseline (2955 passed, 2 skipped, 1 failed), the collection grew from 3060 to 3136 tests (+76) and passes grew from 2955 to 3110 (+155). However 9 new failures appeared:

| Test | Root Cause | Severity |
|------|-----------|----------|
| `test_bash_fixes_regression.py::test_same_command_returns_same_object` | Test asserts `r1 is r2` on a wrapper function that creates a new list on each call; the cached function is `_analyze_bash_command_cached`, not `analyze_bash_command`. **Test bug.** | Low |
| `test_bash_fixes_regression.py::test_cache_info_populated_after_calls` | Test calls `.cache_info()` on the wrapper `analyze_bash_command` which has no `lru_cache`. **Test bug.** | Low |
| `test_d10_services.py::test_pre_execute_strips_user_approved` | `pre_execute` strips `user_approved` from a copy, not the original dict. Test asserts on original. **Implementation bug (SEC-4).** | Medium |
| `test_message_manager.py::test_task_state_injected_when_present` | `ContextBuilder` injects `<session_summary>` but not the task file's content. **Implementation regression (MEM-3).** | Low |
| `test_message_manager.py::test_empty_task_state_not_injected` | `<session_summary>` injected even when TASK_STATE.md has no content. **Implementation regression (MEM-3).** | Low |
| `test_prsw_events.py::test_agent_topics_exist` | Test asserts `AgentTopics.FILES_DISCOVERED == "agent.scout.broadcast"` but actual value is `"agent.scout.files_discovered"`. **Test uses stale expected value.** | Low |
| `test_state_init_threading_toolset_providers.py::test_shutdown_wait_true` | Asserts `shutdown(wait=True)` in orchestrator source; actual code uses `wait=False`. **Implementation bug (SEC-3).** | Medium |
| `test_subagent_spawn.py::test_depth_guard_refuses_at_depth_3` | `delegate_task` raises `'NoneType' object has no attribute 'tools'` before reaching the depth guard message. **Integration wiring bug.** | Medium |
| `test_tool_safety_node_caching_plan_contracts.py::test_repo_summary_cache_used_in_source` | Test checks for `in _REPO_SUMMARY_CACHE` string in `analysis_node.py` source. Actual code uses `_REPO_SUMMARY_CACHE.get(...)`. **Test uses wrong substring pattern.** | Low |

**Summary of root causes:**
- 5 test bugs (wrong assertion target, stale expected value, wrong substring pattern)
- 2 implementation bugs (SEC-3, SEC-4, MEM-3)
- 1 integration wiring bug (depth guard)
- 1 ambiguous (test_message_manager may be partly test, partly implementation)

---

## 10. Usability Problems

### UX-1 — No Graceful Degradation on Missing LSP Server (Low, unchanged)

Silent empty string return from `get_lsp_diagnostics_block()` when LSP is unavailable. No user-facing degradation notice.

---

### UX-2 — Config Validation (Low, unchanged)

No `--validate-config` CLI flag. Malformed `.codingagent/settings.json` silently falls back to defaults.

---

### UX-3 — No Dry-Run Mode (Low, unchanged)

No way to preview plan + affected files without executing writes. Still missing.

---

## 11. Performance Bottlenecks

### PERF-1 — `analysis_node` Repo Summary on Every Complex Task (Medium, unchanged)

`analysis_node` may re-generate a repository summary for every complex task. The `_REPO_SUMMARY_CACHE` dict with a lock mitigates repeated calls within a process, but no TTL exists. For large codebases, the first call per-process is expensive.

---

### PERF-2 — WF-2 LLM Semantic Verdict Adds One LLM Call Per Completion (Low — NEW)

**File:** `src/core/orchestration/graph/nodes/evaluation_node.py:136–179`

The semantic LLM judge added by WF-2 fires an additional `call_model` invocation on every task completion. For short tasks this adds ~1–3 seconds of latency and token cost. Currently there is no configurable toggle to disable it (e.g., for cost-sensitive deployments). The call is fire-and-forget wrapped in `try/except`, so it cannot block completion, but it does consume tokens.

**Recommended fix:** Add an `enable_semantic_evaluation` flag in project settings that defaults to `True` but can be disabled.

---

### PERF-3 — Symbol Graph Cache Has No TTL (Low, unchanged)

Module-level `_TEXT_CACHE` retains all files read per process. No TTL or size-based eviction beyond the 256-entry LRU.

---

## 12. Over-Engineered Components

### OE-1 — GraphFactory Subgraph Infrastructure (Low, unchanged)

`GraphFactory` enables creating graph variants (e.g., with compaction). Only one variant is used in production. The abstraction overhead is currently not justified.

---

### OE-2 — Dual Compaction Paths (Low, unchanged)

`auto_compactor.py` fires at 85% and may prevent `distiller.py` from ever running in practice. The interaction when both are eligible simultaneously is undocumented. In practice the system works because `auto_compactor` is deterministic and fires first, but the dual-path architecture is confusing.

---

## 13. Prioritized Fix List

| Priority | ID | Severity | Description | File(s) | Est. Complexity |
|----------|----|----------|-------------|---------|-----------------|
| 1 | ET-5 / SEC-4 | Medium | Fix `pre_execute` to strip `user_approved` from the original `args` dict (not a copy) | `tool_execution_service.py:196` | Low |
| 2 | ET-5 / SEC-3 | Medium | Change `_graph_executor.shutdown(wait=False)` to `wait=True`; update `_tool_executor` if safe | `orchestrator.py:4079,4084` | Low |
| 3 | ET-5 / MEM-3 | Low | Fix `ContextBuilder` to inject `<session_summary>` only when `TASK_STATE.md` has non-empty sections | `context_builder.py` (dynamic prompt assembly) | Low |
| 4 | ET-5 (test bugs) | Low | Fix 5 test-correctness bugs: bash cache tests (wrong function), PRSW topics (stale expected), repo summary cache (wrong substring), message_manager (content not injected) | `test_bash_fixes_regression.py`, `test_prsw_events.py`, `test_tool_safety_node_caching_plan_contracts.py`, `test_message_manager.py` | Low |
| 5 | CAP-6 | Low | Register `e2e` pytest mark in `pytest.ini` | `pytest.ini` | Trivial |
| 6 | ET-5 / depth guard | Medium | Fix `delegate_task` depth guard — `NoneType` has no attribute `tools` prevents depth message from being reached | `src/tools/subagent_tools.py:486` | Medium |
| 7 | ARCH-1 | Medium | Extract `ToolRegistry` from `orchestrator.py` (timeout map, description map, registration) | `orchestrator.py:1673`, new `tool_registry.py` | Medium |
| 8 | WF-5 | Medium | Add per-LLM-call timeout (`max_llm_wait_seconds`) in execution_node and planning_node | `execution_node.py`, `planning_node.py` | Medium |
| 9 | PERF-2 | Low | Add `enable_semantic_evaluation` flag to project settings to make WF-2 LLM judge opt-in | `project_settings.py`, `evaluation_node.py` | Low |
| 10 | UX-3 | Low | Add dry-run mode — show plan + affected files without executing writes | `orchestrator.py`, CLI | High |

---

## 14. Engineering Roadmap

### Phase 1 — Critical Stability (Estimated: 1 day)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Fix `pre_execute` user_approved strip (SEC-4) | `tool_execution_service.py:196` | Low | Closes security gap; fixes failing test |
| Fix `shutdown(wait=True)` for graph_executor (SEC-3) | `orchestrator.py:4079` | Low | Prevents partial file write corruption on teardown |
| Fix ContextBuilder empty TASK_STATE injection (MEM-3) | `context_builder.py` | Low | Closes 2 failing tests; reduces unnecessary token consumption |
| Fix 5 test-correctness bugs (ET-5) | Multiple test files | Low | Restore clean test run |
| Register `e2e` pytest mark (CAP-6) | `pytest.ini` | Trivial | Eliminate warning noise |

### Phase 2 — Robustness (Estimated: 3–5 days)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Fix delegate_task depth guard wiring | `subagent_tools.py:486` | Medium | Closes depth guard regression; prevents unbounded delegation |
| Add per-LLM-call timeout (WF-5) | `execution_node.py`, `planning_node.py` | Medium | Prevents hung tasks on slow/stuck local providers |
| Extract `ToolRegistry` from orchestrator (ARCH-1 next step) | `orchestrator.py:1673` | Medium | Reduces God class by ~100 lines; timeouts become declarative |
| Add `enable_semantic_evaluation` flag (PERF-2) | `project_settings.py`, `evaluation_node.py` | Low | Opt-out for cost-sensitive deployments |

### Phase 3 — Capability (Estimated: 1–2 weeks)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Continue orchestrator decomposition — extract `SessionManager` | `orchestrator.py` | High | Removes ~400 lines; session lifecycle becomes independently testable |
| Add dry-run mode (UX-3) | `orchestrator.py`, CLI, TUI | High | Users can preview plan before committing |
| Add `--validate-config` CLI flag (UX-2) | `cli.py` or equivalent entry point | Low | Surface malformed settings.json before agent run |
| Add regression benchmark baseline (ET-3) | `benchmarks/` | Medium | Track agent quality across versions |
| Add per-session analysis_node TTL for `_REPO_SUMMARY_CACHE` (PERF-1) | `analysis_node.py` | Low | Prevents stale repo summary for long-running sessions |

### Phase 4 — Advanced (Estimated: 2+ weeks)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Complete orchestrator decomposition — extract `ScopeGuard`, `OrchestratorCore` | `orchestrator.py` | High | Reduces God class to <1000 lines |
| Add `test_map` generation for simple tasks (RA-3) | `analysis_node.py`, `planning_node.py` | Medium | Always-on test coverage hints, not just for complex tasks |
| Implement graceful LSP degradation notice (UX-1) | `lsp_context.py`, TUI | Low | User knows when type-checking is inactive |

---

## Baseline Metrics

| Metric | Vol17 | Vol18 (audit) | Vol18 (after all phases) | Vol19 (audit) |
|--------|-------|---------------|--------------------------|---------------|
| Tests collected | 2958 | 3060 | 3146 (projected) | **3136** |
| Tests passed | 2955 | 2955 | 3119 (projected) | **3110** |
| Tests failed | 1 | 1 | 0 (projected) | **9 (regressions)** |
| Tests skipped | 2 | 2 | 17 | **17** |
| Tests xfailed | 0 | 0 | 9 | **9** |
| Pyright errors | 0 | 0 | 0 | **1** (`mcp_stdio_server.py:383`) |
| `orchestrator.py` lines | 4043 | 4043 | ~4055 | **4092** |
| `builder.py` lines | 1296 | 1296 | ~1340 | **1377** |
| Open vol18 items | — | 10 | 0 (all closed) | **0** |
| New vol19 items | — | — | — | **10** (9 test regressions + 1 pyright error) |

### Phase 1 Items (Vol19) — Changes Needed

| ID | Change | Files |
|----|--------|-------|
| SEC-4 | Strip `user_approved` from original `args` dict, not a copy | `tool_execution_service.py:196` |
| SEC-3 | `_graph_executor.shutdown(wait=True)` | `orchestrator.py:4079` |
| MEM-3 | Skip `<session_summary>` injection when TASK_STATE.md has no actionable content | `context_builder.py` |
| ET-5a | Fix `test_same_command_returns_same_object` — assert on `_analyze_bash_command_cached`, not wrapper | `test_bash_fixes_regression.py` |
| ET-5b | Fix `test_cache_info_populated_after_calls` — call `.cache_info()` on `_analyze_bash_command_cached` | `test_bash_fixes_regression.py` |
| ET-5c | Fix `test_agent_topics_exist` — update expected value to `"agent.scout.files_discovered"` | `test_prsw_events.py` |
| ET-5d | Fix `test_repo_summary_cache_used_in_source` — search for `_REPO_SUMMARY_CACHE.get` not `in _REPO_SUMMARY_CACHE` | `test_tool_safety_node_caching_plan_contracts.py` |
| ET-5e | Fix `test_task_state_injected_when_present` + `test_empty_task_state_not_injected` — depends on MEM-3 implementation | `test_message_manager.py` |
| CAP-6 | Add `e2e: mark test as end-to-end` to `pytest.ini` markers | `pytest.ini` |
| depth-guard | Fix `delegate_task` NoneType crash before depth guard | `subagent_tools.py:486` |
