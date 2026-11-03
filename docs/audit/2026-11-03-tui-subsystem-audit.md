# Comprehensive Engineering Audit: Textual UI (TUI) Subsystem

**Date:** 2026-11-03  
**Auditor:** Antigravity Autonomous Systems Engineering Team  
**Scope:** TUI Architecture, Textual UI App & Bridge subsystems (`tui/tui_src`, `tui/src`, screens, modals, widgets, terminal integration, message-bus bridging, and UI/orchestration interfaces).  
**Methodology:** Full-spectrum static analysis, runtime call-graph verification, event-bus routing matrix inspection, and multi-turn state lifecycle tracing against [audit-instructions.md](file:///Users/tann200/PycharmProjects/CodingAgent/docs/audit/audit-instructions.md).

---

## 1. Executive Summary

The Textual User Interface (TUI) is the primary interactive operator console for CodingAgent. It is engineered with modern Textual paradigms, reactive attributes, decoupled mixin modules, a dual-publishing message bus adapter, and rich visual components (side-by-side diffing, live token meters, subagent dashboards, inline slash palettes, and OAuth device flow screens).

Despite these robust UI primitives, this audit has uncovered **multiple critical architectural flaws, silent data loss bugs, and runtime disconnects** that undermine system determinism, operator control, and safety:

1. **Silent Loss of In-Run User Steering:** Operator messages submitted while the agent is running are captured into a mid-run buffer and passed as `_pending_injections_source` into the initial graph state. However, the cognitive graph loop ([frontier_loop_node.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/frontier_loop_node.py)) **never polls or drains this buffer**. The user's guidance is completely ignored during execution and wiped on the next turn.
2. **Session Resumption State Erasure:** Resuming a persisted session via [SessionListScreen](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/screens/session_list.py#L266-L283) loads historical messages into the bridge, but immediately calls `bridge.start_new_session()`. This triggers `session.new`, which wipes the chat panel DOM and calls `orchestrator.start_new_task()`, which clears `msg_mgr.messages`. Resuming a session completely obliterates both the UI history and the model context.
3. **Approval Gate Deadlocks & Event Typing Mismatches:** File diff preview confirmations throw unhandled `TypeError` exceptions (`PreviewConfirmed(path=path)` vs `preview_id: str`), deadlocking writes behind a 5-minute timeout. Plan approval events (`PlanRequested`) suffer from an attribute mismatch (`plan` vs `plan_text`), rendering blank plan approval dialogs. Tool permission grants (`tool.permission_granted`) are omitted from `_EVENT_IMPORT_PATHS`, disconnecting typed message-bus listeners. Operator denial feedback is discarded because `_on_denial_feedback` expects an `orch.state` attribute that does not exist in LangGraph.
4. **Git Worktree Isolation Illusion:** The `/worktree create` slash command successfully invokes `GitWorktreeManager.create()`, but fails to rebind `bridge._working_dir` or `orchestrator.working_dir`. Subsequent tool executions continue to mutate the primary workspace, leaving orphaned directories on disk.

**Deployment Verdict:** Not ready for autonomous or mission-critical development workflows. Immediate remediation of the Phase 1 stability defects is required to prevent operator frustration, silent mid-run steering failure, and session data loss.

---

## 2. Architecture Strengths

- **Clean Mixin Decomposition:** `AgentApp` has been refactored into focused mixins ([_app_message_handlers_mixin.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_message_handlers_mixin.py), [_app_tool_handlers_mixin.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_tool_handlers_mixin.py), [_app_session_mixin.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_session_mixin.py), [_app_slash_commands_mixin.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_slash_commands_mixin.py), [chat_mixin.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/chat_mixin.py)) with strict protocols ([_app_protocol.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_protocol.py)), keeping core classes maintainable and under 400 lines.
- **Thread Boundary Isolation:** Agent execution occurs strictly in background worker threads ([_bridge_agent.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L60-L130)) managed by a `ThreadPoolExecutor`, preventing blocking calls from freezing Textual's async event loop.
- **Dual-Publishing Message Architecture:** The `AgentBridge` ([core_bridge.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/core_bridge.py)) routes events across both legacy string-based `EventBus` topics and typed `MessageBus` subscriptions ([_bridge_subscriptions.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_subscriptions.py)).
- **Rich Interactive Visualizations:** Built-in modal dialogs for session browsing ([SessionScreen](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/screens/session_screen.py)), settings configuration ([SettingsScreen](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/features/settings/screen.py)), command palettes ([CommandPalette](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/features/palette/screen.py)), side-by-side diff viewers ([SideBySideDiff](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/diff_viewer.py)), and thinking accordions ([ThinkingProcess](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/thinking.py)).
- **Defensive DOM Pruning:** Automatic chat log bounding ([chat_mixin.py:114-120](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/chat_mixin.py#L114-L120)) limits the DOM to 200 child widgets, preventing terminal emulator memory bloat.

---

## 3. Critical Architectural Flaws

| Severity | Finding | Evidence | Trigger and Impact | Fix and Regression Test |
| :--- | :--- | :--- | :--- | :--- |
| **Critical** | Mid-run user prompts are captured into `_pending_injections` but never polled or drained by the execution graph, causing silent loss of operator steering. | [tui/tui_src/ui/_bridge_agent.py:41-50](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L41-L50); [src/core/orchestration/inference_loop_state.py:188](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/inference_loop_state.py#L188); [src/core/orchestration/graph/nodes/frontier_loop_node.py:528-720](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/frontier_loop_node.py#L528-L720) | User types a clarifying prompt while the agent is running. `send_prompt` appends it to `self._pending_injections` and returns `False`. The bridge passes `_pending_injections_source` into initial state, but `frontier_loop_node` never checks or pops it. When the next run starts, `send_prompt` wipes the buffer. User steering is silently dropped forever. | Add turn-boundary injection polling in `frontier_loop_node.py` via `state.get('_pending_injections_source').pop_pending_injections()` and append as `<operator_steering>` user message. Test: `tests/unit/test_mid_run_injection_drain.py`. |
| **Critical** | Resuming a session via `SessionListScreen` / `SessionScreen` wipes conversation history and model context instead of restoring it. | [tui/tui_src/ui/screens/session_list.py:266-283](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/screens/session_list.py#L266-L283); [tui/tui_src/ui/_bridge_session.py:281-290](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_session.py#L281-L290); [tui/tui_src/ui/_app_session_mixin.py:153-157](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_session_mixin.py#L153-L157) | User selects a previous session from the session browser and presses Enter. `_resume_selected` sets `bridge.history`, then calls `bridge.start_new_session()`. This calls `orch.start_new_task()` (clearing `msg_mgr.messages`) and emits `session.new` (calling `_clear_chat_panel()`). The user is left in an empty, cleared session with 0 restored messages. | Update `_resume_selected()` to hydrate orchestrator message manager, mount historical messages into `#chat_log`, and preserve `bridge.history` without calling `start_new_session()`. Test: `tests/unit/test_tui_session_resumption.py`. |
| **High** | Fallback role tool lookup in `_bridge_agent.py` crashes with `AttributeError` when orchestrator lacks `get_tools_for_role`. | [tui/tui_src/ui/_bridge_agent.py:115-119](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L115-L119) | If `hasattr(self._orchestrator, "get_tools_for_role")` is `False`, the ternary `else` branch attempts to evaluate `self._orchestrator.get_tools_for_role("operational")`, immediately throwing `AttributeError`. | Change fallback to `getattr(self._orchestrator, "get_tools", lambda: [])()` or `None`. Test: `tests/unit/test_bridge_agent_role_fallback.py`. |
| **High** | Git worktree isolation in `/worktree create` is disconnected from tool execution, mutating the primary workspace. | [tui/tui_src/ui/_app_slash_commands_mixin.py:986-1008](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_slash_commands_mixin.py#L986-L1008) | User runs `/worktree create task-123`. A new worktree directory is created under `/tmp/` or `.git/worktrees`, but `self._bridge._working_dir` and `orch.working_dir` are never updated. Tools (`write_file`, `bash`) execute in the original workspace. The temporary worktree remains orphaned on disk. | Rebind `self._bridge.working_dir = wt_path` and `orch.working_dir = wt_path`, update status bar `#sb_git`, and register session cleanup hooks. Test: `tests/unit/test_slash_worktree_binding.py`. |
| **High** | `PreviewConfirmed` and `PreviewRejected` event emission crashes with `TypeError` on diff confirmation. | [tui/tui_src/ui/_bridge_tools.py:288-304](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_tools.py#L288-L304); [src/core/messaging/event_types.py:354-377](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/messaging/event_types.py#L354-L377) | When user clicks "Accept" or "Reject" on a diff preview, `confirm_file_preview(path)` calls `PreviewConfirmed(path=path)`. `PreviewConfirmed` only accepts `preview_id: str`. Python raises `TypeError: unexpected keyword argument 'path'`. Write operations remain blocked until the 300s timeout. | Update `PreviewConfirmed` and `PreviewRejected` dataclasses in `event_types.py` to support `preview_id: str = "", path: Optional[str] = None`. Test: `tests/unit/test_diff_preview_event_types.py`. |
| **High** | Plan approval UI breaks due to `PlanRequested` attribute mismatch (`plan` vs `plan_text`). | [tui/tui_src/ui/components/chat_mixin.py:371-393](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/chat_mixin.py#L371-L393); [tui/tui_src/ui/_bridge_tools.py:198-202](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_tools.py#L198-L202); [src/core/messaging/event_types.py:380-393](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/messaging/event_types.py#L380-L393) | Graph publishes typed `PlanRequested(plan=state['current_plan'])` (a `list[dict]`). Bridge forwards `PlanRequestedEvent(plan_text=payload.get('plan'))`. `_chat_handle_plan_requested` expects a string `event.plan_text`. Raw list formatting or markup parse errors cause empty or corrupted plan boxes. | Format structured plan lists into Markdown before publishing or in `_chat_handle_plan_requested`, supporting both string and list payloads. Test: `tests/unit/test_plan_requested_rendering.py`. |
| **High** | Operator tool permission decisions are lost on typed message bus due to missing event import mappings. | [tui/tui_src/ui/_app_tool_handlers_mixin.py:653,698](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_tool_handlers_mixin.py#L653); [src/core/orchestration/event_bus.py:203-240](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/event_bus.py#L203-L240) | Operator clicks "Allow" or "Deny" on tool approval prompt. TUI publishes `"tool.permission_granted"` / `"tool.permission_denied"`. `_EVENT_IMPORT_PATHS` in `event_bus.py` omits these strings. In environments with a typed MessageBus, these decisions are not emitted to typed subscribers. | Add `tool.permission_granted` and `tool.permission_denied` entries to `_EVENT_IMPORT_PATHS` and declare `ToolPermissionGranted` / `ToolPermissionDenied` in `event_types.py`. Test: `tests/unit/test_tool_permission_event_routing.py`. |

---

## 4. High-Risk Safety Issues

| Severity | Finding | Evidence | Trigger and Impact | Fix and Regression Test |
| :--- | :--- | :--- | :--- | :--- |
| **High** | User denial feedback is silently dropped and never injected into agent context. | [src/core/orchestration/orchestrator_event_subscriptions.py:261-277](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/orchestrator_event_subscriptions.py#L261-L277) | When an operator clicks "Deny" on a tool call, the TUI opens a feedback input. When submitted, `_on_denial_feedback` runs `if hasattr(orch, "state") and "history" in orch.state: orch.state["history"].append(...)`. In LangGraph, `Orchestrator` has no `.state` attribute. The feedback is silently dropped, and the agent repeats the failing strategy without knowing why it was denied. | Route denial feedback directly into `orch.msg_mgr.append("user", ...)` or into the active graph state injection buffer. Test: `tests/unit/test_denial_feedback_injection.py`. |
| **High** | Session revert command (`/revert` / `SessionScreen` `r` key) leaves workspace in detached HEAD state and risks discarding uncommitted work. | [tui/tui_src/ui/screens/session_screen.py:224-245](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/screens/session_screen.py#L224-L245) | User triggers session revert. Screen runs raw `subprocess.run(["git", "stash", "--include-untracked"])` followed by `subprocess.run(["git", "checkout", git_sha])`. If stash fails, checkout runs anyway. Checkout detaches HEAD without warning. Subsequent commits by the agent can be lost when branches switch. | Check stash success; create a named recovery branch (e.g. `recovery/session-<id>`) rather than detaching HEAD; execute git commands via sandboxed runner. Test: `tests/unit/test_session_revert_safety.py`. |
| **Medium** | Unescaped Rich markup in diff viewers crashes TUI or renders corrupted code. | [tui/tui_src/ui/components/diff_viewer.py:67-69,178-180](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/diff_viewer.py#L67-L69) | Agent edits code containing square brackets, such as `types[key]` or `list[str]`. `_parse_sidebyside` and `_render_inline` interpolate lines into Rich markup strings (`f"[on #3d0000]{r} [/]"`) without escaping. Textual's Rich parser throws `MarkupError` or misparses tags, breaking screen rendering. | Wrap all code lines in `rich.markup.escape(line)` before applying styling tags. Test: `tests/unit/test_diff_viewer_markup_escaping.py`. |

---

## 5. Major Missing Capabilities

1. **Real-Time Mid-Run Steering:** Modern agent interfaces (OpenCode, Claude Code) permit the operator to interrupt or nudge the agent while an execution wave is running. CodingAgent has the TUI input buffering ready, but the cognitive loop completely lacks the consumption side.
2. **Interactive Worktree Switcher:** While Git worktree manager backend exists, the TUI has no interactive screen or status bar selector to switch between active worktrees or review diffs between worktrees.
3. **Structured Tool Approval Inspection:** Tool approval prompts show truncated one-line string arguments (`_fmt_args`). Operators cannot view multi-line scripts or formatted payloads before granting permissions.
4. **Session Fork & Replay Visualizer:** The session browser supports JSON snapshot loading, but cannot visualize branching session forks, execution DAGs, or rollback checkpoints.

---

## 6. Workflow Reliability Issues

| Severity | Finding | Evidence | Trigger and Impact | Fix and Regression Test |
| :--- | :--- | :--- | :--- | :--- |
| **Medium** | Double response rendering: `StreamView` and `AgentArtifact` both render complete assistant text. | [tui/tui_src/ui/components/chat_mixin.py:286-294,309-318](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/chat_mixin.py#L286-L294); [tui/tui_src/ui/_bridge_agent.py:142](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L142) | Streaming chunk events mount a `StreamView` into the chat log. When execution finishes, `_bridge_agent` posts `AgentFinalResponse`. `_chat_handle_final_response` does not remove or convert `StreamView`; it mounts an `AgentArtifact` below it, causing the entire response to appear twice in the chat log. | In `_finalize_stream()`, convert the active `StreamView` into a final artifact or detach `StreamView` before mounting `AgentArtifact`. Test: `tests/unit/test_tui_response_deduplication.py`. |
| **Medium** | Race condition in `_drain_message_queue` buffers follow-up tasks into dead injection buffer. | [tui/tui_src/ui/_app_message_handlers_mixin.py:80-88](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_message_handlers_mixin.py#L80-L88); [tui/tui_src/ui/_bridge_agent.py:41-45](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L41-L45) | Multiple messages queued in `_queued_messages`. `_drain_message_queue` checks `while self._queued_messages and not self.agent_running:`. The first `send_prompt` starts the background thread and sets `bridge._agent_running = True`, but `app.agent_running` only updates asynchronously on the next Textual event tick. The while-loop immediately pops the second message and calls `send_prompt`, which shunts it into `_pending_injections` where it is lost. | Update `self.agent_running = True` synchronously or await task completion before popping the next message. Test: `tests/unit/test_message_queue_drain_race.py`. |
| **Medium** | Slash command `/undo` confirmation screen lacks keyboard accessibility. | [tui/tui_src/ui/_app_slash_commands_mixin.py:254-280](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_slash_commands_mixin.py#L254-L280) | `_UndoConfirmScreen` relies on mouse button clicks (`btn_undo_yes`, `btn_undo_no`) without handling Enter or Escape keys, trapping keyboard-only operators. | Add `on_key` handlers for `enter`, `y`, `n`, `escape`. Test: `tests/unit/test_undo_modal_keybindings.py`. |

---

## 7. Tool System Weaknesses (TUI Representation)

- **Untyped String Arguments in Approval Prompt:** Tool arguments are passed to the UI as arbitrary dicts and rendered with a crude regex/truncate helper ([_fmt_args](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_tool_handlers_mixin.py#L22-L40)). Complex arguments with nested JSON or multiline code blocks are clipped at 120 characters without an expander widget.
- **Missing Inline Tool Output Collapse:** Inline tool results are rendered as expanded text blocks. In runs with 15+ tool calls, this quickly exceeds viewport height and pushes conversation messages off-screen.
- **Approval Badge Disconnect:** The permission badge `#perm_badge` in the header increments and decrements via `_update_perm_badge()`, but if an approval dialog is dismissed or cleared via `/clear`, the counter remains stuck at a non-zero number.

---

## 8. Repository Awareness Gaps

- **Workspace File Picker Cache Invalidation:** The `@file` completion picker ([chat_mixin.py:150-180](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/chat_mixin.py#L150-L180)) caches file paths for 60 seconds with `max_results=30`. When the agent creates new files or deletes existing files during execution, the picker continues to suggest stale or non-existent files.
- **Git Branch Indicator Stale:** The `#sb_git` widget in the sidebar only updates when explicit `GitBranch` events are received. Switching branches externally or via `/revert` does not trigger an index or sidebar refresh.

---

## 9. Memory System Evaluation (TUI Integration)

- **Token Meter Desynchronization:** Sidebar token counter `#sb_tokens` displays `0 / 32,000 (0.0%)` until an explicit `TokenBudgetUpdate` or `UsageTurnSummaryEvent` is posted. If the active model has a 128k or 1M context window (e.g. Gemini 1.5, Claude 3.5), the hardcoded 32,000 ceiling creates misleading gauge percentages.
- **Session Snapshot Data Completeness:** `_save_session_snapshot` captures basic message histories, but omits graph checkpoints, tool call IDs, and deferred transaction rollback states, preventing lossless session restoration.

---

## 10. Evaluation and Testing Gaps

- **Lack of Headless Textual Pilot Tests for Multi-Turn Sessions:** Existing unit tests mock `AgentBridge` or `AgentApp` with `unittest.mock.MagicMock`. There are zero automated pilot tests using `app.run_test()` to verify full multi-turn conversational flows, streaming chunk assembly, or diff confirmation resolution.
- **Unchecked Edge-Case Modals:** `ProbeResultsScreen`, `SubagentDetailScreen`, and `OAuthDeviceFlowScreen` lack programmatic dismissal and timeout tests.

---

## 11. Usability Problems

- **Diff View Line Truncation:** Side-by-side diff viewers cap display at 50 lines per panel ([diff_viewer.py:127-133](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/diff_viewer.py#L127-L133)). For large refactors, the viewer clips with `… X more lines` with no scroll capability or toggle to view full diff.
- **Modal Screen Trapping on Escape:** Certain modal dialogs do not bind `escape` to `self.dismiss()`, forcing users to use the mouse to click "Close" or restart the application.
- **Chat Input Lock Indication:** When `agent_running` is True, `#user_input` receives CSS class `.input_locked`, but provides no visual placeholder informing the user that text will be queued or buffered.

---

## 12. Performance Bottlenecks

- **Unbounded VerticalScroll DOM Children:** Although `_prune_chat_log` limits `#chat_log` to 200 items, each item is a composite widget (`Horizontal`, `Vertical`, `Static`, `Button`). On low-end terminal emulators or SSH connections, rendering 200 complex composite widgets causes perceptible typing latency.
- **Synchronous Disk I/O on Session List Rendering:** `SessionListScreen._load_sessions` ([session_list.py:110-117](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/screens/session_list.py#L110-L117)) reads and JSON-parses up to 100 session files synchronously on mount, freezing the UI for hundreds of milliseconds when session directories grow large.

---

## 13. Over-Engineered Components

- **Duplicate Bridge Implementations:** `tui/tui_src/ui/mock_engine.py` contains 1,250+ lines of mock engine logic that simulates 20+ event scenarios. While useful for manual styling, it is completely separate from standard pytest test harnesses and duplicates core orchestrator dispatch logic.
- **Triple-Nested Palette Implementations:** The codebase contains three independent palette implementations: `CommandPalette` in `features/palette/screen.py`, inline palette in `chat_mixin.py`, and slash command handler in `_app_slash_commands_mixin.py`.

---

## 14. Prioritized Fix List & Engineering Roadmap

### Phase 1 — Critical Stability Fixes (Immediate Remediation)
1. **Fix In-Run Mid-Turn Steering Loss:**
   - **Description:** Implement injection draining in `frontier_loop_node.py` so that user messages buffered in `self._pending_injections` are popped at every turn boundary and injected as an `<operator_steering>` context block.
   - **Location:** [tui/tui_src/ui/_bridge_agent.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L41-L50), [src/core/orchestration/graph/nodes/frontier_loop_node.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/nodes/frontier_loop_node.py#L528-L538)
   - **Complexity:** Low | **Impact:** High
2. **Fix Session Resumption History Erasure:**
   - **Description:** Modify `_resume_selected()` to populate `msg_mgr.messages` and mount messages into `#chat_log` without calling `start_new_session()` which triggers destructive clearing.
   - **Location:** [tui/tui_src/ui/screens/session_list.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/screens/session_list.py#L266-L283)
   - **Complexity:** Medium | **Impact:** High
3. **Fix Diff Preview Confirmation `TypeError`:**
   - **Description:** Update `PreviewConfirmed` and `PreviewRejected` event dataclasses to accept `preview_id: str = ""` and `path: Optional[str] = None` so `confirm_file_preview` succeeds without throwing keyword argument exceptions.
   - **Location:** [src/core/messaging/event_types.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/messaging/event_types.py#L354-L377)
   - **Complexity:** Low | **Impact:** High
4. **Fix Plan Approval Attribute & List Formatting:**
   - **Description:** Format structured `plan` lists into formatted text in `_on_plan_requested` and accept both `plan` and `plan_text` in `_chat_handle_plan_requested`.
   - **Location:** [tui/tui_src/ui/_bridge_tools.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_tools.py#L198-L202), [tui/tui_src/ui/components/chat_mixin.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/chat_mixin.py#L371-L393)
   - **Complexity:** Low | **Impact:** High

### Phase 2 — Robustness & Safety Improvements
5. **Escape Rich Markup in Diff Viewers:**
   - **Description:** Wrap all source code diff lines in `rich.markup.escape()` in `SideBySideDiff` and `InlineDiff` to prevent `MarkupError` on brackets.
   - **Location:** [tui/tui_src/ui/components/diff_viewer.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/diff_viewer.py#L67-L70,L178-L180)
   - **Complexity:** Low | **Impact:** Medium
6. **Connect `/worktree create` to Orchestrator Context:**
   - **Description:** Rebind `bridge.working_dir` and `orch.working_dir` when worktree is created, and register auto-cleanup on session teardown.
   - **Location:** [tui/tui_src/ui/_app_slash_commands_mixin.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_app_slash_commands_mixin.py#L986-L1008)
   - **Complexity:** Medium | **Impact:** High
7. **Fix Role Tool Lookup Fallback Crash:**
   - **Description:** Replace `self._orchestrator.get_tools_for_role("operational")` fallback with safe tool getter call.
   - **Location:** [tui/tui_src/ui/_bridge_agent.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/_bridge_agent.py#L115-L119)
   - **Complexity:** Low | **Impact:** Medium
8. **Wire Denial Feedback into Model Context:**
   - **Description:** Re-route `_on_denial_feedback` payload to `orch.msg_mgr.append("user", ...)` rather than querying nonexistent `orch.state`.
   - **Location:** [src/core/orchestration/orchestrator_event_subscriptions.py](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/orchestrator_event_subscriptions.py#L261-L277)
   - **Complexity:** Low | **Impact:** High

### Phase 3 — Capability Improvements
9. **Eliminate Double-Response Rendering:**
   - **Description:** Convert streaming view into final artifact on finish instead of duplicating content.
   - **Location:** [tui/tui_src/ui/components/chat_mixin.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/chat_mixin.py#L309-L318)
   - **Complexity:** Medium | **Impact:** Medium
10. **Dynamic Context Window Limits in Token Meter:**
    - **Description:** Dynamically bind `#sb_tokens` maximum limit to provider model capabilities rather than hardcoding 32,000.
    - **Location:** [tui/tui_src/ui/components/status_bar.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/components/status_bar.py)
    - **Complexity:** Low | **Impact:** Medium
11. **Asynchronous Session List Loading:**
    - **Description:** Offload `session_*.json` file scanning and parsing in `SessionListScreen` to a background worker thread with incremental yield.
    - **Location:** [tui/tui_src/ui/screens/session_list.py](file:///Users/tann200/PycharmProjects/CodingAgent/tui/tui_src/ui/screens/session_list.py#L101-L126)
    - **Complexity:** Medium | **Impact:** Medium

### Phase 4 — Long-Term Architecture
12. **Headless Textual Pilot Test Suite:**
    - **Description:** Build an integration test suite using Textual's `App.run_test()` driver to verify end-to-end multi-turn UI interaction, keyboard shortcuts, modal workflows, and diff approvals.
    - **Location:** `tests/integration/test_tui_pilot_suite.py`
    - **Complexity:** High | **Impact:** High
13. **Unified Command Palette & Slash Command Routing:**
    - **Description:** Unify the three redundant palette implementations into a single command dispatcher service.
    - **Location:** `tui/tui_src/ui/features/palette/` and `tui/tui_src/ui/_app_slash_commands_mixin.py`
    - **Complexity:** Medium | **Impact:** Medium
