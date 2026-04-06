# TUI Testing Specification
## TUI System Specification v2.0 — Verification Suite

**How to run the mock engine:**
```bash
python3 -m src.ui.mock_engine
```
The mock engine replays a complete 4-phase agent run (≈ 45 seconds) covering every
event category in §4.5. Each test case below maps to a specific observable moment
during that simulation, or to a user interaction that can be performed at any time.

---

## T01 — Startup sequence (§10.1)

**Trigger:** Application launch (automatic).

**Observe:**
- Working directory panel (`WORKING DIR`) shows `/workspace/project` immediately after startup.
- Provider panel shows `local_lm_studio` / `qwen2.5-coder-14b` within 1 second.
- Role banner shows `SYSTEM`, then transitions to `LEAD ARCHITECT` within 2 seconds.
- Token budget shows `800 / 32,000  (2.5%)` in green.
- Console panel (ctrl+l) contains two `[INFO]` lines from `orchestrator` and `provider`.

**Expected:** All 8 sidebar panels populated before the first tool call appears.

---

## T02 — Tool 3-beat lifecycle (§6.1)

**Trigger:** `list_files` tool call, ~2 seconds into the simulation.

**Observe:**
1. **Beat 1** — In-progress indicator appears in the chat panel: `◐ list_files  path="src/"`.
   The tool activity sidebar (`LAST TOOL`) also updates.
2. **Beat 2** — After ~0.5 s, the spinner is replaced in-place with the formatted result:
   ```
   ✓ list_files
   📁 src/
     📁 ui/
       📄 app.py
   …
   ```
3. The `LAST TOOL` sidebar shows `✓ list_files`.

**Expected:** No duplication in the chat panel — the spinner and the result occupy exactly
one slot.

---

## T03 — Tool error path (§6 / tool.execute.error)

**Trigger:** `search_code` tool call, immediately after `list_files` finishes.

**Observe:**
- Beat 1 shows `◐ search_code  query="EventBus subscribe…"`.
- Beat 2 replaces it with:
  ```
  ✗ search_code — VectorStore not initialised — run initialize_repo_intelligence first
  ```
- Console panel receives a `[WARNING] search: VectorStore unavailable…` line.
- `LAST TOOL` sidebar shows `✗ search_code`.

**Expected:** Error renders in-place; no crash; simulation continues with `grep` fallback.

---

## T04 — ACP schema fields on tool events

**Trigger:** Any tool event during the simulation.

**Verify (developer check — open console panel with ctrl+l):**
- Console panel entries show bridge received events with `toolCallId` (8-char hex), `title`,
  `sessionUpdate.status`, and `rawInput` / `content` fields populated.
- The bridge logs `ToolCallStartEvent(tool_name=…, tool_id=…)` for every tool start.

**Expected:** No "unknown" tool names; all tool widgets keyed by `toolCallId`.

---

## T05 — Diff preview inline in chat (§6.4)

**Trigger:** Phase 2 — Full Stack Engineer. Two `file.diff.preview` events fire before
`write_file` and `edit_file_atomic`.

**Observe:**
```
┌─ Preview: src/ui/mock_engine.py ─────────────────────────────────────────┐
│ --- a/src/ui/mock_engine.py                                               │
│ +++ b/src/ui/mock_engine.py                                               │
│ @@ -1,7 +1,8 @@                                                           │
│ - mock_engine.py — basic spec simulation.                                 │
│ + mock_engine.py — TUI System Specification v2.0 full-coverage simulation │
└───────────────────────────────────────────────────────────────────────────┘
```
- `+` lines are green; `-` lines are red; `@@` hunk headers are cyan.
- The diff panel appears **before** the `write_file` tool start line.
- The subsequent `write_file` finish shows `✓ Modified src/ui/mock_engine.py  [+142 / -31 lines]`.

**Expected:** Diff rendered inline with colour coding; truncated at 60 lines if longer.

---

## T06 — File modified sidebar (§12)

**Trigger:** Phase 2 — after each `write_file` and `edit_file_atomic` finish.

**Observe:**
- `FILES MODIFIED` sidebar accumulates:
  ```
  src/ui/mock_engine.py
  src/ui/core_bridge.py
  ```
- When `file.deleted` fires in Phase 3, the deleted path appears with `[deleted]` prefix.

**Expected:** Every `file.modified` event adds an entry; `file.deleted` appends a deletion
marker; no duplicates.

---

## T07 — Plan progress bar (§12.3)

**Trigger:** Four `plan.progress` events across the simulation (2 ACP, 2 legacy schema).

**Observe:**
- ACP events (steps 1 and 3):
  ```
  ▓▓▓▒▒▒▒▒▒▒  1 / 4  Analyse EventBus subscription coverage
  ▓▓▓▓▓▓▓▒▒▒  3 / 4  Running QA verification checks
  ```
- Legacy events (steps 2 and 4, using `step`/`total`/`description` fields):
  ```
  ▓▓▓▓▓▒▒▒▒▒  2 / 4  Rewriting mock_engine.py with full spec coverage
  ▓▓▓▓▓▓▓▓▓▓  4 / 4  All checks passed — spec compliance verified
  ```

**Expected:** Both schemas render identically. Progress bar fills from left; description
shown below the bar.

---

## T08 — Plan approval UI (§14.1)

**Trigger:** `plan.requested` event fires at the end of Phase 1 (~15 s in).

**Observe:**
- Chat panel renders the plan text verbatim:
  ```
  Step 1: Audit all §4.5 events against current mock_engine.py
  Step 2: Rewrite mock_engine.py with full ACP schema compliance
  …
  ```
- Below the plan text, two inline buttons appear: **[Approve]** and **[Reject]**.
- Input field is dimmed / read-only while the plan is pending.

**Test — Approve:**
Click **[Approve]** (or press Enter when button is focused).
Expected: Buttons disappear; chat shows `[dim]Plan approved.[/dim]`; simulation continues.

**Test — Reject:**
Click **[Reject]**.
Expected: Buttons disappear; chat shows `[dim]Plan rejected.[/dim]`; simulation continues
(mock does not block on rejection).

---

## T09 — Bash tier-3 approval gate (§16.1)

**Trigger:** Phase 2 — `pip install textual==0.89.0 --quiet` bash command (~25 s in).

**Observe:**
- Chat shows:
  ```
  ⚠ This command requires approval:  pip install textual==0.89.0 --quiet
  [Approve]  [Deny]
  ```
- The `LAST TOOL` sidebar shows `⏸ bash (pending approval)`.
- Input field is disabled.

**Test — Approve:**
Click **[Approve]**. Expected: Buttons disappear; after ~2 s, the bash result appears:
```
$ pip install textual==0.89.0 --quiet
Successfully installed textual-0.89.0
```

**Test — Deny:**
Click **[Deny]**. Expected: Buttons disappear; chat shows `✗ bash — user denied`.

**Note:** In mock mode the simulation resolves the tool after 2.5 s regardless of user
choice — the approval UI is exercised for visual verification.

---

## T10 — Token budget colour coding (§12.4)

**Trigger:** Three token budget updates across the simulation.

**Observe:**

| Phase | Percent | Expected sidebar colour |
|---|---|---|
| Startup | 2.5% | Green |
| Phase 1 end | 16.3% | Green |
| Phase 2 streaming | 61.9% | Yellow |
| Phase 3 start | 76.9% | Yellow |
| Phase 3 end | 90.3% | Red |

When the red-zone `token.budget.warning` event fires, the token budget panel should also
show a prominent warning indicator.

**Expected:** Colour transitions at exactly 60% and 86%.

---

## T11 — Session health alert (§4.5 / session.health_alert)

**Trigger:** Phase 3 start — fires after the QA role transition.

**Observe:**
- A banner or notification appears in the chat panel:
  ```
  ⚠ Context window at 77%
  Consider running /compact before the next task to free context
  ```
- The notification level is `warning` (yellow styling).

**Expected:** `SessionHealthEvent` handler renders the title + message distinctly from
regular `ui.notification` events.

---

## T12 — log.new → console panel direct write (§16.4)

**Trigger:** Multiple `log.new` events across all phases.

**Test:**
1. Press `ctrl+l` to open the console panel.
2. Observe lines arriving without any Python `logger` calls being made from `_on_log_new`.

**Expected log entries visible in console panel:**
```
[INFO] orchestrator: Agent session initialised — all services ready
[INFO] provider: local_lm_studio connected at http://127.0.0.1:1234/v1
[DEBUG] tool_registry: Loading tools for role: strategic (23 tools available)
[WARNING] search: VectorStore unavailable, falling back to grep
[INFO] orchestrator: Switching to operational role — write tools now available
[INFO] orchestrator: QA verification phase — linter + tests + syntax check
[INFO] mock_engine: Simulation complete — all §4.5 events exercised
```

**Anti-pattern check:** Confirm that a `log.new` event does NOT trigger another `log.new`
emission (no infinite loop). The console panel should stop receiving new lines after the
simulation ends.

---

## T13 — model.response token count

**Trigger:** Three streaming sequences complete (one per role phase).

**Observe (console panel or sidebar):**
- Phase 1 streaming → `model.response  tokens=347`
- Phase 2 streaming → `model.response  tokens=289`
- Phase 3 streaming → `model.response  tokens=512`

**Expected:** The bridge logs each `model.response` event; no UI crash or handler error.

---

## T14 — Streaming token display (§6.5)

**Trigger:** Each of the three streaming sequences.

**Observe:**
- Words appear one at a time in the current response area.
- A blinking cursor or spinner is visible while `partial=True` tokens arrive.
- The cursor clears when the final `partial=False` token is received.
- The chat panel scrolls automatically to keep the cursor visible (no animation stutter).

**Expected:** Smooth, flicker-free rendering; no full widget repaint between tokens.

---

## T15 — Role transitions (§4.5 / role.transition)

**Trigger:** Three `RoleTransitionEvent` messages during the simulation.

**Observe:**
- A divider line appears in the chat panel between each phase:
  ```
  >> SYSTEM → LEAD ARCHITECT
  >> LEAD ARCHITECT → FULL STACK ENGINEER
  >> FULL STACK ENGINEER → QA LEAD
  ```
- The `ACTIVE ROLE` sidebar updates to the new role name.
- The role banner colour changes:
  - Lead Architect → purple
  - Full Stack Engineer → blue
  - QA Lead → green

---

## T16 — Tool result formatting (§6.3 samples)

Run the simulation and verify each tool's formatted output in the chat panel:

| Tool | Expected format |
|---|---|
| `list_files` | `📁` for directories, `📄` for files, one per line |
| `grep` | `Found N matches:` then `  file.py:line: content` entries |
| `read_file` | `File: path\n────\n[content]` with `… [N more lines]` footer |
| `write_file` | `✓ Modified path  [+N / -N lines]` |
| `edit_file_atomic` | `✓ Modified path  [+1 / -1 lines]` |
| `git_status` | `Branch: main\n\nM  src/…\n` |
| `run_linter` | `❌ Linter: 2 warnings\n\n  file:line  CODE  message` |
| `syntax_check` | `✅ Syntax OK  (41 files checked)` |
| `run_tests` | `✅ Tests passed  (52 passed, 0 failed)` |
| `manage_todo` | `📋 TODO  (N/M done)\n  ✅/⬜ N. description` |
| `search_code` (error) | `✗ search_code — VectorStore not initialised…` |

---

## T17 — Slash command: /help

**Input:** `/help`

**Expected:** A block listing all 10 slash commands with one-line descriptions printed in
the chat panel. No agent call is made.

---

## T18 — Slash command: /clear

**Input:** `/clear` (after some conversation is visible).

**Expected:**
- Chat panel is cleared.
- Conversation history list (for input ↑/↓ recall) is preserved.
- Sidebar panels retain their last known state.

---

## T19 — Slash command: /new (or /reset)

**Input:** `/new`

**Expected:**
- Chat panel cleared.
- All sidebar panels reset to initial state (`—` / `idle`).
- `FILES MODIFIED` cleared.
- `PLAN PROGRESS` reset.
- A `session.new` event is published on the EventBus (visible in the console panel as an
  `[INFO] orchestrator` or `[INFO] bridge` line if logging is active).

---

## T20 — Slash command: /status

**Input:** `/status` (at any point during the simulation or after it ends).

**Expected output in chat panel:**
```
Agent running: No
Provider: local_lm_studio
Model: qwen2.5-coder-14b
Working dir: /workspace/project
Task ID: mock
History: N entries
```

---

## T21 — Slash command: /provider

**Input 1:** `/provider`
Expected: Numbered list of available providers from `SettingsStore`.

**Input 2:** `/provider 1` or `/provider local_lm_studio`
Expected: Active provider switches; sidebar `PROVIDER / MODEL` updates; a `model.routing`
event is published (visible in console panel).

---

## T22 — Slash command: /model

**Input 1:** `/model`
Expected: Numbered list of models for the active provider.

**Input 2:** `/model 2`
Expected: Active model switches; sidebar `PROVIDER / MODEL` updates.

---

## T23 — Slash command: /compact

**Input:** `/compact`

**Expected:**
- If orchestrator is available: context compaction runs on a background thread; chat shows
  `[dim]Context compacted: N → 1 message.[/dim]`.
- In mock mode (no orchestrator): bridge gracefully returns `False`; a notification is
  shown: `Compact not available in mock mode`.

---

## T24 — Slash command: /interrupt

**Input:** `/interrupt` while the mock simulation is still streaming.

**Expected:**
- `_cancel_event` is set.
- If the agent is running, the streaming stops.
- Input field is re-enabled immediately.
- A `[dim]Interrupted.[/dim]` notice appears in chat.

---

## T25 — Double-Esc interrupt

**Trigger:** Press Esc twice within 500 ms while streaming is in progress.

**Expected:**
- `force_interrupt()` is called.
- `_agent_running` is set to `False` immediately (no waiting for the thread to finish).
- Chat shows `Agent force stopped.`.
- Input field is re-enabled.
- A second double-Esc after the agent has stopped does nothing (not treated as an error).

---

## T26 — Input disabled while agent runs

**Trigger:** Type a message while the simulation is in its streaming phase (agent running).

**Expected:**
- Input field appears dimmed.
- Submitting a message (Enter) is silently rejected.
- Slash commands that don't invoke the agent (`/interrupt`, `/status`, `/help`, `/clear`)
  still work while the agent is running.

---

## T27 — Command history in input (§12.5)

**Setup:** Submit two or three messages (e.g. `/help`, `/status`, `test message`).

**Test:** Press ↑ in the input field.
Expected: The previous input (`test message`) is recalled. Press ↑ again → `/status`.
Press ↓ → moves forward through the history. History wraps correctly at both ends.

---

## T28 — Command palette (ctrl+o)

**Trigger:** Press `ctrl+o`.

**Observe:**
- Command palette overlay opens with a search field.
- Typing filters commands in real time.
- Sub-menus for `Switch Model` and `Connect Provider` are accessible by arrow keys / Enter.

**Test:** Navigate to a provider in `Connect Provider`. Press Enter.
Expected: `ProviderConfigScreen` opens, ready for API key entry.

---

## T29 — Settings screen (ctrl+s)

**Trigger:** Press `ctrl+s`.

**Observe:**
- Settings screen opens.
- Textual theme select is populated with available themes.
- Per-agent provider/model selects are shown for Lead Architect, Full Stack Engineer, and
  QA Lead.
- Context window size field shows the current value.

**Test:** Change the theme to `nord`. Press Save.
Expected: Theme applied immediately without restart. Setting persisted to
`~/.agent_tui/settings.json`.

---

## T30 — Provider config screen / API key save (§13)

**Trigger:** Open Settings → Connect Provider → select a cloud provider (e.g. OpenRouter).

**Observe:**
- `ProviderConfigScreen` opens with an API key input field.
- The field uses password masking.

**Test:** Enter a test key and press Save.
Expected:
- `SaveProviderCredentials` event posted.
- `src/config/providers.json` atomically updated (verify via `cat src/config/providers.json`).
- The key is NOT visible in the console panel or log file (§16 — no plaintext key logging).

---

## T31 — History persistence (§15)

**Setup:** Start the mock engine. Observe the final `AgentFinalResponse` is rendered. Quit
with `ctrl+q`.

**Test:** Restart the mock engine.
Expected:
- `~/.coding_agent/tui_conversation_history.json` is loaded on startup.
- Previous conversation entries are rendered in the chat panel before the new simulation
  begins.

**Corruption test:** Manually corrupt the JSON file (truncate it). Restart.
Expected: The bridge silently discards the corrupt file and starts fresh (no crash, no
error dialog).

---

## T32 — Console panel toggle (ctrl+l)

**Test 1:** Press `ctrl+l` during the simulation.
Expected: Console panel slides in from the left (30% width). Existing log lines visible.

**Test 2:** Press `ctrl+l` again.
Expected: Console panel slides out. Chat panel expands to full width.

**Test 3:** Open console during streaming.
Expected: New `log.new` lines appear in the console in real time without disrupting the
chat panel streaming.

---

## T33 — Theme switching

**Test:** Open Settings → change theme to each of:
`textual-dark`, `textual-light`, `nord`, `gruvbox`, `catppuccin-mocha`, `dracula`.

**Expected for each:**
- Theme applies immediately (background, text, border colours change).
- All 8 sidebar panels remain visible and readable.
- Token budget colour coding continues to respect §12.4 thresholds (green/yellow/red)
  regardless of theme.

---

## T34 — Shutdown sequence (§10.2)

**Trigger:** Press `ctrl+q` or type `/quit`.

**Expected:**
1. `_cancel_event` is set (any running agent interrupted).
2. Agent thread joined (max 5 s).
3. Conversation history atomically saved to
   `~/.coding_agent/tui_conversation_history.json`.
4. All 31 EventBus subscriptions unsubscribed (`cleanup()` called).
5. Process exits cleanly (exit code 0).

**Verify:** Re-run the mock engine after quitting. Confirm history is loaded and the
application starts without errors.

---

## T35 — No recursive log.new loop (§16.4)

**Setup:** Open the console panel before the simulation starts.

**Test:** Watch the console panel log lines throughout the full simulation. Count entries.

**Expected:** The total number of log lines is bounded (≤ 15 for a full simulation run).
If any handler were feeding `log.new` back into `logger.*`, the count would grow
unboundedly. A bounded count confirms the §16.4 rule is respected.

---

## T36 — AgentFinalResponse markdown rendering

**Trigger:** End of Phase 3 — `AgentFinalResponse` fires with a markdown compliance table.

**Observe:**
- The response is rendered in the chat panel with markdown:
  - `## Spec-Compliance Achieved` rendered as a heading.
  - The table (`| Category | Events covered |`) rendered as a formatted table or at minimum
    with pipe-delimited columns.
  - Checklist items (`- [x]` / `- [ ]`) rendered with ticks.

---

## T37 — Edge case: duplicate agent submission

**Setup:** Modify the input field to submit two messages in rapid succession (or use
`/interrupt` then immediately send a message before the flag clears).

**Expected:** Only the first message is accepted. The second is silently rejected with the
`_agent_running` guard. No second agent thread is spawned.

---

## T38 — Edge case: EventBus unsubscribe on cleanup

**Verify (developer check):**
After quitting (`ctrl+q`), re-import the MockEventBus in a Python REPL:
```python
from src.ui.mock_eventbus import get_mock_event_bus
bus = get_mock_event_bus()
print(bus._subscribers)
```

**Expected:** All subscription lists are empty (or contain only entries from other
consumers). No stale callbacks from the previous `AgentBridge` instance.

---

## Summary — Coverage Matrix

| Spec Section | Tests |
|---|---|
| §4.5 EventBus events (all 21 published) | T01–T13 |
| §6 Tool display (3-beat lifecycle, formatted results) | T02, T03, T04, T16 |
| §6.4 Diff preview | T05 |
| §9 Threading contract | T24, T25, T26, T37 |
| §10 Lifecycle (startup, shutdown, new, continue) | T01, T19, T34 |
| §11 Slash commands (all 10) | T17–T24 |
| §12 UI panels (all 8) | T01, T06, T07, T10, T15 |
| §12.4 Token budget colour coding | T10 |
| §12.5 Input field (history, autocomplete, disabled) | T26, T27 |
| §13 providers.json atomic write | T30 |
| §14.1 Plan approval UI | T08 |
| §15 History persistence | T31 |
| §16.1 Bash tier-3 gate | T09 |
| §16.4 No recursive log.new | T12, T35 |
| Screens (palette, settings, provider config) | T28, T29, T30 |
| Theme switching | T33 |
| Streaming (flicker-free, cursor, scroll) | T14 |
| Markdown rendering | T36 |
