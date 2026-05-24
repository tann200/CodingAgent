# Changelog

All notable changes to this project are recorded in this file.

## Unreleased — Sprint 4 Improvements (2026-05-24)

### P1-6 Phase A: TUI component extraction

- Created `tui/src/ui/components/status_bar.py` — `StatusBarMixin` with all status-bar
  rendering helpers extracted from `app.py`:
  - `_update_perm_badge()` — pending permission count chip
  - `_update_role_display()` — sidebar role label + `App.sub_title`
  - `_update_status_bar()` — footer left chip (role, tokens, streaming/running flags)
  - `_update_mcp_status_chip()` — MCP server status chip
  - `_update_provider_status_widgets()` — provider sidebar + banner text
  - `_update_status_text()` — sidebar status widget
  - Exports canonical `ROLE_LABELS` and `ROLE_COLORS` dicts
- `AgentApp` now inherits `StatusBarMixin`; inline method bodies removed from `app.py`
- `handle_mcp_status`, `handle_status`, `handle_provider_status` simplified to delegate
  to mixin helpers
- 39 unit tests in `tests/unit/test_status_bar_mixin.py`

### P2-2 / P3-2: Session fork, revert, and Ctrl+R resume

- Created `tui/src/ui/screens/session_screen.py` — `SessionScreen` extends
  `SessionListScreen` with:
  - `f` key → **fork** selected session into a new independent snapshot
  - `r` key → **revert** working directory to session's stored `git_sha` via `git stash`
    + `git checkout`
  - Backward-compatible constructor (same signature as `SessionListScreen`)
- `app.py`: added `Ctrl+R` binding → `action_open_sessions()` → `push_screen(SessionScreen())`
- `/sessions` slash command now opens `SessionScreen` instead of bare `SessionListScreen`
- Subagent footer chip also upgraded to `SessionScreen(filter_subagents=True)`

### P1-6 Phase B: ChatDisplayMixin extraction

- Created `tui/src/ui/components/chat_mixin.py` — `ChatDisplayMixin` with 28 methods
  extracted from `app.py`:
  - Stream widget lifecycle: `_ensure_stream_widget`, `_mount_and_scroll`,
    `_finalize_stream`, `_prune_chat_log`, `_mount_chat_widget`, `_sched_chat_widget`,
    `_clear_chat_panel`
  - @file picker: `_list_workspace_files`, `_at_picker_navigate`, `_at_picker_complete`,
    `_at_picker_hide`
  - Inline palette: `_palette_navigate`, `_palette_complete`
  - Token expansion: `_expand_at_tokens`
  - Chat-display handler implementations: `_chat_handle_stream_chunk`,
    `_chat_handle_thinking_update`, `_chat_handle_reasoning`,
    `_chat_handle_final_response`, `_chat_handle_error`,
    `_chat_handle_usage_turn_summary`, `_chat_handle_doom_loop`,
    `_chat_handle_plan_requested`, `_chat_handle_session_health`,
    `_chat_handle_context_compacted`, `_chat_handle_context_degraded`,
    `_chat_handle_retry_attempt`, `_chat_handle_retry_succeeded`,
    `_chat_handle_retry_failed`, `_chat_handle_text_changed`
- `AgentApp` now inherits `ChatDisplayMixin`; 28 method bodies removed from `app.py`
- `@on`-decorated stubs remain in `AgentApp` (Textual requires them on the class)
  delegating to `self._chat_handle_*()` methods from the mixin
- `app.py` reduced from 3589 → 3256 lines (−333 lines)
- 40 unit tests in `tests/unit/test_chat_display_mixin.py`

## Unreleased — Sprint 3 Improvements (2026-05-24)

### P1-5: Structured slash command registry
- Extended `src/core/orchestration/commands.py`:
  - Added `SlashCommandMeta` — lightweight metadata record (name, description, aliases)
  - Added `_CallableCommand` — wraps plain callables / async coroutines as `Command`
    instances, enabling TUI `_slash_*` methods to be registered without a full subclass
  - Added `CommandRegistry.register_handler(name, description, handler, aliases)` —
    single-call registration for callable handlers
  - Added `CommandRegistry.list_metadata()` — returns sorted `List[SlashCommandMeta]`
    usable to auto-generate `SLASH_COMMANDS` and `SLASH_COMMAND_DESCRIPTIONS` in the TUI
  - Fixed `dispatch()` regex to handle punctuation aliases (e.g. `/?` → `/help`)
- Added `tests/unit/test_slash_command_registry.py` (24 tests)

### P1-7: AgentState lifecycle documentation
- Rewrote `src/core/orchestration/graph/state.py` module docstring with:
  - Full node execution flow for Standard, Frontier, and Lite graphs
  - Per-node reads/writes table (ASCII diagram)
  - Field lifecycle cross-reference for fields written by multiple nodes
  - Reducer semantics section explaining `merge_or_replace_list` / `ReplaceList`
  - "See Also" references to builder.py, nodes/, validate_state()

### P2-1: Standalone compaction service
- Created `src/core/memory/compaction_service.py`:
  - `CompactionResult` dataclass — structured result (success, method, tokens_before/after, error)
  - `CompactionService` — unified facade for all compaction paths:
    - `should_compact(token_limit)` — fast deterministic threshold check via `auto_compactor`
    - `compact()` — tries LLM summariser first, falls back to sliding-window; never raises
    - Publishes `context.compacted` / `context.compact.failed` events when event_bus provided
    - `prefer_deterministic=True` flag for tests and LLM-free environments
- Added `tests/unit/test_compaction_service.py` (23 tests)

### P3-3: Live model switching via model.routing event
- Extended `ProviderManager.set_event_bus()` in `src/core/inference/llm_manager.py`:
  - Now subscribes to `model.routing` at call time
  - Added `_on_model_routing(payload)` handler:
    - Updates `adapter.default_model` on the active adapter immediately
    - Optionally flips the active provider flag when `"provider"` key is present
    - Never raises — all errors are suppressed with a debug log
- TUI `/model` command was already publishing `model.routing`; the backend
  now reacts to it within the same process without requiring a restart
- Added `tests/unit/test_model_routing.py` (14 tests)

## Unreleased — Sprint 1 & 2 Improvements (2026-05-24)

### Dependencies (P0-1, P0-2)
- Pinned `langgraph>=1.1.0,<2.0.0` (was open `>=0.2.0`; 1.1.2 is installed)
- Pinned `langchain-core>=1.0.0,<2.0.0` (was `>=0.3.0`)
- Moved `openai` to `>=1.50.0,<3.0.0` (was pinned to pre-release `==2.0.0`)
- Upgraded `requests>=2.32.0`, `httpx>=0.27.0`, `PyYAML>=6.0.2` (security patches)
- Added upper bounds to `textual`, `pydantic`, `python-dotenv`
- Removed `lancedb` optional dependency (P2-3): semantic search now uses SQLite FTS5

### Repository hygiene (P0-3, P0-4)
- Removed tracked artifacts from git index: `brand_new.txt`, `dummy_path`,
  `coverage.xml`, `pyright-output.json`, `sub/hello.txt`, `tmp_debug_main.log`,
  `results/` (73 benchmark JSON files)
- Updated `.gitignore` to exclude `MagicMock/`, `tmp_*/`, `results/`,
  `brand_new.txt`, `dummy_path`, `sub/`, `coverage.xml`, `pyright-output.json`, `*.log`
- Fixed `src/main.py:_dbg()` to write debug logs to `tempfile.gettempdir()`
  instead of the repo root (`tmp_debug_main.log` → `/tmp/codingagent_debug_main.log`)

### Code quality (P0-5)
- Auto-fixed 22 ruff violations in `src/`: 15 E402, 6 F401, 1 F841
- Fixed remaining 16 E402 violations in `src/core/context/context_builder.py`
  (moved `logger = logging.getLogger(__name__)` after all imports)
- Added `# noqa: E402` to intentional post-function imports in `src/tools/_edit_tools.py`
- Removed unused `res` variable in `src/tools/sandbox.py:117`

### Entrypoint & launch (P1-2)
- Added `[project.scripts] codingagent = "src.main:main"` to `pyproject.toml`
  → `uv run codingagent [args]` now works
- Added `scripts/run.sh` — canonical single launch script for all platforms
- Added `DEPRECATED` notices to `start.sh`, `starth.sh`

### Tool argument validation (P1-4)
- Added `ToolDefinition.validate_args(args: dict) -> list[str]` to `src/tools/_tool.py`
  — validates required fields and basic types against the tool's JSON Schema
- Wired into `src/core/orchestration/tool_preflight.py:preflight_check_impl()`:
  malformed LLM tool calls now return `{"ok": False, "error": "argument_validation_failed"}`
  with a structured error message the LLM can self-correct from
- `ToolRegistry.register_definition()` now stores `__tool_meta__` on the entry dict
  so preflight can access `validate_args` without re-importing `_tool.py`

### Tool discovery (P2-7)
- Added `_OPTIONAL_MODULES` frozenset to `src/tools/_registry.py`
- `discover_module_name()` logs at DEBUG (not WARNING) for optional modules
  — prevents spurious warnings when `pygls` / LSP tools are not installed

### Tool consolidation (P2-5)
- Created `src/tools/repo_read_tools.py` — consolidates `repo_overview`, `find_files`,
  `search_code`, `find_symbol`, `find_references`, `analyze_repository`, `summarize_repo`
- Created `src/tools/repo_write_tools.py` — consolidates `initialize_repo_intelligence`
- Updated `_BUILTIN_MODULES` in `src/tools/_registry.py` to use consolidated files
- Original files (`repo_tools.py`, `repo_analysis_tools.py`, `repo_overview_tool.py`,
  `repo_summary.py`) retained for backward compatibility

### Tests
- Added `tests/unit/test_sprint1_improvements.py` (28 tests):
  - `TestToolDefinitionValidateArgs` — 10 tests for `validate_args()`
  - `TestPreflightValidateArgsWiring` — 4 tests for preflight integration
  - `TestRepoToolsConsolidation` — 10 tests for consolidated repo tool modules
  - `TestOptionalModuleGracefulDiscovery` — 2 tests for log-level behaviour
  - `TestDebugLogPath` — 2 tests for debug log path fix

- Orchestration routing & router purity fixes (`src/core/orchestration/graph/builder.py`):
  - Rewrote `route_after_perception` to make routing deterministic and
    precedence-aware:
    - Top-level short-circuits for clarification and context overflow.
    - Robust extraction of `next_action` from multiple shapes (string/dict).
    - First-round behavior respects `task_complexity` and `model_tier` (NANO/SMALL
      fast-paths, LARGE/FRONTIER planning shortcuts).
    - Subsequent-round precedence: read-only tools favour analysis while write
      or unknown tools favour execution.
  - Added canonical constants for tool-type checks and ensured routers use
    them rather than duplicated literal sets.
  - Implemented backward-compatible, pure wrapper routers (typed and
    docstring-safe) required by tests.
  - Ensured wrappers do not call token budget compaction helpers or mutate
    the provided state (purity enforced to satisfy tests).

- CI / repo housekeeping:
  - Added tests to verify canonical constants usage and router purity.
  - Cherry-picked/merged routing fixes into `main` and removed temporary
    local branches created during the merge process.

### Notes

- The vectorstore branch was intentionally left unmerged; no external vector DB
  code was introduced in the routing fixes. Local temporary branches and the
  local vectorstore branch were removed per request.
