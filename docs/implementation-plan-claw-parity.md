# Implementation Plan: CodingAgent → Claw Code Parity

> **Source:** Gap analysis matrix and implementation plan provided by user.
> **Analysed against:** live codebase as of 2026-04-12.
> **Companion docs:** `docs/claw-code-architecture.md`, `docs/codingagent-architecture.md`

---

## Gap Analysis: Actual vs. Plan

Before the task list, each phase is audited against the current codebase so work is not
duplicated.

| Phase | Plan description | Actual status | Key files |
|---|---|---|---|
| 1 — Instruction Discovery | `discover_instruction_files()` ancestor walk | **DONE** | `src/core/context/instruction_files.py` wired in `context_builder.py:741` |
| 2 — Permission System (engine) | `PermissionPolicy` allow/deny/ask | **DONE** (different shape) | `permission_policy.py`, `permission_gateway.py`, `project_settings.py` |
| 2 — Permission System (`permission_kind` on tools) | `@tool(permission_kind=...)` | **NOT DONE** | Tools still use `side_effects: list[str]` |
| 2 — Permission System (preflight wiring) | `PermissionPolicy.evaluate()` in preflight | **NOT DONE** | `tool_preflight.py` has no PermissionPolicy call |
| 3 — Hook System | `PreToolUse`/`PostToolUse` shell hooks | **DONE** | `shell_hooks.py` + `tool_hooks.py` wired in `tool_execution_service.py:26` |
| 4 — JSONL Session Store | Append-only `.jsonl` store with fork/revert | **NOT DONE** | `session_store.py` is SQLite-only |
| 5.1 — Core Protocols | `ApiClientProtocol`, `ToolExecutorProtocol` | **NOT DONE** | No `interfaces.py` |
| 5.2 — Multi-Transport MCP | SSE + WebSocket MCP transports | **NOT DONE** | `mcp_client.py` is stdio-only (comment: "deferred to S3-A-http") |
| 6 — Frontier Fast Loop | Single mega-node for LARGE/FRONTIER | **NOT DONE** | No `frontier_loop_node.py` |

### Phase 1 — Detail

`src/core/context/instruction_files.py` implements the full ancestor walk:

- Walks `cwd → /` (root-first, same order as claw code)
- Checks `AGENTS.md`, `AGENTS.local.md`, `.agent/AGENTS.md`, `.agent/instructions.md`
- SHA-256 dedup, 4 000 chars per file / 12 000 total (matches claw code constants)
- Integrated into `context_builder.py` dynamic section (lines 741–749)

**Minor gap:** The candidate file names use `AGENTS.md` rather than `CLAUDE.md` /
`CLAUDE.local.md`. Projects using the `CLAUDE.md` convention (which many developers
already have) are not discovered. This needs a single-line addition to `_CANDIDATE_NAMES`.

### Phase 2 — Detail

Three separate permission layers already exist:

1. **`permission_policy.py`** — `PermissionPolicy` / `PermissionRule` / `Behavior(ALLOW|DENY|ASK)`.
   Pattern-based, last-rule-wins. Loaded from `~/.coding_agent/permissions.json`.
   Singleton via `get_permission_policy()`. **Not wired into the hot path.**

2. **`permission_gateway.py`** — `PermissionGateway` with 5 sequential gates:
   plan-mode write gate → explore-mode guard → PermissionLevel gate →
   active-mode enforcement → user-approval gate.
   Called from `tool_execution_service.py`.

3. **`project_settings.py`** — `PermissionMode` string enum (`ReadOnly`, `WorkspaceWrite`,
   `DangerFullAccess`) loaded from `.agent-context/config.json#permissionMode`.
   Applied via `set_active_permission_mode()` in `tools_config.py`.

The plan's goal is already 80% met. The remaining gaps:

- `PermissionPolicy` (pattern rules) is not consulted by `PermissionGateway` (Gate 3 reads
  `tools_config.get_tool_permission()` — a static per-tool level, not a dynamic rule set).
- `@tool` decorator has no `permission_kind` parameter; tools declare `side_effects=["write"]`
  which maps to `PermissionLevel` via `tools_config.py`, not to `PermissionKind`.

### Phase 3 — Detail

`ShellHookRunner` (`shell_hooks.py`) and `ToolHookRunner` (`tool_hooks.py`) are both
fully implemented and wired. `ToolExecutionService.__init__` accepts `hook_runner=` and
calls `run_pre` / `async_run_post` around every tool dispatch. Config loaded from
`.agent/settings.json` (`ShellHookRunner`) or `.agent/hooks.json` (`ToolHookRunner`).

No outstanding work here unless the plan requires matcher-based per-tool hook config
(currently `ShellHookRunner` applies hooks globally to all tools that match event type).

---

## Revised Execution Order

Based on the gap analysis, the actual remaining work is:

| Priority | Phase | Task | Effort |
|---|---|---|---|
| 1 | 1 | Add `CLAUDE.md` / `CLAUDE.local.md` to `_CANDIDATE_NAMES` | XS |
| 2 | 5.1 | Define `interfaces.py` (Protocol classes) | S |
| 3 | 2 | Add `permission_kind` to `@tool` decorator | M |
| 4 | 2 | Wire `PermissionPolicy` into `PermissionGateway` Gate 3 | S |
| 5 | 6 | Implement `frontier_loop_node.py` | L |
| 6 | 6 | Tier-based graph routing in `builder.py` | M |
| 7 | 4 | Implement `jsonl_session_store.py` | L |
| 8 | 4 | Config toggle for storage backend | S |
| 9 | 5.2 | MCP SSE transport | M |
| 10 | 5.2 | MCP WebSocket transport | M |

---

## Task Breakdown

---

### TASK-1: Add CLAUDE.md to instruction file candidates

**Phase:** 1 (Instruction Discovery — minor gap)
**File:** `src/core/context/instruction_files.py`
**Effort:** XS (~10 min)

**Background:**
`_CANDIDATE_NAMES` currently checks only `AGENTS.md` variants. Many developers and
tools (Claude Code, claw code itself) write `CLAUDE.md`. Without this addition, projects
using the `CLAUDE.md` convention get no context injection.

**Implementation:**

In `instruction_files.py`, extend `_CANDIDATE_NAMES`:

```python
_CANDIDATE_NAMES: tuple[tuple[str, ...], ...] = (
    ("AGENTS.md",),
    ("AGENTS.local.md",),
    (".agent", "AGENTS.md"),
    (".agent", "instructions.md"),
    # TASK-1: also discover CLAUDE.md / claw code convention
    ("CLAUDE.md",),
    ("CLAUDE.local.md",),
    (".claude", "CLAUDE.md"),
    (".claw", "CLAUDE.md"),
    (".claw", "instructions.md"),
)
```

SHA-256 dedup already handles the case where both `CLAUDE.md` and `AGENTS.md` exist
with identical content, so no extra guard is needed.

**Tests:**
- `tests/unit/test_instruction_files.py` — add test that a `CLAUDE.md` in a parent
  directory is discovered and rendered.
- Verify dedup still works when two paths have identical content.

**Acceptance criteria:**
- `discover_instruction_files("/some/project")` finds `CLAUDE.md` anywhere in the
  ancestor chain.
- SHA-256 dedup prevents double-injection if both `AGENTS.md` and `CLAUDE.md` exist
  with identical content.

---

### TASK-2: Define `src/core/interfaces.py` — Protocol classes

**Phase:** 5.1 (Decoupling)
**File:** `src/core/interfaces.py` (new)
**Effort:** S (~2 h)

**Background:**
The `Orchestrator` is tightly coupled to concrete adapter classes. Defining Python
`Protocol` interfaces (the equivalent of Rust's trait-based `ConversationRuntime<C, T>`)
enables:

- `MockApiClient` / `MockToolExecutor` in tests without monkey-patching
- Future adapter implementations that don't inherit from `OpenAICompatibleAdapter`
- Easier sub-agent isolation (sub-agents can receive a protocol-typed dependency)

**Implementation:**

```python
# src/core/interfaces.py
from __future__ import annotations
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

@runtime_checkable
class ApiClientProtocol(Protocol):
    """Minimal contract that any LLM adapter must satisfy."""

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        *,
        stream: bool = False,
        format_json: bool = False,
        max_tokens: Optional[int] = None,
        model: Optional[str] = None,
    ) -> Any:
        ...

    @property
    def model_name(self) -> str:
        ...

    @property
    def provider_name(self) -> str:
        ...


@runtime_checkable
class ToolExecutorProtocol(Protocol):
    """Minimal contract for tool dispatch (sync or async)."""

    async def execute(
        self,
        name: str,
        args: Dict[str, Any],
        *,
        working_dir: Optional[str] = None,
    ) -> Any:
        ...


@runtime_checkable
class SessionStoreProtocol(Protocol):
    """Minimal contract for conversation persistence."""

    def add_message(self, session_id: str, role: str, content: str) -> None: ...
    def get_messages(self, session_id: str) -> List[Dict[str, Any]]: ...
    def fork_session(self, session_id: str, new_session_id: str) -> None: ...
```

**Refactor:**
- Type-annotate `Orchestrator.__init__` adapter parameter as `ApiClientProtocol`.
- Type-annotate `ToolExecutionService.__init__` as accepting `ToolExecutorProtocol`.
- Update `MockAdapter` (`src/core/inference/adapters/mock_adapter.py`) to confirm it
  satisfies `ApiClientProtocol` via `isinstance(mock, ApiClientProtocol)`.

**No runtime changes** — Protocol is structural (duck typing); no inheritance needed.

**Tests:**
- `tests/unit/test_interfaces.py` — assert `MockAdapter` satisfies `ApiClientProtocol`,
  assert `ToolExecutionService` accepts `ToolExecutorProtocol`.

**Acceptance criteria:**
- `isinstance(any_existing_adapter, ApiClientProtocol)` returns `True` for all adapters.
- `Orchestrator` constructor type annotation passes mypy with `--strict`.
- `MockAdapter` verified compatible without code changes to the adapter.

---

### TASK-3: Add `permission_kind` to `@tool` decorator

**Phase:** 2 (Granular Permission System — tool metadata)
**Files:** `src/tools/_tool.py`, all `src/tools/*.py` tool modules
**Effort:** M (~3 h)

**Background:**
Tools currently declare `side_effects: list[str]` (e.g. `["write"]`). The
`permission_gateway.py` translates this into `PermissionLevel` via `tools_config.py`.
The plan calls for explicit `permission_kind: PermissionKind` on each tool — the same
approach as claw code's `ToolSpec.permission_required: PermissionKind`.

This makes intent explicit and removes the indirect mapping through `tools_config.py`.

**Note:** The existing `side_effects` field must be retained for backward compatibility
— other subsystems (preflight path containment check, MODIFYING_TOOLS sets) rely on it.
`permission_kind` is an additive field.

**Step 1 — Define `PermissionKind`** in `src/tools/_tool.py` (or `src/core/interfaces.py`):

```python
from enum import Enum

class PermissionKind(str, Enum):
    READ_FILE    = "ReadFile"
    WRITE_FILE   = "WriteFile"
    EXECUTE_BASH = "ExecuteBash"
    NETWORK      = "NetworkFetch"
    GIT_WRITE    = "GitWrite"      # commit, push, stash
    GIT_READ     = "GitRead"
    MEMORY       = "MemoryWrite"
    DELEGATE     = "Delegate"      # subagent spawn
    LSP          = "LSP"
    NONE         = "None"
```

**Step 2 — Add to decorator:**

```python
@tool(
    side_effects=["write"],
    tags=["coding"],
    permission_kind=PermissionKind.WRITE_FILE,   # new field
)
def write_file(path: str, content: str, workdir: Path = None) -> Dict[str, Any]:
    ...
```

**Step 3 — Annotate all tools:**

Priority mapping (update all ~60 tool functions in one pass):

| `side_effects` | `permission_kind` |
|---|---|
| `["write"]` on file tools | `WRITE_FILE` |
| `["execute"]` on bash | `EXECUTE_BASH` |
| `["network"]` | `NETWORK` |
| `["write"]` on git commit/push | `GIT_WRITE` |
| `[]` on git log/diff/status | `GIT_READ` |
| `["write"]` on memory | `MEMORY` |
| `["execute"]` on delegate_task | `DELEGATE` |
| read-only file tools | `READ_FILE` |
| lsp_rename / lsp_diagnostics | `LSP` / `READ_FILE` |

**Step 4 — Expose via registry:**

```python
# _registry.py
def get_permission_kind(tool_name: str) -> Optional[PermissionKind]:
    meta = self.tools.get(tool_name, {})
    return meta.get("permission_kind")
```

**Tests:**
- `tests/unit/test_permission_kind.py` — verify every registered tool has a `permission_kind`.
- Verify `ToolRegistry.get_permission_kind("write_file")` returns `PermissionKind.WRITE_FILE`.

**Acceptance criteria:**
- All 60+ tools in `src/tools/` declare `permission_kind`.
- `get_permission_kind(name)` returns the correct kind for a sample of tools.
- No existing tests broken (side_effects still present).

---

### TASK-4: Wire `PermissionPolicy` into `PermissionGateway` Gate 3

**Phase:** 2 (Granular Permission System — preflight wiring)
**Files:** `src/core/orchestration/permission_gateway.py`
**Effort:** S (~2 h)

**Background:**
`PermissionPolicy` (pattern/behavior rules loaded from `~/.coding_agent/permissions.json`)
exists but is not consulted by `PermissionGateway`. Gate 3 currently calls
`tools_config.get_tool_permission()` which returns a static `PermissionLevel` — it does
not support dynamic user-defined allow/deny/ask rules.

The fix: insert a `PermissionPolicy.combined_check()` call between Gate 2 and Gate 3.

**Implementation in `permission_gateway.py`:**

```python
# After Gate 2 (explore-mode guard), before Gate 3 (PermissionLevel)

def _gate2b_policy_rules(self, name: str, args: Dict[str, Any]) -> PermissionResult:
    """TASK-4: Check user-defined PermissionPolicy rules."""
    try:
        from src.core.orchestration.permission_policy import (
            get_permission_policy, Behavior
        )
        from src.tools.permission_context import get_permission_context
        policy = get_permission_policy()
        ctx = get_permission_context()
        behavior = policy.combined_check(name, cli_context=ctx)
        if behavior == Behavior.DENY:
            return PermissionResult(
                blocked=True,
                rejection={"ok": False, "error": f"Permission denied: '{name}' is blocked by policy."},
            )
        if behavior == Behavior.ASK:
            # Publish event to TUI for user confirmation (same path as Gate 5)
            return self._gate5_user_approval(name, args)
    except Exception:
        pass  # policy unavailable → allow
    return PermissionResult(blocked=False)
```

Wire it into `PermissionGateway.check()` between gates 2 and 3.

**Config-driven deny example** (`.agent-context/config.json` or `permissions.json`):

```json
{
  "default_behavior": "allow",
  "rules": [
    {"pattern": "bash",       "behavior": "ask"},
    {"pattern": "git_commit", "behavior": "ask"},
    {"pattern": "web_*",      "behavior": "deny"}
  ]
}
```

**Project-level policies** — also load from `.agent-context/permissions.json` (project)
and merge with user-level `~/.coding_agent/permissions.json` (user).

**Tests:**
- `tests/unit/test_permission_gateway_policy.py` — mock `get_permission_policy()` to
  return a DENY policy for `bash`, verify gateway blocks it.
- Verify ASK behavior triggers the same TUI event as Gate 5.

**Acceptance criteria:**
- A `deny` rule for `"bash"` in permissions.json prevents bash execution.
- An `ask` rule prompts the user via the existing `tool.permission_required` event.
- An `allow` rule on `"read_*"` lets through all read tools regardless of other gates.
- Gate ordering preserved (doom_loop DENY still works).

---

### TASK-5: Implement `frontier_loop_node.py`

**Phase:** 6 (Pipeline Streamlining)
**File:** `src/core/orchestration/graph/nodes/frontier_loop_node.py` (new)
**Effort:** L (~6 h)

**Design decisions (locked):**

- **Plan mode (Q1):** Pause the inner loop and set `awaiting_plan_approval = True` when a
  write tool is requested while `plan_mode` is active. Return to LangGraph →
  `wait_for_user_node`. After user approves, the graph routes back into `frontier_loop`
  which resumes from the next iteration (state carries all prior history). Disabling plan
  mode or batch-approving writes would both violate user trust.

- **Replan on failure (Q2):** `evaluation_node` failure routes directly back into
  `frontier_loop` with the failure reason appended to `AgentState.history`. No external
  `replan_node` or `planning_node` — the frontier model reads failure context and adjusts
  its own next tool calls. `replan_attempts` is still incremented (≥ 5 → `memory_sync`
  as a safety valve).

**Background:**
The 16-node LangGraph pipeline adds overhead that is counterproductive for LARGE/FRONTIER
models (GPT-4o, Claude Opus, Gemini Ultra) — models that can plan, execute, and
self-correct in a single tight loop. The frontier fast loop collapses the pipeline into a
single node that mirrors claw code's `ConversationRuntime::run_turn()`.

**Design constraints:**

1. `AgentState` must be updated correctly so `memory_update_node` and `evaluation_node`
   still receive coherent state when the loop returns.
2. All tool calls must pass through `ToolExecutionService` (hooks, preflight, PRSW).
3. Inner loop must respect `max_tool_calls` (default 60 for FRONTIER).
4. Context overflow surfaces as `errors=["context_overflow"]`.
5. Plan mode write gate: pause loop, set `awaiting_plan_approval=True`, break.
   On resume (after `wait_for_user_node` approves), the loop restarts from the top
   with `plan_mode_approved=True` in state — the pending tool call is replayed.
6. The node must store the **pending tool call** in state when pausing so the resume
   path can execute it without asking the LLM again.

**New AgentState fields required:**

```python
# graph/state.py — add these fields
frontier_loop_pending_tool: Optional[Dict[str, Any]]  # tool call awaiting plan approval
frontier_loop_resume: Optional[bool]                   # True when resuming after approval
```

**Algorithm:**

```python
async def frontier_loop_node(state: AgentState, config: RunnableConfig) -> AgentState:
    orch = _resolve_orchestrator(state, config)
    tools = orch.get_tools_for_role("operational")
    system_prompt = orch.context_builder.build_system_prompt(
        role="operational-frontier", task=state["task"],
        tools=tools, model_tier="frontier",
    )
    messages = _build_messages(state, system_prompt)
    tool_call_count = state.get("tool_call_count") or 0
    max_calls = state.get("max_tool_calls") or 60

    # Resume path: pending tool call was held for plan approval → execute it now
    pending = state.get("frontier_loop_pending_tool")
    if pending and state.get("plan_mode_approved"):
        result = await orch.tool_execution_service.execute(
            pending["name"], pending.get("arguments", {})
        )
        tool_call_count += 1
        messages.append(_tool_result_msg(pending, result))
        state = {**state,
                 "last_result": result,
                 "last_tool_name": pending["name"],
                 "tool_call_count": tool_call_count,
                 "frontier_loop_pending_tool": None,
                 "plan_mode_approved": None}

    while tool_call_count < max_calls:
        # 1. LLM call
        response = await call_model(messages, tools=tools,
                                    model=orch._model, adapter=orch._adapter)

        # 2. Overflow guard
        if _is_overflow(response):
            return {**state, "errors": ["context_overflow"]}

        # 3. No tool call → model says it is done
        if not _has_tool_call(response):
            state = {**state, "history": state["history"] + [_assistant_msg(response)]}
            break

        # 4. Parse tool call
        tool_call = _parse_tool_call(response)
        state = {**state, "history": state["history"] + [_assistant_msg(response)]}

        # 5. Plan-mode write gate — pause before executing write tool
        if (orch.plan_mode and orch.plan_mode.enabled
                and tool_call["name"] in _WRITE_TOOLS
                and not state.get("plan_mode_approved")):
            return {**state,
                    "awaiting_plan_approval": True,
                    "frontier_loop_pending_tool": tool_call}

        # 6. Execute via ToolExecutionService (hooks + preflight + PRSW)
        result = await orch.tool_execution_service.execute(
            tool_call["name"], tool_call.get("arguments", {})
        )
        tool_call_count += 1

        # 7. Update messages and state for next iteration
        messages.append(_tool_result_msg(tool_call, result))
        state = {**state,
                 "last_result": result,
                 "last_tool_name": tool_call["name"],
                 "tool_call_count": tool_call_count,
                 "history": state["history"] + [_tool_event(tool_call, result)]}

    return state
```

**Routing from `frontier_loop_node`:**

```
frontier_loop_node
  ├─ errors=["context_overflow"]        → memory_sync
  ├─ awaiting_plan_approval = True      → wait_for_user
  └─ (default — loop exited cleanly)   → evaluation
```

**Routing from `evaluation_node` back to `frontier_loop` (Q2):**

In `builder.py`, extend `should_after_evaluation`:

```python
def should_after_evaluation(state: AgentState) -> str:
    tier = (state.get("model_tier") or "").lower()
    if tier in ("large", "frontier"):
        if state.get("evaluation_result") == "fail":
            replan_count = (state.get("replan_attempts") or 0) + 1
            # Safety valve: give up after 5 replan loops
            if replan_count >= 5:
                return "memory_sync"
            return "frontier_loop"   # inject failure context, re-enter loop
    # standard path
    if state.get("evaluation_result") == "fail":
        return "replan"
    return "memory_update"
```

When routing back, the calling code (or a small wrapper in the router) appends the
evaluation failure reason to `state["history"]` before re-entering `frontier_loop`.

**Tests:**
- `tests/unit/test_frontier_loop_node.py`:
  - Mock 3 sequential tool calls → verify all 3 executed, loop exits on 4th (no-tool) response.
  - `max_tool_calls=2`, mock 5 tool calls → verify only 2 executed, loop exits.
  - Overflow: mock response with `context_overflow` → verify `state["errors"]` set.
  - Plan mode pause: write tool while `plan_mode.enabled=True` → verify
    `awaiting_plan_approval=True`, `frontier_loop_pending_tool` populated.
  - Resume: feed state with `frontier_loop_pending_tool` set + `plan_mode_approved=True` →
    verify pending tool executed before first LLM call.
- `tests/unit/test_builder_tier_routing.py`:
  - `evaluation_result="fail"` + `model_tier="frontier"` → routes to `frontier_loop`.
  - `evaluation_result="fail"` + `replan_attempts=5` + `model_tier="frontier"` → routes to
    `memory_sync`.

**Acceptance criteria:**
- Node executes multiple tool calls without returning to perception/planning/debug.
- `tool_call_count` increments on every dispatch.
- `history` contains assistant messages and tool result events in correct order.
- `last_result` and `last_tool_name` reflect the final tool execution.
- Loop exits cleanly when model produces no tool call.
- Plan mode: write tool pauses loop, stores pending tool in state.
- Resume: pending tool executes first, then loop continues normally.
- Overflow: sets `errors=["context_overflow"]` and returns immediately.

---

### TASK-6: Tier-based graph routing in `builder.py`

**Phase:** 6 (Pipeline Streamlining — graph routing)
**File:** `src/core/orchestration/graph/builder.py`, `src/core/orchestration/orchestrator.py`
**Effort:** M (~3 h)

**Design decisions (locked):**

- **Graph cache strategy (Q3):** One compiled graph per tier, cached in a dict keyed by
  tier string — `_GRAPH_CACHE: Dict[str, CompiledStateGraph]`. Cache up to 5 entries
  (one per `ModelTier` value). Graph switches apply **only at task boundaries**
  (`start_new_task()`). If the user types `/model` mid-session, the tier is recorded
  but the active graph does not swap until the next `start_new_task()` call.

**Background:**
`compile_agent_graph()` always compiles the full 16-node graph regardless of model tier.
LARGE/FRONTIER models should bypass analysis, planning, validation, step_controller,
verification, and debug, routing `perception → frontier_loop → evaluation → memory_update`.

**Graph cache implementation in `builder.py`:**

```python
# builder.py — module-level cache
_GRAPH_CACHE: Dict[str, "CompiledStateGraph"] = {}
_GRAPH_CACHE_LOCK = threading.Lock()

def get_compiled_graph(
    orchestrator,
    model_tier: str = "",
    graph_type: str = "standard",
) -> "CompiledStateGraph":
    """Return a cached compiled graph for the given tier.

    Compiles on first call for each tier; subsequent calls return the
    cached object.  Cache is invalidated per-tier by
    invalidate_graph_cache(tier).
    """
    tier_lower = (model_tier or "").lower()
    cache_key = f"{graph_type}:{tier_lower}"
    with _GRAPH_CACHE_LOCK:
        if cache_key not in _GRAPH_CACHE:
            _GRAPH_CACHE[cache_key] = _compile_graph(orchestrator, tier_lower, graph_type)
        return _GRAPH_CACHE[cache_key]


def invalidate_graph_cache(tier: str = "") -> None:
    """Evict the cached graph for *tier* (or all tiers if tier is empty)."""
    with _GRAPH_CACHE_LOCK:
        if tier:
            _GRAPH_CACHE.pop(f"standard:{tier.lower()}", None)
        else:
            _GRAPH_CACHE.clear()


def _compile_graph(orchestrator, tier_lower: str, graph_type: str) -> "CompiledStateGraph":
    graph = StateGraph(AgentState)

    # Nodes present in every variant
    graph.add_node("perception",    perception_node)
    graph.add_node("memory_update", memory_update_node)
    graph.add_node("memory_sync",   memory_sync_node)
    graph.add_node("wait_for_user", wait_for_user_node)
    graph.add_node("evaluation",    evaluation_node)
    graph.add_node("delegation",    delegation_node)

    if tier_lower in ("large", "frontier"):
        # ── FRONTIER FAST PATH ──────────────────────────────────────────────
        from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node
        graph.add_node("frontier_loop", frontier_loop_node)

        graph.set_entry_point("perception")
        graph.add_conditional_edges("perception",     _route_after_perception_frontier)
        graph.add_conditional_edges("frontier_loop",  _route_after_frontier_loop)
        graph.add_conditional_edges("evaluation",     should_after_evaluation)
        graph.add_conditional_edges("wait_for_user",  _route_after_wait_for_user_frontier)
        graph.add_edge("memory_update", END)
        graph.add_edge("memory_sync",   END)

    else:
        # ── STANDARD 16-NODE PATH ───────────────────────────────────────────
        # (unchanged from current compile_agent_graph body)
        graph.add_node("analysis",            analysis_node)
        graph.add_node("analyst_delegation",  analyst_delegation_node)
        graph.add_node("planning",            planning_node)
        graph.add_node("plan_validator",      plan_validator_node)
        graph.add_node("execution",           execution_node)
        graph.add_node("step_controller",     step_controller_node)
        graph.add_node("verification",        verification_node)
        graph.add_node("debug",               debug_node)
        graph.add_node("replan",              replan_node)
        # ... all existing conditional edges ...

    return graph.compile()
```

**New frontier routers:**

```python
def _route_after_perception_frontier(state: AgentState) -> str:
    if "context_overflow" in (state.get("errors") or []):
        return "memory_sync"
    if state.get("needs_clarification"):
        return "memory_sync"
    return "frontier_loop"


def _route_after_frontier_loop(state: AgentState) -> str:
    if "context_overflow" in (state.get("errors") or []):
        return "memory_sync"
    if state.get("awaiting_plan_approval"):
        return "wait_for_user"
    return "evaluation"


def _route_after_wait_for_user_frontier(state: AgentState) -> str:
    """After user acts on a plan-mode gate inside frontier loop."""
    if state.get("plan_approved"):
        return "frontier_loop"   # resume with plan_mode_approved=True
    return "memory_sync"         # user rejected → end task
```

**Wire-up in `Orchestrator`:**

Replace the current `self._compiled_graph = compile_agent_graph(self)` call with:

```python
# orchestrator.py
def _get_graph(self) -> "CompiledStateGraph":
    """Return the compiled graph for the active model tier.

    Called at the start of each task (start_new_task calls this).
    Graph is cached per-tier; switches only take effect at task boundaries.
    """
    tier = getattr(self, "_active_model_tier", "") or ""
    return get_compiled_graph(self, model_tier=tier)
```

In `start_new_task_impl()` (`task_lifecycle.py`), after setting `_active_model_tier`:

```python
# task_lifecycle.py — start_new_task_impl()
# Detect tier from active adapter model name
try:
    from src.core.inference.model_tiers import classify_model
    _new_tier = classify_model(orch._model or "").value
    if _new_tier != getattr(orch, "_active_model_tier", ""):
        orch._active_model_tier = _new_tier
        # No need to invalidate cache — get_compiled_graph compiles on demand
except Exception:
    pass
```

**Tests:**
- `tests/unit/test_builder_tier_routing.py`:
  - `get_compiled_graph(orch, "frontier")` graph node names contain `frontier_loop`,
    do not contain `planning`, `debug`, `step_controller`.
  - `get_compiled_graph(orch, "small")` graph node names contain all 16 standard nodes.
  - Cache hit: calling twice returns the same object (`is` identity check).
  - `invalidate_graph_cache("frontier")` clears only the frontier entry.
  - `/model` switch mid-task: `_active_model_tier` updated but same graph used until
    next `start_new_task()`.

**Acceptance criteria:**
- Frontier graph contains exactly 7 nodes: perception, frontier_loop, evaluation,
  memory_update, memory_sync, wait_for_user, delegation.
- Standard graph (NANO/SMALL/MEDIUM) is functionally unchanged.
- Cache returns the same `CompiledStateGraph` object on repeated calls for the same tier.
- Graph switch applies at task boundary, not mid-task.
- `invalidate_graph_cache()` (no args) clears all entries — useful for test isolation.

---

### TASK-7: Implement `jsonl_session_store.py`

**Phase:** 4 (Session Storage Simplification)
**File:** `src/core/memory/jsonl_session_store.py` (new)
**Effort:** L (~5 h)

**Design decision (locked — Q4):** One-shot migration script
`scripts/migrate_sessions.py` converts existing SQLite data to JSONL. No read-shim in
the orchestrator. SQLite remains the default until the migration script is proven and
the JSONL store has passed a full test cycle. Users flip `CODING_AGENT_STORAGE_BACKEND=jsonl`
explicitly after running the migration.

**Background:**
SQLite (`session_store.py`) works but is heavier than needed and prone to WAL/locking
issues in concurrent scenarios. A JSONL append-only store mirrors claw code's `.jsonl`
approach: simpler to inspect externally, naturally supports fork/revert via byte offsets,
and has no schema migration concerns.

**Interface contract** (must match `SessionStore` for drop-in replacement):

```python
class JsonlSessionStore:
    # Standard message ops
    def add_message(session_id: str, role: str, content: str) -> None
    def get_messages(session_id: str) -> List[Dict[str, Any]]

    # Tool call logging
    def add_tool_call(session_id, tool_name, args, result) -> None
    def get_tool_calls(session_id) -> List[Dict[str, Any]]

    # Error logging
    def add_error(session_id, error_type, error_message, context) -> None

    # Plan persistence
    def save_plan(session_id, plan, status) -> None
    def get_plan(session_id) -> Optional[Dict[str, Any]]

    # Fork / revert
    def fork_session(session_id: str, new_session_id: str) -> str
        # Copy events up to current position to new_session_id.jsonl
    def revert_session(session_id: str, snapshot_id: str) -> None
        # Truncate file to byte_offset stored in snapshot

    # Snapshots
    def save_snapshot(session_id: str, state_json: str) -> str  # returns snapshot_id
    def get_snapshot(session_id: str, snapshot_id: str) -> Optional[str]
```

**File layout:**

```
.agent-context/sessions/
├── <session_id>.jsonl        # active session
├── <session_id>.1.jsonl      # rotated (256 KB overflow)
├── <session_id>.2.jsonl      # rotated
└── <session_id>.snapshots/   # snapshot byte-offsets
    └── <snapshot_id>.json    # {"byte_offset": 12345, "created_at": ...}
```

**Event schema** (one JSON object per line):

```json
{"type": "message",   "ts": 1744000000.0, "role": "user",   "content": "..."}
{"type": "message",   "ts": 1744000001.0, "role": "assistant","content": "..."}
{"type": "tool_call", "ts": 1744000002.0, "name": "read_file", "args": {}, "result": {}}
{"type": "error",     "ts": 1744000003.0, "error_type": "...", "message": "...", "context": {}}
{"type": "plan",      "ts": 1744000004.0, "plan": [...], "status": "active"}
```

**Rotation:** When file exceeds 256 KB, rotate:
`session.jsonl` → `session.1.jsonl` and open fresh `session.jsonl`.
Keep at most 3 rotated files; delete oldest beyond that.

**Fork implementation:**

```python
def fork_session(self, session_id, new_session_id):
    src = self._session_path(session_id)
    dst = self._session_path(new_session_id)
    shutil.copy2(src, dst)  # full copy at this point in time
    # Write a fork-marker event to both files
    fork_event = {"type": "fork", "parent_id": session_id, ...}
    self._append(new_session_id, fork_event)
    return new_session_id
```

**Thread safety:** `threading.Lock` per session_id (dict of locks). New sessions
create a new lock. No global lock needed (sessions are independent files).

**Tests:**
- `tests/unit/test_jsonl_session_store.py` — add 100 messages, read back, assert order.
- Rotation: add messages until > 256 KB, assert rotation occurred.
- Fork: fork session, add messages to child, assert parent unchanged.
- Snapshot + revert: save snapshot, add 5 messages, revert, assert messages gone.
- Concurrent: 4 threads append to same session, assert no corruption.

**Acceptance criteria:**
- `add_message` + `get_messages` round-trip produces correct order.
- File rotation occurs at 256 KB and keeps ≤ 3 rotated files.
- `fork_session` produces an independent copy; writes to child don't affect parent.
- `revert_session` to a snapshot removes all events after snapshot byte_offset.
- Thread-safe: no data corruption under concurrent writes.

---

### TASK-8: Config toggle + migration script for JSONL backend

**Phase:** 4 (Session Storage — backend selection + migration)
**Files:** `src/core/orchestration/orchestrator_bootstrap.py`, `scripts/migrate_sessions.py` (new)
**Effort:** S+S (~2 h total)

**Background:**
After TASK-7 provides the JSONL store, two things are needed: (1) a bootstrap toggle to
select the backend and (2) a migration script to convert existing SQLite session history.

**Part A — Bootstrap toggle (`orchestrator_bootstrap.py`):**

```python
# _init_infrastructure()
import os

def _resolve_storage_backend(orch) -> str:
    # Priority: env var > config file > default
    env_val = os.getenv("CODING_AGENT_STORAGE_BACKEND", "").lower()
    if env_val in ("sqlite", "jsonl"):
        return env_val
    try:
        from src.core.config_loader import load_project_config
        cfg = load_project_config(orch.working_dir or "")
        cfg_val = str(cfg.get("storage_backend", "")).lower()
        if cfg_val in ("sqlite", "jsonl"):
            return cfg_val
    except Exception:
        pass
    return "sqlite"  # default until JSONL is proven in production

backend = _resolve_storage_backend(orch)
if backend == "jsonl":
    from src.core.memory.jsonl_session_store import JsonlSessionStore
    orch.session_db = JsonlSessionStore(workdir=orch.working_dir)
else:
    from src.core.orchestration.session_store import SessionStore
    orch.session_db = SessionStore(workdir=orch.working_dir)
```

**Part B — Migration script (`scripts/migrate_sessions.py`):**

```python
#!/usr/bin/env python3
"""Migrate SQLite session history to JSONL format.

Usage:
    python scripts/migrate_sessions.py [--workdir PATH] [--dry-run]

For each session in the SQLite DB under .agent-context/session.db:
  1. Read all rows from messages, tool_calls, errors, plans tables.
  2. Sort by timestamp.
  3. Write to .agent-context/sessions/<session_id>.jsonl
     (one JSON event per line, same schema as JsonlSessionStore).

Safe to re-run: skips session_ids that already have a .jsonl file.
Does not delete the SQLite DB — run with --delete-sqlite to clean up.
"""

import argparse, json, sqlite3
from pathlib import Path

def migrate(workdir: Path, dry_run: bool, delete_sqlite: bool) -> None:
    db_path = workdir / ".agent-context" / "session.db"
    out_dir  = workdir / ".agent-context" / "sessions"

    if not db_path.exists():
        print(f"No SQLite DB found at {db_path}; nothing to migrate.")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)

    # Collect session IDs
    session_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_id FROM messages"
    ).fetchall()]

    for sid in session_ids:
        out_file = out_dir / f"{sid}.jsonl"
        if out_file.exists():
            print(f"  SKIP {sid} (already migrated)")
            continue

        events = []
        for role, content, ts in conn.execute(
            "SELECT role, content, timestamp FROM messages "
            "WHERE session_id=? ORDER BY timestamp", (sid,)
        ):
            events.append({"type": "message", "ts": ts, "role": role, "content": content})

        for name, args, result, ts in conn.execute(
            "SELECT tool_name, args_json, result_json, timestamp FROM tool_calls "
            "WHERE session_id=? ORDER BY timestamp", (sid,)
        ):
            events.append({"type": "tool_call", "ts": ts, "name": name,
                            "args": json.loads(args or "{}"),
                            "result": json.loads(result or "{}")})

        # Sort all events by timestamp
        events.sort(key=lambda e: e.get("ts", 0))

        if dry_run:
            print(f"  DRY-RUN {sid}: {len(events)} events → {out_file}")
        else:
            with out_file.open("w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev) + "\n")
            print(f"  MIGRATED {sid}: {len(events)} events → {out_file}")

    conn.close()
    if delete_sqlite and not dry_run:
        db_path.unlink()
        print(f"Deleted {db_path}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=".", help="Project working directory")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delete-sqlite", action="store_true")
    args = ap.parse_args()
    migrate(Path(args.workdir).resolve(), args.dry_run, args.delete_sqlite)
```

**Migration workflow for users:**

```bash
# 1. Migrate existing history
python scripts/migrate_sessions.py --workdir /path/to/project

# 2. Verify output
ls .agent-context/sessions/

# 3. Switch backend
export CODING_AGENT_STORAGE_BACKEND=jsonl
# or add to .agent-context/config.json: {"storage_backend": "jsonl"}

# 4. Once confident, clean up SQLite
python scripts/migrate_sessions.py --workdir . --delete-sqlite
```

**Default flip schedule:** Flip default from `"sqlite"` to `"jsonl"` after TASK-7
acceptance criteria pass + 2-sprint production soak. Change the default string in
`_resolve_storage_backend()`.

**Tests:**
- `tests/unit/test_storage_backend_toggle.py`:
  - `CODING_AGENT_STORAGE_BACKEND=jsonl` → `JsonlSessionStore` instantiated.
  - No env var → `SessionStore` instantiated.
  - `config.json#storage_backend=jsonl` → `JsonlSessionStore` instantiated.
- `tests/unit/test_migrate_sessions.py`:
  - Create in-memory SQLite with 3 sessions, run migrate, verify JSONL output.
  - Re-run on already-migrated sessions → no duplicate events.
  - `--dry-run` → files not created.

---

### TASK-8b: Extend `ShellHookRunner` with per-tool `matcher` field

**Phase:** 3 (Hook System — granularity extension)
**File:** `src/core/orchestration/shell_hooks.py`
**Effort:** S (~1.5 h)

**Design decision (locked — Q5):** Extend `ShellHookRunner` in-place rather than
creating a wrapper class. All shell-execution logic stays in one place. The `matcher`
field is optional — hooks without it keep their existing behaviour (run for all tools).

**Background:**
`ShellHookRunner` loads hook commands from `.agent/settings.json` under the keys
`"PreToolUse"` and `"PostToolUse"`. Currently each entry is a plain command string;
there is no per-tool filtering. The plan requires matcher support so users can write:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "bash",      "command": "./pre-bash.sh"},
      {"matcher": "write_*",   "command": "./pre-write.sh"},
      {"matcher": "*",         "command": "./audit-log.sh"}
    ]
  }
}
```

**Schema change (backward compatible):**

Each hook entry is now either:
- A plain string `"./script.sh"` (existing — runs for all tools, matcher = `"*"`)
- A dict `{"matcher": "bash", "command": "./script.sh"}` (new)

**Implementation in `shell_hooks.py`:**

```python
# _load_hooks_config — update parser to normalise both forms into dicts:

def _normalise_entry(entry: Any) -> Optional[Dict[str, str]]:
    """Normalise a hook config entry to {"matcher": str, "command": str}."""
    if isinstance(entry, str):
        return {"matcher": "*", "command": entry}
    if isinstance(entry, dict) and "command" in entry:
        return {
            "matcher": str(entry.get("matcher", "*")),
            "command": str(entry["command"]),
        }
    return None  # malformed — skip

# _run_commands — add matcher check before spawning subprocess:

def _run_commands(event, commands, tool_name, ...):
    for entry in commands:
        matcher = entry.get("matcher", "*")
        cmd     = entry.get("command", "")
        if not cmd:
            continue
        if not fnmatch.fnmatch(tool_name.lower(), matcher.lower()):
            continue   # TASK-8b: skip if tool name doesn't match
        # ... existing subprocess logic unchanged ...
```

**`_load_hooks_config` return type** changes from `Dict[str, List[str]]` to
`Dict[str, List[Dict[str, str]]]`. Internal consumers (`_run_commands`) are updated
accordingly. The public `run_pre` / `run_post` / `async_run_post` signatures are
unchanged.

**Tests:**
- `tests/unit/test_shell_hooks_matcher.py`:
  - Matcher `"bash"`: hook fires for `tool_name="bash"`, not for `tool_name="write_file"`.
  - Matcher `"write_*"`: hook fires for `write_file`, not for `read_file`.
  - Matcher `"*"` (default, plain string entry): hook fires for every tool.
  - Dict entry with no `matcher` key defaults to `"*"`.
  - Malformed entry (no `command` key) is silently skipped.

**Acceptance criteria:**
- Plain string entries continue to run for all tools (no regression).
- `{"matcher": "bash", "command": "..."}` runs only when `tool_name == "bash"`.
- `{"matcher": "write_*", "command": "..."}` runs for `write_file`, `write_to_path`, etc.
- `reload()` invalidates the config cache; new matcher entries take effect immediately.
- Zero changes to the `run_pre` / `run_post` public interface.

---

### TASK-9: MCP SSE transport

**Phase:** 5.2 (Multi-Transport MCP — Server-Sent Events)
**File:** `src/core/mcp/mcp_client.py`
**Effort:** M (~4 h)

**Background:**
`mcp_client.py` is stdio-only with a comment "HTTP/SSE transport deferred to S3-A-http".
SSE is the transport used by browser-accessible MCP servers and some hosted tools.

**Implementation:**

Add a `McpSseClient` class alongside `McpStdioClient`:

```python
class McpSseClient:
    """MCP client for Server-Sent Events transport (HTTP streaming)."""

    def __init__(self, name: str, url: str, headers: Dict[str, str] = None):
        self._name = name
        self._url = url  # e.g. "http://localhost:3000/sse"
        self._headers = headers or {}
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(headers=self._headers)
        # Send initialize over HTTP POST, receive via SSE stream
        await self._initialize()
        await self._discover_tools()

    async def call_tool(self, name: str, args: Dict[str, Any]) -> McpToolResult:
        # HTTP POST to /messages endpoint, read response via SSE
        ...

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()
```

**Transport selection factory:**

```python
def create_mcp_client(config: Dict[str, Any]) -> Union[McpStdioClient, McpSseClient, McpWsClient]:
    transport = config.get("transport", "stdio")
    if transport == "stdio":
        return McpStdioClient(name=config["name"], cmd=config["command"])
    elif transport == "sse":
        return McpSseClient(name=config["name"], url=config["url"],
                            headers=config.get("headers", {}))
    elif transport in ("websocket", "ws"):
        return McpWsClient(name=config["name"], url=config["url"])
    raise ValueError(f"Unknown MCP transport: {transport!r}")
```

**Config example** (`.agent-context/config.json`):

```json
{
  "mcp": {
    "servers": {
      "hosted-tool": {
        "transport": "sse",
        "url": "http://localhost:3000/sse",
        "headers": {"Authorization": "Bearer token"}
      }
    }
  }
}
```

**Dependencies:** `aiohttp` (already in requirements for async HTTP).

**Tests:**
- `tests/unit/test_mcp_sse_transport.py` — mock aiohttp session, verify `initialize`
  handshake, `list_tools`, and `call_tool` produce correct JSON-RPC payloads.

**Acceptance criteria:**
- `create_mcp_client({"transport": "sse", "url": "..."})` returns a `McpSseClient`.
- `connect()` performs `initialize` handshake and discovers tools.
- `call_tool()` sends correct JSON-RPC 2.0 payload and parses response.
- `disconnect()` closes the HTTP session cleanly.

---

### TASK-10: MCP WebSocket transport

**Phase:** 5.2 (Multi-Transport MCP — WebSocket)
**File:** `src/core/mcp/mcp_client.py`
**Effort:** M (~3 h)

**Background:**
WebSocket MCP servers offer lower latency than SSE for bidirectional tool streaming.
Used by some local MCP proxies and orchestration frameworks.

**Implementation:**

```python
class McpWsClient:
    """MCP client for WebSocket transport (bidirectional)."""

    def __init__(self, name: str, url: str, headers: Dict[str, str] = None):
        self._name = name
        self._url = url  # e.g. "ws://localhost:3000/ws"
        self._headers = headers or {}
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._pending: Dict[int, asyncio.Future] = {}  # request_id → future
        self._recv_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        session = aiohttp.ClientSession()
        self._ws = await session.ws_connect(self._url, headers=self._headers)
        self._recv_task = asyncio.create_task(self._recv_loop())
        await self._initialize()
        await self._discover_tools()

    async def _recv_loop(self) -> None:
        """Receive loop: route responses to pending futures."""
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                req_id = data.get("id")
                if req_id in self._pending:
                    self._pending[req_id].set_result(data)

    async def call_tool(self, name: str, args: Dict[str, Any]) -> McpToolResult:
        req_id = self._next_id()
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        await self._ws.send_json({"jsonrpc": "2.0", "id": req_id,
                                   "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})
        response = await asyncio.wait_for(future, timeout=60)
        return McpToolResult.from_response(response)

    async def disconnect(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            await self._ws.close()
```

**Tests:**
- `tests/unit/test_mcp_ws_transport.py` — mock aiohttp WebSocket, verify bidirectional
  JSON-RPC, verify pending future resolution, verify timeout handling.

**Acceptance criteria:**
- `connect()` → WS connection established + `initialize` complete.
- `call_tool()` sends request and waits on future resolved by `_recv_loop`.
- Concurrent calls to `call_tool()` work correctly (each with its own `req_id`).
- `disconnect()` cancels recv_task and closes WS cleanly.

---

## Design Decisions Log

All open questions are resolved. Decisions are incorporated into the relevant task
descriptions above.

| Q | Topic | Decision |
|---|---|---|
| Q1 | Frontier loop + plan mode | **(a) Pause and resume.** Set `awaiting_plan_approval=True`, store pending tool call in `frontier_loop_pending_tool`, return to `wait_for_user_node`. Resume by replaying the pending tool on re-entry. |
| Q2 | Replan path for frontier tier | **(b) Direct re-entry.** `evaluation_node` failure appends failure reason to `history` and routes straight back into `frontier_loop`. `replan_node`/`planning_node` are not used. Safety valve: `replan_attempts ≥ 5` → `memory_sync`. |
| Q3 | Graph recompilation strategy | **(c) Per-tier cache.** `_GRAPH_CACHE: Dict[str, CompiledStateGraph]` keyed by `"standard:<tier>"`. Up to 5 entries. Tier switches apply only at task boundaries (`start_new_task()`), not mid-task. |
| Q4 | JSONL migration path | **(a) Migration script.** `scripts/migrate_sessions.py` does a one-shot SQLite → JSONL conversion. No read-shim in the orchestrator. Users run the script manually before flipping `CODING_AGENT_STORAGE_BACKEND=jsonl`. |
| Q5 | Hook matcher granularity | **Extend `ShellHookRunner` in-place.** Add optional `matcher` field to each hook entry. Plain string entries (`"./script.sh"`) default to `matcher="*"` for backward compatibility. |

---

## Summary

| Task | Phase | Effort | Depends on | Status |
|---|---|---|---|---|
| TASK-1: Add CLAUDE.md to `_CANDIDATE_NAMES` | 1 | XS | — | Ready |
| TASK-2: `src/core/interfaces.py` Protocol classes | 5.1 | S | — | Ready |
| TASK-3: `permission_kind` on `@tool` decorator + all tools | 2 | M | TASK-2 | Ready |
| TASK-4: Wire `PermissionPolicy` into `PermissionGateway` Gate 3 | 2 | S | TASK-3 | Ready |
| TASK-5: `frontier_loop_node.py` | 6 | L | TASK-2 | Ready |
| TASK-6: Tier-based graph cache + routing in `builder.py` | 6 | M | TASK-5 | Ready |
| TASK-7: `jsonl_session_store.py` | 4 | L | — | Ready |
| TASK-8: Storage backend toggle (`orchestrator_bootstrap.py`) | 4 | S | TASK-7 | Ready |
| TASK-8b: `scripts/migrate_sessions.py` | 4 | S | TASK-7 | Ready |
| TASK-8c: `ShellHookRunner` matcher field | 3 | S | — | Ready |
| TASK-9: MCP SSE transport | 5.2 | M | — | Ready |
| TASK-10: MCP WebSocket transport | 5.2 | M | TASK-9 | Ready |

**Recommended execution order (dependency-respecting, risk-ascending):**

```
Sprint 1 (low-risk, high-value):
  TASK-1  → TASK-2  → TASK-3  → TASK-4
  TASK-8c (ShellHookRunner matcher — isolated, no deps)

Sprint 2 (medium complexity):
  TASK-7  → TASK-8 + TASK-8b (JSONL store + toggle + migration)
  TASK-9  → TASK-10 (MCP transports — independent of everything else)

Sprint 3 (high complexity — frontier path):
  TASK-5  → TASK-6 (frontier_loop_node → tier-based routing)
```

*Updated 2026-04-12 with resolved design decisions.*
