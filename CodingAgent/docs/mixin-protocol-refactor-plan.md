# Mixin → Protocol Refactor Plan

## Context

The codebase currently uses two distinct "mixin" patterns — both cause mypy
`"object" has no attribute …` errors and make the inheritance graph hard to
reason about:

### Pattern A — TUI Mixin classes (Textual `App` subclasses)

Files: `tui/tui_src/ui/_bridge_*.py`, `tui/tui_src/ui/_app_*_mixin.py`

```
class AgentApp(App, BridgeAgentMixin, BridgeProviderMixin, ...):
    ...
```

Each mixin inherits from bare `object` and calls `self._post(...)`,
`self._subscribe(...)`, etc. — methods that exist on `AgentApp` but not on
`object`. Mypy cannot verify this because `object` has no such attributes.

### Pattern B — Inference layer duck-typed collaborators

Files: `src/core/inference/provider_config.py`, `provider_discovery.py`,
`provider_loading.py`, `provider_probe.py`

```python
def set_provider_active_flag(*, lock: object, logger: object, ...) -> None:
    with lock:          # ← "object" has no attribute "__enter__"
        logger.debug()  # ← "object" has no attribute "debug"
```

Functions accept `object` to avoid circular imports from `LLMManager`, but lose
all structural type information in the process.

---

## Current state (after this session's fixes)

- **Pattern B** is fully resolved: `LockProtocol`, `LoggerProtocol`,
  `ProviderManagerProtocol`, `CLIContextProtocol` now exist in
  `src/core/inference/_protocols.py` and `src/core/orchestration/_protocols.py`.
  All four inference helper files use them.
- **Pattern A** (TUI mixins) is the remaining work.

---

## Remaining mypy errors after this session

| Category | Count | Root cause |
|----------|-------|-----------|
| `"object" has no attribute` (TUI mixins) | 23 | Pattern A — `BridgeXxxMixin` inherits `object`, calls `self._post` |
| `Cannot assign to a type` | 8 | Conditional import pattern: `cls = None` then `cls = importlib.import_module(…)` |
| `All conditional function variants must have identical signatures` | 5 | `ollama_adapter.py` / `llm_manager.py` — try/except redefinitions |
| `Incompatible import` / `no-redef` | 4 | `llm_client.py` try/except import aliasing |
| `truthy-function` / `Incompatible assignment` | 3 | `orchestrator_helpers.py`, `adapter_wrappers.py` |
| `arg-type` / `return-value` misc | 8 | Planning node kwargs splat, delegation node gather |

Total: **69** (down from 133 at start of session)

---

## Plan A — TUI mixin refactor (Protocol-based)

### Goal

Each mixin declares the interface it _requires_ from its host class via a
`Protocol`, and the host (`AgentApp`) is annotated to satisfy all of them.
mypy can then verify every mixin call site structurally.

### Step 1 — Create `tui/tui_src/ui/_app_protocol.py`

Define one master `AgentAppProtocol` (a `Protocol`) that aggregates all
attributes and methods that mixins depend on:

```python
from typing import Any, Protocol, runtime_checkable
from textual.message import Message

@runtime_checkable
class AgentAppProtocol(Protocol):
    """Structural interface shared by all AgentApp mixins."""
    # from BridgeSubscriptionsMixin
    def _subscribe(self, topic: str, handler: Any) -> None: ...
    def _post(self, message: Message) -> None: ...
    # from BridgeProviderMixin
    _working_dir: str
    # … etc.
```

### Step 2 — Update each mixin base class

Change:
```python
class BridgeAgentMixin:           # inherits object
```
to:
```python
class BridgeAgentMixin(AgentAppProtocol):  # Protocol base → mypy can resolve self.X
```

> **Why `Protocol` as base?** When a class inherits from a `Protocol` *without*
> `runtime_checkable`, it becomes a *partial implementation*. mypy resolves
> `self._post(...)` because the Protocol declares it. This is the idiomatic
> pattern for mixins in typed Python — see PEP 544.

### Step 3 — Keep `AgentApp` as the concrete implementation

```python
class AgentApp(
    App,
    BridgeAgentMixin,
    BridgeProviderMixin,
    BridgeSessionMixin,
    BridgeContextMixin,
    BridgeToolsMixin,
    BridgeSubscriptionsMixin,
    AppSlashCommandsMixin,
    AppStatusHandlersMixin,
    AppMessageHandlersMixin,
    AppToolHandlersMixin,
    AppSessionMixin,
):
    ...
```

No changes to `AgentApp` itself — it already provides all the concrete
implementations. The Protocol just tells mypy what to expect.

### Step 4 — Sync `tui/src/` mirror

After every edit to `tui/tui_src/ui/`, copy the changed files to `tui/src/ui/`.

### Files to touch

| File | Change |
|------|--------|
| `tui/tui_src/ui/_app_protocol.py` | **NEW** — `AgentAppProtocol` |
| `tui/tui_src/ui/_bridge_agent.py` | `class BridgeAgentMixin(AgentAppProtocol):` |
| `tui/tui_src/ui/_bridge_context.py` | same |
| `tui/tui_src/ui/_bridge_provider.py` | same |
| `tui/tui_src/ui/_bridge_session.py` | same |
| `tui/tui_src/ui/_bridge_subscriptions.py` | same |
| `tui/tui_src/ui/_bridge_tools.py` | same |
| `tui/tui_src/ui/_app_slash_commands_mixin.py` | same |
| `tui/tui_src/ui/_app_status_handlers_mixin.py` | same |
| `tui/tui_src/ui/_app_message_handlers_mixin.py` | same |
| `tui/tui_src/ui/_app_tool_handlers_mixin.py` | same |
| `tui/tui_src/ui/_app_session_mixin.py` | same |
| `tui/tui_src/ui/components/status_bar.py` | `StatusBarMixin(AgentAppProtocol)` |
| `tui/tui_src/ui/components/chat_mixin.py` | `ChatDisplayMixin(AgentAppProtocol)` |
| `tui/src/ui/` | mirror all above |

### Estimated effort

~3–4 hours (mostly discovery of which attributes each mixin uses, and adding
them to the Protocol).

### Risk

**Low** — no runtime behaviour changes. Protocol inheritance is purely
structural for mypy; it adds no `__init__` or MRO changes. Existing tests will
continue to pass unchanged.

---

## Plan B — Remaining non-mixin mypy errors

These are independent of the mixin refactor and can be done in any order:

### B1 — `Cannot assign to a type` (8 errors)

**Pattern:**
```python
try:
    from foo import Bar
except ImportError:
    Bar = None          # ← mypy: "Cannot assign to a type"
```

**Fix:** Use `Optional[type]` annotation:
```python
_Bar: Optional[type] = None
try:
    from foo import Bar as _Bar
except ImportError:
    pass
```

**Files:** `permission_gateway.py` (4), `perception_node.py` (2),
`builder.py` (1), `dag_parser.py` (1)

### B2 — `All conditional function variants must have identical signatures` (5 errors)

**Pattern** in `ollama_adapter.py` and `llm_manager.py`:
```python
try:
    def run_with_corr(loop, executor, fn, *args): ...
except ImportError:
    def run_with_corr(loop, executor, fn, *args): ...  # slightly different signature
```

**Fix:** Use a single `Optional` variable + late binding:
```python
_run_with_corr: Optional[Callable[..., Any]] = None
try:
    from src.core.utils import run_with_correlation as _run_with_corr
except ImportError:
    pass

def run_with_corr(loop, executor, fn, *args):
    if _run_with_corr is not None:
        return _run_with_corr(loop, executor, fn, *args)
    return asyncio.get_event_loop().run_in_executor(executor, fn, *args)
```

**Files:** `ollama_adapter.py`, `llm_manager.py`

### B3 — `llm_client.py` incompatible import aliasing (2 errors)

Same pattern as B2 — `run_with_corr` imported conditionally with two
incompatible type signatures. Fix with single `Optional` + runtime dispatch.

### B4 — Planning node kwargs splat (10 errors)

`planning_node.py:121–122` passes `**dict[str, object]` to functions that
expect specific keyword args. Fix: narrow the dict type before splatting, or
use explicit keyword arguments at the call site.

### B5 — `delegation_node.py` gather typing (4 errors)

`asyncio.gather(..., return_exceptions=True)` returns `list[T | BaseException]`.
Destructured variables need explicit `object` annotations (already done for
`perception_retrieval.py` — apply same pattern here).

### B6 — `orchestrator_helpers.py` truthy-function (1 error)

```python
if get_provider_manager:    # ← function is always truthy
```
Fix: `if get_provider_manager is not None:` or call it: `if get_provider_manager():`

---

## Execution order recommendation

1. **B1** (assign-to-type) — quick wins, ~30 min
2. **B6, B3** — isolated fixes, ~20 min each
3. **B2** (conditional function redefinition) — medium complexity, ~1 hr
4. **Plan A** (TUI Protocol mixin) — largest effort, ~3–4 hr, do in a
   dedicated session after B1–B3 are merged and green
5. **B4** (planning node kwargs) — requires reading `save_last_plan` signature,
   ~1 hr
6. **B5** (delegation gather) — straightforward annotation, ~20 min

After all steps: expected mypy errors ≤ 15 (remaining stubs + `annotation-unchecked` notes).
