# Comprehensive Engineering Audit: Tools & Graph Workflows Subsystem

**Date:** 2026-11-03  
**Auditor:** Antigravity Autonomous Systems Engineering Team  
**Scope:** Tool execution pipeline ([tool_execution_pipeline.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py)), tool primitives ([src/tools/](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/)), sandbox and security gates ([sandbox.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/sandbox.py), [bash_security.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/bash_security.py), [_bash_exec.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_bash_exec.py), [WorkspaceGuard](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_workspace_guard.py)), unified 6-node LangGraph cognitive workflows ([src/core/orchestration/graph/](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/)), evidence-based completion contracts, and deferred-rollback transaction settlement.  
**Methodology:** Full-spectrum static analysis, sandbox boundary verification, AST patch simulation, cognitive routing edge analysis, and fail-closed settlement validation against [audit-instructions.md](file:///Users/tann200/PycharmProjects/CodingAgent/docs/audit/audit-instructions.md).

---

## 1. Executive Summary

CodingAgent employs an advanced cognitive architecture centered around a unified 6-node deterministic LangGraph topology (`initialize -> agent_loop -> [approval | verify | compact | finalize] -> END`), supported by centralized tool execution pipelines, AST-level shell security analysis, multi-tier evidence contracts, and deferred rollback snapshotting.

However, a deep engineering audit of the execution and graph layers reveals **critical safety loopholes, broken language runtime gates, and dead-ended approval paths**:

1. **Hard-Blocking of All TypeScript Mutations:** In [tool_execution_pipeline.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L651-L700), pre-write sandbox syntax validation checks `.ts` and `.tsx` files by writing content to a temporary file forced to extension `.js` and running `node --check`. Node treats the code as standard JavaScript and fails on valid TypeScript syntax (interfaces, types, generics, enums), triggering a fail-closed write abort. **The agent cannot write or edit any valid TypeScript file.**
2. **Cancellation Transaction Settlement Leak:** When a task is cancelled by the operator, [verification_node.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/verification_node.py#L210-L215) returns `{"verification_passed": True, "verification_result": {"cancelled": True}}`. When execution reaches [finalize_node.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/finalize_node.py#L61-L70), the transaction settlement guard checks `can_commit = state.get("verification_passed") is True`. Because this evaluates to `True`, the open snapshot is **committed to disk**, permanently persisting unverified, partial, and cancelled code mutations.
3. **Dead-Ended Approval Gate for Dangerous Shell Commands:** In [src/tools/_bash_exec.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_bash_exec.py#L547-L551), Gate 2 security analysis immediately hard-blocks both `BashRiskLevel.BLOCKED` and `BashRiskLevel.DANGEROUS`. Legitimate commands requiring user consent (e.g. `pip install`, `npm install`, `cargo install`, `curl`) are rejected outright with an error before ever reaching the downstream approval gate (`requires_approval: True`), making autonomous package installation or operator-approved network fetch impossible.
4. **AST Pre-Write Validation Bypass in `edit_file_atomic`:** The pipeline's pre-write AST validator only inspects `args.get("content")`. Because `edit_file_atomic` accepts `old_string` and `new_string`, `new_content` evaluates to `None`, bypassing pre-write syntax validation entirely and allowing syntax-corrupting edits to reach disk.
5. **Legacy Bus Disconnect in Diff Preview Gates:** Mutating tools check `get_event_bus().has_subscribers("file.diff.preview")` on the legacy untyped bus. Because the TUI bridge subscribes to typed `FileDiffPreview` on the `MessageBus`, the check always returns `False`, completely bypassing the preview confirmation gate even when configured as mandatory.

**Deployment Verdict:** Production-blocked for polyglot (TypeScript/Node) development and unsafe under operator cancellation. Immediate remediation of Phase 1 critical defects is mandatory before autonomous operation is permitted.

---

## 2. Architecture Strengths

- **Unified 6-Node Graph Pipeline:** Elimination of divergent lite/frontier compilers in favor of a single canonical graph topology ([canonical_routing.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/canonical_routing.py)) enforces deterministic state machine transitions across all LLM tiers.
- **Centralized Tool Execution Pipeline:** All model tool calls route through [tool_execution_pipeline.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py), composing permission checks, workspace confinement, dry-run evaluation, tool contracts, hooks, and execution timeouts.
- **AST-Level Bash Security Analysis:** Three-pass shell security analyzer ([bash_security.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/bash_security.py)) parses commands into canonical AST tokens to detect fork bombs, obfuscated pipe-to-shell injections, and destructive flags.
- **Deferred Rollback Transactions:** Mutating operations begin scoped step transactions ([_step_snapshot_id](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/frontier_loop_node.py#L342)) that defer disk commit until polyglot verification succeeds.
- **Evidence-Based Completion Contracts:** Completion evaluation ([completion_contract.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/completion_contract.py)) classifies user intent into informational, read-only, and mutating tasks, preventing the agent from claiming completion without verifiable tool evidence.

---

## 3. Critical Architectural Flaws

| Severity | Finding | Evidence | Trigger and Impact | Fix and Regression Test |
| :--- | :--- | :--- | :--- | :--- |
| **Critical** | Sandbox syntax validation hard-blocks valid TypeScript writes by forcing `.js` temporary files and running plain `node --check`. | [src/core/orchestration/tool_execution_pipeline.py:651-700](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L651-L700) | Agent calls `write_file("src/types.ts", content="interface User { id: string; }")`. Pipeline creates temp file with `suffix=".js"` and runs `node --check`. Node treats it as JS and fails on `interface` (`SyntaxError: Unexpected token 'interface'`). Pipeline aborts write with error. Valid TypeScript files cannot be written or modified. | Retain original file extension (`suffix=Path(path_arg).suffix`); use `tsc --noEmit` or Node `--experimental-strip-types` for `.ts`/`.tsx`, or delegate to `src.tools.lint_dispatch.quick_lint`. Test: `tests/unit/test_tool_pipeline_typescript_validation.py`. |
| **Critical** | Cancellation in `verification_node` reports `verification_passed=True`, causing `finalize_node` to commit unverified mutations to disk. | [src/core/orchestration/graph/nodes/verification_node.py:214,231,237](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/verification_node.py#L214); [src/core/orchestration/graph/nodes/finalize_node.py:61-70](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/finalize_node.py#L61-L70) | User cancels an in-flight mutating task. `verification_node` detects `_is_cancelled()` and returns `{"verification_passed": True, "verification_result": {"cancelled": True}}`. In `finalize_node`, `can_commit = state.get("verification_passed") is True` evaluates to `True`. `commit_step_transaction()` executes, committing incomplete, broken code to disk. | Return `verification_passed: False` when cancelled in `verification_node.py`. In `finalize_node.py`, enforce `run_state.get("status") not in ("cancelled", "failed")` in `can_commit`. Test: `tests/unit/test_settlement_guard_on_cancellation.py`. |
| **High** | Gate 2 in `_bash_exec.py` hard-blocks `BashRiskLevel.DANGEROUS` commands, permanently dead-ending operator approval for package installation. | [src/tools/_bash_exec.py:544-551,566-577](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_bash_exec.py#L544-L551) | Model runs `npm install express` or `pip install pytest`. `analyze_bash_command` classifies it as `BashRiskLevel.DANGEROUS`. Line 547 checks `if _risk_level in (BashRiskLevel.BLOCKED, BashRiskLevel.DANGEROUS): return {"status": "error"}`. The call is rejected immediately; lines 566-577 (`requires_approval: True`) are never reached. Operator is never prompted for approval. | Restrict Gate 2 hard-block strictly to `BashRiskLevel.BLOCKED`. For `BashRiskLevel.DANGEROUS`, return `{"status": "error", "requires_approval": True, "risk_reasons": _risk_reasons}`. Test: `tests/unit/test_bash_dangerous_approval_flow.py`. |
| **High** | `edit_file_atomic` bypasses pre-write AST syntax validation. | [src/core/orchestration/tool_execution_pipeline.py:635-640](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L635-L640); [src/tools/_edit_tools.py:476-560](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_edit_tools.py#L476-L560) | Model calls `edit_file_atomic(path="a.py", old_string="...", new_string="broken syntax ((")`. Pipeline checks `new_content = args.get("content")`. For `edit_file_atomic`, args are `old_string` and `new_string`, so `new_content` is `None`. Pre-write AST validation is bypassed. | If `name == "edit_file_atomic"`, load file and compute candidate text `original.replace(old, new, 1)` to validate prior to execution. Test: `tests/unit/test_edit_file_atomic_prewrite_ast_validation.py`. |
| **High** | Legacy `EventBus` subscriber check bypasses diff preview gate when TUI uses typed `MessageBus`. | [src/tools/_edit_tools.py:583-585](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_edit_tools.py#L583-L585); [src/tools/_file_io.py:257-259](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_file_io.py#L257-L259) | Config sets `preview_confirmation: true`. Tools check `_get_bus().has_subscribers("file.diff.preview")`. The bridge subscribes to typed `FileDiffPreview` on `MessageBus`. The legacy check evaluates to `False`. The tool skips the preview gate and writes immediately. | Check subscriber registration across both legacy `EventBus` and typed `MessageBus` for `FileDiffPreview`. Test: `tests/unit/test_diff_gate_message_bus_subscriber_detection.py`. |
| **High** | Mid-run in-flight steering buffer is ignored throughout `frontier_loop_node` execution. | [src/core/orchestration/graph/nodes/frontier_loop_node.py:528-720](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/frontier_loop_node.py#L528-L720); [src/core/orchestration/graph/state.py:400](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/state.py#L400) | `_pending_injections_source` is declared in `AgentState` and populated by `inference_loop_state.py`, but `frontier_loop_node.py` never checks or pops it. In-flight operator prompts are ignored while the loop runs. | Poll `_pending_injections_source.pop_pending_injections()` at the start of each turn in `frontier_loop_node.py` and inject into `messages` / `history`. Test: `tests/unit/test_frontier_loop_mid_run_injection.py`. |

---

## 4. High-Risk Safety Issues

| Severity | Finding | Evidence | Trigger and Impact | Fix and Regression Test |
| :--- | :--- | :--- | :--- | :--- |
| **High** | Thread event loop destruction in `_dispatch_tool_call` causes thread-local task corruption. | [src/core/orchestration/tool_execution_pipeline.py:868-885](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L868-L885) | An awaitable tool function is invoked from a worker thread with an active running event loop. Pipeline creates `new_loop = asyncio.new_event_loop()`, sets it as the thread event loop, and calls `new_loop.close()`. The original thread event loop is destroyed, corrupting concurrent async operations. | Use `asyncio.run_coroutine_threadsafe(rv, loop)` when a running loop exists; never overwrite and close an active thread loop. Test: `tests/unit/test_tool_pipeline_thread_event_loop_safety.py`. |
| **Medium** | Path traversal normalization gap in `WorkspaceGuard` prefix validation on macOS `/var` -> `/private/var`. | [src/tools/_workspace_guard.py:65-80](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_workspace_guard.py#L65-L80) | On macOS, temporary directories can be lexically rooted under `/var/folders` while resolved paths are under `/private/var/folders`. `WorkspaceGuard` checks `str(resolved).startswith(str(self.workspace_root))`. If root is not strictly resolved, valid workspace writes are rejected or escaped paths permitted. | Ensure `self.workspace_root = Path(workspace_root).resolve()` is always strictly canonicalized before prefix matching. Test: `tests/unit/test_workspace_guard_macos_var_symlinks.py`. |

---

## 5. Major Missing Capabilities

1. **Pre-Write Polyglot Linter Integration:** `lint_dispatch.py` contains solid syntax checkers for Python, Go, and Rust, but `tool_execution_pipeline.py` relies on a broken, ad-hoc Node check instead of using the unified `quick_lint` dispatcher.
2. **Dynamic Targeted Verification Scoping:** When modifying files in a large monolithic repository, `verification_node` defaults to running the entire workspace test suite (`run_js_tests`, `run_tests`) unless `verification_mode == "targeted"`, leading to multi-minute timeouts.
3. **Structured Mini-Plan DAG State Transitions:** The graph supports wave execution for parallel read-only tools, but mutating tools are strictly executed serially without support for independent file transaction DAGs.
4. **Adaptive Context Pruning at Step Boundaries:** `compact_node.py` triggers only upon `context_overflow`. It lacks proactive sliding-window eviction of large read outputs before LLM inference requests.

---

## 6. Workflow Reliability Issues

| Severity | Finding | Evidence | Trigger and Impact | Fix and Regression Test |
| :--- | :--- | :--- | :--- | :--- |
| **Medium** | Polyglot verification heuristic blindly executes full suite on large repositories. | [src/core/orchestration/graph/nodes/verification_node.py:225-240](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/verification_node.py#L225-L240) | In a JS/TS project, modifying a single CSS or markdown file triggers `run_js_tests` and `run_ts_check` across the entire project if `verification_mode != "targeted"`. Unrelated test suite failures fail the transaction settlement guard. | Dynamically detect whether modified files contain executable test/source code; default to targeted verification when modified file count is small. Test: `tests/unit/test_verification_node_targeted_heuristics.py`. |
| **Medium** | `approval_node` lacks automatic digest recalculation on argument normalization. | [src/core/orchestration/graph/nodes/approval_node.py:54-73](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/approval_node.py#L54-L73) | If an approved action dictionary has arguments subtly re-ordered or typed during serialization (e.g. integer to string), `compute_action_digest` fails with digest mismatch, erroneously cancelling the user-approved action. | Canonicalize argument dictionaries (sort keys, stringify values) before computing action digests. Test: `tests/unit/test_approval_digest_canonicalization.py`. |

---

## 7. Tool System Weaknesses

- **Disjointed Preflight Validation:** Tools are validated twice with divergent schemas: first via Pydantic tool contracts ([tool_contracts.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_contracts.py)) in the pipeline, and second inside tool function bodies ([WorkspaceGuard](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_workspace_guard.py)). A failure in the second check does not trigger contract retry nudges.
- **`batch()` Tool Output Truncation:** Concurrent read-only outputs from `batch()` are combined into a single payload without per-call character budgets, risking context overflow when multiple large files are read simultaneously.

---

## 8. Repository Awareness Gaps

- **Symbol Graph Disconnect in Planning:** Symbol graphs generated during `analysis_node` are stored in `state["call_graph"]` and `state["test_map"]`, but `frontier_loop_node` only injects `state["analyst_findings"]` and `repo_context`, omitting symbol relationship maps from the model prompt.
- **Unindexed File Modifications:** Files created or edited during a multi-turn run are not incrementally added to the vector store until task finalization, leaving semantic search unaware of newly added files.

---

## 9. Memory System Evaluation

- **Context Spikes from Unpruned Tool Errors:** When a tool call fails with a 50-line traceback, the full raw error string is appended to `history`. If repeated 3 times during retry attempts, error traces consume up to 15% of the token context.
- **Deterministic Compaction Priority:** Under high token pressure, `compact_node.py` correctly prunes old messages deterministically, but does not summarize earlier tool responses into condensed state facts.

---

## 10. Evaluation and Testing Gaps

- **Absence of TypeScript Edit Pipeline Tests:** Existing test suites contain extensive tests for Python syntax validation, but zero tests asserting that `.ts` and `.tsx` file modifications successfully pass through `tool_execution_pipeline.py`.
- **Untested Cancellation Settlement Path:** No integration tests exist asserting that cancelling a run rolls back open transactions without committing unverified changes to disk.

---

## 11. Usability Problems

- **Cryptic Security Analysis Errors:** When Gate 2 blocks a command, it returns: `Command blocked by security analysis: pattern match`. It does not inform the operator how to request approval or reformat the command safely.
- **Missing Partial Edit Recovery Feedback:** When `edit_file_atomic` fails because `old_string` was not found, the returned error suggests fuzzy strategies but does not provide line numbers of closest matches.

---

## 12. Performance Bottlenecks

- **Redundant AST Parsing in Pre-Write Gates:** In `write_file`, AST validation runs once in `_verify_write_candidate` via `quick_lint`, and a second time in `tool_execution_pipeline.py` via `_run_sandbox_and_snapshot`, doubling CPU overhead for every write.
- **Synchronous Subprocess Spawning in Shell Gate Analysis:** `analyze_bash_command` executes regexes efficiently, but downstream linter tools spawn synchronous `subprocess.run` instances sequentially instead of leveraging worker pools.

---

## 13. Over-Engineered Components

- **Redundant Sandbox Suffix Transformation:** The logic in `_run_sandbox_and_snapshot` attempting to convert polyglot code to temporary `.js` files is unnecessary given that `quick_lint` already provides specialized, tested linters.
- **Triple-Checked Read-Before-Write Guards:** Read-before-write checks run in `tool_execution_pipeline.py`, in `_edit_tools.py`, and in `guardrails.py` with separate lock states and path caches.

---

## 14. Prioritized Fix List & Engineering Roadmap

### Phase 1 — Critical Stability Fixes (Immediate Remediation)
1. **Fix TypeScript Sandbox Validation Failure:**
   - **Description:** Remove the hardcoded `.js` temporary file conversion and raw `node --check` call in `tool_execution_pipeline.py`. Retain the actual file extension and delegate syntax validation to `quick_lint` (which correctly uses `tsc --noEmit`).
   - **Location:** [src/core/orchestration/tool_execution_pipeline.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L651-L700)
   - **Complexity:** Low | **Impact:** High
2. **Fix Cancellation Settlement Guard Commit Leak:**
   - **Description:** Update `verification_node.py` to return `verification_passed: False` when cancelled. In `finalize_node.py`, strictly check that `run_state.get("status") not in ("cancelled", "failed")` before permitting `commit_step_transaction()`.
   - **Location:** [src/core/orchestration/graph/nodes/verification_node.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/verification_node.py#L210-L238), [src/core/orchestration/graph/nodes/finalize_node.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/finalize_node.py#L61-L70)
   - **Complexity:** Low | **Impact:** High
3. **Unblock Gate 2 Approval Flow for Dangerous Shell Commands:**
   - **Description:** Modify Gate 2 in `_bash_exec.py` to hard-block only `BashRiskLevel.BLOCKED`. Return `requires_approval: True` for `BashRiskLevel.DANGEROUS` so package management and remote fetch commands can be approved by the operator.
   - **Location:** [src/tools/_bash_exec.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_bash_exec.py#L544-L551)
   - **Complexity:** Low | **Impact:** High
4. **Implement Pre-Write AST Validation for `edit_file_atomic`:**
   - **Description:** Compute proposed replacement content for `edit_file_atomic` in `_run_sandbox_and_snapshot` so surgical string replacements undergo pre-write AST validation.
   - **Location:** [src/core/orchestration/tool_execution_pipeline.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L634-L640)
   - **Complexity:** Low | **Impact:** High

### Phase 2 — Robustness & Safety Improvements
5. **Connect Diff Preview Gate to MessageBus Subscribers:**
   - **Description:** Update `_edit_tools.py` and `_file_io.py` to verify subscriber existence across both `EventBus` and `MessageBus` so diff preview confirmation functions as intended.
   - **Location:** [src/tools/_edit_tools.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_edit_tools.py#L583), [src/tools/_file_io.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_file_io.py#L257)
   - **Complexity:** Low | **Impact:** Medium
6. **Drain Mid-Run Injections in `frontier_loop_node`:**
   - **Description:** Poll `_pending_injections_source` at turn boundaries in `frontier_loop_node.py` and inject buffered messages into context.
   - **Location:** [src/core/orchestration/graph/nodes/frontier_loop_node.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/frontier_loop_node.py#L528-L538)
   - **Complexity:** Low | **Impact:** High
7. **Protect Running Thread Event Loops in `_dispatch_tool_call`:**
   - **Description:** Safely dispatch coroutines using `asyncio.run_coroutine_threadsafe` instead of overwriting and closing active thread loops.
   - **Location:** [src/core/orchestration/tool_execution_pipeline.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L868-L885)
   - **Complexity:** Low | **Impact:** Medium

### Phase 3 — Capability Improvements
8. **Intelligent Targeted Verification Scoping:**
   - **Description:** Restrict full test suite runs in `verification_node` to when non-test source files or multiple modules are touched; run targeted test files for localized edits.
   - **Location:** [src/core/orchestration/graph/nodes/verification_node.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/verification_node.py#L225-L245)
   - **Complexity:** Medium | **Impact:** Medium
9. **Deduplicate Syntax Validation Passes:**
   - **Description:** Consolidate pre-write validation into a single execution pass using `quick_lint`, removing redundant parsing in `write_file`.
   - **Location:** [src/tools/_file_io.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_file_io.py#L240), [src/core/orchestration/tool_execution_pipeline.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L634-L700)
   - **Complexity:** Medium | **Impact:** Low

### Phase 4 — Long-Term Architecture
10. **Independent Transaction Settlement DAGs:**
    - **Description:** Expand `RollbackManager` to support multi-file transaction graphs where independent file edits can be verified and committed separately from failing sibling edits.
    - **Location:** `src/tools/rollback_manager.py` and `src/core/orchestration/graph/nodes/finalize_node.py`
    - **Complexity:** High | **Impact:** High
11. **Comprehensive TypeScript End-to-End Test Suite:**
    - **Description:** Add unit and integration tests verifying TypeScript project creation, interface edits, type checking, and transaction rollbacks.
    - **Location:** `tests/integration/test_typescript_workflow.py`
    - **Complexity:** Medium | **Impact:** High
