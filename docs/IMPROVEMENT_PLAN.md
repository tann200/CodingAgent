# CodingAgent — Detailed Architecture Improvement Plan

> **Scope:** Section-by-section analysis of the CodingAgent codebase
> benchmarked against Claw Code architectural patterns.
> **Reference:** `docs/CLAW_CODE_ARCHITECTURE_REFERENCE.md`
> **Claw Code source:** `/Users/tann200/PycharmProjects/claw-code` (read directly)
> **Goal:** Local-first coding agent that also supports frontier cloud models.
> **Date:** 2026-05-24
> **Status:** Source-verified. All file paths and line counts confirmed from live codebase.

---

## Table of Contents

1. [Overall Architecture](#1-overall-architecture)
2. [Tooling](#2-tooling)
3. [Memory Management](#3-memory-management)
4. [Orchestration](#4-orchestration)
5. [User Interface & Usability](#5-user-interface--usability)
6. [Cross-Cutting Issues](#6-cross-cutting-issues)
7. [Improvement Plan — Prioritised](#7-improvement-plan--prioritised)
8. [What CodingAgent Does Better Than Claw Code](#8-what-codingagent-does-better-than-claw-code)
9. [Source-Verified Corrections vs Claw Code Docs](#9-source-verified-corrections-vs-claw-code-docs)

---

## 1. Overall Architecture

### Current State

CodingAgent is a **single-language Python system** built on LangGraph. The
entry point is `src/main.py`, with a thin shim at the repo root `main.py`.

```
User (TUI or CLI)
      |
   src/main.py  ←── root main.py shim
      |
  Orchestrator (orchestrator.py — 487 lines, re-export hub)
      |
  LangGraph StateGraph (graph/builder.py — 655 lines, 16 nodes, 3 variants)
      |
  Inference layer (llm_manager.py → adapters/ → providers)
      |
  Local LLMs (Ollama, LM Studio) or Cloud APIs (Anthropic, OpenAI, Copilot, …)
```

The architecture correctly separates:
- **Orchestration** (`src/core/orchestration/`) — 73 files (108 Python files total including subdirs)
- **Inference** (`src/core/inference/`) — 27 files, 10 provider adapters
- **Memory** (`src/core/memory/`) — 20 files
- **Tools** (`src/tools/`) — 49 files
- **TUI** (`tui/`) — separate Textual application, own `pyproject.toml`

### Strengths

- Clear local-first philosophy with 5-tier model system
  (NANO → SMALL → MEDIUM → LARGE → FRONTIER) adapting tool count and
  context budget per tier
- Multi-provider support across 10 adapter types from a single abstraction
- LangGraph `StateGraph` gives a well-defined state machine with typed
  `AgentState` (~100 fields, well-sectioned)
- Separation of TUI from core via EventBus bridge is the right pattern
- SQLite session store + JSONL sidecar for durability is robust
- `validate_state()` in `state.py` catches field type errors at every node entry

### Weaknesses

**W1 — No performance layer.** Everything runs in Python. For a local-first
agent, this limits responsiveness with local models where latency is already
high. Streaming, tool execution, and terminal rendering all contend on the GIL.

**W2 — Split TUI package.** `tui/` is a fully separate package with its own
`pyproject.toml`, `src/` tree, and path loader (`_core_paths_loader.py`).
The bridge (`core_bridge.py` — 1840 lines) re-imports `src.core` across
package boundaries, causing fragile import resolution and test complexity.

**W3 — Orchestrator.py is a re-export hub.** The file's primary job is
backwards-compatibility re-exports from ~10 other modules. The actual
`Orchestrator` class has attributes declared `Any` because they are dynamically
assigned during bootstrap. Static type analysis is severely limited.

**W4 — Four shell startup scripts.** `start.sh`, `starth.sh`, `start.ps1`,
`start_tui.sh` set up the venv and run slightly different variants.
There is no single canonical `uv run` entrypoint.

**W5 — Dependency pinning is incoherent.** `openai==2.0.0` is a pre-release
API; `langgraph>=0.2.0` is a 0.x→1.x open range (breaking changes happened);
`PyYAML==6.0` misses 6.0.2 security patch; `requests==2.31.0` misses 2.32.x
fixes. Meanwhile `langgraph==1.1.2` is installed — a major version jump from
the `>=0.2.0` minimum with breaking API changes.

---

## 2. Tooling

### Current State

49 files in `src/tools/`, auto-discovered via `@tool` decorator. Tier tool
counts: NANO=8, SMALL=20, MEDIUM=35, LARGE=50, FRONTIER=60.

Tool categories: file I/O, git, web, AST, repo search, verification,
memory, subagents, bash (sandboxed), patch, LSP, lint, rollback, batch.

Tool pipeline:
```
@tool decorator → src/tools/_tool.py:ToolDefinition
  → src/tools/_registry.py:ToolRegistry.discover()
  → src/core/orchestration/tool_preflight.py:preflight_check_impl()
  → src/core/orchestration/permission_gateway.py
  → src/core/orchestration/tool_execution_pipeline.py
  → src/core/orchestration/tool_execution_service.py
```

### Strengths

- `@tool(side_effects=[], tags=[])` decorator pattern is clean;
  `ToolDefinition.to_openai_schema()` generates schema from type annotations
  (`src/tools/_tool.py:110–199`)
- `_workspace_guard.py` enforces path containment
- 5-layer security: `bash_security.py` + AST analysis + `sandbox.py` +
  `workspace_guard.py` + `permission_gateway.py`
- `tool_preflight.py` already has case-correction and fuzzy-match for
  misspelled tool names (lines 35–80)
- `_diff_gate.py` and approval flow implement read-before-write pattern
- `PermissionKind` enum mirrors Claw Code's `PermissionKind` from `tools/src/lib.rs`

### Weaknesses

**W6 — JSON Schema generation is opportunistic, not enforced.** `to_openai_schema()`
generates schema from type annotations at registration time but there is no
runtime validation of incoming LLM tool call arguments against that schema before
execution. Malformed calls (wrong types, missing required fields) reach the tool
function and produce confusing Python errors.

**W7 — Tool count not enforced at LLM context level.** Tier limits (8/20/35/50/60)
are defined in `model_tiers.py` but the cap on what reaches the LLM system prompt
is only a render-time concern. No hard `ToolPool` object prevents 50+ definitions
from inflating small-model prompts.

**W8 — Tool discovery requires upfront full import.** All 21 `_BUILTIN_MODULES`
are imported unconditionally in `_registry.py:44–64`. If any module fails to import
(e.g. `pygls` not installed for LSP tools), the entire registry build fails rather
than gracefully skipping unavailable tools.

**W9 — Subagent tool spawns Orchestrator in-process, synchronously.** No worktree
isolation; subagents share parent process memory.

**W10 — Overlapping repo tool files.** Four files with unclear distinctions:
`repo_overview_tool.py`, `repo_summary.py`, `repo_tools.py`,
`repo_analysis_tools.py`. All registered in `_BUILTIN_MODULES`.

---

## 3. Memory Management

### Current State

```
src/core/memory/
├── sqlite_session_store.py     — primary store (848 lines)
├── sqlite_store_queries.py     — 20+ query builder functions
├── sqlite_store_migrations.py  — schema migrations
├── sqlite_store_operations.py  — CRUD operations
├── sqlite_store_search.py      — FTS5 full-text search
├── jsonl_session_store.py      — alternative / sidecar
├── auto_compactor.py           — deterministic compaction (621 lines)
├── distiller.py                — LLM-based summarisation
├── advanced_features.py        — vector search (lancedb, optional)
└── abstract_session_store.py   — interface
```

Session data stored in `.codingAgent/` per-workspace (SQLite + JSONL sidecar).
FTS5 full-text search on message content. Snapshotting supports fork/revert.

### Strengths

- SQLite + FTS5 is the right choice for local-first: zero external deps,
  fast full-text search, reliable ACID transactions
- `auto_compactor.py` is a direct port of Claw Code's `compact.rs` logic
  (same `len(text) // 4 + 1` token heuristic, same `COMPACT_CONTINUATION_PREAMBLE`)
- Dual store (SQLite primary + JSONL sidecar) provides durability
- Snapshot/fork/revert is a genuine differentiator; Claw Code has no equivalent
- `abstract_session_store.py` interface enables store substitution

### Weaknesses

**W11 — `sqlite_session_store.py` decomposition is fragile.** The 848-line
main file imports 20+ builder functions from `sqlite_store_queries.py` at
module level. The distinction between `_queries.py`, `_operations.py`,
`_search.py`, `_migrations.py` is not documented; any contributor adding a new
query must decide arbitrarily which file it belongs to.

**W12 — Vector store is optional but silently relied upon.** `lancedb` in
optional extras but `advanced_features.py` and some memory tools depend on it.
If not installed, failures are silent or occur deep in a tool call rather than
at startup.

**W13 — Compaction is coupled to the perception node lifecycle.**
`perception_compaction.py` triggers compaction inside the LangGraph node.
Compaction cannot be triggered outside a running turn (e.g. on session resume,
or via a user command). The `_force_compact` and `_budget_compaction` fields in
`AgentState` are the only trigger mechanism.

**W14 — No `/compact` slash command.** Claw Code exposes `/compact` as a
first-class user action. CodingAgent compaction can only happen automatically.

**W15 — Session fork/revert not surfaced to the user.** `snapshot_manager.py`
and `rollback_manager.py` are implemented but there is no CLI or TUI affordance
to trigger them. The feature exists only at the code level.

---

## 4. Orchestration

### Current State

The orchestration layer: 73 files in `src/core/orchestration/` directly,
plus 31 files in `graph/nodes/`. Three pipeline variants compiled by
`graph/builder.py` (655 lines):

```
Fast-path:  perception → execution → verification → evaluation → memory_sync
Full:       perception → analysis → planning → plan_validator → execution
            → verification → evaluation → memory_sync
Frontier:   perception → frontier_loop → verification → evaluation → memory_sync
```

`AgentState` TypedDict has ~100 fields across 8 groups (core task, conversation
history, plan & step, plan approval, tool execution, debug & recovery,
verification, analysis & context, delegation, memory, cost, internal, control).

### Strengths

- LangGraph `StateGraph` with typed `AgentState` gives reproducible,
  inspectable state transitions
- Tier-adaptive workflow selection (`workflow_selector.py`) routes local models
  to simpler graphs
- `loop_guards.py` detects stuck/doom loops — essential for small models
- `agent_hooks.py` + `shell_hooks.py` provide extension points
- `cross_session_bus.py` + `event_bus.py` with correlation IDs enable
  distributed tracing
- `plan_mode.py` + `preview_service.py` implement plan-before-execute correctly
- `validate_state()` in `state.py:216` runs at node entry, logs but does not raise
- Bootstrap refactored into 4 focused helpers:
  `_init_infrastructure`, `_init_providers`, `_init_event_subscriptions`,
  `_init_services` (`orchestrator_bootstrap.py`)

### Weaknesses

**W16 — `AgentState` ~100 fields with no lifecycle documentation.** Many
fields are `Optional[X] | None` with no statement of "which node sets this"
or "when is this reset". New contributors cannot understand what is "alive"
at each node without reading every node file.

**W17 — `execution_helpers.py` is 1344 lines** — the largest node-adjacent
file. Contains tool invocation helpers, retry logic, error recovery, and
tool output formatting. Not a node itself but imported by `execution_node.py`
and `frontier_loop_node.py`.

**W18 — `perception_node.py` is 1016 lines** with 10 collaborator modules
(`perception_parsing.py`, `perception_result.py`, `perception_runtime.py`,
`perception_messages.py`, `perception_no_tool.py`, `perception_post_call.py`,
`perception_retrieval.py`, `perception_compaction.py`). The split saves
per-file line count but creates cross-import graphs that are harder to follow
than a well-commented single file.

**W19 — `builder.py` is 655 lines**, mixing graph construction with 30+
re-exported router functions for backwards compatibility. Every test that
patches a router must import from `builder` rather than the authoritative module.

**W20 — Provider bootstrap failure exits the process.** If phase 3 (provider
init) fails (e.g. Ollama not running), the entire process exits. There is no
graceful degradation to "start in offline mode, retry when provider available."

**W21 — `langgraph>=0.2.0` is a dangerously open constraint.** `langgraph 1.1.2`
is installed — a major version jump. The `StateGraph` API, reducer protocol,
and `RunnableConfig` signatures have all changed between 0.2 and 1.x.

**W22 — Subagent delegation blocks the graph turn.** `delegation_node.py` and
`analyst_delegation_node.py` create new `Orchestrator` instances synchronously.
For local models with slow inference, delegation effectively serialises what
could run in parallel.

---

## 5. User Interface & Usability

### Current State

Two UI modes:
1. **TUI** — `tui/` Textual application. `app.py` is 3662 lines.
2. **Headless CLI** — `src/main.py --task "..."`

TUI communicates with core exclusively via `core_bridge.py` (1840 lines)
which subscribes to `EventBus` and runs the agent in a `ThreadPoolExecutor`.

Slash commands handled inline in `app.py`: `/fast`, `/provider`, `/model`,
`/mcp`, `/diff`, `/fork`, `/share`, `/rename`, `/worktree` — 9 commands.

### Strengths

- Textual is the right framework for a local-first TUI
- `core_bridge.py` correctly enforces: TUI never imports `src.core` directly
- `SideBySideDiff` widget, `SubagentProgress`, per-tool icons are ahead of
  most agent UIs
- 20+ event types on EventBus enable fine-grained UI updates
- `mock_engine.py` / `mock_eventbus.py` allow TUI development without a running agent

### Weaknesses

**W23 — `app.py` is 3662 lines.** Contains widget composition, event handlers,
slash command parsing, diff rendering, settings, and theme logic. A 3662-line
Textual `App` class is untestable as a unit and hard to extend safely.

**W24 — No structured slash command system.** Slash commands are parsed inline
with string matching in `app.py`. No command registry, no `/help` generation,
no extensibility contract.

**W25 — TUI and core in separate packages.** `_core_paths_loader.py` exists
specifically to resolve cross-package paths. Tests exercising TUI must handle
two package roots.

**W26 — Four startup scripts.** Each sets up the venv and runs a slightly
different variant. No single `uv run codingagent` command.

**W27 — No streaming output in headless mode.** Running `python -m src.main
--task "..."` gives no progress feedback until the task completes. Local models
can take minutes.

**W28 — No session resume from TUI.** CLI supports `--resume-session` but
TUI has no affordance for selecting and resuming a previous session.

---

## 6. Cross-Cutting Issues

**C1 — Repo artifacts committed.** `MagicMock/` (436-entry mock working dir),
`brand_new.txt`, `dummy_path`, `tmp_debug_main.log`, `tmp_test_lock/`,
`tmp_test_orch/`, `sub/`, `results/` (73 benchmark JSON files) are committed
to the repo root. All should be gitignored and removed.

**C2 — `tmp_debug_main.log` written by `src/main.py`.** Lines 23–30 of
`src/main.py` write to a hardcoded `tmp_debug_main.log` in the repo root
when `CODING_AGENT_DEBUG` is set. Debug logs should go to a temp dir, not
the repo root.

**C3 — No observability export.** The EventBus publishes rich telemetry with
correlation IDs but there is no export to OpenTelemetry, Prometheus, or any
external sink. The `observability/` directory exists but its EventBus
integration is incomplete.

**C4 — 38 ruff lint errors unfixed.** 15× E402 (module-level imports not at top),
rest are F401 (unused imports) and F841 (unused variable). 22 are auto-fixable
with `ruff check --fix`.

**C5 — 59 `TODO`/`FIXME`/`HACK` comments in `src/`.** These are in production
code paths, not tests. Several are architectural (`FIXME: blocks graph turn`
in delegation, `TODO: schema validation` in preflight).

---

## 7. Improvement Plan — Prioritised

Priority: **P0** (blocking/correctness), **P1** (high impact),
**P2** (medium), **P3** (polish). Effort: **S** <2h, **M** <1d, **L** <3d.

---

### P0 — Stability & Correctness

---

#### P0-1: Fix LangGraph version pin
**Problem:** `langgraph>=0.2.0` in `pyproject.toml` but `1.1.2` is installed.
Major version jump with breaking `StateGraph` API changes.

**Fix:**
```toml
# pyproject.toml
"langgraph>=1.1.0,<2.0.0",
"langchain-core>=0.3.0,<0.4.0",
```

**Files:** `pyproject.toml:16–17`

**Acceptance:** `uv lock` resolves without conflict; `pytest tests/unit` still passes.

---

#### P0-2: Pin openai to stable release
**Problem:** `openai==2.0.0` is a pre-release API not on PyPI stable.
`openai 1.x` is the current stable series with the same async streaming API.

**Fix:**
```toml
"openai>=1.50.0,<2.0.0",
```

Also update `PyYAML`, `requests`, `httpx`:
```toml
"requests>=2.32.0,<3.0.0",
"httpx>=0.27.0,<1.0.0",
"PyYAML>=6.0.2,<7.0.0",
```

**Files:** `pyproject.toml:9–15`

**Acceptance:** `uv lock && uv sync` succeeds; all adapters in
`src/core/inference/adapters/` that use `openai` import successfully.

---

#### P0-3: Clean repo root artifacts
**Problem:** `MagicMock/`, `brand_new.txt`, `dummy_path`, `tmp_debug_main.log`,
`tmp_test_lock/`, `tmp_test_orch/`, `sub/`, `results/` (73 JSON files)
committed to repo root.

**Fix — add to `.gitignore`:**
```gitignore
# Test artifacts
MagicMock/
tmp_*/
results/
brand_new.txt
dummy_path
sub/
```

**Fix — remove tracked files:**
```bash
git rm -r --cached MagicMock/ brand_new.txt dummy_path \
    tmp_debug_main.log tmp_test_lock/ tmp_test_orch/ sub/ results/
```

**Files:** `.gitignore` (bottom section), then `git rm` commit.

**Acceptance:** `git status` shows clean working tree; `pytest tests/unit`
does not produce any of these directories.

---

#### P0-4: Move debug log to temp dir
**Problem:** `src/main.py:23–30` writes `tmp_debug_main.log` to the repo root
when `CODING_AGENT_DEBUG` is set. This produces a committed artifact every time
a developer debugs startup.

**Fix:** Replace the hardcoded path with `tempfile.gettempdir()`:
```python
import tempfile
p = os.path.join(tempfile.gettempdir(), "codingagent_debug_main.log")
```

**Files:** `src/main.py:28`

**Acceptance:** Setting `CODING_AGENT_DEBUG=1` writes the log to `/tmp/` (or
OS equivalent), not the repo root.

---

#### P0-5: Fix 22 auto-fixable ruff errors
**Problem:** 38 ruff errors; 22 are auto-fixable (E402, F401, F841).

**Fix:**
```bash
ruff check --fix src/ tui/
```

Then manually review the 16 remaining E402 violations (imports that must stay
at module level due to conditional `sys.path` manipulation).

**Files:** Various — `ruff` identifies them.

**Acceptance:** `ruff check src/ tui/` exits 0 or shows only known exclusions.

---

### P1 — Architecture

---

#### P1-1: Graceful provider bootstrap (offline mode)
**Problem:** If Ollama or any provider is unavailable during phase 3
(`_init_providers` in `orchestrator_bootstrap.py`), the process exits.
There is no "start degraded, retry on demand" path.

**Implementation:**
1. In `orchestrator_provider_init.py:_init_providers()`, wrap provider
   connectivity check in `try/except` and set `orch._provider_degraded = True`
   on failure instead of raising.
2. Publish `ProviderUnavailableEvent` on the EventBus.
3. In TUI `core_bridge.py`, subscribe to `ProviderUnavailableEvent` and display
   a banner: "No provider available — run /model to configure one."
4. When the user runs `/model`, call `orch.reload_provider()` (already exists
   in `orchestrator_config_reload.py`) to attempt reconnection.

**Files:**
- `src/core/orchestration/orchestrator_provider_init.py` — catch failures
- `src/core/orchestration/event_bus.py` — add `ProviderUnavailableEvent`
- `tui/src/ui/core_bridge.py` — subscribe and display banner

**Acceptance:** Starting with `OLLAMA_HOST=http://invalid:11434` launches the
agent, shows the banner, and `/model ollama:qwen3:7b` reconnects successfully.

---

#### P1-2: Single canonical entrypoint
**Problem:** Four startup scripts (`start.sh`, `starth.sh`, `start.ps1`,
`start_tui.sh`) plus two `main.py` files. New users face confusion.

**Implementation:**
1. Add a `[project.scripts]` entry in `pyproject.toml`:
   ```toml
   [project.scripts]
   codingagent = "src.main:main"
   ```
2. Create `scripts/run.sh` that accepts `--tui` / `--headless`:
   ```bash
   #!/usr/bin/env bash
   uv run codingagent "$@"
   ```
3. Deprecate `start.sh`, `starth.sh`, `start_tui.sh`, `start.ps1` with a
   one-line redirect comment in each.
4. Update `README.md` to reference `uv run codingagent`.

**Files:** `pyproject.toml`, `scripts/run.sh` (new), `start*.sh` (comments)

**Acceptance:** `uv run codingagent --help` works; `uv run codingagent --tui`
launches Textual; `uv run codingagent --task "list files"` runs headless.

---

#### P1-3: Merge TUI into main package
**Problem:** `tui/pyproject.toml` separate package creates fragile cross-package
imports requiring `_core_paths_loader.py` and complicating tests.

**Implementation (phased — this is L effort):**
1. **Phase A:** Copy `tui/src/ui/` → `src/ui/`. Update all imports from
   `tui.src.ui.*` → `src.ui.*`.
2. **Phase B:** Remove `_core_paths_loader.py` — replace every usage with
   direct `from src.core.paths import ...`.
3. **Phase C:** Remove `tui/pyproject.toml`. Update `pyproject.toml:58`:
   ```toml
   packages = ["src/"]
   ```
4. **Phase D:** Update `hatch.build.targets.wheel` and CI to remove `tui/`
   from package list.

**Files:**
- `tui/src/ui/` → `src/ui/` (mass move)
- `tui/_core_paths_loader.py` (delete)
- `tui/pyproject.toml` (delete)
- `pyproject.toml:58`
- All `import tui.src.ui.*` references

**Acceptance:** `pytest tests/unit/test_standalone_tui.py` passes; `from src.ui.app import AgentApp` works.

---

#### P1-4: JSON Schema argument validation per tool
**Problem:** `tool_preflight.py:preflight_check_impl()` validates tool name and
path containment but not argument types or required fields. `ToolDefinition.to_openai_schema()`
generates the schema already — it just isn't used for validation.

**Implementation:**
1. In `src/tools/_tool.py:ToolDefinition`, add a `validate_args(args: dict) -> list[str]`
   method that checks required fields and types using the existing `to_openai_schema()` output.
2. In `tool_preflight.py:preflight_check_impl()` after the tool name check (line ~90),
   call `defn.validate_args(args)` and return an error dict if validation fails.

```python
# tool_preflight.py — after successful tool name resolution
defn = tool.get("__tool_meta__")
if defn is not None:
    errs = defn.validate_args(args)
    if errs:
        return {"ok": False, "error": f"Argument validation failed: {'; '.join(errs)}"}
```

**Files:**
- `src/tools/_tool.py` — add `validate_args()` to `ToolDefinition`
- `src/core/orchestration/tool_preflight.py` — call `validate_args` after name check

**Acceptance:** A tool call with a missing required argument returns
`{"ok": False, "error": "Argument validation failed: missing required field 'path'"}`.

---

#### P1-5: Structured slash command registry
**Problem:** 9 slash commands are parsed inline in `app.py` with ad-hoc string matching.
No help generation, no extensibility, no documentation.

**Implementation:**
1. Create `src/ui/commands/registry.py` (or `tui/src/ui/commands/registry.py`
   if P1-3 not done yet):
   ```python
   @dataclass
   class SlashCommand:
       name: str           # e.g. "compact"
       description: str
       handler: Callable   # async def handler(app, args: str) -> None
       source: str = "builtin"  # "builtin" | "plugin"

   class CommandRegistry:
       def register(self, cmd: SlashCommand) -> None: ...
       def dispatch(self, app, text: str) -> bool: ...
       def help_text(self) -> str: ...
   ```
2. Move the 9 `_slash_*` methods out of `app.py` into
   `tui/src/ui/commands/builtin_commands.py`.
3. Register them at `AgentApp.on_mount()`.
4. Wire `/help` to `registry.help_text()`.

**Files:**
- New: `tui/src/ui/commands/registry.py`
- New: `tui/src/ui/commands/builtin_commands.py`
- `tui/src/ui/app.py` — remove `_slash_*` methods, add registry setup

**Acceptance:** `/help` prints all registered commands; adding a new command
requires only one new `SlashCommand` registration, not modifying `app.py`.

---

#### P1-6: Split `app.py` into screen modules
**Problem:** `tui/src/ui/app.py` is 3662 lines. Contains widget composition,
event handlers, slash command parsing, diff rendering, settings, and theme logic.

**Implementation (phased, do after P1-5):**

Phase A — Extract without changing behaviour:
- `screens/chat_screen.py` — main conversation widget composition + `on_*` handlers
  for message submission, tool events, streaming. Target ≤600 lines.
- `screens/session_screen.py` — session browser/list/resume (currently absent).
  Target ≤300 lines.
- `screens/settings_screen.py` — currently `features/settings/screen.py:675` lines;
  consolidate into this.
- `components/status_bar.py` — `_update_status_bar()`, `_update_perm_badge()`,
  `_update_role_display()`.

Phase B — Wire screens to `AgentApp` as proper Textual `Screen` subclasses.
`AgentApp` becomes a navigator with `push_screen()` / `pop_screen()`.

**Files:**
- `tui/src/ui/screens/chat_screen.py` (new)
- `tui/src/ui/screens/session_screen.py` (new)
- `tui/src/ui/components/status_bar.py` (new)
- `tui/src/ui/app.py` — shrink to navigator + mount logic, target ≤400 lines

**Acceptance:** `tui/src/ui/app.py` ≤ 400 lines;
`tests/unit/test_standalone_tui.py` still passes.

---

### P1 — Orchestration

---

#### P1-7: `AgentState` lifecycle documentation
**Problem:** ~100 fields in `state.py` with no documentation of which node
sets them, when they are cleared, or what invariants must hold.

**Implementation:**
1. Add a `# LIFECYCLE` comment block before each section group in `state.py`
   that lists: "Set by: X, Cleared by: Y, Invariants: Z".
2. For each `Optional` field, add a one-line docstring in the TypedDict
   (Python 3.12+ supports inline comments in TypedDicts).
3. Identify the 10 fields that are only written by one node and never read
   by another — candidates for removal or merger into `metadata: dict`.
4. Cap total field count at 85 by merging 15+ rarely-used optional fields
   into a `debug_meta: dict | None` bag that does not bloat the type.

**Files:** `src/core/orchestration/graph/state.py`

**Acceptance:** A new contributor can read `state.py` and understand the
lifecycle of any field without reading node source files.

---

### P2 — Memory & Context

---

#### P2-1: Standalone compaction service
**Problem:** Compaction is coupled to `perception_compaction.py` (inside
LangGraph node lifecycle). Cannot trigger compaction outside a running turn.

**Implementation:**
1. Extract `CompactionService` class from `auto_compactor.py` +
   `perception_compaction.py`:
   ```python
   class CompactionService:
       def should_compact(self, history: list, budget: TokenBudget) -> bool: ...
       async def compact(self, history: list, session_id: str) -> list: ...
   ```
2. Register `CompactionService` in `_init_services` (bootstrap phase 4).
3. Call from:
   - `perception_compaction.py` — replace inline logic with `orch.compaction_svc.compact()`
   - Session resume path in `session_manager.py`
   - TUI `/compact` slash command (after P1-5)
4. Add a background check: if `token_budget.usage_ratio() > 0.80`, schedule
   compaction on next idle turn.

**Files:**
- New: `src/core/memory/compaction_service.py`
- `src/core/orchestration/graph/nodes/perception_compaction.py` — delegate to service
- `src/core/orchestration/orchestrator_services_init.py` — register service
- `src/core/orchestration/session_manager.py` — call on resume

**Acceptance:** `/compact` slash command compacts the current session history
outside of a running turn; `pytest tests/unit/test_compaction_service.py` passes.

---

#### P2-2: Surface session fork/revert in TUI
**Problem:** `snapshot_manager.py` and `rollback_manager.py` are implemented
but have no TUI or CLI affordance.

**Implementation:**
1. Add `SnapshotListEvent` and `RollbackRequestEvent` to `event_bus.py`.
2. In TUI: add a `Ctrl+H` (history) shortcut that opens `screens/session_screen.py`
   listing snapshots from `snapshot_manager.list_snapshots()`.
3. Each snapshot row has "Fork here" and "Revert to" buttons.
4. "Fork here" sends `ForkRequestEvent`; bridge calls `snapshot_manager.fork()`.
5. "Revert to" sends `RollbackRequestEvent`; bridge calls `rollback_manager.rollback()`.

**Files:**
- `src/core/orchestration/event_bus.py` — 2 new event types
- `tui/src/ui/core_bridge.py` — subscribe to new events
- `tui/src/ui/screens/session_screen.py` (created in P1-6)

**Acceptance:** User can press `Ctrl+H`, see snapshot list, and fork from a
past snapshot. The forked session gets a new `session_id`.

---

#### P2-3: Resolve lancedb optionality
**Problem:** `lancedb` in optional extras but `advanced_features.py` and memory
tools depend on it. Silent failures if not installed.

**Decision required:** Choose one of two paths:

**Option A — Make required:**
```toml
"lancedb>=0.6.0",  # move from [optional-dependencies.vector] to [dependencies]
```
Document the 50–200 MB disk requirement in README.

**Option B — Remove lancedb, use FTS5:**
- `advanced_features.py` already wraps FTS5 for full-text search.
- Replace all semantic search calls with FTS5 BM25 ranking (already present).
- Delete `advanced_features.py` vector-specific code.
- Remove `lancedb` from optional deps.

Recommendation: **Option B** — FTS5 is sufficient for local-first,
eliminates an optional native dependency that requires Rust compilation.

**Files:**
- `src/core/memory/advanced_features.py`
- `pyproject.toml` (remove `vector` extra)
- Any file importing `lancedb` directly

---

#### P2-4: Upgrade pinned dependencies
**See P0-2** for the `openai`, `requests`, `httpx`, `PyYAML` fixes.

Additionally:
```toml
"pydantic>=2.7.0,<3.0.0",   # 2.7+ has better TypedDict support
"textual>=0.80.0,<1.0.0",   # 8.1.0 is already current, pin upper bound
```

---

### P2 — Tooling

---

#### P2-5: Consolidate overlapping repo tools
**Problem:** `repo_overview_tool.py`, `repo_summary.py`, `repo_tools.py`,
`repo_analysis_tools.py` serve overlapping purposes.

**Implementation:**
1. Audit all exported `@tool` functions across the 4 files.
2. Merge into two files:
   - `src/tools/repo_read_tools.py` — read-only: overview, summary, file tree,
     symbol search, dependency graph
   - `src/tools/repo_write_tools.py` — write/annotate: repo notes, workspace
     setup, project scaffolding
3. Remove the 4 original files.
4. Update `_BUILTIN_MODULES` in `_registry.py`.
5. Add a `tools/README.md` documenting the tool grouping convention.

**Files:** `src/tools/` (4 files → 2), `src/tools/_registry.py`

**Acceptance:** All existing tool names still resolve; `pytest tests/unit/` passes.

---

#### P2-6: ToolPool cap enforcement at LLM context level
**Problem:** Tier limits defined in `model_tiers.py` but not enforced at the
point where tool schemas are appended to the LLM system prompt.

**Implementation:**
1. Add `ToolPool` class to `src/core/orchestration/tool_registry.py`:
   ```python
   class ToolPool:
       def __init__(self, registry: ToolRegistry, cap: int) -> None: ...
       def select(self, tags: list[str] | None = None) -> list[dict]: ...
       # Returns at most `cap` tool schemas, prioritised by tag relevance
   ```
2. In `registry_builder.py` where tool schemas are assembled for the LLM,
   instantiate `ToolPool(registry, cap=tier.tool_limit)` and call `.select()`.
3. Log a warning if the registry contains more tools than the cap.

**Files:**
- `src/core/orchestration/tool_registry.py` — add `ToolPool`
- `src/core/orchestration/registry_builder.py` — use `ToolPool.select()`

**Acceptance:** With SMALL tier, the LLM receives at most 20 tool schemas
regardless of how many are registered.

---

#### P2-7: Graceful tool discovery (optional imports)
**Problem:** All 21 `_BUILTIN_MODULES` are imported unconditionally. A missing
optional dependency (e.g. `pygls` for LSP) fails the entire registry build.

**Implementation:**
In `_registry.py:build_registry()` and `discover()`, wrap each module import:
```python
for module_path in _BUILTIN_MODULES:
    try:
        mod = importlib.import_module(module_path)
        registry.discover(mod)
    except ImportError as e:
        logger.warning("Skipping tool module %s: %s", module_path, e)
```

Mark LSP tool modules as `_OPTIONAL_MODULES` so the warning message is clearer.

**Files:** `src/tools/_registry.py:build_registry()`

**Acceptance:** Starting the agent without `pygls` installed logs a warning
and continues rather than raising `ImportError`.

---

#### P2-8: Async subagent delegation
**Problem:** `delegation_node.py` and `analyst_delegation_node.py` create new
`Orchestrator` instances synchronously, blocking the graph turn.

**Implementation:**
1. Wrap the delegation call in `asyncio.get_event_loop().run_in_executor()`
   with a dedicated `ThreadPoolExecutor` (separate from the main one).
2. For parallel delegations (when `delegations` list has multiple entries),
   use `asyncio.gather()`.
3. Store a `delegation_task` handle in `AgentState._delegation_tasks`
   so results can be awaited at the next `wait_for_delegations` node.

**Files:**
- `src/core/orchestration/graph/nodes/delegation_node.py`
- `src/core/orchestration/graph/nodes/analyst_delegation_node.py`
- `src/core/orchestration/graph/state.py` — add `_delegation_tasks` field

**Acceptance:** Two parallel delegations complete in wall-clock time ≈ max(T1, T2)
rather than T1 + T2.

---

### P3 — UX Polish & Performance

---

#### P3-1: Streaming output in headless mode
**Problem:** `python -m src.main --task "..."` gives no progress until complete.

**Implementation:**
1. In `src/main.py` headless path, subscribe to EventBus events.
2. On `ToolCallEvent`, print: `[tool] {tool_name} {args_summary}`.
3. On `StreamChunkEvent`, buffer and print chunks with a `\r` overwrite.
4. On `TaskCompleteEvent`, print the final response.

Optional: support `--output json` for structured machine-readable streaming.

**Files:** `src/main.py`, `src/core/orchestration/event_bus.py`

**Acceptance:** `python -m src.main --task "list files"` prints tool calls
as they happen, not just the final answer.

---

#### P3-2: Session resume from TUI
**Problem:** TUI has no affordance for `--resume-session`. History is shown
but forked/past sessions are inaccessible.

**Implementation:**
1. `Ctrl+R` opens `screens/session_screen.py` (from P1-6) showing a list
   of past sessions with summary + date.
2. Selecting a session calls `orch.resume_session(session_id)`.
3. Session screen also appears as the default view on launch if a previous
   session exists (with "New session" option prominent).

**Files:**
- `tui/src/ui/app.py` — add `Ctrl+R` binding
- `tui/src/ui/screens/session_screen.py` — session list screen
- `tui/src/ui/core_bridge.py` — wire resume call

**Acceptance:** User can resume a previous session from TUI without restarting
with `--resume-session` flag.

---

#### P3-3: `/model` command for live model switching
**Problem:** Switching providers/models requires restarting the agent.

**Implementation:**
1. Add to slash command registry (after P1-5): `/model <provider>:<model_name>`.
2. Handler calls `orch.reload_provider(provider, model_name)` which is
   implemented in `orchestrator_config_reload.py` but not yet wired to a command.
3. Display confirmation: "Switched to ollama:qwen3:14b".

**Files:**
- `tui/src/ui/commands/builtin_commands.py` — add `/model` handler
- `src/core/orchestration/orchestrator_config_reload.py` — expose `reload_provider()`

**Acceptance:** `/model ollama:llama3:8b` during a session switches the
inference adapter for the next turn without restarting.

---

#### P3-4: Reduce `execution_helpers.py` surface area
**Problem:** `execution_helpers.py` is 1344 lines — the largest node-adjacent file.

**Implementation:**
1. Split into:
   - `execution_retry.py` — retry logic, backoff, error classification
   - `execution_formatting.py` — tool output formatting, truncation
   - `execution_helpers.py` — keep only the primary tool invocation path (~300 lines)
2. Re-export from `execution_helpers.py` for backwards compatibility during transition.

**Files:**
- `src/core/orchestration/graph/nodes/execution_helpers.py` (split)
- `src/core/orchestration/graph/nodes/execution_retry.py` (new)
- `src/core/orchestration/graph/nodes/execution_formatting.py` (new)

**Acceptance:** `execution_helpers.py` ≤ 400 lines; all imports from it
still resolve.

---

#### P3-5: OpenTelemetry export
**Problem:** EventBus publishes rich telemetry with correlation IDs but there
is no export.

**Implementation:**
1. Add `opentelemetry-sdk` to `[project.optional-dependencies.observability]`.
2. In `src/core/observability/` (already exists), create `otel_exporter.py`:
   ```python
   class OtelExporter:
       def __init__(self, endpoint: str) -> None: ...
       def subscribe(self, event_bus: EventBus) -> None: ...
       # Maps CodingAgent events → OTEL spans
   ```
3. If `OTEL_EXPORTER_OTLP_ENDPOINT` env var is set, instantiate and subscribe
   at bootstrap.

**Files:**
- `src/core/observability/otel_exporter.py` (new)
- `src/core/orchestration/orchestrator_services_init.py` — conditional init
- `pyproject.toml` — add `observability` extra

**Acceptance:** With Jaeger running locally and `OTEL_EXPORTER_OTLP_ENDPOINT`
set, multi-turn agent sessions appear as traces with per-tool spans.

---

## 8. Summary Table

| ID | Area | Issue | Priority | Effort |
|----|------|-------|----------|--------|
| P0-1 | Deps | LangGraph open range | P0 | S |
| P0-2 | Deps | openai pre-release + stale pins | P0 | S |
| P0-3 | Repo | Committed artifacts | P0 | S |
| P0-4 | Repo | Debug log to repo root | P0 | S |
| P0-5 | Quality | 22 auto-fixable ruff errors | P0 | S |
| P1-1 | Orch | No graceful provider fallback | P1 | M |
| P1-2 | UX | Four start scripts, no uv entrypoint | P1 | S |
| P1-3 | Arch | Split TUI package | P1 | L |
| P1-4 | Tools | No argument schema validation | P1 | M |
| P1-5 | UI | No slash command registry | P1 | M |
| P1-6 | UI | app.py 3662 lines | P1 | L |
| P1-7 | Orch | AgentState undocumented lifecycle | P1 | M |
| P2-1 | Memory | Compaction coupled to perception node | P2 | M |
| P2-2 | Memory | Fork/revert not surfaced | P2 | M |
| P2-3 | Memory | lancedb optionality unclear | P2 | S |
| P2-4 | Deps | Loose pydantic/textual bounds | P2 | S |
| P2-5 | Tools | 4 overlapping repo tool files | P2 | S |
| P2-6 | Tools | No ToolPool cap enforcement | P2 | M |
| P2-7 | Tools | Tool discovery fails on missing optional dep | P2 | S |
| P2-8 | Orch | Sync subagent delegation | P2 | L |
| P3-1 | UX | No headless streaming | P3 | M |
| P3-2 | UX | No TUI session resume | P3 | M |
| P3-3 | UX | No live model switching | P3 | M |
| P3-4 | Orch | execution_helpers.py 1344 lines | P3 | M |
| P3-5 | Obs | No OTel export | P3 | M |

---

## 9. Recommended Sequencing

Execute in this order to minimise breakage risk:

**Sprint 1 — Foundation (all P0):**
P0-1 → P0-2 → P0-3 → P0-4 → P0-5

**Sprint 2 — Quick wins (small P1/P2):**
P1-2 → P1-4 → P2-3 → P2-4 → P2-5 → P2-7

**Sprint 3 — Slash commands (enables later work):**
P1-5 → P1-7 → P2-1 → P3-3

**Sprint 4 — TUI restructure:**
P1-6 → P2-2 → P3-2

**Sprint 5 — Architecture (high effort):**
P1-1 → P1-3 → P2-6 → P2-8

**Sprint 6 — Polish:**
P3-1 → P3-4 → P3-5

---

## 10. What CodingAgent Does Better Than Claw Code

These are genuine strengths to preserve and build on:

1. **Snapshot/fork/revert** — no equivalent in Claw Code; a real
   local-first differentiator (once surfaced in the UI)
2. **Model tier adaptation** — NANO→FRONTIER tier system with adaptive
   tool counts is more sophisticated than Claw Code's `simple_mode` boolean
3. **5-layer Python security model** — `bash_security.py` + AST analysis +
   `sandbox.py` + `workspace_guard.py` + `permission_gateway.py`. CodingAgent's
   AST-level Python analysis catches more patterns before execution reaches the shell
4. **Hardware-aware inference** — `hardware_capability_profile.py` +
   `kv_cache_governor.py` for VRAM monitoring is unique to local-first;
   Claw Code has no equivalent
5. **FTS5 + SQLite for semantic search** — no external vector DB required;
   works fully offline. Claw Code uses flat JSON session files
6. **Multi-provider from day one** — 10 adapters including local (Ollama,
   LM Studio) and frontier (Anthropic, OpenAI, GitHub Copilot, Groq, LiteLLM).
   Claw Code's `ProviderClient` supports Anthropic and OpenAI-compat natively
   but has no local model routing
7. **Plan mode + preview** — `plan_mode.py` + `preview_service.py` let users
   review and approve changes before execution; Claw Code defers to the
   permission prompt system for this
8. **`validate_state()`** — runs at every node entry, catches state corruption
   early. Claw Code has no equivalent runtime state validation

---

## 11. Source-Verified Corrections vs Claw Code Documentation

Reading the actual source revealed inaccuracies in the claw-code.codes website:

| Claim (website) | Reality (source) |
|-----------------|------------------|
| 6 Rust crates | 9 crates: +`mock-anthropic-service`, `plugins`, `telemetry` |
| 3 permission modes (Allow/Deny/Prompt) | 5 modes: `ReadOnly`, `WorkspaceWrite`, `DangerFullAccess`, `Prompt`, `Allow` |
| 2 token counters (input/output) | 4 counters: +`cache_creation_input_tokens`, `cache_read_input_tokens` |
| 16 Rust runtime modules | 46 source files in `runtime/src/` |
| ToolPool caps at 15 tools | Cap is in `as_markdown()` render only, not in tool list passed to LLM |
| Sonnet: $15/$75 per million | Default pricing struct uses those numbers but per-model overrides exist |
| Auto-compact after 12 turns | Rust layer triggers on 100k *input tokens*, not turn count |
| `perception_node` is the only compaction trigger | `_force_compact` and `_budget_compaction` flags also trigger it |
