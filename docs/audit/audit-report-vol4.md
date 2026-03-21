# CodingAgent Audit Report — Vol 4

**Date:** 2026-03-21
**Auditor:** Claude Sonnet 4.6
**Codebase:** /Users/tann200/PycharmProjects/CodingAgent
**Test baseline:** 863 passed, 5 skipped, 0 failed
**Focus areas:** TUI usability & features · Orchestration resilience & determinism · Test coverage · Implementation optimisation

---

## 1. Executive Summary

The CodingAgent is a well-structured local coding agent with a solid LangGraph pipeline, comprehensive safety mechanisms, and a growing test suite (863 tests). All critical security and correctness bugs from prior audits (Vol 1–3) have been addressed.

This audit uncovered **4 new critical bugs** (two in the TUI, one in verification_node, one in bash security), **8 high-severity issues** (primarily in orchestration determinism and TUI feature completeness), and **9 medium-severity issues** (performance and robustness gaps). None of the critical bugs were previously documented.

The most impactful problems are:

1. **The diff rendering in the TUI silently breaks on every diff** due to an escaped regex.
2. **`MainViewController` (plan progress, tool activity, file tracking dashboards) is fully implemented but never wired to the actual Textual app** — it is dead code.
3. **`verification_node` crashes with `TypeError` when `working_dir` is `None`** in the LangGraph state.
4. **`DANGEROUS_PATTERNS` whitespace check is not normalised**, allowing `rm  -rf` (double space) to bypass the block.
5. **Full test suite runs on every side-effect tool call** — a 5-step edit plan triggers 5 full pytest runs.
6. **No per-step retry limit** in `step_controller` — a permanently failing step burns all 30 tool calls.

---

## 2. Architecture Strengths

| Strength | Evidence |
|----------|----------|
| Clear LangGraph pipeline | 13 nodes, all with distinct responsibilities |
| Immutable state transitions | F3 deep-copy pattern enforced; `Annotated[List, operator.add]` reducers |
| Bounded loops | plan_validator rounds cap (8), debug cap (3), tool_call_count cap (30) |
| Circuit breaker | Per-provider CLOSED/OPEN/HALF_OPEN in `llm_manager.py` |
| WorkspaceGuard | Pattern + explicit list; `args.pop("user_approved")` prevents LLM bypass |
| Step rollback | `rollback_step_transaction()` called from verification_node on failure |
| Thread-safe TUI | `_history_lock` on all history access; `loop.call_soon_threadsafe` for cross-thread UI updates |
| Correlation IDs | Auto-injected on EventBus publish; logged in `call_model` |
| Multi-language verification | Python (pytest + ruff + syntax) and JS/TS (jest + tsc + eslint) |
| LRU caches | `_TEXT_CACHE`/`_JSON_CACHE` with 256-entry OrderedDict eviction |

---

## 3. Critical Architectural Flaws

### C1 — `_render_side_by_side_diff` regex is broken (TUI)

**Severity:** Critical
**File:** `src/ui/textual_app_impl.py:804`

```python
# BROKEN: r"..." raw string — \\d+ matches literal backslash-d-plus, not digits
match = re.search(r"@@ -(\\d+),?\\d* \\+(\\d+),?\\d* @@", line)
```

In a Python raw string `r"\\d+"`, `\\` is two literal backslashes → the regex tries to match the literal text `\d+` (not a digit sequence). Every unified diff `@@` hunk header looks like `@@ -5,7 +5,7 @@`, which contains digits, not backslashes. The regex **never matches**. As a result, the side-by-side diff table is never populated and the fallback plain-text path is always taken.

**Fix:** `r"@@ -(\d+),?\d* \+(\d+),?\d* @@"`

---

### C2 — `MainViewController` is fully implemented but never instantiated (TUI dead code)

**Severity:** Critical
**File:** `src/ui/views/main_view.py` (157 lines) vs `src/ui/textual_app_impl.py`

`MainViewController` subscribes to 8 EventBus events:
- `file.modified` / `file.deleted` → `DashboardState.modified_files`
- `tool.execute.start/finish/error` → `DashboardState.tool_activity` (last 10 activities)
- `plan.progress` → `DashboardState.plan_progress`
- `verification.complete` → `DashboardState.verification_status`

None of this is ever rendered. `CodingAgentTextualApp.__init__` (line 311) never instantiates `MainViewController`. The orchestration already publishes `plan.progress`, `tool.execute.finish`, `file.modified` etc. to the EventBus — the data is available but the wiring is missing. Users see a bare sidebar with a static "No active task" label instead of a live plan progress panel, tool activity log, and modified-file list.

---

### C3 — `verification_node` crashes with `TypeError` when `working_dir` is `None`

**Severity:** Critical
**File:** `src/core/orchestration/graph/nodes/verification_node.py:109`

```python
wd = Path(state.get("working_dir"))   # TypeError if working_dir is None
```

`Path(None)` raises `TypeError: argument should be str or an os.PathLike object, not 'NoneType'`. LangGraph catches unhandled node exceptions and aborts the graph run. Any task where `working_dir` was never set (e.g. a fresh graph invocation or state reset) will crash at the first verification pass.

**Fix:** `wd = Path(state.get("working_dir") or ".")`

---

### C4 — `DANGEROUS_PATTERNS` whitespace not normalised — `rm  -rf` bypass

**Severity:** Critical
**File:** `src/tools/file_tools.py:289`

```python
cmd_lower = command.lower()                    # NOT normalised
for pattern in DANGEROUS_PATTERNS:
    if pattern in cmd_lower: ...               # checks unnormalised string
```

The NEW-7 fix (audit vol2) added `_re.sub(r"\s+", " ", command).lower()` normalisation at line 487, but that only applies to the `RESTRICTED_COMMANDS` loop. The `DANGEROUS_PATTERNS` loop at line 289 still uses `command.lower()` without normalisation. Sending `rm  -rf /` (double space) produces `cmd_lower = "rm  -rf /"`, and `"rm -rf"` (single space) is NOT a substring — the check passes.

**Fix:** Change line 289 to `cmd_lower = re.sub(r"\s+", " ", command).lower()` (reuse the already-imported `_re` alias).

---

## 4. High-Risk Safety Issues

### H1 — Full test suite triggered on every side-effect tool call

**Severity:** High
**File:** `src/core/orchestration/graph/nodes/verification_node.py:107-121`

Every `bash` exit-0, `write_file`, `edit_file`, `edit_by_line_range`, `patch_apply`, `create_file`, and `delete_file` triggers `run_tests + run_linter + syntax_check` (or the JS equivalent). A 5-step plan that edits files at each step runs the full test suite 5 times. On a medium Python project this is 5 × 15-30s = 75-150s of overhead. There is no "verify only at plan completion" mode or per-step opt-out.

**Fix options:**
1. Only run full verification when `_step_requests_verification(state)` is true (explicit test step) — use `last_tool_name` as a signal and skip for non-critical write tools.
2. Add `verify_on_each_step: bool = False` to AgentState; planning_node sets it to True only for test-heavy tasks.

---

### H2 — No per-step retry limit in `step_controller`

**Severity:** High
**File:** `src/core/orchestration/graph/builder.py:409-415`

When `last_result.get("ok")` is False, `should_after_step_controller` always returns `"execution"` (retry). There is no step-specific retry counter. If a tool call permanently fails (e.g. syntax error in generated code), the pipeline loops `execution → step_controller → execution` indefinitely, consuming all 30 `tool_call_count` budget on a single broken step. The task fails with no meaningful error message.

**Fix:** Add `step_retry_counts: Dict[int, int]` to `AgentState`. `step_controller_node` increments the counter; `should_after_step_controller` routes to `"verification"` (then `"debug"`) when a step exceeds 3 retries.

---

### H3 — `action_interrupt_agent` always fires due to `or True` hack

**Severity:** High
**File:** `src/ui/textual_app_impl.py:958`

```python
if thread_alive or True:   # Be more permissive...
```

The `or True` unconditionally evaluates the block. Pressing `Escape` when no agent is running shows `"Agent interrupted"` every time. More importantly, the interrupt logic may interfere with UI state (`_agent_running = False`) when no agent is actually running, causing the next `send_prompt` to skip the running-guard. This was clearly a debug hack.

**Fix:** Remove `or True`. The condition should be `if thread_alive:` or simply execute when `_agent_running` is True (already checked in the outer `if`).

---

### H4 — `verification_node` runs tests with no timeout guard

**Severity:** High
**File:** `src/core/orchestration/graph/nodes/verification_node.py:119`

`verification_tools.run_tests(str(wd))` runs `pytest` with a hardcoded `timeout=120` (2 minutes). For a slow test suite this can stall the pipeline for 2 minutes per verification pass, and the `cancel_event` from the TUI is not checked during this period. The agent is uninterruptible while verification is running.

---

### H5 — `tee` and `touch` in `SAFE_COMMANDS` can bypass write controls

**Severity:** High
**File:** `src/tools/file_tools.py:380-386`

`tee` writes to arbitrary files: `tee /etc/cron.d/evil` redirects stdin to a file. `touch` creates new files anywhere on the filesystem. Both bypass `WorkspaceGuard` because they go through `bash()` rather than `write_file()`. The `>` dangerous-pattern check blocks `>` redirects, but `tee` is a process that writes directly, not via shell redirection.

**Fix:** Remove `tee` and `touch` from `SAFE_COMMANDS`. Both can be replaced by `write_file` for legitimate use cases.

---

### H6 — `should_after_execution_with_replan` routes to `perception` without incrementing `rounds`

**Severity:** High
**File:** `src/core/orchestration/graph/builder.py` (route_after_perception / should_after_execution_with_replan)

When execution routes back to `perception` (e.g. when `next_action` is exhausted), the `rounds` counter is not incremented in the returned state. The plan_validator's `rounds >= 8` guard counts `perception` passes via the `rounds` field. If `rounds` is never incremented on the execution-to-perception path, the rounds cap may never fire on this route, potentially creating an unbounded loop.

---

### H7 — EventBus subscribers never unsubscribed (memory leak)

**Severity:** High
**File:** `src/core/orchestration/event_bus.py`

`MainViewController.__init__` registers 8 subscribers. `CodingAgentTextualApp.on_mount` registers 6 more. If the TUI is torn down and reinstantiated (e.g. in tests or if the app restarts), all subscriptions accumulate. There is no `unsubscribe` call in any `__del__` or cleanup method. Each subscriber keeps the subscribing object alive (preventing GC) and fires stale callbacks on dead UI widgets.

---

### H8 — `verification_node` rollback runs even when `working_dir` is relative

**Severity:** High
**File:** `src/core/orchestration/graph/nodes/verification_node.py:139`

`_resolve_orchestrator(state, config)` is called to get the orchestrator for rollback. But `state.get("working_dir")` may be a relative path like `.` or `src/`. If `orchestrator.rollback_step_transaction()` resolves snapshot paths relative to a different CWD than the current process CWD, rollback silently restores files to wrong paths. The rollback path is not validated against `safe_resolve()`.

---

## 5. Major Missing Capabilities

### M1 — No streaming LLM output to TUI

All `call_model` invocations use `stream=False`. On a local 9B model (LM Studio), responses take 30–90 seconds. Users see only `"🔄 Working..."` during this time. Modern coding agent TUIs (Aider, Claude Code, Cursor) stream tokens in real time.

**Recommendation:** EventBus already has `model.response` events. Add a `model.token` event that fires per-chunk during streaming, and subscribe in `CodingAgentTextualApp` to append tokens to `RichLog`.

### M2 — No live plan progress display

`DashboardState.plan_progress` is populated by `plan.progress` EventBus events, but `MainViewController` (which tracks it) is never connected to the TUI. Users cannot see "Step 2/5: Edit authentication module" during execution.

### M3 — No input autocomplete / command palette

`ChatInput` has up/down arrow history (good), but no tab-completion for slash commands, file paths, or common task patterns.

### M4 — No diff preview before applying edits

The agent applies file edits immediately without showing the user a preview. Aider and Cursor show diffs for user approval before writing. This is a trust and safety gap.

### M5 — No benchmark / evaluation harness for measuring task success rate

There is a `test_scenario_evaluator.py` (195 lines) and a `scenario_evaluator.py`, but no canonical task suite for measuring end-to-end success rate on representative coding tasks. There is no baseline to detect regressions in agent quality.

---

## 6. Workflow Reliability Issues

### W1 — `step_controller` infinite retry on permanent failures (see H2)

See H2 above. No per-step retry budget.

### W2 — Verification runs full suite unconditionally on every side-effect (see H1)

See H1 above.

### W3 — `replan_node` path rarely triggers

`should_after_execution_with_replan` routes to `"replan"` when `last_result.get("requires_split")` is True (set by F13 when > 200 lines changed). This is the only trigger. The replan node itself (`replan_node.py`) uses the `"strategic"` LLM role to split the step. This path is correct but almost never exercised in practice because 200-line edits are rare. No tests cover the replan→step_controller→execution sub-path.

### W4 — `debug_node` reset on `error_type` change may mask cascading failures

`debug_attempts` resets to 0 when `error_type` changes (vol4 fix). If a debug fix introduces a new error, the debug budget resets, allowing up to `3 × N` debug iterations where N is the number of distinct error types encountered. A pathological case could loop indefinitely if errors alternate.

### W5 — `evaluation_node` routes to `step_controller` for remaining plan steps

When `verification_passed` is True but there are remaining plan steps, `evaluation_node` routes to `step_controller`. This is correct, but if `current_step >= len(current_plan)` at this point (e.g. step was already advanced by execution_node), `should_after_step_controller` immediately returns `"verification"`, creating a `step_controller → verification → evaluation → step_controller` loop with no forward progress.

---

## 7. Tool System Weaknesses

### T1 — `tee` and `touch` write-bypass in `SAFE_COMMANDS` (see H5)

### T2 — `bash` timeout not enforced for all blocked-then-allowed commands

`bash` sets `timeout=30` for `subprocess.run` (line ~570 of file_tools.py). However the timeout is hardcoded and not configurable. A slow `find` across a large filesystem can legitimately take >30s.

### T3 — `edit_by_line_range` line-range semantics unclear

`edit_by_line_range(path, start_line, end_line, new_content)` — is `end_line` exclusive (Python slice) or inclusive (user-visible line number)? The docstring and implementation do not clearly document this, making it easy for the LLM to produce off-by-one errors.

### T4 — `generate_patch` diff format

`patch_tools.generate_patch` generates a diff but does not validate it can be applied cleanly to the current file before returning. If the file has been modified since the snapshot, the patch will fail silently on `apply_patch`.

### T5 — No tool call idempotency markers

Tools have no `idempotent: bool` flag. The orchestrator cannot distinguish a retried tool call from a new one, so if a retry occurs after a partial write, the tool may double-write or produce inconsistent state.

---

## 8. Repository Awareness Gaps

### R1 — Analysis node runs even for trivial tasks

`route_after_perception` fast-paths simple tasks to execution only when `_task_is_complex` is False AND `next_action` is set (perception generated a tool call). For tasks like "what does this function do?", perception returns a tool call (`read_file`), and the fast path triggers. But for "refactor the auth module", perception does NOT generate a tool call, so the full `analysis → planning` path runs. This is correct behaviour, but `analysis_node` runs `index_repository` (now cached) and `find_symbol` queries even for tasks that need only a single targeted file read.

### R2 — `analyst_delegation_node` subagent is fire-and-forget for complex tasks

`analyst_delegation_node` spawns a subagent via `delegate_task_async`. The findings are stored in `AgentState.analyst_findings` and injected into the planning prompt. However, if the delegation fails or times out, `analyst_findings` is None and planning proceeds without repo analysis. There is no fallback: planning silently receives less context.

### R3 — Symbol graph not used during planning

`planning_node` receives `analyst_findings` (if present) and the task description. It does not receive the output of `_INDEXED_DIRS` or the symbol graph directly. The planning prompt has no explicit "here are the relevant files and symbols" section derived from the symbol graph. Planning is effectively code-blind beyond what the analyst delegation provides.

---

## 9. Memory System Evaluation

| Component | Status | Notes |
|-----------|--------|-------|
| `_TEXT_CACHE` / `_JSON_CACHE` | ✅ Fixed (LRU 256) | Vol2 NEW-20 |
| `verified_reads` cross-task reset | ✅ Fixed | Vol3 F16 |
| Distiller asyncio fix | ✅ Fixed | Vol7 C9 |
| SQLite WAL + busy_timeout | ✅ Good | session_store.py |
| Token budget dynamic | ✅ Fixed | Vol3 F10, provider_context.py |
| EventBus subscriber cleanup | ❌ Missing | Never unsubscribed (H7) |
| Session store old record pruning | ❌ Missing | No auto-cleanup policy |
| Context truncation O(n) loop | ⚠️ Present | context_builder.py:410-414 |

**Context truncation detail:** The fine-tuning loop at `context_builder.py:410-414` removes characters one-by-one until the token count fits. After the heuristic character-limit cut (which is close but may overshoot), this loop runs O(overshoot) iterations, each calling `token_estimator(truncated_text)`. For a 50K-token document truncated to 6K, the overshoot is typically 10–100 characters = 10–100 extra estimator calls. This is acceptable in practice. However, the `max_tokens < marker_tokens` fallback branch at line 381-387 has the same O(n) loop with no heuristic pre-cut — for small `max_tokens` this is O(len(text)) iterations.

---

## 10. Evaluation and Testing Gaps

### E1 — `MainViewController` has unit tests but TUI integration is untested

`tests/unit/test_ui_main_view.py` and `tests/unit/test_dashboard.py` test `MainViewController` in isolation. There are no tests verifying that `CodingAgentTextualApp` correctly wires EventBus events to widget updates, that the settings modal opens, or that the diff renderer produces correct output.

### E2 — No test for `_render_side_by_side_diff` (the broken regex)

The broken regex at line 804 has no unit test. A test asserting that a standard unified diff produces a populated Rich table would have caught this.

### E3 — No test for `verification_node` with `working_dir=None`

The C3 crash path has no test. Any `test_verification_node_*` tests likely provide a valid `working_dir`.

### E4 — No test for DANGEROUS_PATTERNS whitespace bypass

The `rm  -rf` bypass path (C4) has no test.

### E5 — No test for step_controller infinite-retry scenario

No test exercises the path where `last_result.ok = False` and verifies that the step eventually terminates rather than looping forever.

### E6 — `test_scenario_evaluator.py` tests call `agent.run()` but agent is mocked

The scenario evaluator tests verify that the evaluator calls the agent and returns a result, but with a mocked agent. There is no end-to-end test on a real (but sandboxed) codebase.

### E7 — No test coverage for `replan_node` integration path

`requires_split=True` → `replan` → `step_controller` → `execution` is untested end-to-end.

### E8 — No adversarial YAML injection tests

`parse_tool_block` is not tested with:
- Unclosed YAML blocks
- YAML with injected `---` document boundaries
- JSON-in-YAML fields with embedded newlines

---

## 11. Usability Problems

### U1 — No real-time LLM output (streaming)

Most impactful UX issue. Users wait silently for 30–90s with only `"🔄 Working..."`.

### U2 — Plan progress panel implemented but invisible (C2)

`MainViewController.DashboardState.plan_progress` is populated but never rendered.

### U3 — Diff viewer broken (C1)

Side-by-side diffs always render as plain text due to broken regex. The output is hard to read for large diffs.

### U4 — Settings modal initialisation fragile

`self._settings_modal` is set at line 1237 in a code path that only runs when `TEXTUAL_AVAILABLE=True`. If this code path fails (e.g. import error in `SettingsModal`), `action_open_settings` raises `AttributeError: 'CodingAgentTextualApp' object has no attribute '_settings_modal'`. No try/except guard exists around line 945.

### U5 — Context sidebar hardcodes "128k" limit

The context sidebar (`context_info` label) is initialised with `"Used: 0\nLimit: 128k\n0%"`. The 128k limit is hardcoded; it is not derived from the active provider's `context_length`. For a 32K model this misleads the user. `_on_token_usage` updates "Used/Prompt/Reply/Latency" but never updates the limit.

### U6 — `continue` command restores message history but not graph state

`_restore_state_for_continue()` restores `msg_mgr.messages` and `_session_read_files`. But `AgentState` (the LangGraph graph state: `current_plan`, `current_step`, `verified_reads`, etc.) is NOT restored. Re-running after a `continue` starts a fresh graph execution, losing the plan position. The user experience of "resume from where I left off" is misleading.

### U7 — Log panel hidden by default with no indicator

The system log panel (`sys_log`) is hidden by default (`self.sys_log.display = False`). There is no visual indicator that it exists or that Ctrl+L toggles it. New users miss all log output.

---

## 12. Performance Bottlenecks

| Issue | Location | Impact | Severity |
|-------|----------|--------|----------|
| **Full test suite per side-effect** | verification_node.py:107 | 5-step plan = 5 × pytest | High |
| **O(n) truncation fallback** | context_builder.py:381-387 | O(len(text)) for tiny max_tokens | Medium |
| **Context rebuilt from scratch per node** | context_builder.py | Msg list re-serialised each node call | Medium |
| **Pre-retrieval tool calls on first perception** | perception_node.py:205 | 3 tool calls every new task | Medium |
| **Analyst delegation blocks planning** | analyst_delegation_node.py | Subagent runs serially before planning | Medium |
| **Task state file read on every result** | textual_app_impl.py:844 | File I/O per agent response | Low |
| **`_refresh_provider_info` probes API** | textual_app_impl.py:464-484 | Network call on startup | Low |

---

## 13. Over-Engineered Components

### OE1 — `MainViewController` (157 lines) with no integration

Full dashboard controller tracking file modifications, tool activity, and plan progress via EventBus — never displayed. Should either be wired to the TUI or removed.

### OE2 — `provider_panel.py` and `settings_panel.py` views

Both exist as standalone view controllers but are only used inside the settings modal. The views are well-structured but have significant boilerplate for what amounts to a provider-name dropdown.

### OE3 — Dual task state files (`TODO.md` + `TASK_STATE.md`)

Both files track task progress. `TODO.md` is written by `planning_node` (deterministic plan tracker). `TASK_STATE.md` is written by the LLM (inferred context recovery). These serve different purposes (per MEMORY.md), but both are injected into the context as `<task_progress>` and `<session_summary>` respectively. When both are populated they may contain contradictory state, causing confusing LLM behaviour.

### OE4 — `delegation_node` fire-and-forget with no result integration

The delegation system (`delegation_node` + `subagent_tools.py`) runs subagents in background threads after `memory_sync`. The results are never read by the pipeline (graph ends at `delegation → END`). For the current single-agent use case, this is dead infrastructure.

### OE5 — `replan_node` triggered only by `requires_split=True` (rare)

The replan subsystem (`replan_node` + `should_after_replan`) exists for splitting oversized steps, but this path is triggered only when a write tool returns `requires_split=True` (> 200 lines changed). In practice, LLM-generated edits are rarely this large. The node is present but practically unused.

---

## 14. Prioritised Fix List

### Phase 1 — Critical Stability (1–2 days)

| # | Fix | File | Complexity | Impact |
|---|-----|------|------------|--------|
| **P1-1** | Fix DANGEROUS_PATTERNS whitespace normalisation (C4) | `src/tools/file_tools.py:289` | Trivial (1 line) | Security |
| **P1-2** | Fix `verification_node` None working_dir guard (C3) | `src/core/orchestration/graph/nodes/verification_node.py:109` | Trivial (1 line) | Crash prevention |
| **P1-3** | Fix diff rendering regex (C1) | `src/ui/textual_app_impl.py:804` | Trivial (1 line) | TUI correctness |
| **P1-4** | Remove `or True` from `action_interrupt_agent` (H3) | `src/ui/textual_app_impl.py:958` | Trivial (1 line) | UX correctness |
| **P1-5** | Remove `tee` and `touch` from `SAFE_COMMANDS` (H5) | `src/tools/file_tools.py:380,386` | Trivial (2 lines) | Security |
| **P1-6** | Add tests for C1, C3, C4 paths | `tests/unit/test_audit_vol4.py` | Small | Regression prevention |

### Phase 2 — Robustness (3–5 days)

| # | Fix | File | Complexity | Impact |
|---|-----|------|------------|--------|
| **P2-1** | Add per-step retry counter to `step_controller` (H2) | `state.py`, `step_controller_node.py`, `builder.py` | Medium | Prevents infinite retry |
| **P2-2** | Conditional verification — skip full suite on non-test steps (H1) | `verification_node.py` | Medium | 5–10× faster multi-step plans |
| **P2-3** | Wire `MainViewController` to `CodingAgentTextualApp` (C2) | `textual_app_impl.py` | Medium | Plan progress display |
| **P2-4** | Add EventBus `unsubscribe` and cleanup on TUI teardown (H7) | `event_bus.py`, `textual_app_impl.py` | Medium | Memory leak prevention |
| **P2-5** | Fix `continue` to restore full AgentState, not just messages (U6) | `textual_app_impl.py`, `orchestrator.py` | Medium | UX correctness |
| **P2-6** | Make context sidebar limit dynamic from provider config (U5) | `textual_app_impl.py:366` | Small | UX accuracy |

### Phase 3 — Capability Improvements (1–2 weeks)

| # | Fix | File | Complexity | Impact |
|---|-----|------|------------|--------|
| **P3-1** | Add streaming LLM output to TUI (M1) | `llm_manager.py`, `textual_app_impl.py` | Large | Major UX improvement |
| **P3-2** | Implement live plan progress panel in TUI sidebar (M2) | `textual_app_impl.py` | Medium | Transparency |
| **P3-3** | Add rounds increment on execution→perception route (H6) | `builder.py` / `execution_node.py` | Small | Loop safety |
| **P3-4** | Add `edit_by_line_range` line-range documentation and validation (T3) | `file_tools.py` | Small | LLM usability |
| **P3-5** | Session store auto-pruning policy | `session_store.py` | Small | Resource cleanup |

### Phase 4 — Advanced Features (2–4 weeks)

| # | Feature | Notes |
|---|---------|-------|
| **P4-1** | Diff preview before applying edits (M4) | Show diff → user approval → apply |
| **P4-2** | End-to-end benchmark harness (M5) | Canonical task suite; measure pass@1 |
| **P4-3** | Input autocomplete / command palette (M3) | Slash commands, file path completion |
| **P4-4** | Symbol-aware planning context (R3) | Inject top-N relevant symbols into planning prompt |
| **P4-5** | Integrate or remove delegation subsystem (OE4) | Either read delegation results or remove fire-and-forget |

---

## 15. Test Coverage Summary

**Well-covered areas:**
- WorkspaceGuard (21 tests)
- Verification node behaviour (29 tests)
- Thread safety (8 tests)
- Session store concurrency (WAL mode, concurrent writes)
- Symbol graph multi-language (11 tests)
- Correlation IDs (13 tests)
- Orchestrator loop handling (21 tests)
- All prior audit regression tests (Vol1–3)

**Coverage gaps (no tests):**
- `MainViewController` integration with Textual app
- `_render_side_by_side_diff` regex correctness
- `verification_node` with `working_dir=None`
- `DANGEROUS_PATTERNS` whitespace bypass via double-space
- `step_controller` permanent-failure infinite-loop scenario
- `replan_node` integration path (requires_split → replan → step_controller → execution)
- EventBus unsubscribe / subscriber cleanup
- Adversarial YAML injection in `parse_tool_block`
- `action_open_settings` modal initialisation failure path

---

## 16. Fix Status Tracking (to be updated)

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| C1 | Diff rendering regex broken | Critical | ✅ Fixed — `textual_app_impl.py:804` |
| C2 | MainViewController not wired | Critical | ⬜ Open |
| C3 | verification_node None working_dir crash | Critical | ✅ Fixed — `verification_node.py:109` |
| C4 | DANGEROUS_PATTERNS whitespace bypass | Critical | ✅ Fixed — `file_tools.py:289` |
| H1 | Full test suite on every side-effect | High | ⬜ Open |
| H2 | No per-step retry limit | High | ✅ Fixed — `step_controller_node.py` + `builder.py` (MAX=3) |
| H3 | action_interrupt_agent `or True` bug | High | ✅ Fixed — `textual_app_impl.py:958` |
| H4 | Verification runs with no cancel check | High | ⬜ Open |
| H5 | `tee`/`touch` write bypass | High | ✅ Fixed — `file_tools.py:380,386` |
| H6 | rounds not incremented on exec→perception | High | ⬜ Assessed — rounds incremented by perception_node; not a real gap |
| H7 | EventBus subscribers never unsubscribed | High | ✅ Fixed — `_eb_subscriptions` list + `on_unmount` |
| H8 | Rollback path not validated with safe_resolve | High | ⬜ Open |
| M1 | No streaming LLM output | Medium | ⬜ Open |
| M2 | No live plan progress display | Medium | ✅ Fixed — C2 dashboard wired to TUI sidebar |
| M3 | No input autocomplete | Medium | ⬜ Open |
| M4 | No diff preview before applying | Medium | ⬜ Open |
| M5 | No benchmark harness | Medium | ⬜ Open |
| W1 | Step controller infinite retry | Medium | ✅ Fixed — same as H2 |
| W3 | replan_node path untested | Low | ⬜ Open |
| W4 | debug_attempts reset may loop on cascading errors | Low | ⬜ Open |
| W5 | evaluation→step_controller→verification loop | Low | ✅ Fixed — `evaluation_node.py` clears `replan_required` |
| U1 | No streaming (same as M1) | High | ⬜ Open |
| U2 | Plan progress panel invisible (same as C2/M2) | High | ✅ Fixed — wired in C2 |
| U3 | Diff viewer broken (same as C1) | High | ✅ Fixed — C1 regex fix |
| U4 | Settings modal init fragile | Medium | ✅ Fixed — `getattr` guard in `action_open_settings` |
| U5 | Context sidebar hardcodes 128k | Low | ✅ Fixed — reads `get_context_budget()` dynamically |
| U6 | `continue` restores messages only, not graph state | Medium | ⬜ Open |
| U7 | Log panel hidden with no indicator | Low | ✅ Fixed — legend now mentions `Ctrl+L (toggle)` |
| OE1 | MainViewController dead code | — | ✅ Fixed — wired via C2 |
| OE4 | Delegation fire-and-forget dead infrastructure | Low | ⬜ Open |
| E1–E8 | Test coverage gaps | — | Partially addressed (+32 new tests) |
