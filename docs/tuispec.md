# TUI Overview

## Purpose

A production-grade terminal user interface (TUI) built with the Textual framework, optimized for flicker-free token streaming at 100+ tokens per second. The UI is fully decoupled from the LLM engine through a message bus pattern and communicates with the real LangGraph backend via the WorkflowController.

Inspired by opencode, and Claude Code TUI patterns.

---

## Architecture

### Decoupling via Message Bus

The core architectural principle is **complete separation of UI and backend**. The UI layer never imports or calls any LLM engine directly. Instead, all communication flows through typed `Message` subclasses defined in two modules:

- **`bus.py`** defines backend-to-UI events (data flowing from the engine to the display)
- **`events.py`** defines UI-to-backend events (user actions flowing from the display to the engine)

The `AgentApp` registers `@on()` handlers for each message type. The backend posts messages to the app, and the app reacts purely to those typed events.

### Telemetry Bridge Pattern

The `WorkflowController` bridges the LangGraph engine to the Textual UI:

1. **Engine → UI**: The controller connects to the telemetry singleton (`get_session().register_ui_callback()`). Every time the telemetry system emits an event, it flows through the bridge to the Textual message bus.

2. **UI → Engine**: When the user submits a prompt, the controller creates an async task that runs the LangGraph workflow. Events are streamed via `graph_app.astream_events()` and forwarded to the UI.

### Provider Banner

The provider banner at the top of the UI displays the currently selected provider from `UserPrefs.selected_model_provider`. On startup, if no providers are configured, a warning notification guides the user to connect a provider.

### Settings Decoupling Flow

The UI never imports `src/core` directly. Configuration reaches the UI through bus events:

1. On mount, the UI checks for provider connectivity
2. If providers exist, validates the connection
3. When the user changes provider credentials, the UI posts `SaveProviderCredentials`
4. When the user changes a role's model, the UI posts `UpdateRoleModel` and `UpdateRoleProvider`

### Differential Rendering

Three optimizations eliminate flicker during high-frequency updates:

1. **`StreamView`** uses `reactive("", layout=False, repaint=True)` so only the changed text triggers a repaint, with no layout recalculation.
2. **`AgentArtifact`** uses the same reactive pattern for its content property. The watcher rebuilds the renderable (Markdown, diff, or plain text) only when content actually changes.
3. **`scroll_end(animate=False)`** during streaming prevents animation queue buildup that would cause visual stuttering.

### Settings Persistence

Settings are stored in `~/.agent_tui/settings.json`. The `SettingsStore`:

- Provides default values for all fields
- Stores `available_providers` list from the backend
- Supports per-agent provider/model configuration
- Loads provider configuration from `UserPrefs`

### Structured Logging

All components use a centralized logging system that writes to both:

- **File**: `~/.agent_tui/logs/agent.log` (persistent, all levels)
- **In-memory buffer**: A ring buffer (500 lines) with callback support, feeding the live console panel

---

## Execution Modes

The agent uses a single runtime role (`local_coding_agent`) with 6 execution modes that determine behavior, available tools, and workflow. Mode is the primary user-facing abstraction.

| Mode | Slash Command | Tab Key | Color | Tools | Workflow |
|------|--------------|---------|-------|-------|----------|
| Chat | `/chat` | ✅ | Purple | 13 read-only | discuss.md |
| Plan | `/plan` | ✅ | Blue | 14 planning | plan-phase.md |
| Implement | `/implement` | ✅ | Green | 22 full | gsd-execution.md |
| Review | `/review` | ✅ | Amber | 16 validation | verification.md |
| Debug | `/debug` | ✅ | Red | 17 diagnostic | debug.md |
| Map | `/map` | ✅ | Cyan | 12 exploration | map-codebase.md |

Modes are cycled via the `Tab` key binding or by typing a slash command (e.g., `/implement`). Each mode has a distinct color in the sidebar and status bar. The command palette (ctrl+o) also offers mode-switching commands.

**Subagent roles** (Full Stack Engineer, QA Lead) are only active during delegation and are configured separately in Settings.

---

## Providers

Defined in `src/core/providers.json` with 9 providers:

Local LM Studio, Groq, OpenRouter, DeepSeek, OpenAI, Anthropic, Google, Copilot, Zai

Each provider entry includes: `name`, `base_url`, `api_key_env`, `type`, and `models` list.

---

## File and Folder Structure

```
src/
  core/
    __init__.py
    defaults.py                     Centralized defaults (PROVIDER_MODEL_DEFAULTS, DEFAULT_SETTINGS)
    providers.json                  Provider definitions (9 providers with models, base URLs, API key env vars)
    graph.py                       LangGraph workflow (Phase 5 deterministic runtime)
    telemetry.py                   Telemetry singleton (AgentRunSession)
    state.py                      AgentState dataclass
    message_manager.py             Message pruning and token management

  ui/
    __init__.py
    app.py                          Main AgentApp class (event handlers, layout, key bindings)
    bus.py                          Backend-to-UI message types
    events.py                       UI-to-backend message types
    settings.py                     Settings store (loads from UserPrefs)
    logging.py                      Centralized structured logging
    widgets.py                      DiffViewer widget
    coordinator.py                  Data coordinator (decoupled)
    controller.py                   WorkflowController (telemetry bridge to LangGraph)

    components/
      __init__.py                   Component exports
      artifact.py                   AgentArtifact (reactive rendering)
      stream_view.py                StreamView (append-only streaming)
      history_input.py              HistoryInput (command history + double-Esc)
      thinking.py                   ThinkingProcess (collapsible reasoning)
      console.py                    ConsolePanel (log viewer)
      cards.py                      ProviderCard (status display)

    features/
      palette/
        __init__.py
        screen.py                   CommandPalette modal screen
        logic.py                    Menu structure and action resolution
      settings/
        __init__.py
        screen.py                   SettingsScreen + ProviderConfigScreen

    screens/
      __init__.py
      probe_results.py              ProbeResultsScreen modal

    styles/
      app.tcss                      All application styles
```

---

## File Descriptions

### Core Modules

#### `app.py` - Main Application

The `AgentApp` class is the central Textual `App` subclass. It:

- Composes the full layout: header, provider banner, main workspace (console pillar + chat column + sidebar), input, and footer
- Declares reactive properties: `active_role`, `total_tokens`, `context_window`, `pending_tasks`, `queue_size`, `is_streaming`
- Registers `@on()` handlers for every bus event type and UI event type
- Manages stream widget lifecycle (`_ensure_stream_widget`, `_finalize_stream`)
- Implements all key binding actions
- On mount, posts `RequestSystemSettings` and applies loaded theme
- Handles `SystemSettingsLoaded` to populate settings and context window
- Passes shared `self._settings` store to `SettingsScreen` and `CommandPalette`
- Resolves smart paste payloads via `input_widget.resolve_submitted_text()` before processing user prompts
- Truncates large paste displays in chat to 200 chars with character count

#### `bus.py` - Backend Event Types

Defines `Message` subclasses that flow from the backend to the UI:

| Event | Purpose |
|-------|---------|
| `StreamChunkEvent` | Single token/chunk during streaming |
| `StreamingThinkingUpdate` | Thinking/reasoning content during streaming |
| `DisplayReasoning` | Complete reasoning block to display |
| `StatusUpdate` | Status message for the sidebar and chat |
| `ToolExecutionNotice` | Tool call notification with name and arguments |
| `AgentFinalResponse` | Complete response content (rendered as artifact) |
| `WorkerError` | Error message with optional traceback |
| `ModeTransitionEvent` | Execution mode change (chat/plan/implement/review/debug/map) |
| `RoleTransitionEvent` | Subagent delegation role change (FSE/QA handoff) |
| `TokenUsageEvent` | Token counts (system, task, tools, total) and context window |
| `TaskQueueUpdatedEvent` | Task queue state change |
| `FileModifiedEvent` | File change notification with optional diff |
| `TaskEscalatedEvent` | Task escalation with reason and retry count |
| `ContextDegradedEvent` | Context window degradation warning |
| `RetryAttemptEvent` | Retry attempt notification |
| `RetrySucceededEvent` | Retry succeeded |
| `RetryFailedEvent` | All retries exhausted |
| `ProviderStatusChangeEvent` | Provider connection status change |
| `SystemSettingsLoaded` | Backend settings + available providers list |
| `PolicyBannerEvent` | Execution policy from backend |
| `UpdateSidebarData` | Generic sidebar data update |

All event classes call `super().__init__()` as their last statement for Textual 8.0 compatibility.

#### `events.py` - UI Event Types

Defines `Message` subclasses for UI-originated actions:

| Event | Purpose |
|-------|---------|
| `PaletteCommand` | Command selected from the command palette |
| `ConnectProvider` | Request to connect a provider (triggers config screen) |
| `UpdateSettings` | Settings were changed (carries key-value updates) |
| `SlashCommand` | Slash command entered in input (e.g., `/help`, `/clear`) |
| `AgentInterrupt` | Agent interrupt signal (from double-Esc) |
| `ConsoleLogLine` | Log line for the console panel |
| `SaveProviderCredentials` | Forward provider API key to backend |
| `UpdateRoleModel` | Update a specific role's model selection (with provider_id) |
| `UpdateRoleProvider` | Update a specific role's provider selection |

#### `controller.py` - Workflow Controller

The `WorkflowController` bridges the LangGraph engine to the Textual UI:

- **`_wire_telemetry_bridge()`**: Connects `AgentRunSession` singleton to Textual. Registers a callback that forwards telemetry events to the UI via `app.post_message()`.

- **`start_agent_worker(user_prompt)`**: Creates an async task that runs the LangGraph workflow.

- **`_run_agent_workflow(user_prompt)`**: Executes the full LangGraph workflow:
  1. Creates `AgentState` with user prompt
  2. Streams events via `graph_app.astream_events()`
  3. Forwards events: chat model chunks, tool starts/ends, token usage
  4. Posts final response to UI

- **`cancel_all()`**: Cancels all running agent tasks (called on double-Esc interrupt)

#### `mock_engine.py` - Mock Backend

`MockSimulationApp` extends `AgentApp` and runs an automated simulation on mount. It:

- Loads `src/core/providers.json` on startup
- Handles `RequestSystemSettings` by responding with `SystemSettingsLoaded` containing providers and default settings
- Handles `SaveProviderCredentials` and `UpdateRoleModel` events
- Exercises every event type in a realistic sequence

Simulation sequence:
1. Provider connection (`ProviderStatusChangeEvent`)
2. Mode transition to implement (`ModeTransitionEvent`)
3. Task queue initialization (`TaskQueueUpdatedEvent`)
4. Status updates and tool execution notices
5. High-speed token streaming (100 tok/s via `StreamChunkEvent`)
6. Token usage updates (`TokenUsageEvent`)
7. Retry simulation (`RetryAttemptEvent` + `RetrySucceededEvent`)
8. Role transition to full stack engineer
9. File modifications with diff display (`FileModifiedEvent`)
10. Context degradation warning (`ContextDegradedEvent`)
11. Task escalation (`TaskEscalatedEvent`)
12. Role transition to QA lead
13. QA verification streaming
14. Final response with markdown table (`AgentFinalResponse`)
15. Final token count and task queue completion

#### `settings.py` - Settings Store

`SettingsStore` provides a local UI cache with:

- **Defaults**: theme, per-agent provider/model selections (local_coding_agent, full_stack_engineer, qa_lead), console/sidebar visibility, context window, active mode
- **System settings**: Populated via `apply_system_settings(settings, providers)` when `SystemSettingsLoaded` arrives
- **Available providers**: Stored from backend, accessible via `self.available_providers`
- **Per-agent config**: `get_agent_provider(role_id)` / `get_agent_model(role_id)`
- **Provider lookup**: `get_provider_by_id(provider_id)` searches available providers
- **Load/save**: Reads from `~/.agent_tui/settings.json`, merges with defaults
- **No src/core imports**: All configuration comes through bus events

#### `logging.py` - Centralized Logging

Sets up a single `agent_tui` logger with two handlers:

- **FileHandler**: Writes all log levels to `~/.agent_tui/logs/agent.log`
- **InMemoryHandler**: Ring buffer (500 lines) with callback registration for live display

The `InMemoryHandler` supports:
- `register_callback(fn)` / `unregister_callback(fn)` for live consumers
- `get_lines()` to retrieve buffered log history

Format: `HH:MM:SS [LEVEL  ] component_name: message`

Child loggers are created via `get_logger("component_name")` (e.g., `get_logger("app")`, `get_logger("palette")`).

### Components

#### `stream_view.py` - StreamView

Append-only streaming widget optimized for high-frequency updates:

- `_buffer` is a `reactive("", layout=False, repaint=True)` property
- `append_chunk(chunk)` concatenates to `_raw` and sets `_buffer`, triggering the watcher
- `watch__buffer()` rebuilds a `Rich.Text` with role prefix styling and calls `self.update()`
- `finalize()` returns the accumulated raw text

#### `artifact.py` - AgentArtifact

Reactive content display supporting multiple render modes:

- `content` is a `reactive("", layout=False, repaint=True)` property
- `watch_content()` calls `_build_renderable()` which returns:
  - **diff**: `Rich.Syntax` with diff lexer inside a green `Panel`
  - **markdown**: `Rich.Markdown` inside a blue `Panel` (with sanitization fallback)
  - **plain text**: Raw `Rich.Text`
- `append_chunk()` supports incremental content building

#### `history_input.py` - HistoryInput

Extended `Input` widget with command history, interrupt support, and smart paste handling:

- **Command history**: Up/down arrow keys navigate through previous inputs (100-entry cap)
- **Double-Esc interrupt**: Two Escape presses within 500ms emit `InterruptSignal`
- Both Escape presses are consumed (`prevent_default` + `stop`) to prevent screen dismissal
- History is managed via `_history` list and `_history_index` pointer

**Smart Paste**:
- Intercepts `on_paste` events and checks line count
- **Small pastes (3 lines or fewer)**: Newlines collapsed to spaces, inserted at cursor position
- **Large pastes (more than 3 lines)**: Raw text stored in `_hidden_paste_payload`, input shows `[Pasted ~N lines]` placeholder tag, cursor placed after tag so user can keep typing
- **Very large pastes (over 20,000 chars)**: Warning notification displayed
- On submission, `resolve_submitted_text()` replaces the placeholder tag with actual payload
- History stores the resolved (full) text, not the placeholder
- `_resolved_text` is cached so both the widget and app handler get the correct resolved value

#### `thinking.py` - ThinkingProcess

Collapsible reasoning display:

- Shows a summary header with elapsed time on initial render
- Click toggles between collapsed (summary) and expanded (full Markdown content) states
- Pulse animation on mount (brief border color change via CSS class toggle)
- Content sanitization for non-printable characters

#### `console.py` - ConsolePanel

Full-height vertical pillar log viewer (dock: left, 30% width):

- Docked to the left side of the main workspace with a border separator
- On mount, loads buffered log history from `InMemoryHandler.get_lines()`
- Registers a callback to receive new log lines in real time
- Color-codes lines by log level (DEBUG=gray, INFO=blue, WARNING=yellow, ERROR=red)
- Uses `RichLog` with `auto_scroll=True` and `wrap=True` for proper scrolling and long-line handling
- Container has `overflow-y: auto` to prevent clipping
- Hidden by default, toggled with ctrl+l (uses `display: none` via `.hidden` class)
- 500-line cap via `max_lines`

#### `cards.py` - ProviderCard

Simple status display widget showing `provider_name: status`.

#### `widgets.py` - DiffViewer

Utility widget for showing unified diffs or new file content using `Rich.Syntax`.

### Features

#### `features/palette/screen.py` - CommandPalette

Modal screen with searchable, nested command menus:

- Receives shared `settings_store` parameter from app (shares the same SettingsStore instance)
- Root menu organized into categories: Suggested, Session, Provider, System
- Sub-menus for provider connection (lists all 9 providers from providers.json) and model selection (lists all models across providers)
- Model selection applies to the currently active role via `UpdateRoleModel(role=active_role, model_id=...)`
- Back navigation via Escape (pops menu stack) or dismisses if at root
- Real-time filter search across current menu level
- Breadcrumb display showing navigation path
- Posts `PaletteCommand`, `ConnectProvider`, or `UpdateRoleModel` messages on selection

#### `features/palette/logic.py` - Menu Logic

Defines the menu structure and builds dynamic menus from system data:

- `build_root_menu()` returns the nested menu dictionary
- `get_provider_menu(available_providers)` returns providers from the system-provided list
- `get_model_menu(available_providers)` builds model selection from all providers' model lists
- `filter_commands(items, query)` filters menu items by search text
- `find_action_in_menu()` resolves an item ID to its action string

#### `features/settings/screen.py` - Settings Screens

Two modal screens (width: 60, matching CommandPalette):

**SettingsScreen**: Scrollable settings form using Textual `Select` widgets:
- Receives `settings_store` parameter from app
- **General**: Theme selector (Textual themes with live preview)
- **Per-agent config**: Combined "Provider / Model" dropdown for Lead Architect, Full Stack Engineer, and QA Lead
  - Options formatted as "ProviderName / ModelName" (e.g., "Local / qwen2.5-coder-14b")
  - Value stored as "provider_id::model_id"
- **Context**: Context window size selector (8K to 200K)
- Save posts `UpdateSettings`, `UpdateRoleModel`, and `UpdateRoleProvider` events
- Buttons: Compact (height: 1, no border), with hover states

**ProviderConfigScreen**: API key configuration form:
- Password-masked input for API key
- Posts `SaveProviderCredentials` event

### Screens

#### `screens/probe_results.py` - ProbeResultsScreen

Modal screen displaying provider probe results in a scrollable view with status, details, models, and resolved information per provider.

### Styles

#### `styles/app.tcss` - All Application Styles

Single stylesheet loaded by `AgentApp` via `CSS_PATH`. Contains all styles for:

- Screen background and layout
- Provider banner (with connected/error state classes)
- Header
- Main workspace layout (horizontal split: console pillar + chat column + right sidebar)
- Chat log
- Console panel (dock: left, 30% width, 100% height, border-right separator, overflow-y: auto, with hidden class)
- Message types (user, error, system, stream)
- HistoryInput
- Footer status bar
- Right sidebar (with hidden class, section titles)
- DiffViewer
- Settings screen (box, scroll area, sections, field labels, actions, Select widgets)
- Provider config screen
- Command palette (container, header, input, options, highlight)
- ThinkingProcess (pulse animation, expanded state, header, content, time label)
- AgentArtifact and StreamView
- ProbeResultsScreen

---

## Key Bindings

| Binding | Action | Description |
|---------|--------|-------------|
| `ctrl+o` | `action_show_commands` | Open the command palette |
| `ctrl+s` | `action_open_settings` | Open the settings screen |
| `ctrl+l` | `action_toggle_console` | Show/hide the console log panel |
| `tab` | `action_toggle_mode` | Cycle through agent roles (Lead Architect, Full Stack Engineer, QA Lead) |
| `Esc Esc` | `InterruptSignal` | Interrupt the agent (double-tap within 500ms) |
| `ctrl+q` | `action_quit` | Quit the application |
| `up` | `action_history_up` | Previous command in input history |
| `down` | `action_history_down` | Next command in input history |

---

## Slash Commands

Entered in the input field with a `/` prefix:

| Command | Action |
|---------|--------|
| `/help` | Show available commands |
| `/settings` | Open settings screen |
| `/console` | Toggle console panel |
| `/clear` | Clear session (chat log, tokens, files, tasks) |
| `/status` | Show current role and token usage |

---

## Sidebar Sections

The right sidebar displays 7 live-updated sections:

1. **SESSION** - Pending task count and queue size
2. **Status** - Current operation status message
3. **CONTEXT & MODEL** - System, task, and tool token breakdown + model name
4. **TELEMETRY** - Total tokens used vs context window with percentage
5. **FILES MODIFIED** - List of recently modified files (last 5)
6. **ACTIVE ROLE** - Current agent role with color coding
7. **PROVIDER** - Provider connection status

---

## Textual Themes

Theme switching uses Textual's built-in `app.theme` property with live preview in the settings screen. Available themes include:

textual-dark, textual-light, nord, gruvbox, catppuccin-mocha, catppuccin-latte, dracula, tokyo-night, monokai, solarized-light, flexoki, textual-ansi, github-light, github-dark, galaxy, nebula, cobalt, vscode-dark, and more.

---

## Data Flow

```
LangGraph Engine (src/core/graph.py)
    |
    | AgentRunSession.emit() → telemetry events
    v
WorkflowController._ui_callback
    |
    | app.post_message(bus_event)
    v
AgentApp (@on handlers)
    |
    |-- Updates reactive properties (active_role, total_tokens, etc.)
    |-- Mounts widgets to chat_log (StreamView, AgentArtifact, ThinkingProcess)
    |-- Updates sidebar Static widgets
    |-- Updates status bar
    |-- Logs via structured logging
    v
Textual Reactive System
    |
    |-- Triggers watch_* methods only on actual value change
    |-- Batches repaints within animation frame
    |-- Repaints only affected screen regions
    v
Terminal Output (differential, no flicker)

---

User Prompt Flow:
User types → HistoryInput.submit → controller.start_agent_worker()
    → LangGraph astream_events() → telemetry events → UI updates
```

---

## Running

```bash
python -m src.main
```

Or via PowerShell:

```bash
.\start.ps1
```

This launches `AgentApp`, which:
1. Checks provider connectivity on mount
2. Wires the telemetry bridge to the LangGraph engine
3. Displays the selected provider in the banner

---

## Test specification (TUI)

This section provides a concrete test plan for the Textual UI. Tests are organized into unit tests for widgets and features, and integration tests that exercise the controller/telemetry bridge and the mock engine. Use pytest and existing test conventions in the repo (see tests/). Each test below includes the target file(s), the intended assertions, and a suggested pytest file path.

General test commands

```bash
# run all UI unit tests
python -m pytest tests/unit -q -k ui
# run all tests (including UI integration)
python -m pytest -q
# run a single test file
python -m pytest tests/unit/test_stream_view.py -q
```

Unit tests (fast, isolated)

- tests/unit/test_stream_view.py
  - Target: `src/ui/components/stream_view.py`
  - Purpose: verify streaming append behavior and renderable update.
  - Assertions:
    - `append_chunk()` increases internal `_raw` and sets `_buffer` to the expected value.
    - `finalize()` returns the accumulated string.
    - Calling `append_chunk()` many times (simulate 100 chunks) does not raise and `_buffer` equals joined chunks.

- tests/unit/test_agent_artifact.py
  - Target: `src/ui/components/artifact.py`
  - Purpose: verify `AgentArtifact` reactive content, append_chunk merging, and rendering mode selection (markdown/diff/plain).
  - Assertions:
    - `append_chunk()` accumulates and `content` reactive property reflects final content.
    - `_build_renderable()` returns a Panel for markdown, Syntax for diff, and Text for plain.

- tests/unit/test_history_input.py
  - Target: `src/ui/components/history_input.py`
  - Purpose: test paste handling, history navigation, and double-Esc interrupt.
  - Assertions:
    - Small paste (<=3 lines) collapses to single-line insertion.
    - Large paste stores `_hidden_paste_payload` and input shows placeholder tag.
    - `resolve_submitted_text()` returns the original payload when present.
    - Double Escape within `ESC_DOUBLE_TAP_MS` posts `HistoryInput.InterruptSignal` (use a fake/spy message bus or subclass to capture posted messages).

- tests/unit/test_console_panel.py
  - Target: `src/ui/components/console.py` and `src/ui/logging.py`
  - Purpose: ensure `ConsolePanel` loads existing buffer lines and registers callbacks.
  - Assertions:
    - `ConsolePanel.on_mount()` calls `get_memory_handler().register_callback` (use a mock handler or monkeypatch `get_memory_handler`).
    - `append_line()` results in the memory handler receiving the formatted message.

- tests/unit/test_file_modification_panel.py
  - Target: `src/ui/components/file_modification_panel.py`
  - Purpose: file listing and diff preview formatting behavior.
  - Assertions:
    - `add_file()` adds files with correct display prefixes for added/deleted/modified.
    - `render()` produces expected icon prefixes and truncates >20 entries.
    - `DiffPreviewModal.render()` colorizes plus/minus/@ lines and truncates long diffs.

- tests/unit/test_features_palette.py
  - Target: `src/ui/features/palette/logic.py` and `screen.py`
  - Purpose: ensure menu building and filtering behave as expected.
  - Assertions:
    - `build_root_menu()` groups commands by category.
    - `filter_commands(query)` returns expected subset.
    - `get_provider_menu()` returns an appropriate menu even when `available_providers` is empty.

Integration tests (exercise controller and telemetry)

- tests/integration/test_mock_engine_simulation.py
  - Target: `src/ui/mock_engine.py` and `src/ui/app.py`
  - Purpose: run `MockSimulationApp` in headless/test mode and ensure the UI receives a subset of events.
  - Assertions:
    - `SystemSettingsLoaded` is posted and the app updates `SettingsStore.available_providers`.
    - `StreamChunkEvent` sequences result in non-empty `StreamView._raw` content.
    - `TokenUsageEvent`, `ProviderStatusChangeEvent`, and `AgentFinalResponse` lead to the expected UI state updates (sidebar tokens, provider banner, and final artifact mounted).
  - Run in CI with a headless terminal environment (Xvfb if needed) or by running the app in a test harness that does not require terminal.

- tests/integration/test_controller_and_telemetry_bridge.py
  - Target: `src/ui/controller.py`, `src/ui/telemetry_bridge.py`, core telemetry (mocked)
  - Purpose: verify the telemetry mapping logic and that a telemetry event results in exactly one UI `post_message` call.
  - Assertions:
    - Using a mocked `get_session()` (monkeypatch), register a fake telemetry callback and assert the bridge converts `AgentTelemetryEvent` -> the correct UI Message subclass and calls `app.post_message()` exactly once.
    - Ensure that repeated `register_ui_callback()` attempts do not create duplicate UI postings (see duplication mitigation below).

Test utilities and mocks

- Provide a small test fixture in `tests/conftest.py` to create a minimal fake `Textual` app object with a `post_message()` spy to capture posted messages.
- Provide a helper `fake_telemetry_event(event_type, payload, run_id)` to create objects matching the interface expected by `TelemetryBridge` and `WorkflowController._wire_telemetry_bridge()`.

Test coverage targets

- Focus on the streaming widgets and the telemetry->UI conversion code paths.
- Add tests for the `WorkflowController.initialize_handshake()` path that loads providers and posts `SystemSettingsLoaded` (mock I/O for providers.json to avoid repo dependency).


## Telemetry wiring gap and mitigation tasks

Background

During the audit we detected duplicated telemetry wiring logic in two places:

- `WorkflowController._wire_telemetry_bridge()` registers a callback with the telemetry singleton (`get_session().register_ui_callback(...)`) and directly converts AgentTelemetryEvent -> UI Message and posts it to `self.app`.
- `src/ui/telemetry_bridge.py` implements `TelemetryBridge.register()` that also calls `get_session().register_ui_callback(self._on_telemetry_event)` and converts events to UI messages.

If both are active, this risks duplicate UI events, inconsistent conversion mappings, and double-posting to the UI. The mitigation below describes a low-risk way to consolidate wiring and add tests to prevent regressions.

Recommended mitigation tasks (concrete, ordered)

1) Audit registration points (Immediate, Low-risk)
   - File(s) to inspect:
     - `src/ui/controller.py` (method: `_wire_telemetry_bridge()`)
     - `src/ui/telemetry_bridge.py` (method: `register()` and `get_telemetry_bridge()`)
   - Goal: determine whether both mechanisms can register against the same telemetry session during normal startup.
   - Action: add logging at the beginning of both registration methods to emit a debug line describing whether registration occurred.
   - Tests: None required; this is diagnostic. Estimated time: 15–30 minutes.

2) Choose a single authoritative bridge (Design decision, ~15-30 minutes)
   - Option A (recommended): Use `src/ui/telemetry_bridge.py` as the canonical, centralized conversion layer. It already exposes `get_telemetry_bridge()` and `initialize_telemetry_bridge()`.
   - Option B: Keep `WorkflowController` as the only bridge. (Less preferred — duplicates conversion and mixes concerns.)
   - Decision: Prefer Option A for separation of concerns: controller handles graph invocation and lifecycle; `telemetry_bridge` handles converting telemetry to UI events.

3) Implement centralization (Moderate, ~30–90 minutes)
   - Edit `src/ui/controller.py`:
     - Remove or disable the `_wire_telemetry_bridge()` call from `WorkflowController.__init__()` (or make it conditional).
     - Instead, call `from src.ui.telemetry_bridge import get_telemetry_bridge` and register the bridge once during app startup (e.g., in `AgentApp.__init__()` after controller created or in `controller.initialize_handshake()` but only once).
   - Edit `src/ui/telemetry_bridge.py`:
     - Harden `register()` so it is idempotent (already present, but ensure a boolean guard `_registered` is robust and thread-safe if necessary).
     - Add a defensive `session.unregister_ui_callback()` path or a `session.has_callback()` guard if the telemetry API supports it.

   - Example change (conceptual):
     - In `AgentApp.__init__()` (or `on_mount()`), call `get_telemetry_bridge().register(app=self)`.
     - Remove `_wire_telemetry_bridge()` invocation in the controller constructor.

   - Tests to add (see below). Estimated time: 30–90 minutes.

4) Add unit tests to prevent duplicate registration regressions (High-value, ~1–2 hours)
   - Add `tests/unit/test_telemetry_bridge.py` with the following scenarios:
     - Mock `src.core.telemetry.get_session()` to return a fake session object that records how many times `register_ui_callback()` was called. Import `telemetry_bridge.get_telemetry_bridge()` and call `register(app=FakeApp)` twice; assert `register_ui_callback()` was called at most once and no duplicate conversion occurs.
     - Validate `TelemetryBridge._on_telemetry_event()` mapping: create a fake `AgentTelemetryEvent` with `event_type` == `TOKEN_USAGE` and payload containing token counts; call the `_convert_to_ui_event()` or `_on_telemetry_event()` path and assert that the produced UI message is an instance of `TokenUsageEvent` and that `app.post_message()` was called exactly once with that object.
   - Add `tests/integration/test_controller_telemetry_integration.py`:
     - Monkeypatch the telemetry session to call registered callback once with a fake telemetry event, then call `controller.initialize_handshake()` and assert the UI receives exactly one corresponding UI message. Use a spy app with `post_message` recording calls.

5) CI guard and runtime assertion (optional but recommended, low overhead)
   - Add a small runtime check during startup (in `AgentApp.on_mount()` or in `telemetry_bridge.register()`) that logs the number of callbacks registered for telemetry and warns if >1. This reduces the chance of silent duplication in deployed runs.
   - Add a unit test that fails if `telemetry_bridge.register()` is not idempotent.

Who owns what (suggested responsibilities)

- UI owner: implement `telemetry_bridge` centralization and tests.
- Controller owner: remove internal telemetry wiring once `telemetry_bridge` is authoritative and ensure controller still posts any controller-specific messages (e.g., StatusUpdate) to the app directly.

Estimated effort summary

- Audit registration points: 15–30 minutes
- Decide and document authoritative bridge: 15–30 minutes
- Implement centralization (code changes + small refactor): 30–90 minutes
- Add unit & integration tests preventing duplicates: 60–120 minutes
- Total: ~2–4 hours for a single developer to implement and verify changes end-to-end.

Acceptance criteria for telemetry mitigation

- No duplicate UI messages from a single telemetry event in normal startup scenarios.
- `telemetry_bridge.register()` is idempotent and safe to call multiple times.
- Tests exist verifying idempotency and mapping correctness.
- Clear documentation in `tui_overview.md` referencing the authoritative telemetry bridge and the expected single registration pattern.


## What I changed in this document

- Appended a concrete Test Specification section with unit and integration test cases, fixtures, and commands.
- Appended a Telemetry wiring gap mitigation section with a prioritized, actionable plan and tests to prevent duplicate UI events.


If you want, I can next:

- Create the test skeleton files listed above under `tests/unit/` and `tests/integration/` (TDD-first), or
- Implement the telemetry bridge centralization change (apply code edits to `src/ui/controller.py` and `src/ui/telemetry_bridge.py`) and run the new tests locally.

Which should I do next?
