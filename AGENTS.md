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
│   ├── mcp/                 # MCP client
│   └── evaluation/          # scenario evaluator, pass@k, CLI runner, regression baselines, SWE-bench, model comparison
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
└── unit/                    # 4,826 tests (pytest; 355 files; +3 fast-path integration)
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
- **Per-symbol import safety** — Instead of one monolithic `from ... import (...)` block (where any single ImportError destroys the entire mapping), use an `_EVENT_IMPORT_PATHS` dict with dotted module paths and import each class lazily via `importlib.import_module()` on first use. Cache imported classes under a thread lock. A single failed import degrades only that event type.
- **MessageBus async architecture** — `sync_queue` (thread-safe for `publish()`) → `_bridge` task (transfers to event loop) → `_dispatch` task (runs handlers via `run_in_executor` so blocking handlers don't block the event loop)
- **Sequenced dispatch** — Critical lifecycle events (tool/step/delegation) are delivered in FIFO order within their category via per-category `asyncio.Queue` consumer tasks. Categories are independent (tool does not block delegation).
- **Owner-scoped cancellation** — `FileLockManager.cancel(owner=)` / `reset_cancel(owner=)` prevents cross-coroutine cancel corruption. When an owner is set, `reset_cancel()` with no owner or non-matching owner is a no-op. Guard condition: `if self._cancel_owner is not None: if owner is None or owner != self._cancel_owner: return`.
- **File lock auto-release** — `_auto_release_after_timeout` guard task releases write locks after 30s timeout. `reset_instance()` on orchestrator re-init provides clean lock state.
- **Contract tests** — Each stabilization phase adds dedicated contract tests (`test_fast_path_locks.py`, `test_fast_path_event_ordering.py`) that verify the exact guarantees. These run alongside existing unit tests.
- **Optional import type-ignoring** — Use `# type: ignore[import-not-found]` for optional dependencies inside try/except blocks (pyright `reportMissingImports`). Use `Optional[Callable[..., Any]]` annotations + `assert is not None` guards for conditional-import-then-call patterns (pyright `reportOptionalCall`). Always add the `Callable` and `Any` to the typing import line rather than adding inline imports.
- **`state.get("session_id", "")`** — Always provide a default when passing state dict lookups to event constructors that expect `str`. Prevents `Any | None` type mismatch.
- **`SessionState` field parity** — If `update_session_state()` parameter types in `agent_session_manager.py` don't match `SessionState.__init__` field types, update the method signature (the docstring comment `Consolidates _state_lock + _sessions_lock` is stale if the field was renamed).
- **Event field types should match callers** — When callers pass `str | None` or `int | None` to event constructors, the event field type should be `Optional[str]` or `Optional[float]` (e.g., `ProviderSelectionChanged.model`, `PlanRequested.blocked_tool`, `StepFinish.elapsed_ms`).
- **`**dict` splat in event constructors is fragile** — Use explicit keyword args instead (e.g., `LogEntry(level=..., message=...)`) or add `# type: ignore` when dict values may not match event field types.
- **JsonlSessionStore needs explicit implementations** — When adding methods used by `SessionStoreProtocol` consumers, implement them in `JsonlSessionStore` rather than relying on protocol-only definitions.
- **`Any | None` return types** — When a function uses `last_response = None` with `return last_response` after a loop, annotate the return type as `Optional[Response]` (not `Response`).
- **`isinstance` + index access** — `ch[0]` after `isinstance(ch, (list, tuple))` narrows to `tuple[()]` in pyright; use `# type: ignore[index]` or restructure the check.
- **Optional return from retry functions** — Always guard `response is None` before accessing `.json()`, `.status_code`, etc. on the return of retry wrappers.
- **Fast-path active nodes** — perception, analysis, planning, plan_validator, wait_for_user, execution, step_controller, verification, evaluation, and memory_sync. The fast-path 10-node graph is test/legacy only (the legacy `_USE_FULL_GRAPH` flag and `compile_agent_graph()` are NOT the production path). Production uses tier graphs: `_compile_frontier_graph` (capable tier) and `_compile_lite_graph` (lite/small). The frontier graph has analyst_delegation, debug, delegation, AND replan active; replan fires when a tool result sets `requires_split` (patch-size guard in `frontier_loop_node` → `route_frontier_loop_exit` returns `"replan"` → replan node → `route_replan_frontier` re-enters `frontier_loop` on success or bails to `memory_sync` on the recovery cap). Approval-required routes must never bypass wait_for_user.
- **Bridge handler key strategy** — Defensive dual-key lookups: `payload.get("snake_case") or payload.get("camelCase", default)`. Typed event field names are preferred when they differ semantically (e.g., `reason` vs `error_type`).
- **Test event names must match typed events** — After Phase 5 migration, old string event names changed. Verify `subscribe()` names match the `_EVENT_IMPORT_PATHS` keys in `event_bus.py` (e.g., `"provider.model.missing"` not `"provider.config.missing"`).

- **MessageBus _bridge infinite error loop** — When `run_in_executor` raises "cannot schedule new futures after shutdown" during Python process exit, `_bridge`'s generic `except Exception: continue` creates a tight error loop that prevents clean shutdown and causes pytest hangs under `pytest-timeout`. Fix: detect the specific shutdown error in `_bridge` and `_seq_consumer`, set `_shutdown_flag`, and `break`.
- **`_MockTypedBus` in tests** — Must handle both callable handlers and `.handle()`-style adapter objects (used by `_DictBridgeAdapter` in bridge subscriptions). Check `if hasattr(h, "handle"): h.handle(event)`.
- **Dashboard tests with old EventBus** — Tests that rely on old-style `publish("string.name", dict)` cannot be used with `_MockTypedBus` after Phase 5 removed old subscriptions from the bridge. Convert to typed `publish(TypedEvent(...))` on the typed bus, or use `bridge._bus.publish("string.name", dict)` if the bridge still has a MockEventBus instance.
- **`_seed_context_window_from_config` posts `UpdateSettings`** — Called during bridge `setup_subscriptions()`. This posts a synthetic `UpdateSettings` event via `app.post_message`. Tests checking `mock_app.post_message.call_args` after setup need to account for this initial message.
- **Fail-open → fail-closed restructure needs an explicit ALLOW return** — When converting a trailing `return PermissionResult(allowed=True)` (that previously served both the normal ALLOW path and the exception path via `except: pass`) into a fail-closed `except` branch, you MUST add a separate `return PermissionResult(allowed=True)` at the end of the `try` block. Otherwise the ALLOW fall-through yields an implicit `None` (mypy "Missing return statement" + broken authorization). (See `_gate2b_policy_rules` in `permission_gateway.py`.)
- **Episodic memory persistence (Mem-4)** — VectorStore persists to `agent_context_path(workdir)/vectorstore/memories.jsonl`. JSONL append with atomic tmp-file `replace()`; `_MEMORY_LOCK` held across the dedup-read + append so concurrent writers can't double-append; rotation keeps newest `_MEMORY_MAX_RECORDS` (200); corrupt/missing file yields `[]`; dedup key is a text-derived `id`. Returned search records strip the `vector` field. (See `vector_store.py`.)
- **Async VectorStore model loading (PHASE-4 4.8)** — The sentence-transformers load is single-flight (`_ST_MODEL_LOCK`), prefetched in a daemon thread from `VectorStore.__init__` (`_preload_st_model`), and never blocks hot paths: `encode`/`search`/`search_memories` use the non-blocking `_get_st_model_ready()` and degrade to the SHA-256 stub/token fallback during warm-up. Event-loop callers use `get_st_model_async()` / the `a*` wrappers (`asearch`, `aadd_memory`, `asearch_memories`, `aindex_code`), which run the sync bodies via `asyncio.to_thread`. The blocking `_get_st_model()` is retained for callers that need guaranteed semantic vectors. (See `vector_store.py`.)
- **Tool aliases MUST be normalized before any permission/policy check** — `TOOL_ALIASES` (run→bash, ls→list_files, write→write_file, ...) exist so a deny/approval rule keyed on the canonical name can't be evaded via an alias. Use the centralized `resolve_tool_alias(name)` helper (`tools_config.py`). The single production normalization point is the top of `execute_tool_impl` in `tool_execution_pipeline.py` (before the dry-run + all `_check_*` guards), because `PermissionGateway` and `ToolExecutionService.pre_execute` are dead code in production. (See SEC-2 / HS-4.)
- **Tool classifier sets live in one place** — `src/tools/constants.py` is the canonical home for the shared tool-classification sets (`WRITE_TOOLS_REQUIRING_READ`, `MODIFYING_TOOLS`, `DRY_RUN_BLOCKED_TOOLS`, `PERMISSION_REQUIRED_TOOLS`, `PERM_ORDER`, `WORKDIR_SAFE_TOOLS`, `FILE_TOOLS`) and the `TOOL_ALIASES` map. `tool_constants.py`, `loop_guards.py`, `permission_gateway.py`, and `tools_config.py` re-export them (object identity is asserted by `TestCanonicalCentralization`); add a new tool to a set there, never in a private copy, or the membership will drift. `MODIFYING_TOOLS` is derived from `WRITE_TOOLS_REQUIRING_READ | {"multiedit"}`. (See PHASE-4 4.6.)
- **All `@tool` decorators must pass an explicit `permission_kind=`** — the semantic category (WRITE_FILE / EXECUTE_BASH / GIT_READ / GIT_WRITE / LSP_READ / LSP_WRITE / PLAN / READ_FILE / NONE) must be stated at definition time, never inherited from `side_effects`. `ToolDefinition.permission_kind_explicit` records that it was passed; `test_tool_permission_kind_explicit` fails the exhaustive registry scan if a new builtin relies on the implied default. (See PHASE-4 4.7.)
- **delete_file must never be auto-approved** — Deletion is irreversible; it must NOT be in `_WORKDIR_SAFE_TOOLS` and `_check_workdir_confinement` must return True (requires approval) for `delete_file` regardless of path confinement. Keep the `permission_gateway.py`/`_is_workdir_confined` consistent even though the gateway is not the production path. (See HS-5 / SEC-8.)
- **Sandbox must not fail open in autonomous mode** — `sandbox.py`'s fallback to plain `subprocess` (when no bwrap/sandbox-exec is available or enforcing) must be refused when `_enforcement_required()` is true. `_enforcement_required()` is True when `SANDBOX_REQUIRE_ENFORCEMENT` is set OR `is_autonomous()`. Interactive mode still falls back to unsandboxed with a warning. (See CF-2 / 2.1.)
- **Schema violations must be observable** — `_default_publish_violation` in `state_schemas.py` must increment the thread-safe `metrics` singleton (`graph.node_validation_failed` total + per-node + per-reason) so fail-open validation violations can be aggregated. Guard with try/except (metrics must never break node execution). (See CF-5 / 2.3.)
- **Langgraph node wrappers must annotate `config` as `Optional[RunnableConfig]`, not `RunnableConfig | None`** — `langgraph._internal._runnable.RunnableCallable` only injects the `config` arg when the node's `config` annotation matches `typing.Optional[RunnableConfig]` (or `RunnableConfig`). The PEP 604 union `RunnableConfig | None` is `types.UnionType` and is NOT recognized, so langgraph calls the wrapper with `(state,)` only and raises `TypeError: missing 1 required positional argument: 'config'`. Give the param a default `= None` too. (Applies to `wrap_node` in `state_schemas.py`; affects ALL `_validated(...)` graph nodes.)
- **Routing thresholds live in `routing_constants.py`** — `src/core/orchestration/graph/routing_constants.py` is the single source of truth for routing caps/limits (replan, step-retry, no-plan, debug, recovery, tool-budget, planning). Routing modules import these instead of inlining magic numbers. `execution_routing.py`/`planning_routing.py` keep their legacy private names (`_RECOVERY_CAPS`, `_DEFAULT_MAX_TOOL_CALLS`, `_LOOP_GUARD_ROUNDS`, `_MAX_ROUNDS_PLANNING`) as re-exports so `builder.py` aliases + tests stay valid. (See audit 2.7.)
- **Single canonical tool-output pruner** — `tool_output_truncation.prune_tool_outputs` is the only token-based history pruner; `perception_node` imports it (use `return_pruned_count=True` for `(history, count)`; default returns list-only for `frontier_loop_node`). Old messages over `_PRUNE_PROTECT_TOKENS` are replaced with `_PRUNED_TOOL_PLACEHOLDER`; the `_PRUNE_PROTECT_RECENT` tail and `metadata.preserve` messages are never pruned. Do not add a second clone. (See audit 2.4.)
- **Shared token estimator** — `estimate_text_tokens(text)` (in both `tool_output_truncation.py` and `token_truncation.py`) wraps `tokenizer.count_tokens` (tiktoken/HF → len//3.5 fallback) with a `len // 4` last-resort guard. `token_truncation.truncate_*` now default to this estimator when no `token_estimator` is passed, so pruning and truncation agree on token counts. (See audit 2.4.)
- **Graph-state checkpointing (audit 3.4)** — Production tier graphs (`frontier`/`lite`) compile with `checkpointer=_graph_checkpointer()` (shared `JsonlCheckpointSaver` singleton); the legacy full/fast-path graphs stay saver-less. langgraph 1.1.10 only persists `{channel_values, channel_versions, id, ts, updated_channels, v, versions_seen}` — **there is no stored `next`/`versions`**, and `app.invoke(None, thread_cfg)` does NOT re-run pending nodes from a plain on-disk checkpoint (node resume needs in-memory task bookkeeping the loop never serializes). Crash recovery therefore lives at **round granularity**: `inference_loop` writes a JSON-safe state snapshot after each round (`checkpoint_{thread}.state.jsonl`, sanitizer skips non-JSON values, never `LIVE_CHANNELS`) and rehydrates `initial_state` on same-thread re-entry (`--continue` seeds `orch._current_task_id` = original session id in `src/main.py`, minting a `uuid4()` otherwise). Completed tasks purge the thread's files (snapshot + `.jsonl` + `.writes.jsonl`); cancellation/loop-limit paths keep them. Toggle: `CODINGAGENT_GRAPH_CHECKPOINTING` env 0/1 → config `graph_checkpointing` → OFF under pytest.
- **Evaluation framework (audit 3.2)** — `python -m src.core.evaluation` is the evaluation CLI (`list`/`run`/`baseline-save`); `--agent module:callable` selects a factory returning a fresh agent per attempt; `--samples n` gives pass@k; `--baseline <json>` exits 1 on golden-regression (CI gate). Baseline units are **best-of-n** scenario status (pass if any attempt passed) to avoid false positives from variance. Report/baseline serialization lives in `src/core/evaluation/regression.py`; do not hand-roll a second baseline format.
- **SWE-bench harness (audit 3.3)** — `src/core/evaluation/swebench.py` loads SWE-bench style instances (JSON/JSONL/dir) and grades a run by extracting the agent patch (`git add -N` + `git diff` so new files are captured), applying `test_patch`, then running FAIL_TO_PASS/PASS_TO_PASS (or an explicit `test_command`) with `-x`. `repo` may be a local path (offline fixtures/tests), `file://`, or `org/repo` GitHub spec (network at grade time). `python -m src.core.evaluation swebench --source … --agent … [--baseline …]` reuses the same regression gate; live runs use the local `python -m pytest` runner, so real-world installs may need `pip install` env shims per instance.
- **Model comparison (audit 4.5)** — `python -m src.core.evaluation compare --model name=module:callable …` runs the same suite across multiple agent factories (one per provider/model) and emits pass rates, per-scenario winners, ranking, and tie counts. `run_models` gives **each model its own isolated run dir** (`workdir/model_runs/<label>`) so one model's files can't leak into another's grading. `compare_models` accepts both `ScenarioResult`s and SWE-bench style dicts. Reports via `save_comparison`; do not hand-roll a second comparison format.

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

## Prevention Patterns

### Metaclass incompatibility with textual.app.App

- `textual.app.App` uses `_MessagePumpMeta` metaclass
- If a mixin inherits from `typing.Protocol`, it gets `_ProtocolMeta` metaclass
- These metaclasses are incompatible; `AgentApp` cannot inherit from both
- **Fix**: protocol-like mixin bases must NOT inherit from `typing.Protocol`. Use a plain class instead. The structural subtyping of `Protocol` is not needed for mixins since they are explicitly inherited, not structurally matched.
- **New pattern**: When creating a base class for TUI mixins, use `class MyProtocol:` (plain class), not `class MyProtocol(Protocol):`
- The `_check_tui_imports()` function in `src/main.py` detects this at startup

### Startup errors must always be visible

- `_dbg()` requires `CODINGAGENT_DEBUG` env var — hidden from normal users
- Use `_err()` in `src/main.py` for startup failures that always prints to stderr
- **CLI**: `--debug` flag sets `CODINGAGENT_DEBUG=1` automatically

### When to scope mixin metaclass usage

| Scope | Base class | Metaclass | Compatible with |
|-------|-----------|-----------|-----------------|
| App mixins (`StatusBarMixin`, etc.) | `AgentAppProtocol` (plain class) | `type` | `App._MessagePumpMeta` |
| Bridge mixins (`BridgeProviderMixin`, etc.) | `AgentBridgeProtocol` (Protocol) | `_ProtocolMeta` | Only other Protocol classes |

