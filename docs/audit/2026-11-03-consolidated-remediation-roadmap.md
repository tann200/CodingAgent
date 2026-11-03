# Consolidated Engineering Remediation Roadmap (2026-11-03)

**Document:** Engineering Remediation Specifications & Implementation Roadmap  
**Audits Covered:**
- [docs/audit/2026-11-03-tui-subsystem-audit.md](file:///Users/tann200/PycharmProjects/CodingAgent/docs/audit/2026-11-03-tui-subsystem-audit.md)
- [docs/audit/2026-11-03-tools-and-graph-workflows-audit.md](file:///Users/tann200/PycharmProjects/CodingAgent/docs/audit/2026-11-03-tools-and-graph-workflows-audit.md)
**Status:** Phase 1 & Phase 2 Implemented and Verified (24/24 unit tests passing, ruff & mypy clean)

---

## Executive Overview & Architectural Guardrails

This roadmap consolidates all findings uncovered during the dual audits of the **Textual UI (TUI)** and **Tools & Graph Workflows** subsystems into an actionable, phased engineering execution plan.

Every task includes:
1. **Root Cause Analysis:** Exact failure trigger, affected modules, and failure mechanisms.
2. **Technical Specification:** Concrete code changes, interface contracts, and invariants.
3. **Regression Test Plan:** Test file location, test cases, and assertion requirements.
4. **Verification Gates:** Linting, typing, and test execution commands.

### Engineering Rules & Invariants
- `src/main.py` remains the strict dispatch boundary.
- No UI modules from `tui/` may be imported into `src/core/`.
- Cross-component communication must use typed event models in `src/core/messaging/event_types.py`.
- Mutating tools must enforce read-before-write, sandbox containment, atomic write, and transaction rollback.
- Every change must maintain 100% green test gates across `ruff`, `mypy`, `tests/unit`, and CI workflows.

---

## Phase 1: Critical Stability Fixes (Implementation Priority 1)

### SPEC-P1.1: Fix TypeScript Sandbox Syntax Validation Failure
- **Location:** [src/core/orchestration/tool_execution_pipeline.py:651-700](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L651-L700)
- **Problem:** When writing to `.ts` or `.tsx` files, `_run_sandbox_and_snapshot` creates a temporary file forced to `suffix=".js"` and executes `node --check <tmp_path>`. Node treats the code as standard JavaScript and fails with syntax errors on valid TypeScript constructs (e.g., `interface Foo { id: string; }`, type aliases, generics). This causes the sandbox validation to return an error, hard-blocking all writes to TypeScript files.
- **Specification:**
  1. In `_run_sandbox_and_snapshot`, do not force `.js` suffix for `.ts`/`.tsx` files.
  2. For `.ts` and `.tsx` files, delegate to `src.tools.lint_dispatch.quick_lint` (which uses `tsc --noEmit`), or run `node --check` with `--experimental-strip-types` (Node 22+) if available, preserving the `.ts` extension.
  3. If `quick_lint` returns no syntax errors or linter tools are not installed, allow the write to proceed safely without false-positive blocking.
- **Complexity:** Low | **Impact:** Critical (unblocks all TypeScript project editing)
- **Regression Test:** `tests/unit/test_tool_pipeline_typescript_validation.py` asserting that `write_file` and `_run_sandbox_and_snapshot` accept valid TypeScript interfaces, types, and generic functions without syntax errors.

---

### SPEC-P1.2: Fix Cancellation Settlement Guard Commit Leak in `verification_node` & `finalize_node`
- **Location:** [src/core/orchestration/graph/nodes/verification_node.py:210-238](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/verification_node.py#L210-L238), [src/core/orchestration/graph/nodes/finalize_node.py:61-70](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/finalize_node.py#L61-L70)
- **Problem:** When a user cancels a run (`cancel_event.is_set()`), `verification_node.py` early-returns `{"verification_passed": True, "verification_result": {"cancelled": True}}`. When execution routes to `finalize_node.py`, line 61 checks:
  ```python
  can_commit = state.get("verification_passed") is True or (...)
  ```
  Because `verification_passed` was set to `True`, `can_commit` evaluates to `True`. `finalize_node` calls `commit_step_transaction()`, committing unverified, partial, and cancelled code modifications to disk when the user cancelled the task.
- **Specification:**
  1. In `verification_node.py`, return `{"verification_passed": False, "verification_result": {"cancelled": True}}` when `_is_cancelled()` is true.
  2. In `finalize_node.py`, update `can_commit` condition to explicitly check that the run was not cancelled or failed:
  ```python
  is_cancelled = run_state.get("status") == "cancelled" or (
      cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set()
  )
  can_commit = (not is_cancelled) and (
      state.get("verification_passed") is True or (
          state.get("use_verification") is False
          and run_state.get("status") not in ("failed", "cancelled")
          and run_state.get("outcome") not in ("failed", "cancelled")
      )
  )
  ```
  3. When `can_commit` is false, ensure `orchestrator.rollback_step_transaction()` executes, rolling back all unverified or cancelled changes.
- **Complexity:** Low | **Impact:** Critical (prevents corrupted workspace state on user cancellation)
- **Regression Test:** `tests/unit/test_settlement_guard_cancellation_rollback.py` asserting that cancelling an execution step with open snapshots triggers rollback and does not commit files.

---

### SPEC-P1.3: Unblock Gate 2 Approval Flow for `BashRiskLevel.DANGEROUS` Shell Commands
- **Location:** [src/tools/_bash_exec.py:544-577](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_bash_exec.py#L544-L577)
- **Problem:** In `_bash_exec.py`, Gate 2 runs AST-level security analysis. Line 547 checks:
  ```python
  if _risk_level in (BashRiskLevel.BLOCKED, BashRiskLevel.DANGEROUS):
      return {"status": "error", "error": f"Command blocked by security analysis: {'; '.join(_risk_reasons)}"}
  ```
  Commands such as `pip install`, `npm install`, `cargo install`, `curl`, and `git clean` are classified as `BashRiskLevel.DANGEROUS`. Hard-blocking them here rejects them immediately before they can reach the downstream approval gate at lines 566–577 (`requires_approval: True`). The operator is never prompted to approve package installs or network fetches.
- **Specification:**
  1. Restrict the immediate hard-block in Gate 2 strictly to `BashRiskLevel.BLOCKED`.
  2. For `BashRiskLevel.DANGEROUS`:
     - If the command requires approval, return:
       ```python
       return {
           "status": "error",
           "error": f"Command requires operator approval (risk: {'; '.join(_risk_reasons)}).",
           "requires_approval": True,
           "risk_level": "dangerous",
           "risk_reasons": _risk_reasons,
       }
       ```
     - If already user-approved (`user_approved=True`), permit the command to proceed to execution in sandbox.
- **Complexity:** Low | **Impact:** High (allows approved package installations and tool setups)
- **Regression Test:** `tests/unit/test_bash_dangerous_approval_flow.py` asserting that `pip install` returns `requires_approval: True` rather than an outright unrecoverable error.

---

### SPEC-P1.4: Drain Mid-Run In-Flight Steering Injections in `frontier_loop_node`
- **Location:** [src/core/orchestration/graph/nodes/frontier_loop_node.py:528-720](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/frontier_loop_node.py#L528-L720), [tui/tui_src/ui/_bridge_agent.py:41-50](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L41-L50)
- **Problem:** When an operator types a prompt while the agent is running, `_bridge_agent.py` buffers it into `self._pending_injections`. The orchestrator passes `_pending_injections_source` into the initial graph state. However, `frontier_loop_node.py` never calls `pop_pending_injections()` during its multi-turn loop. On the next task, `send_prompt()` wipes `_pending_injections`, completely dropping the operator's guidance.
- **Specification:**
  1. At the start of each turn in the `frontier_loop_node` while-loop, check `state.get("_pending_injections_source")` or `getattr(orchestrator, "_injection_source", None)`.
  2. If present and has callable `pop_pending_injections`, drain all pending user injections:
     ```python
     inj_source = state.get("_pending_injections_source") or getattr(orchestrator, "_injection_source", None)
     if inj_source and hasattr(inj_source, "pop_pending_injections"):
         pending_msgs = inj_source.pop_pending_injections()
         for pmsg in pending_msgs:
             history.append({
                 "role": "user",
                 "content": f"<operator_steering>\n{pmsg}\n</operator_steering>",
             })
     ```
  3. Emit an event or log message notifying that operator mid-run guidance was incorporated.
- **Complexity:** Low | **Impact:** High (enables real-time mid-run steering)
- **Regression Test:** `tests/unit/test_frontier_loop_mid_run_injection.py` asserting that buffered messages are popped and appended to `history` on turn 2.

---

### SPEC-P1.5: Fix Session Resumption Conversation History Erasure in `session_list.py`
- **Location:** [tui/tui_src/ui/screens/session_list.py:266-283](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/screens/session_list.py#L266-L283), [tui/tui_src/ui/_bridge_session.py:281-290](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_session.py#L281-L290)
- **Problem:** In `_resume_selected()`, the code loads history into `bridge.history`, then calls `bridge.start_new_session()`. `start_new_session()` calls `orch.start_new_task()` (which clears `msg_mgr.messages`) and emits `session.new` (which clears `#chat_log` DOM). Resuming a session completely wipes both the UI and the orchestrator history.
- **Specification:**
  1. In `_resume_selected()`, do not call `bridge.start_new_session()`.
  2. Instead, introduce a dedicated `bridge.resume_session(session_id, messages, working_dir)` method:
     - Hydrate `bridge.history` with historical messages.
     - Hydrate `orchestrator.msg_mgr.messages` with `[{"role": m["role"], "content": m["content"]} ...]`.
     - Rebind `bridge.working_dir` and `orchestrator.working_dir`.
     - Emit a `session.hydrated` event so the TUI mounts the restored messages into `#chat_log` and updates the sidebar.
- **Complexity:** Medium | **Impact:** High (makes session browser functional for resumption)
- **Regression Test:** `tests/unit/test_tui_session_resumption.py` asserting that resuming a session preserves loaded messages in both bridge history and orchestrator message manager.

---

### SPEC-P1.6: Fix `PreviewConfirmed` and `PreviewRejected` Event Dataclass Parameter Mismatch
- **Location:** [src/core/messaging/event_types.py:354-377](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/messaging/event_types.py#L354-L377), [tui/tui_src/ui/_bridge_tools.py:288-304](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_tools.py#L288-L304)
- **Problem:** `confirm_file_preview(path)` and `reject_file_preview(path)` call `self._bus.publish_typed(PreviewConfirmed(path=path))` and `PreviewRejected(path=path)`. In `event_types.py`, both dataclasses only declare `preview_id: str`, causing a `TypeError: unexpected keyword argument 'path'`. Diff preview confirmation crashes, leaving file writes hung.
- **Specification:**
  1. In `src/core/messaging/event_types.py`, update `PreviewConfirmed` and `PreviewRejected`:
  ```python
  @dataclass
  class PreviewConfirmed(Event):
      preview_id: str = ""
      path: Optional[str] = None

  @dataclass
  class PreviewRejected(Event):
      preview_id: str = ""
      path: Optional[str] = None
  ```
  2. In `_bridge_tools.py`, pass `preview_id=path, path=path` so listeners querying either attribute succeed.
- **Complexity:** Low | **Impact:** High (fixes diff preview approval deadlock)
- **Regression Test:** `tests/unit/test_preview_confirmed_event_signature.py` asserting instantiation with `path` and `preview_id`.

---

### SPEC-P1.7: Implement Pre-Write AST Validation for `edit_file_atomic`
- **Location:** [src/core/orchestration/tool_execution_pipeline.py:634-650](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L634-L650)
- **Problem:** `_run_sandbox_and_snapshot` checks `new_content = args.get("content")`. `edit_file_atomic` takes `path`, `old_string`, `new_string`, so `new_content` is `None`. Pre-write AST syntax validation is bypassed, allowing syntax errors to corrupt files on disk.
- **Specification:**
  1. In `_run_sandbox_and_snapshot`:
  ```python
  new_content = args.get("content")
  if new_content is None and name == "edit_file_atomic" and path_arg:
      old_str = args.get("old_string") or ""
      new_str = args.get("new_string") or ""
      target_file = Path(path_arg)
      if target_file.is_file():
          try:
              orig = target_file.read_text(encoding="utf-8")
              if old_str in orig:
                  new_content = orig.replace(old_str, new_str, 1)
          except Exception:
              pass
  ```
  2. Validate the computed `new_content` with Python `ast.parse()` or language linters prior to write.
- **Complexity:** Low | **Impact:** High (prevents broken Python/JS syntax via atomic edits)
- **Regression Test:** `tests/unit/test_edit_file_atomic_ast_validation.py` asserting that passing malformed Python syntax to `edit_file_atomic` is blocked by sandbox validation.

---

### SPEC-P1.8: Fix `PlanRequested` Attribute Mismatch & List Formatting in TUI
- **Location:** [tui/tui_src/ui/_bridge_tools.py:198-202](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_tools.py#L198-L202), [tui/tui_src/ui/components/chat_mixin.py:371-393](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/chat_mixin.py#L371-L393)
- **Problem:** `wait_for_user_node.py` publishes `PlanRequested(plan=state.get("current_plan"))` where `plan` is a list of step dicts. `_bridge_tools.py` passes this list directly as `plan_text`. `chat_mixin.py:376` checks `if event.plan_text:` and mounts raw dict representations, which can cause Rich markup parse errors.
- **Specification:**
  1. In `_bridge_tools.py:_on_plan_requested`, convert structured plan lists to formatted Markdown bullet points if `payload.get("plan")` is a list.
  2. In `chat_mixin.py:_chat_handle_plan_requested`, check both `getattr(event, "plan_text", None)` and `getattr(event, "plan", None)` and escape with `rich.markup.escape()`.
- **Complexity:** Low | **Impact:** High (plan approval dialog displays cleanly formatted steps)
- **Regression Test:** `tests/unit/test_plan_requested_rendering.py` asserting that list and string plan payloads format cleanly.

---

## Phase 2: Robustness & Safety Improvements

### SPEC-P2.1: Escape Rich Markup in Diff Viewers (`SideBySideDiff` & `InlineDiff`)
- **Location:** [tui/tui_src/ui/components/diff_viewer.py:67-70,178-180](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/diff_viewer.py#L67-L70)
- **Specification:** Wrap removed and added lines in `rich.markup.escape(line)` before styling with `[on #3d0000]...[/]` to prevent `MarkupError` on brackets like `list[str]` or `data[0]`.

### SPEC-P2.2: Rebind Working Directory on `/worktree create`
- **Location:** [tui/tui_src/ui/_app_slash_commands_mixin.py:986-1008](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_slash_commands_mixin.py#L986-L1008)
- **Specification:** When `GitWorktreeManager.create()` succeeds, set `self._bridge.working_dir = wt_path` and `orch.working_dir = wt_path`, update `#sb_git`, and register cleanup handlers on session exit.

### SPEC-P2.3: Fix Role Tool Lookup Fallback Crash
- **Location:** [tui/tui_src/ui/_bridge_agent.py:115-119](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L115-L119)
- **Specification:** Replace `self._orchestrator.get_tools_for_role("operational")` in the ternary fallback with `getattr(self._orchestrator, "get_tools", lambda: [])()` or `None`.

### SPEC-P2.4: Wire User Denial Feedback into Model Context
- **Location:** [src/core/orchestration/orchestrator_event_subscriptions.py:261-277](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/orchestrator_event_subscriptions.py#L261-L277)
- **Specification:** Route denial feedback to `orch.msg_mgr.append("user", f"Tool permission denied. Feedback: {feedback}")` instead of querying non-existent `orch.state`.

### SPEC-P2.5: Connect Diff Preview Gate to Typed `MessageBus` Subscribers
- **Location:** [src/tools/_edit_tools.py:583](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_edit_tools.py#L583), [src/tools/_file_io.py:257](file:///Users/tann200/PycharmProjects/CodingAgent/src/tools/_file_io.py#L257)
- **Specification:** Query both `_get_bus().has_subscribers("file.diff.preview")` and `has_typed_subscribers("FileDiffPreview")` before bypassing the preview gate.

### SPEC-P2.6: Protect Running Thread Event Loops in `_dispatch_tool_call`
- **Location:** [src/core/orchestration/tool_execution_pipeline.py:868-885](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/tool_execution_pipeline.py#L868-L885)
- **Specification:** When an awaitable tool runs in a worker thread with an existing running loop, use `asyncio.run_coroutine_threadsafe(rv, loop).result()` instead of overwriting and closing the thread loop.

---

## Phase 3: Capability Improvements

- **SPEC-P3.1:** Eliminate double-response rendering between `StreamView` and `AgentArtifact`.
- **SPEC-P3.2:** Dynamically scale context window limit in `#sb_tokens` based on provider profile.
- **SPEC-P3.3:** Asynchronous session list scanning in `SessionListScreen` to prevent UI stalls.
- **SPEC-P3.4:** Intelligent targeted verification scoping for single-file frontend edits.
- **SPEC-P3.5:** Deduplicate syntax validation passes between `write_file` and tool pipeline.

---

## Phase 4: Long-Term Architecture

- **SPEC-P4.1:** Multi-file transaction settlement DAGs in `RollbackManager`.
- **SPEC-P4.2:** Headless Textual pilot test suite (`App.run_test()`).
- **SPEC-P4.3:** Unified slash command and command palette dispatch service.
