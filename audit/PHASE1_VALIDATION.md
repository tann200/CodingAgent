# Phase 1 Implementation-Readiness Validation

**Scope:** Validate that each Phase 1 item in the audit report has enough detail to begin implementation (correct file, precise line numbers, confirmed root cause, clear fix intent, and test approach).
**Method:** Re-read every Phase 1 referenced source file to verify the audit claims at line level.

---

## Item 1.1 — Flip Gate 2c to fail-closed
**Status: ✅ SUFFICIENT DETAIL — READY**

**Verified at line level:**
- `permission_gateway.py:469-474` contains the fail-open handler:
  - Line 469: `except Exception as _e:`
  - Line 470-471: warning log `"Gate 2c permission policy check failed (fail-open)"`
  - Line 474: `return PermissionResult(allowed=True)` ← the bug
- The `combined_check` at line 445 is where DENY/ASK decisions occur; DENY path (lines 447-460) and ASK path (lines 462-467) already construct proper `PermissionResult` objects with `gate=`, `reason=`, `rejection=` fields — these are the exact patterns to reuse in the fail-closed branch.
- The correct `PermissionResult(allowed=False, gate=2, ...)` pattern is already demonstrated in the DENY branch (lines 448-460).

**Fix is unambiguous:** Replace line 474 `return PermissionResult(allowed=True)` with a fail-closed DENY result mirroring lines 448-460. No additional design decisions needed.

**Implementation steps available:**
1. Change return at line 474 to `PermissionResult(allowed=False, gate=2, reason="permission policy check failed (fail-closed)", rejection={...})`
2. Update the log message text (remove "fail-open").
3. Existing tests exercise the DENY path structure — search `test_permission` for `allowed=False` patterns to mirror.

**Test approach:**
- Unit test that stubs the policy to raise an exception and asserts `allowed=False`.
- Check `_gate2c_permission_policy` (the method containing this) for the test class/name. Grep `test_permission_gateway.py` for existing Gate 2 tests to extend.

**Blocker: None.**

---

## Item 1.2 — Fix bash timeout status "ok"
**Status: ⚠️ DETAIL MOSTLY SUFFICIENT — ONE DESIGN DECISION NEEDED**

**Verified at line level:**
- `_bash_exec.py:545-562`: the `except subprocess.TimeoutExpired` handler.
  - Lines 553-562 build the return dict with `"status": "ok"` and `"returncode": -1`.
  - Line 553: `_raw_stdout, _raw_stderr, _, _ = _truncate_bash_output(...)`.
  - Line 560: `"return_code_interpretation": "timeout"`.

**Confirmed downstream consumer dependency (critical for the fix):**
- Line 559-560 keeps `returncode: -1` and adds `"return_code_interpretation": "timeout"`. Many nodes/verification check `result.get("status") == "ok"` (per previous audit: `_is_success`/`execution_ok` in `execution_routing.py:25`, `perception_routing.py:209`). **Changing `"status"` to `"error"` may break these consumers** — need to know whether they also accept `returncode: -1` + `interrupted: true` as an error signal.

**GAP — implementation needs one clarification:**
- Decision: (A) flip `"status"` to `"error"` globally (semantically correct, but check all `status=="ok"` consumers won't mis-handle), OR (B) keep `status:"ok"` but add a distinct `"timeout": True` marker and update consumers. The audit report states only the problem, not the chosen resolution.

**Recommended resolution (ready to implement):**
- Change line 554 to `"status": "error"` (semantically correct), add `"error": "command timed out"`, and re-verify the two `_is_success` consumers. Add a contract test asserting timeout ⇒ `status != "ok"`.

**Blocker: one design choice (A vs B) — otherwise implementable now.**

---

## Item 1.3 — Fix WorkspaceGuard no-op fallback
**Status: ✅ SUFFICIENT DETAIL — READY**

**Verified at line level:**
- `_workspace_guard.py:12-26`:
  - Line 12-17: no-op `WorkspaceGuard.guard_operation` always returns `{"status": "ok"}`.
  - Line 20-26: `try: from src.core.orchestration.workspace_guard ... except ImportError:` falls back to the no-op.
- The real guard at `src/core/orchestration/workspace_guard.py:81-111` returns `{"status": "error", "requires_approval": True}` for unapproved protected files — **fail-closed**. The shim violates this contract.

**Fix is unambiguous:**
- The no-op fallback should **fail closed** (return `{"status": "error", ...}`) or **raise**, not return `ok`. 
- However, there is a **nuance to confirm**: the shim exists specifically for "when the core module is unavailable (e.g., during testing)". Failing closed here could break large numbers of tool tests that don't import `src.core`. 

**Implementation decision needed (low risk):**
- Preferred: make the fallback raise `ImportError` loudly OR return `{"status": "error", "error": "WorkspaceGuard unavailable, security checks bypassed"}` and update tests to expect the guarded behavior / mock the guard. This is a behavioral change to the testing path.

**Blocker: minor test-impact consideration only; the fix direction is clear.**

---

## Item 1.4 — Fix VectorStore add_memory/search_memories
**Status: ⚠️ DETAIL PARTIALLY SUFFICIENT — NEEDS DESIGN SPEC**

**Verified at line level:**
- `vector_store.py:302-307`:
  - Line 302-303: `add_memory()` logs and returns (no-op).
  - Line 305-307: `search_memories()` returns `[]`.
- `distiller.py:620-634` calls `_vs.add_memory(_summary_text, metadata=distilled_state)` and logs "summary persisted to VectorStore" — but nothing is stored.
- `distill_context` returns `_compacted_history` (line 638-639) and `distilled_state` — the metadata used for VectorStore is available.

**GAP — this is the LEAST implementation-ready item.** The report identifies the bug but does not specify:
1. **Storage backend** — Where should memories be persisted? The class already has a semantic search over in-memory `symbols` (vector_store.py:236-281). Is cross-session memory to be persisted to disk (new file/SQLite), or to the existing session store? No target location is specified.
2. **Schema** — What does a stored "memory" record look like (text + metadata dict)? Unspecified.
3. **Embedding** — Should memories be embedded with `all-MiniLM-L6-v2` (for real semantic recall) or stored text-only? Unspecified.
4. **Search semantics** — Should `search_memories` do cosine similarity over embedded memories? Unspecified.
5. **Scope** — Per-workspace? Per-project? Global? Cross-session retrieval already exists via `retrieve_relevant_prior_sessions` in distiller.py:725 (using SessionStore) — so what role does VectorStore memory play that this doesn't cover?

**Readiness verdict: NOT READY.** Requires a short design spec before implementation. The bug is real and confirmed, but the target architecture is undefined. Recommend adding ~5 bullet decisions (backend, schema, embedding strategy, search semantics, scope) before coding.

---

## Item 1.5 — Fix CompactionService LLM key mismatch
**Status: ⚠️ PARTIALLY VALIDATED — ROOT CAUSE DIFFERS FROM REPORT, FIX EASIER THAN STATED**

**Verified at line level:**
- `compaction_service.py:195-225` `_compact_with_llm`:
  - Line 199-202: calls `distill_context(...)`.
  - Line 203: `compacted = output.get("history") or output.get("compacted_history") or []`.
  - Lines 204-210: if empty, falls back to `output.get("summary")` and wraps as a system message.
- `distiller.py:638-639` confirms the actual key written is `distilled_state["_compacted_history"]`.

**IMPORTANT CORRECTION:** The audit report said it reads `"history"` but should be `_compacted_history`. **Verified correction:** Line 203 already reads `"compacted_history"` (without leading underscore) AND `"history"` — but the actual key is `"_compacted_history"` (WITH leading underscore). So none of the three keys currently read match the emitted key. The existing `summary` fallback (lines 206-210) usually saves it, masking the bug.

**Fix is minimal and unambiguous:**
- Change line 203 to read `output.get("_compacted_history")` in addition to the existing keys:
  `compacted = output.get("_compacted_history") or output.get("history") or output.get("compacted_history") or []`
- No design decisions needed.

**Readiness verdict: READY** (after correcting the key name in the fix instruction).

---

## Item 1.6 — Add node output schemas for 12 uncovered nodes
**Status: ⚠️ DETAIL SUFFICIENT — LARGE, MECHANICAL TASK**

**Verified at line level:**
- `state_schemas.py:46-143` defines `_NODE_OUTPUT_SCHEMAS` with only 4 entries: perception, planning, execution, verification.
- `wrap_node` (line 221-249) returns `fn` unwrapped if node has no schema (line 248-249).
- Confirmed the 11 other node functions exist (via grep): `analysis_node` (analysis_node.py:203), `plan_validator_node` (plan_validator_node.py:201), `step_controller_node` (step_controller_node.py:11), `evaluation_node` (evaluation_node.py:19), `debug_node` (debug_node.py:64), `replan_node` (replan_node.py:15), `delegation_node` (delegation_node.py:131), `analyst_delegation_node` (analyst_delegation_node.py:75), `memory_update_node` (memory_update_node.py:36), `wait_for_user_node` (wait_for_user_node.py:23), `frontier_loop_node` (frontier_loop_node.py:770).

**Task is well-defined but LARGE:**
1. For each of the 11-12 nodes, enumerate every return path (inspect the node's `dict` literals) and derive `allowed_keys` + `core_keys`.
2. Add each to `_NODE_OUTPUT_SCHEMAS`.
3. Register the wrap in `builder.py` via the `_validated()` import (the 4 existing nodes show the pattern at builder.py:141-147, 279-285, 477, 489, 636).

**Readiness: detail is sufficient** (mechanism is clear, pattern established), but this is a big mechanical task best executed node-by-node with a per-node test. The report correctly scoped it Medium. The main risk is enumerating complete return shapes per node — a thoroughness concern, not a design blocker.

**Could benefit from:** a short checklist of the 12 node files + note that `frontier_loop_node` may not be in the active graph (worth confirming before investing).

---

## Item 1.7 — Wire HOOK_SESSION_START call-site
**Status: ⚠️ DETAIL SUFFICIENT — SINGLE CALL-SITE LOCATION NEEDED**

**Verified at line level:**
- `hook_registry.py:79` defines `HOOK_SESSION_START = "session.start"` and documents it at lines 35-37 (payload `{"session_id": str, "task": str}`).
- `task_lifecycle.py:26-95` `start_new_task_impl` is the natural session-start point (it generates `orch._current_task_id = str(uuid.uuid4())[:8]` at line 39 — clear `session_id`/task boundary).
- Other hooks show the existing invocation pattern (e.g., `HOOK_ROUND_END` → perception_node.py:598) — the `try: from src.core.plugin.hook_registry import ...` fallback pattern is established.

**Fix is clear but one location decision remains:**
- The doc contract says "payload: {session_id, task}". Need to confirm where "task" is available at session start. `start_new_task_impl` resets state but the task text may live elsewhere (msg_mgr / orchestrator state). Options: invoke in `start_new_task_impl` (line ~39, after `_current_task_id` set) with whatever task is currently held, or in `run_agent_once` start. 

**Readiness: READY** — one small wiring decision (exact payload source for "task"), easily resolved during implementation. The fix is to add a guarded `registry.call(HOOK_SESSION_START, {"session_id": ..., "task": ...})` at the top of `start_new_task_impl`.

---

## Summary

| # | Item | Readiness | Confirmed Blocker |
|---|------|-----------|-------------------|
| 1.1 | Gate 2c fail-closed | ✅ READY | None — pattern already in file |
| 1.2 | Bash timeout status | ⚠️ READY w/ 1 decision | status flip (A) vs marker (B); consumer check needed |
| 1.3 | WorkspaceGuard fallback | ✅ READY | Minor: fail-closed affects tests |
| 1.4 | VectorStore memory | ❌ NOT READY | Needs storage/embedding/scope design spec |
| 1.5 | Compaction key mismatch | ✅ READY | None (correct key is `_compacted_history`) |
| 1.6 | Node output schemas | ⚠️ READY (large) | None — mechanical, pattern established |
| 1.7 | HOOK_SESSION_START | ✅ READY | Task-payload source decision |

## Recommendation

**6 of 7 items are implementable now** with the clarifications above. Item **1.4 (VectorStore)** is the only one that needs a short design spec (backend, schema, embedding strategy, search semantics, scope) before coding. I recommend:

1. Add a ~5-point design decision to 1.4 and split it into its own sub-task.
2. Correct the 1.5 fix note (read `_compacted_history` with the leading underscore — the report's stated key name is wrong; line 203 doesn't currently include it).
3. For 1.2, decide and document the status-flip vs. marker approach before implementation to avoid breaking `_is_success` consumers.

**Suggested Phase 1 execution order (by dependency/risk):** 1.1 → 1.5 → 1.3 → 1.7 → 1.2 → 1.6 → 1.4 (1.4 last, pending design).
