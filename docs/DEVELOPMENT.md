# Development Guide

## Read-Before-Write Enforcement

All write tools enforce that any existing file must have been read in the current session before it can be modified. New files (not yet on disk) are always allowed.

**Implementation** (`src/tools/guardrails.py`): Dual-tracking for correctness across all threading models:
- `ContextVar` — propagates through async chains (same-context) and Python 3.12+ executor threads
- Global `threading.Lock`-protected set — visible from any executor thread including Python 3.11 `run_in_executor`

Both are reset by `Orchestrator.start_new_task()` between tasks.

**Enforcement points:**
- `read_file`, `read_file_chunk` → call `mark_file_read(path)` on success
- `ast_rename` → calls `mark_file_read()` after reading, `check_read_before_write()` before writing
- All other write tools (`write_file`, `edit_file`, `edit_file_atomic`, `edit_by_line_range`, `apply_patch`, `manage_todo`) → call `check_read_before_write()` before modifying

`check_read_before_write()` returns `{}` if OK or `{"error": "...", "requires_read_first": True}` on violation. The AgentState also maintains a `files_read` O(1) dict and `verified_reads` list as supplementary tracking used by execution_node.

## Subagent Delegation

The `delegate_task` tool spawns isolated subagents for specialized tasks (planning, code review, etc.). Subagents run in isolated graph contexts with `{"configurable": {"orchestrator": None}}` to enable subagent mode.

PRSW roles: `scout`, `researcher`, `reviewer` (read-only, parallel) and `coder`, `tester` (write, sequential).

## Session Management

Each session has a unique ID (8-character UUID) that tracks all operations:

- **Task ID**: Generated on `start_new_task()`, tracked in `orch._current_task_id`
- **Message history**: Stored in `SessionStore` (SQLite) with session_id
- **Tool calls**: Logged with session_id for audit trail
- **TUI conversation history**: Persisted to `~/.coding_agent/tui_conversation_history.json` (atomic write)

### On Quit/Reset

When quitting or starting a new session:
1. Plans → SessionStore (SQLite); task state → SessionStore; session summary → VectorDB
2. TODO.md, TASK_STATE.md, last_plan.json, execution_trace.json, usage.json are cleared

## Provider Configuration

`src/config/providers.json` is the authoritative source. Must be an array `[{...}]`.

```json
[
  {
    "name": "lm_studio_local",
    "type": "lm_studio",
    "base_url": "http://localhost:1234/v1",
    "models": ["qwen/qwen3.5-9b"]
  },
  {
    "name": "openrouter",
    "type": "openrouter",
    "models": ["anthropic/claude-3.5-sonnet"]
  }
]
```

Provider types: `lm_studio`, `ollama`, `openrouter`. API keys for OpenRouter are stored in `~/.config/codingagent/prefs.json` via the TUI settings panel.

## Tool Registry and Auto-Discovery

Tools are registered via the `@tool` decorator in `src/tools/_tool.py`. `build_registry()` in `src/tools/_registry.py` auto-discovers all 60 built-in tools across 16 modules:

```python
from src.tools import build_registry

# Standard setup
registry = build_registry(working_dir="/path/to/project")

# Add project-specific tools
registry = build_registry(extra_modules=[my_tool_module])

# Filter by role
coding_tools = registry.filter_by_tags("coding")
```

**Adding a new tool:**
```python
from src.tools._tool import tool

@tool(side_effects=["write"], tags=["coding"])
def my_tool(path: str, content: str) -> Dict[str, Any]:
    """Description shown in system prompt."""
    return {"status": "ok"}
```

Then add the module path to `_BUILTIN_MODULES` in `src/tools/_registry.py`.

**Schema generation:** Parameter types are inferred from annotations (`str→string`, `int→integer`, `bool→boolean`, `list→array`, `dict→object`). `*args`, `**kwargs`, `self`, `cls`, and `workdir` are excluded from the JSON schema.

## Running Tests

```bash
# All unit tests (no live LLM required) — recommended
.venv/bin/pytest tests/unit -q -p no:logging

# Specific test file
.venv/bin/pytest tests/unit/test_audit_vol9.py -q -p no:logging

# Integration tests (requires local provider running)
RUN_INTEGRATION=1 .venv/bin/pytest tests/integration -q
```

The `-p no:logging` flag suppresses TUI log noise. Tests run on Python 3.11 (`pyproject.toml` pins `>=3.11,<3.12`).

## Regenerating the System Map

```bash
.venv/bin/python scripts/generate_system_map.py
```

Outputs `docs/system_map.md` (ASCII tree) and `scripts/tree.json`.

## Running Scenarios Headless

```bash
.venv/bin/python scripts/test_agent_stability.py --scenario fix_syntax --working-dir /tmp/agent_run
```

## TUI Diff Formatting

File changes are displayed with Rich markup:
- New files: `[bold cyan]🆕 New file:[/bold cyan]`
- Edits: `[bold cyan]✏️  Edit:[/bold cyan]`
- Added lines (`+`): `[green]`; removed (`-`): `[red]`; context: `[dim]`; hunk headers: `[cyan]`

The `file.diff.preview` EventBus event fires *before* the write so the TUI shows the proposed diff first.

## Session Search

Sessions can be retrieved via:
- **VectorDB**: Semantic search for session summaries (LanceDB)
- **SessionStore**: Direct queries by session_id (SQLite at `.agent-context/session.db`)
