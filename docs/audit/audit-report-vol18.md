# Audit Report — Vol18

**Date:** 2026-04-06
**Auditor:** OpenCode agent (claude-sonnet-4.6)
**Scope:** Full-spectrum architectural audit — all 15 required categories.
**Baseline:** 2955 passed, 2 skipped, 1 failed (pre-existing `test_llm_manager_fallback`). Test collection at audit time: 3060 tests.
**Previous audit:** Vol17 — closed CP-10 (LSP diagnostics injection) and CP-12 (Anthropic `cache_control` wiring). All claw-code parity items now closed.

---

## Executive Summary

The agent is functionally capable and passes a large test suite with 0 Pyright errors. All previously identified claw-code parity gaps are closed. The pipeline correctly implements perception → analysis → planning → execution → verification → evaluation with retry limits, loop guards, snapshot rollback, and token budgeting.

However several significant architectural debts have accumulated:

1. `orchestrator.py` at 4043 lines is a textbook God class that combines tool dispatch, permission gates, session management, cost tracking, MCP server management, preview service, plan mode, scope guard, and graph execution in a single file.
2. `builder.py` (1296 lines) contains both graph wiring logic and all routing functions, several of which are dead code not wired into the live graph.
3. `WorkspaceGuard` exists as a safety component but its `guard_operation()` return value is never checked in the hot paths of `bash()` or `write_file()`.
4. The two read-before-write enforcement sets (`WRITE_TOOLS_REQUIRING_READ` in orchestrator, `MODIFYING_TOOLS` in loop_guards) are inconsistent — three tools are missing from `loop_guards.py` and one is missing from `orchestrator.py`.
5. The `_task_is_complex()` routing heuristic is keyword-based and fragile.
6. The scenario evaluator is not wired into CI.
7. No true end-to-end integration test exercises the full pipeline without mocks.

No Critical-severity issues were found. The most impactful near-term actions are architectural decomposition of `orchestrator.py`, syncing the write-tool sets, and integrating `WorkspaceGuard` properly.

---

## 1. Architecture Strengths

| Strength | Detail |
|----------|--------|
| LangGraph workflow | Clean DAG: perception → analysis → analyst_delegation → planning → plan_validator → execution → step_controller → verification → evaluation → memory_sync → delegation/end. Deterministic routing via typed conditional edges. |
| Graph singleton with test reset | `_get_compiled_graph()` caches the compiled graph; `_reset_compiled_graph()` enables test isolation. |
| Multi-layer security | Gate 1 (DANGEROUS_PATTERNS string match) + Gate 2 (AST-level `bash_security.py`) + Gate 3 (SAFE_COMMANDS allowlist) + Gate 4 (flag inspection) + Gate 5 (scope guard). Defense in depth. |
| Token budget monitoring | `TokenBudgetMonitor` singleton tracks per-session usage; 70% warn / 85% compact thresholds trigger deterministic compaction via `auto_compactor.py` before context overflow. |
| Snapshot rollback | `SnapshotManager` provides git-stash-backed rollback. The agent can undo file changes after a failed verification cycle. |
| Plan persistence | `last_plan.json` written by `planning_node`, loaded on session resume — correct for crash recovery. |
| Anthropic cache_control | `AnthropicAdapter` correctly splits system prompt on `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` and sets `cache_control: ephemeral` on the static block (CP-12). |
| LSP diagnostics auto-injection | Live type errors and lint warnings are injected into the dynamic system prompt section at every turn (CP-10). |
| Per-project settings | `project_settings.py` supports `.codingagent/settings.json` overrides. |
| Idempotency guard | `_seen_calls` prevents identical tool calls within a single turn; reset at turn start. |

---

## 2. Critical Architectural Flaws

### ARCH-1 — `orchestrator.py` God Class (High)

**File:** `src/core/orchestration/orchestrator.py` (4043 lines)

`CodingAgentOrchestrator` combines:
- LangGraph graph creation and invocation
- Tool registration (all ~40 tools)
- Permission gate logic (5 gates, ~200 lines)
- Scope guard (`_affected_files` enforcement)
- Session management (UUID, title generation, cost tracking)
- MCP server lifecycle
- Preview service lifecycle
- Plan mode handling
- Idempotency guard
- Read-before-write enforcement
- Worker thread management

No single class should carry this many responsibilities. The consequences are: (a) unit tests must mock enormous surface area; (b) adding new permission logic risks unintended interactions; (c) the file is nearly impossible to review in a single sitting.

**Recommended decomposition:**
- `ToolRegistry` — tool registration, timeout map, description map
- `PermissionGateway` — all 5 gates as a dedicated class
- `SessionManager` — UUID, title, version, cost
- `ScopeGuard` — `_affected_files`, read-before-write enforcement
- `OrchestratorCore` — graph invocation, turn loop

---

### ARCH-2 — Dead Routing Functions in `builder.py` (Medium)

**File:** `src/core/orchestration/graph/builder.py`

Four routing functions exist but are explicitly documented as NOT wired into `compile_agent_graph()`:

| Function | Line | Status |
|----------|------|--------|
| `should_after_planning` | 32 | Dead — comment says "NOT WIRED" |
| `should_after_execution` | 325 | Dead — superseded by `route_execution` |
| `should_after_execution_with_replan` | 485 | Wired only in `GraphFactory` subgraphs (not main graph) |
| `should_after_verification` | 520 | Dead — "NOT WIRED IN compile_agent_graph()" |

`route_execution` (line 1042, 219 lines) is the live router. The dead functions add maintenance confusion — future developers may wire them by mistake or modify them without effect.

---

### ARCH-3 — `route_execution` Monolithic Router (Medium)

**File:** `src/core/orchestration/graph/builder.py:1042`

At 219 lines, `route_execution` handles fast-path detection, plan mode, step controller, loop guard checking, and delegation routing in a single function. This violates the single-responsibility principle and makes the routing logic difficult to test in isolation.

---

### ARCH-4 — `AgentState` Mega-TypedDict (Medium)

**File:** `src/core/orchestration/graph/state.py`

`AgentState` has 195 fields across a single `TypedDict`. While LangGraph requires a flat state dict for graph nodes, the lack of logical grouping (e.g., `PlanState`, `ExecutionState`, `MemoryState`) makes it hard to reason about state ownership and lifecycle.

---

## 3. High-Risk Safety Issues

### SEC-1 — `WorkspaceGuard` Not Integrated Into Hot Paths (Medium) — ✅ CLOSED

**File:** `src/core/orchestration/workspace_guard.py`, `src/tools/file_tools.py`

**Finding at audit time:** `WorkspaceGuard` was described as unintegrated. Post-audit deep-read confirmed this finding was **already resolved** — `write_file`, `edit_file`, `delete_file`, `edit_by_line_range`, `edit_file_atomic`, and `multiedit` all call `guard.guard_operation()` and return an error dict when `status == "error"` (Phase 4.3 implementation, `file_tools.py:303–307`, `519–523`, `678–682`, `1256–1259`, `1429–1432`, `1609–1612`).

The residual gap is `bash()` — pre-plan fast-path shell commands that write files via shell redirection (e.g. `echo > foo.py`) bypass the guard. This is addressed by the existing `DANGEROUS_PATTERNS` Gate 1 + `SAFE_COMMANDS` Gate 3 in `bash()`, which prevents direct shell file writes. No further action needed for Phase 2.

---

### SEC-2 — Inconsistent Write-Tool Enforcement Sets (Medium)

Two independent read-before-write enforcement mechanisms use slightly different tool sets:

**`orchestrator.py::WRITE_TOOLS_REQUIRING_READ` (line 36):**
```
edit_file, edit_file_atomic, write_file, edit_by_line_range,
apply_patch, delete_file, rename_file, ast_rename
```

**`loop_guards.py::MODIFYING_TOOLS` (line 64):**
```
edit_file, edit_by_line_range, apply_patch, write_file,
delete_file, manage_todo
```

Missing from `loop_guards.py`: `edit_file_atomic`, `rename_file`, `ast_rename`
Missing from `orchestrator.py`: `manage_todo`

A call to `edit_file_atomic` bypasses the loop-guard read-before-write check. A call to `manage_todo` bypasses the orchestrator-level read-before-write check. These are not exploitable as catastrophic vulnerabilities but represent inconsistent enforcement.

---

### SEC-3 — Session Title Generation in Daemon Thread (Low)

**File:** `src/core/orchestration/orchestrator.py`

Session title generation fires an LLM call in a daemon thread (via `asyncio.create_task` or `threading.Thread`). If this thread raises an unhandled exception it is silently swallowed. This is low-risk for correctness but can hide inference failures that are useful for debugging.

---

## 4. Major Missing Capabilities

### CAP-1 — No Unmocked End-to-End Integration Test (High)

There are 3060 unit tests and some mock-based integration tests under `tests/integration/`. However there is no test that exercises the full LangGraph pipeline (perception → planning → execution → verification) against a real (but sandboxed) repository without mocking the LLM responses. This means routing bugs in `route_execution`, timing interactions between `auto_compactor` and `verification_node`, and snapshot rollback correctness are never exercised together.

---

### CAP-2 — Scenario Evaluator Not Wired Into CI (Medium)

**File:** `src/core/evaluation/scenario_evaluator.py`

`scenario_evaluator.py` implements a structured scenario evaluation framework (task description → expected outcome → pass/fail). It is not run in CI (`benchmarks/` directory exists but is excluded from `pytest` collection). There is no automated measurement of agent task success rate.

---

### CAP-3 — No Retrieval-Augmented Planning (Medium)

**File:** `src/core/orchestration/graph/nodes/planning_node.py`

The repo indexer (`repo_indexer.py`) builds a full symbol graph and semantic search index. However `planning_node` does not query the symbol graph or semantic index before generating a plan. Planning is done purely from the LLM's context window contents. Relevant symbols, call sites, and test locations are not automatically retrieved and injected into the planning prompt.

---

### CAP-4 — No Proactive Debugging (Medium)

`debug_node` fires only after a `verification_node` failure. There is no proactive debugging step that, for example, catches tool execution errors mid-plan and adjusts. Error signals from failed tool calls accumulate in state but do not trigger early re-routing.

---

### CAP-5 — No Performance Benchmarks in CI (Low)

`benchmarks/` directory exists but benchmarks are not run in CI. There is no automated tracking of token usage per task, LLM call latency, or plan quality over time.

---

## 5. Workflow Reliability Issues

### WF-1 — `_task_is_complex()` Keyword Heuristic (Medium)

**File:** `src/core/orchestration/graph/builder.py:192`

```python
def _task_is_complex(state: Mapping[str, Any]) -> bool:
```

This function determines whether to route through `analyst_delegation` (which adds 2 extra LLM calls). It is keyword-based: matching strings like `"codebase"`, `"refactor"`, `"architecture"` in the task description. This causes:

- False positives: simple tasks mentioning "codebase" unnecessarily invoke the analyst.
- False negatives: genuinely complex tasks that don't match keywords bypass analysis.

A better approach: use a lightweight classification LLM call, or delegate the decision to `perception_node` via a state flag.

---

### WF-2 — `evaluation_node` is Purely Rule-Based (Medium)

**File:** `src/core/orchestration/graph/nodes/evaluation_node.py`

`evaluation_node` determines "complete" / "replan" / "debug" using deterministic rules (plan completion flags + verification result). It does not use an LLM to assess whether the executed changes actually satisfy the original task intent. This can result in false "complete" verdicts when all plan steps are checked off but the implementation is semantically incorrect.

---

### WF-3 — No Step-Level Verification (Medium)

The step controller (`step_controller_node`) advances through plan steps, but there is no per-step verification that the step's changes are correct before proceeding to the next step. Verification only runs at the end of all steps. A bad step early in the plan can propagate errors through subsequent steps before detection.

---

### WF-4 — Replan Loop Has No Divergence Detection (Low)

The evaluation → replan loop has a maximum iteration counter, but when replanning produces the same plan as the previous attempt (e.g., the LLM is stuck), there is no plan-diff check to detect this and escalate to the user rather than burning iterations.

---

## 6. Tool System Weaknesses

### TS-1 — `manage_todo` Missing from `WRITE_TOOLS_REQUIRING_READ` (Medium)

See SEC-2. `manage_todo` writes `TODO.md` and is enforced in `loop_guards.py` but not in the orchestrator-level read-before-write check.

---

### TS-2 — `edit_file_atomic`, `rename_file`, `ast_rename` Missing from `MODIFYING_TOOLS` (Medium)

See SEC-2. These three tools are enforced at the orchestrator level but bypass the loop-guard read-before-write check.

---

### TS-3 — Tool Timeout Map Hardcoded in Orchestrator (Low)

**File:** `src/core/orchestration/orchestrator.py:1673`

Tool timeouts (e.g., `"edit_file_atomic": 30`) are hardcoded in `orchestrator.py`. Adding a new tool requires editing the timeout map inside the God class rather than declaring the timeout on the tool definition itself.

---

### TS-4 — No Tool Retry on Transient Failure (Low)

When a tool call raises a transient exception (network timeout, file lock, etc.) it surfaces as a tool error in state with no automatic retry. The agent must detect the error in `verification_node` and replan. A per-tool retry-with-backoff would handle transient failures without consuming a full replan cycle.

---

## 7. Repository Awareness Gaps

### RA-1 — Symbol Graph Not Used During Planning (Medium)

**File:** `src/core/indexing/repo_indexer.py`, `src/core/orchestration/graph/nodes/planning_node.py`

The indexer builds cross-language symbol graphs with call-site tracking. This data is not queried during `planning_node` execution. The planner has no automated way to discover: "which files import the function I'm about to change", "which tests cover this module", "what is the call graph around this symbol".

---

### RA-2 — Repo Summary Cached Per-Process, No TTL (Low)

**File:** `src/core/context/context_builder.py`

`_TEXT_CACHE` and `_JSON_CACHE` (module-level LRU, 256 entries) have no TTL. If the repository changes significantly during a long session, cached file content and summaries may become stale without invalidation. `ContextBuilder.clear_cache()` is called at task start (fix from vol ≤ 15), which mitigates inter-task staleness but not within-task staleness when the agent itself modifies files.

---

### RA-3 — No Test-to-Symbol Mapping Used at Plan Time (Low)

The repo indexer includes test file detection, but there is no automated injection of "tests covering this module" into the planning prompt. When the agent plans to modify a function, it must discover relevant tests organically via tool calls rather than having them pre-loaded.

---

## 8. Memory System Evaluation

The memory system is well-designed overall.

**Strengths:**
- Dual compaction: deterministic `auto_compactor.py` (token-count-triggered at 85%) + LLM-based `distiller.py` (semantic condensation).
- `TokenBudgetMonitor` singleton provides consistent usage tracking across the session.
- `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` correctly separates static (cacheable) and dynamic (per-turn) system prompt sections.
- Message history uses `Annotated[List, operator.add]` — correct append-only semantics for LangGraph state.

**Weaknesses:**

### MEM-1 — No Within-Task Cache Invalidation (Low)

`ContextBuilder._TEXT_CACHE` is module-level and cleared at task start. When the agent writes a file during execution, the cached version of that file in `_TEXT_CACHE` is now stale for the remainder of the task. Subsequent reads of the same file via `context_builder` (e.g., in system prompt assembly) will return the pre-edit content.

---

### MEM-2 — No Decision Memory Across Sessions (Low)

There is no persistent store of "decisions made" (e.g., "chose approach X over Y because Z") across sessions. Each session starts from scratch with only the git history as implicit context. Long-running projects would benefit from a structured decision log that survives session boundaries.

---

## 9. Evaluation and Testing Gaps

### ET-1 — No Unmocked Pipeline Integration Test (High)

See CAP-1. The test suite is extensive (3060 tests) but is entirely unit-level. There is no test that wires up the real LangGraph graph, invokes it with a synthetic task, and asserts on the final state. Routing logic, state transitions, and compaction interactions are tested only indirectly.

---

### ET-2 — Scenario Evaluator Not in CI (Medium)

See CAP-2. `scenario_evaluator.py` defines a structured pass/fail framework but is not invoked by `pytest` or any CI step.

---

### ET-3 — No Regression Benchmark Baseline (Low)

There is no tracked metric for "what fraction of standard tasks does this agent complete successfully across versions". Without this, regressions in agent quality are invisible until users report them.

---

### ET-4 — Pre-Existing Test Failure Unresolved (Low)

`test_llm_manager_fallback` requires a live LM Studio instance and has been pre-existing failing since at least vol17. It should either be marked `pytest.mark.skip(reason="requires live LM Studio")` or the test should be rewritten to use a mock server.

---

## 10. Usability Problems

### UX-1 — No Graceful Degradation on Missing LSP Server (Low)

If the LSP server is unavailable, `get_lsp_diagnostics_block()` returns `""` silently. There is no user-facing message explaining that type checking is degraded. A developer who expects diagnostics injection may not realize the feature is inactive.

---

### UX-2 — Configuration Discovery Requires Documentation (Low)

Per-project settings live in `.codingagent/settings.json`. There is no `--validate-config` CLI flag or startup validation that tells the user whether their config is syntactically correct. Malformed JSON in settings silently falls back to defaults.

---

### UX-3 — No Dry-Run Mode (Low)

There is no way to run the agent in a dry-run mode that shows what plan it would generate and what files it would modify without executing any writes. This makes it difficult for users to preview the agent's intent before committing to an action.

---

## 11. Performance Bottlenecks

### PERF-1 — `analysis_node` Repo Summary on Every Complex Task (Medium)

For every task classified as complex by `_task_is_complex()`, `analysis_node` may re-generate a repository summary. The per-process module-level cache in `context_builder.py` helps but is not guaranteed to be warm. For large codebases this adds latency and tokens.

---

### PERF-2 — `route_execution` is 219 Lines of Inline Logic (Low)

**File:** `src/core/orchestration/graph/builder.py:1042`

The monolithic router evaluates all conditions (fast-path, plan mode, loop guards, step controller, delegation) in sequence on every execution cycle. While functionally correct, the sequential evaluation is slightly wasteful when early conditions could short-circuit. More critically, the size makes profiling and optimization difficult.

---

### PERF-3 — Symbol Graph Cache Has No TTL (Low)

See RA-2. Beyond staleness, the unbounded module-level LRU cache in `context_builder.py` will retain all files read during a session in memory indefinitely until process restart. For very large repositories with many files, this could grow to significant memory usage.

---

## 12. Over-Engineered Components

### OE-1 — Multiple Overlapping Routing Functions in `builder.py` (Medium)

`builder.py` contains:
- `should_after_planning` (not wired in main graph — used only by `GraphFactory` subgraphs and tested in `test_graph_builder_routing.py`)
- `should_after_execution` (not wired in main graph — superseded by `route_execution`; used by `should_after_execution_with_replan` and tested in two test files)
- `should_after_execution_with_replan` (delegates to `should_after_execution`; used in `GraphFactory` subgraphs)
- `should_after_execution_with_compaction` (line 1259 — used in `GraphFactory` subgraphs)
- `should_after_verification` (not wired in main graph — tested in two test files as a reference implementation)
- `route_execution` (live main graph router, 219 lines)

All five non-live functions have unit tests and some are used by `GraphFactory`. They are not strictly dead code, but they create maintenance confusion because the main graph uses none of them. The `GraphFactory`-specific variants should migrate to `graph_factory.py`. The `should_after_execution` / `should_after_verification` functions should be clearly marked as `GraphFactory`-only or deprecated, and their tests should document this explicitly.

---

### OE-2 — `GraphFactory` Subgraph Infrastructure (Low)

`GraphFactory` enables creating graph variants (e.g., with compaction). In practice only one graph variant is used in production. The factory infrastructure adds abstraction overhead that is not justified by current usage. If multiple graph variants become necessary in the future, this can be re-introduced.

---

### OE-3 — Dual Compaction Paths Without Clear Boundary (Low)

Both `auto_compactor.py` (deterministic) and `distiller.py` (LLM-based) can compact the context. The boundary between when each fires is defined by thresholds but the interaction when both would be eligible simultaneously is not documented. In practice `auto_compactor` fires first (85% threshold), which may prevent `distiller` from ever running. The purpose of maintaining both paths should be clarified or one should be designated the primary.

---

## 13. Prioritized Fix List

| Priority | ID | Severity | Description | File(s) |
|----------|----|----------|-------------|---------|
| 1 | SEC-2 | Medium | Sync `WRITE_TOOLS_REQUIRING_READ` and `MODIFYING_TOOLS` — add `edit_file_atomic`, `rename_file`, `ast_rename` to `MODIFYING_TOOLS`; add `manage_todo` to `WRITE_TOOLS_REQUIRING_READ` | `loop_guards.py:64`, `orchestrator.py:36` |
| 2 | ET-4 | Low | Mark `test_llm_manager_fallback` as skip with reason or rewrite with mock | `tests/unit/test_llm_manager_fallback.py` |
| 3 | OE-1 | Medium | Migrate `GraphFactory`-only routing functions (`should_after_planning`, `should_after_execution`, etc.) from `builder.py` into `graph_factory.py`; add clear module-level docstrings indicating these are not used by the main graph | `builder.py:32,325,520`, `graph_factory.py` |
| 4 | SEC-1 | Medium | ✅ CLOSED — Already integrated: `write_file`, `edit_file`, `delete_file`, `edit_file_atomic`, `multiedit`, `edit_by_line_range` all check `guard_operation()` return value | `file_tools.py:303,519,678,1256,1429,1609` |
| 5 | ET-2 | Medium | Wire `scenario_evaluator.py` into CI via a `pytest` mark and at least one smoke scenario | `src/core/evaluation/scenario_evaluator.py` |
| 6 | WF-1 | Medium | Replace `_task_is_complex()` keyword heuristic with a state flag set by `perception_node` | `builder.py:192`, `perception_node.py` |
| 7 | CAP-1 | High | Add one unmocked integration test for the full pipeline using a real sandboxed repo | `tests/integration/` |
| 8 | RA-1 | Medium | Inject relevant symbol graph results into `planning_node` prompt | `planning_node.py`, `repo_indexer.py` |
| 9 | WF-3 | Medium | Add per-step verification flag to `step_controller_node` — mark step done only after step-level lint/test passes | `step_controller_node.py` |
| 10 | ARCH-1 | High | Begin decomposing `orchestrator.py` — extract `PermissionGateway` as a first step | `orchestrator.py` |

---

## 14. Engineering Roadmap

### Phase 1 — Critical Stability (Estimated: 1–2 days)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Sync `WRITE_TOOLS_REQUIRING_READ` and `MODIFYING_TOOLS` (SEC-2) | `orchestrator.py:36`, `loop_guards.py:64` | Low | Consistent read-before-write enforcement for all modifying tools |
| Mark `test_llm_manager_fallback` skip or rewrite with mock (ET-4) | `tests/unit/test_llm_manager_fallback.py` | Low | Eliminates pre-existing CI noise; clean test run |
| Remove dead routing functions from `builder.py` (OE-1) | `builder.py:32,325,520` | Low | Reduces confusion, prevents accidental re-wiring of dead code |
| Document or enforce pre-plan fast-path write permissions (SEC-1) | `workspace_guard.py`, `file_tools.py` | Low–Medium | ✅ CLOSED — Already implemented in Phase 4.3; all 6 write tools checked |

### Phase 2 — Robustness (Estimated: 1 week)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Replace `_task_is_complex()` heuristic with perception-node flag (WF-1) | `builder.py:192`, `perception_node.py` | Medium | Accurate complexity routing reduces false-positive analyst delegation |
| Wire scenario evaluator into CI with smoke scenarios (ET-2) | `scenario_evaluator.py`, `pytest` config | Medium | Automated agent quality measurement; regression detection |
| Add one unmocked end-to-end pipeline integration test (CAP-1) | `tests/integration/` | Medium–High | Catches routing bugs and state interaction issues not covered by unit tests |
| Add per-step verification in `step_controller_node` (WF-3) | `step_controller_node.py` | Medium | Early detection of bad steps prevents error propagation |
| Invalidate `_TEXT_CACHE` entries for files the agent has written (MEM-1) | `context_builder.py`, `file_tools.py` | Low–Medium | Prevents stale file content in system prompt during active editing |

### Phase 3 — Capability (Estimated: 1–2 weeks)

| Task | Location | Complexity | Impact |
|------|----------|------------|--------|
| Inject symbol graph results into planning prompt (RA-1, CAP-3) | `planning_node.py`, `repo_indexer.py` | Medium–High | Repo-aware planning; planner knows affected call sites and test coverage |
| Add LLM-based `evaluation_node` verdict (WF-2) | `evaluation_node.py` | Medium | Semantic correctness check, not just plan-step completion |
| Add plan divergence detection in replan loop (WF-4) | `evaluation_node.py`, `orchestrator.py` | Low–Medium | Prevents burning replan budget on identical plans |
| Add tool retry with backoff for transient errors (TS-4) | `tool_execution_service.py` | Medium | Handles transient network/file-lock failures without consuming a replan |
| Dry-run mode — show plan and affected files without executing (UX-3) | `orchestrator.py`, CLI layer | Medium | User can preview agent intent before committing |

### Phase 4 — Advanced ✅ COMPLETE

| Task | Location | Complexity | Status |
|------|----------|------------|--------|
| Decompose `orchestrator.py` God class — extract `PermissionGateway` (ARCH-1) | `src/core/orchestration/permission_gateway.py` | High | ✅ CLOSED |
| Decompose `route_execution` into sub-routers (ARCH-3) | `builder.py` | Medium | ✅ CLOSED |
| Persistent decision memory across sessions (MEM-2) | `session_store.py`, `perception_node.py` | Medium–High | ✅ CLOSED |
| Performance benchmarks in CI (CAP-5) | `benchmarks/bench_pipeline.py` | Medium | ✅ CLOSED |

---

## Baseline Metrics

| Metric | Vol17 | Vol18 (audit) | Vol18 (after Phase 1 fixes) | Vol18 (after Phase 2) | Vol18 (after Phase 3) | Vol18 (after Phase 4) |
|--------|-------|---------------|-----------------------------|-----------------------|-----------------------|-----------------------|
| Tests collected | 2958 | 3060 | 3060 | 3083 | 3111 | **3146** |
| Tests passed | 2955 | 2955 | **2956** | **3057** | **3084** | **3119** |
| Tests skipped | 2 | 2 | 2 | 17 | 17 | 17 |
| Tests xfailed | 0 | 0 | 0 | 9 | 9 | 9 |
| Pre-existing failures | 1 (`test_llm_manager_fallback`) | 1 (same) | **0 (fixed)** | **0** | **0** | **0** |
| Pyright errors | 0 | 0 | 0 | 0 | 0 | 0 |
| `orchestrator.py` lines | 4043 | 4043 | 4043 | 4043 | ~4055 | ~4055 |
| `builder.py` lines | 1296 | 1296 | 1296 | ~1330 | ~1340 | ~1340 |
| Open CP items | 0 | 0 | 0 | 0 | 0 | **0** |
| Open audit items | 0 | 10 (new findings) | 6 (4 closed: SEC-2, ET-4, OE-1, SEC-1) | **1** (Phase 3–4 items remain) | **0 Phase 3 open** (Phase 4 items remain) | **0 — ALL CLOSED** |

### Phase 1 Changes Applied

| ID | Change | Files |
|----|--------|-------|
| SEC-2 | Added `edit_file_atomic`, `rename_file`, `ast_rename` to `MODIFYING_TOOLS`; added `manage_todo` to `WRITE_TOOLS_REQUIRING_READ` | `loop_guards.py:64`, `orchestrator.py:36` |
| ET-4 | Fixed `test_llm_manager_fallback` test isolation — clears `_MODEL_CACHE` and `_models_cache["lm_studio"]` before/after test to prevent cross-test contamination from singleton state | `tests/unit/test_lm_studio_adapter_and_fallback.py:64` |
| OE-1 | Added "NOT WIRED IN main graph" docstring to `should_after_execution` in `builder.py` (the other two already had this); updated audit report to note these are `GraphFactory`-only, not truly dead code | `builder.py:330` |
