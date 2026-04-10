# Audit Report — Vol20

**Date:** 2026-04-09
**Auditor:** OpenCode agent (claude-sonnet-4.6)
**Scope:** Full-spectrum architectural audit — all 15 required categories.
**Baseline:** 3206 passed, 4 skipped, 0 failed, 4 warnings. Test collection: 3210 tests.
**Previous audit:** Vol19 — reported 10 open items (9 test regressions + 1 Pyright error). Phase 1 was the target for this session.
**Post-report addendum (session 1):** 15 additional Pyright errors found and fixed across 5 files; WF-5 guard extended to `perception_node.py`; `test_lmstudio_end_to_end.py` missing `@pytest.mark.lmstudio` marker fixed.
**Post-report addendum (session 2):** All Phase 1–3 roadmap items confirmed closed or resolved. ARCH-1 ToolRegistry verified complete. RA-3 test_map verified already implemented for both simple and complex tasks. OE-1 dead routing functions confirmed non-removable (actively used by `graph_factory.py` and test suite). UX-1 `get_lsp_status_notice()` wired into `instruction_loader.py` so the degradation notice appears in the system prompt. All 4 previously open items are now resolved.

---

## Executive Summary

Vol20 finds the agent in the cleanest test state yet recorded: **3206 passed, 4 skipped, 0 failed**. All 9 Vol19 test regressions are closed. The 1 Pyright error in `mcp_stdio_server.py` is suppressed. A previously undetected hang in `test_shutdown_clears_started_flag` (15 s LSP JSON-RPC wait on a MagicMock process) was found and fixed. All 5 live-provider LM Studio integration test files now have a 2-second reachability probe so they skip gracefully when LM Studio is unreachable rather than hanging indefinitely.

The major structural finding is that `orchestrator.py` has been successfully decomposed into three focused files: `orchestrator.py` (375 lines, clean delegation shell), `session_manager.py` (339 lines), and `orchestrator_bootstrap.py` (446 lines). This closes ARCH-1 from Vol19 more completely than anticipated.

Post-report, 15 additional Pyright errors were discovered across 5 files (`orchestrator.py`, `orchestrator_bootstrap.py`, `tool_execution_pipeline.py`, `inference_loop.py`, `execution_trace.py`) — all caused by the same pattern of inline type annotations on attributes of an external object (`orch.attr: Type = val`). All 15 are fixed. WF-5 timeout guard was extended to cover `perception_node.py` (it was already in `execution_node.py` and `planning_node.py`). `test_lmstudio_end_to_end.py` was missing `@pytest.mark.lmstudio`, causing it to run as a live LLM test in the non-live integration suite when LM Studio happened to be reachable — fixed.

No Critical-severity issues remain. All previously open items are now resolved: (1) ARCH-1 ToolRegistry — `tool_registry.py` already contains the canonical timeout map and full delegation chain; no duplicate map exists in `orchestrator_bootstrap.py`. (2) RA-3 — `analysis_node.py` already generates `test_map` for both simple tasks (`_build_lightweight_test_map`) and complex tasks. (3) OE-1 — "dead" routing functions cannot be removed; `graph_factory.py` and 10+ test files import them directly; marked as intentionally retained. (4) UX-1 — `get_lsp_status_notice()` wired into `instruction_loader.py` (system prompt injection).

---

## Vol19 Phase 1 Item Status

| ID | Vol19 Status | Vol20 Status | Evidence |
|----|-------------|-------------|----------|
| SEC-4 | Phase 1 — OPEN | ✅ CLOSED | `tool_execution_service.py`: `args.pop("user_approved", None)` now operates on original dict; `test_pre_execute_strips_user_approved` passes |
| SEC-3 | Phase 1 — OPEN | ✅ CLOSED | `orchestrator_bootstrap.py`: `_graph_executor.shutdown(wait=True)`; `test_shutdown_wait_true` passes |
| MEM-3 | Phase 1 — OPEN | ✅ CLOSED | `context_builder.py`: `<session_summary>` block skipped when TASK_STATE.md has no non-empty sections; both `test_message_manager` tests pass |
| ET-5a | Phase 1 — OPEN | ✅ CLOSED | `test_bash_fixes_regression.py`: asserts on `_analyze_bash_command_cached` (the lru_cache wrapper); passes |
| ET-5b | Phase 1 — OPEN | ✅ CLOSED | `test_bash_fixes_regression.py`: `.cache_info()` called on `_analyze_bash_command_cached`; passes |
| ET-5c | Phase 1 — OPEN | ✅ CLOSED | `test_prsw_events.py`: expected value updated to `"agent.scout.files_discovered"`; passes |
| ET-5d | Phase 1 — OPEN | ✅ CLOSED | `test_tool_safety_node_caching_plan_contracts.py`: checks `_REPO_SUMMARY_CACHE.get`; passes |
| ET-5e | Phase 1 — OPEN | ✅ CLOSED | Both `test_message_manager` tests pass (depends on MEM-3) |
| CAP-6 | Phase 1 — OPEN | ✅ CLOSED | `pytest.ini`: `e2e` marker registered; no `PytestUnknownMarkWarning` |
| depth-guard | Phase 1 — OPEN | ✅ CLOSED | `subagent_tools.py:256–271`: depth guard fires before any attribute access; `test_depth_guard_refuses_at_depth_3` passes |

All 10 Vol19 Phase 1 items are closed.

---

## Vol19 Phase 2 Item Status

| ID | Vol19 Status | Vol20 Status | Evidence |
|----|-------------|-------------|----------|
| WF-5 | Phase 2 — OPEN | ✅ CLOSED | `execution_node.py`: `asyncio.wait_for` guard; `planning_node.py`: deadline-based poll guard; `perception_node.py`: deadline-based poll guard added post-report. All three LLM call sites covered. |
| ARCH-1 (ToolRegistry) | Phase 2 — OPEN | ✅ CLOSED | `tool_registry.py` already contains `_TOOL_TIMEOUTS`, `DEFAULT_TOOL_TIMEOUT`, and `get_tool_timeout()`. Delegation chain: `orchestrator.py._get_tool_timeout()` → `orchestrator_helpers._get_tool_timeout_impl()` → `tool_registry.get_tool_timeout()`. No duplicate map anywhere in `orchestrator_bootstrap.py`. |
| PERF-2 (enable_semantic_evaluation) | Phase 2 — OPEN | ✅ CLOSED | `project_settings.py`: `enable_semantic_evaluation: bool = False` (opt-in); `evaluation_node.py` reads the flag before firing the LLM judge. |

---

## Vol19 Phase 3 Item Status

All 5 Phase 3 items were confirmed implemented in the prior session:

| ID | Vol19 Status | Vol20 Status | Evidence |
|----|-------------|-------------|----------|
| ARCH-1 (SessionManager) | Phase 3 — OPEN | ✅ CLOSED | `session_manager.py` (339 lines) fully extracts session state; `orchestrator.py` is 375 lines |
| UX-3 (dry-run) | Phase 3 — OPEN | ✅ CLOSED | `src/main.py`: `--dry-run` flag with `DRY_RUN_BLOCKED_TOOLS` constant |
| UX-2 (--validate-config) | Phase 3 — OPEN | ✅ CLOSED | `src/main.py`: `_run_validate_config()` implemented |
| ET-3 (benchmark baseline) | Phase 3 — OPEN | ✅ CLOSED | `benchmarks/baseline.json` + `benchmarks/bench_pipeline.py` + `tests/unit/test_benchmark_baseline.py` |
| PERF-1 (per-session TTL) | Phase 3 — OPEN | ✅ CLOSED | `clear_repo_summary_cache()` in `analysis_node.py`; called in `orchestrator_bootstrap.py` on every new `Orchestrator()` |

---

## New Findings This Session

### BUG-V20-01 — `test_shutdown_clears_started_flag` Hung 15 Seconds (Fixed)

**File:** `tests/unit/test_lsp_auto_restart.py:81`

The test set `client._proc = _crashed_proc(returncode=None)` (a `MagicMock`) but did not patch `_request` or `_notify`. `shutdown()` reached `await self._request("shutdown", {})`, which internally called `await self._proc.stdin.drain()` on the mock. The mock `drain()` returned a non-coroutine, which was swallowed by the `try/except` in `_send`. The `asyncio.Future` in `_pending` was then never resolved, so `asyncio.wait_for(fut, timeout=15.0)` blocked for the full 15 seconds before timing out and returning.

Without `--timeout=10`, this test always "passed" by silently waiting 15 s then proceeding to the assertion. With `--timeout=10`, it failed.

**Fix applied:** Patch `_request` and `_notify` with `AsyncMock(return_value=None)` inside the test so `shutdown()` completes instantly.

---

### BUG-V20-02 — Live LM Studio Tests Hung When LM Studio Unreachable (Fixed)

**Files:** 5 integration test files (`test_lm_studio_live_pipeline.py`, `test_lmstudio_end_to_end.py`, `test_orchestrator_lmstudio_e2e.py`, `test_system_prompts_against_lmstudio.py`, `test_system_prompts_json_mode.py`)

Each file auto-enabled itself by reading `providers.json` for an `lm_studio` entry without verifying LM Studio was actually reachable. When LM Studio was not running, tests attempted real LLM calls and hung indefinitely.

**Fix applied:** Added a 2-second HTTP reachability probe (`requests.get(f"{_base}/models", timeout=2)`) before setting `RUN = True`. Two files also add `not os.getenv("CI")` to prevent accidental activation in CI. Mirrors the pattern already used in `test_system_prompts_against_ollama.py`.

---

### BUG-V20-03 — `test_delegate_task_valid_roles` Deadlocked After Full Suite (Fixed, prior session)

**File:** `tests/unit/test_subagent_tools.py`

Called real `delegate_task()` which ran `graph.ainvoke()` inside a `ThreadPoolExecutor` with 300 s timeout. After the full unit suite with a polluted global executor state this could deadlock.

**Fix applied:** Mock `GraphFactory.get_graph` to return a graph stub with `AsyncMock` for `ainvoke`. Test runs in 1.72 s.

### BUG-V20-04 — 15 Pyright Errors: Inline Annotations on External Object (Fixed post-report)

**Files:** `orchestrator.py`, `orchestrator_bootstrap.py`, `tool_execution_pipeline.py`, `inference_loop.py`, `execution_trace.py`

All 15 errors had the same root cause: `orch.attr: SomeType = value` — a type annotation on an attribute of an external object. Python only supports this syntax for `self` inside a class body, not for arbitrary objects. Pyright correctly flags every instance as "Type annotation not supported for this statement".

Additionally, `orchestrator.py` accessed `self.session_mgr`, `self._graph_executor`, and `self._tool_executor` which are set by `bootstrap_orchestrator()` after `__init__` returns, so Pyright reported them as unknown attributes. The fallback `_format_tool_result` function in `tool_execution_pipeline.py` had parameter names (`res`, `name`) mismatched from the real signature (`result`, `tool_name`). The `register_tool_gate` fallback was `None`, causing Pyright to flag the call site as "Object of type None cannot be called".

**Fixes applied:**
- `orchestrator.py`: Added stub attribute declarations (`self.session_mgr: _SM`, `self._graph_executor: _cf.ThreadPoolExecutor`, `self._tool_executor: _cf.ThreadPoolExecutor`) in `__init__` before the `bootstrap_orchestrator()` call so Pyright knows they exist.
- `orchestrator_bootstrap.py` (10 sites): Replaced `orch.attr: Type = val` with `orch.attr = val  # type: ignore[attr-defined]`.
- `tool_execution_pipeline.py`: Replaced `None` fallback for `register_tool_gate` with a lambda stub; aligned fallback `_format_tool_result` parameter names to match real signature; removed inline annotation on `orch._dry_run_log`.
- `inference_loop.py`: Removed inline annotation on `orch._dry_run_log`.
- `execution_trace.py`: Removed inline annotation on `orch._execution_trace_buffer`.

---

### BUG-V20-05 — `test_lmstudio_end_to_end` Missing `@pytest.mark.lmstudio` (Fixed post-report)

**File:** `tests/integration/test_lmstudio_end_to_end.py`

The test used `@pytest.mark.skipif(not RUN, ...)` but not `@pytest.mark.lmstudio`. The other four LM Studio test files all use `pytestmark = pytest.mark.lmstudio`. When LM Studio was reachable, `RUN` was set to `True` by the reachability probe, the `skipif` was bypassed, and the test ran a real LLM call — causing it to appear in the non-live integration suite (filtered by `-m "not lmstudio"`) and time out.

**Fix applied:** Added `@pytest.mark.lmstudio` decorator. Test is now correctly deselected (27 deselected instead of 26).

---

| Component | Lines | Status |
|-----------|-------|--------|
| `orchestrator.py` | 375 | Clean delegation shell — no God class |
| `session_manager.py` | 339 | Fully extracted session state |
| `orchestrator_bootstrap.py` | 446 | Bootstrap wiring + tool registration |
| `builder.py` | 1445 | DAG builder; dead routing fns documented |
| `permission_gateway.py` | 374 | All 5 security gates |
| `loop_guards.py` | 426 | Canonical fingerprint + alternating-loop detection |

**Strength:** `orchestrator.py` decomposition is now substantially complete. The remaining ARCH-1 item (ToolRegistry extraction from `orchestrator_bootstrap.py`) is cosmetic.

**Open:** "Dead" routing functions `should_after_planning`, `should_after_execution`, `should_after_execution_with_replan`, `should_after_verification` remain in `builder.py`. They are **not removable** — `graph_factory.py` uses `should_after_planning` directly, `should_after_execution` is called by `should_after_execution_with_replan`, and multiple test files import all three. The OE-1 precondition ("GraphFactory subgraph usage confirmed unused") is **not met**. Functions are documented with "NOT WIRED IN main graph" and retained intentionally.

---

## 2. Security

### SEC-1 — WorkspaceGuard ✅ CONFIRMED CLOSED

All 6 write tools check `guard_operation()`. Unchanged.

### SEC-2 — Write-Tool Enforcement Sets Synced ✅ CONFIRMED CLOSED

`WRITE_TOOLS_REQUIRING_READ` in `orchestrator_bootstrap.py` and `MODIFYING_TOOLS` in `loop_guards.py` are identical. Unchanged.

### SEC-3 — Graph Executor Shutdown `wait=False` ✅ CLOSED (Vol19 Phase 1)

`_graph_executor.shutdown(wait=True)` in `orchestrator_bootstrap.py`. `test_shutdown_wait_true` passes.

### SEC-4 — `user_approved` Strip on Copy Not Original ✅ CLOSED (Vol19 Phase 1)

`tool_execution_service.py:196` now strips from original dict. `test_pre_execute_strips_user_approved` passes.

---

## 3. Workflow Reliability

### WF-5 — LLM Call Timeout ✅ CLOSED (post-report)

`max_llm_wait_seconds` is read from `project_settings.py` in all three LLM graph nodes:

- `execution_node.py`: `asyncio.wait_for(..., timeout=max_llm_wait_seconds)` around the step-generation call.
- `planning_node.py`: deadline-based poll guard (`_deadline = loop.time() + _llm_timeout`) inside the `while not llm_task.done()` loop; returns `wait_for_user` on timeout.
- `perception_node.py`: same deadline-based poll guard added post-report; returns `wait_for_user` on timeout.

All three LLM call sites in the graph are now guarded. A stuck local provider can no longer hang the agent indefinitely.

---

## 4. Memory System

### MEM-3 — Empty TASK_STATE Injection ✅ CLOSED (Vol19 Phase 1)

`context_builder.py` skips `<session_summary>` block when TASK_STATE.md has only header lines with no content.

### MEM-1/MEM-2 — Cache Invalidation and Decision Memory ✅ CONFIRMED CLOSED

Both confirmed from Vol19. Unchanged.

---

## 5. Test Suite Health

| Suite | Command | Result |
|-------|---------|--------|
| Unit | `pytest tests/unit --timeout=10` | **3206 passed, 4 skipped, 0 failed** |
| Non-live integration | `pytest tests/integration -m "not lmstudio and not ollama and not integration"` | **142 passed, 27 deselected, 0 failed** |
| Live LM Studio | `pytest tests/integration -m lmstudio` (LM Studio running) | Expected pass (reachability probe guards skip; `lmstudio` mark now correctly applied to all 5 files) |

**New tests added this session:** 0 (only fixes applied).

---

## 6. Prioritized Fix List (All Items Resolved)

| Priority | ID | Severity | Description | Status | Evidence |
|----------|----|----------|-------------|--------|----------|
| 1 | ARCH-1 | Low | Extract `ToolRegistry` from `orchestrator_bootstrap.py` | ✅ Already done | `tool_registry.py` has canonical map; delegation chain confirmed |
| 2 | RA-3 | Low | Generate `test_map` for simple tasks | ✅ Already done | `analysis_node._build_lightweight_test_map()` runs for all tasks (lines 99–137, 227–243) |
| 3 | OE-1 | Low | Remove dead routing fns from `builder.py` | ✅ Deferred — non-removable | `graph_factory.py` + 10+ test files import them; precondition not met |
| 4 | UX-1 | Low | Graceful degradation notice when LSP unavailable | ✅ Closed | `get_lsp_status_notice()` wired into `instruction_loader.py` (lines 216–224) |

---

## 7. Engineering Roadmap

### Phase 1 — All Closed ✅

All Phase 1 items were resolved this session. No open Phase 1 items remain.

### Phase 2 — All Closed ✅

| Task | Status | Evidence |
|------|--------|----------|
| Always-on test_map for simple tasks (RA-3) | ✅ Already done | `analysis_node._build_lightweight_test_map()` |
| LSP graceful degradation notice (UX-1) | ✅ Done this session | `instruction_loader.py:216–224` |

### Phase 3 — Carry-Forward

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Complete orchestrator decomposition — extract `ScopeGuard`, `OrchestratorCore` | `orchestrator_bootstrap.py` | High | Final reduction of orchestrator to <300 lines |
| Tool retry on transient failure (TS-4) | `tool_execution_service.py` | Medium | Fewer spurious replans on network/lock errors |

---

## Baseline Metrics

| Metric | Vol18 | Vol19 | Vol20 |
|--------|-------|-------|-------|
| Tests collected | 3060 | 3136 | **3210** |
| Tests passed | 2955 | 3110 | **3206** |
| Tests failed | 1 | 9 | **0** |
| Tests skipped | 2 | 17 | **4** |
| Tests xfailed | 0 | 9 | **0** |
| Pyright errors | 0 | 1 | **0** (suppressed with `# type: ignore`) |
| `orchestrator.py` lines | 4043 | 4092 | **375** (decomposed) |
| `session_manager.py` lines | — | — | **339** (new) |
| `orchestrator_bootstrap.py` lines | — | — | **446** (new) |
| `builder.py` lines | 1296 | 1377 | **1445** |
| Open vol19 Phase 1 items | — | 10 | **0** |
| New vol20 items | — | — | **0 open** (all 4 items resolved) |
