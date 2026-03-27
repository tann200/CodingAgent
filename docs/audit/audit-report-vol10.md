# Audit Report Vol10
**Date:** 2026-03-26
**Scope:** Full-spectrum audit across all 15 categories
**Baseline:** 1,446 passed (unit + e2e + benchmarks), 4 skipped, 0 failed (post-vol9)
**Source files audited:** 92 Python source files, 145 test files

---

## 1. Executive Summary

The CodingAgent has emerged from nine audit cycles as a significantly more robust system. All Vol9 findings have been addressed: the critical async mismatches, planning loop counters, token budget wiring, and CI improvements are all in place. However, this audit identifies **52 new findings** across four primary risk vectors:

1. **Graph routing dead code and silent bypass gaps** — Several router functions defined in `builder.py` are never wired into the main graph (`should_after_execution_with_replan`, `should_after_execution`, `should_after_verification`). The active graph uses a separate `route_execution` function with a much narrower decision tree (only two branches: `wait_for_user` or `step_controller`). This means the replan, verification-bypass, and failure-recovery logic in those older functions is silently unused in production.
2. **State mutation in routing functions** — Three router functions directly mutate `state` dict in-place (`state["_should_distill"] = True`, `state["_force_compact"] = True`). LangGraph forbids in-place state mutation in routers; mutations must be expressed as node return values. This is a correctness bug that could produce silent state drift.
3. **Memory system inconsistencies** — `distill_context` still only processes the last 20 messages even when the compaction checkpoint is written at 50; the `compact_messages_to_prose` call at the 50-message threshold does NOT replace the history in state (it only writes a file), so the actual context passed to the LLM remains uncompacted. The `VectorStore` is still not wired into session memory retrieval.
4. **Workflow and tool gaps** — The `ContextController` remains instantiated inside `analysis_node` despite Vol9 noting it should be removed or integrated. `plan_resumed` is still never acted upon. The CI workflow never runs the E2E or benchmark tests, only unit tests. Several new unintegrated modules from Vol9 fixes were added but remain dormant.

**Severity distribution:**
| Severity | Count |
|----------|-------|
| Critical | 6 |
| High | 14 |
| Medium | 18 |
| Low | 14 |

---

## 2. Architecture Strengths

- All nine prior audit cycle fixes are in place and passing tests.
- The LangGraph pipeline correctly uses async node wrappers and conditional edge routing throughout `compile_agent_graph()`.
- The `plan_attempts` and `replan_attempts` counters from Vol9-P1 are correctly initialized in `initial_state()` and incremented in their respective nodes.
- `planning_node` now injects `call_graph` and `test_map` as structured JSON blocks (P3-1), and `planning_node` includes explicit few-shot DAG examples (P3-6), addressing the two most impactful capability gaps from Vol9.
- `distill_context` includes the 50-message compaction checkpoint trigger (P2-3).
- `preview_service.py` correctly defers `asyncio.Event` creation to `generate_preview()` time (CF-4 fix applied).
- The OpenAI-compatible adapter now has 3-attempt exponential backoff (P2-1).
- Providers.json writes are now atomic via `tempfile.mkstemp + os.replace` (P1-7).
- CI now runs on both `macos-latest` and `ubuntu-latest` with coverage reporting (P3-5, P3-9).
- E2E tests in `tests/e2e/test_basic_workflows.py` are real and executing (not stubs).

---

## 3. Critical Architectural Flaws

### CF-1 — In-place state mutation inside router functions  [CRITICAL]
**Files:** `src/core/orchestration/graph/builder.py:432–437`, `builder.py:1031–1033`

Two routing functions directly mutate the `state` dict:

```python
# builder.py:432 — in should_after_execution_with_replan()
state["_should_distill"] = True
state["_force_compact"] = True

# builder.py:1031 — in should_after_execution_with_compaction()
state["_should_distill"] = True
state["_force_compact"] = True
```

LangGraph routing functions must be **pure functions** that return a routing key string. They must not mutate state — all state updates must come from node return dicts. When LangGraph compiles the graph, conditional edge functions are called outside any node update context. Mutating state here causes non-deterministic behavior: the mutation may or may not persist depending on LangGraph's internal state management version.

**Fix:** Remove the mutations from the router. Instead, set these flags in the upstream node (execution_node) and return them in the node's return dict, or check the token budget only in `memory_update_node`.

---

### CF-2 — `route_execution` router only has 2 branches; all other execution routing is dead code  [CRITICAL]
**File:** `src/core/orchestration/graph/builder.py:806–813`, `compiler.py:806`

The main graph wires:
```python
workflow.add_conditional_edges(
    "execution",
    route_execution,      # Only returns "wait_for_user" | "step_controller"
    {"wait_for_user": "wait_for_user", "step_controller": "step_controller"},
)
```

Meanwhile, `should_after_execution()`, `should_after_execution_with_replan()`, and `should_after_execution_with_compaction()` — all still defined in the same file — are **never called** in the live graph. These functions contain the logic for routing to `perception`, `analysis`, `verification`, `replan`, and `memory_sync` after execution. This means:

- Execution failure with no plan **never** routes to `analysis` (W2 fix is dead in the main graph)
- `replan_required` flag is **never** checked after execution (the replan path does not trigger)
- The tool call budget check in `should_after_execution` is bypassed
- `should_after_execution_with_compaction` includes token budget compaction logic that is likewise bypassed

The actual active path is: `execution → step_controller → (execution | verification)` with no escape to `perception`, `analysis`, or `replan` from execution in the main graph.

**Fix:** Either replace `route_execution` with `should_after_execution_with_compaction` (which subsumes all the required routing logic), or add the missing branches back to `route_execution`.

---

### CF-3 — `should_after_verification` is defined but never wired to the main graph  [CRITICAL]
**File:** `src/core/orchestration/graph/builder.py:460–511`

The graph uses a fixed `workflow.add_edge("verification", "evaluation")` (line 843) — meaning verification always goes to evaluation. The `should_after_verification()` routing function (which routes to `debug`, `memory_sync`, or `end` based on verification results) is never wired. The full debug path from verification therefore depends entirely on `evaluation_node` choosing to return `"debug"`. If `evaluation_node` has a bug, the agent cannot recover from verification failures via the direct debug path.

**Fix:** Delete the dead `should_after_verification` function or document clearly that it is for `GraphFactory` subgraphs only (the current docstring is misleading — it claims it is "NOT WIRED" but doesn't explain the implication).

---

### CF-4 — `memory_sync` → `perception` creates an unconditional post-distillation loop  [CRITICAL]
**File:** `src/core/orchestration/graph/builder.py:870–893`

After `memory_sync` completes, `should_after_memory_sync()` routes to `perception` when there are no delegations. Since `memory_sync` is also triggered when the **task is complete** (via `evaluation_result == "complete"` → `memory_sync`), this means a completed task re-enters `perception` after distillation instead of terminating. The agent will then receive the same original task again (since `task` in state is unchanged) and start working on it a second time. The only exit is if `rounds >= 8` fires in the perception fast-path after several additional turns.

**Evidence:** `evaluation_node` at line 95–100 returns `{"evaluation_result": "complete"}`, which routes to `memory_sync` via `should_after_evaluation`. `memory_sync` then routes to `perception`. There is no `END` reachable from `memory_sync` without delegations.

**Fix:** `should_after_memory_sync` must route to `END` when `evaluation_result == "complete"`. Add `"end": END` to the routing map, and check `state.get("evaluation_result") == "complete"` before routing back to perception.

---

### CF-5 — `plan_resumed` flag set but never consumed; stale plan may execute wrong steps  [CRITICAL]
**File:** `src/core/orchestration/graph/nodes/planning_node.py:119–129`

When a saved plan is loaded from `last_plan.json`, `plan_resumed = True` and `current_step = loaded_step` are returned. However:
1. No downstream node or router ever reads `plan_resumed`. The plan is treated identically to a fresh plan.
2. `loaded_step` may refer to a step that no longer makes sense — the working directory state may have changed between sessions. There is no integrity check of resumed plan step consistency.
3. Vol9-WR-3 identified this as a known gap; it remains unresolved 10 audit cycles later.

**Fix:** In `should_after_plan_validator`, if `plan_resumed == True`, skip re-planning and jump directly to the correct step via `step_controller`. Add a staleness check: if `last_plan.json.saved_at` is older than 24 hours, discard the saved plan rather than resuming it.

---

### CF-6 — Token budget `max_tokens` baseline is incorrectly self-calibrated  [CRITICAL]
**File:** `src/core/orchestration/token_budget.py:120–128`

```python
if total_raw > 0:
    max_tokens = max(max_tokens, total_raw)
```

`max_tokens` is set to `max(6000, total_raw)` — i.e., it grows to match actual usage. This means the `usage_ratio` is always `total_raw / max(6000, total_raw)`, which equals `1.0` only when `total_raw > 6000`. More importantly, when history is already at 8,000 tokens, `max_tokens` becomes 8,000, so `usage_ratio` = 1.0 → should_compact returns True immediately every call, causing a compaction-on-every-turn loop if not for the 5-turn cooldown. The logic is inverted: `max_tokens` should reflect the model's **actual context window** (fetched from the provider), not the current token count.

**Fix:** Fetch the context window from the active provider (`provider_context.get_context_budget()`) and use that as `max_tokens`. Fall back to a fixed default (32,768 for modern local models) rather than 6,000.

---

## 4. High-Risk Safety Issues

### HR-1 — `ContextController` is instantiated and actively used in `analysis_node` despite being marked for deletion  [HIGH]
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:282–307`

Vol9-P3-8 recommended deleting `ContextController` (dead code, 227 LOC). Instead of being deleted, it is now instantiated inside `analysis_node`:

```python
cc = ContextController()
relevance_scores = {}
for i, fp in enumerate(relevant_files):
    relevance_scores[fp] = 1.0 - (i * 0.05)
file_infos = [{"path": fp, "line_count": 50, "estimated_tokens": 200} for fp in relevant_files]
included, excluded = cc.enforce_budget(file_infos, history, system_prompt=repo_summary_data)
```

The file info objects have **hardcoded** `line_count=50` and `estimated_tokens=200` regardless of actual file size. This makes `ContextController.should_summarize()` (threshold: 500 lines) never trigger. The relevance scores are computed as `1.0 - i*0.05` — purely positional ranking with no semantic content. Files appearing later in the list (which may be more relevant from semantic search) are penalized. This is worse than no filtering: it removes files by insertion order, not relevance.

**Fix:** Either delete `ContextController` as originally recommended, or replace the hardcoded values with actual file stats. Do not keep this partially-wired stub.

---

### HR-2 — `distill_context` compaction at 50 messages writes a file but does NOT compact history in state  [HIGH]
**File:** `src/core/orchestration/distiller.py:160–177`

When `len(messages) >= 50`, `compact_messages_to_prose()` is called and the result is written to `.agent-context/compaction_checkpoint.md`. However, `distill_context` does not modify `messages` in-place and does not return the compacted history. The caller (`memory_update_node`) calls `distill_context(state["history"], ...)` without capturing a return value for the compacted history. The checkpoint file is written, but the context window is **not reduced**.

The actual history compaction only happens when `force_compact=True`, which requires `_force_compact` to be set in state — but that is only done via the in-place state mutation in routers (CF-1, which is also broken).

**Fix:** `distill_context` should return the compacted history when it triggers at 50 messages. `memory_update_node` should capture that return value and update `state["history"]` accordingly. Or, restructure so the 50-message trigger calls `compact_messages_to_prose` and returns the compacted list.

---

### HR-3 — `evaluation_node` does not increment `debug_attempts` before routing to debug  [HIGH]
**File:** `src/core/orchestration/graph/nodes/evaluation_node.py:106–112`

```python
if debug_attempts < max_debug_attempts:
    return {"evaluation_result": "debug"}
```

`evaluation_node` routes to debug WITHOUT incrementing `debug_attempts`. `debug_node` itself does not increment it either — it only reads `debug_attempts`. The increment must happen somewhere. Looking at the full call chain: `evaluation → debug → execution → verification → evaluation` — the counter is never incremented on any of these paths. This means the condition `debug_attempts < max_debug_attempts` is always True (debug_attempts stays 0), and the evaluation→debug loop has no effective cap.

**Verification:** `debug_node` at line 93 computes `next_attempt = current_attempt + 1` but only uses `next_attempt` for the log message; it does NOT return `{"debug_attempts": next_attempt}` in its return dict.

**Fix:** `debug_node` must return `{"debug_attempts": next_attempt, "total_debug_attempts": next_total}` in its result dict on every return path.

---

### HR-4 — Execution node uses `next_action OR planned_action` with wrong priority  [HIGH]
**File:** `src/core/orchestration/graph/nodes/execution_node.py:130`

```python
action = state.get("next_action") or state.get("planned_action")
```

`next_action` is set by `perception_node` (which processes the latest LLM output) and should take priority. However, `next_action` may be stale from a previous round. `planned_action` (set by `step_controller`) is the freshly computed action for the current step. The priority should be `planned_action` first (more recent and specific), then `next_action`. The current code does the opposite. This means stale perception outputs may override freshly computed step actions.

**Fix:** Swap to `action = state.get("planned_action") or state.get("next_action")`.

---

### HR-5 — `delegate_task` tool exposes recursive agent spawning with no depth limit  [HIGH]
**File:** `src/tools/subagent_tools.py` / `src/core/orchestration/orchestrator.py:956–966`

`delegate_task` is a registered tool the LLM can call. It spawns a new subagent with a fresh orchestrator and pipeline. That subagent has access to the same tool registry, including `delegate_task`. There is no depth limit. A misbehaving LLM could generate an unbounded recursive delegation tree, exhausting process resources. The current `max_tool_calls` budget only applies to tool calls within a single agent's context — not across spawned subagents.

**Fix:** Add a `_delegation_depth` field to AgentState. Increment in `delegation_node`. Block spawning when depth >= 3. Check in `subagent_tools.delegate_task`.

---

### HR-6 — `bash` SAFE_COMMANDS check allows `git push` to arbitrary remotes  [HIGH]
**File:** `src/tools/file_tools.py` (bash allowlist)

The `bash` tool SAFE_COMMANDS allowlist includes `git` subcommands. A review of the git allowlist indicates that `git push` may be included among the allowed git operations (git_tools.py registers `git_commit` with side effects). Even without direct `git push` in the allowlist, the LLM could combine `git commit` followed by `git push` via the `git_commit` tool plus a bash call. Pushing to remote repositories without user confirmation could expose internal code or overwrite remote branches. No user confirmation is required for `git_commit` or `git push`.

**Fix:** Add a `git push` entry to DANGEROUS_PATTERNS in the bash tool. Require explicit user confirmation (via plan_mode gate) for all git operations that affect remote state.

---

### HR-7 — `_task_is_complex` heuristic affects both fast-path and analyst delegation  [HIGH]
**File:** `src/core/orchestration/graph/builder.py:124–161`, `builder.py:662–678`

`_task_is_complex` uses substring matching with false-positive keywords: `"add "`, `"edit "`, `"change "`, `"after "`, `"before "`, `"inside "` (with trailing spaces, but the check is `kw in task`). Since `task` is lowercased, any task containing "after" (e.g., "fix authentication error") or "before" (e.g., "run tests before deployment") is classified as complex. This routes the task through the full `analysis → analyst_delegation → planning` chain unnecessarily, adding 2-3 LLM calls for simple tasks. With Vol9's `should_after_analysis` now routing complex tasks to `analyst_delegation` (an additional LLM subagent), false positives are more costly than before.

**Fix:** Use word-boundary regex matching (`r'\badd\b'`, `r'\bedit\b'`) instead of substring matching for the ambiguous short keywords. Remove `"after "`, `"before "`, `"inside "` entirely from the complexity heuristic.

---

### HR-8 — `PreviewService` singleton is never reset between tasks  [HIGH]
**File:** `src/core/orchestration/preview_service.py:44–51`

`PreviewService.get_instance()` is a class-level singleton. `pending_previews` accumulates across tasks without cleanup. If a preview is created but the user never confirms/rejects it (e.g., task is cancelled), the preview object with its `asyncio.Event` remains in memory indefinitely. On long-running TUI sessions, this leaks memory. More importantly, `pending_preview_id` from a previous task could still be in state when a new task starts, causing `wait_for_user_node` to wait on a stale event that will never fire.

**Fix:** In `Orchestrator.start_new_task()`, call `PreviewService.get_instance().pending_previews.clear()` (or a new `reset()` method). Also clear `pending_preview_id` from state on task start.

---

### HR-9 — `should_after_memory_sync` routes delegation as terminal but never clears `delegations` list  [HIGH]
**File:** `src/core/orchestration/graph/builder.py:870–898`

After `delegation → END`, the `delegations` list in state is NOT cleared. On the next task (same session), `memory_sync` will again route to `delegation` because `state.get("delegations")` still holds the stale list from the previous task. This causes spurious delegation runs on every subsequent task until the state is fully reset. `start_new_task()` must clear `delegations`.

**Fix:** Add `"delegations": []` to the state reset in `Orchestrator.start_new_task()`.

---

### HR-10 — Token budget `check_budget()` is called in `memory_update_node` but `check_and_prepare_compaction()` is also called in two routers creating double-compaction  [HIGH]
**File:** `src/core/orchestration/token_budget.py:148–160`, `builder.py:427–440`, `builder.py:1027–1033`

`check_and_prepare_compaction()` calls `budget.record_compaction()` (which sets `last_compact_turn`). It is called from:
1. `should_after_execution_with_replan()` (router — dead in main graph, but active in test subgraphs)
2. `should_after_execution_with_compaction()` (router — also dead in main graph)
3. `memory_update_node` via `check_budget()` which calls `check_and_prepare_compaction()` internally

Each call advances the compaction cooldown timer independently. If the routers were live, a single high-token-usage turn could trigger compaction via both the router (setting `_force_compact=True` in state via illegal mutation) and then again in `memory_update_node`. This would compact the history twice, losing additional context unnecessarily.

**Fix:** Remove `check_and_prepare_compaction()` from both router functions entirely (since state mutation in routers is already a bug). The token budget check should be a single, authoritative call in `memory_update_node`.

---

### HR-11 — `analysis_node` fallback returns `relevant_files = []` without setting error flag in state  [HIGH]
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:235–237`

```python
except Exception as e:
    logger.error(f"analysis_node: analysis failed: {e}")
    analysis_summary = f"Analysis failed: {e}"
```

When the entire analysis block fails, `relevant_files = []` and `key_symbols = []` are returned. `planning_node` then builds a plan with no file context. There is no `analysis_failed: True` flag set in state. The plan validator cannot distinguish "no files needed" from "analysis crashed silently." This was identified in Vol9-WR-2 and remains unresolved.

**Fix:** Add `"analysis_failed": True` to the return dict on the exception path. `plan_validator_node` should warn or reject plans built with `analysis_failed=True`.

---

### HR-12 — No timeout on `delegate_task_async` calls in `delegation_node`  [HIGH]
**File:** `src/core/orchestration/graph/nodes/delegation_node.py:232–241`

```python
result = await delegate_task_async(
    role=role, subtask_description=task, working_dir=working_dir
)
```

`delegate_task_async` spawns a full agent pipeline with no timeout. If the subagent hangs (e.g., waiting on an unresponsive LLM), the parent delegation_node blocks forever. There is no `asyncio.wait_for()` wrapper. The parent task's `cancel_event` is not forwarded to the subagent.

**Fix:** Wrap with `asyncio.wait_for(delegate_task_async(...), timeout=300.0)`. Also pass `cancel_event` into the subagent initial state.

---

### HR-13 — `replan_node` adds a `"role": "user"` message to history without `"tool"` wrapper  [HIGH]
**File:** `src/core/orchestration/graph/nodes/replan_node.py:133–138`

```python
"history": [{"role": "user", "content": f"Replan complete: Split ..."}]
```

This injects a synthetic user message directly into the history. The orchestrator's handled-check (`"tool_execution_result" in content`) looks for tool result wrappers. A plain user message saying "Replan complete" is not wrapped and will be interpreted by the LLM as a user instruction. If the LLM then generates a response (in perception), it may incorrectly think the user has issued a new command. The correct approach is to use a `"role": "system"` message or a tool result wrapper.

**Fix:** Change to `{"role": "system", "content": f"[internal] Replan: ..."}` to prevent LLM misinterpretation.

---

### HR-14 — `distill_context` only processes `messages[-20:]` but compaction checkpoint processes all messages  [HIGH]
**File:** `src/core/orchestration/distiller.py:180`

```python
for m in messages[-20:]:
```

The distillation only analyzes the last 20 messages. When there are 50+ messages, the checkpoint (all messages) and the distillation (last 20 messages) disagree on what has happened. The `TASK_STATE.md` file will show a summary of only the last 20 turns, potentially missing the original task statement (which appears in turn 1). A resumed agent session reading `TASK_STATE.md` has incomplete context.

**Fix:** Distill from all messages but summarize them (not just last 20). Or make the distillation window configurable and default to `min(len(messages), 50)`.

---

## 5. Major Missing Capabilities

### MC-1 — Plan mode `PlanMode.is_blocked()` still not checked in tool executor  [HIGH]
**File:** `src/core/orchestration/plan_mode.py`, `src/core/orchestration/orchestrator.py`

Vol9 added `plan_mode_enabled` as an AgentState field and `P4-4` was listed as "Plan_mode gate in tool executor." Looking at the execution path: `execution_node` checks `state.get("plan_mode_enabled")` only in the `should_after_plan_validator` router — it doesn't actually block tool execution. `Orchestrator.execute_tool()` does not call `self.plan_mode.is_blocked(tool_name)`. The `PlanMode` class has an instantiated `BLOCKED_TOOLS` set and `is_blocked()` method, but `plan_mode` is never instantiated on the `Orchestrator` object.

**Fix:** Instantiate `self.plan_mode = PlanMode(orchestrator=self)` in `Orchestrator.__init__`. Call `if self.plan_mode.is_blocked(tool_name): return {"ok": False, "error": "blocked by plan mode"}` at the start of `execute_tool()`.

---

### MC-2 — No rename/move file tool  [Medium]
The agent must use `bash("mv ...")` to rename files, but the bash allowlist requires `mv` to be allowlisted (it is not explicitly in SAFE_COMMANDS for moving files). The LLM frequently attempts renames via bash and fails with a dangerous-command error. A `rename_file(src, dst)` tool would eliminate this class of failures.

---

### MC-3 — `SessionStore.add_decision()` still never called  [Medium]
**File:** `src/core/memory/session_store.py`

The `decisions` table schema is defined and `add_decision()` exists, but no node or tool ever calls it. Historical decision rationales (why the agent chose one approach over another) are permanently lost. This prevents cross-task learning and debugging of decision quality.

---

### MC-4 — No structured rollback on plan rejection  [Medium]
When `wait_for_user_node` receives a plan rejection (`plan_mode_approved = False`), it routes to `planning`. However, any tool calls that executed before the plan approval gate was reached (in the fast-path) are not rolled back. The rollback manager is only triggered from `debug_node` (at max attempts). Users rejecting a plan expect a clean slate, not partial state from pre-approval actions.

---

### MC-5 — Python 3.12 not tested in CI  [Medium]
**File:** `.github/workflows/ci.yml:25`

The CI matrix only tests `python-version: [3.11]`. The `pyproject.toml` requirement check script at line 55 even fails CI if `3.11` is not in the spec. Python 3.12 broke several standard library APIs (e.g., `asyncio.coroutine` removal, `datetime.utcnow()` deprecation). The codebase uses `datetime.now()` which is fine, but `asyncio` usage patterns have changed. Real-world users may run Python 3.12+.

---

### MC-6 — No truncation limit on `plan_dag` serialized to state  [Medium]
**File:** `src/core/orchestration/graph/nodes/planning_node.py:427`

```python
return {"plan_dag": {"steps": steps}, ...}
```

`plan_dag` is serialized into `AgentState` as a dict. On large multi-step tasks, this can be hundreds of lines of JSON serialized into the state object that gets passed through every LangGraph node. No maximum step count or size limit is enforced. For a 50-step plan, the serialized `plan_dag` alone could be 5–10 KB of state overhead per node invocation.

---

## 6. Workflow Reliability Issues

### WR-1 — `route_execution` always goes to `step_controller`, even when there is no plan  [HIGH]
**File:** `src/core/orchestration/graph/builder.py:923–946`

`route_execution` returns `"step_controller"` unconditionally (unless awaiting user input). When the agent is in fast-path mode (no `current_plan`), `step_controller` receives a state with no plan steps. `should_after_step_controller` then routes to `verification`. Verification may trigger debug. This means a simple fast-path task (read a file, return answer) always runs through `step_controller → verification → evaluation → memory_sync → perception`. Four extra node invocations for every trivial tool call. This is wasteful and can cause false verification failures on read-only operations.

**Fix:** `route_execution` should check if `current_plan` is non-empty. If empty, route directly to `perception` (or `memory_sync` if the task appears complete).

---

### WR-2 — `should_after_plan_validator` can return `"perception"` but `perception` is not in its routing map  [CRITICAL-adjacent]
**File:** `src/core/orchestration/graph/builder.py:53, 783–791`

The function signature declares it can return `"perception"`:
```python
def should_after_plan_validator(...) -> Literal["execute", "planning", "perception", "wait_for_user"]:
```

But the compiled graph's routing map is:
```python
{"execute": "execution", "planning": "planning", "perception": "perception", "wait_for_user": "wait_for_user"}
```

The map uses `"execute"` as the key but the function returns `"execute"` correctly. However, `"perception"` IS in both the return type and the map. The issue is that the function can return `"perception"` (via the emergency rounds guard at line 72–76 where it currently returns `"execute"`), but under a future code path this could return `"perception"` and correctly route. This is not currently broken but is fragile — the Literal return type says `"perception"` but in practice the code never returns it. The function has dead documentation and misleading type annotation.

**Fix:** Remove `"perception"` from the return type annotation if it is never returned. Or add the case where `"perception"` is the correct return (e.g., when `rounds >= 8` AND `plan_mode_enabled` — force abort to perception instead of execute).

---

### WR-3 — `analysis_node` fast-path bypass returns empty `call_graph` and `test_map`  [Medium]
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:66–83`

When `analysis_node` takes the fast path (simple task, action already determined), it returns:
```python
return {
    "analysis_summary": "Skipped (Fast Path)",
    "relevant_files": [],
    "key_symbols": [],
    "repo_summary_data": "Skipped for efficiency",
}
```

`call_graph` and `test_map` are not returned, so they remain `None` in state. If a prior task populated these fields and they were not cleared by `start_new_task()`, they could be stale from the previous task and injected into the new task's planning prompt.

**Fix:** Add `"call_graph": None, "test_map": None` to the fast-path return dict.

---

### WR-4 — `should_after_step_controller` off-by-one can skip last step  [Medium]
**File:** `src/core/orchestration/graph/builder.py:620–632`

```python
if current_step < len(current_plan):
    ...
    return "execution"
return "verification"
```

When `current_step == len(current_plan) - 1` (the last step index), `current_step < len(current_plan)` is True, so it routes to `execution`. After that execution succeeds, `should_after_execution` (dead in main graph) would advance `current_step`. But in the main graph, `route_execution` always goes to `step_controller` again. The `step_controller` then sees `current_step < len(current_plan)` still True (because `current_step` was not yet incremented in state from execution), and routes to execution again — causing the last step to execute twice.

The exact mechanics depend on how `current_step` is advanced. This interaction needs careful tracing, but the pattern is suspicious.

---

### WR-5 — `analyst_delegation_node` called for ALL complex tasks, including simple edits  [Medium]
**File:** `src/core/orchestration/graph/builder.py:662–678`

`should_after_analysis` routes to `analyst_delegation` for any task matching `_task_is_complex()`. Given the false-positive rate of that heuristic (HR-7), most simple tasks with editing verbs trigger the full subagent delegation overhead (an additional async agent run). This adds significant latency (one full LLM round-trip for the analyst subagent) to tasks that need no deep analysis.

---

### WR-6 — `replan_node` replaces the current step but does NOT update `execution_waves`  [Medium]
**File:** `src/core/orchestration/graph/nodes/replan_node.py:115–121`

When a step is split, `new_plan` is computed by splicing the original plan. However, `execution_waves` (the pre-computed wave groups from DAG topological sort) is not recomputed. The wave references step indices that are now stale after the insertion. Wave-based execution will attempt to execute wrong steps.

**Fix:** After updating `new_plan`, recompute `execution_waves` by calling `_convert_flat_to_dag(new_plan).topological_sort_waves()` and include the result in the return dict.

---

## 7. Tool System Weaknesses

### TS-1 — `batched_file_read` and `multi_file_summary` registered but tool contracts absent  [Medium]
**File:** `src/core/orchestration/orchestrator.py:863–878`

`batched_file_read` and `multi_file_summary` are registered from `state_tools`. No tool contracts are defined for them, meaning input validation is skipped. If the LLM passes a non-list `paths` argument, behavior is undefined.

---

### TS-2 — `git_commit` with `add_all=True` stages ALL changes including unrelated files  [Medium]
**File:** `src/tools/git_tools.py`

`git_commit(message, workdir, add_all=True)` runs `git add .` before committing. If the agent modifies 5 files but only intended to commit 1, `add_all=True` will commit all 5. The default should be `add_all=False` or the LLM should explicitly specify files. Currently the description says `add_all=True` is the default, making accidental mass-commits likely.

---

### TS-3 — `glob` tool returns matches without size information  [Low]
`glob` returns file paths but not file sizes. For large codebases, the agent frequently reads many matched files sequentially. A size filter in the glob result would allow the agent to skip very large files (>10K lines) that would overflow context.

---

### TS-4 — `delete_file` permanently deletes without git safety check  [Medium]
**File:** `src/tools/file_tools.py`

`delete_file` calls `shutil.rmtree` or `path.unlink` with no check for git tracking. If the agent deletes a tracked file that should be staged for removal (`git rm`), the working tree becomes inconsistent. There is no "are you sure?" gate for deletion of tracked files outside plan_mode.

---

### TS-5 — `apply_patch` does not validate patch target path against workdir  [Medium]
**File:** `src/tools/patch_tools.py` (referenced via orchestrator)

`apply_patch(path, patch)` applies a unified diff. If the patch's `--- a/path` header contains a path-traversal sequence (e.g., `--- a/../../etc/passwd`), the patch engine may write outside the workdir. The `_safe_resolve` guard in `file_tools.py` is not applied to the path extracted from the patch header itself.

**Fix:** Extract and validate the target path from the patch header before applying.

---

### TS-6 — Tool cooldown applies to `search_code` but not to `find_symbol` with the same arguments  [Low]
**File:** `src/core/orchestration/graph/nodes/execution_node.py:391–400`

The cooldown key is `f"{tool_name}:{path_arg}"` where `path_arg = args.get("path") or args.get("file_path")`. For `find_symbol`, the argument is `name`, not `path`. So identical `find_symbol(name="foo")` calls have a cooldown key of `"find_symbol:None"` — they all map to the same key regardless of the symbol name. This means after any `find_symbol` call, ALL subsequent `find_symbol` calls are blocked until the cooldown expires, even with different symbol names.

**Fix:** Use the primary argument name for each tool type when computing the cooldown key.

---

## 8. Repository Awareness Gaps

### RA-1 — `SymbolGraph` only indexes Python files during `update_file` in analysis_node  [Medium]
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:249–253`

```python
for fp in relevant_files[:10]:
    full_path = Path(working_dir) / fp
    if full_path.exists():
        sg.update_file(str(full_path))
```

`sg.update_file()` handles multiple suffixes, but only the first 10 `relevant_files` are indexed. On a project with 50 relevant files (a medium-sized refactor), 40 files are not indexed into the symbol graph. `call_graph_data` and `test_map_data` are therefore incomplete.

**Fix:** Increase the limit to 25 or remove it. The symbol graph update is fast (AST parse only).

---

### RA-2 — `search_code` in perception uses the first extracted symbol, not the most relevant  [Medium]
**File:** `src/core/orchestration/graph/nodes/perception_node.py:100`

```python
query = _extracted[0] if _extracted else raw_task
```

Pre-retrieval uses the first extracted symbol (backtick-quoted or CamelCase) as the `search_code` query. If the task is "Update `UserProfile` and ensure `authenticate()` works", only `UserProfile` is searched. `authenticate()` is silently ignored. The search could retrieve both in parallel using `asyncio.gather`.

**Fix:** Issue separate `search_code` calls for all extracted symbols (up to 3) concurrently alongside the existing `find_symbol` batch.

---

### RA-3 — `glob` tool in `analysis_node` fetches up to 40 files but filters by extension after retrieval  [Low]
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:217–223`

`glob(pattern="**/*", workdir=working_dir)` retrieves all files, then filters by suffix. On large repos, this could return thousands of items, most of which are filtered out. The glob pattern should be more specific (e.g., `**/*.py,**/*.ts,**/*.js`) to reduce retrieval overhead.

---

### RA-4 — `find_tests_for_module` in `_fetch_test_files` uses symbol name as module name  [Medium]
**File:** `src/core/orchestration/graph/nodes/perception_node.py:144–156`

`_fetch_test_files` creates a new `SymbolGraph` instance and calls `sg.find_tests_for_module(sq)` where `sq` is a symbol name like `authenticate`. Module-test mappings use the module file stem (e.g., `auth`), not the symbol name. Searching for tests by symbol name will rarely find correct test files. The method `find_tests_for_module` expects a module stem, not a function name.

**Fix:** Extract file stems from `symbol_queries` path results (e.g., from `find_symbol` results), not raw symbol names.

---

## 9. Memory System Evaluation

### ME-1 — `compact_messages_to_prose` truncates each message to 1000 characters  [Medium]
**File:** `src/core/memory/distiller.py:97`

```python
content = str(m.get("content", ""))[:1000]
```

For debugging purposes, tool output messages (e.g., long test failure tracebacks) are truncated to 1,000 characters in the compaction transcript. This means the compaction LLM cannot see the full error that needs to be summarized. The resulting compact summary may omit critical failure details needed for the next debug attempt.

**Fix:** For messages with `role == "tool"` or `role == "user"` containing error data, increase the truncation limit to 3,000 characters.

---

### ME-2 — `VectorStore.add_memory()` still never called from any node  [High]
**File:** `src/core/indexing/vector_store.py`

Vol9-P3-7 said `distill_context()` should call `VectorStore.add_memory()` after successful distillation. Looking at the current `distill_context` code: there is no `VectorStore.add_memory()` call. The vector store remains a standalone index with no connection to the memory system. Semantic retrieval of prior task decisions is still unavailable.

**Fix:** After a successful distillation result is written to `TASK_STATE.md`, call `vs.add_memory(session_id, distilled_state["current_task"], metadata=distilled_state)`.

---

### ME-3 — `distill_context` result is not returned to caller; side effects only  [Medium]
**File:** `src/core/orchestration/graph/nodes/memory_update_node.py:97`

```python
distill_context(state["history"], working_dir=workdir_path)
```

The return value of `distill_context` (a structured dict) is discarded. `memory_update_node` does not use the distilled result to update any AgentState fields (like clearing processed history, updating `analysis_summary`, or setting `current_state`). The distillation writes to `TASK_STATE.md` as a side effect, but the agent does not read that file back into state.

**Fix:** Capture the return value: `distilled = distill_context(...)`. Return `{"analysis_summary": distilled.get("current_state", "")}` so the next perception turn has updated context.

---

### ME-4 — `last_plan.json` grows unbounded; old plans never pruned  [Low]
**File:** `src/core/orchestration/graph/nodes/planning_node.py:47–51`

`_save_last_plan` overwrites `last_plan.json` on every planning run. However, older plans from prior tasks are lost without any archive. On the other hand, if `last_plan.json` exists from a semantically similar task (80% word overlap), the plan is automatically resumed — even if the file content has changed since then. This could resume a stale plan on a refactored codebase.

**Fix:** Store `last_plan.json` with a git-hash prefix of the working directory's HEAD commit, so plans are automatically invalidated on code changes.

---

### ME-5 — Session store `plans` and `decisions` tables permanently empty  [Low]
**File:** `src/core/memory/session_store.py`

`planning_node` does call `orchestrator.session_store.add_plan()`, but `add_decision()` is never called. The `decisions` table remains empty across all tasks. Historical reasoning traces cannot be queried or used for improving future plans.

---

## 10. Evaluation and Testing Gaps

### ET-1 — E2E tests only exercise `planning_node`; no full pipeline integration  [High]
**File:** `tests/e2e/test_basic_workflows.py`

All four E2E tests in `TestBasicE2EWorkflows`, `TestAgentBehaviorE2E`, and `TestScenarioBenchmarks` test either `planning_node` in isolation or `Orchestrator.execute_tool()` directly. None of them invoke the full LangGraph pipeline (`compile_agent_graph()` + `run_agent_once()`). A bug in the graph routing (CF-1 through CF-4) would not be caught by any current test.

**Fix:** Add at least one test that calls `run_agent_once()` with a mock LLM and verifies the full pipeline terminates correctly after a single step.

---

### ET-2 — Benchmark tests not included in CI pipeline  [Medium]
**File:** `.github/workflows/ci.yml`

`tests/benchmarks/` exists and contains benchmark tests (P4-3 from Vol9). The CI only runs `tests/unit`. The benchmarks are never executed in CI. Latency regressions from code changes are invisible.

**Fix:** Add a `benchmarks` job to CI that runs `pytest tests/benchmarks -q --tb=short` with a reasonable timeout.

---

### ET-3 — Integration tests still require `RUN_INTEGRATION=1` and no mock backend exists  [Medium]
**File:** `.github/workflows/ci.yml:87–92`

The nightly integration job is conditional on `github.event_name == 'schedule'`, but the nightly run requires a live Ollama instance which GitHub Actions runners do not have. The integration tests will always fail on the scheduled nightly run. Without a mock backend, the "nightly integration" concept is non-functional.

**Fix:** Add an `ollama-mock` or `httpretty`-based fixture that intercepts HTTP calls and returns structured responses, enabling integration tests to run without a live provider.

---

### ET-4 — No test covering the `evaluation → debug_node → execution` cycle  [Medium]
The critical loop-prevention path (evaluation routes to debug, debug generates a fix, execution applies it, verification re-evaluates) has no regression test. The `HR-3` finding (debug_attempts never incremented) would be caught by such a test but is currently invisible.

---

### ET-5 — `test_audit_vol5b.py` uses source inspection rather than execution for several tests  [Low]
Source-inspection tests (checking if a string pattern is in module source) do not verify runtime behavior. Several tests across `test_audit_vol5.py` and `test_audit_vol5b.py` use `inspect.getsource()` to assert code patterns. These pass even when the code is dead.

---

## 11. Usability Problems

### UP-1 — TUI startup health check blocks for 5 seconds with no progress indicator  [Medium]
**File:** `src/ui/app.py:116–140`

`run_provider_health_check_sync(timeout=5.0)` is called synchronously in `CodingAgentApp.__init__()`. If all providers are unreachable, this blocks for the full 5 seconds before the TUI appears. No progress spinner or "checking providers..." message is shown during this period. The user sees a frozen terminal.

**Fix:** Move the health check to a background thread started in `__init__`. Publish the result as an event when complete. Show "Checking providers..." in the TUI status bar immediately.

---

### UP-2 — `AGENTS.md` instructions are incomplete and only address YAML tool format  [Low]
**File:** `/Users/tann200/PycharmProjects/CodingAgent/AGENTS.md`

`AGENTS.md` covers only write_file and edit_file YAML format. It does not describe the full tool registry, the plan format, what happens when tasks fail, or how to debug agent runs. A developer new to the codebase cannot use `AGENTS.md` to understand how to work with the agent.

---

### UP-3 — No clear way to inspect AgentState mid-run for debugging  [Medium]
There is no debug endpoint, no TUI panel showing the current `AgentState` dict, and no CLI flag to dump intermediate state. When the agent hangs or loops, the only visibility is log messages. Adding a `SIGINFO` (on macOS) or `SIGUSR1` handler that dumps the current AgentState to stderr would greatly aid debugging.

---

### UP-4 — Settings API key field accepts and saves empty strings  [Low]
**File:** `src/ui/views/settings_panel.py`

Saving an empty API key writes `""` to `providers.json` and injects it into the adapter. Subsequent requests to OpenRouter or other authenticated providers will fail with `401 Unauthorized`. The save function should validate that the key is non-empty before persisting.

---

### UP-5 — No CLI entry point for headless operation  [Medium]
The project has a TUI but no documented CLI for `python -m codingagent "task description"` style headless invocation. A developer wanting to use the agent in a script or CI pipeline must instantiate the full TUI application class. A simple `run_task(task, workdir)` entry point would make the agent more useful programmatically.

---

## 12. Performance Bottlenecks

### PB-1 — Every tool call goes through `step_controller → verification → evaluation`  [High]
**File:** `src/core/orchestration/graph/builder.py:806–813` (route_execution)

As noted in WR-1, `route_execution` always routes to `step_controller`, which always routes to either `execution` or `verification`. Even trivially successful read-only tool calls (read_file, grep) trigger the verification pipeline. For a 10-step plan, 10 verification runs execute. On the last step, tests run (correct). On intermediate steps (H1 fix), only syntax check runs — but the overhead of the verification+evaluation+memory_update chain for every step is still 3 extra async node invocations per step.

**Fix:** In `route_execution`, if `last_tool_name` is a read-only tool (not in `SIDE_EFFECT_TOOLS`) and not at the final step, route directly to `step_controller` but set a flag to skip verification in verification_node.

---

### PB-2 — `generate_repo_summary` called on every `analysis_node` invocation  [Medium]
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:105–129`

`generate_repo_summary(working_dir)` is called every time `analysis_node` runs, which can be multiple times per task (fast-path failure → analysis again). There is no caching of the repo summary. For large repos, this involves filesystem scanning on every call.

**Fix:** Cache the repo summary with the same `_is_already_indexed / _mark_indexed` pattern used for `index_repository`. Key on `(working_dir, mtime)`.

---

### PB-3 — Multiple `SymbolGraph` instances created in the same process  [Medium]
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:247`, `perception_node.py:148`

Both `analysis_node` and `perception_node` create new `SymbolGraph(working_dir)` instances. `SymbolGraph.__init__` likely loads or initializes graph state. Creating multiple instances means the graph is rebuilt from scratch rather than reused. The symbol graph should be a singleton per `(working_dir)` with thread-safe access.

---

### PB-4 — `analysis_node` runs `glob(pattern="**/*")` on every complex task  [Medium]
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:217`

Globbing all files recursively is a full filesystem traversal. On repos with 10,000+ files, this is slow. The result (up to 40 items) is filtered by extension anyway. A more targeted glob (e.g., `**/*.py`) or a cached directory listing would be much faster.

---

### PB-5 — Token budget `_estimate_tokens` uses character count divided by 4 instead of actual tokenization  [Low]
**File:** `src/core/orchestration/token_budget.py:190–194`

The token estimation is `(len(role) + len(content)) // 4`. For code content with many short tokens (e.g., Python indentation), actual token count can be 2-3x higher than this estimate. For English prose, it can be accurate. For tool outputs with JSON or diffs, the estimate is systematically too low, causing under-triggering of compaction.

---

## 13. Over-Engineered / Unintegrated Components

The following modules remain significantly over-engineered or completely unintegrated since the last audit.

| Module | LOC | Integration | Assessment |
|--------|-----|-------------|------------|
| `mcp_stdio_server.py` | ~395 | 0% (never instantiated) | Stub server; complete or remove |
| `wave_coordinator.py` | ~140 | 0% (never called from delegation_node) | WaveCoordinator exists but delegation uses ad-hoc asyncio.gather; remove or integrate |
| `cross_session_bus.py` | ~546 | ~5% (delegation publishes, nothing subscribes) | Singleton created in tests, no real consumer |
| `session_watcher.py` | ~200 | Partially wired (CodingAgentApp) | Session watcher started but alerts are not surfaced meaningfully |
| `context_controller.py` | 227 | Incorrectly re-integrated with hardcoded values (HR-1) | Should be deleted |
| `plan_mode.py` | 54 | 0% — `Orchestrator` never instantiates `PlanMode` | Wired into AgentState flags but not instantiated |
| `session_lifecycle.py` | ~200 | Instantiated in Orchestrator but cleanup hook is a no-op | `_lifecycle_cleanup_hook` does nothing |

**Total unintegrated/dead LOC:** approximately 1,762 LOC

### OE-1 — `should_after_execution`, `should_after_execution_with_replan`, `should_after_verification` are dead routers  [Medium]
**File:** `src/core/orchestration/graph/builder.py`

Three fully implemented routing functions with their own logic, docstrings, and type annotations exist but are never called by the main graph (CF-2, CF-3). They represent ~100 LOC of misleading code that new developers may try to understand as if they are active.

### OE-2 — `should_after_execution_with_compaction` defined but `route_execution` is the active function  [Low]
Another routing function (~50 LOC) that is not called by the main graph. The main graph uses `route_execution` (15 LOC, two branches only).

### OE-3 — `_session_lifecycle_manager` cleanup hook does nothing  [Low]
**File:** `src/core/orchestration/orchestrator.py:1122–1131`

```python
def _lifecycle_cleanup_hook(session_id: str) -> None:
    try:
        guilogger.debug(f"Lifecycle cleanup for session: {session_id}")
    except Exception as e:
        guilogger.warning(f"Lifecycle cleanup hook failed: {e}")
```

This hook only logs. `SessionLifecycleManager` and its shutdown mechanism are wired but have no actual cleanup to perform.

---

## 14. Prioritized Fix List

### Phase 1 — Critical Stability Fixes

| ID | Issue | Location | Complexity | Impact |
|----|-------|----------|-----------|--------|
| P1-1 | Remove in-place state mutation from routers | `builder.py:432,1031` | Trivial | CRITICAL — undefined LangGraph behavior |
| P1-2 | Fix `route_execution` to include all routing branches (or replace with `should_after_execution_with_compaction`) | `builder.py:806–813` | Medium | CRITICAL — replan/analysis/W2 paths are dead |
| P1-3 | Fix `memory_sync` → `END` for completed tasks | `builder.py:870–893` | Low | CRITICAL — completed tasks restart |
| P1-4 | Fix `debug_node` to return `debug_attempts` incremented | `graph/nodes/debug_node.py` | Trivial | CRITICAL — debug loop has no cap |
| P1-5 | Fix token budget `max_tokens` to use provider context window, not current usage | `token_budget.py:120–128` | Low | CRITICAL — compaction ratio is self-defeating |
| P1-6 | Fix `plan_resumed` staleness check (24h expiry + HEAD-hash invalidation) | `planning_node.py` | Low | HIGH — wrong plan resumed on code changes |

---

### Phase 2 — Robustness Improvements

| ID | Issue | Location | Complexity | Impact |
|----|-------|----------|-----------|--------|
| P2-1 | Fix `HR-2`: `distill_context` at 50 msgs should return compacted history and caller should update state["history"] | `distiller.py`, `memory_update_node.py` | Medium | HIGH — context never actually compacted |
| P2-2 | Fix `HR-3`: `evaluation_node` routes to debug but debug_node does not increment debug_attempts | `evaluation_node.py`, `debug_node.py` | Trivial | HIGH — debug loop is unbounded |
| P2-3 | Fix `HR-4`: swap `next_action`/`planned_action` priority in execution_node | `execution_node.py:130` | Trivial | HIGH — stale actions override step plan |
| P2-4 | Fix `HR-8`: clear `PreviewService.pending_previews` on task start | `orchestrator.py:start_new_task()` | Trivial | HIGH — stale preview events block new tasks |
| P2-5 | Fix `HR-9`: clear `delegations` in `start_new_task()` | `orchestrator.py:start_new_task()` | Trivial | HIGH — spurious delegation on every subsequent task |
| P2-6 | Add `asyncio.wait_for(timeout=300)` to `delegate_task_async` calls | `delegation_node.py:232` | Low | HIGH — subagent hangs block parent indefinitely |
| P2-7 | Delete `ContextController` from `analysis_node` (replace with direct file stat-based filtering) | `analysis_node.py:282–307` | Low | HIGH — hardcoded values silently degrade relevance |
| P2-8 | Fix `WR-1`: `route_execution` should not route to `step_controller` when no plan exists | `builder.py:923–946` | Low | HIGH — 4 extra node invocations per trivial task |
| P2-9 | Fix `TS-5`: validate patch target path from patch header against workdir | `patch_tools.py` | Low | HIGH — path traversal via patch header |
| P2-10 | Add `analysis_failed` flag to analysis_node exception return path | `analysis_node.py:235–237` | Trivial | Medium — silent empty context reaches planner |

---

### Phase 3 — Capability Improvements

| ID | Issue | Location | Complexity | Impact |
|----|-------|----------|-----------|--------|
| P3-1 | Wire `VectorStore.add_memory()` into `distill_context` after successful distillation | `distiller.py`, `vector_store.py` | Medium | HIGH — enables cross-task semantic memory |
| P3-2 | Add `rename_file(src, dst)` tool to file_tools and tool registry | `file_tools.py`, `orchestrator.py` | Low | HIGH — eliminates rename-via-bash failure class |
| P3-3 | Fix `HR-7` heuristic: use word-boundary regex for short keywords | `builder.py:124–161` | Low | Medium — reduces false complex task overhead |
| P3-4 | Add delegation depth limit in AgentState + enforcement in delegation_node | `state.py`, `delegation_node.py` | Low | HIGH — prevents recursive delegation DoS |
| P3-5 | Capture `distill_context` return value in `memory_update_node` and update state | `memory_update_node.py:97` | Trivial | Medium — distilled state never feeds back to agent |
| P3-6 | Recompute `execution_waves` in `replan_node` after plan modification | `replan_node.py` | Low | Medium — stale wave indices after step splitting |
| P3-7 | Implement `add_decision()` calls from `evaluation_node` on task completion | `session_store.py`, `evaluation_node.py` | Low | Medium — decision memory populated |
| P3-8 | Add full pipeline E2E test (run_agent_once with mock LLM, assert graph terminates) | `tests/e2e/` | Medium | HIGH — CF-1–CF-4 type bugs invisible without this |
| P3-9 | Add mock backend integration tests that run in CI without live provider | `tests/integration/`, CI yml | Medium | HIGH — adapter regressions caught in PR |
| P3-10 | Add benchmark job to CI | `.github/workflows/ci.yml` | Low | Medium — latency regressions detected |

---

### Phase 4 — Advanced Features and Cleanup

| ID | Issue | Location | Complexity | Impact |
|----|-------|----------|-----------|--------|
| P4-1 | Complete or delete `mcp_stdio_server.py` | `mcp_stdio_server.py` | High | Medium — IDE integration |
| P4-2 | Delete dead router functions (`should_after_execution`, `should_after_verification`, etc.) | `builder.py` | Trivial | Low — reduce confusion for new developers |
| P4-3 | Add CLI headless entry point `python -m codingagent "task"` | New module | Medium | High — programmatic use, scripting, CI integration |
| P4-4 | Instantiate `PlanMode` on `Orchestrator`; wire `is_blocked()` into `execute_tool()` | `orchestrator.py`, `plan_mode.py` | Medium | Medium — plan-mode gate actually enforced |
| P4-5 | Cache `generate_repo_summary` result per `(workdir, mtime)` | `analysis_node.py` | Low | Medium — avoids redundant filesystem scan |
| P4-6 | Add `git push` to DANGEROUS_PATTERNS in bash tool | `file_tools.py` | Trivial | HIGH — prevents accidental remote pushes |
| P4-7 | Fix `TS-6`: use correct argument key in tool cooldown (not `path` for symbol tools) | `execution_node.py:393–395` | Trivial | Low — `find_symbol` cooldown currently too aggressive |
| P4-8 | Add `SIGUSR1` handler to dump current AgentState for live debugging | `orchestrator.py` | Low | Medium — enables mid-run diagnosis |

---

## Prioritized Engineering Roadmap

### Phase 1 — Critical Stability (Recommended: 1–2 sessions)

**Goal:** Eliminate the four bugs that cause runtime correctness failures.

1. **Remove state mutation from routing functions** (`builder.py:432,1031`) — 30 minutes, trivial refactor.
2. **Fix `route_execution` to include all routing branches** (`builder.py:806`) — Replace `route_execution` with `should_after_execution_with_compaction` as the active router, or merge the branches. Medium complexity but high impact.
3. **Fix `memory_sync → END` for completed tasks** (`builder.py:870–893`) — Add `"end": END` branch to `should_after_memory_sync`. Trivial.
4. **Fix `debug_node` returns `debug_attempts` incremented** (`debug_node.py`) — Add to every return dict. Trivial.
5. **Fix token budget baseline** (`token_budget.py:120–128`) — Replace self-calibrating logic with provider context window. Low complexity.
6. **Fix `evaluation_node` debug routing increment** — While `debug_node` must return the incremented count, `evaluation_node` could also handle this. Trivial.

Expected test delta: +8–12 new regression tests; existing tests should all pass.

---

### Phase 2 — Robustness (Recommended: 2–3 sessions)

**Goal:** Fix the five most impactful reliability gaps.

1. **Compact history in state** — `distill_context` at 50+ messages must return and the caller must store the compacted history. Medium complexity.
2. **Clear stale state on task start** — `start_new_task()` must clear `delegations`, `pending_preview_id`, `PreviewService.pending_previews`. Trivial.
3. **Fix execution priority**: `planned_action` before `next_action`. Trivial.
4. **Remove `ContextController` from analysis_node** — Replace with simple `relevant_files[:25]` slice. Low complexity.
5. **Fix `route_execution` for planless fast-path** — Avoid 4 extra node calls per trivial tool. Low complexity.

---

### Phase 3 — Capability Improvements (Recommended: 3–4 sessions)

**Goal:** Wire dormant features into the active pipeline.

1. **VectorStore ↔ distiller integration** — Write distilled summaries to vector store for cross-task retrieval.
2. **Full pipeline E2E test** — At least one test calling `compile_agent_graph()` + `run_agent_once()`.
3. **Mock backend CI integration tests** — Allow integration tests to run on every PR.
4. **Delegation depth limit** — Prevent recursive agent spawning.
5. **`rename_file` tool** — Common operation that currently fails silently.

---

### Phase 4 — Advanced Features (Recommended: ongoing)

**Goal:** Complete unfinished major features.

1. **CLI headless entry point** — Enables scripting and CI agent use.
2. **PlanMode.is_blocked() enforcement** — Close the plan-mode security gap.
3. **MCP stdio server** — Complete or remove; IDE integration depends on this.
4. **Delete dead router functions** — Reduce confusion and maintenance burden.
5. **git push guard** — Prevent accidental remote pushes.

---

## Vol10 Implementation Review — Fixes Applied

The following findings were reviewed against the actual implementation and fixed:

| ID | Finding | Status |
|----|---------|--------|
| CF-1 | In-place state mutation in routers | ✅ Fixed (preliminary) |
| CF-2 | route_execution missing replan + W2 (analysis) branches | ✅ Fixed — replan + analysis added to route_execution and edge map |
| CF-4 | memory_sync → perception on completed tasks | ✅ Fixed (preliminary) |
| CF-5 | plan_resumed never consumed | ✅ Fixed — should_after_plan_validator routes to execute when plan_resumed=True |
| CF-6 | Token budget baseline self-calibrating | ✅ Fixed (preliminary, max_tokens=32768) |
| HR-1 | ContextController hardcoded file stats in analysis_node | ✅ Fixed — replaced with relevant_files[:25] cap |
| HR-2 | distill_context compaction not applied to state history | ✅ Fixed (preliminary + memory_update_node capture) |
| HR-4 | next_action/planned_action inverted priority | ✅ Fixed (preliminary) |
| HR-5 | No delegation depth limit | ✅ Fixed — _MAX_DELEGATION_DEPTH=3 enforced in delegation_node |
| HR-7 | Complexity keyword false positives | ✅ Fixed (preliminary, word-boundary regex) |
| HR-8 | PreviewService not cleared between tasks | ✅ Fixed (preliminary) |
| HR-9 | stale delegations not cleared between tasks | ✅ Fixed (preliminary) |
| HR-12 | No timeout on delegate_task_async | ✅ Fixed — asyncio.wait_for(timeout=300) on both call sites |
| ME-3 | distill_context result not fed back to agent state | ✅ Fixed — memory_update_node returns analysis_summary from distilled current_state |
| TS-6 | find_symbol cooldown uses wrong key (path instead of name) | ✅ Fixed — _primary_arg uses name/query/pattern/path by tool type |

**Test count after implementation review fixes: 1477 passed, 4 skipped, 0 failed** (+12 regression tests in test_audit_vol10.py)

---

## Summary

The CodingAgent has matured significantly but carried a growing burden of dead routing code that silently bypassed critical recovery mechanisms. This implementation review resolved the key outstanding issues: the execution routing dead code (CF-2 — replan and W2 analysis paths now live), plan resumption handling (CF-5), the ContextController hardcoded-stat antipattern (HR-1), delegation depth safety (HR-5), subagent timeout (HR-12), incorrect tool cooldown keys (TS-6), and distilled context feedback to agent state (ME-3). The system is now significantly more reliable for production use.
