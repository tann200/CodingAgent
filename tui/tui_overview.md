# TUI Overview

## Purpose

A production-grade terminal user interface (TUI) built with the Textual framework, optimized for flicker-free token streaming at 100+ tokens per second. The UI is fully decoupled from any LLM engine through a message bus pattern. A mock engine simulates realistic backend behavior for development and testing.

Inspired by opencode, kilocode, and Claude Code TUI patterns.

---

## Architecture

### Decoupling via Message Bus

The core architectural principle is **complete separation of UI and backend**. The UI layer never imports or calls any LLM engine or `src/core` module directly. Instead, all communication flows through typed `Message` subclasses defined in two modules:

- **`bus.py`** defines backend-to-UI events (data flowing from the engine to the display)
- **`events.py`** defines UI-to-backend events (user actions flowing from the display to the engine)

The `AgentApp` registers `@on()` handlers for each message type. Any backend (real or mock) posts messages to the app, and the app reacts purely to those typed events.

### Settings Decoupling Flow

The UI never imports `src/core` directly. Configuration reaches the UI through bus events:

1. On mount, the UI posts a `RequestSystemSettings` event
2. The backend responds with `SystemSettingsLoaded(settings_dict, available_providers)`
3. The UI stores settings in a local `SettingsStore` cache
4. When the user changes provider credentials, the UI posts `SaveProviderCredentials`
5. When the user changes a role's model, the UI posts `UpdateRoleModel`

### Differential Rendering

Three optimizations eliminate flicker during high-frequency updates:

1. **`StreamView`** uses `reactive("", layout=False, repaint=True)` so only the changed text triggers a repaint, with no layout recalculation.
2. **`AgentArtifact`** uses the same reactive pattern for its content property. The watcher rebuilds the renderable (Markdown, diff, or plain text) only when content actually changes.
3. **`scroll_end(animate=False)`** during streaming prevents animation queue buildup that would cause visual stuttering.

### Settings Persistence

Settings are stored in the TUI config file under the user data directory (see ``src.core.paths.get_config_dir()``). The `SettingsStore` historically used a local per-user TUI cache (legacy: ``~/.agent_tui/settings.json``) as a fallback in dev-mode; callers should prefer the canonical helper above.

- Provides default values for all fields
- Is populated by backend via `SystemSettingsLoaded` event
- Stores `available_providers` list from the backend
- Supports per-agent provider/model configuration
- Never imports from `src/core`

### Structured Logging

All components use a centralized logging system that writes to both:

- **File**: TUI log file under the user data directory (see ``src.core.paths.get_log_dir()``). A legacy fallback path (``~/.agent_tui/logs/agent.log``) exists for dev-mode compatibility, but code should use the helper above.
- **In-memory buffer**: A ring buffer (500 lines) with callback support, feeding the live console panel

---

## Agents / Roles

| Role ID | Display Name |
|---------|-------------|
| `lead_architect` | Lead Architect |
| `full_stack_engineer` | Full Stack Engineer |
| `qa_lead` | QA Lead |
| `system` | System |

Roles are cycled via `tab` key binding. Each role has a distinct color in the UI.

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

  ui/
    __init__.py
    app.py                          Main AgentApp class (event handlers, layout, key bindings)
    bus.py                          Backend-to-UI message types (incl. SystemSettingsLoaded)
    events.py                       UI-to-backend message types (incl. RequestSystemSettings, SaveProviderCredentials, UpdateRoleModel)
    mock_engine.py                  Mock backend simulator (responds to RequestSystemSettings, loads providers.json)
    settings.py                     Local UI settings cache (no src/core imports, populated by bus events)
    logging.py                      Centralized structured logging
    widgets.py                      DiffViewer widget
    coordinator.py                  Data coordinator (decoupled)
    controller.py                   Workflow controller (decoupled)

    components/
      __init__.py                   Component exports
      artifact.py                   AgentArtifact (reactive rendering)
      stream_view.py                StreamView (append-only streaming)
      history_input.py              HistoryInput (command history + double-Esc + smart paste)
      thinking.py                   ThinkingProcess (collapsible reasoning)
      console.py                    ConsolePanel (full-height vertical pillar log viewer)
      cards.py                      ProviderCard (status display)

    features/
      palette/
        __init__.py
        screen.py                   CommandPalette modal screen (uses shared settings store)
        logic.py                    Menu structure, provider/model menus from system data
      settings/
        __init__.py
        screen.py                   SettingsScreen (theme Select, agent config, context window) + ProviderConfigScreen

    screens/
      __init__.py
      probe_results.py              ProbeResultsScreen modal

    styles/
      app.tcss                      All application styles (single file)
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
| `RoleTransitionEvent` | Agent role change (e.g., architect to engineer) |
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
| `RequestSystemSettings` | UI requests system settings from backend |
| `SaveProviderCredentials` | Forward provider API key to backend |
| `UpdateRoleModel` | Update a specific role's model selection |

#### `mock_engine.py` - Mock Backend

`MockSimulationApp` extends `AgentApp` and runs an automated simulation on mount. It:

- Loads `src/core/providers.json` on startup
- Handles `RequestSystemSettings` by responding with `SystemSettingsLoaded` containing providers and default settings
- Handles `SaveProviderCredentials` and `UpdateRoleModel` events
- Exercises every event type in a realistic sequence

Simulation sequence:
1. Provider connection (`ProviderStatusChangeEvent`)
2. Role transition to lead architect (`RoleTransitionEvent`)
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

- **Defaults**: theme, per-agent provider/model selections (lead_architect, full_stack_engineer, qa_lead), console/sidebar visibility, context window, active mode
- **System settings**: Populated via `apply_system_settings(settings, providers)` when `SystemSettingsLoaded` arrives
- **Available providers**: Stored from backend, accessible via `self.available_providers`
- **Per-agent config**: `get_agent_provider(role_id)` / `get_agent_model(role_id)`
- **Provider lookup**: `get_provider_by_id(provider_id)` searches available providers
- **Load/save**: Reads from the TUI config file under the user data directory and merges with defaults. A legacy per-user fallback (``~/.agent_tui/settings.json``) is retained for dev-mode compatibility; prefer ``src.core.paths.get_config_dir()`` at runtime.
- **No src/core imports**: All configuration comes through bus events

#### `logging.py` - Centralized Logging

Sets up a single `agent_tui` logger with two handlers:

- **FileHandler**: Writes all log levels to the TUI log directory (see ``src.core.paths.get_log_dir()``). Legacy behaviour wrote to ``~/.agent_tui/logs/agent.log``; that path is only used as a dev-mode fallback.
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

Two modal screens:

**SettingsScreen**: Scrollable settings form using Textual `Select` widgets:
- Receives shared `settings_store` parameter from app
- **General**: Theme selector (20 built-in Textual themes with live preview)
- **Per-agent config**: Provider and model `Select` dropdowns for Lead Architect, Full Stack Engineer, and QA Lead
- Provider options built from `available_providers` list (uses normalized provider IDs)
- Model options show all models across all providers
- **Context**: Context window size selector (8K to 200K options)
- Scrollable via `VerticalScroll` container
- Save persists to JSON and posts `UpdateSettings` + per-agent `UpdateRoleModel` events

**ProviderConfigScreen**: API key configuration form:
- Password-masked input for API key
- Validation (non-empty check)
- Posts `SaveProviderCredentials` event to forward credentials to backend

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
Backend (mock_engine or real LLM)
    |
    | post_message(BusEvent)
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
```

---

## Running

```bash
python3 -m src.ui.mock_engine
```

This launches `MockSimulationApp`, which extends `AgentApp` with an automated simulation that demonstrates all UI features in sequence.
