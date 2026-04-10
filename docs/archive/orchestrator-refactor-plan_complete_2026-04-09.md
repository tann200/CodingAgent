# Orchestrator Refactoring Plan

**File:** `src/core/orchestration/orchestrator.py`
**Current size:** 4,162 lines
**Target:** No file exceeds ~400 lines; each module owns exactly one responsibility.

---

## Problem Statement

`orchestrator.py` is a God Object. It contains six distinct responsibilities that have grown together over time:

1. **Tool constants & helpers** — write-guard sets, audit logging, git diff helpers
2. **Tool registration** — `ToolRegistry` class + `example_registry()` factory (~630 lines)
3. **Subsystem bootstrap** — `Orchestrator.__init__` wires 20+ collaborators (~500 lines)
4. **Tool execution pipeline** — `execute_tool` enforces 15+ sequential guards (~924 lines)
5. **Task lifecycle** — `start_new_task`, `restore_continue_state`, plan approval (~230 lines)
6. **Inference loop** — `run_agent_once` drives LangGraph, handles cancellation, fallback LLM (~700 lines)

Many of these responsibilities already have *partial* extractions in the `orchestration/` sub-package (e.g. `tool_execution_service.py`, `permission_gateway.py`, `session_manager.py`), but the work was never completed — the logic was duplicated rather than moved.

---

## Guiding Principles

1. **The `Orchestrator` class stays** — callers (`main.py`, TUI bridge, tests) all construct `Orchestrator(...)`. We refactor its internals, not its public API.
2. **Extract, don't duplicate** — if a module already exists for a responsibility (e.g. `tool_execution_service.py`), move the remaining logic there; delete the inline copy.
3. **No new circular imports** — every new module must sit *below* `orchestrator.py` in the dependency graph. `orchestrator.py` imports from them; they never import back from `orchestrator.py`.
4. **Backward-compatible re-exports** — anything currently imported from `orchestrator` by external code (`WRITE_TOOLS_REQUIRING_READ`, `PERMISSION_REQUIRED_TOOLS`, `ToolRegistry`, `Orchestrator`) stays importable from `orchestrator.py` via explicit re-exports.
5. **Tests must stay green** — run `pytest tests/unit/` after each phase. No phase lands unless all 3088 tests pass.
6. **One phase = one PR** — each phase is independently reviewable and reversible.

---

## Current vs Target File Sizes

| Module (current) | Lines | Module (target) | Est. Lines |
|---|---|---|---|
| `orchestrator.py` | 4,162 | `orchestrator.py` (shell + re-exports) | ~250 |
| *(inline)* | — | `tool_constants.py` (new) | ~80 |
| `ToolRegistry` + `example_registry` | ~630 | `tool_registry_builder.py` (extend existing `tool_registry.py`) | ~200 |
| `Orchestrator.__init__` | ~500 | `orchestrator_bootstrap.py` (new) | ~350 |
| `execute_tool` | ~924 | `tool_execution_pipeline.py` (extend existing `tool_execution_service.py`) | ~380 |
| `start_new_task` / plan / lifecycle | ~230 | `task_lifecycle.py` (new) | ~200 |
| `run_agent_once` | ~700 | `inference_loop.py` (new) | ~350 |
| helpers: `_write_permission_audit`, `_is_git_repo`, etc. | ~200 | moved to existing `permission_gateway.py` / `snapshot_manager.py` | n/a |

---

## Phase-by-Phase Plan

---

### Phase A — Extract `tool_constants.py` *(~1 day, very low risk)*

**What moves:**

- `WRITE_TOOLS_REQUIRING_READ` (line 41)
- `DRY_RUN_BLOCKED_TOOLS` (line 57)
- `PERMISSION_REQUIRED_TOOLS` (line 68)
- `_write_permission_audit()` function (line 102)

**Why:** These three sets are already imported by `permission_gateway.py` from `orchestrator.py`, creating the only real circular import risk in the codebase. Moving them to a leaf module (`tool_constants.py`) that nothing else depends on eliminates the cycle permanently.

**New file:** `src/core/orchestration/tool_constants.py`

```
tool_constants.py
  WRITE_TOOLS_REQUIRING_READ: frozenset
  DRY_RUN_BLOCKED_TOOLS: frozenset
  PERMISSION_REQUIRED_TOOLS: frozenset
  _write_permission_audit(working_dir, tool_name, args, decision, reason) -> None
```

**In `orchestrator.py`:** replace the inline definitions with:
```python
from src.core.orchestration.tool_constants import (
    WRITE_TOOLS_REQUIRING_READ,
    DRY_RUN_BLOCKED_TOOLS,
    PERMISSION_REQUIRED_TOOLS,
    _write_permission_audit,
)
```

**In `permission_gateway.py`:** update the existing `try/except` import to point at `tool_constants` instead of `orchestrator`.

**Tests:** Add `tests/unit/test_tool_constants.py` — import the sets, verify membership of key tool names.

---

### Phase B — Complete `tool_registry.py` *(~1 day, low risk)*

**What moves:**

- The `ToolRegistry` class (lines 322–432) → merge into existing `src/core/orchestration/tool_registry.py`
- The `example_registry()` factory function (lines 435–948) → new `src/core/orchestration/registry_builder.py`

**Why:** `tool_registry.py` already exists and holds `get_tool_timeout()`. `ToolRegistry` belongs there. `example_registry()` is a 500-line factory that knows about every tool sub-package; it deserves its own file so new tool packages can be added without touching `orchestrator.py`.

**`src/core/orchestration/tool_registry.py` additions:**
```
ToolRegistry
  __init__()
  register(name, fn, schema, description)
  get(name) -> Optional[dict]
  list() -> List[str]
  filter_by_names(names) -> ToolRegistry
  get_openai_functions() -> List[dict]
```

**New file:** `src/core/orchestration/registry_builder.py`
```
registry_builder.py
  example_registry() -> ToolRegistry   # moved verbatim; just changes the import path
```

**In `orchestrator.py`:** replace inline `ToolRegistry` class and `example_registry` function with:
```python
from src.core.orchestration.tool_registry import ToolRegistry
from src.core.orchestration.registry_builder import example_registry
```

**Backward-compatible re-export** in `orchestrator.py`:
```python
# Re-export for callers that do: from src.core.orchestration.orchestrator import ToolRegistry
__all__ = [..., "ToolRegistry", "example_registry"]
```

**Tests:** Existing tests that construct `ToolRegistry` directly should pass unchanged. Add `tests/unit/test_registry_builder.py` — verify `example_registry()` returns a `ToolRegistry` with at least the well-known tools.

---

### Phase C — Extract `tool_execution_pipeline.py` *(~3 days, highest complexity)*

**What moves:**

All of `execute_tool()` (lines 1800–2723) into `src/core/orchestration/tool_execution_pipeline.py`.

This is the largest and riskiest extraction. `execute_tool` references ~25 instance variables on `Orchestrator`. The solution is a **context struct** passed by reference:

**New file:** `src/core/orchestration/tool_execution_pipeline.py`

```python
@dataclass
class ExecutionContext:
    """Snapshot of the Orchestrator state needed by execute_tool."""
    working_dir: Path
    dry_run: bool
    dry_run_log: list
    session_read_files: set          # reference (mutated in place)
    session_modified_files: set      # reference (mutated in place)
    rollback_manager: RollbackManager
    current_snapshot_id_ref: list    # [str|None] — mutable single-element wrapper
    step_snapshot_id_ref: list       # [str|None]
    plan_mode: PlanMode
    explore_mode: bool
    current_role: Optional[str]
    event_bus: EventBus
    cost_tracker: SessionCostTracker
    session_store: SessionStore
    tool_executor: ThreadPoolExecutor
    current_task_id: str
    affected_files: list             # reference
    permission_gate: Optional[AsyncGate]
    permission_granted_ref: list     # [bool]
    tool_hook_runner: ToolHookRunner


def execute_tool(ctx: ExecutionContext, tool_registry: ToolRegistry, tool_call: dict) -> dict:
    """Full permissioned tool execution pipeline."""
    ...  # verbatim body of Orchestrator.execute_tool, variables renamed to ctx.*
```

**In `Orchestrator`:** `execute_tool` becomes a thin wrapper that builds `ExecutionContext` from `self` and delegates:

```python
def execute_tool(self, tool_call: dict) -> dict:
    ctx = ExecutionContext(
        working_dir=self.working_dir,
        dry_run=self._dry_run,
        ...
    )
    return _execute_tool(ctx, self.tool_registry, tool_call)
```

**Why `ExecutionContext` instead of passing `self`:** It makes all dependencies explicit, breaks the hidden coupling to `Orchestrator`, and allows the pipeline to be unit-tested without constructing a full `Orchestrator`. The existing `ToolExecutionService` and `permission_gateway.py` were steps in this direction — this completes the extraction.

**Important:** `_current_snapshot_id` and `_step_snapshot_id` are mutated during execution. Using single-element list wrappers (`[value]`) lets the function update them while keeping them out of `Orchestrator.__dict__` long-term (in a later cleanup phase they become `ExecutionContext` fields stored in `Orchestrator`).

**Tests:**
- All existing `test_orchestrator*.py` tests must pass unchanged (they call `orchestrator.execute_tool`).
- Add `tests/unit/test_tool_execution_pipeline.py` — construct a minimal `ExecutionContext` with mocks, call `execute_tool()` directly (bypassing `Orchestrator`).

---

### Phase D — Extract `task_lifecycle.py` *(~1 day, medium risk)*

**What moves:**

- `start_new_task()` (line 3079)
- `restore_continue_state()` (line 3187)
- `_sync_session_state()` (line 3215)
- `approve_plan()` / `reject_plan()` / `wait_for_plan_approval()` (lines 3234–3255)
- `get_current_task_id()` (line 3226)

**New file:** `src/core/orchestration/task_lifecycle.py`

```python
class TaskLifecycleManager:
    """Owns per-task state reset and plan approval workflow."""

    def __init__(self, orchestrator_ref):
        self._orch = orchestrator_ref   # weak reference back to Orchestrator

    def start_new_task(self, ...) -> str: ...
    def restore_continue_state(self, state: dict) -> None: ...
    def approve_plan(self) -> None: ...
    def reject_plan(self) -> None: ...
    async def wait_for_plan_approval(self) -> bool: ...
```

**In `Orchestrator.__init__`:** `self.task_lifecycle = TaskLifecycleManager(self)`

**In `Orchestrator`:** delegate methods become one-liners:
```python
def start_new_task(self, ...) -> str:
    return self.task_lifecycle.start_new_task(...)

def approve_plan(self) -> None:
    self.task_lifecycle.approve_plan()
```

**Why:** Task lifecycle and plan approval have no business being inside `execute_tool`'s host class. They currently reference ~12 instance variables — passing `self` as a reference is acceptable here because the lifecycle manager is a *controller* of the orchestrator, not a standalone component.

**Tests:** existing plan-mode tests pass unchanged. Add `tests/unit/test_task_lifecycle.py`.

---

### Phase E — Extract `inference_loop.py` *(~2 days, high complexity)*

**What moves:**

`run_agent_once()` (lines 3315–4023) and its helpers:
- `_publish_git_status()` (line 3017)
- `compact_context()` (line 2810)
- `_background_model_check()` (line 2989)
- Provider family helpers: `_PROVIDER_FAMILY_MAP`, `_map_provider_family`, `get_provider_capabilities` (lines 4025–4137)

**New file:** `src/core/orchestration/inference_loop.py`

```python
class InferenceLoop:
    """Drives the LangGraph cognitive pipeline for a single agent turn."""

    def __init__(self, orchestrator_ref):
        self._orch = orchestrator_ref
        self._graph_executor: ThreadPoolExecutor = ...  # moved from Orchestrator

    def run_once(
        self,
        system_prompt_name: Optional[str],
        messages: list,
        tools: dict,
        cancel_event: Optional[Any] = None,
    ) -> dict: ...

    def compact_context(self) -> dict: ...
    def get_provider_capabilities(self) -> dict: ...

    @classmethod
    def map_provider_family(cls, provider_name: str, model_id: str) -> str: ...
```

**In `Orchestrator`:** `run_agent_once` becomes a one-liner:
```python
def run_agent_once(self, system_prompt_name, messages, tools, cancel_event=None):
    return self.inference_loop.run_once(system_prompt_name, messages, tools, cancel_event)
```

**`_graph_executor` ThreadPoolExecutor** moves to `InferenceLoop.__init__` and is shut down in `InferenceLoop.close()`, called from `Orchestrator.close()`.

**Tests:** All `test_orchestrator*.py` pass unchanged. Add `tests/unit/test_inference_loop.py`.

---

### Phase F — Slim `Orchestrator.__init__` into `orchestrator_bootstrap.py` *(~1 day)*

After phases A–E, `Orchestrator.__init__` is still ~500 lines of sequential subsystem wiring. Extract to a builder function:

**New file:** `src/core/orchestration/orchestrator_bootstrap.py`

```python
@dataclass
class OrchestratorDeps:
    """All subsystems wired during Orchestrator construction."""
    event_bus: EventBus
    msg_mgr: MessageManager
    rollback_manager: RollbackManager
    file_lock_manager: FileLockManager
    snapshot_manager: GitSnapshotManager
    session_store: SessionStore
    lifecycle_manager: SessionLifecycleManager
    session_mgr: SessionManager
    token_monitor: TokenBudgetMonitor
    context_controller: ContextController
    preview_service: PreviewService
    plan_mode: PlanMode
    cost_tracker: SessionCostTracker
    tool_execution_service: ToolExecutionService
    preview_coordinator: PreviewCoordinator
    tool_executor: ThreadPoolExecutor


def bootstrap_orchestrator(
    adapter,
    tool_registry: ToolRegistry,
    working_dir: Path,
    allow_external: bool,
    message_max_tokens: int,
    deterministic: bool,
    seed: Optional[int],
    event_bus: Optional[EventBus],
    dry_run: bool,
) -> OrchestratorDeps:
    """Wire all subsystems and return them as a single struct."""
    ...
```

**In `Orchestrator.__init__`:**
```python
def __init__(self, ...):
    deps = bootstrap_orchestrator(...)
    self.event_bus = deps.event_bus
    self.msg_mgr = deps.msg_mgr
    ...  # flat assignment, ~25 lines
    self.task_lifecycle = TaskLifecycleManager(self)
    self.inference_loop = InferenceLoop(self)
```

**Why `OrchestratorDeps` dataclass:** keeps `bootstrap_orchestrator` testable in isolation — you can assert that the right subsystems were constructed without invoking a real `Orchestrator`.

---

### Phase G — Final cleanup *(~0.5 days)*

After A–F, `orchestrator.py` should be ~250 lines:

```python
"""Orchestrator public API — thin shell over extracted subsystems."""

# Re-exports for backward compatibility
from src.core.orchestration.tool_constants import (
    WRITE_TOOLS_REQUIRING_READ,
    DRY_RUN_BLOCKED_TOOLS,
    PERMISSION_REQUIRED_TOOLS,
)
from src.core.orchestration.tool_registry import ToolRegistry
from src.core.orchestration.registry_builder import example_registry

class Orchestrator:
    """Public API — delegates to extracted subsystems."""
    def __init__(self, ...): ...          # ~30 lines via bootstrap
    def execute_tool(self, ...): ...      # 5-line delegate
    def run_agent_once(self, ...): ...    # 3-line delegate
    def start_new_task(self, ...): ...    # 3-line delegate
    def approve_plan(self): ...           # 2-line delegate
    # ... all other public methods remain, each ≤5 lines
    def close(self): ...                  # shuts down executors
```

**Remove:** all dead code, duplicate logic, and inline helpers that were moved.

**Verify:** `wc -l orchestrator.py` ≤ 300.

---

## Dependency Graph After Refactoring

```
orchestrator.py  (shell, ~250 lines)
├── tool_constants.py          (leaf — no internal imports)
├── tool_registry.py           (existing, extended)
├── registry_builder.py        (imports tool_registry, all tool sub-packages)
├── orchestrator_bootstrap.py  (imports all subsystem constructors)
├── tool_execution_pipeline.py (imports tool_constants, tool_registry, subsystems)
├── task_lifecycle.py          (imports plan_mode, session_manager)
└── inference_loop.py          (imports graph/builder, agent_brain, llm_manager)
```

**Circular imports eliminated:**
- `permission_gateway.py` → `tool_constants.py` (not `orchestrator.py`) ✓
- No module imports from `orchestrator.py` except `main.py` and the TUI bridge ✓

---

## Execution Order & Rationale

| Phase | Effort | Risk | Rationale |
|---|---|---|---|
| A — `tool_constants.py` | 0.5 day | Minimal | Eliminates the only circular import; purely mechanical move |
| B — `tool_registry.py` / `registry_builder.py` | 1 day | Low | Self-contained class + factory; no shared mutable state |
| C — `tool_execution_pipeline.py` | 3 days | High | Largest, most complex; do with full test coverage in place |
| D — `task_lifecycle.py` | 1 day | Medium | Touches plan approval asyncio state; test carefully |
| E — `inference_loop.py` | 2 days | Medium-High | LangGraph wiring + ThreadPoolExecutor ownership |
| F — `orchestrator_bootstrap.py` | 1 day | Low | Pure constructor extraction; no behaviour change |
| G — Final cleanup | 0.5 day | Minimal | Delete dead code, verify line count |

**Total estimate: ~9 developer-days**, executable in phases over 2 sprints.

---

## What We Do NOT Change

- `Orchestrator`'s public constructor signature
- All public method names (`execute_tool`, `run_agent_once`, `start_new_task`, etc.)
- All currently passing tests (3088)
- The `graph/` sub-package (LangGraph node implementations)
- All other `orchestration/` modules not listed above
