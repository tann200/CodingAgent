# AGENTS.md — Agent Instructions

## Project Overview

CodingAgent is a LangGraph-based autonomous coding agent. It runs on local or cloud LLMs.

## System Architecture

### Code Layout

```
src/
├── core/
│   ├── orchestration/       # LangGraph pipeline, EventBus, services
│   │   ├── event_bus.py     # EventBus with typed dual-emission (Phase 5)
│   │   ├── graph/           # Graph builder + 16 node files
│   │   ├── inference_loop.py
│   │   └── ...              # loop_guards, shell_hooks, permission, etc.
│   ├── messaging/           # Typed MessageBus + event types
│   │   ├── bus.py           # MessageBus (typed event delivery)
│   │   ├── event_types.py   # 90+ typed event dataclasses
│   │   ├── events.py        # Base Event class
│   │   └── __init__.py      # Re-exports (lazy get_typed_bus/reset_typed_bus)
│   ├── inference/           # LLM adapters, provider manager
│   ├── memory/              # Session store, distiller, compaction
│   ├── context/             # ContextBuilder, prompt assembly
│   ├── indexing/            # RepoIndexer, symbol graph, LSP
│   └── mcp/                 # MCP client
├── tools/                   # 60+ @tool-decorated tools
├── config/                  # providers.json, agent-brain (SOUL/roles/skills)
├── server/                  # HTTP/SSE server
└── main.py                  # CLI entrypoint

tui/
└── src/ui/                  # Textual TUI
    ├── core_bridge.py       # Bridge (MessageBus subscriber)
    ├── _bridge_subscriptions.py  # Typed event routing table
    ├── _bridge_protocol.py  # Protocol for mixin typing
    └── ...                  # Screens, components, mixins

tests/
└── unit/                    # 4388 tests (pytest)
    ├── messaging/           # MessageBus + EventBus + adapter tests
    ├── test_event_bus.py
    └── ...
```

### Event System (Critical Context)

The event system has **two cooperating buses**:

1. **EventBus** (`src/core/orchestration/event_bus.py`) — legacy string-addressed bus.
   - `publish("name", dict)` — delivers to old subscribers AND auto-emits typed event on MessageBus
   - `publish_typed(Event(...))` — emits on MessageBus AND delivers `to_dict()` to old subscribers

2. **MessageBus** (`src/core/messaging/bus.py`) — typed event delivery with error isolation.
   - Subscribers register on typed event classes, not string names.
   - `get_typed_bus()` / `reset_typed_bus()` manage the singleton.

**Phase 5 completed:** DualPublishBus adapter eliminated. EventBus natively owns a MessageBus reference.
- 103 publish sites migrated from `publish("name", dict)` to `publish_typed(EventClass(...))`
- TUI bridge subscribes exclusively through MessageBus with `_DictBridgeAdapter`
- `src/core/messaging/__init__.py` uses `__getattr__` lazy re-export to break circular imports

**Phase 6 completed:** MessageBus async migration.
- Internals rewritten: thread-safe `queue.Queue` for synchronous `publish()`, bridge task transfers to `asyncio` event loop, handler dispatch via `run_in_executor` (blocking handlers don't block the event loop)
- Public API unchanged — all 30 bus tests and 316 messaging tests pass
- Architecture: `sync_queue → bridge → async dispatch tasks → executor threads for handlers`
- Legacy `_worker` tasks kept as stubs for backward compatibility; real dispatch now goes through `_bridge` + one-shot `_dispatch` tasks

### Cognitive Pipeline

16 LangGraph nodes in a state-machine (not a simple loop):
```
perception → analysis → planning → plan_validator → execution → step_controller → verification → evaluation → memory_sync
```

With conditional routing for: fast-path, overflow, debug, replan, delegation, wait_for_user.

### Key Conventions

- **Typed events** — always use `publish_typed(EventClass(...))` for new code
- **Event fields** — `kw_only=True` on base fields so subclasses have required positional fields
- **Thread safety** — EventBus uses `threading.RLock()`, session store uses `threading.local()`
- **Correlation IDs** — `ContextVar` propagated across async/thread boundaries via `run_with_correlation()`
- **Lazy imports** — used to break circular dependencies (event_types in event_bus, MessageBus in messaging)
- **Graceful degradation** — all optional features wrapped in try/except
- **try/except pattern for module-level code** — used in `_bridge_subscriptions.py` for mock mode
- **MessageBus async architecture** — `sync_queue` (thread-safe for `publish()`) → `_bridge` task (transfers to event loop) → `_dispatch` task (runs handlers via `run_in_executor` so blocking handlers don't block the event loop)

## Agent Definitions

### Available Agents

| Agent | Description |
|-------|-------------|
| coding | Default agent for coding tasks, file operations, and general development |
| analyst | Deep-dive code analysis, patterns, and architecture review |
| planning | Task breakdown, strategy, and planning |
| review | Code review, verification, and quality checks |
| debugging | Error investigation and fix suggestions |

### Agent Dispatch

Use `delegate_task` to delegate tasks to specialized subagents.

#### Syntax

```python
delegate_task(
    role="analyst",
    subtask_description="Analyze the authentication flow for security issues",
    working_dir="/path/to/project"
)
```

#### When to Dispatch

- **analyst**: Deep code analysis, pattern detection, architecture review
- **planning**: Complex task breakdown, strategy formulation
- **review**: Code review, test verification, quality checks
- **debugging**: Error investigation, bug hunting
- **operational**: File operations, refactoring, migrations

## Tool Call Format

### write_file
When writing file content, output the **actual content** not escaped newlines:

```yaml
name: write_file
arguments:
  path: /path/to/file.md
  content: |
    # Heading
    
    Content here
    More content
```

**IMPORTANT**: Do NOT escape newlines as `\n`. Use literal newlines in the content field.

### edit_file  
When editing, use the exact content to replace:

```yaml
name: edit_file
arguments:
  path: /path/to/file.md
  oldString: |
    Old content
    to replace
  newString: |
    New content
    here
```

## File Content Guidelines

1. **Use literal newlines** - Not `\n` or `\\n`
2. **No trailing newlines** - Don't add extra blank lines at end of files
3. **Clean formatting** - One blank line between sections, not multiple

## Task Completion

When a task is complete:
1. Update AGENTS.md with any new patterns or conventions discovered
2. Output a brief summary of what was done
3. Do NOT read back the file to verify (the system handles this)
4. Move on to next task or indicate completion
