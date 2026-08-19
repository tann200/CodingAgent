# Textual TUI — TUI System Specification v2.0 Compliant

## Overview

This project develops a production-grade, Textual-based Terminal User Interface (TUI) that serves as a TUI System Specification v2.0 compliant drop-in replacement. It is designed to be fully decoupled from the underlying Large Language Model (LLM) engine, interacting exclusively through the Orchestrator public API, EventBus (`get_event_bus()`), and ProviderManager. The TUI features a comprehensive simulation mode, allowing for testing and demonstration of all specified events across a realistic multi-phase agent run, inspired by leading TUI patterns.

## User Preferences

The user prefers an iterative development approach. They want clear communication regarding proposed changes and explanations of decisions. They also prefer to be asked before any major changes are made to the codebase or before the agent performs sensitive actions (e.g., executing bash commands or modifying files). The user likes functional programming paradigms where appropriate and favors detailed explanations over terse summaries.

## System Architecture

The TUI architecture emphasizes a strict decoupling pattern where the UI never directly imports core backend components. Instead, all communication flows through an `AgentBridge` that translates backend `EventBus` events into Textual messages for UI consumption and UI actions back into backend events. This ensures a clean separation of concerns and allows for a `MockEventBus` and `MockSimulationApp` to fully simulate agent interactions without a live backend.

**Core Components & Decoupling:**
- **`AgentBridge`** (`src/ui/core_bridge.py`): Subscribes to **37** `EventBus` events, translates them to Textual messages, handles threading, history persistence, session lifecycle, and approval flows (plan/bash). Falls back to `MockEventBus` when the real core is unavailable. Public methods: `setup_subscriptions()`, `cleanup()`, `send_prompt()`, `interrupt()`, `force_interrupt()`, `approve_plan()`, `reject_plan()`, `bash_approved()`, `bash_denied()`, `publish()`, `save_history()`, `clear_history()`, `compact_context()`, `start_new_session()`, `restore_and_continue()`.
- **`bus.py` / `events.py`**: Define a comprehensive set of Textual message types for UI-to-backend and backend-to-UI communication, ensuring all interactions are message-driven. Includes `GitBranchEvent` for sidebar git status, `RetryAttemptEvent`, `RetrySucceededEvent`, `RetryFailedEvent`, `ContextDegradedEvent`, `TaskQueueUpdatedEvent`.
- **`app.py` (Main `AgentApp`)**: Manages the UI lifecycle, handles all Textual messages, orchestrates tool call displays, plan/bash approval interfaces, and processes slash commands. Includes `handle_git_branch` (populates `#sb_git`), cost accumulation in `handle_token_budget`/`handle_token_usage` (populates `#sb_cost`), token breakdown In/Out display (`#sb_context`), `_tool_call_count` counter (`#sb_tool_count`), inline chat widgets for retry/context events, and file-deleted visual distinction in `#sb_files`.
- **`mock_engine.py`**: Provides a `MockSimulationApp` for development and testing, publishing all relevant events in a 4-phase simulation (Startup, Lead Architect, Full Stack Engineer, QA Lead) with ACP schema compliance. Exercises all 37 subscribed EventBus events.

**Bridge EventBus Subscriptions (37 total):**
provider/model: `orchestrator.startup`, `provider.status.changed`, `provider.models.list`, `provider.models.cached`, `provider.models.empty`, `provider.model.missing`, `model.routing`, `model.response`, `model.token`
tool: `tool.execute.start`, `tool.invoked`, `tool.execute.finish`, `tool.execute.error`
file: `file.diff.preview`, `file.modified`, `file.deleted`
plan: `plan.progress`, `plan.requested`
session: `session.new`, `session.hydrated`, `session.registered`, `session.unregistered`, `session.health_alert`
notifications: `ui.notification`, `log.new`
token: `token.budget.update`, `token.budget.warning`
role: `role.transition`
preview: `preview.pending`, `preview.confirmed`, `preview.rejected`
git: `git.branch`
retry/resilience: `retry.attempt`, `retry.succeeded`, `retry.failed`
context: `context.degraded`
task: `task.queue.updated`

**UI/UX and Features:**
- **Layout**: Features 8 required panels including chat log, input, task status, plan progress, tool activity, token budget, provider/model info, and working directory, alongside an expanded sidebar (width 44) with widgets: ACTIVE ROLE, SESSION, FILES MODIFIED, TOKEN BUDGET, TOKEN BREAKDOWN (`#sb_context`), SESSION COST (`#sb_cost`), PROVIDER/MODEL, GIT (`#sb_git`), WORKING DIR, TOOLS CALLED (`#sb_tool_count`), and a full-height `ConsolePanel`.
- **Components**: Utilizes `StreamView` for efficient, append-only streaming with reactive updates, `AgentArtifact` for differential rendering, `HistoryInput` for command history and interrupt functionality, `ThinkingProcess` for collapsible reasoning, `ConsolePanel` for direct log output, and `SideBySideDiff` (`src/ui/components/diff_viewer.py`) for two-column unified-diff rendering with Accept/Reject buttons.
- **`SideBySideDiff`**: Parses unified diff hunks into paired OLD/NEW line columns; renders each side in a scrollable horizontal layout; posts `SideBySideDiff.Accepted` / `SideBySideDiff.Rejected` Textual messages on button press. Button style (`border: none; height: 1`) is applied via `app.tcss` rule `SideBySideDiff .sbs_actions Button` to override Textual's built-in button rendering. Stores `self._path` for duplicate-mount guard in `handle_file_modified`.
- **Inline chat widgets for events**: Retry attempt/succeeded/failed and context.degraded events mount coloured `Static` widgets into the chat log (class `.retry_msg` — no italic, markup-driven colour). File accepted/rejected diffs, plan approved/rejected, bash approval warning, and bash allowed/denied also use `.retry_msg` for visual consistency.
- **FILES MODIFIED sidebar**: Shows green `✓` for modified files and red `✗` for deleted files (paths starting with `[deleted]`). Last 5 files shown.
- **Duplicate-diff guard**: `handle_file_modified` checks for existing `SideBySideDiff` widgets with matching `_path` before mounting an `AgentArtifact` fallback diff.
- **Screens**: Includes `CommandPalette` for searchable menus, `SettingsScreen` (rewritten — reactive model filtering on `Select.Changed`, API Keys section per provider with status dot `●/○`, password input, and Test button; settings box width 72), `ProviderConfigScreen` for API key entry, and `ProbeResultsScreen`.
- **Slash Commands**: Implements all 10 standard slash commands for session management, control, and information display (e.g., `/help`, `/new`, `/interrupt`, `/settings`).
- **Key Bindings**: Standardized key bindings for common actions like `ctrl+o` (Command palette), `ctrl+s` (Settings screen), and `Esc×2` (interrupt agent).
- **Theming**: Supports Textual's built-in theme switching, offering a variety of dark and light themes.
- **Performance Optimizations**: Employs reactive watchers, differential rendering, non-animated scrolling, a bus pattern for decoupling, and atomic writes for efficient and responsive UI.

**Security & Data Handling:**
- **Security Rules**: Enforces Bash tier-3 gate approval, read-before-write display for file operations, plan mode write blocking, and direct logging to prevent recursive `log.new` events.
- **Storage**: Manages local UI settings, structured log files, conversation history, and provider API keys in designated paths with atomic write operations to ensure data integrity.

## CSS Notes

- `app.tcss` is the **sole source of truth** for all widget styles. No `DEFAULT_CSS` blocks exist in any widget class — all styles are defined externally in `app.tcss`. This means `app.tcss` rules apply at full precedence with no risk of internal CSS overrides.
- `SideBySideDiff` styles (including `.sbs_header`, `.sbs_panels`, `.sbs_col`, `.sbs_col_old`, `.sbs_col_label`, `.sbs_content`, `.sbs_actions`, `.sbs_actions Button`, `.sbs_prompt`) all live in `app.tcss`.
- Token budget colour thresholds: 0–60% `#22c55e` (green), 61–85% `#facc15` (yellow), 86–100% `#ff5555` (red).
- `.retry_msg { padding: 0 1; margin: 0 0; }` — used for retry/context-degraded/SideBySideDiff outcome/plan approval/bash outcome/session health widgets. No italic; colour is driven entirely by inline Rich markup.
- `.system_msg { color: #888888; text-style: italic; }` — used only for plain grey informational messages (status, /help, /compact, /status output, role transition banner, etc.).

## Known Design Constraints

- **TOKEN BREAKDOWN (`#sb_context`)** shows "In: 0 | Out: 0" in mock mode because `mock_engine.py` does not publish per-role token breakdown data. Real backend must emit `token.usage`-type events for this widget to populate. `handle_token_usage` (triggered by `TokenUsageEvent`) handles real-backend accumulation; `handle_token_budget` (triggered by `TokenBudgetEvent`) handles budget bar only.
- **`model.response` tokens**: Bridge logs token counts from `model.response` events but does not update the token breakdown widget (no per-role split available in the payload).
- **All 36 bridge paths are exercised by `mock_engine.py`**, including `session.new` (fires once at startup Phase 0 to clear stale state) and `role.transition` (published via EventBus, routed through the `_on_role_transition` bridge translator rather than direct `post_message`).

## External Dependencies

- **Orchestrator public API**: Primary interface for agent control and interaction.
- **EventBus (`get_event_bus()`)**: System-wide event broadcasting mechanism.
- **ProviderManager**: Manages and interacts with various LLM providers.
- **Textual**: TUI framework for building the user interface.
- **LLM Providers**:
    - Local LM Studio
    - Groq
    - OpenRouter
    - DeepSeek
    - OpenAI
    - Anthropic
    - Google
    - Copilot
    - Zai
- **`src/config/providers.json`**: Stores user API keys and provider configurations.
