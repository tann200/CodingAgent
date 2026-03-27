# TUI System Specification

**Version:** 2.0
**Date:** 2026-03-26
**Purpose:** Complete specification for a drop-in replacement TUI. A compliant implementation that honours every contract in this document can replace the current Textual UI without touching any core code.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Entry Point and Wiring](#2-entry-point-and-wiring)
3. [Required TUI Interface](#3-required-tui-interface)
4. [EventBus Contract](#4-eventbus-contract)
5. [Tool Registry — Complete Reference](#5-tool-registry--complete-reference)
6. [Tool Result Display Requirements](#6-tool-result-display-requirements)
7. [Orchestrator Public API](#7-orchestrator-public-api)
8. [ProviderManager Public API](#8-providermanager-public-api)
9. [Concurrency and Threading Contract](#9-concurrency-and-threading-contract)
10. [Lifecycle Contract](#10-lifecycle-contract)
11. [Slash Command Contract](#11-slash-command-contract)
12. [UI Panel Requirements](#12-ui-panel-requirements)
13. [Settings and Configuration](#13-settings-and-configuration)
14. [Plan Mode and Preview Mode](#14-plan-mode-and-preview-mode)
15. [History Persistence](#15-history-persistence)
16. [Security Rules the TUI Must Enforce](#16-security-rules-the-tui-must-enforce)
17. [Non-Goals / Out of Scope](#17-non-goals--out-of-scope)
18. [Compliance Checklist](#18-compliance-checklist)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                         TUI  (replaceable)                       │
│                                                                  │
│  ┌──────────────┐   ┌──────────────────────────────────────────┐ │
│  │    Widgets   │   │          TUI Controller / App Base       │ │
│  │    / Views   │   │  - owns _agent_lock, _cancel_event       │ │
│  └──────────────┘   │  - exposes send_prompt(), is_running()   │ │
│                     │  - spawns _run_agent() on bg thread       │ │
│                     └────────────────┬─────────────────────────┘ │
└──────────────────────────────────────┼──────────────────────────┘
                                       │  public API only
                       ┌───────────────▼──────────────────────────┐
                       │         CodingAgentApp  (app.py)          │
                       │  creates EventBus singleton               │
                       │  creates Orchestrator                     │
                       │  wires ProviderManager                    │
                       │  starts SessionWatcher, Telemetry         │
                       └──────────┬───────────────┬───────────────┘
                                  │               │
                     ┌────────────▼───┐   ┌───────▼─────────────┐
                     │  Orchestrator  │   │  EventBus (shared)   │
                     │  (LangGraph    │   │  module singleton    │
                     │   pipeline)    │   │  get_event_bus()     │
                     └────────────────┘   └─────────────────────┘
```

**Cardinal rule:** The TUI communicates with core exclusively through:
- The **Orchestrator public API** (§7)
- The **EventBus** via `get_event_bus()` (§4)
- The **ProviderManager public API** (§8)

The TUI must **never** read or write private attributes (prefixed `_`) on any core object except the one-time snapshot needed for `/continue` (§10.4).

---

## 2. Entry Point and Wiring

### 2.1 What `CodingAgentApp` does for you

Instantiating `CodingAgentApp` is sufficient to bring up all core services:

1. Installs the centralised stdlib logging handler.
2. Creates one `EventBus` and assigns it to `_event_bus_module._default_bus` — `get_event_bus()` returns this instance everywhere.
3. Calls `pm.set_event_bus()` on the `ProviderManager` singleton.
4. Constructs `Orchestrator(working_dir=config.working_dir)`.
5. Starts `SessionWatcher` and `SessionRegistry` health monitoring.
6. Optionally starts `TelemetryConsumer`.
7. Runs a provider health check and publishes `ui.notification` if no providers are reachable.

```python
from src.ui.app import CodingAgentApp, AppConfig
from src.core.orchestration.event_bus import get_event_bus

app  = CodingAgentApp(AppConfig(working_dir="/path/to/project"))
bus  = get_event_bus()     # the one true bus
orch = app.orchestrator    # fully wired Orchestrator
```

### 2.2 `AppConfig` fields

```python
@dataclass
class AppConfig:
    working_dir:       Optional[str] = None
    debug:             bool          = False
    telemetry_enabled: bool          = False
    telemetry_path:    Optional[str] = None
```

### 2.3 TUI launch

Override or call `CodingAgentApp.run()`. The env var `ENABLE_TUI=1` gates UI launch; when absent the process runs headless. A TUI implementation should set this variable or bypass `run()` entirely.

---

## 3. Required TUI Interface

### 3.1 Mandatory capabilities

| Capability | Requirement |
|---|---|
| Send prompt | Accept text, pass to agent; thread-safe; reject (not queue) if already running |
| Display agent output | Render text returned by `run_agent_once()` after completion |
| Streaming tokens | Subscribe to `model.token`, render partial tokens as they arrive |
| Cancel task | Provide keyboard shortcut / button that sets `_cancel_event` |
| EventBus subscriptions | Subscribe to all events in §4; unsubscribe on shutdown |
| Tool result display | Render each tool call and result per §6 |
| Diff preview | Render diffs from `file.diff.preview` before writes complete |
| History persistence | Load from disk on startup; atomic save after every agent result |
| Settings surface | Select provider/model; persist to `providers.json` atomically |
| Slash commands | All commands in §11 |
| Plan approval | Show pending plan; surface approve/reject controls when plan mode is active |

### 3.2 Forbidden behaviours

| Behaviour | Reason |
|---|---|
| Access `orchestrator._*` private fields | Breaks encapsulation; request a public method instead |
| Subscribe `log.new` to a logging call | Creates an infinite publish loop |
| Block EventBus callback thread with I/O | Callbacks run synchronously on the publisher thread |
| Spawn a second agent thread while one is running | Check lock first; reject duplicates |
| Construct `EventBus()` directly | Use `get_event_bus()` |
| Write `msg_mgr.messages` directly | Use `orchestrator.restore_continue_state()` |
| Call `pm.set_event_bus()` or `pm.initialize()` | `CodingAgentApp` does this |

---

## 4. EventBus Contract

### 4.1 Obtaining the bus

```python
from src.core.orchestration.event_bus import get_event_bus
bus = get_event_bus()
```

### 4.2 Subscription tracking — required pattern

```python
self._subscriptions: list = []

def _subscribe(self, event: str, cb) -> None:
    bus.subscribe(event, cb)
    self._subscriptions.append((event, cb))

def cleanup(self) -> None:
    for event, cb in self._subscriptions:
        bus.unsubscribe(event, cb)
    self._subscriptions.clear()
```

### 4.3 Delivery semantics

- Callbacks run **synchronously on the publisher's thread** (often the agent background thread).
- If you need to update UI widgets from a callback, schedule via `_schedule_callback` (§9.6).
- Offload any I/O or slow computation to a daemon thread; never block the callback.
- Exceptions in callbacks are caught and logged at DEBUG; they do not propagate.

### 4.4 Correlation IDs

Every dict payload automatically receives `_correlation_id: str` (UUID4) if a turn is in progress. Handlers may use it for log correlation but must not fail if absent.

### 4.5 Complete event registry

#### Provider / Model events

| Event | Always-present fields | Optional fields | Meaning |
|---|---|---|---|
| `orchestrator.startup` | `time: float`, `working_dir: str` | — | Core services ready |
| `provider.status.changed` | `provider: str`, `status: str` | — | Adapter connected / disconnected |
| `provider.models.list` | `provider: str`, `models: [str]` | — | Models discovered |
| `provider.models.cached` | `provider: str`, `models: [str]` | — | Models served from cache |
| `provider.models.empty` | `provider: str` | — | Provider has no models |
| `provider.model.missing` | `provider: str`, `requested: str` | — | Requested model unavailable |
| `model.routing` | `provider: str`, `selected: str` | — | Active provider/model changed |
| `model.response` | — | `provider`, `model`, `tokens: int` | LLM call complete; token stats |
| `model.token` | `text: str`, `partial: bool` | — | Streaming token; `partial=True` → append; `partial=False` → finalise |

#### Tool execution events

All tool events use ACP schema. Legacy field names are kept for backwards compatibility. Always check both:

```python
tool_name  = payload.get("title") or payload.get("tool", "unknown")
tool_args  = payload.get("rawInput") or payload.get("args", {})
tool_id    = payload.get("toolCallId", "")
```

| Event | Always-present fields | Optional fields | Meaning |
|---|---|---|---|
| `tool.execute.start` | `sessionUpdate`, `toolCallId`, `title`, `status: "in_progress"`, `rawInput` | `tool`, `args`, `workdir` | Tool call began |
| `tool.invoked` | `sessionUpdate`, `toolCallId`, `title`, `status: "invoked"`, `timestamp` | `workdir` | Low-level invocation trace (after execution) |
| `tool.execute.finish` | `sessionUpdate`, `toolCallId`, `title`, `status: "completed"`, `content: [{"text": str}]` | `tool`, `args`, `result`, `result_formatted`, `ok`, `rawOutput`, `workdir` | Tool call complete — `content[0]["text"]` is the formatted result |
| `tool.execute.error` | `sessionUpdate`, `toolCallId`, `title`, `status: "error"` | `tool`, `args`, `error: str` | Tool call failed |

#### File events

| Event | Fields | Timing | Meaning |
|---|---|---|---|
| `file.diff.preview` | `path: str`, `diff: str`, `is_new_file: bool` | **Before** the write completes | Show diff to user |
| `file.modified` | `path: str`, `tool: str`, `workdir: str` | After write | File was written/edited |
| `file.deleted` | `path: str`, `workdir: str` | After delete | File was removed |

#### Planning events

| Event | Fields | Meaning |
|---|---|---|
| `plan.progress` | `currentStep: int`, `totalSteps: int`, `stepDescription: str` (ACP) **or** `step: int`, `total: int`, `description: str` (legacy) | Step progress; always accept both schemas |
| `plan.requested` | — | Agent waiting for plan approval (plan mode) |

#### Session events

| Event | Fields | Who publishes | Meaning |
|---|---|---|---|
| `session.new` | `timestamp: float` | Settings panel / `/new` command | New session started; clear state |
| `session.hydrated` | full state dict | `agent_session_manager` | Restore session state |
| `session.request_state` | `session_id: str` | **TUI publishes this** on mount | Request hydration |
| `session.registered` | `session_id: str` | Session registry | Session added |
| `session.unregistered` | `session_id: str` | Session registry | Session removed |
| `session.health_alert` | `level: str`, `title: str`, `message: str` | Session watcher | Health warning |

#### Notifications and logging

| Event | Fields | Meaning |
|---|---|---|
| `ui.notification` | `level: str` (info/warning/error), `message: str`, `source: str` | Show to user as toast or inline message |
| `log.new` | `level: str`, `message: str`, `logger: str` | Structured log line. **Must NOT be fed back into any logging call.** |

#### Token budget

| Event | Fields | Meaning |
|---|---|---|
| `token.budget.update` | `used: int`, `limit: int`, `percent: float` | Update budget display |
| `token.budget.warning` | `used: int`, `limit: int`, `percent: float` | Budget threshold warning; highlight panel |

#### Preview mode

| Event | Fields | Meaning |
|---|---|---|
| `preview.pending` | `preview_id: str`, `tool: str`, `diff: str` | Write waiting for user confirmation |
| `preview.confirmed` | `preview_id: str` | User confirmed |
| `preview.rejected` | `preview_id: str` | User rejected |

---

## 5. Tool Registry — Complete Reference

There are 44 registered tools. They are divided into functional groups below.

### 5.1 File operation tools

#### `read_file`
```
Parameters:
  path       str   required  File path (relative to workdir)
  summarize  bool  optional  If true and file >500 chars, show first 10 lines + "... [N more]"

Returns:
  {
    "path":      str,
    "status":    "ok" | "not_found",
    "content":   str,
    "truncated": bool
  }

Events:   none
Side fx:  none — records path in session_read_files (enables later writes)
```

#### `write_file`
```
Parameters:
  path    str   required  Destination path
  content str   required  Full file content

Returns:
  {
    "path":           str,
    "status":         "ok" | "error",
    "lines_added":    int,
    "lines_removed":  int,
    "is_new_file":    bool,
    "diff":           str,            ← unified diff
    "requires_split": bool            ← true if >200 lines changed (agent must split)
  }

Events:
  file.diff.preview  →  {path, diff, is_new_file}    BEFORE write
  file.modified      →  {path, tool: "write_file"}   AFTER write

Side fx:  ["write"]
Security: pre-existing files require prior read_file call this session
```

#### `edit_file`
```
Parameters:
  path    str   required  Target file path
  patch   str   required  Unified diff patch

Returns:
  { "path", "status": "ok"|"error", "lines_added", "lines_removed", "diff", "requires_split" }

Events:   file.diff.preview (before), file.modified (after)
Side fx:  ["write"]
Security: requires prior read_file
```

#### `edit_file_atomic`
```
Parameters:
  path       str   required  Target file
  old_string str   required  Exact string to replace (must appear exactly once)
  new_string str   required  Replacement string

Returns:
  { "path", "status": "ok"|"error", "lines_added", "lines_removed", "diff" }

Events:   file.diff.preview (before), file.modified (after)
Side fx:  ["write"]
Security: requires prior read_file; fails if old_string appears 0 or >1 times
Notes:    Preferred for surgical edits — no line-number drift issues
```

#### `edit_by_line_range`
```
Parameters:
  path        str   required  Target file
  start_line  int   required  First line to replace (1-indexed)
  end_line    int   required  Last line to replace (1-indexed, inclusive)
  new_content str   required  Replacement text

Returns:
  { "path", "status": "ok"|"error", "lines_added", "lines_removed" }

Events:   file.diff.preview (before), file.modified (after)
Side fx:  ["write"]
Security: requires prior read_file
```

#### `delete_file`
```
Parameters:
  path  str  required  File or directory to delete

Returns:
  { "path", "status": "ok"|"deleted", "warning": str }
  warning: non-empty if file is git-tracked

Events:   file.deleted → {path, workdir}
Side fx:  ["write"]
```

#### `rename_file`
```
Parameters:
  src_path  str  required  Source path
  dst_path  str  required  Destination path

Returns:
  { "ok": bool, "status": "ok"|"error"|"not_found", "renamed": str }

Side fx:  ["write"]
```

#### `list_files`  (alias: `fs.list`)
```
Parameters:
  path  str  optional  Directory to list (default ".")

Returns:
  {
    "path":   str,
    "status": "ok",
    "items":  [ {"name": str, "is_dir": bool}, ... ]
  }
  Sorted: directories first, then case-insensitive by name.
  Excludes: .DS_Store, Thumbs.db, __pycache__, *.pyc

Events:   none
Side fx:  none
```

#### `read_file_chunk`
```
Parameters:
  path    str  required  File path
  offset  int  optional  Byte offset (default 0)
  limit   int  optional  Bytes to read (-1 = all)

Returns:
  { "path", "status": "ok"|"not_found", "content": str, "offset": int, "limit": int }
```

#### `glob`
```
Parameters:
  pattern  str  required  Glob pattern ("**" for recursive; ".." rejected)

Returns:
  { "status": "ok"|"error", "pattern": str, "matches": [str] }   ← max 500 entries

Security: patterns containing ".." are rejected outright
```

#### `bash`
```
Parameters:
  command  str  required  Shell command string (no pipes, redirects, or shell operators)

Returns:
  {
    "status":           "ok" | "error",
    "command":          str,
    "stdout":           str,   ← max 50 KB
    "stderr":           str,   ← max 5 KB
    "returncode":       int,
    "error":            str,
    "requires_approval": bool  ← true for Tier-3 commands (pip, curl, npm install, sudo…)
  }

Side fx:  ["execute"]

TIER 1 — auto-allowed (read-only, git, system info):
  ls, cat, grep, find, git (read ops), head, tail, wc, pwd, echo, date, which,
  env, tree, sort, uniq, awk, sed (no -i), diff, stat, file, du, df, id, whoami,
  hostname, ps, pgrep, lsof, uname, uptime, free, top, htop, nm, objdump,
  readelf, ldd, strings, realpath, basename, dirname, readlink, md5sum, sha256sum,
  xxd, less, more, column, cut, tr, xargs, test, true, false, tar (list only),
  zip, unzip (list only), type, sw_vers, defaults, pbpaste, otool, codesign

TIER 2 — auto-allowed (test / compile):
  python, python3, pytest, py.test, tox, nox, ruff, mypy, pyright, uv, poetry,
  pdm, hatch, npm (test/run/build), npx, node, yarn, pnpm, tsc, jest, vitest,
  mocha, jasmine, eslint, prettier, biome, cargo (test/check/build), rustc,
  javac, java, jar, mvn, gradle, go (build/test/vet), gofmt, golint, gcc, g++,
  clang, clang++, make, cmake, ninja, bundle, rake, rspec, ruby, composer,
  php, swift, swiftc, dotnet

TIER 3 — requires_approval=true (TUI must ask user):
  pip, pip3, curl, wget, npm install, npm i, cargo install,
  go install, go get, apt, apt-get, yum, dnf, brew,
  sudo, su, chmod, chown, rm, del

BLOCKED completely (DANGEROUS_PATTERNS):
  &&, ||, ;, |, >, >>, <, $(, ` (backtick), rm -rf, rm -r, rm -f,
  del (Windows), format, shutdown, reboot, halt, poweroff, git push,
  sed -i, tar -x (extract), unzip without -l, python -c/-e, node -e/-r, ruby -e, php -r
```

### 5.2 Search and analysis tools

#### `search_code`
```
Parameters:
  query  str  required  Natural-language or keyword query

Returns:
  { "status": "ok"|"error", "results": [ {file_path, symbol_name, snippet, score}, ... ] }
  Up to 10 results from VectorStore semantic search.
```

#### `find_symbol`
```
Parameters:
  name  str  required  Exact class or function name

Returns:
  {
    "status": "ok"|"error",
    "results": [
      { "symbol_name": str, "file_path": str, "symbol_type": str,
        "start_line": int, "end_line": int }
    ]
  }
```

#### `find_references`
```
Parameters:
  name  str  required  Symbol name to search for

Returns:
  { "status": "ok"|"error", "results": [ {file: str, snippet: str} ] }
  snippet: 100-char prefix + 200-char suffix around the match.
```

#### `initialize_repo_intelligence`
```
Parameters:
  workdir  str  required  Repository root

Returns:
  { "status": "ok"|"error", "indexed_files": int, "indexed_symbols": int }
```

#### `grep`
```
Parameters:
  pattern  str  required  Regex pattern
  path     str  optional  Search root (default ".")
  include  str  optional  File-glob filter (e.g. "*.py")
  context  int  optional  Lines before/after each match

Returns:
  {
    "status":  "ok"|"error",
    "output":  str,
    "matches": [ {"file_path": str, "line_number": int, "content": str}, ... ]
  }
  Capped at 20 displayed matches.
```

#### `get_git_diff`
```
Parameters:  none

Returns:
  { "diff": str }  or  { "status": "error", "error": str }
```

#### `summarize_structure`
```
Parameters:
  path        str  optional  Root path (default ".")
  max_entries int  optional  Cap on entries shown

Returns:
  {
    "path":       str,
    "file_count": int,
    "dir_count":  int,
    "total_size": int,
    "top":        [ {"name": str, "is_dir": bool, "size": int}, ... ]
  }
```

### 5.3 Verification and testing tools

#### `run_tests`
```
Parameters:
  workdir         str        required  Project root
  test_files      [str]      optional  Specific test files / node IDs
  use_last_failed bool       optional  Pass --lf to pytest
  changed_files   [str]      optional  Filter to tests covering these files

Returns:
  {
    "status":       "ok" | "fail" | "error" | "skipped",
    "returncode":   int,
    "passed":       int,
    "failed":       int,
    "failed_tests": [str],           ← list of failing test IDs
    "errors":       [ {type, message} ],
    "tracebacks":   [str],
    "summary":      str              ← last 1000 chars of pytest output
  }

Timeout: 120 seconds
```

#### `run_linter`
```
Parameters:
  workdir  str   required  Project root
  fix      bool  optional  Apply auto-fixes (default false)

Returns:
  {
    "status":        "ok" | "fail" | "error" | "skipped",
    "returncode":    int,
    "error_count":   int,
    "warning_count": int,
    "errors": [
      { "file": str, "line": int, "column": int,
        "code": str, "message": str, "severity": str }
    ],
    "summary": str
  }

Timeout: 60 seconds
```

#### `syntax_check`
```
Parameters:
  workdir       str    required  Root to walk
  timeout_secs  float  optional  Deadline (default 30.0)

Returns:
  {
    "checked_files":  int,
    "syntax_errors":  [ {"file": str, "line": int, "error": str, "type": str} ],
    "status":         "ok" | "fail" | "partial" | "error",
    "error_count":    int,
    "warning":        str    ← set when status="partial" (deadline hit)
  }
```

#### `run_js_tests`
```
Parameters:
  workdir     str    required  Project root
  test_files  [str]  optional  Specific files

Returns:
  {
    "status":     "ok" | "fail" | "error" | "skipped",
    "runner":     str,   ← "jest" | "vitest" | "mocha"
    "returncode": int,
    "passed":     int | null,
    "failed":     int | null,
    "summary":    str
  }

Runner auto-detected from package.json scripts.
Timeout: 120 seconds
```

#### `run_ts_check`
```
Parameters:
  workdir  str  required  Project root

Returns:
  {
    "status":      "ok" | "fail" | "error" | "skipped",
    "returncode":  int,
    "error_count": int,
    "errors": [ {"file": str, "line": int, "column": int, "code": str, "message": str} ],
    "summary":     str
  }

Runs: npx tsc --noEmit (or global tsc). Timeout: 120 seconds
```

#### `run_eslint`
```
Parameters:
  workdir  str    required  Project root
  paths    [str]  optional  Specific paths to lint

Returns:
  {
    "status":        "ok" | "fail" | "error" | "skipped",
    "returncode":    int,
    "error_count":   int,
    "warning_count": int,
    "errors": [ {"file": str, "line": int, "column": int, "severity": str, "message": str} ],
    "summary":        str
  }

Timeout: 60 seconds
```

### 5.4 Git tools

#### `git_status`
```
Returns:
  {
    "status": "ok"|"error",
    "branch": str,
    "files":  [str],   ← short format: "XY filename"
                          X = index status (M/A/D/R/C/?/!)
                          Y = working-tree status
    "output": str      ← full git output
  }
```

#### `git_log`
```
Parameters:
  max_count  int  optional  Commits to return (default 10)

Returns:
  { "status": "ok"|"error", "commits": [str] }
  Each commit: "HASH message" (--oneline format)
```

#### `git_diff`
```
Parameters:
  staged  bool  optional  Show staged changes (default false = working tree)
  path    str   optional  Filter to single path

Returns:
  { "status": "ok"|"error", "diff": str }
```

#### `git_commit`
```
Parameters:
  message  str   required  Commit message (non-empty)
  add_all  bool  optional  Run git add -A before committing

Returns:
  { "status": "ok"|"error", "output": str, "summary": str }

Side fx: ["write"]
```

#### `git_stash`
```
Parameters:
  message  str  optional  Stash message

Returns:
  { "status": "ok"|"error", "output": str, "error": str }

Side fx: ["write"]
```

#### `git_restore`
```
Parameters:
  path    str   required  File or directory to restore
  staged  bool  optional  Unstage instead of discard (default false)

Returns:
  { "status": "ok"|"error", "path": str, "output": str }

Side fx: ["write"]
```

### 5.5 Patch tools

#### `generate_patch`
```
Parameters:
  path        str  required  Target file
  new_content str  required  Desired full content

Returns:
  { "status": "ok"|"error", "patch": str }   ← unified diff
```

#### `apply_patch`
```
Parameters:
  path   str  required  Target file
  patch  str  required  Unified diff to apply

Returns:
  { "path", "status": "ok"|"error", "lines_added", "lines_removed", "diff" }

Side fx: ["write"]
Security: requires prior read_file
```

### 5.6 TODO management

#### `manage_todo`
```
Parameters:
  action      str        required  "create" | "check" | "update" | "read" | "clear"
  description str        optional  Step description (for "update" / "check")
  step_id     int        optional  0-based step index (for "check" / "update")
  steps       [str]      optional  List of step descriptions (for "create")
  depends_on  [[int]]    optional  Dependency graph — list of lists (for "create")

Returns:
  {
    "status":     "ok" | "error",
    "action":     str,
    "steps":      [ {"description": str, "done": bool, "depends_on": [int]} ],
    "step_count": int,
    "done_count": int,
    "total":      int,
    "todo_path":  str   ← absolute path to TODO.md
  }

Side fx: ["write"]
Storage: .agent-context/TODO.md (human-readable) + .agent-context/todo.json (machine)
```

### 5.7 Subagent tools

#### `delegate_task`
```
Parameters:
  role                 str  required  "analyst" | "operational" | "strategic" | "reviewer" | "debugger"
                                      (also accepts aliases: "coder", "planner", "qa", "debug", "research")
  subtask_description  str  required  Detailed description of what the subagent must do
  working_dir          str  optional  Override working directory for subagent

Returns:
  str  — prose summary of what the subagent did and its results

Depth limit: 3 (enforced via CODINGAGENT_DELEGATION_DEPTH env var; deeper calls error)
```

#### `list_subagent_roles`
```
Returns:
  {
    "status": "ok",
    "available_roles": {
      "analyst":     { "description": str, "best_for": str, "aliases": [str] },
      "operational": { ... },
      "strategic":   { ... },
      "reviewer":    { ... },
      "debugger":    { ... }
    },
    "note": str
  }
```

### 5.8 Repository analysis

#### `analyze_repository`
```
Parameters:
  workdir  str  required  Repository root

Returns:
  { "status": "ok"|"error", "message": str }

Writes: .agent-context/repo_memory.json (classes, functions, imports for all .py files)
```

### 5.9 State and checkpoint tools

#### `create_state_checkpoint`
```
Parameters:
  current_task       str    required
  tool_call_history  [dict] required
  modified_files     [str]  required
  reasoning_summary  str    required

Returns:
  { "status": "ok"|"error", "checkpoint_id": str, "checkpoint_path": str }

Storage: .agent-context/checkpoints/checkpoint_TIMESTAMP.json
```

#### `list_checkpoints`
```
Returns:
  { "status": "ok", "checkpoints": [ {"checkpoint_id", "created_at", "current_task"} ] }
```

#### `restore_state_checkpoint`
```
Parameters:
  checkpoint_id  str  required  (alphanumeric, dash, underscore only)

Returns:
  {
    "status":            "ok"|"error",
    "checkpoint_id":     str,
    "current_task":      str,
    "tool_call_history": list,
    "modified_files":    list,
    "reasoning_summary": str
  }
```

#### `diff_state`
```
Parameters:
  checkpoint_id1  str  required
  checkpoint_id2  str  required

Returns:
  {
    "status": "ok"|"error",
    "diff":   {
      "tasks_different":        bool,
      "tool_calls_added":       int,
      "files_modified_added":   [str]
    }
  }
```

#### `batched_file_read`
```
Parameters:
  paths          [str]  required  Files to read
  max_file_size  int    optional  Skip files over this byte size (default 10000)

Returns:
  {
    "status": "ok"|"error",
    "files":  { "<path>": {"content": str} | {"error": str} }
  }
```

#### `multi_file_summary`
```
Parameters:
  paths  [str]  required

Returns:
  {
    "status": "ok"|"error",
    "files":  [ {"path", "size", "lines", "type", "error"} ]
  }
```

### 5.10 Memory and system tools

#### `memory_search`
```
Parameters:
  query  str  required

Returns:
  { "status": "ok"|"error", "results": [...] }
  Searches TASK_STATE.md and execution trace.
```

#### `grep` (also listed under search tools above)

#### `get_git_diff` (also listed under search tools above)

### 5.11 Role-to-toolset mapping

| Role | Available tools |
|---|---|
| **operational** (coding) | read_file, write_file, edit_file, edit_file_atomic, edit_by_line_range, delete_file, list_files, glob, search_code, find_symbol, find_references, grep, run_tests, run_linter, syntax_check, apply_patch, generate_patch, get_git_diff, read_file_chunk, batched_file_read, manage_todo, git_status, git_log, git_diff, git_commit, git_stash, git_restore, bash |
| **strategic** (planning) | read_file, list_files, search_code, find_symbol, find_references, memory_search, analyze_repository, initialize_repo_intelligence, grep, multi_file_summary, batched_file_read, delegate_task, list_subagent_roles |
| **analyst** (research) | Same as strategic |
| **reviewer** (QA) | read_file, list_files, run_tests, run_linter, syntax_check, get_git_diff, grep, find_symbol, find_references, multi_file_summary, search_code |
| **debugger** | read_file, list_files, grep, search_code, find_symbol, find_references, run_tests, run_linter, syntax_check, bash, get_git_diff, memory_search, batched_file_read, multi_file_summary |

---

## 6. Tool Result Display Requirements

### 6.1 Tool call lifecycle display

For every tool call the TUI must show three beats:

```
① tool.execute.start  →  show tool name + args summary  (in-progress indicator)
② tool.execute.finish →  replace ① with formatted result
   or tool.execute.error →  replace ① with error message
```

Render inline in the chat output stream. Do not show a separate "tool log" widget unless it is additive — the chat panel must also show tool activity.

### 6.2 Tool argument display

Truncate argument values longer than 120 characters with `…`. For `content` or `patch` arguments omit the value entirely and show `<N chars>` instead:

```
✦ write_file  path="src/foo.py"  content=<1 243 chars>
✦ bash        command="pytest tests/unit/ -q"
✦ edit_file_atomic  path="src/bar.py"  old_string="def foo():"  new_string="def foo(x: int):"
```

### 6.3 Formatted result rendering by tool

The Orchestrator computes `result_formatted` for every call. The TUI must render the `content[0]["text"]` field from `tool.execute.finish`. The formats per tool are:

#### File listing (`list_files`)
```
📁 src/
📁 tests/
📄 README.md
📄 pyproject.toml
```
One icon-prefixed line per item. Directories get `📁`, files get `📄`.

#### File read (`read_file`)
```
File: src/core/orchestration/orchestrator.py
─────────────────────────────────────────────
<file content, possibly truncated>
... [N more lines]
```

#### Code search (`search_code`, `find_symbol`, `find_references`)
```
Found 3 results:
  📄 src/core/orchestration/orchestrator.py
     class Orchestrator: manages the agent runner, tool registry…

  📄 src/core/orchestration/graph/state.py
     AgentState TypedDict with 60+ fields…
```
Cap at 10 results.

#### Grep (`grep`)
```
Found 5 matches:
  src/tools/file_tools.py:42:    def write_file(path, content, workdir):
  src/tools/file_tools.py:98:    def edit_file(path, patch, workdir):
  …
```
Cap at 20 displayed matches.

#### File write / edit (`write_file`, `edit_file`, `edit_file_atomic`, `edit_by_line_range`, `apply_patch`)
```
✓ Modified src/core/foo.py  [+12 / -3 lines]
```diff
--- a/src/core/foo.py
+++ b/src/core/foo.py
@@ -10,7 +10,19 @@
 def bar():
-    pass
+    return 42
```
Truncate diffs > 60 lines with `… N more lines`. For new files, show `✓ Created path [+N lines]`.

#### File delete (`delete_file`)
```
✓ Deleted src/old_file.py
⚠ Warning: file was git-tracked
```

#### Tests (`run_tests`, `run_js_tests`)

On pass:
```
✅ Tests passed  (47 passed, 0 failed)
```

On failure:
```
❌ Tests failed  (45 passed, 2 failed)

Failed tests:
  tests/unit/test_foo.py::TestBar::test_baz
  tests/unit/test_foo.py::TestBar::test_qux

Traceback (last):
  AssertionError: expected 42, got None
```

#### Linter (`run_linter`)

On pass:
```
✅ Linter clean
```

On failure:
```
❌ Linter: 3 errors, 1 warning

  src/core/foo.py:42:8  E501  line too long (120 > 79 characters)
  src/core/bar.py:7:1   F401  'os' imported but unused
```

#### TypeScript check (`run_ts_check`)
```
❌ TypeScript: 2 errors

  src/app.ts:15:3  TS2345  Argument of type 'string' is not assignable to parameter of type 'number'.
```

#### ESLint (`run_eslint`)
```
❌ ESLint: 1 error, 2 warnings

  src/app.js:10:5  error    no-unused-vars: 'foo' is defined but never used
  src/app.js:20:1  warning  no-console: Unexpected console statement
```

#### Syntax check (`syntax_check`)
```
✅ Syntax OK  (38 files checked)
```
or:
```
❌ Syntax errors in 2 files:

  src/broken.py:15  SyntaxError: invalid syntax
```
or (deadline hit):
```
⚠ Syntax check partial (deadline exceeded after 24 files)
```

#### Git tools (`git_status`, `git_log`, `git_diff`, `git_commit`, `git_stash`, `git_restore`)

`git_status`:
```
Branch: main

M  src/core/orchestrator.py
??  docs/new_file.md
```

`git_log`:
```
a1b2c3d  fix: correct token budget calculation
e4f5a6b  feat: add streaming token support
```

`git_diff`:
```diff
--- a/src/core/foo.py
+++ b/src/core/foo.py
@@ -10,3 +10,4 @@
 def bar():
+    """Return the answer."""
     return 42
```
Truncate at 100 lines with `… N more lines`.

`git_commit`:
```
✓ Committed: "feat: add plan mode approval"
  1 file changed, 15 insertions(+), 2 deletions(-)
```

#### Bash (`bash`)
```
$ pytest tests/ -q --tb=short
..........
10 passed in 0.42s
```
Show the command, then `stdout` (truncated at 80 lines / 4 KB), then `stderr` in a distinct style if non-empty. Returncode ≠ 0 adds a `⚠ Exit code: N` footer.

For `requires_approval=true`:
```
⚠ This command requires approval:  pip install requests
[Approve] [Deny]
```
Block until the user responds. On approval, republish the tool call. On denial, return `{"status": "error", "error": "user denied"}`.

#### TODO (`manage_todo`)

`read` / `create` / `check` / `update`:
```
📋 TODO  (3/5 done)

  ✅ 1. Analyse existing auth module
  ✅ 2. Write unit tests
  ✅ 3. Implement login endpoint
  ⬜ 4. Implement logout endpoint
  ⬜ 5. Add rate limiting
```

#### Subagent delegation (`delegate_task`)

While running:
```
🤖 Delegating to analyst agent…
```

On completion, render the returned string as plain agent output (markdown-aware).

#### Checkpoint tools (`create_state_checkpoint`, `list_checkpoints`, `restore_state_checkpoint`)
```
✓ Checkpoint saved: cp_20260326_142500
```
```
Checkpoints:
  cp_20260326_142500  2026-03-26 14:25  "Implement auth module"
  cp_20260326_130000  2026-03-26 13:00  "Fix token budget…"
```

#### Tool errors (any tool)
```
✗ edit_file failed: path "src/missing.py" not found
```
or for security blocks:
```
✗ write_file blocked: read "src/config.py" before editing it
```

### 6.4 Diff preview panel

When `file.diff.preview` fires, render the diff **before** the write completes. Suggested placement: inline in chat, immediately before the subsequent `tool.execute.finish` line.

```
┌─ Preview: src/core/foo.py ────────────────────────────────────┐
│ @@ -10,4 +10,6 @@                                            │
│   def bar():                                                  │
│ -     pass                                                    │
│ +     """Return the answer."""                                │
│ +     return 42                                               │
└───────────────────────────────────────────────────────────────┘
```

Colour convention: `+` lines green, `-` lines red, `@@` lines cyan. Truncate at 60 lines.

### 6.5 Streaming token display

```python
def _on_model_token(self, payload: dict) -> None:
    text    = payload["text"]
    partial = payload.get("partial", True)
    if partial:
        self._schedule_callback(self._append_to_current_response, text)
    else:
        self._schedule_callback(self._finalise_response, text)
```

Show a blinking cursor or spinner while `partial=True` tokens are arriving.

---

## 7. Orchestrator Public API

All interaction with the engine goes through these methods. Private attributes are not part of the API.

```python
class Orchestrator:
    # ── Exposed attributes (read-only) ──────────────────────────────
    working_dir:      Path          # workspace root
    event_bus:        EventBus      # same as get_event_bus()
    tool_registry:    ToolRegistry  # registered tools
    session_store:    SessionStore  # SQLite-backed session data
    rollback_manager: RollbackManager
    plan_mode:        PlanMode      # plan-first mode manager

    # ── Task lifecycle ───────────────────────────────────────────────
    def start_new_task(self) -> str:
        """Reset per-task state; return new task ID.
        Resets: msg_mgr.messages, _session_read_files, _session_modified_files,
                _execution_trace_buffer, rollback snapshots, plan_mode,
                _pending_delegations, PreviewService.pending_previews.
        Must be called before every run_agent_once()."""

    async def run_agent_once(
        self,
        system_prompt_name: str,
        messages:           list,
        tools:              list,
        cancel_event:       threading.Event,
    ) -> dict:
        """Run the full LangGraph pipeline for one task.
        Returns {"response": str, "last_result": dict, ...}.
        Blocking — always run on a background thread."""

    def get_current_task_id(self) -> Optional[str]: ...

    def flush_execution_trace(self) -> None:
        """Flush in-memory trace buffer to .agent-context/execution_trace.jsonl.
        Call once at task end."""

    # ── Token budget ─────────────────────────────────────────────────
    def get_budget_status(self, session_id: str = "default") -> dict:
        """Returns {"used": int, "limit": int, "percent": float}."""

    # ── Role-filtered tools ──────────────────────────────────────────
    def get_tools_for_role(self, role: str) -> list:
        """Returns list of tool dicts for the given role.
        Falls back to full registry if toolset unknown or <3 tools match."""

    # ── Plan mode ────────────────────────────────────────────────────
    def approve_plan(self) -> None: ...
    def reject_plan(self) -> None: ...
    async def wait_for_plan_approval(self) -> bool: ...

    # ── /continue workflow ───────────────────────────────────────────
    def restore_continue_state(self, state: dict) -> None:
        """Restore saved conversation state.
        state keys (all optional):
          history:            list   — message list to restore into msg_mgr
          session_read_files: list   — files read this session
          current_plan:       list   — plan steps
          current_step:       int    — plan cursor
          working_dir:        str
          step_retry_counts:  dict
        """
```

### 7.1 Running the agent — canonical pattern

```python
import asyncio, threading

def send_prompt(self, text: str) -> None:
    with self._agent_lock:
        if self._agent_running:
            return               # reject duplicate
        self._agent_running = True
    self._cancel_event.clear()
    with self._history_lock:
        self.history.append(("user", text))
    thread = threading.Thread(target=self._run_agent, args=(text,), daemon=True)
    thread.start()

def _run_agent(self, text: str) -> None:
    try:
        self.orchestrator.start_new_task()
        result = asyncio.run(self.orchestrator.run_agent_once(
            system_prompt_name="operational",
            messages=list(self.orchestrator.msg_mgr.messages),
            tools=self.orchestrator.get_tools_for_role("operational"),
            cancel_event=self._cancel_event,
        ))
        self.orchestrator.flush_execution_trace()
        content = (result.get("response")
                   or result.get("last_result", {}).get("output", ""))
        with self._history_lock:
            self.history.append(("assistant", content))
        self._save_history()
        self._schedule_callback(self.on_agent_result, content)
    except Exception as exc:
        self._schedule_callback(self.on_agent_result, f"Error: {exc}")
    finally:
        with self._agent_lock:
            self._agent_running = False
```

---

## 8. ProviderManager Public API

```python
from src.core.inference.llm_manager import get_provider_manager

pm = get_provider_manager()

pm.list_providers()            -> list[str]
pm.get_provider(key: str)      -> Optional[Adapter]
pm.get_cached_models(key: str) -> list[str]
```

`CodingAgentApp` calls `pm.set_event_bus()` and `await pm.initialize()`. The TUI must not call these again.

All adapters support:
- Exponential back-off retry (3 attempts; 1 s / 2 s sleep) for HTTP 429/500/502/503/504 and ConnectionError.
- Streaming via `model.token` events.

---

## 9. Concurrency and Threading Contract

### 9.1 Required primitives

```python
self._agent_lock:    threading.Lock    # guards _agent_running
self._agent_running: bool             # True while pipeline runs
self._cancel_event:  threading.Event  # set = interrupt the running agent
self._history_lock:  threading.Lock   # guards self.history
self.history:        list             # [(role, text), ...]
```

### 9.2 Starting a task

```python
with self._agent_lock:
    if self._agent_running:
        return     # do not queue; reject immediately
    self._agent_running = True
```

### 9.3 Clearing the running flag — only one location

```python
def _run_agent(self, text: str) -> None:
    try:
        ...
    finally:
        with self._agent_lock:
            self._agent_running = False
```

No other code path may write `_agent_running` without the lock.

### 9.4 Interrupt (Escape key)

```python
def interrupt(self) -> None:
    self._cancel_event.set()
    # Do NOT touch _agent_running here.
    # The finally block in _run_agent is authoritative.
```

### 9.5 Force interrupt (double-Escape or dedicated button)

```python
def force_interrupt(self) -> None:
    self._cancel_event.set()
    with self._agent_lock:
        running = self._agent_running
        if running:
            self._agent_running = False
    if running:
        self._show_message("Agent force stopped.")
```

### 9.6 Scheduling UI updates from background threads

```python
def _schedule_callback(self, fn: Callable, *args) -> None:
    # Override per framework:
    # Textual:  self.call_from_thread(fn, *args)
    # asyncio:  loop.call_soon_threadsafe(fn, *args)
    # tkinter:  self.after(0, fn, *args)
    # Qt:       QMetaObject.invokeMethod(...)
    raise NotImplementedError
```

EventBus callbacks run on the publisher thread (often the agent thread). Always schedule widget updates via `_schedule_callback`.

---

## 10. Lifecycle Contract

### 10.1 Startup sequence

```
1.  app = CodingAgentApp(config)           # wires all core services
2.  bus = get_event_bus()
3.  orch = app.orchestrator
4.  _load_history()                        # restore from disk
5.  _subscribe(...)  for all events in §4  # track in _subscriptions
6.  bus.publish("session.request_state",   # trigger hydration
                {"session_id": current_id})
7.  Render saved history in chat panel
8.  Enter UI event loop
```

### 10.2 Shutdown sequence

```
1.  _cancel_event.set()                    # interrupt agent if running
2.  Join _agent_thread (timeout 5 s)
3.  orchestrator.flush_execution_trace()
4.  _save_history()                        # atomic write
5.  cleanup()                              # unsubscribe all EventBus handlers
6.  app.shutdown()                         # stops SessionWatcher, CrossSessionBus
7.  Exit UI event loop
```

### 10.3 New session (`/new`)

```
1.  orchestrator.start_new_task()
2.  with _history_lock: history.clear()
3.  _save_history()
4.  bus.publish("session.new", {"timestamp": time.time()})
5.  Clear chat output panel
6.  Reset all sidebar panels to initial state
```

### 10.4 Continue (`/continue`)

**Save before interruption:**
```python
import copy
continue_state = {
    "history":            list(orchestrator.msg_mgr.messages),
    # Reading _session_read_files once for snapshot is acceptable
    "session_read_files": list(getattr(orchestrator, "_session_read_files", set())),
    "current_plan":       last_agent_state.get("current_plan"),
    "current_step":       last_agent_state.get("current_step"),
    "working_dir":        last_agent_state.get("working_dir"),
    "step_retry_counts":  last_agent_state.get("step_retry_counts"),
}
```

**Restore:**
```python
orchestrator.restore_continue_state(continue_state)
# Then call send_prompt("continue") or re-enter with the previous task text
```

---

## 11. Slash Command Contract

| Command | Expected behaviour |
|---|---|
| `/help` | Print list of all slash commands with one-line descriptions |
| `/clear` | Clear the chat output panel; keep history list intact |
| `/new` or `/reset` | New session (§10.3) |
| `/compact` | Run `compact_messages_to_prose()` on background thread; replace chat with `[dim]Context compacted: N → 1 message.[/dim]` |
| `/continue` | Restore saved state (§10.4); re-run the agent with the previous prompt |
| `/interrupt` | Set `_cancel_event` |
| `/status` | Show: agent running Y/N, current provider, current model, working dir, task ID |
| `/provider [n]` | No arg → list providers with index. With arg → switch to provider by index or name. Publish `ui.notification` on success. |
| `/model [n]` | No arg → list models for active provider. With arg → switch model. |
| `/settings` | Open provider/model configuration surface |
| `/quit` | Execute §10.2 shutdown sequence |

Any unrecognised `/xyz` command falls through to the agent as plain text.

---

## 12. UI Panel Requirements

### 12.1 Required panels

| Panel | Content | Update trigger |
|---|---|---|
| **Chat output** | Conversation turns: user + assistant + tool calls | `on_agent_result`, `model.token`, tool events |
| **Chat input** | Text field with Up/Down history, Tab autocomplete for slash commands | User interaction |
| **Task status** | Current task text, truncated to 80 chars | On each `send_prompt()` |
| **Plan progress** | `Step N / M — description` + progress bar | `plan.progress` |
| **Tool activity** | Last tool name, ✓/✗, brief result | `tool.execute.finish`, `tool.execute.error` |
| **Token budget** | Used / limit / percent, colour-coded | `token.budget.update` |
| **Provider / model** | Active provider name + model name | `model.routing`, `provider.status.changed` |
| **Working directory** | Absolute path | Static; set at startup |

### 12.2 Chat output rendering rules

- **User turns:** display with a visual distinction (bold name, different background, or left-aligned bubble).
- **Assistant turns:** plain markdown-aware text; support code fences (`` ``` ``) with syntax highlighting.
- **Tool calls:** render as per §6 — in-progress indicator → formatted result.
- **Streaming tokens:** append character-by-character; clear in-progress indicator when `partial=False`.
- **Diff previews:** render inline before the corresponding write completes (§6.4).
- **Notifications:** `ui.notification` events appear as dismissible banners or inline system messages.

### 12.3 Progress bar format

```
▓▓▓▓▓▓▒▒▒▒  3 / 5  Implementing login endpoint
```

Update on every `plan.progress` event. Accept both ACP schema (`currentStep`, `totalSteps`, `stepDescription`) and legacy (`step`, `total`, `description`).

### 12.4 Token budget colour coding

| Percent | Colour |
|---|---|
| 0 – 60 % | green |
| 61 – 85 % | yellow |
| 86 – 100 % | red |

Show `token.budget.warning` as a prominent inline warning in the budget panel.

### 12.5 Input field requirements

- **History:** Up/Down keys cycle through previous inputs (same session).
- **Slash autocomplete:** Tab key cycles through matching slash commands; show dropdown of completions.
- **Multi-line paste:** preserve literal newlines; do not collapse to `\n` characters.
- **Disabled while agent runs:** dim the field and reject input (except slash commands that do not send to the agent, e.g. `/interrupt`, `/status`).

---

## 13. Settings and Configuration

### 13.1 `providers.json` format

Location: `src/config/providers.json`

```json
[
  {
    "name":     "ollama",
    "type":     "ollama",
    "base_url": "http://localhost:11434",
    "model":    "llama3.2",
    "enabled":  true
  },
  {
    "name":     "lmstudio",
    "type":     "lmstudio",
    "base_url": "http://localhost:1234",
    "model":    "mistral-7b-instruct",
    "api_key":  "",
    "enabled":  false
  },
  {
    "name":    "openrouter",
    "type":    "openrouter",
    "model":   "anthropic/claude-3-5-sonnet",
    "api_key": "sk-or-…",
    "enabled": false
  }
]
```

**Supported types:** `ollama`, `lmstudio`, `openrouter`

### 13.2 Atomic write

```python
import os, tempfile, json

def save_providers(config_path: str, providers: list) -> None:
    dir_ = os.path.dirname(os.path.abspath(config_path))
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(providers, f, indent=2)
        os.replace(tmp, config_path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
```

### 13.3 Settings UI requirements

The settings surface must allow the user to:
- Enable / disable each provider.
- Change the model for each provider (shown as a dropdown populated from `pm.get_cached_models(key)`).
- Set the `base_url` for local providers.
- Set the `api_key` for cloud providers (OpenRouter). Store as-is in `providers.json`; inject into the live adapter's HTTP headers on save.
- Reload providers after save (call `await pm.initialize()` on a background thread, or display a "restart required" notice).

---

## 14. Plan Mode and Preview Mode

### 14.1 Plan mode

When `orchestrator.plan_mode.enabled` is True, the agent proposes a plan before executing any write tools. The TUI must surface an approval UI.

```python
status = orchestrator.plan_mode.get_status()
# {
#   "enabled":          bool,
#   "has_pending_plan": bool,
#   "blocked_tools":    ["edit_file", "write_file", "delete_file",
#                        "edit_by_line_range", "apply_patch"]
# }
```

On `plan.requested` event (or when `status["has_pending_plan"]` is True):
1. Render the plan text to the user.
2. Show **Approve** and **Reject** buttons / keyboard shortcuts.
3. On Approve: `orchestrator.approve_plan()`
4. On Reject: `orchestrator.reject_plan()`

Blocked tool calls while pending should display:
```
⏸ write_file blocked — awaiting plan approval
```

### 14.2 Preview mode

`file.diff.preview` is always published before writes; the TUI renders it (§6.4). No user action is required unless `AgentState.awaiting_user_input` is True, in which case publish `preview.confirmed` or `preview.rejected` to unblock the agent.

---

## 15. History Persistence

### 15.1 Storage path

```python
from pathlib import Path

def _get_history_path(self) -> Path:
    return Path.home() / ".coding_agent" / "tui_conversation_history.json"
```

### 15.2 Format

```json
[
  ["user",      "implement the login feature"],
  ["assistant", "I'll start by analysing the authentication module…"]
]
```

List of `[role, content]` pairs. Roles: `"user"`, `"assistant"`.

### 15.3 Atomic load

```python
def _load_history(self) -> None:
    path = self._get_history_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            with self._history_lock:
                self.history = [tuple(item) for item in data
                                if isinstance(item, (list, tuple)) and len(item) == 2]
    except Exception:
        pass   # corrupt; start fresh
```

### 15.4 Atomic write

```python
def _save_history(self) -> None:
    path = self._get_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            with self._history_lock:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
```

---

## 16. Security Rules the TUI Must Enforce

### 16.1 `bash` Tier-3 approval gate

When `tool.execute.finish` carries a result with `requires_approval=true` (or when `tool.execute.start` is received for a Tier-3 command and the TUI intercepts before execution), the TUI must display the command and block until the user approves or denies. Do not pass `user_approved` as a tool argument — this is enforced server-side; the TUI's approval gate is informational.

### 16.2 Read-before-write user messaging

If the agent receives a write-tool error containing "must read … before writing", display it prominently:

```
✗ Blocked: you must read "src/config.py" before editing it
```

Do not attempt to auto-resolve this; the agent will re-plan.

### 16.3 Plan mode write blocking display

If a write tool is blocked by plan mode show:
```
⏸ write_file  →  waiting for plan approval
```
and surface the approve/reject controls.

### 16.4 No recursive `log.new` subscription

Never write:
```python
bus.subscribe("log.new", lambda p: logger.info(p["message"]))
```
This creates an infinite loop. Render `log.new` directly to the debug log panel without going through Python's logging system.

### 16.5 Path display

All file paths displayed to the user should be relative to `orchestrator.working_dir` when possible. Never display resolved absolute paths that expose home directory structure unless the user explicitly requests it.

---

## 17. Non-Goals / Out of Scope

A TUI implementation is **not** responsible for:

- Any LLM inference logic or adapter implementation.
- The LangGraph pipeline, AgentState machine, or graph nodes.
- Tool execution (handled by `Orchestrator.execute_tool()`).
- Vector store indexing or semantic search.
- Session distillation or context compaction (handled by `distiller.py`).
- SQLite session storage (handled by `session_store.py`).
- Starting `SessionWatcher`, `SessionRegistry`, or `CrossSessionBus` (done by `CodingAgentApp`).
- Interpreting `AgentState` TypedDict fields — the TUI receives formatted output via events and `on_agent_result()`.
- Writing to `.agent-context/` files other than through registered tools.

---

## 18. Compliance Checklist

### EventBus
- [ ] Uses `get_event_bus()` exclusively — never `EventBus()` constructor
- [ ] All subscriptions tracked; all unsubscribed during shutdown
- [ ] `log.new` is rendered directly, never fed into a logging call
- [ ] EventBus callbacks offload blocking work to daemon threads

### Threading
- [ ] `_agent_running = True` only inside `_agent_lock`
- [ ] `_agent_running = False` only inside `_agent_lock` in the `finally` block of `_run_agent`
- [ ] No second agent thread spawned while `_agent_running` is True
- [ ] UI widget updates from EventBus callbacks go through `_schedule_callback`
- [ ] `history` mutations protected by `_history_lock`

### Orchestrator access
- [ ] Only public methods used (no `_` prefix except the one-time `/continue` snapshot)
- [ ] `orchestrator.restore_continue_state()` used for `/continue`
- [ ] `orchestrator.start_new_task()` called before each `run_agent_once()`
- [ ] `orchestrator.flush_execution_trace()` called at task end

### Tool display
- [ ] Every tool call shows: in-progress → formatted result (or error)
- [ ] `bash requires_approval=true` blocks until user approves or denies
- [ ] `file.diff.preview` rendered inline before write completes
- [ ] Both ACP schema and legacy field names handled for tool and plan events
- [ ] Diffs truncated at 60 lines with `… N more lines`
- [ ] Test output shows: passed count, failed count, failed test names, traceback excerpt

### Lifecycle
- [ ] History loaded from `~/.coding_agent/tui_conversation_history.json` on startup
- [ ] History saved atomically (tempfile + `os.replace`) after every agent result
- [ ] `session.request_state` published on startup
- [ ] `app.shutdown()` called during teardown
- [ ] `cleanup()` unsubscribes all EventBus handlers

### UI panels
- [ ] All 8 panels from §12.1 present and updated from correct events
- [ ] Token budget colour-coded by threshold (§12.4)
- [ ] Input disabled while agent runs (except non-agent slash commands)
- [ ] Plan progress bar shows step N/M + description

### Settings
- [ ] `providers.json` written atomically
- [ ] Provider and model selection surfaces all available options
- [ ] API key stored in `providers.json`; not logged or displayed in plaintext

### Security
- [ ] `bash` Tier-3 commands gated behind user approval
- [ ] Read-before-write errors displayed clearly to user
- [ ] Plan mode blocked writes shown with approve/reject controls
- [ ] No recursive `log.new` subscription
