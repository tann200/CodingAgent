# Comprehensive System Audit Report — CodingAgent

**Date:** 2026-08-31
**Scope:** Full-spectrum audit across 15 categories
**Method:** Parallel deep exploration of all subsystems with line-level verification

---

## 1. Executive Summary

CodingAgent is a LangGraph-based autonomous coding agent with a well-architected foundation. The system demonstrates **strong architectural separation** (event buses, permission gates, context builder), **thorough tool safety** (5-gate permission system, AST-level bash analysis, read-before-write enforcement), and **comprehensive memory/context infrastructure** (distillation, compaction, frozen snapshots, dual session stores).

However, the system has **critical security orientation issues** (fail-open on security boundaries), **significant dormant code** (6/16 graph nodes frozen, broken cross-session memory, unused plugin hooks), and **no formal evaluation framework** to measure agent quality. The single largest debt item is **1,868 `except Exception` blocks** with many silently swallowing errors in graph nodes.

**Overall Maturity: 6.5/10** — Strong engineering, but not production-ready for autonomous operation due to security orientation gaps and incomplete feature integration.

---

## 2. Architecture Strengths

1. **Dual-bus event system** — EventBus (legacy) + MessageBus (typed async) with 131 `publish_typed` call sites. Well-designed migration with backward compatibility.
2. **5-gate permission system** — Layered defense (plan-mode, explore-mode, policy rules, SQLite rules, directory access, DANGER/PROMPT classification, interactive approval). Gate 5 correctly fails closed.
3. **State-machine routing** — 6 routing modules with model-tier awareness, replan caps, debug attempts, and tool-budget guards. Complex but well-structured.
4. **Context builder sophistication** — Two-tier prompt cache, tier-specific tool rendering, cross-session memory injection, LSP context, past-mistakes retrieval, plugin hooks.
5. **Tool safety depth** — AST-level bash analysis, tiered command classification, read-before-write enforcement, workspace guard protecting 33 file patterns, shell hooks.
6. **Clean TUI/backend separation** — `AgentBridge` mediates exclusively via typed MessageBus, never imports `src.core` directly. Metaclass compatibility handled correctly.

---

## 3. Critical Architectural Flaws

### CF-1: Permission Policy Fail-Open [CRITICAL]
- **File:** `src/core/orchestration/permission_gateway.py:469-474`
- **Issue:** Gate 2c (PermissionPolicy check) returns `PermissionResult(allowed=True)` on any exception. A broken/absent policy file silently grants full permission to all tools.
- **Impact:** Any ImportError, malformed JSON, or runtime exception in the policy layer disables permission enforcement.
- **Fix:** Flip to fail-closed — return `PermissionResult(allowed=False)` on policy failure, mirroring the correct pattern already in `tool_execution_service._check_permission_gate`.

### CF-2: Sandbox Fail-Open to Unsandboxed Execution [HIGH]
- **File:** `src/tools/sandbox.py:451-478`
- **Issue:** When bubblewrap/sandbox-exec is unavailable or fails, the command runs with full user privileges unsandboxed. `SANDBOX_REQUIRE_ENFORCEMENT=1` is opt-in, not default.
- **Impact:** On macOS (sandbox-exec deprecated) or systems without bubblewrap, all bash commands run unsandboxed by default.
- **Fix:** Make sandbox enforcement default-strict for autonomous mode.

### CF-3: Graph Running in Stabilization Mode [HIGH]
- **File:** `src/core/orchestration/graph/builder.py:94`
- **Issue:** `_USE_FULL_GRAPH = False` freezes 4 nodes (replan, debug, delegation, analyst_delegation). The agent cannot self-delegate, replan from failure, or enter debug loops.
- **Impact:** Recovery and delegation capabilities are disabled. Agent cannot handle complex multi-step failures.
- **Fix:** Complete stabilization and enable full graph, or remove dead code if fast-path is permanent.

### CF-4: Cross-Session Memory Persistence Broken [HIGH]
- **File:** `src/core/indexing/vector_store.py:302-307`
- **Issue:** `add_memory()` and `search_memories()` are no-op stubs. The distiller calls `add_memory()` thinking it persists summaries, but nothing is stored.
- **Impact:** Cross-session memory recall via VectorStore is non-functional. Semantic memory retrieval across sessions does not work.
- **Fix:** Implement actual storage/retrieval or remove the dead code path.

### CF-5: Node Output Validation Covers Only 4/16 Nodes [HIGH]
- **File:** `src/core/orchestration/graph/state_schemas.py`
- **Issue:** Only perception, planning, execution, verification have output schemas. 12 nodes have zero boundary validation, and the strict flag (`_STATE_SCHEMAS_STRICT`) is hardcoded `False`.
- **Impact:** Node output pollution is possible and undetected. Violations are logged but never enforced or counted.
- **Fix:** Add schemas for all 16 nodes. Wire a `NodeResultValidationFailed` counter for observability.

---

## 4. High-Risk Safety Issues

### HS-1: Autonomous Mode Suppresses All Approval Prompts
- **Files:** `src/tools/tools_config.py:250-260`, `src/core/orchestration/permission_gateway.py:677-678`
- **Issue:** `is_autonomous()` auto-allows DANGER/PROMPT tools with no operator override. The env var `CODINGAGENT_AUTONOMOUS` is checked at each call, so any process with env control disables all safety prompts.
- **Severity:** HIGH

### HS-2: WorkspaceGuard No-Op Fallback
- **File:** `src/tools/_workspace_guard.py:12-26`
- **Issue:** If `src.core.orchestration.workspace_guard` fails to import, a no-op stub returns `{"status": "ok"}` for every guard operation. Security checks silently bypassed.
- **Severity:** HIGH

### HS-3: Bash Timeout Returns Status "ok"
- **File:** `src/tools/_bash_exec.py:553-562`
- **Issue:** On timeout, bash returns `status:"ok"` with `returncode: -1` instead of an error. The LLM may treat a hung command as success.
- **Severity:** HIGH

### HS-4: Alias Evasion of Bash Permission Rules
- **File:** `src/tools/tools_config.py:116-145`
- **Issue:** ~25 aliases exist (run, shell, cmd, etc.) for `bash`. Permission rules keyed on `bash` don't apply to alias `run`. A deny rule on `bash` can be evaded via alias.
- **Severity:** MEDIUM

### HS-5: Delete File Auto-Approval Inside Workspace
- **File:** `src/core/orchestration/permission_gateway.py:200-202, 232-236`
- **Issue:** `delete_file` is in `_WORKDIR_SAFE_TOOLS`, auto-approved when path is inside workdir. Deletion is irreversible.
- **Severity:** MEDIUM

### HS-6: 1,868 Silent Exception Swallows
- **Files:** Across `src/` (1,341 unnamed, 526 named)
- **Issue:** Graph nodes silently swallow exceptions, returning unchanged/partial state. Corrupted state flows into routing logic.
- **Severity:** MEDIUM

---

## 5. Major Missing Capabilities

### MC-1: No Formal Evaluation Framework
- No SWE-bench integration, no scenario evaluation harness, no regression test suite, no model comparison evaluation.
- Testing is unit-test-centric (373+ test files, ~4,660 tests) but lacks systematic agent-quality measurement.
- **Impact:** Cannot quantify agent reliability, edit accuracy, or tool usage correctness.

### MC-2: HOOK_SESSION_START Defined but Never Invoked
- **File:** `src/core/plugin/hook_registry.py:79`
- **Issue:** The hook is exported and documented but has zero call-sites. Documented API without implementation.
- **Impact:** Plugin authors cannot hook into session start.

### MC-3: No Crash-State Checkpointing
- **Issue:** LangGraph state is not checkpointed per-superstep. A crash mid-graph loses all in-flight state. Resume requires manual `/continue` invocation.
- **Impact:** Partial work from crashed runs is lost unless manually checkpointed via tool.

### MC-4: Duplicate Skill Directories
- **Files:** `src/config/agent-brain/skills/` (8 skills) and `src/config/skills/` (5 older skills)
- **Issue:** Two parallel skill sets with different formats. `explore_codebase` only exists in legacy dir.
- **Impact:** Unclear which is authoritative. Potential confusion.

### MC-5: Stub Roles Referencing Defunct Architecture
- **Files:** `src/config/agent-brain/roles/researcher.md`, `scout.md`, `tester.md`
- **Issue:** Reference legacy P2P broadcast topics (`agent.researcher.broadcast`) that don't exist in current architecture.
- **Impact:** Roles are non-functional if dispatched.

### MC-6: Missing Per-Tool Network Policy in Sandbox
- **Issue:** `bash` (vs `bash_readonly`) doesn't pass `network=False`. Network-capable commands depend on sandbox level, which is opt-in.
- **Impact:** Without sandbox enforcement, `curl`, `wget` etc. run with full network access.

---

## 6. Workflow Reliability Issues

### WR-1: Routing Default-to-Perception Creates Loop Risk
- **File:** `src/core/orchestration/graph/routing/session_routing.py:74`
- **Issue:** `should_after_memory_sync` defaults to `"perception"` when no condition matches. Without interlocking guards (round caps, tool budget), this creates an infinite loop.
- **Severity:** MEDIUM-HIGH

### WR-2: Two Independent Round-Limiting Mechanisms
- **Files:** `inference_loop.py:259` (default 20), `planning_routing.py:8` (default 15)
- **Issue:** Different default values for the same concept. Operators tuning one may not realize the other exists.
- **Severity:** LOW

### WR-3: Inter-Round Compaction Drops Role Alternation
- **File:** `src/core/orchestration/inference_loop_rounds.py:126-143`
- **Issue:** Compaction replaces history with a single `[Context summary]` user message, breaking role alternation pattern expected by many models.
- **Severity:** LOW

### WR-4: CompactionService LLM Path Key Mismatch
- **File:** `src/core/memory/compaction_service.py:199-203`
- **Issue:** Reads `output.get("history")` but `distill_context` returns `_compacted_history`. Primary LLM path silently returns empty; falls back to deterministic.
- **Severity:** MEDIUM

### WR-5: Complexity Heuristic Fragility
- **File:** `src/core/orchestration/graph/routing/perception_routing.py`
- **Issue:** `_task_is_complex()` uses keyword matching (exact phrases + word-boundary regex) on task description. Fragile for non-English or unusual descriptions.
- **Severity:** LOW

---

## 7. Tool System Weaknesses

### TW-1: Implicit permission_kind Inference is Lossy
- **File:** `src/tools/_tool.py:308-319`
- **Issue:** Only 20/75 tools explicitly declare `permission_kind`. 55 tools use inferred defaults from `side_effects` alone. Tools that mutate state but don't declare it are classified read-only.
- **Severity:** MEDIUM

### TW-2: run_in_background Bypasses Sandboxing
- **File:** `src/tools/_bash_exec.py:496-515`
- **Issue:** `Popen` with `stdout=DEVNULL` — no sandbox, no output capping, unsupervised process.
- **Severity:** MEDIUM

### TW-3: Contract Validation Fail-Open
- **File:** `src/core/orchestration/graph/nodes/tool_execution_pipeline.py:969-980`
- **Issue:** Contract `model_validate` wrapped in `try/except Exception: pass`. Broken/missing contracts silently pass.
- **Severity:** MEDIUM

### TW-4: Inconsistent Truncation Limits
- **Issue:** Bash caps stdout at 16 KB, pipeline at 8 KB chars, `_truncate` at 100 KB/2000 lines. Different limits apply depending on call path.
- **Severity:** LOW

### TW-5: `_BUILTIN_MODULES` Hardcoded
- **File:** `src/tools/_registry.py:43`
- **Issue:** New `@tool` modules require manual addition to the list. Maintenance/consistency risk.
- **Severity:** LOW

---

## 8. Repository Awareness Gaps

### RA-1: VectorStore Semantic Search Functional But Memory Persistence Broken
- **File:** `src/core/indexing/vector_store.py`
- **Issue:** When `sentence-transformers` is installed, cosine similarity search works. But `add_memory()`/`search_memories()` are no-ops, so the search has nothing to search against for cross-session data.
- **Severity:** HIGH (see CF-4)

### RA-2: 80MB Model Blocks Calling Thread
- **File:** `src/core/indexing/vector_store.py:33-48`
- **Issue:** `all-MiniLM-L6-v2` loads synchronously on first call. No async variant.
- **Severity:** LOW

### RA-3: LSP Semaphore Not Enforced
- **File:** `src/core/indexing/lsp_manager.py:120-121`
- **Issue:** `_semaphore` is created but not used internally for concurrency limiting.
- **Severity:** LOW

### RA-4: Symbol Graph Uses MD5
- **File:** `src/core/indexing/symbol_graph.py:164`
- **Issue:** MD5 for file change detection. Deprecated but not a security concern (just change detection).
- **Severity:** LOW

---

## 9. Memory System Evaluation

### What Works Well
- **Distiller** (753 lines): Complete LLM-based session summarization with structured JSON output, fallback compaction, title generation, cross-session retrieval.
- **Auto Compactor** (621 lines): Faithful Python port of claw-code's `compact.rs` with deterministic compaction, summary merging, key-file extraction.
- **Compaction Service** (268 lines): Unified facade over LLM + deterministic paths with typed events.
- **Session Store** (616 lines): Dual-backend (JSONL + SQLite with FTS5), thread-local connections, retry logic with diagnostic sidecar.
- **Frozen Snapshot** (189 lines): Session-stable memory for prompt caching with thread-safe singleton.

### Issues
| Severity | Issue | Location |
|----------|-------|----------|
| HIGH | `add_memory()`/`search_memories()` are no-ops | `vector_store.py:302-307` |
| MEDIUM | LLM compaction reads wrong key (`"history"` vs `_compacted_history`) | `compaction_service.py:199-203` |
| MEDIUM | Token estimation formula inconsistency (`len//4` vs `len//4+1`) | `context_controller.py:48` |
| LOW | `context_builder.py:283` falls back to `Path.cwd()` despite comment forbidding it | `context_builder.py:283` |
| LOW | Session store `inspect.signature()` on every proxied call | `session_store.py:233-263` |

---

## 10. Evaluation and Testing Gaps

### Current State
- **373+ test files** across `tests/unit/` with ~4,660 tests
- Strong security-specific tests (bypass vectors, SSRF, injection, concurrency)
- Golden regression suite with pass@k metric
- SWE-bench-style evaluator with 23 scenarios

### Missing
| Gap | Impact |
|-----|--------|
| No SWE-bench integration | Cannot compare against industry standard |
| No formal evaluation framework beyond E2E tests | No systematic quality measurement |
| No performance benchmarks (1 file only) | Cannot track performance regressions |
| No model comparison evaluation | Cannot compare provider quality |
| No fuzz testing or property-based testing | Edge cases unexplored |
| No automated CI benchmark tracking | Quality drift undetected |

---

## 11. Usability Problems

### UP-1: CLI Thin Relative to TUI
- **File:** `src/main.py`
- Most features (role switching, slash commands, settings, themes) only reachable in TUI. No headless/scriptable CLI parity.
- **Severity:** MEDIUM

### UP-2: Configuration Complexity
- Providers in `providers.json`, agent-brain in `src/config/agent-brain/`, permissions in `permissions.json`, toolsets in `src/config/toolsets/`, skills in two locations. No unified config interface.
- **Severity:** LOW

### UP-3: Stale Documentation
- Test baselines inconsistent across docs (3537, 3844, 4388, 4659). Deprecated docs still present and linked. Mixin refactor status outdated.
- **Severity:** LOW

---

## 12. Performance Bottlenecks

### PB-1: Three Overlapping Context-Pruning Strategies
- `_prune_tool_outputs` (perception_node.py, 40K-token boundary)
- `prune_stale_tool_outputs` (tool_output_pruning.py, turn-count-based)
- `truncate_to_token_budget` (token_truncation.py)
- **Issue:** Redundant compute on every round. Double-compaction possible.
- **Severity:** MEDIUM

### PB-2: Unconditional Per-Round Debug Serialization
- **File:** `src/core/orchestration/graph/nodes/perception_node.py:939`
- **Issue:** `repr(resp)[:1000]` runs every round even when logging disabled. String allocation overhead.
- **Severity:** LOW

### PB-3: 80MB Model Load on First Vector Search
- **File:** `src/core/indexing/vector_store.py:33-48`
- **Issue:** Synchronous load blocks calling thread.
- **Severity:** LOW

### PB-4: AgentState Bloat
- **File:** `src/core/orchestration/graph/state.py`
- **Issue:** 79 flat fields on single TypedDict. All `total=False`. Any node can write to any field.
- **Severity:** LOW

---

## 13. Over-Engineered Components

### OE-1: Perception Node Fragmentation [HIGH]
- **File:** `src/core/orchestration/graph/nodes/perception_node.py` (1021 lines)
- **Issue:** Split into 9+ helper files but the main file is still 1021 lines of thin forwarding shims (~170 lines of one-line wrappers). Complexity relocated, not reduced.
- **Impact:** 10 files to maintain for what was one file. `as _X_impl` aliasing makes tracing hard.

### OE-2: Duplicate Defensive Import Fallbacks [MEDIUM]
- **Files:** `ollama_adapter.py:22-50`, `perception_node.py:402-414`, hook import patterns
- **Issue:** Whole helper blocks re-implemented inline in `except` blocks. The failure branches cannot be reached in practice (no optional deps).
- **Impact:** Second source of truth that can rust and diverge.

### OE-3: Duplicate `_is_success()` Implementations [LOW]
- **Files:** `execution_routing.py:25`, `perception_routing.py:209`
- **Issue:** Identical functions defined in two files.
- **Impact:** Drift risk.

### OE-4: Duplicate Tool Constant Sets [LOW]
- **Issue:** `READ_ONLY_TOOLS`, `MODIFYING_TOOLS`, `COOLDOWN_READ_TOOLS`, `WRITE_TOOLS_REQUIRING_READ` defined across different files with overlap.
- **Impact:** Drift risk.

---

## 14. Prioritized Fix List

### Phase 1 — Critical Stability Fixes (Weeks 1-2)

| # | Issue | Location | Complexity | Impact |
|---|-------|----------|------------|--------|
| 1.1 | Flip Gate 2c to fail-closed | `permission_gateway.py:469-474` | Low | Prevents broken policy from granting full access |
| 1.2 | Fix bash timeout status | `_bash_exec.py:553-562` | Low | Prevents LLM from treating hung commands as success |
| 1.3 | Fix WorkspaceGuard no-op fallback | `_workspace_guard.py:12-26` | Low | Prevents security bypass on import failure |
| 1.4 | Fix VectorStore add_memory/search_memories | `vector_store.py:302-307` | Medium | Enables cross-session memory persistence |
| 1.5 | Fix CompactionService LLM key mismatch | `compaction_service.py:199-203` | Low | Restores LLM compaction path |
| 1.6 | Add node output schemas for 12 uncovered nodes | `state_schemas.py` | Medium | Enables state validation across all nodes |
| 1.7 | Wire HOOK_SESSION_START call-site | `hook_registry.py` + caller | Low | Fulfills documented API contract |

### Phase 2 — Robustness Improvements (Weeks 3-4)

| # | Issue | Location | Complexity | Impact |
|---|-------|----------|------------|--------|
| 2.1 | Make sandbox default-strict for autonomous mode | `sandbox.py` | Medium | Prevents unsandboxed execution |
| 2.2 | Fix alias permission bypass | `tools_config.py` + `permission_gateway.py` | Medium | Prevents deny-rule evasion via aliases |
| 2.3 | Add NodeResultValidationFailed counter/metric | `state_schemas.py` + events | Medium | Makes fail-open violations observable |
| 2.4 | Consolidate three pruning strategies | `perception_node.py`, `tool_output_pruning.py`, `token_truncation.py` | High | Eliminates redundant compute |
| 2.5 | Guard per-round debug serialization | `perception_node.py:939` | Low | Reduces per-round overhead |
| 2.6 | Remove duplicate `_is_success()` | `execution_routing.py`, `perception_routing.py` | Low | Eliminates drift risk |
| 2.7 | Centralize routing magic numbers | All routing modules | Medium | Single source of truth for thresholds |
| 2.8 | Fix delete_file auto-approval | `permission_gateway.py:200-202` | Low | Requires explicit approval for deletions |

### Phase 3 — Capability Improvements (Weeks 5-8)

| # | Issue | Location | Complexity | Impact |
|---|-------|----------|------------|--------|
| 3.1 | Enable full graph (`_USE_FULL_GRAPH = True`) | `builder.py:94` | High | Restores replan, debug, delegation capabilities |
| 3.2 | Build evaluation framework | New `src/evaluation/` | High | Enables systematic quality measurement |
| 3.3 | Add SWE-bench integration | New evaluation harness | High | Industry-standard benchmarking |
| 3.4 | Implement graph-state checkpointing | `inference_loop.py` + LangGraph checkpointer | High | Enables automatic crash recovery |
| 3.5 | Consolidate duplicate skill directories | `src/config/skills/` → `agent-brain/skills/` | Medium | Single authoritative skill set |
| 3.6 | Remove/update stub roles | `researcher.md`, `scout.md`, `tester.md` | Low | Eliminates defunct code |
| 3.7 | Add CLI feature parity with TUI | `src/main.py` | Medium | Enables headless/scriptable usage |
| 3.8 | Reconcile documentation test baselines | All docs | Low | Single authoritative count |

### Phase 4 — Advanced Features (Weeks 9-12)

| # | Issue | Location | Complexity | Impact |
|---|-------|----------|------------|--------|
| 4.1 | Refactor perception node (reduce fragmentation) | `perception_node.py` + helpers | High | Reduces maintenance burden |
| 4.2 | Remove duplicate defensive fallbacks | Multiple files | Medium | Eliminates second source of truth |
| 4.3 | Add fuzz testing / property-based tests | `tests/` | High | Explores edge cases systematically |
| 4.4 | Add performance benchmark suite | `tests/benchmarks/` | Medium | Tracks performance regressions |
| 4.5 | Add model comparison evaluation | `src/evaluation/` | Medium | Enables provider quality comparison |
| 4.6 | Centralize tool constant sets | `src/tools/constants.py` | Low | Single source of truth |
| 4.7 | Make permission_kind explicit on all tools | All 55 tools in `src/tools/` | Medium | Improves permission precision |
| 4.8 | Add async VectorStore model loading | `vector_store.py` | Low | Prevents thread blocking |

---

## Appendix: Severity Distribution

| Severity | Count | Key Themes |
|----------|-------|------------|
| CRITICAL | 1 | Permission policy fail-open |
| HIGH | 7 | Sandbox fail-open, frozen graph, broken memory, no node validation, autonomous mode suppression, WorkspaceGuard fallback, bash timeout |
| MEDIUM | 18 | Alias bypass, contract validation, pruning overlap, routing defaults, exception swallows, compaction key mismatch, etc. |
| LOW | 20 | MD5 usage, hardcoded constants, documentation staleness, CLI thinness, etc. |

---

## Appendix: File Reference Index

| Component | Key Files | Line Count |
|-----------|-----------|------------|
| Graph Builder | `src/core/orchestration/graph/builder.py` | ~650 |
| AgentState | `src/core/orchestration/graph/state.py` | ~500 |
| Permission Gateway | `src/core/orchestration/permission_gateway.py` | 736 |
| Permission Policy | `src/core/orchestration/permission_policy.py` | 512 |
| Loop Guards | `src/core/orchestration/loop_guards.py` | 447 |
| Event Bus | `src/core/orchestration/event_bus.py` | 447 |
| MessageBus | `src/core/messaging/bus.py` | ~600 |
| Event Types | `src/core/messaging/event_types.py` | ~3000 |
| Orchestrator | `src/core/orchestration/orchestrator.py` | 503 |
| Inference Loop | `src/core/orchestration/inference_loop.py` | 402 |
| Context Builder | `src/core/context/context_builder.py` | 1113 |
| Vector Store | `src/core/indexing/vector_store.py` | 310 |
| Symbol Graph | `src/core/indexing/symbol_graph.py` | 492 |
| LSP Client | `src/core/indexing/lsp_client.py` | 606 |
| Distiller | `src/core/memory/distiller.py` | 753 |
| Auto Compactor | `src/core/memory/auto_compactor.py` | 621 |
| Session Store | `src/core/memory/session_store.py` | 616 |
| Bash Execution | `src/tools/_bash_exec.py` | ~800 |
| Security Constants | `src/tools/_security.py` | 394 |
| Tool Registry | `src/tools/_registry.py` | ~530 |
| Tool Definitions | `src/tools/_tool.py` | ~400 |
| Perception Node | `src/core/orchestration/graph/nodes/perception_node.py` | 1021 |
| Execution Node | `src/core/orchestration/graph/nodes/execution_node.py` | ~400 |
