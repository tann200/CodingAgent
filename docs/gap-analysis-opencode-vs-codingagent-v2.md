# Gap Analysis: OpenCode vs CodingAgent — v2 (Complete)

Generated: 2026-04-09
Scope: Full source read of OpenCode TUI, tool subsystem, permission engine, worktree subsystem.
Prior analysis: `docs/gap-analysis-opencode-vs-codingagent.md` (superseded)

---

## Executive Summary

CodingAgent has a solid foundation: 3229 unit tests pass, all prior audit items are closed, and
SUBAGENT-VIS is fully implemented. The primary gaps fall into three categories:

1. **TUI tool rendering** — CodingAgent shows generic tool notices; OpenCode renders each tool with
   a dedicated icon, diff view, LSP diagnostics, and collapsible output.
2. **Permission system** — CodingAgent only gates `bash`; OpenCode gates every tool with
   `allow / deny / ask` policy, "reject with feedback" flow, and glob patterns persisted per project.
3. **Session commands / UX** — `/undo`, `/share`, `/fork`, `/rename` are missing; the footer is
   static; the compaction divider is absent; no queued-message indicator.

Items are rated **HIGH / MEDIUM / LOW** by user-visible impact.

---

## Part 1 — TUI Tool Rendering Gaps

### GAP-TUI-1 — Per-tool inline icons (HIGH)

**OpenCode:** Every tool call renders with a distinct icon prefix and two lines of context:

| Tool | Pending text | Completed text |
|------|-------------|----------------|
| `read` | `→ Reading …` | `→ Read src/foo.py` |
| `write` | `← Writing …` | `← Write src/foo.py` (then full diff block) |
| `edit` | `← Preparing edit…` | `← Edit src/foo.py` (then diff block) |
| `apply_patch` | `% Preparing patch…` | `← Patched / # Created / # Deleted` (per file) |
| `bash` | `# <desc>` | `$ <cmd>` with expandable output |
| `glob` | `✱ Glob "pat" in dir` | `✱ Glob "pat" in dir (N matches)` |
| `grep` | `✱ Grep "pat" in dir` | `✱ Grep "pat" in dir (N matches)` |
| `list` (`ls`) | `→ Listing dir` | `→ List dir` |
| `webfetch` | `% WebFetch url` | same |
| `task` | `│ Delegating…` | `│ <AgentType> Task — <desc>` + `↳ toolcalls / duration` |
| `todowrite` | `⚙ Updating todos…` | full `# Todos` block with status icons |
| `question` | `→ Asking questions…` | `# Questions` block with Q&A |
| `skill` | `→ Loading skill…` | `→ Skill "<name>"` |

**CodingAgent:** `ToolCallStartEvent` / `ToolCallFinishEvent` events exist and are bridged, but
`app.py` renders them all as the same generic `ToolExecutionNotice` widget with no per-tool
differentiation or icons. See `tui/src/ui/app.py:~800–900`.

**Files to change:**
- `tui/src/ui/app.py` — add per-tool rendering switch in `handle_tool_call_start` /
  `handle_tool_call_finish`
- `tui/src/ui/components/artifact.py` — add `InlineToolWidget` and `BlockToolWidget` helpers

---

### GAP-TUI-2 — Inline diff view for Edit/Write/ApplyPatch (HIGH)

**OpenCode:** `Edit`, `Write`, `ApplyPatch` tools render a full inline diff block with:
- Syntax-highlighted `<diff>` component
- Split view (>120 cols) or unified view
- Line numbers
- LSP error diagnostics shown below the diff (up to 3 errors per file)
- File type detection from extension

`index.tsx:2017–2141`

**CodingAgent:** `DiffPreviewEvent` is published and received (`bus.py:~290`, `app.py:~920`), and
`SideBySideDiff` component exists (`tui/src/ui/components/diff_viewer.py`), but it is shown in a
separate panel not inline with the tool call. No LSP diagnostics are shown.

**Files to change:**
- `tui/src/ui/app.py` — render diff inline when `ToolCallFinishEvent` for `edit`/`write`/`apply_patch`
- `tui/src/ui/components/diff_viewer.py` — expose inline-compatible variant

---

### GAP-TUI-3 — Bash block with expandable output (HIGH)

**OpenCode:** Bash renders as a fenced code block: `# <description>` header, `$ <command>` body,
output shown in a scrollable region. Click header to expand/collapse. Truncated at ~40 lines with
"click to expand" hint. `index.tsx:~1580–1700`

**CodingAgent:** Bash output is appended as a streaming `StreamView` widget which is not
tool-scoped, not collapsible, and not clearly attributed to the specific bash call.

**Files to change:**
- `tui/src/ui/app.py` — wrap bash output in a dedicated `BashBlock` widget
- `tui/src/ui/components/` — add `bash_block.py`

---

### GAP-TUI-4 — TodoWrite rendered as interactive list (MEDIUM)

**OpenCode:** `TodoWrite` tool renders as a `# Todos` block using `TodoItem` component with
status-specific icons (`pending → ○`, `in_progress → ●`, `completed → ✓`, `cancelled → ✗`).
`index.tsx:2144–2163`

**CodingAgent:** TodoWrite events are not specifically rendered in the TUI; todos are not shown.

**Files to change:**
- `tui/src/ui/app.py` — add `TodoWrite` render handler
- `tui/src/ui/components/` — add `todo_item.py`

---

### GAP-TUI-5 — Question tool rendered as Q&A block (MEDIUM)

**OpenCode:** `Question` tool renders as a `# Questions` block with each question and its answer
shown in a two-line format (question in muted color, answer in normal color). `index.tsx:2165–2197`

**CodingAgent:** Question tool exists (`src/tools/question_tool.py`) but no TUI rendering beyond
the generic `ToolCallFinishEvent`.

**Files to change:**
- `tui/src/ui/app.py` — add `Question` render handler

---

### GAP-TUI-6 — Task tool shows live subagent progress (MEDIUM)

**OpenCode:** The `task` tool inline render fetches the child session's messages and shows:
- While running: `↳ <current tool name> <current tool title>` (live, reactive)
- On completion: `└ N toolcalls · <duration>`
- Click to navigate into the child session

`index.tsx:1952–2015`

**CodingAgent:** SUBAGENT-VIS shows spinner in sidebar, but the task tool call in the message
stream does not show live subagent activity or a click-to-navigate link.

**Files to change:**
- `tui/src/ui/app.py` — update task tool widget to show live tool name from child session
- `tui/src/ui/components/subagent_progress.py` — add click handler / navigation hint

---

## Part 2 — Permission System Gaps

### GAP-PERM-1 — Per-tool permission policy (HIGH)

**OpenCode:** Every tool calls `ctx.ask({permission, patterns, always, metadata})` before executing.
The `Permission` service checks rules in order: session rules → project rules → config file rules.
Actions: `allow`, `deny`, `ask`. `always` contains glob patterns that, when the user clicks
"Allow always", are persisted to the `PermissionTable` in the SQLite DB.

Supported permission types: `edit`, `bash`, `webfetch`, `task`, `todowrite`, `read`, `glob`,
`grep`, `list`, `codesearch`, `websearch`, `external_directory`, `doom_loop`.

`permission/index.ts:1–322`, `tool/task.ts:51–61`, `tool/bash.ts`, `tool/edit.ts`

**CodingAgent:** Only `bash` is gated via `BashApprovalEvent` / `BashApproved` / `BashDenied`.
`ToolPermissionEvent` / `ToolPermissionApproved` / `ToolPermissionDenied` exist in the bus and
events but are not wired to a consistent pre-tool gate for all tools.

**Files to change:**
- `src/tools/` — each tool should publish `ToolPermissionEvent` and await approval before running
- `tui/src/ui/app.py` — unify `BashApprovalEvent` and `ToolPermissionEvent` handlers

---

### GAP-PERM-2 — "Reject with feedback" permission flow (HIGH)

**OpenCode:** In the permission overlay, clicking "Reject" opens a text input:
*"Tell OpenCode what to do differently"*. The rejection message is returned to the agent as a
`CorrectedError` with `feedback` field, allowing the agent to revise its approach without user
having to re-type a message. `permission/index.ts:89–95`, `permission.tsx:~450–500`

**CodingAgent:** `BashDenied` / `ToolPermissionDenied` carry no feedback message. The agent
receives a generic "denied" and must rely on the user sending a follow-up message.

**Files to change:**
- `tui/src/ui/features/` — add feedback text input to permission prompt
- `tui/src/ui/bus.py` — add `feedback: Optional[str]` to denial events
- `src/core/` — route feedback into the next LLM turn

---

### GAP-PERM-3 — "Allow always" with glob patterns (MEDIUM)

**OpenCode:** "Allow always" stores per-pattern glob rules in the project's permission table.
The rules show on the permission overlay before the user clicks. `permission/index.ts:237–243`

**CodingAgent:** "Allow always" concept does not exist for tools; bash only has `BashApproved`
which is single-shot.

---

## Part 3 — Session Command Gaps

### GAP-CMD-1 — `/undo` command (HIGH)

**OpenCode:** `/undo` reverts to the previous user message, trimming everything after it from the
session. Works even while the agent is running (aborts first). Useful for "that was wrong, try
this instead" flows. Session stores all messages; undo is a slice operation.

**CodingAgent:** No `/undo` command exists.

**Files to change:**
- `tui/src/ui/app.py` — add `/undo` to `SLASH_HELP` and `_handle_slash`
- `src/core/orchestration/` — add session message trim endpoint

---

### GAP-CMD-2 — `/share` and `/unshare` (LOW)

**OpenCode:** Generates a shareable URL for the session. Requires a configured share endpoint.
Not critical for local use.

**CodingAgent:** Not applicable without share infrastructure.

---

### GAP-CMD-3 — `/rename` (LOW)

**OpenCode:** Renames the current session title. Stored in session metadata.

**CodingAgent:** Sessions have titles (generated from first message) but no `/rename` command.

**Files to change:**
- `tui/src/ui/app.py` — add `/rename <title>` slash command

---

## Part 4 — Footer / Status Bar Gaps

### GAP-FOOTER-1 — Live footer: pending permissions count (HIGH)

**OpenCode:** Footer shows `△ N Permission(s)` in warning color when there are pending permission
requests. `footer.tsx:~45–55`

**CodingAgent:** Footer shows static keybind hints only. MCP status chip exists
(`#mcp_status_chip`) but pending permission count is not shown.

**Files to change:**
- `tui/src/ui/app.py` — add permission counter to `coding_footer`
- `tui/src/ui/styles/app.tcss` — style for permission badge

---

### GAP-FOOTER-2 — Live footer: LSP / MCP counts with error state (MEDIUM)

**OpenCode:** Footer shows `• N LSP` (connected language servers) and `⊙ N MCP` (connected MCP
servers), with `⊙` in red when any MCP server has an error. `footer.tsx:~56–80`

**CodingAgent:** MCP status chip exists but only shows a static `[MCP]` label; no count, no error
color. LSP counts are not shown.

**Files to change:**
- `tui/src/ui/app.py` — update `McpServerStatusEvent` handler to show count + error color
- `tui/src/ui/core_bridge.py` — track MCP server states

---

### GAP-FOOTER-3 — Subagent footer for child session navigation (MEDIUM)

**OpenCode:** When viewing a child session (subagent), a `SubagentFooter` replaces the normal
footer, showing: agent label, `(N of M)`, token context %, cost, and `Parent` / `Prev` / `Next`
navigation buttons. `subagent-footer.tsx:1–131`

**CodingAgent:** Subagents are shown in the sidebar but there is no way to navigate into or
between subagent sessions.

---

## Part 5 — Message Stream UX Gaps

### GAP-MSG-1 — Compaction divider in message stream (MEDIUM)

**OpenCode:** When a compaction occurs, a visual `═══ Compaction ═══` divider is injected into
the message stream at the exact point it happened, so users can see what context was compacted.

**CodingAgent:** `/compact` runs but no divider is inserted into the chat log.

**Files to change:**
- `tui/src/ui/app.py` — add divider widget when compaction succeeds

---

### GAP-MSG-2 — Queued message indicator (MEDIUM)

**OpenCode:** When a second message is submitted while the agent is running, it is queued and
shown with a `QUEUED` badge in the agent's accent color on the user message bubble.

**CodingAgent:** No message queueing. Input is disabled while agent is running; user must wait.

---

### GAP-MSG-3 — Strikethrough / warning state for denied tool calls (MEDIUM)

**OpenCode:** Tool call widgets show strikethrough text when permission is denied, and warning
color when awaiting permission. `index.tsx:~InlineTool component`

**CodingAgent:** No visual state change for pending/denied tool calls.

---

## Part 6 — Worktree / Isolation Gap

### GAP-WORKTREE-1 — Git worktree isolation for subagents (LOW)

**OpenCode:** `Worktree` subsystem creates separate `git worktree` branches per subagent session,
so subagent file edits are isolated and can be merged or discarded independently.
`worktree/index.ts:1–643`

**CodingAgent:** Subagents operate in the same working directory as the parent. No isolation.
This is a significant architecture change; scope is LOW for the current roadmap.

---

## Part 7 — Config / Provider Gaps

### GAP-CONFIG-1 — `tui.diff_style` config option (LOW)

**OpenCode:** `tui.diff_style: "stacked" | "auto"` controls whether diffs are shown as
unified or split view. `index.tsx:2022–2026`

**CodingAgent:** No equivalent config; diff style is hardcoded.

---

### GAP-CONFIG-2 — `tui.scroll_speed` / scroll acceleration (LOW)

**OpenCode:** Configurable scroll speed with MacOS-style acceleration.

**CodingAgent:** Textual's default scroll behavior.

---

### GAP-CONFIG-3 — `conceal` toggle for secrets in output (LOW)

**OpenCode:** API keys and secrets are concealed by default; `ctrl+k` toggles.

**CodingAgent:** No secret concealment.

---

## Priority Implementation Plan

### Batch 1 — HIGH priority ✅ CLOSED (2026-04-09)

| ID | Description | Status |
|----|-------------|--------|
| GAP-TUI-1 | Per-tool inline icons | ✅ `_TOOL_ICONS` map + per-tool dispatch in `handle_tool_start/finish` |
| GAP-TUI-2 | Inline diff for Edit/Write/ApplyPatch | ⚠ Diff still shown in panel; inline variant deferred to Batch 3 |
| GAP-TUI-3 | Bash block with expandable output | ✅ Fenced `# desc / $ cmd` block, 40-line truncation |
| GAP-PERM-1 | Per-tool permission policy | ✅ `ToolPermissionEvent` handler unified; badge wired |
| GAP-PERM-2 | "Reject with feedback" permission flow | ⚠ Denial events wired; feedback text input deferred |
| GAP-FOOTER-1 | Live footer: pending permissions count | ✅ `#perm_count_chip` shows `△ N Permission(s)` |
| GAP-CMD-1 | `/undo` command | ✅ Trims last turn from history |

### Batch 2 — MEDIUM priority ✅ CLOSED (2026-04-09)

| ID | Description | Status |
|----|-------------|--------|
| GAP-TUI-4 | TodoWrite as interactive list | ✅ `_render_todo_block()` — `# Todos` with `○ ● ✓ ✗` icons |
| GAP-TUI-5 | Question tool Q&A block | ✅ `_render_question_block()` — `# Questions` Q&A block |
| GAP-TUI-6 | Task tool live subagent progress | ✅ Finish renders `│ <role> Task — <desc>  └ N toolcalls` |
| GAP-PERM-3 | "Allow always" with glob patterns | ⚠ Deferred to Batch 3 |
| GAP-FOOTER-2 | Live footer: LSP/MCP counts + error state | ✅ `has_error` field; red `⊙ MCP N` chip when error |
| GAP-FOOTER-3 | Subagent footer navigation | ⚠ Deferred to Batch 3 |
| GAP-MSG-1 | Compaction divider in stream | ✅ `.compaction_divider` injected on `/compact` |
| GAP-MSG-2 | Queued message indicator | ✅ Message queued with `[QUEUED]` badge; sent when agent idles |
| GAP-MSG-3 | Denied tool strikethrough/warning | ✅ Pending widget updated to red strikethrough on deny |

### Batch 3 — LOW priority / OPEN

| ID | Description |
|----|-------------|
| GAP-TUI-2 | Inline diff view (deferred from Batch 1) |
| GAP-PERM-2 | Reject-with-feedback text input |
| GAP-PERM-3 | "Allow always" with glob patterns |
| GAP-FOOTER-3 | Subagent footer navigation |
| GAP-CMD-2 | `/share` / `/unshare` (needs infra) |
| GAP-CMD-3 | `/rename` session title |
| GAP-WORKTREE-1 | Git worktree isolation for subagents |
| GAP-CONFIG-1 | `tui.diff_style` option |
| GAP-CONFIG-2 | `tui.scroll_speed` option |
| GAP-CONFIG-3 | `conceal` toggle for secrets |
