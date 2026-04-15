# Audit Report — Vol25

**Date:** 2026-04-10
**Prior cycle:** Vol24 (2026-04-09)
**Scope:** Batch 3 / LOW parity gap findings — TUI inline diff, permission UX, footer navigation, slash commands, git worktree isolation, and display config.

---

## Summary

| ID | Severity | Status | File(s) | Description |
|----|----------|--------|---------|-------------|
| GAP-TUI-2 | Low | FIXED | `diff_viewer.py` | Inline diff view (single-column unified renderer) |
| GAP-PERM-2 | Low | FIXED | `app.py` | Reject-with-feedback: deny sends optional reason to agent |
| GAP-PERM-3 | Low | FIXED | `app.py` | Allow-always glob: "Allow Always" button + session-scoped auto-approve |
| GAP-FOOTER-3 | Low | FIXED | `app.py` | Subagent footer navigation chip |
| GAP-CMD-2 | Low | FIXED | `app.py` | `/share` — export conversation to clipboard or markdown file |
| GAP-CMD-3 | Low | FIXED | `app.py` | `/rename` — rename the current session |
| GAP-WORKTREE-1 | Low | FIXED | `git_worktree_manager.py` (NEW) | Git worktree isolation for tasks |
| GAP-CONFIG-1 | Low | FIXED | `settings.py`, `screen.py` | `diff_style` setting ("side-by-side" / "inline") |
| GAP-CONFIG-2 | Low | FIXED | `settings.py`, `screen.py` | `scroll_speed` setting (1–10, default 3) |
| GAP-CONFIG-3 | Low | FIXED | `settings.py`, `screen.py` | `conceal_sensitive` setting (bool, default False) |

---

## Findings

### GAP-TUI-2 — Inline Diff View ✅ FIXED

**Files:** `tui/src/ui/components/diff_viewer.py`, `tui/src/ui/components/__init__.py`, `tui/src/ui/app.py`

**Issue:** The TUI only supported side-by-side diff rendering. A single-column "inline" (unified) view showing `+`/`-` markers was missing.

**Fix:**
- Added `_render_inline(diff)` function that converts unified diff to coloured single-column markup (red `[on #3d0000]` for removed, green `[on #003d00]` for added, cyan for hunk headers).
- Added `InlineDiff` widget that uses `_render_inline`, shares `Accepted`/`Rejected` message types with `SideBySideDiff` (so `app.py` handlers work for both styles without change), and has Accept/Reject buttons.
- `handle_diff_preview()` now reads `diff_style` setting and uses `InlineDiff` when `"inline"`, `SideBySideDiff` otherwise.
- `InlineDiff` exported from `tui/src/ui/components/__init__.py`.

---

### GAP-PERM-2 — Reject-with-Feedback ✅ FIXED

**File:** `tui/src/ui/app.py`

**Issue:** When the user denied a tool permission, no feedback was sent to the agent. The agent had no way to know why it was denied or how to adjust.

**Fix:**
- After denying, a `Horizontal` row (`tool_deny_fb_{tool_id}`) is mounted with an `Input` and "Send" / "Skip" buttons.
- On "Send", feedback text is published as `tool.denial_feedback` event with `{tool_id, feedback}` payload.
- On "Skip" (or empty input), the row is simply removed.
- `tool.permission_denied` is still published immediately on Deny — the feedback is optional and asynchronous.

---

### GAP-PERM-3 — Allow-Always Glob ✅ FIXED

**File:** `tui/src/ui/app.py`

**Issue:** Users had to approve every tool permission individually. There was no way to say "always allow this tool for the rest of the session."

**Fix:**
- `_allow_always_tools: set[str]` added to `AgentApp.__init__`.
- `handle_tool_permission()` checks this set first — if the tool name is registered, it auto-approves without showing a UI prompt.
- "Allow Always" button (`btn_tool_perm_always_{tid}`) added alongside Allow/Deny.
- Handler registers the tool name in `_allow_always_tools`, approves the current request, and shows a gold confirmation badge.

---

### GAP-FOOTER-3 — Subagent Footer Navigation ✅ FIXED

**File:** `tui/src/ui/app.py`

**Issue:** Active subagents were shown in the right sidebar but the footer had no indicator. Users couldn't see at a glance whether subagents were running.

**Fix:**
- Added `Static("", id="subagent_footer_chip", markup=True)` to the `coding_footer` horizontal.
- Added `_update_subagent_footer()` method that sets the chip to `"⇢ N subagent(s)"` (purple) when active, or `""` when idle.
- `_update_subagent_footer()` called from both `handle_subagent_start()` and `handle_subagent_finish()`.

---

### GAP-CMD-2 — `/share` Slash Command ✅ FIXED

**File:** `tui/src/ui/app.py`

**Issue:** No way to export the conversation history from the TUI.

**Fix:**
- `/share` added to `SLASH_HELP` and `handle_slash_command()`.
- `_slash_share()` formats `self._bridge.history` as a markdown document (role headers, content, separators).
- Tries `pyperclip.copy()` first (clipboard); falls back to writing an export file under the user's data directory (see ``src.core.paths.get_data_dir()``) such as `export_YYYYMMDD_HHMMSS.md`.
- Displays confirmation with message count.

---

### GAP-CMD-3 — `/rename` Slash Command ✅ FIXED

**File:** `tui/src/ui/app.py`

**Issue:** No way to rename a session from the TUI.

**Fix:**
- `/rename <name>` added to `SLASH_HELP` and `handle_slash_command()`.
- `_slash_rename(args)` calls `store.rename_session(current_id, new_name)` if `SessionStore` has the method; otherwise patches `store._sessions[current_id]["title"]` directly.
- Updates `Header.sub_title` for immediate visual feedback.
- Publishes `session.renamed` event with `{name}`.
- Gracefully degrades to "display name only" if session store cannot be reached.

---

### GAP-WORKTREE-1 — Git Worktree Isolation ✅ FIXED

**File:** `src/core/orchestration/git_worktree_manager.py` (NEW), `tui/src/ui/app.py`

**Issue:** The agent had no way to isolate task changes in a separate git worktree. All agent edits landed in the main working tree.

**Fix:**
- `GitWorktreeManager(workspace)` manages `task_id → Path` mappings.
- `create(task_id, branch=None)` — runs `git worktree add --detach <tmpdir>` (or with a branch); returns path; idempotent if already active; cleans temp dir on failure.
- `remove(task_id)` — runs `git worktree remove --force`; falls back to `shutil.rmtree` if the git command fails.
- `remove_all()` — removes all active worktrees; call on shutdown.
- `list_registered()` — returns parsed output of `git worktree list --porcelain`.
- `_parse_worktree_list()` — pure parser, no subprocess.
- `/worktree [list | create [<task_id>] | remove <task_id>]` slash command wired in `app.py`.

---

### GAP-CONFIG-1/2/3 — Display Settings ✅ FIXED

**Files:** `tui/src/ui/settings.py`, `tui/src/ui/features/settings/screen.py`

**Issue:** The settings system had no user-configurable display preferences.

**Fix (settings.py DEFAULTS):**
- `"diff_style": "side-by-side"` — GAP-CONFIG-1
- `"scroll_speed": 3` — GAP-CONFIG-2 (lines per tick, 1–10)
- `"conceal_sensitive": False` — GAP-CONFIG-3 (conceal API keys/tokens in TUI output)

**Fix (screen.py `_compose_inner`):**
- New "Display" section added before "Agent role configuration".
- `Select` for `diff_style` (Side by side / Inline).
- `Input` for `scroll_speed`.
- `Select` for `conceal_sensitive` (Off / On).

**Fix (screen.py `_do_save`):**
- All three fields read and written to `updates` dict on save.

---

## Test Coverage

**File:** `tests/unit/test_audit_batch3_low.py` (42 tests)

| Class | Tests |
|-------|-------|
| `TestGapTUI2InlineDiff` | 7 |
| `TestGapConfig` | 9 |
| `TestGapWorktree1` | 9 |
| `TestGapPerm3AllowAlways` | 2 |
| `TestGapCommands` | 8 |
| `TestGapPerm2DenyFeedback` | 4 |
| `TestGapFooter3` | 3 |

---

## Baseline Metrics

| Metric | Vol24 (closed) | Vol25 (closed) |
|--------|----------------|----------------|
| Tests passed | 3229 | **3271** |
| Tests failed | 0 | **0** |
| Tests skipped | 4 | **4** |
| New tests | — | **+42** |
| Open LOW items | 10 | **0** |
