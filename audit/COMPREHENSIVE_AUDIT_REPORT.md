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

### ~~CF-1: Permission Policy Fail-Open~~ RESOLVED (Phase 1.1)
- **File:** `src/core/orchestration/permission_gateway.py:469-474`
- **Issue:** Gate 2c (PermissionPolicy check) returns `PermissionResult(allowed=True)` on any exception. A broken/absent policy file silently grants full permission to all tools.
- **Impact:** Any ImportError, malformed JSON, or runtime exception in the policy layer disables permission enforcement.
- **Fix:** Flip to fail-closed — return `PermissionResult(allowed=False)` on policy failure, mirroring the correct pattern already in `tool_execution_service._check_permission_gate`.
- **Status:** DONE — `_gate2b_policy_rules`/gate 2c now return `PermissionResult(allowed=False)` with a `"Gate 2c permission policy check failed (fail-closed)"` log on any policy exception; the trailing ALLOW return was made explicit so the fail-closed `except` never yields an implicit allow.

### ~~CF-2: Sandbox Fail-Open to Unsandboxed Execution~~ RESOLVED (Phase 2)
- **File:** `src/tools/sandbox.py:451-478`
- **Issue:** When bubblewrap/sandbox-exec is unavailable or fails, the command runs with full user privileges unsandboxed. `SANDBOX_REQUIRE_ENFORCEMENT=1` is opt-in, not default.
- **Impact:** On macOS (sandbox-exec deprecated) or systems without bubblewrap, all bash commands run unsandboxed by default.
- **Fix:** Make sandbox enforcement default-strict for autonomous mode.
- **Status:** DONE — `sandbox.py` refuses the unsandboxed-`subprocess` fallback when `_enforcement_required()` is true (autonomous mode or `SANDBOX_REQUIRE_ENFORCEMENT`); interactive mode keeps a warned fallback. Network-policy denies are enforced via `_check_network_policy` when the deny cannot be enforced (MC-6).

### ~~CF-3: Graph Running in Stabilization Mode~~ RESOLVED (Phase 3.1)
- **File:** `src/core/orchestration/graph/builder.py:94`
- **Issue:** `_USE_FULL_GRAPH = False` freezes 4 nodes (replan, debug, delegation, analyst_delegation). The agent cannot self-delegate, replan from failure, or enter debug loops.
- **Impact:** Recovery and delegation capabilities are disabled. Agent cannot handle complex multi-step failures.
- **Fix:** Complete stabilization and enable full graph, or remove dead code if fast-path is permanent.
- **Status:** DONE — premise was stale; production runs tier graphs (`_compile_frontier_graph` capable tier, `_compile_lite_graph` lite/small) which already had analyst_delegation/debug/delegation. The one missing capability (replan) is wired: patch-size guard → replan node → re-enter `frontier_loop`. `compile_agent_graph()`/`_USE_FULL_GRAPH` are legacy test-only paths.

### ~~CF-4: Cross-Session Memory Persistence Broken~~ RESOLVED (Phase 1.4 / Mem-4)
- **File:** `src/core/indexing/vector_store.py:302-307`
- **Issue:** `add_memory()` and `search_memories()` are no-op stubs. The distiller calls `add_memory()` thinking it persists summaries, but nothing is stored.
- **Impact:** Cross-session memory recall via VectorStore is non-functional. Semantic memory retrieval across sessions does not work.
- **Fix:** Implement actual storage/retrieval or remove the dead code path.
- **Status:** DONE — `add_memory()` persists to `agent_context_path(workdir)/vectorstore/memories.jsonl` (atomic tmp-file `replace()`, `_MEMORY_LOCK` dedup-read + append, rotation keeps newest 200); `search_memories()` returns deduped records with the `vector` field stripped. Async wrappers (`asearch`/`aadd_memory`/`asearch_memories`/`aindex_code`) run sync bodies via `asyncio.to_thread`.

### ~~CF-5: Node Output Validation Covers Only 4/16 Nodes~~ RESOLVED (Phase 1.6 / 2.3)
- **File:** `src/core/orchestration/graph/state_schemas.py`
- **Issue:** Only perception, planning, execution, verification have output schemas. 12 nodes have zero boundary validation, and the strict flag (`_STATE_SCHEMAS_STRICT`) is hardcoded `False`.
- **Impact:** Node output pollution is possible and undetected. Violations are logged but never enforced or counted.
- **Fix:** Add schemas for all 16 nodes. Wire a `NodeResultValidationFailed` counter for observability.
- **Status:** DONE — all 16 nodes wrapped via `_validated(...)`, and `_default_publish_violation` publishes `NodeResultValidationFailed` and increments the thread-safe `metrics` singleton (`graph.node_validation_failed` total + per-node + per-reason), guarded so metrics can never break node execution.

---

## 4. High-Risk Safety Issues

### ~~HS-1: Autonomous Mode Suppresses All Approval Prompts~~ RESOLVED (Phase 5)
- **Files:** `src/tools/tools_config.py:250-260`, `src/core/orchestration/permission_gateway.py:677-678`
- **Issue:** `is_autonomous()` auto-allows DANGER/PROMPT tools with no operator override. The env var `CODINGAGENT_AUTONOMOUS` is checked at each call, so any process with env control disables all safety prompts.
- **Severity:** HIGH
- **Fix:** Autonomous approval now requires an explicit operator allowlist. `autonomous_approval_allowed(name)` is the single decision point (alias-resolved; honors `set_autonomous_approve()`/`configure(autonomous_approve=...)` + the `CODINGAGENT_AUTONOMOUS_APPROVE` env var, or `*`). Unlisted gated tools are denied loudly (fail-closed) in all four suppression sites: production `tool_execution_pipeline._run_permission_gate`, `tool_execution_service._check_permission_gate`, `permission_gateway._gate5_user_approval`, and `_bash_exec._check_tier3_approval`. CLI: `--autonomous-approve <TOOL>…`. 20 contract tests in `tests/unit/test_phase5_security_hardening.py`. See `audit/PHASE5_PROGRESS.md`.

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
- ~~No SWE-bench integration, no scenario evaluation harness, no regression test suite, no model comparison evaluation.~~ **Resolved (3.2 + 3.3 COMPLETED):** `src/core/evaluation/` ships a public API + `python -m src.core.evaluation` CLI (`list`/`run`/`baseline-save`/`swebench`) over 23 scenarios with pass@k and a golden-regression gate, plus a SWE-bench style instance loader + grader (patch extraction, test_patch apply, FAIL_TO_PASS/PASS_TO_PASS runs, baseline-compatible).
- Testing is unit-test-centric (354 test files, ~4,804 tests collected by `pytest tests/unit tests/integration/test_fast_path_graph_e2e.py`) but lacks systematic agent-quality measurement.
- **Impact:** Cannot quantify agent reliability, edit accuracy, or tool usage correctness.

### MC-2: HOOK_SESSION_START Defined but Never Invoked
- **File:** `src/core/plugin/hook_registry.py:79`
- **Issue:** The hook is exported and documented but has zero call-sites. Documented API without implementation.
- **Impact:** Plugin authors cannot hook into session start.

### MC-3: No Crash-State Checkpointing
- **Issue:** LangGraph state is not checkpointed per-superstep. A crash mid-graph loses all in-flight state. Resume requires manual `/continue` invocation.
- **Impact:** Partial work from crashed runs is lost unless manually checkpointed via tool.

### ~~MC-4: Duplicate Skill Directories~~ RESOLVED (Phase 3.5)
- **Files:** `src/config/agent-brain/skills/` (8 skills) and `src/config/skills/` (5 older skills)
- **Issue:** Two parallel skill sets with different formats. `explore_codebase` only exists in legacy dir.
- **Impact:** Unclear which is authoritative. Potential confusion.
- **Fix:** Removed the legacy `src/config/skills/` directory. `explore_codebase.md` migrated into `agent-brain/skills/` in the canonical format (front-matter + When to Use/Strategy/Execution Steps); the 4 legacy duplicates (`code_review`, `debug_checklist`, `refactor`, `write_tests`) dropped because the `agent-brain/skills/` versions are strictly newer. `skill_tools.load_skill`/`list_skills` now point at `agent-brain/skills/`. Prompt templates updated and goldens regenerated. Contract tests in `tests/unit/test_skill_consolidation.py`.

### ~~MC-5: Stub Roles Referencing Defunct Architecture~~ RESOLVED (Phase 3.6)
- **Files:** `src/config/agent-brain/roles/researcher.md`, `scout.md`, `tester.md`
- **Issue:** Reference legacy P2P broadcast topics (`agent.researcher.broadcast`) that don't exist in current architecture.
- **Impact:** Roles are non-functional if dispatched.
- **Fix:** Deleted `researcher.md` (the role canonicalizes to `analyst`, so the file was dead content). Rewrote `scout.md`/`tester.md` as functional roles reporting via the returned result instead of a publish topic. Aligned `SCOUT_AGENT`/`TESTER_AGENT` prompt_overrides in `agent_types.py`. Contract tests in `tests/unit/test_role_brain.py`.

### ~~MC-6: Missing Per-Tool Network Policy in Sandbox~~ RESOLVED (Phase 5)
- **Issue:** `bash` (vs `bash_readonly`) doesn't pass `network=False`. Network-capable commands depend on sandbox level, which is opt-in.
- **Impact:** Without sandbox enforcement, `curl`, `wget` etc. run with full network access.
- **Fix:** `bash()` now declares an explicit `network=False`; new `_check_network_policy` guard (`_bash_exec.py`) refuses network-capable commands (classified via `is_network_capable` in `_approval.py`) when the deny cannot be enforced and enforcement is required (autonomous / `SANDBOX_REQUIRE_ENFORCEMENT`); warns otherwise. Defense-in-depth beyond the existing gate-2 DANGEROUS block + git subcommand allowlist.

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

### ~~TW-2: run_in_background Bypasses Sandboxing~~ RESOLVED (Phase 5)
- **File:** `src/tools/_bash_exec.py:496-515`
- **Issue:** `Popen` with `stdout=DEVNULL` — no sandbox, no output capping, unsupervised process.
- **Severity:** MEDIUM
- **Fix:** Background spawns are now wrapped in a sandbox when a backend exists (`_build_background_sandbox`: bwrap prefix / enforcing sandbox-exec profile → filesystem confinement + network deny present); refused fail-closed when enforcement is required (autonomous / `SANDBOX_REQUIRE_ENFORCEMENT`) but no backend is available; interactive mode falls back unsandboxed with a `system.warning`. Output stays `DEVNULL` (inherently capped).

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
- **354 test files** across `tests/unit/` with ~4,804 tests (collected: `pytest tests/unit tests/integration/test_fast_path_graph_e2e.py`)
- Strong security-specific tests (bypass vectors, SSRF, injection, concurrency)
- Scenario evaluation framework with pass@k + golden-regression gate + SWE-bench harness (3.2 + 3.3 COMPLETED)
- SWE-bench-style evaluator with 23 scenarios

### Missing
| Gap | Impact |
|-----|--------|
| SWE-bench integration (3.3, now harness present; live dataset env shims still manual) | Cannot compare against industry standard |
| Formal evaluation framework (resolved in 3.2) | No systematic quality measurement |
| No performance benchmarks (1 file only) | Cannot track performance regressions |
| ~~No model comparison evaluation~~ (resolved in 4.5) | Cannot compare provider quality |
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
| 3.1 | ~Enable full graph (~`_USE_FULL_GRAPH`)~ → Add replan to frontier graph. `COMPLETED` — see `audit/PHASE3_PROGRESS.md`. Premise was stale: production runs tier graphs, not `compile_agent_graph()`; frontier graph already had analyst_delegation/debug/delegation. Replan was the one missing capability; now wired (patch-size guard → replan node → re-enter loop). | `builder.py`, `tier_graph_routing.py`, `frontier_loop_node.py` | High | Restores replan capability in production |
| ~~3.2~~ | **Build evaluation framework (COMPLETED − public API in `src/core/evaluation/__init__.py`, `python -m src.core.evaluation` CLI runner (`list`/`run`/`baseline-save`) with pass@k sampling, JSON reports, and a golden-regression gate (exit 1 on regression); regression compare helpers in `regression.py`; 16 new tests; full suite 4,780 passing)** | New `src/evaluation/` → `src/core/evaluation/` | High | Enables systematic quality measurement |
| ~~3.3~~ | **Add SWE-bench integration (COMPLETED − `swebench.py` loader + grader + `SWEBenchRunner`, `swebench` CLI subcommand with regression gate, 24 new tests; full suite 4,804 passing)** | New evaluation harness | High | Industry-standard benchmarking |
| ~~3.4~~ | **Implement graph-state checkpointing (COMPLETED − `JsonlCheckpointSaver` wired into production tier graphs + round-boundary durable state snapshots in `inference_loop.py`; `--continue` resumes the same thread via task-id seeding in `src/main.py`; toggle OFF under pytest)** | `src/core/orchestration/graph/checkpoint_saver.py` (new), `builder.py`, `inference_loop.py`, `src/main.py` | High | Automatic crash recovery at round granularity; node-level run forensics via per-thread checkpoint JSONL |
| ~~3.5~~ | **Consolidate duplicate skill directories (COMPLETED)** | `src/config/skills/` → `agent-brain/skills/` (legacy dir removed) | Medium | Single authoritative skill set |
| ~~3.6~~ | **Remove/update stub roles (COMPLETED − scope corrected: `researcher.md` removed, `scout.md`/`tester.md` rewritten as functional roles)** | ~~`researcher.md`, `scout.md`, `tester.md`~~ → see `audit/PHASE3_PROGRESS.md` | Low | Eliminates defunct code |
| ~~3.7~~ | **Add CLI feature parity with TUI (COMPLETED − new `src/cli/` package: `session`/`status`/`mcp`/`diff` subcommands + `--provider`/`--model`/`--continue` flags, live `ModelRouting` override, `--continue` resume via last_plan task)** | `src/main.py`, new `src/cli/` | Medium | Enables headless/scriptable usage |
| ~~3.8~~ | **Reconcile documentation test baselines (COMPLETED − README "Test Baseline" is now the single authoritative count via `pytest tests/unit tests/integration/test_fast_path_graph_e2e.py`; 4,764 tests; all current-state claims in dev docs/ARCH/AGENTS/audit reports aligned; historical test reports annotated as point-in-time; root-level duplicate report still open cleanup)** | All docs | Low | Single authoritative count |

### Phase 4 — Advanced Features (Weeks 9-12)

| # | Issue | Location | Complexity | Impact |
|---|-------|----------|------------|--------|
| ~~4.1~~ | **Refactor perception node (reduce fragmentation) (COMPLETED − `perception_node.py` is now a thin orchestrator; the 12 wrapper functions + dead `_await_llm_task` shim removed; each helper module is self-contained (own module logger + deps resolved in-module via graceful imports); genuinely-local functions moved to subject modules (`_parse_yaml_tool_call_from_content`→`perception_parsing`, `_select_corrective_prompt`→`perception_no_tool`, `_classify_model_tier`/`_check_small_model_clarification`→`perception_runtime`); 20+ DI params dropped from helper signatures (API kept only the `call_model_fn` patch seam in `_validate_call_model_and_adapter` and the `call_model` module attribute that integration tests patch); legacy test surface kept via `# noqa: F401` re-exports; suite still 4,850 collecting — all green)** | `perception_node.py` + helpers | High | Reduces maintenance burden |
| ~~4.2~~ | **Remove duplicate defensive fallbacks (COMPLETED − collapsed the duplicated `estimate_text_tokens` (2 identical copies → 1 canonical in `token_truncation.py`, imported by `tool_output_truncation.py`); pruned 4 stale legacy names (`glob_tool`/`grep_tool`/`list_dir`/`run_bash`) from `permission_gateway._TOOL_KIND_MAP`, added alias resolution in `_tool_kind_for_name`, and drift-locked the fallback with `test_tool_kind_map_keys_resolve_to_live_registry_tools`; suite now 4,829 collecting)** | Multiple files | Medium | Eliminates second source of truth |
| ~~4.3~~ | **Add fuzz testing / property-based tests (COMPLETED − deterministic seed-based suite `tests/unit/test_property_based_fuzzing.py` (21 tests, `random.Random(0xC0FFEE)`, 150 iters) covering token estimator/truncation, both output pruners, prompt-injection guard, byte-cap truncation, alias/kind maps, `_path_inside`; fuzz found + fixed a latent crash on non-dict `metadata` in `prune_tool_outputs`; suite now 4,850 collecting)** | `tests/` | High | Explores edge cases systematically |
| ~~4.4~~ | **Add performance benchmark suite (COMPLETED − added `tests/benchmarks/test_pure_hotpath_benchmarks.py` (11 deterministic LLM-free tests, thresholds ≥10x headroom): token estimation/truncation on 1 MB+ inputs, tool-output pruning, permission/registry kind lookup (20k), prompt-injection guard, typed MessageBus publish+dispatch (3k); existing dispatch/planning/DAG suite retained; benchmark suite now 18 tests in the dedicated CI `benchmarks` job)** | `tests/benchmarks/` | Medium | Tracks performance regressions |
| ~~4.5~~ | **Add model comparison evaluation (COMPLETED − `compare.py`: `run_models` with per-model isolated run dirs, `compare_models` (per-scenario winners / ranking / ties, accepts ScenarioResult or SWE-bench dicts), `save_comparison` JSON report; `compare` CLI subcommand `--model label=factory …`; 10 new tests)** | `src/evaluation/` | Medium | Enables provider quality comparison |
| ~~4.6~~ | **Centralize tool constant sets (COMPLETED − canonical `src/tools/constants.py`: `WRITE_TOOLS_REQUIRING_READ`/`MODIFYING_TOOLS`/`DRY_RUN_BLOCKED_TOOLS`/`PERMISSION_REQUIRED_TOOLS`/`PERM_ORDER`/`WORKDIR_SAFE_TOOLS`/`FILE_TOOLS`/`TOOL_ALIASES`; `tool_constants.py`, `loop_guards.py`, `permission_gateway.py`, `tools_config.py` re-export; object-identity tests + `TestCanonicalCentralization` (6 tests); suite now 4,820 collecting)** | `src/tools/constants.py` | Low | Single source of truth |
| ~~4.7~~ | **Make permission_kind explicit on all tools (COMPLETED − every built-in `@tool` now declares `permission_kind=` at definition time (33 decorators updated across 15 modules); `ToolDefinition.permission_kind_explicit` records explicitness; registry `get_permission_kind` now returns precise kinds (WRITE_FILE/EXECUTE_BASH/GIT_READ/GIT_WRITE/LSP_*/PLAN); contract test `test_tool_permission_kind_explicit` (4 tests) enforces no regressions; suite now 4,830 collecting)** | All 55 tools in `src/tools/` | Medium | Improves permission precision |
| ~~4.8~~ | **Add async VectorStore model loading (COMPLETED − single-flight model loader (`_ST_MODEL_LOCK`); non-blocking `_get_st_model_ready()`; daemon-thread background preload on `VectorStore.__init__`; event-loop-safe `get_st_model_async()` via `asyncio.to_thread`; VectorStore `asearch`/`aadd_memory`/`asearch_memories`/`aindex_code` async wrappers; search/encode hot paths degrade to stub instead of blocking; 6 new tests; suite now 4,826 collecting)** | `vector_store.py` | Low | Prevents thread blocking |

### Phase 5 — Security-Policy & Audit-Backlog Closeout (Weeks 13-16)

| # | Item | Location | Complexity | Impact |
|---|------|----------|------------|--------|
| ~~HS-1~~ | **Autonomous-mode approval suppression (COMPLETED − `autonomous_approval_allowed(name)` is the single decision point, alias-resolved, honoring `set_autonomous_approve()`/`configure(autonomous_approve=...)`/`CODINGAGENT_AUTONOMOUS_APPROVE` (`*` or comma-separated); empty allowlist fails closed — every DANGER/PROMPT tool is DENIED; wired into all four suppression sites (`tool_execution_pipeline._run_permission_gate`, `tool_execution_service._check_permission_gate`, `permission_gateway._gate5_user_approval`, `_bash_exec._check_tier3_approval`); 20 contract tests in `test_phase5_security_hardening.py`)** | `tools_config.py`, `permission_gateway.py`, `_bash_exec.py`, pipeline | High | Prevents autonomous mode from disabling all safety prompts |
| ~~MC-6~~ | **Missing per-tool network policy in sandbox (COMPLETED − every `run_sandboxed()` call passes `network=` explicitly (`bash()`/`bash_readonly()` pass `network=False`); `is_network_capable` classifies curl/wget/pip/apt/brew/ssh/rsync/scp + multi-token git remote ops + npm/cargo/go install; `_check_network_policy` refuses when the deny can't be enforced (sandbox level `off`/no enforcing backend) under enforcement-required; interactive mode warns instead)** | `_bash_exec.py`, `_approval.py`, `sandbox.py` | High | Network-capable commands no longer escape explicit deny |
| ~~TW-2~~ | **run_in_background sandbox escape (COMPLETED − background spawns route through `_build_background_sandbox` (bwrap `--unshare-net`, ro binds, `--die-with-parent`, or enforcing sandbox-exec profile); `None` fallback only when not enforcement-required + `system.warning`; output stays `DEVNULL`; 7 contract tests `TestTW2BackgroundSandbox`)** | `_bash_exec.py` | Medium | Background commands cannot silently escape the sandbox |
| ~~TW-3~~ | **Contract `model_validate` fail-open (COMPLETED − broken/failing `get_tool_contract` or `model_validate` now fail closed with observable `contract validation` error; pydantic `ValidationError` still returns the schema message; valid contract permits execution; 4 tests `TestTW3ContractValidation`)** | `tool_execution_pipeline.py` | Medium | Broken contracts no longer silently pass |
| ~~MC-2 / 1.7~~ | **HOOK_SESSION_START call-site (VERIFIED no-op − already wired at `inference_loop.py` (Phase 3.4) and covered by `test_hook_registry` item 1.7; backward item, no code change)** | `hook_registry.py`, `inference_loop.py` | Low | Documented API already fulfilled |
| ~~HS-6~~ | **1,868 silent exception swallows (COMPLETED − full triage confirmed the majority are intentional (event publish / optional import / parse fallback); converted the high-value defect-hiding sites to logged failures: post-exec contract framework, plugin post-tool hook, and CP-3.4 thread-state recovery — log + `exc_info=True`, behavior-neutral (fail-open infra retained); 3 tests `TestHS6ObservableSwallows`)** | across `src/` | Medium | High-value blind swallows are now observable |
| ~~WR-1~~ | **Routing default-to-perception loop risk (COMPLETED − verified interlocking guards hold: no-tool exhaustion emits `infinite_loop_no_tool` → terminal message; fast-path END for completed simple tasks; rounds monotonic + outer `MAX_GRAPH_ROUNDS`; 3 contract tests)** | `session_routing.py`, `perception_no_tool.py` | Medium-High | Default loop has no unbounded-exit hole |
| ~~WR-2~~ | **Two independent round-limiting mechanisms (COMPLETED − `routing_constants.MAX_GRAPH_ROUNDS = 20` is the single outer graph-loop budget; `inference_loop` default + `_actual_limit` fallback both bind to it; identity-binding test `test_routing_constants.py`)** | `routing_constants.py`, `inference_loop.py`, `inference_loop_rounds.py` | Low | No drift between round caps |
| ~~WR-3~~ | **Inter-round compaction role alternation (COMPLETED − compaction now emits `[COMPACTED]` system summary + recent messages verbatim (`_KEEP_RECENT_MSGS`) + single user continuation turn; trailing already-appended user turn dropped; tests in `test_distillation_wiring.py`)** | `inference_loop_rounds.py` | Low | No back-to-back user turns after compaction |
| ~~WR-5~~ | **Complexity-heuristic keyword fragility (COMPLETED − language-agnostic fallback: ≥20 words (multi-script) or ≥60 CJK chars → complex; English keywords still run first; tests in `test_graph_builder_routing.py`)** | `perception_routing.py` | Low | Non-English tasks no longer bypass complexity routing |
| ~~TW-4~~ | **Inconsistent truncation limits (COMPLETED − `RESULT_MAX_CHARS = 8_000` added to `_truncate.py` as the third tier; pipeline `TOOL_OUTPUT_MAX_CHARS` binds to the canonical value; audit's 16 KB no longer present in code (stale); tests in `test_truncate.py`)** | `_truncate.py`, `tool_execution_pipeline.py` | Low | Single set of tiered caps |
| ~~TW-5~~ | **`_BUILTIN_MODULES` hardcoded (COMPLETED − `pkgutil.iter_modules` auto-discovery of `@tool` modules (`_builtin_module_names()`, lru_cache, sorted, skips private/init/packages); `_OPTIONAL_MODULES` (lsp_tools) retained; tests `TestBuiltinModuleAutoDiscovery`)** | `src/tools/_registry.py` | Low | New tool modules register automatically |
| ~~RA-3~~ | **LSP semaphore unenforced (COMPLETED − `LSPManager.limit_concurrency()` asynccontextmanager over the existing semaphore; all 6 LSP tools wrapped; tests verify peak concurrency == `max_concurrent` + shared-semaphore reuse)** | `lsp_manager.py`, `lsp_tools.py` | Low | Enforced concurrency limit |
| ~~RA-4~~ | **Symbol graph MD5 (COMPLETED − SHA-256 swap; new tests `TestSymbolGraphHashPrimitives`)** | `symbol_graph.py` | Low | No MD5 in change detection |
| ~~3.8 leftover~~ | **Root-level test-report cleanup (COMPLETED − `.coverage` untracked via `git rm --cached`; `coverage.xml`/`results/` already ignored)** | repo root | Low | Clean tree root |
| ▶ Live-provider checks | **Phase-3/4 feature-surface verification end-to-end — still skipped in CI (`live-provider-checks` job); requires provider credentials** | CI | — | Manual/credential-gated |

All Phase-5 items above were each gated by the full unit baseline (`pytest tests/unit` → **~4,909 passed, 1 skipped**) + ruff, committed as single `AUDIT PHASE-5 (…): …` commits, and their GitHub Actions pushes report `success`. Detailed narrative: `audit/PHASE5_PROGRESS.md`.

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
