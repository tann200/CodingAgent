# Codebase Audit — CodingAgent
**Date:** 2026-05-12
**Auditor:** Automated deep-technical audit (claude-sonnet-4.6)
**Scope:** Full-spectrum engineering audit across all 15 required categories

---

## 1. Executive Summary

CodingAgent is a LangGraph-based local coding agent with a well-structured cognitive pipeline: perception → analysis → planning → execution → verification. The architecture is broadly sound, with meaningful investments in loop-prevention guards, read-before-write safety, session memory, and multi-provider LLM routing.

However, several **critical and high-severity issues** undermine reliability and safety in production use:

- A hard-coded `MAX_TOOL_LOOP_ITERATIONS = 5` silently truncates complex multi-tool tasks after only 5 graph rounds.
- The non-atomic `edit_file` tool variant is absent from the `MODIFYING_TOOLS` guard set, meaning it bypasses read-before-write enforcement.
- Two incompatible toolset loaders coexist (`src/tools/toolsets/loader.py` vs `src/config/toolsets/loader.py`) with separate caches — a known P0 open item.
- The sandbox silently falls back to unprotected `subprocess.run` on macOS when `bwrap` is unavailable, with warnings only emitted to EventBus (invisible in headless mode).
- Subagent recursion depth has no enforced test, leaving delegation open to stack overflow in adversarial prompts.
- Context distillation is implemented but not wired into the active `MessageManager`.

The system is not yet production-ready for autonomous code modification on real repositories without human supervision. It is at a **late-beta** level of maturity.

---

## 2. Architecture Strengths

- **LangGraph cognitive pipeline** is logically well-structured: dedicated nodes for perception, analysis, planning, execution, verification, and debugging provide clear separation of concerns.
- **Loop-prevention guards** (`loop_guards.py`) are thorough: doom-loop detection (identical-fingerprint and alternating-tool patterns), cooldown gates for read-only tools, and read-before-write enforcement.
- **Multi-provider LLM routing** (`provider_capabilities.py`, `llm_manager.py`) with model-tier awareness is more sophisticated than most open-source coding agents.
- **SessionStore** (`sqlite_session_store.py`) provides durable conversation persistence across restarts.
- **Cost tracking** (`SessionCostTracker`) with per-task buffering and flush is production-quality.
- **Atomic file writes** (`io_utils.atomic_write_text`, `atomic_write_json`) are used consistently in memory and persistence layers.
- **EventBus** decouples observability from execution logic cleanly.
- **`AgentState` validation** (`validate_state()` in `state.py`) catches corrupted numeric fields and out-of-bounds step indices at node entry without raising — a robust defensive pattern.
- **`permission_policy.py`** (PERM-02) provides a user-overridable permission layer for doom-loop behavior.
- **Test coverage** is substantial: 355 test files across unit, integration, and e2e suites.

---

## 3. Critical Architectural Flaws

### C-1 — Hard-coded `MAX_TOOL_LOOP_ITERATIONS = 5` (Critical)
**Location:** `src/core/orchestration/inference_loop.py:247`

The outer graph-round loop is capped at exactly 5 iterations with a magic constant. Any task requiring more than 5 tool-call cycles — routine for non-trivial refactoring, multi-file changes, or debugging sessions — will be silently truncated. The loop exits without error propagation visible to the user. The constant is not configurable via `agent_config.yaml` or any environment variable.

**Impact:** Any complex coding task silently fails after 5 rounds. No warning is shown to the user in the default response path.

**Fix:** Expose as a named config key (`max_graph_rounds`, default 20–50). Add a visible warning in the response when the limit is hit.

---

### C-2 — `edit_file` absent from `MODIFYING_TOOLS` (Critical)
**Location:** `src/core/orchestration/loop_guards.py:65–76`

`MODIFYING_TOOLS` contains `edit_file_atomic`, `edit_by_line_range`, `apply_patch`, `multiedit`, `write_file`, `delete_file`, `rename_file`, `ast_rename`, and `manage_todo` — but **not** the plain `edit_file` tool. If an LLM uses `edit_file` (the non-atomic variant) on an existing file without a prior `read_file`, the read-before-write guard is bypassed entirely.

**Impact:** An LLM can blindly overwrite file content without ever reading it, risking silent content corruption or full-file replacement.

**Fix:** Add `"edit_file"` to `MODIFYING_TOOLS` on line 66. Also add a CI test asserting all registered modifying tools appear in this set.

---

### C-3 — Dual Toolset Loader with Incompatible Caches (Critical)
**Location:** `src/tools/toolsets/loader.py` (legacy) vs `src/config/toolsets/loader.py` (canonical)

Two separate `ToolsetManager` implementations coexist, each with its own in-memory toolset cache. The canonical model-aware loader lives in `src/config/toolsets/`; the legacy one in `src/tools/toolsets/`. The orchestration layer imports from the canonical path, but `src/tools/_tool.py:208` also imports from `src/config/toolsets/loader.py`. The legacy loader in `src/tools/toolsets/__init__.py` remains importable.

**Impact:** Any code that imports from the legacy path will use a stale or divergent toolset, leading to missing or wrong tools being registered for a session. This is a confirmed P0 open item in `docs/REQUIREMENTS.md:142`.

**Fix:** Delete `src/tools/toolsets/loader.py` and `src/tools/toolsets/*.yaml`. Update all imports to `src/config/toolsets/loader.py`. Gate on a CI import-path check.

---

### C-4 — Silent Sandbox Degradation on macOS (Critical)
**Location:** `src/tools/sandbox.py:1–381`

When `bwrap` (Linux bubblewrap) is unavailable, the sandbox attempts `sandbox-exec` (macOS). When neither is available and `sandbox_level != "off"`, execution falls through to plain `subprocess.run` with only an EventBus warning. In headless/CLI mode without a TUI subscribed to EventBus, this warning is invisible. The default `sandbox_level` is `"workspace"`, so macOS users believe they are sandboxed when they are not.

**Impact:** Shell commands executed via the agent run with full user privileges on macOS. A prompt-injected `rm -rf ~` or similar would execute unimpeded.

**Fix:** On macOS, verify `sandbox-exec` availability at startup and emit a loud stderr/log warning if neither isolation mechanism is available. Consider refusing `sandbox_level != "off"` tasks if no sandbox backend is present, or make the degradation opt-in.

---

## 4. High-Risk Safety Issues

### H-1 — Subagent Recursion Depth Has No Enforced Test (High)
**Location:** `src/tools/subagent_tools.py`, `src/core/orchestration/graph/state.py` (`delegation_depth`)

`delegation_depth` is tracked in `AgentState`, and there is logic to check it in subagent dispatch. However, there is no integration test that asserts delegation is actually refused at the configured limit. This is flagged as a P0 open item in `docs/REQUIREMENTS.md`. A prompt-injection or runaway planning loop could trigger unbounded recursive delegation.

**Fix:** Add an integration test that invokes a subagent task that attempts to re-delegate and asserts the second delegation is rejected. Add a hard `assert delegation_depth <= MAX_DELEGATION_DEPTH` guard at node entry.

---

### H-2 — Context Distillation Not Wired to Active MessageManager (High)
**Location:** `src/core/memory/distiller.py`, orchestration pipeline

The distillation infrastructure (`distiller.py`, 659 lines) is fully implemented and supports compaction, session title generation, and summary export. However, it is not connected to the active `MessageManager` used during task execution. Context window protection relies on `_should_distill` / `_force_compact` flags in `AgentState`, but the wiring between the `MessageManager` and the distiller's compaction path is incomplete (flagged as `Step 17: Partial` in `docs/REQUIREMENTS.md`). Long tasks will accumulate unbounded history.

**Fix:** Wire `distiller.compact_messages()` into `MessageManager.get_messages()` or the history-building path in `inference_loop_state.py`, triggered when token budget crosses a configured threshold.

---

### H-3 — No Syntax or Lint Validation Before File Writes (High)
**Location:** `src/core/orchestration/graph/nodes/execution_helpers.py`

File modification tools (`edit_file`, `write_file`, `apply_patch`) do not run syntax checking (e.g., `py_compile`, `ast.parse`) or linting before committing changes. The verification node exists as a post-edit step, but it is not mandatory and is skipped when `verification_passed` is already truthy.

**Fix:** Add a lightweight `ast.parse()` gate in execution_helpers for `.py` files before any write is committed. Make the verification step mandatory for any turn that produced a file write.

---

## 5. Major Missing Capabilities

### M-1 — No Step Controller (High)
The planning node produces a `current_plan` list and tracks `current_step`, but there is no dedicated **step controller node** that enforces: "execute exactly step N, verify it passed, then advance to step N+1." The frontier/execution loop can skip steps, re-plan mid-execution, or proceed past a failed step. This makes multi-step plan execution non-deterministic.

### M-2 — No Automated Debug Loop (High)
When a tool or test fails, the agent falls to the `debug_node`, but there is no structured loop: identify error class → select recovery strategy → apply fix → retest. The debug node makes a single LLM call and returns. `max_debug_attempts` defaults to 3 but without a retry-with-different-strategy mechanism.

### M-3 — No Repo-Aware Planning Integration (Medium)
Repository indexing (`repo_indexer.py`, `symbol_graph.py`, `perception_node.py`) is implemented and runs during perception. However, `planning_node.py` imports `get_symbols_for_task` from `repo_indexer` but only uses it conditionally. There is no guarantee that planning always consults the symbol graph before producing a file-modification plan. Planning can and does proceed on LLM priors alone.

### M-4 — No Plan Persistence Across Sessions (Medium)
`current_plan` exists only in `AgentState` for the duration of a task. There is no mechanism to serialize a partially-completed plan to disk and resume it in a new session. Long multi-session tasks must restart from scratch.

### M-5 — Evaluation Scenarios Too Thin (Medium)
`scenario_evaluator.py` is a well-designed framework, but only 3 trivial scenarios are defined. There are no edit-accuracy benchmarks, no real-repository test scenarios, and no regression suite that validates the cognitive pipeline end-to-end with a real LLM adapter.

### M-6 — No Patch Rollback Mechanism (Medium)
Atomic writes exist at the I/O layer, but there is no session-level rollback: if a multi-file edit partially succeeds and then fails, there is no "undo all changes from this task" command. `snapshots` exists as a field in `AgentState` but is never populated by the execution path.

---

## 6. Workflow Reliability Issues

### W-1 — Plan Enforcement Is Not Mandatory (High)
`plan_enforce_warnings` and `plan_strict_mode` are tracked in `AgentState` but are opt-in. The agent can freely deviate from the current plan without triggering any guard. A planning pass that produces a 5-step plan provides no actual constraint on execution.

### W-2 — Graph Round State Reconstruction Was Fragile (Now Fixed) (Low)
`_prepare_next_round_state()` in `inference_loop_rounds.py` (SCAN3-1 fix) previously discarded wave state, analysis results, and loop counters between rounds. This has been fixed per the commit history, but is noted as an area requiring regression tests.

### W-3 — `turn_count` vs `rounds` Confusion (Medium)
`AgentState` has both `rounds` (graph rounds within a single `run_agent_once` call) and `turn_count` (turns across the session's lifetime). `max_turns` guards against `turn_count`. `MAX_TOOL_LOOP_ITERATIONS` guards `rounds`. These two limits interact in non-obvious ways and neither is prominently documented.

### W-4 — Empty Response Spiral Not Fully Mitigated (Medium)
`empty_response_count` is tracked and there is a guard in the execution node, but the threshold is not centrally configured and the recovery path (inject a nudge prompt) is not tested. A provider returning empty completions can still spin for several rounds before the guard fires.

---

## 7. Tool System Weaknesses

### T-1 — `edit_file` Missing from `MODIFYING_TOOLS` (Critical — see C-2)

### T-2 — Tool Argument Validation Is Per-Tool, Not Schema-Enforced (Medium)
Tools validate their own arguments internally, but there is no JSON Schema validation layer at the registry level. Invalid arguments (wrong type, missing required field) produce inconsistent error messages depending on which tool handles the failure.

### T-3 — `manage_todo` in `MODIFYING_TOOLS` But `TODO.md` May Not Exist (Low)
The read-before-write guard for `manage_todo` will block the first write if `TODO.md` doesn't yet exist (the guard checks `Path(resolved).exists()` before enforcing). This is handled correctly — new files are exempt — but creates a confusing "why did the guard fire?" experience if `TODO.md` was created in a prior session and is now in the `files_read` gap.

### T-4 — Shell Tool Has No Command Allowlist in Default Config (High)
The `bash` / shell execution tool relies entirely on the sandbox for containment. There is no command allowlist (e.g., block `curl`, `wget`, `ssh`, `nc`) at the tool layer. If the sandbox degrades (see C-4), any shell command the LLM emits executes directly.

### T-5 — Tool Idempotency Reset Is Per-Task Only (Low)
`tool_execution_service.reset_idempotency()` is called at the start of each `run_agent_once` call. Within a single multi-round task, idempotency guards accumulate correctly. However, if a task is canceled mid-execution and immediately retried, the reset ensures a clean slate, which is correct.

---

## 8. Repository Awareness Gaps

### R-1 — Symbol Graph Not Consulted Before Every Plan (Medium)
`analysis_node.py` caches `SymbolGraph` per `working_dir` and `perception_node.py` calls into `perception_retrieval.py` with `symbol_graph_cls`. But `planning_node.py` only calls `get_symbols_for_task` conditionally (import is inside the node function). There is no contract that planning always receives repo-aware context.

### R-2 — `repo_summary_data` Injected as Raw String (Low)
Repository summary is stored as `repo_summary_data: str | None` in `AgentState` and injected into the system prompt as a raw string blob. For large repositories this can consume significant context budget. There is no truncation or hierarchical summarization.

### R-3 — No Incremental Index Update on File Changes (Medium)
The repo indexer runs at session start and is not triggered by file writes. After the agent edits files during a task, the symbol graph becomes stale for the remainder of the session. Subsequent find-symbol queries may return incorrect references.

### R-4 — `call_graph` and `test_map` Fields Populated But Not Used in Planning (Medium)
`AgentState` has `call_graph` and `test_map` fields. `analysis_node.py` populates them, but `planning_node.py` does not consult `test_map` when selecting which tests to run for a change. Test selection is LLM-directed rather than graph-directed.

---

## 9. Memory System Evaluation

### Strengths
- `sqlite_session_store.py` provides durable session transcripts with `add_message` / `get_messages`.
- `distiller.py` supports compaction, session title generation, and summary export.
- `_should_distill`, `_force_compact`, `_budget_compaction` flags provide hooks for triggered compaction.
- Optional `lancedb` vector store integration for semantic memory retrieval.

### Weaknesses

### ME-1 — Distillation Not Wired to Active Pipeline (High — see H-2)

### ME-2 — No Cross-Session Memory Retrieval (Medium)
Summaries from completed sessions are written to disk, but there is no retrieval path that surfaces relevant prior-session context into a new session's system prompt. Each session starts cold regardless of related past work.

### ME-3 — `_compacted_history` and `_compaction_last_round` Are State Fields, Not Disk-Backed (Low)
Compaction metadata lives only in `AgentState` for the session duration. If the process crashes mid-task, compaction progress is lost and the next session will re-compact from scratch, potentially losing distilled summaries.

### ME-4 — Vector Memory Is Optional and Not Auto-Activated (Low)
`lancedb` vector memory is behind the `vector` optional dependency. The orchestrator does not automatically enable it when available. Developers must explicitly configure it.

---

## 10. Evaluation and Testing Gaps

### ET-1 — Only 3 Evaluation Scenarios Defined (High)
`scenario_evaluator.py` is a solid framework, but the scenario library is nearly empty. There are no scenarios for: multi-file refactoring, bug introduction/fix cycles, ambiguous task disambiguation, or tool failure recovery.

### ET-2 — All E2E Tests Use MockAdapter (Medium)
All 5 e2e test files use a `MockAdapter` rather than a real LLM. The CI pipeline never validates that the cognitive pipeline produces correct outputs with an actual model. Behavioral regressions (prompt engineering changes, planning regressions) are invisible to CI.

### ET-3 — No Edit Accuracy Metric (High)
There is no metric measuring whether the agent's file edits are syntactically correct, semantically correct, or break existing tests. `evaluation_llm_verdict` and `evaluation_llm_reason` fields exist in `AgentState` but are not populated by any automated test runner.

### ET-4 — No Performance Benchmark (Medium)
There is no latency or token-cost benchmark for standard task types. It is impossible to detect performance regressions (e.g., a prompt change that doubles token usage) in CI.

### ET-5 — Test Coverage for Guard Interactions Is Thin (Medium)
The three loop guards in `loop_guards.py` have unit tests, but there are no integration tests that exercise guard interactions (e.g., doom-loop detection firing on a real multi-round execution, or the cooldown gate blocking a redundant read mid-task).

---

## 11. Usability Problems

### U-1 — `MAX_TOOL_LOOP_ITERATIONS = 5` Is Not Surfaced to the User (High)
When a task hits the 5-round limit, the agent exits silently or with a generic "loop limit exceeded" error buried in logs. There is no user-visible message explaining what happened or suggesting a workaround.

### U-2 — No Onboarding Documentation for New Providers (Medium)
Adding a new LLM provider requires implementing a new adapter, registering it in `provider_capabilities.py`, and updating `tools_config.yaml`. None of this is documented in a developer guide. The `DEVELOPER_METRICS.md` document covers metrics, not extension points.

### U-3 — Configuration Complexity Is High (Medium)
The system has `agent_config.yaml`, `tools_config.yaml`, `permissions.json`, `toolsets/*.yaml` (in two locations), and environment variables (`CODINGAGENT_SANDBOX_LEVEL`, `DISTILLER_LLM_TIMEOUT_SECONDS`). There is no single reference document listing all knobs and their defaults.

### U-4 — Headless Mode Loses EventBus Warnings (Medium — see C-4)
EventBus warnings (sandbox degradation, doom-loop `ASK` behavior) are invisible when running without a TUI subscriber. CLI/headless users have no visibility into safety-relevant events.

---

## 12. Performance Bottlenecks

### P-1 — `pandas>=2.0.0` in Core Dependencies (Low)
`pyproject.toml:19` lists `pandas>=2.0.0` as a core (non-optional) dependency. `pandas` adds ~50MB to the install and several seconds to import time. No usage of `pandas` was found in `src/core/` or `src/tools/`. If used only in analytics/metrics scripts, it should be moved to an optional `[analytics]` extra.

### P-2 — Oversized Node Files Indicate Missing Decomposition (Medium)
- `execution_helpers.py`: 1,322 lines
- `frontier_loop_node.py`: 1,007 lines
- `planning_node.py`: 888 lines

Files of this size are difficult to test in isolation, slow to navigate, and accumulate responsibilities. They are likely performance hot-paths that are hard to profile or optimize.

### P-3 — Symbol Graph Rebuilt Per-Workdir Per-Session (Medium)
`analysis_node.py` caches `SymbolGraph` per `working_dir` in a module-level dict. This is correct for a single process, but the cache is not persisted to disk. Each new process (e.g., each CLI invocation) rebuilds the symbol graph from scratch.

### P-4 — Graph Compiled Per-Process but Not Cached Across Test Runs (Low)
`get_compiled_graph_for_orchestrator()` uses a module-level cache. This is correct for production but means the test suite may compile the graph multiple times across test worker processes.

### P-5 — `1,785` Silent `except Exception` Blocks (Medium)
There are 1,785 `except Exception` occurrences in `src/`. Many swallow errors silently (inferred from context — actual `pass` count varies). Silent exceptions hide performance anomalies (e.g., slow LLM timeouts being swallowed and retried) and make profiling unreliable.

---

## 13. Over-Engineered Components

### OE-1 — DAG/Wave Execution Infrastructure Largely Unused (Medium)
`AgentState` has `plan_dag`, `execution_waves`, and `current_wave` fields. `dag_parser.py` and related infrastructure (~1,000 lines) implement parallel wave execution. In practice, the agent uses sequential step execution. The wave infrastructure adds state complexity and maintenance burden without active use.

### OE-2 — `_p2p_context` Field with No Active Consumer (Low)
`AgentState._p2p_context: List[Dict[str, Any]] | None` is defined but never populated or consumed in any node.

### OE-3 — `_file_lock_manager` and `_write_queue` as State Fields (Low)
These mutable object fields in `AgentState` are anti-patterns for a LangGraph state dict, which is intended to be serializable. Passing live object references through state makes serialization, checkpointing, and testing harder.

### OE-4 — Legacy `src/tools/toolsets/` Directory (Critical — see C-3)
The entire legacy toolset loader directory (`src/tools/toolsets/loader.py`, `src/tools/toolsets/*.yaml`) is dead code that should be deleted.

### OE-5 — `_AgentStateSpec` Has ~100 Fields (Medium)
`state.py` defines approximately 100 fields on `_AgentStateSpec`. Many are rarely or never written (e.g., `seed`, `deterministic`, `snapshots`, `_pending_injections_source`). Consider grouping into nested sub-states (`PlanState`, `DebugState`, `SessionState`) to reduce cognitive overhead and enable targeted validation.

---

## 14. Prioritized Fix List

| Priority | ID | Issue | Location | Severity | Complexity | Impact |
|---|---|---|---|---|---|---|
| 1 | P1-T1 | Add `edit_file` to `MODIFYING_TOOLS` | `loop_guards.py:66` | Critical | XS (1 line) | Prevents silent file corruption |
| 2 | P1-T2 | Delete legacy `src/tools/toolsets/` loader | `src/tools/toolsets/` | Critical | S (delete + update imports) | Eliminates dual-cache toolset bug |
| 3 | P1-T3 | Make `MAX_TOOL_LOOP_ITERATIONS` configurable | `inference_loop.py:247` | Critical | S | Unblocks complex tasks |
| 4 | P1-T4 | Loud warning / hard fail when sandbox degrades | `sandbox.py` | Critical | S | Prevents invisible privilege escalation |
| 5 | P1-T5 | Wire distillation into active MessageManager | `distiller.py`, `inference_loop_state.py` | High | M | Prevents context window overflow |
| 6 | P2-T1 | Add integration test for delegation depth limit | `tests/integration/` | High | S | Closes P0 open item in REQUIREMENTS.md |
| 7 | P2-T2 | Add syntax validation before file writes | `execution_helpers.py` | High | S | Catches LLM-generated invalid Python |
| 8 | P2-T3 | Add shell command allowlist at tool layer | `src/tools/bash_tool.py` | High | M | Defense-in-depth against prompt injection |
| 9 | P2-T4 | Add user-visible message when round limit hit | `inference_loop.py` | High | XS | Critical UX gap |
| 10 | P2-T5 | Move `pandas` to optional dependency | `pyproject.toml:19` | Low | XS | Reduces install size and import time |
| 11 | P3-T1 | Implement step controller node | new node | High | L | Enforces deterministic plan execution |
| 12 | P3-T2 | Implement automated debug retry loop | `debug_node.py` | High | M | Improves task success rate |
| 13 | P3-T3 | Trigger symbol graph refresh after file writes | `execution_helpers.py`, `repo_indexer.py` | Medium | M | Keeps repo intelligence current |
| 14 | P3-T4 | Add plan persistence (serialize to disk) | `planning_node.py`, `sqlite_session_store.py` | Medium | M | Enables multi-session task resumption |
| 15 | P3-T5 | Enforce plan-step advancement as mandatory | `frontier_loop_node.py` | Medium | M | Makes multi-step plans reliable |
| 16 | P3-T6 | Add `snapshots` population for rollback support | `execution_helpers.py` | Medium | M | Enables undo after failed tasks |
| 17 | P3-T7 | Split `execution_helpers.py` (1,322 lines) | `execution_helpers.py` | Medium | M | Improves testability and maintainability |
| 18 | P3-T8 | Split `frontier_loop_node.py` (1,007 lines) | `frontier_loop_node.py` | Medium | M | Same as above |
| 19 | P3-T9 | Route EventBus warnings to stderr in headless mode | `event_bus.py` | Medium | S | Surfaces safety warnings in CLI use |
| 20 | P3-T10 | Add JSON Schema validation at tool registry | `_registry.py` | Medium | M | Consistent argument error messages |
| 21 | P4-T1 | Expand evaluation scenario library (≥20 scenarios) | `scenario_evaluator.py` | High | L | Enables CI regression detection |
| 22 | P4-T2 | Add real-LLM e2e smoke test in CI | `tests/e2e/` | High | M | Detects behavioral regressions |
| 23 | P4-T3 | Implement cross-session memory retrieval | `distiller.py`, `sqlite_session_store.py` | Medium | L | Enables continuity across sessions |
| 24 | P4-T4 | Retire DAG/wave infrastructure or activate it | `dag_parser.py`, `state.py` | Medium | L | Reduces dead-code complexity |
| 25 | P4-T5 | Consolidate `AgentState` into nested sub-states | `state.py` | Low | L | Reduces cognitive overhead |

---

## 15. Engineering Roadmap

### Phase 1 — Critical Stability (Week 1–2)

**Goal:** Eliminate critical safety and correctness bugs that could corrupt repositories or silently truncate tasks.

| Task | Description | Location | Complexity | Impact |
|---|---|---|---|---|
| P1-T1 | Add `"edit_file"` to `MODIFYING_TOOLS` | `loop_guards.py:66` | XS | Prevents blind file overwrites |
| P1-T2 | Delete legacy toolset loader; unify imports | `src/tools/toolsets/` | S | Eliminates dual-cache P0 bug |
| P1-T3 | Read `max_graph_rounds` from config (default 20) | `inference_loop.py:247` | S | Unblocks complex multi-tool tasks |
| P1-T4 | Emit stderr warning / refuse degraded sandbox | `sandbox.py` | S | Makes macOS sandbox state visible |
| P1-T5 | Surface round-limit hit as user-visible message | `inference_loop.py` | XS | Critical UX gap |

---

### Phase 2 — Robustness (Week 3–5)

**Goal:** Close high-severity reliability and safety gaps.

| Task | Description | Location | Complexity | Impact |
|---|---|---|---|---|
| P2-T1 | Integration test for delegation depth enforcement | `tests/integration/` | S | Closes REQUIREMENTS.md P0 item |
| P2-T2 | `ast.parse()` gate before Python file writes | `execution_helpers.py` | S | Catches invalid generated code |
| P2-T3 | Shell command allowlist / denylist at tool layer | `src/tools/bash_tool.py` | M | Prompt-injection defense |
| P2-T4 | Wire distillation into MessageManager pipeline | `distiller.py`, `inference_loop_state.py` | M | Prevents unbounded context growth |
| P2-T5 | Route EventBus safety warnings to stderr in headless mode | `event_bus.py` | S | Surfaces sandbox/doom-loop events in CLI |
| P2-T6 | Move `pandas` to optional `[analytics]` extra | `pyproject.toml:19` | XS | Reduces install footprint |

---

### Phase 3 — Capability (Week 6–9)

**Goal:** Close the most impactful capability gaps vs. strong coding agents.

| Task | Description | Location | Complexity | Impact |
|---|---|---|---|---|
| P3-T1 | Implement step controller node | new graph node | L | Deterministic multi-step plan execution |
| P3-T2 | Automated debug retry loop with strategy selection | `debug_node.py` | M | Higher task success rate on failures |
| P3-T3 | Trigger repo index refresh after file writes | `execution_helpers.py` | M | Keeps symbol graph current |
| P3-T4 | Serialize plans to disk; resume across sessions | `planning_node.py`, `sqlite_session_store.py` | M | Multi-session task continuity |
| P3-T5 | Populate `snapshots` for task rollback | `execution_helpers.py` | M | Undo support after failed multi-file edits |
| P3-T6 | Split oversized node files (execution_helpers, frontier_loop, planning_node) | respective files | M | Testability and maintainability |

---

### Phase 4 — Advanced Features (Week 10+)

**Goal:** Production-grade evaluation, memory, and architecture maturity.

| Task | Description | Location | Complexity | Impact |
|---|---|---|---|---|
| P4-T1 | Expand evaluation scenario library (≥20 scenarios) | `scenario_evaluator.py` | L | CI behavioral regression detection |
| P4-T2 | Real-LLM e2e smoke test in CI | `tests/e2e/` | M | Validates cognitive pipeline with real models |
| P4-T3 | Cross-session memory retrieval | `distiller.py`, `sqlite_session_store.py` | L | Session continuity |
| P4-T4 | Retire or activate DAG/wave execution | `dag_parser.py`, `state.py` | L | Reduce dead code or unlock parallelism |
| P4-T5 | Refactor `AgentState` into nested sub-states | `state.py` | L | Developer ergonomics |
| P4-T6 | Developer extension guide (new tools, providers, nodes) | `docs/` | M | Onboarding and ecosystem growth |

---

*End of audit report.*
