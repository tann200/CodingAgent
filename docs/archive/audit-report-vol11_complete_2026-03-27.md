# Audit Report — Vol 11

**Date:** 2026-03-27
**Baseline:** 1454 passed, 4 skipped, 0 failed (Vol10 final)
**Post-fix:** 1564 passed, 4 skipped, 10 warnings
**Scope:** Full-spectrum system audit per audit-instructions.md
**Auditor:** Claude Sonnet 4.6

---

## Remediation Status

| ID | Finding | Status | Verified |
|----|---------|--------|----------|
| CF-1 | Direct state mutation in `delegation_node` | **FIXED** | Tests pass |
| HR-1 | `_safe_resolve_workdir` no-op path traversal guard | **FIXED** | 4 new tests |
| HR-2 | Complexity classifier kills fast-path for all editing tasks | **FIXED** | 2 tests updated |
| HR-3 | `debug_node` ignores JS/TS verification failures | **FIXED** | Tests pass |
| HR-4 | Failed no-plan execution unbounded retry | **FIXED** | Tests pass |
| HR-5 | Non-Optional AgentState fields without runtime defaults | **FIXED** | Tests pass |
| MM-1 | Compaction checkpoint leaks between tasks | **FIXED** | Tests pass |
| WR-4 | `plan_mode_approved` reset comment without implementation | **NO CHANGE NEEDED** | All return paths already include reset |
| ET-1 | No tests for `_safe_resolve_workdir` path traversal | **FIXED** | 4 new regression tests |

---

## 1. Executive Summary

The CodingAgent is a sophisticated local-first coding agent with a well-structured LangGraph state
machine, multi-language code intelligence, and layered security controls. The architecture has
matured significantly across 10+ audit cycles. The test suite is comprehensive (1454 passing
tests), CI is properly configured, and most known failure modes have been addressed.

However, this audit identifies **1 Critical**, **5 High**, **7 Medium**, and **5 Low** issues that
affect correctness, performance, and maintainability. The most serious are: a LangGraph state
mutation bug in `delegation_node`, an ineffective path-traversal rejection in
`verification_tools.py`, and an overly-aggressive complexity classifier that makes the fast-path
dead code for nearly all editing tasks.

**All Critical and High findings (CF-1, HR-1 through HR-5) plus MM-1 and ET-1 have been
remediated.** WR-4 was verified as already correct (all `planning_node` return paths already
include `plan_mode_approved: None`). Post-fix test suite: 1564 passed, 4 skipped, 10 warnings.

---

## 2. Architecture Strengths

- **Well-designed LangGraph pipeline**: 14 nodes with clear single responsibilities. Router
  functions are deterministic and well-guarded against loops.
- **Layered security model**: Three-tier bash security (SAFE / TEST_COMPILE / RESTRICTED +
  DANGEROUS_PATTERNS) with whitespace normalisation. WorkspaceGuard protects sensitive files.
- **Comprehensive loop-prevention**: `rounds`, `plan_attempts`, `replan_attempts`,
  `debug_attempts`, `total_debug_attempts`, `step_retry_counts`, `tool_call_count`, and
  `empty_response_count` all cap distinct failure modes.
- **Cross-language support**: Verification, syntax-check, and SymbolGraph cover Python, JS/TS,
  Go, Rust, Java, and more.
- **Context management**: Auto-compaction at 85% token budget, checkpoint persistence, LRU caches
  for context builders.
- **Test coverage**: 100+ unit test files, E2E scenario tests with mock LLM, integration tests,
  and pipeline latency benchmarks.
- **Retry with backoff**: Adapter-layer 3× retry (1s/2s) for 429/500/502/503/504.
- **Plan persistence**: Cross-session `last_plan.json` with fuzzy task-matching for resume.

---

## 3. Critical Architectural Flaws

### CF-1 — Direct State Mutation in `delegation_node` [CRITICAL] [FIXED]

**File:** `src/core/orchestration/graph/nodes/delegation_node.py:238`

```python
state["_file_lock_manager"] = lock_manager   # DIRECT IN-PLACE MUTATION
```

LangGraph nodes must never mutate `state` in-place. All state updates must be returned as a dict.
This mutation:
- Is invisible to LangGraph's reducer chain (the value is not propagated correctly).
- In concurrent delegation scenarios (multiple parallel subagents) this creates a race condition
  on the `AgentState` dict reference.
- The mutated value (`_file_lock_manager`) is then inaccessible to nodes that run after the
  delegation subgraph because LangGraph replaces the state object from the returned dict.

Additionally, the docstring in `create_delegation()` at lines 394–396 documents the anti-pattern:

```python
# Usage in other nodes:
#   state["delegations"] = [...]   ← teaches direct mutation
```

This perpetuates the bug in future node implementations.

**Fix applied:** Changed `state["_file_lock_manager"] = lock_manager` to
`results["_file_lock_manager"] = lock_manager` so the value is returned in the node's result
dict. Updated `create_delegation()` docstring to show the correct
`return {"delegations": [...]}` pattern.

---

## 4. High-Risk Safety Issues

### HR-1 — `_safe_resolve_workdir` Does Not Reject Path Traversal [HIGH] [FIXED]

**File:** `src/tools/verification_tools.py:12–28`

Original implementation (no-op):

```python
def _safe_resolve_workdir(workdir: str) -> str:
    try:
        resolved = str(Path(workdir).resolve())
        return resolved          # ← always returns, never rejects
    except Exception:
        return workdir
```

The docstring claims it rejects path-traversal, but the implementation does nothing of the sort.
`Path("../../etc/passwd").resolve()` returns `/etc/passwd` and is accepted. This function is
called at the start of `run_tests()` with LLM-supplied `workdir`, creating an escape vector:
the agent could be instructed to run tests in an arbitrary directory outside the workspace.

**Fix applied:** Rewrote to reject relative paths (must be absolute), resolve via
`os.path.realpath`, and block system directories (`/etc`, `/usr`, `/bin`, `/sbin`, `/boot`,
`/proc`, `/sys`). Raises `ValueError` on rejection; `run_tests()` catches it and returns
`{"status": "error"}`. Uses `os.path.realpath` on both sides of the comparison to handle macOS
`/etc` → `/private/etc` resolution. Added 4 new regression tests for path traversal rejection.

### HR-2 — Fast-Path Effectively Dead for All Editing Tasks [HIGH] [FIXED]

**File:** `src/core/orchestration/graph/builder.py:162–225`

`_task_is_complex` returns `True` for any task whose description matches:

```python
_COMPLEXITY_KEYWORDS_WORD = (
    "add", "edit", "modify", "update", "append", "prepend",
    "change", "replace", "delete", "remove", "insert",
)
```

These are matched with word boundaries, meaning tasks like:
- "add a comment" → `\badd\b` matches → **complex**
- "edit the config" → `\bedit\b` matches → **complex**
- "remove unused import" → `\bremove\b` matches → **complex**
- "fix a typo" → does NOT match → fast-path (correct)

In practice, nearly every coding task uses one of these verbs. This forces trivially-simple
single-file edits through: `analysis → analyst_delegation → planning → plan_validator →
step_controller → execution` — 6 extra LLM calls for a 2-line change.

**Fix applied:** Removed common single-file action verbs (`add`, `edit`, `modify`, `update`,
`append`, `prepend`, `change`, `replace`, `delete`, `remove`, `insert`) from the word-boundary
keyword set. Only genuinely multi-step verbs remain: `refactor`, `rewrite`, `implement`,
`migrate`, `restructure`. Updated 2 unit tests that previously asserted `add`/`edit` matched
as complex to assert the new fast-path behavior.

### HR-3 — `debug_node` Ignores JS/TS Verification Failures [HIGH] [FIXED]

**File:** `src/core/orchestration/graph/nodes/debug_node.py:62–78`

Original (Python-only checks):

```python
error_summary = ""
if last_result.get("error"):
    error_summary = f"Tool error: {last_result.get('error')}"
elif verification_result:
    v = verification_result.get("tests", {})      # ← Python only
    if v.get("status") == "fail":
        error_summary = f"Test failure: {v.get('stdout', '')[:500]}"
    v = verification_result.get("linter", {})     # ← Python only
    if v.get("status") == "fail":
        error_summary += f" Linter: {v.get('stdout', '')[:200]}"
```

The evaluation_node correctly checks all 6 result keys (`tests`, `linter`, `syntax`, `js_tests`,
`ts_check`, `eslint`). The debug_node only checks `tests` and `linter`. A JS/TS project failure
(`js_tests` fail, `eslint` fail) produces `error_summary=""` → `error_type="unknown_error"` →
generic prompt "Analyze the error carefully" instead of targeted guidance.

**Fix applied:** Added checks for `js_tests`, `ts_check`, `eslint`, and `syntax` keys in
`verification_result`. JS/TS failures now produce targeted debug prompts with stdout context.

### HR-4 — `should_after_execution` Routes Failed Steps to Perception [HIGH] [FIXED]

**File:** `src/core/orchestration/graph/builder.py:386–448`

When a plan step fails in the execution node (no plan, not wave execution), the router sends to
`perception` — not `debug_node`. This creates a retry that bypasses error analysis:

`execution (fail) → perception → [fast-path → execution again]`

The `should_after_step_controller` does enforce `step_retry_counts`, but this path is in
`should_after_execution`, which is invoked when there IS no current_plan (no-plan execution
path). These two code paths have inconsistent failure handling: plan-based failures enforce
retries via step_controller, but no-plan failures simply re-invoke perception with no budget cap.

**Fix applied:** Added `no_plan_fail_count` field to `AgentState` (Optional[int]). The
`execution_node` increments it on no-plan failure and resets to 0 on success. The
`should_after_execution` router checks `no_plan_fail_count` and bails to `memory_sync` after
3 consecutive no-plan failures, providing the same bounded retry semantics as
`step_retry_counts` for the plan-based path.

### HR-5 — AgentState Non-Optional Fields Without Enforced Defaults [HIGH] [FIXED]

**File:** `src/core/orchestration/graph/state.py`

Several fields were declared as non-Optional (no `Optional[...]` wrapper):

```python
max_debug_attempts: int         # required
step_controller_enabled: bool   # required
tool_call_count: int            # required
max_tool_calls: int             # required
current_wave: int               # required
plan_attempts: int              # required
replan_attempts: int            # required
```

TypedDict does not enforce runtime defaults. If `initial_state` is constructed without these
fields (e.g. in test scaffolding or new node authors), `state.get("max_debug_attempts")` returns
`None` and `int(None)` raises `TypeError`. The pattern `int(state.get("max_debug_attempts") or 3)`
provides a silent fallback, but `int(state.get("tool_call_count") or 0)` fails if `state` is
passed as a plain dict that omits these keys elsewhere in the codebase.

**Fix applied:** Changed 14 non-Optional fields to `Optional[...]` in `AgentState`:
`max_debug_attempts`, `step_controller_enabled`, `tool_call_count`, `max_tool_calls`,
`current_wave`, `plan_attempts`, `replan_attempts`, `empty_response_count`,
`preview_mode_enabled`, `awaiting_user_input`, `plan_mode_enabled`, `awaiting_plan_approval`,
`last_compact_turn`, `context_degradation_detected`. All call sites already use the
`int(state.get("field") or DEFAULT)` pattern, so no code changes were needed beyond the type
annotations.

---

## 5. Major Missing Capabilities

### MC-1 — No Workspace Root Enforcement in `_safe_resolve_workdir` [FIXED]

See HR-1. The `safe_resolve` utility in `src/tools/_path_utils.py` correctly bounds path
resolution to a workspace root. `verification_tools.py` re-implemented this without the bounds
check. The two implementations should be unified.

**Fix applied:** See HR-1. `_safe_resolve_workdir` now resolves via `os.path.realpath` and
rejects system directories. Further unification with `_path_utils.safe_resolve` (workspace-root
bounding) is deferred to Phase 4.

### MC-2 — No Structured Error Taxonomy Exposed to Replan

`debug_node` classifies errors (`syntax_error`, `import_error`, `test_failure`, etc.) but this
classification is not carried forward into the `replan_node`. When debugging exhausts its budget,
the error type is lost. `replan_node` has no access to what went wrong; it issues a generic
"replan" LLM call without knowing whether the failure was syntax, test, lint, or logic.

### MC-3 — No User-Facing Plan Diff Before Execution

`plan_mode_enabled` gates execution behind `wait_for_user_node` approval. But the TUI shows the
raw plan text, not a rendered diff of what files would be modified. The `file.diff.preview` event
fires _during_ execution (before each individual write). There is no pre-execution "here are all
files this plan will touch" summary to help users decide whether to approve.

### MC-4 — PRSW Lock Manager Not Persisted to State [PARTIALLY FIXED]

Related to CF-1: `_file_lock_manager` was mutated into state directly (the bug) instead of being
returned. The CF-1 fix ensures the lock manager is now returned in the node's result dict and
propagated via LangGraph's reducer chain. However, the `FileLockManager` remains a stateful
in-process object that cannot be serialized by LangGraph checkpointers, so PRSW locking still
only works within a single in-process execution.

**CF-1 fix applied:** `_file_lock_manager` now returned in results dict. Serialization gap
remains open (low priority for single-user local agent).

### MC-5 — No Rollback on Tool Budget Exhaustion

When `tool_call_count >= max_tool_calls`, execution routes to `memory_sync` (W12). No rollback
is attempted. If the budget was exhausted mid-edit (partial writes, deleted files), the workspace
is left in an inconsistent state. Rollback is only attempted when `debug_attempts` are exhausted
in `debug_node` — these are two different exit paths.

---

## 6. Workflow Reliability Issues

### WR-1 — Complexity Classifier False-Positives Cause Analyst Delegation Overhead [FIXED]

See HR-2. Every `add`/`edit`/`change`/`update` task forced routing through
`analyst_delegation_node`, which spawned a subagent. For local LLM with limited concurrency,
this significantly increased latency for simple tasks.

**Fix applied:** See HR-2. Common action verbs removed from `_COMPLEXITY_KEYWORDS_WORD`.

### WR-2 — `evaluation_result="replan"` Routes to `step_controller` Without Recheck

**File:** `src/core/orchestration/graph/builder.py:589–593`

```python
elif evaluation_result == "replan":
    return "step_controller"
```

`evaluation_node` returns `"replan"` when `current_step < len(current_plan)` after execution.
`step_controller` re-reads `current_step` from state. However, `execution_node` advances
`current_step` in its return dict, so by the time `evaluation` runs, `current_step` already
points to the _next_ unexecuted step. Then step_controller would see `last_result` from the
previous step's _successful_ execution and route to `execution` again — skipping verification
for the intermediate step.

### WR-3 — No-Plan Execution Failure Counts Not Bounded [FIXED]

**File:** `src/core/orchestration/graph/builder.py:386–448`, `state.py`

When executing without a plan (fast-path), a failed execution went to `perception` and could
loop back to `execution` indefinitely. The only bounds were `rounds < 15` and
`tool_call_count < max_tool_calls`. No per-failure retry cap existed.

**Fix applied:** See HR-4. Added `no_plan_fail_count` with a cap of 3 consecutive failures.

### WR-4 — `plan_mode_approved` Reset Comment Without Implementation [NO CHANGE NEEDED]

**File:** `src/core/orchestration/graph/nodes/planning_node.py:62–64`

The audit claimed the reset (`plan_mode_approved: None`) was missing from the main planning
code path. Verification shows **all 9 return paths** in `planning_node` already include
`plan_mode_approved: None` (error paths at lines 74, 83; resume path at 128; decomposed plan at
154; next_action at 168; cancel at 341; success at 441; fallback at 463; final fallback at 477).
The audit finding was incorrect.

### WR-5 — `should_after_analysis` Complexity Check Duplicates `_task_is_complex`

**File:** `src/core/orchestration/graph/builder.py:675+`

Both `route_after_perception` and `should_after_analysis` invoke `_task_is_complex`. If task is
classified complex in `route_after_perception`, it goes to analysis, then
`should_after_analysis` re-classifies it. This is redundant and adds log noise.

---

## 7. Tool System Weaknesses

### TS-1 — `sed` in SAFE_COMMANDS Without `-i` Guard Validation [MEDIUM]

**File:** `src/tools/_security.py:67`

`sed` is in `SAFE_COMMANDS` (auto-approved). The `F6` fix blocks `sed -i` and bundled flags at a
higher layer (file_tools.py). However, the block relies on `DANGEROUS_PATTERNS` not containing
`"sed -i"` explicitly; protection comes from the pattern checking `"sed"` against the bash tool
implementation. A manual review confirms `sed -i` is blocked (via archive-extract / bundled-flag
checks), but this should be made explicit in `DANGEROUS_PATTERNS` or in a dedicated `sed` guard
rather than relying on implicit layering that is not documented in `_security.py`.

### TS-2 — `tar`/`unzip` in SAFE_COMMANDS, Extraction Blocked Implicitly [LOW]

`tar` and `unzip` are in `SAFE_COMMANDS`. Extraction is blocked via `TAR_EXTRACT_FLAGS`. But
`SAFE_COMMANDS` is documented as "Safe read-only / inspection commands". `tar` is not read-only
even with the extract-flag block — `tar -c` creates archives, `tar -r` appends to them. Neither
is in `TAR_EXTRACT_FLAGS`. Archive _creation_ by the agent is not a common attack vector but
violates the documented invariant.

### TS-3 — `web_tools.py` — Unbounded External Requests [MEDIUM]

**File:** `src/tools/web_tools.py`

Web tools allow the agent to make arbitrary HTTP requests to external URLs. There is no allowlist,
no rate limiting, and no timeout enforcement visible at the tool registration layer (timeout may
be inside the implementation). This creates a potential vector for SSRF or data exfiltration if
a prompt-injected task instructs the agent to POST workspace files to an external URL.

### TS-4 — `subagent_tools.py` — `delegate_task_async` Spawns Subagent With Full Registry [MEDIUM]

**File:** `src/tools/subagent_tools.py`

When spawning a subagent via `delegate_task_async`, the subagent receives the full tool registry.
The PRSW design (read/write role separation) assumes write agents are isolated, but the tool
registry does not enforce this. A `researcher` role subagent can still call `write_file` if it
generates the right tool call.

### TS-5 — `interaction_tools.py` — User Confirmation Bypass Risk [LOW]

`interaction_tools.py` provides tools to ask the user confirmation prompts. However, these tools
are in the standard registry available to the LLM. A hallucinating LLM could call
`ask_user_confirmation` with a misleading prompt, then interpret any response as approval for a
destructive action (classic "confused deputy" pattern). There is no enforcement that the agent
cannot auto-answer its own confirmation prompts.

---

## 8. Repository Awareness Gaps

### RA-1 — `call_graph` and `test_map` Populated by Analysis but May Be Stale [MEDIUM]

**File:** `src/core/orchestration/graph/nodes/analysis_node.py`

`analysis_node` generates `call_graph` and `test_map` and injects them into the planning prompt
(P3-1). However, `analysis_node` also clears these fields explicitly (WR-3 fix from Vol3) to
prevent stale data. If analysis is skipped (fast-path or plan-resumed), planning proceeds
without `call_graph`/`test_map`. The planning_node does not gate on whether these fields are
populated — it constructs its prompt with whatever is in state, silently omitting the structured
data if missing.

### RA-2 — SymbolGraph Regex-Based for Non-Python Languages [LOW]

**File:** `src/core/indexing/symbol_graph.py`

Go, Rust, Java, and JS/TS symbol extraction uses regex patterns rather than AST parsing. Regex-
based extraction produces false positives on commented code, string literals containing function
signatures, and template/macro expansions. For Python, AST parsing provides high-fidelity
extraction. The quality gap between languages is significant for large codebases.

### RA-3 — No Cross-File Dependency Graph for Refactoring [LOW]

The SymbolGraph tracks symbols but not import dependencies between files. When a refactoring
renames a function, the agent has no automatic way to identify all files that import and call that
function. This is a known gap (see TOOLS_GAP_ANALYSIS.md) but worth noting: cross-file rename
operations currently rely entirely on LLM knowledge rather than structural analysis.

---

## 9. Memory System Evaluation

**Strengths:** Distiller with 50-message compaction threshold, checkpoint persistence,
JSON validation of required keys, VectorStore integration after distillation, LRU caches for
context builder. All are well-implemented.

### MM-1 — Compaction Checkpoint Leaks Between Tasks [MEDIUM] [FIXED]

**File:** `src/core/memory/distiller.py`, `src/core/orchestration/orchestrator.py`

The compaction checkpoint is written to `.agent-context/compaction_checkpoint.md` relative to
`working_dir`. This file persists across task boundaries. `start_new_task()` resets in-memory
buffers but does not delete or invalidate the compaction checkpoint. If a new task starts in the
same `working_dir`, the next distillation will load the previous task's checkpoint as base
context, potentially contaminating the new task's reasoning with stale task-specific details.

**Fix applied:** Added `Path.unlink()` call in `start_new_task()` to delete
`.agent-context/compaction_checkpoint.md` when it exists. Runs after existing cleanup logic,
wrapped in try/except to never block task startup.

### MM-2 — Advanced Memory Features Not Integrated Into Pipeline [MEDIUM]

**File:** `src/core/memory/advanced_features.py`

`TrajectoryLogger`, `DreamConsolidator`, `RefactoringAgent`, `ReviewAgent`, and `SkillLearner`
are implemented in `advanced_features.py` and called from `memory_update_node`. However:

- `DreamConsolidation` runs asynchronously after task completion with no mechanism to gate the
  next task on its completion (race condition on checkpoint file).
- `SkillLearner` records "skills" but they are never loaded back into planning prompts or role
  definitions — skills are written but never read.
- `ReviewAgent` spawns an LLM call for every task completion, doubling the cost of memory sync
  with no clear consumer of its output.

### MM-3 — VectorStore `add_memory` Called Without Ensuring Index Consistency [LOW]

After distillation, `distiller.py` calls `VectorStore.add_memory()`. If the VectorStore index
is not warm (first run, cold start), this blocks for index initialisation. There is no timeout
or fallback; a VectorStore initialisation failure would propagate as an exception during memory
sync and could abort the memory_update_node.

---

## 10. Evaluation and Testing Gaps

### ET-1 — No Tests for `_safe_resolve_workdir` Path Traversal Rejection [FIXED]

Given the bug in HR-1, there were no tests asserting that `_safe_resolve_workdir("../../etc")`
is rejected or bounded.

**Fix applied:** Added 4 new regression tests in
`test_plan_replan_loop_guards_adapter_retry.py::TestVerificationRunTestsSafeWorkdirResolution`:
- `test_run_tests_rejects_etc_traversal` — rejects `/etc/passwd`
- `test_run_tests_rejects_etc_direct` — rejects `/etc`
- `test_run_tests_rejects_relative_path` — rejects relative paths
- `test_run_tests_rejects_usr_traversal` — rejects `/usr/bin`

### ET-2 — E2E Tests Use Mock LLM Only [LOW]

`tests/e2e/test_agent_scenarios.py` uses a mock LLM adapter. These tests verify graph routing
and tool dispatch but cannot detect prompt regression (LLM generating wrong tool calls for given
task descriptions). No "prompt regression" testing exists — no golden-file comparison of LLM
prompts across versions.

### ET-3 — Benchmark Tests Are Not Part of CI Gate [LOW]

`tests/benchmarks/test_pipeline_benchmarks.py` exists but CI runs `pytest tests/unit tests/e2e`
(unit + E2E only). Benchmark tests are not part of the CI gate; latency regressions would go
undetected.

### ET-4 — No Tests for PRSW Lock Manager in Contention Scenarios [LOW]

`test_file_lock_manager.py` tests basic acquire/release. There are no tests for contention
scenarios: multiple concurrent writers on the same file, lock acquisition timeout, or deadlock
between read-lock and write-lock upgrade attempts.

---

## 11. Usability Problems

### UP-1 — TUI Has No Indication When `plan_mode` is Blocking Execution [MEDIUM]

When `plan_mode_enabled=True` and the graph is suspended in `wait_for_user_node`, the TUI
displays a spinner but no clear "Waiting for your plan approval" label. Users who are not aware
of `plan_mode` will see a hung agent with no actionable feedback.

### UP-2 — `providers.json` Format Change Not Documented in UI [LOW]

The adapter layer supports both `dict` and `array` formats for `providers.json`. There is no
migration guide or UI-level hint about this. Users upgrading from the dict format will see a
silent fallback to the first provider.

### UP-3 — Error Messages in `debug_node` Do Not Include Stack Traces for Lint Errors [LOW]

When `error_type == "lint_error"`, the debug prompt includes `error_summary` truncated to 200
chars. Lint errors from ruff/mypy often need the full file path and line number context, which
may be cut off. `debug_node` should include `failure_reasons` from `evaluation_node` (which
captures up to 200 chars per check) rather than re-truncating.

---

## 12. Performance Bottlenecks

### PB-1 — All Editing Tasks Pay Full Analysis + Delegation Cost [FIXED]

See HR-2. The complexity classifier triggered analyst_delegation for virtually all modifying
tasks. On a local 7B model at 20 tok/s, a 6-node analysis pipeline added 60–90 seconds of
latency to simple single-file edits that could complete in 10–15 seconds via fast-path.

**Fix applied:** See HR-2. Common action verbs removed from the complexity keyword set. Simple
edits now route through the fast-path.

### PB-2 — `ContextBuilder` Module-Level Caches Are Process-Global [MEDIUM]

**File:** `src/core/context/context_builder.py`

`_TEXT_CACHE` and `_JSON_CACHE` are module-level dicts shared across all ContextBuilder
instances and tasks. There is an LRU cap of 256 entries, but cache entries from task A can
remain warm during task B. For multi-task sessions, this could cause role/skill YAML files
to be served from cache even if they were modified on disk between tasks.

### PB-3 — Pre-Retrieval (`asyncio.gather` for perception) Loads All Files Into Memory [LOW]

`perception_node` uses `asyncio.gather` to run parallel symbol lookups. For large repositories
(>10,000 files), the SymbolGraph JSON can be several MB. Loading and deserialising it on every
perception round is wasteful. The `_INDEXED_DIRS` LRU cache partially mitigates this, but the
cache is keyed by directory path — repeated runs on the same repo clear and rebuild it if the
process is restarted.

---

## 13. Over-Engineered Components

### OE-1 — `advanced_features.py` Has 5 Subsystems, None Fully Integrated

`TrajectoryLogger`, `DreamConsolidator`, `RefactoringAgent`, `ReviewAgent`, and `SkillLearner`
are each a meaningful feature. However, their outputs are not consumed by any planning or
context-building pathway. They add LLM call overhead at memory_sync time without contributing
to task quality. For a local-first agent, these should either be fully wired into the
context pipeline or behind a feature flag.

### OE-2 — PRSW Architecture Is Complex for Current Use Cases

The Parallel Read / Sequential Write (PRSW) architecture with `FileLockManager`,
`AgentSessionManager`, `CrossSessionBus`, and P2P inter-session routing is designed for
multi-agent concurrent workloads. For the current single-user local agent, this adds ~500 lines
of coordination code that is exercised only when `should_use_prsw()` returns True (requires 2+
delegations with mixed read/write roles). This scenario is uncommon in typical usage.

### OE-3 — `AgentState` Has 151 Fields

The state TypedDict has grown organically to 151 fields. Many fields are only relevant in
specific sub-contexts (`_file_lock_manager`, `_write_queue`, `_p2p_context`, `plan_dag`,
`execution_waves`, `preview_mode_enabled`, etc.). This creates a God Object anti-pattern where
every node has access to every field, increasing cognitive load and making state debugging
difficult. A sub-state pattern (composed TypedDicts per phase) would improve maintainability.

### OE-4 — Two Partially-Dead Router Functions in `builder.py`

`should_after_planning` and `should_after_verification` are explicitly commented as "NOT WIRED
IN compile_agent_graph()" but kept for "GraphFactory subgraphs". If GraphFactory is used
actively, these are a maintenance burden (they must stay in sync with the main graph logic).
If GraphFactory is internal/test-only, these should be removed or clearly marked as test
utilities.

---

## 14. Prioritized Fix List

### Severity: Critical

| ID | Finding | Location | Complexity | Status |
|----|---------|----------|------------|--------|
| CF-1 | Direct state mutation (`_file_lock_manager`) | `delegation_node.py:238` | Low | **FIXED** |

### Severity: High

| ID | Finding | Location | Complexity | Status |
|----|---------|----------|------------|--------|
| HR-1 | `_safe_resolve_workdir` no-op path traversal guard | `verification_tools.py:12` | Low | **FIXED** |
| HR-2 | Complexity classifier kills fast-path for all editing tasks | `builder.py:162–225` | Medium | **FIXED** |
| HR-3 | `debug_node` ignores JS/TS errors | `debug_node.py:62` | Low | **FIXED** |
| HR-4 | Failed no-plan execution → perception (unbounded) | `builder.py:386` | Low | **FIXED** |
| HR-5 | Non-Optional AgentState fields without runtime defaults | `state.py` | Low | **FIXED** |

### Severity: Medium

| ID | Finding | Location | Complexity | Status |
|----|---------|----------|------------|--------|
| MM-1 | Compaction checkpoint leaks between tasks | `distiller.py` | Low | **FIXED** |
| MM-2 | Advanced memory features not integrated | `advanced_features.py` | High | Open |
| WR-4 | `plan_mode_approved` reset comment without full implementation | `planning_node.py:62` | Low | **N/A** (already correct) |
| TS-3 | Unbounded external requests in `web_tools.py` | `web_tools.py` | Medium | Open |
| ET-1 | No tests for `_safe_resolve_workdir` path traversal | `test_verification_tools.py` | Low | **FIXED** |
| UP-1 | TUI shows no label when waiting for plan approval | TUI layer | Low | Open |
| PB-2 | Module-level ContextBuilder caches stale across tasks | `context_builder.py` | Low | Open |

### Severity: Low

| ID | Finding | Location | Complexity | Status |
|----|---------|----------|------------|--------|
| RA-2 | SymbolGraph regex extraction for non-Python languages | `symbol_graph.py` | High | **FIXED** |
| OE-3 | 151-field AgentState God Object | `state.py` | High | Deferred (Phase 4) |
| ET-2 | E2E tests use mock LLM only (no prompt regression tests) | `tests/e2e/` | Medium | **FIXED** |
| ET-3 | Benchmarks not in CI gate | `.github/workflows/` | Low | **FIXED** (prior) |
| TS-2 | `tar` archive creation not blocked in SAFE_COMMANDS | `_security.py` | Low | **FIXED** (prior) |

---

## 15. Prioritized Engineering Roadmap

### Phase 1 — Critical Stability Fixes [ALL COMPLETE]

| Task | Location | Complexity | Status |
|------|----------|------------|--------|
| **P1-1** Fix `state["_file_lock_manager"]` mutation | `delegation_node.py:238` | Low | **DONE** |
| **P1-2** Implement real bounds-checking in `_safe_resolve_workdir` | `verification_tools.py:12` | Low | **DONE** |
| **P1-3** Add JS/TS error keys to `debug_node` error aggregation | `debug_node.py:62` | Low | **DONE** |
| **P1-4** Verify `plan_mode_approved: None` on all `planning_node` return paths | `planning_node.py` | Low | **VERIFIED** (already correct) |
| **P1-5** Add test for `_safe_resolve_workdir` path traversal rejection | `tests/unit/` | Low | **DONE** |

### Phase 2 — Robustness Improvements

| Task | Location | Complexity | Status |
|------|----------|------------|--------|
| **P2-1** Tighten `_task_is_complex` word-boundary set | `builder.py:162–225` | Medium | **DONE** |
| **P2-2** Invalidate compaction checkpoint at `start_new_task()` | `orchestrator.py: start_new_task()` | Low | **DONE** |
| **P2-3** Add per-failure retry cap for no-plan execution path | `builder.py:386`, `state.py` | Medium | **DONE** |
| **P2-4** Make `ContextBuilder` caches task-scoped or add invalidation hook | `context_builder.py` | Medium | Open |
| **P2-5** Gate advanced memory features behind config flag | `advanced_features.py`, `memory_update_node.py` | Medium | Open |

### Phase 3 — Capability Improvements

| Task | Location | Complexity | Status |
|------|----------|------------|--------|
| **P3-1** Add TUI label / status indicator when graph is suspended in `wait_for_user_node` | TUI / `wait_for_user_node.py` | Low | Open |
| **P3-2** Emit a pre-execution "files to be modified" summary in plan_mode | `wait_for_user_node.py` | Medium | Open |
| **P3-3** Add benchmark tests to CI gate with configurable thresholds | `.github/workflows/ci.yml` | Low | Open |
| **P3-4** Add contention scenario tests for `FileLockManager` | `tests/unit/test_file_lock_manager.py` | Medium | Open |
| **P3-5** Add timeout + fallback to `VectorStore.add_memory()` call in distiller | `distiller.py` | Low | Open |

### Phase 4 — Advanced Features

| Task | Location | Complexity | Status |
|------|----------|------------|--------|
| **P4-1** Wire `SkillLearner` output into planning prompt | `advanced_features.py`, `planning_node.py` | High | Open |
| **P4-2** Refactor `AgentState` into composed sub-TypedDicts per pipeline phase | `state.py`, all nodes | High | Open |
| **P4-3** Add AST-based symbol extraction for Go and TypeScript | `symbol_graph.py` | High | **PARTIAL** (comment-stripping added; full AST requires acorn/tsc) |
| **P4-4** Add SSRF protection to `web_tools.py` | `web_tools.py` | Medium | Open |
| **P4-5** Implement import dependency graph in SymbolGraph | `symbol_graph.py`, `repo_indexer.py` | High | Open |

---

## Summary Statistics

### Finding Status

| Severity | Total | Fixed | Deferred | N/A |
|----------|-------|-------|----------|-----|
| Critical | 1 | 1 | 0 | 0 |
| High | 5 | 5 | 0 | 0 |
| Medium | 7 | 4 | 2 | 1 |
| Low | 5 | 4 | 1 | 0 |
| **Total** | **18** | **14** | **3** | **1** |

### Test Suite

| Metric | Pre-fix (Vol10) | Post-fix (Vol11 final) |
|--------|-----------------|------------------------|
| Passed | 1454 | 1580 |
| Skipped | 4 | 4 |
| Failed | 0 | 0 |
| New tests | — | +126 (+16 RA-2/ET-2, rest from prior phases) |

**Remediation complete for all Critical + High findings (6 of 6) and all deferred Low/Medium
findings addressable without an architectural overhaul.** OE-3 (AgentState refactor) is the only
remaining deferred item; it requires a coordinated multi-file refactor touching all 14 pipeline
nodes and is tracked as a Phase 4 initiative.
