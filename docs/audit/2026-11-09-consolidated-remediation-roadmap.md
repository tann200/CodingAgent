# Consolidated Engineering Remediation Roadmap (2026-11-09)

**Document:** Engineering Remediation Specifications & Implementation Roadmap  
**Audits Covered:**
- [docs/audit/2026-11-09-tui-subsystem-audit.md](file:///Users/tann200/PycharmProjects/CodingAgent/docs/audit/2026-11-09-tui-subsystem-audit.md)
- [docs/audit/2026-11-09-tools-and-graph-workflows-audit.md](file:///Users/tann200/PycharmProjects/CodingAgent/docs/audit/2026-11-09-tools-and-graph-workflows-audit.md)
**Status:** Phase 1, Phase 2, Phase 3, and Phase 4 Implemented & Verified in CI (Run `42895238948`)

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

## Phase 1: Critical Stability & Safety Fixes (Current Sprint)

### SPEC-P1.1: Fix Cross-Thread Event Loop Desynchronization in `core_bridge.py`
- **Location:** `tui/tui_src/ui/core_bridge.py:304-319`
- **Problem:** When called from a background thread running a worker event loop (e.g. LangGraph worker), `asyncio.get_running_loop()` returns the worker loop instead of Textual's event loop. Calling `loop.call_soon(fn, *args)` executes UI callbacks off-thread, causing dropped messages and UI deadlocks.
- **Specification:**
  Explicitly check `loop is getattr(self.app, "_loop", None)`. If different or `RuntimeError`, call `self.app.call_from_thread(fn, *args)`.
- **Complexity:** Low | **Impact:** Critical (eliminates thread race conditions and UI freezes)
- **Regression Test:** `tests/unit/test_tui_core_bridge_thread_safety.py`

---

### SPEC-P1.2: Escape Rich Markup across TUI Notifications, Diffs, and Commands
- **Location:** `tui/tui_src/ui/_app_tool_handlers_mixin.py:218,554,641`, `tui/tui_src/ui/components/diff_viewer.py:128,224`, `tui/tui_src/ui/components/file_picker.py:45,47`, `tui/tui_src/ui/_app_slash_commands_mixin.py:244`
- **Problem:** Tool commands, diff paths (e.g. `[slug]/page.tsx`), and unrecognized slash commands interpolated directly into strings formatted for `Static(..., markup=True)` trigger unhandled `MarkupError` exceptions when they contain square brackets.
- **Specification:**
  Import `from rich.markup import escape` and escape all dynamic string variables before inserting them into Rich markup strings.
- **Complexity:** Low | **Impact:** Critical (prevents UI crashes on modern frontend project paths)
- **Regression Test:** `tests/unit/test_tui_rich_markup_escaping.py`

---

### SPEC-P1.3: Fix Session Fork ID Collision in `SessionScreen`
- **Location:** `tui/tui_src/ui/screens/session_screen.py:161-168`
- **Problem:** `fork_data = dict(data)` preserves the ancestor session's `session_id`. When resumed, the forked session overwrites the original session's snapshots and message logs.
- **Specification:**
  Import `uuid` and assign `fork_data["session_id"] = str(uuid.uuid4())`.
- **Complexity:** Low | **Impact:** High (preserves session history isolation)
- **Regression Test:** `tests/unit/test_session_screen_fork_isolation.py`

---

### SPEC-P1.4: Safe File Reading in File I/O and Rollback Snapshotting
- **Location:** `src/tools/_file_io.py:211`, `src/core/orchestration/rollback_manager.py:178,643`
- **Problem:** When overwriting or taking snapshots of existing binary files (e.g. SQLite databases, images, `.wasm`, `.pyc`), `p.read_text(encoding="utf-8")` raises unhandled `UnicodeDecodeError`, failing tool execution and breaking rollbacks.
- **Specification:**
  Catch `UnicodeDecodeError` in `write_file` and `RollbackManager` methods (`snapshot_files`, `append_to_snapshot`, `_snapshot_directory_children`), falling back to safe replacement decoding (`errors="replace"`).
- **Complexity:** Low | **Impact:** Critical (prevents tool crashes on binary workspace assets)
- **Regression Test:** `tests/unit/test_file_io_binary_read_safety.py`

---

## Phase 2: Robustness & Non-Blocking I/O

### SPEC-P2.1: Asynchronous Workspace File Scanning in Chat Picker
- **Location:** `tui/tui_src/ui/components/chat_mixin.py:160-185`
- **Problem:** Synchronous `os.walk` across 2,000 files on the Textual UI thread freezes the event loop during typing when the 60s cache expires.
- **Specification:**
  Offload disk scanning to `asyncio.to_thread` with non-blocking background cache warming.
- **Complexity:** Low | **Impact:** High (eliminates UI typing stutter)
- **Regression Test:** `tests/unit/test_chat_mixin_async_file_listing.py`

---

### SPEC-P2.2: Session Custom Title Persistence Across Turns
- **Location:** `tui/tui_src/ui/_app_slash_commands_mixin.py:910-940`, `tui/tui_src/ui/_app_session_mixin.py:75`
- **Problem:** `/rename` sets `self.sub_title`, but subsequent turns overwrite `task_name` with `first_user` in `_save_session_snapshot()`.
- **Specification:**
  Store `self._session_custom_title` on the app instance and check it in `_save_session_snapshot()`, saving the snapshot immediately upon `/rename`.
- **Complexity:** Low | **Impact:** Medium (persists user-specified session titles)
- **Regression Test:** `tests/unit/test_session_title_persistence.py`

---

### SPEC-P2.3: Async Safe Git Revert in `SessionScreen`
- **Location:** `tui/tui_src/ui/screens/session_screen.py:224-250`
- **Problem:** Synchronous `git stash` and `git checkout` freeze the UI thread and leave the repository in detached-HEAD mode.
- **Specification:**
  Execute git commands asynchronously via `asyncio.to_thread` and create a safety branch or notify cleanly.
- **Complexity:** Low | **Impact:** Medium (improves safety and responsiveness)
- **Regression Test:** `tests/unit/test_session_screen_async_revert.py`

---

### SPEC-P2.4: Multi-File Transaction Rollback Journal Incompleteness
- **Location:** `src/core/orchestration/rollback_manager.py:150-160`, `src/core/orchestration/graph/nodes/finalize_node.py:80-95`
- **Problem:** Newly created files in a multi-file step must be recorded with `exists=False` and cleanly unlinked if the transaction fails verification.
- **Specification:**
  Ensure all mutated and created target paths are registered with `exists=False` before write operations execute.
- **Complexity:** Medium | **Impact:** High (guarantees zero orphaned files on failed steps)
- **Regression Test:** `tests/unit/test_multi_file_rollback_cleanup.py`

---

## Phase 3: Capability Improvements (Implemented)

### SPEC-P3.1: Pre-Write AST & Syntax Validation for `edit_file_atomic`, `edit_code_block`, and `multiedit`
- **Location:** `src/core/orchestration/tool_execution_pipeline.py:636-680`
- **Problem:** `edit_file_atomic` and multi-edit tools take `path`, `old_string`, `new_string`, so `args.get("content")` evaluated to `None`. Pre-write AST syntax validation was bypassed, allowing syntax errors to land on disk before verification.
- **Specification:**
  Project candidate replacement content in `_run_sandbox_and_snapshot` before disk write. Run language AST parsing (`ast.parse` for Python, `tsc`/`node` for JS/TS). If syntax is broken, block execution with a fail-closed sandbox error. Valid candidate content is marked as pre-validated.
- **Complexity:** Low | **Impact:** High (prevents file syntax corruption from partial atomic string edits)
- **Regression Test:** `tests/unit/test_tool_pipeline_atomic_ast_validation.py`

---

### SPEC-P3.2: Scoped Verification Heuristics for Non-Executable Assets
- **Location:** `src/core/orchestration/graph/nodes/verification_node.py:222-243`
- **Problem:** Modifying non-code files (e.g. `.md`, `.css`, `.json`, `.svg`, `.txt`) triggered expensive project-wide test suites (`run_tests` / `run_js_tests`), failing non-functional documentation or asset edits on unrelated test failures.
- **Specification:**
  When `unverified_paths` contains only non-executable asset files, skip full test runner invocations, record verification passed, and mark `status: "skipped_non_code"` with `non_code_assets: True`.
- **Complexity:** Low | **Impact:** High (unblocks documentation, asset, and config edits from unrelated project test failures)
- **Regression Test:** `tests/unit/test_verification_node_non_code_heuristics.py`

---

### SPEC-P3.3: Dynamic Context Window Scaling in `#sb_tokens` Status Bar
- **Location:** `tui/tui_src/ui/_app_message_handlers_mixin.py:92-120`, `tui/tui_src/ui/app.py:209,740-768`
- **Problem:** `#sb_tokens` displayed a static 32,000 capacity regardless of the routed provider or model profile (e.g., Gemini 1M, Claude 200k, Ollama 8k).
- **Specification:**
  Added model profile lookup in `handle_model_routing` and reactive `watch_context_window` scaling so `#sb_tokens` immediately reformats using the routed model's true context capacity.
- **Complexity:** Low | **Impact:** Medium (provides accurate live token telemetry in TUI)
- **Regression Test:** `tests/unit/test_tui_context_window_dynamic_scaling.py`

---

### SPEC-P3.4: Deduplicate Redundant Syntax Validation Passes
- **Location:** `src/tools/_lint_verify.py:85-97`, `src/core/orchestration/tool_execution_pipeline.py:770-785`
- **Problem:** Files validated in `_run_sandbox_and_snapshot` were re-validated in `_verify_candidate_content`, doubling tempdir creation and subprocess linter invocations.
- **Specification:**
  Propagated pre-validation marks for all edit and write tools, enabling the fast-path bypass in `verify_candidate_content`.
- **Complexity:** Low | **Impact:** Medium (reduces temporary file disk I/O and latency)
- **Regression Test:** `tests/unit/test_tool_pipeline_atomic_ast_validation.py`

---

## Phase 4: Long-Term Architecture & Multi-File DAGs (Implemented & Verified)

### SPEC-P4.1: Granular Selective File Rollback & Atomic Commit
- **Location:** `src/core/orchestration/rollback_manager.py:475-530`
- **Specification:** Added `rollback_files(paths, snapshot_id)` and `commit_files(paths, snapshot_id)` to allow surgical rollbacks of individual file paths without discarding the rest of a snapshot journal.
- **Regression Tests:** `tests/unit/test_rollback_manager.py`, `tests/unit/test_rollback_manager_dag.py`

---

### SPEC-P4.2: Unified Slash Command and Command Palette Dispatch Service
- **Location:** `tui/tui_src/ui/features/palette/logic.py`, `tui/tui_src/ui/features/palette/screen.py`, `tui/tui_src/ui/app.py`
- **Specification:** Unified all slash commands (`/help`, `/clear`, `/new`, `/compact`, `/undo`, `/continue`, `/interrupt`, `/status`, `/doctor`, `/skills`, `/fast`, `/provider`, `/model`, `/settings`, `/sessions`, `/timeline`, `/diff`, `/fork`, `/export`, `/worktree`, `/mcp`, `/quit`) into the `ctrl+p` command palette. Rendered bold cyan slash badges on palette items, enabled leading slash search filtering, and aliased `action_open_palette`.
- **Regression Tests:** `tests/unit/test_palette_slash_dispatch.py`

---

### SPEC-P4.3: Headless Textual Pilot Test Suite Expansion & Markup Hardening
- **Location:** `tests/unit/test_tui_pilot_suite.py`, `tests/unit/test_bash_block.py`, `tui/tui_src/ui/components/bash_block.py`, `tui/tui_src/ui/_app_tool_handlers_mixin.py`
- **Specification:** Expanded headless pilot test coverage via `App.run_test()`. Replaced fragile string markup with Rich `Text` streaming in `BashBlock` to prevent unhandled bracket collisions (e.g. `[1/10]`, `[ERROR]`, `[slug]`), fixed invalid Textual CSS (`cursor: pointer;`), and escaped dynamic tool output strings in `_app_tool_handlers_mixin.py`.
- **Regression Tests:** `tests/unit/test_tui_pilot_suite.py`, `tests/unit/test_bash_block.py`
