# TUI Gap Analysis: OpenCode vs CodingAgent

Comparison of opencode's TUI feature set against CodingAgent's current Textual implementation.
Items are grouped by priority (P1 = high impact / low effort, P2 = medium, P3 = low priority / high effort).

---

## P1 — High Impact, Relatively Low Effort

### 1. Toast / Notification System
**Opencode:** Non-blocking transient toasts for confirmations, errors, warnings. Appear at top-right, auto-dismiss after ~3s, support severity levels (info/warn/error).
**CodingAgent:** All notifications written inline to `#chat_output` or `#sys_log`, no dismissable toasts.
**Impact:** Reduces noise in chat output; status messages (token warning, provider connected) should not pollute the conversation log.
**Implementation hint:** Textual `notify()` API supports this natively since Textual 0.30. Replace `_safe_write()` calls for non-conversation messages.

---

### 2. Searchable Model Selection Dialog
**Opencode:** Full-screen modal with searchable list, recency sorting, keyboard navigation, cost-per-token indicators, model variant badges (e.g. "Thinking").
**CodingAgent:** Plain `Select` dropdown in Settings modal — no search, no recency, no cost info.
**Impact:** With 7+ Copilot models + LM Studio models, discovery is awkward in a dropdown.
**Implementation hint:** Replace `Select` widget with a custom `Screen` using a `ListView` + `Input` filter — similar pattern to `_DeviceCodeModal` already in the codebase.

---

### 3. `/` Slash Command Palette with Argument Hints
**Opencode:** Typing `/` opens a floating overlay listing all commands with descriptions; argument hints shown inline as user types.
**CodingAgent:** Tab autocomplete cycles through command names only; no overlay, no descriptions shown.
**Impact:** 14 commands are hard to discover without the overlay.
**Implementation hint:** On `/` keypress in `ChatInput`, mount a `ComposeResult` overlay widget showing the command list, filter on subsequent characters. Already have the command list in `_SLASH_COMMANDS`.

---

### 4. Multi-line Prompt Input (Shift+Enter)
**Opencode:** Shift+Enter inserts a real newline in the prompt. Multi-line prompts displayed as-is.
**CodingAgent:** Pastes compact newlines to literal `\n`; no Shift+Enter for intentional newlines. Single-line `Input` widget.
**Impact:** Pasting code snippets or multi-paragraph prompts currently garbles formatting.
**Implementation hint:** Swap `Input` for `TextArea` widget (available in Textual 0.47+). History and slash-command autocomplete need to be ported.

---

### 5. Input History with Arrow-Key Navigation (already partial)
**Opencode:** Full up/down history with frecency scoring.
**CodingAgent:** Up/down history implemented but no frecency — purely sequential.
**Impact:** Low-hanging fruit; simple persistence improvement.
**Implementation hint:** Weight recent + frequently used prompts higher in the history list.

---

## P2 — Medium Priority

### 6. Session Management Dialog
**Opencode:** Full session list with search, date grouping, fork, rename, share, compact, delete.
**CodingAgent:** `/continue` restores last session; `/new` starts fresh; `/compact` compresses. No list/browse UI.
**Impact:** Power users managing multiple tasks across sessions have no overview.
**Implementation hint:** `TextualAppBase._load_history()` already exists. Build a `Screen` that lists saved sessions from `~/.config/codingagent/` sorted by date, with `Enter` to resume.

---

### 7. MCP Server Status in Footer
**Opencode:** Footer shows live MCP server count, LSP status, active permission prompts as clickable chips.
**CodingAgent:** Footer only shows keyboard legend. MCP server state not surfaced in the UI at all.
**Impact:** When an MCP server goes down, there's no visible indicator.
**Implementation hint:** Add a reactive `Label` to the footer row fed by an EventBus subscription (e.g. `mcp.server.status`). Existing `Footer` widget can be replaced with a custom `Horizontal` containing the legend + status chips.

---

### 8. Permission Prompt Dialogs
**Opencode:** When the agent requests a potentially destructive tool (file delete, shell exec), a modal appears asking for user approval with the command shown.
**CodingAgent:** `plan_mode` approval exists in the orchestrator (`execute_tool` blocks write tools when enabled), but there is no TUI dialog surfacing this. The user has no way to approve/reject from the UI unless they send a message.
**Impact:** Safety feature with existing backend support — just needs the UI layer.
**Implementation hint:** Subscribe to a new `tool.permission_required` EventBus event; push a confirmation `Screen`; emit `tool.permission_granted` / `tool.permission_denied` on user choice.

---

### 9. File Attachment in Prompt
**Opencode:** `@filename` autocomplete in input; drag-and-drop file attachment; file content injected into context.
**CodingAgent:** No attachment UI. Users must paste file content manually or use slash commands.
**Impact:** Common use case for code review tasks.
**Implementation hint:** Detect `@` prefix in `ChatInput`; show a `ListView` overlay of workspace files (using existing `_INDEXED_DIRS` data); inject `<file: path>\ncontent\n</file>` into the message before sending.

---

### 10. Theme Switcher
**Opencode:** 30+ themes (Tokyo Night, Catppuccin, Gruvbox, Solarized, etc.) with live preview and automatic dark/light detection.
**CodingAgent:** Single hardcoded dark theme (VS Code-inspired).
**Impact:** Personal preference but high perceived polish.
**Implementation hint:** Textual supports loading different `.tcss` files at runtime via `app.stylesheet`. Define 3–5 theme files (dark/light/high-contrast minimum). Add theme picker to Settings modal.

---

### 11. Cost / Token Display per Message
**Opencode:** Each assistant message shows input+output token count and estimated cost.
**CodingAgent:** Global token budget shown in sidebar, but no per-message breakdown.
**Impact:** Helps users understand which operations are expensive.
**Implementation hint:** The orchestrator already tracks token usage via `token.budget.update` events. Extend the event payload to include per-call counts; append a dim footer line to each assistant message in the chat output.

---

### 12. Keyboard Shortcut for Model Switch
**Opencode:** `/` → model command or dedicated keybinding opens model picker directly.
**CodingAgent:** Must open Settings modal (Ctrl+O), then use dropdown.
**Impact:** Switching models mid-task is a common operation requiring 3+ steps.
**Implementation hint:** Add a `Ctrl+M` binding that opens the model selection dialog directly, bypassing the full Settings modal.

---

## P3 — Lower Priority / Higher Effort

### 13. Sidebar with File Tree, Symbols, TODOs
**Opencode:** Right sidebar (togglable) shows workspace file tree, LSP symbols for open file, MCP resources, TODO list.
**CodingAgent:** Right sidebar shows context/plan/tool state — useful but no file browser or symbols.
**Impact:** Significant engineering effort; useful for large codebases.

---

### 14. Diff Wrapping Toggle
**Opencode:** Toggle between wrapped and side-by-side diff views inline.
**CodingAgent:** Side-by-side diff table with fallback to unified. No toggle UI; layout is fixed.
**Impact:** Low priority — current implementation is already good.

---

### 15. Timeline / Message History Dialog
**Opencode:** Scrollable timeline of all turns in the session with jump-to navigation.
**CodingAgent:** Chat output is scrollable but there is no jump-to or structured history viewer.
**Impact:** Useful for long sessions; low urgency given scrolling works.

---

### 16. Agent Selection Dialog
**Opencode:** Multi-agent mode where the user picks which sub-agent to direct a message to.
**CodingAgent:** Delegation is automatic (analyst_delegation_node); no user-facing agent selector.
**Impact:** Requires broader architectural changes beyond the TUI.

---

### 17. Sidebar Plugin / Slot System
**Opencode:** Extensible sidebar where MCP servers can register UI panels.
**CodingAgent:** No plugin system.
**Impact:** Large engineering effort; not a near-term priority.

---

## Already Implemented (No Gap)

| Feature | Status |
|---|---|
| Rich markdown + code block rendering | Done |
| Diff preview before file write | Done (file.diff.preview event) |
| Provider + model selection | Done (Settings modal) |
| API key management | Done (save to prefs.json) |
| GitHub Copilot OAuth device flow | Done (auth module + TUI) |
| Session save/restore | Done (/continue, /new) |
| Context compaction | Done (/compact) |
| System log panel (toggleable) | Done (Ctrl+L) |
| Token budget display | Done (sidebar + warnings) |
| Plan progress visualization | Done (sidebar) |
| Tool activity log | Done (sidebar) |
| Slash command autocomplete (Tab) | Done |
| Keyboard interrupt (Escape) | Done |
| Real-time EventBus updates | Done |
| 14 slash commands | Done |

---

## Recommended Implementation Order

1. **Toast notifications** — 1 day, high polish gain, Textual native API
2. **Shift+Enter multi-line input** — 1 day, unblocks pasting code
3. **Searchable model dialog** — 2 days, replaces dropdown
4. **Session list dialog** — 2 days, infrastructure already exists
5. **Permission prompt dialog** — 2 days, backend already exists
6. **MCP status footer** — 1 day, EventBus wiring only
7. **`@file` attachment autocomplete** — 2 days
8. **Theme switcher (3 themes)** — 1 day
9. **Per-message token display** — 1 day
10. **Ctrl+M model shortcut** — 0.5 day
