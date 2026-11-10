# Textual UI (TUI) Subsystem Engineering Audit (2026-11-09)

**Subsystem:** Textual User Interface (`tui/`) & UI-to-Core Bridge  
**Auditor:** Antigravity Advanced Agentic Engineering Team  
**Evaluation Date:** November 9, 2026  
**Status:** Deep Inspection Completed — Actionable Remediation Defined  

---

## 1. Executive Summary

A comprehensive architectural and code-level audit was conducted across the Textual UI subsystem (`tui/`), encompassing `tui/tui_src/ui/core_bridge.py`, bridge modules (`_bridge_agent.py`, `_bridge_session.py`, `_bridge_tools.py`), application mixins (`_app_tool_handlers_mixin.py`, `_app_slash_commands_mixin.py`, `_app_session_mixin.py`), screens (`session_screen.py`, `session_list.py`, `settings_screen.py`), and widgets (`diff_viewer.py`, `file_picker.py`, `chat_mixin.py`).

The TUI provides a responsive, terminal-native developer environment with token tracking, diff viewers, approval flows, and slash commands. However, the audit revealed several critical vulnerabilities:
1. **Cross-thread event loop desynchronization** in `core_bridge.py` when background worker threads run local event loops, causing UI callbacks to be scheduled onto background loops rather than the Textual main loop.
2. **Unescaped Rich markup injection** in tool notification widgets, diff headers, file pickers, and slash command echo handlers, leading to unhandled `MarkupError` exceptions when processing paths with bracket syntax (e.g. Next.js `[slug]/page.tsx` routes) or bracketed shell commands.
3. **Session fork identity collision** in `session_screen.py` retaining ancestral `session_id`s across forks.
4. **Main-thread blocking disk traversal** in the `@file` autocomplete picker during workspace discovery.
5. **Synchronous Git checkout executions** in session revert dialogs risking detached-HEAD repository states.

---

## 2. Architecture Strengths

1. **Strict UI/Core Decoupling:**
   The TUI codebase lives entirely under `tui/` and communicates with `src/core/` exclusively through `core_bridge.py` and typed events on the `MessageBus`. Zero core modules import UI components, upholding the architecture invariant.
2. **Modular App Architecture:**
   `AgentApp` delegates domain responsibilities cleanly across mixins (`_app_tool_handlers_mixin.py`, `_app_slash_commands_mixin.py`, `_app_session_mixin.py`, `chat_mixin.py`), keeping source files manageable.
3. **Reactive Layout & Token Telemetry:**
   Textual reactive attributes (`watch_context_window`, `watch_total_tokens`) ensure responsive status bar updates without polling.
4. **Unified Palette & Slash Command Dispatch:**
   Command palette integration cleanly surfaces slash command actions and modal transitions with fuzzy filtering.

---

## 3. Critical Architectural Flaws

### Issue TUI-C1: Cross-Thread Event Loop Desynchronization in `core_bridge.py`
- **Location:** `tui/tui_src/ui/core_bridge.py:304-319`
- **Severity:** Critical
- **Failure Mechanism:**
  `_schedule_callback` retrieves `loop = asyncio.get_running_loop()`. When called from a background worker thread executing its own event loop (e.g. LangGraph graph node execution, `asyncio.run()`, or async inference worker), `get_running_loop()` returns that worker thread's event loop. Calling `loop.call_soon(fn, *args)` schedules the UI callback on the worker thread, causing `app.post_message` to run off-thread without `call_from_thread`.
- **Impact:** Dropped UI messages, silent state desynchronization, and random deadlocks in terminal event dispatch.
- **Remediation:** Verify `loop is getattr(self.app, "_loop", None)`. If different or `RuntimeError`, call `self.app.call_from_thread(fn, *args)`.

### Issue TUI-C2: Unescaped Rich Markup Injection in Tool Notices & Slash Handlers
- **Location:** `tui/tui_src/ui/_app_tool_handlers_mixin.py:218,554,641`, `tui/tui_src/ui/_app_slash_commands_mixin.py:244`
- **Severity:** Critical
- **Failure Mechanism:**
  Tool commands, arguments, and unrecognized slash inputs containing brackets (`[id]`, `[slug]/route.ts`, `[foo]`) are directly interpolated into f-strings passed to `Static(..., markup=True)` or `Label(..., markup=True)` without `rich.markup.escape()`.
- **Impact:** Crashes the Textual application with unhandled `rich.errors.MarkupError` when editing modern frontend routes or running bracketed shell commands.
- **Remediation:** Wrap all dynamic input strings with `rich.markup.escape()`.

---

## 4. High-Risk Safety Issues

### Issue TUI-H1: Session Fork ID Collision in `SessionScreen`
- **Location:** `tui/tui_src/ui/screens/session_screen.py:161-168`
- **Severity:** High
- **Failure Mechanism:**
  `action_fork_session` copies ancestral metadata via `fork_data = dict(data)`, preserving `fork_data["session_id"]`.
- **Impact:** Forked sessions overwrite the parent session's transaction checkpoints and message history upon resumption, violating workspace isolation.
- **Remediation:** Assign `fork_data["session_id"] = str(uuid.uuid4())` on every fork operation.

### Issue TUI-H2: Unescaped File Paths in Diff Viewers
- **Location:** `tui/tui_src/ui/components/diff_viewer.py:128,224`, `tui/tui_src/ui/components/file_picker.py:45,47`
- **Severity:** High
- **Failure Mechanism:**
  `SideBySideDiff`, `InlineDiff`, and `FilePicker` render candidate paths with `[bold]...[/]` without escaping the path variable. Paths containing `[id]` crash diff review.
- **Remediation:** Apply `rich.markup.escape()` to paths in diff headers and picker rows.

---

## 5. Major Missing Capabilities

1. **Custom Session Rename Persistence:**
   While `/rename` sets `self.sub_title`, `_save_session_snapshot` overwrites `task_name` with `first_user` on subsequent turns, discarding user-specified names.
2. **Safe Detached-HEAD Git Reversion:**
   `SessionScreen._do_revert` executes raw `git checkout <sha>` without creating a safety branch, leaving operators in detached-HEAD mode.

---

## 6. Workflow Reliability Issues

1. **Double-Message Prevention on Resumption:**
   Restoring sessions must cleanly hydrate DOM elements without duplicating system welcome banners or active stream views.
2. **Subagent Approval Notification Routing:**
   Approvals spawned by subagents must clear pending permission badges reliably when handled via keyboard shortcuts.

---

## 7. Tool System Weaknesses (UI Interface)

1. **Tool Output Truncation & ANSI Stripping:**
   Excessively verbose tool outputs (>100KB) can bog down Textual DOM rendering if not pre-truncated and stripped of malformed ANSI terminal sequences.

---

## 8. Repository Awareness Gaps

1. **Synchronous File Discovery Blocking UI Thread:**
   In `chat_mixin.py:163-176`, `_list_workspace_files` calls synchronous `os.walk` directly on the Textual event loop when cache expires, causing frame drops during typing.
   - **Remediation:** Offload disk walking to `asyncio.to_thread` with non-blocking cache updates.

---

## 9. Memory System Evaluation

1. **Snapshot Redaction in TUI Session Storage:**
   The TUI session snapshot correctly routes payloads through `redact_data` before writing to disk, protecting sensitive tokens from being persisted in local history.

---

## 10. Evaluation and Testing Gaps

1. **Headless Pilot Test Coverage:**
   Prior to recent additions, modal transitions and interactive approval flows lacked headless driver tests (`app.run_test()`).

---

## 11. Usability Problems

1. **Disjoint Slash Command and Palette Registries:**
   Operators could trigger `/worktree` or `/diff` in chat, but these actions were missing from the fuzzy command palette (`ctrl+p`).

---

## 12. Performance Bottlenecks

1. **Large Workspace File Indexing:**
   On workspaces with deeply nested directory structures, recursive directory traversal without depth capping causes CPU spikes on the UI thread.

---

## 13. Over-Engineered Components

1. **Multiple Competing Diff Viewer Modes:**
   Maintaining both full side-by-side and inline diffs adds complexity; keeping line rendering unified under shared parser helpers simplifies maintenance.

---

## 14. Prioritized Fix List & Roadmap

### Phase 1 — Critical Stability & Thread Safety
1. **Fix TUI Event Loop Bridging:** Update `core_bridge.py:_schedule_callback` to enforce `loop is getattr(self.app, "_loop", None)` check before calling `call_soon`, delegating to `call_from_thread` otherwise.
2. **Escape Rich Markup Everywhere:** Apply `rich.markup.escape()` across `_app_tool_handlers_mixin.py`, `diff_viewer.py`, `file_picker.py`, and `_app_slash_commands_mixin.py`.
3. **Session Fork ID Isolation:** Generate fresh UUIDs in `SessionScreen.action_fork_session`.

### Phase 2 — UX Robustness & Non-Blocking I/O
1. **Asynchronous File Picker Scanning:** Offload `os.walk` in `chat_mixin.py` to `asyncio.to_thread`.
2. **Session Custom Title Persistence:** Wire `self._session_custom_title` into `_save_session_snapshot`.
3. **Async Revert Safety:** Wrap `git checkout` in `asyncio.to_thread` with safety branch preservation.
