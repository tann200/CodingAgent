# Code Quality Audit — Vol 40

**Date:** 2026-05-02
**Auditor:** OpenCode (automated structural review)
**Scope:** 20 files across `src/core/orchestration/` (non-graph, non-adapter layer)

---

## Files Audited

| # | File | Lines |
|---|------|-------|
| 1 | `src/core/orchestration/agent_types.py` | 625 |
| 2 | `src/core/orchestration/approval_gate.py` | 343 |
| 3 | `src/core/orchestration/commands.py` | 349 |
| 4 | `src/core/orchestration/context_manager.py` | 251 |
| 5 | `src/core/orchestration/dag_parser.py` | 349 |
| 6 | `src/core/orchestration/event_bus.py` | 409 |
| 7 | `src/core/orchestration/event_log.py` | 366 |
| 8 | `src/core/orchestration/inference_loop.py` | 1096 |
| 9 | `src/core/orchestration/loop_guards.py` | 427 |
| 10 | `src/core/orchestration/message_manager.py` | 411 |
| 11 | `src/core/orchestration/permission_gateway.py` | 622 |
| 12 | `src/core/orchestration/permission_policy.py` | 511 |
| 13 | `src/core/orchestration/permission_table.py` | 344 |
| 14 | `src/core/orchestration/registry_builder.py` | 548 |
| 15 | `src/core/orchestration/session_lifecycle.py` | 549 |
| 16 | `src/core/orchestration/session_registry.py` | 558 |
| 17 | `src/core/orchestration/tool_execution_pipeline.py` | 1104 |
| 18 | `src/core/orchestration/tool_parser.py` | 508 |
| 19 | `src/core/orchestration/token_budget.py` | 220 |
| 20 | `src/core/orchestration/shell_hooks.py` | 498 |

**Total lines audited:** 10 088

---

## Audit Scope and Exclusions

- **Excluded:** purely stylistic findings (naming, blank-line spacing, docstring formatting).
- **Excluded:** findings that would require significant public API changes.
- **Excluded:** inline imports that are demonstrably guarding against circular imports (e.g., `inference_loop.py` L43 `model_tiers` import inside `_compute_default_max_turns`, and L173/L349/L1009 `llm_manager` imports which are deferred because `llm_manager` imports orchestration modules at its own module level).
- **Included:** redundant inline imports where the module is already imported at module level, duplicate logic, dead code, and structural DRY violations.

---

## Summary Table

| ID | Severity | File | Line(s) | Category | Short Description |
|----|----------|------|---------|----------|-------------------|
| M-1 | Medium | `tool_parser.py` | 17, 486 | Redundant inline import | `import re as _re` inside two functions; `re` is already imported at module level (L5) |
| M-2 | Medium | `approval_gate.py` | 56–66, 264–344 | DRY violation | Two structurally identical gate registries (`_bash_*` / `_tool_*`) with six parallel functions; a generic `_GateRegistry` class would eliminate ~80 lines of duplication |
| M-3 | Medium | `token_budget.py` / `context_manager.py` | tb:27–30, cm:59–62 | DRY violation | `usage_ratio` property is defined with identical logic in both `TokenBudget` (token_budget.py) and `ContextSnapshot` (context_manager.py); one should delegate to the other or share a mixin |
| M-4 | Medium | `registry_builder.py` | 78–390 | Swallowed broad exception | All optional tool registrations use bare `except Exception: pass`; since `ToolRegistry.register()` never raises, the try/except blocks that guard `AttributeError`-prone attribute accesses should catch `AttributeError` specifically, not `Exception` |
| L-1 | Low | `token_budget.py` | 60 | Unused singleton field | `TokenBudgetMonitor._instance = None` is the backing field for the `get_instance()` classmethod singleton — this is correct but the singleton is **not thread-safe** (no lock around the `if cls._instance is None` check at L74–76); concurrent first calls can create two instances |
| L-2 | Low | `permission_policy.py` | 35, 185, 190 | FALSE POSITIVE — `Behavior.ASK` is used in `loop_guards.py` and `permission_gateway.py` |
| L-3 | Low | `inference_loop.py` | 170–180 | Repeated deferred import | `from src.core.inference.llm_manager import ...` appears at L173, L349, and L1009 — three independent deferred imports of the same module inside one file; a single module-level conditional import with a fallback would be cleaner and easier to maintain |
| L-4 | Low | `registry_builder.py` | 23–53 | Silent outer fallback | The outer `try/except Exception: pass` (L23/L52) that wraps the entire `build_registry()` delegation silently swallows any import failure and falls through to the manual registration block with no log line; a failure here is hard to diagnose |

---

## Detailed Findings

---

### M-1 — Redundant inline `re` import in `tool_parser.py`

**File:** `src/core/orchestration/tool_parser.py`
**Lines:** 17, 486

`re` is imported at module level on line 5:

```python
import re
```

Two private functions then re-import it locally under an alias:

```python
# line 17, inside _extract_json_object()
import re as _re

# line 486, inside _parse_xml_tool_block()
import re as _re
```

Since `re` is a standard-library module that is already in `sys.modules` after the top-level import, these inline imports are purely redundant — they add an attribute lookup overhead on every call and obscure the true dependency graph.

**Fix:** Remove lines 17 and 486. Replace all `_re.` usages inside those functions with `re.`.

---

### M-2 — Duplicate gate registry in `approval_gate.py`

**File:** `src/core/orchestration/approval_gate.py`
**Lines:** 56–66 (state variables), 264–344 (functions)

The module maintains **two** structurally identical sets of state variables and six parallel functions for bash approvals and tool approvals:

```
_pending_bash / _bash_denied / _bash_result   ←→   _pending_tool / _tool_denied / _tool_result
register_bash_gate()                           ←→   register_tool_gate()
resolve_bash_gate()                            ←→   resolve_tool_gate()
is_bash_denied()                              ←→   is_tool_denied()
```

The bodies of `register_bash_gate` / `register_tool_gate`, `resolve_bash_gate` / `resolve_tool_gate`, and `is_bash_denied` / `is_tool_denied` are word-for-word identical except for which set of dicts/sets they access. This is ~80 lines of pure duplication.

**Fix:** Extract a `_GateRegistry` dataclass or small class that owns `_pending`, `_denied`, and `_result` and exposes `register()`, `resolve()`, and `is_denied()` methods. Replace the six module-level functions with thin wrappers:

```python
_bash_registry = _GateRegistry()
_tool_registry = _GateRegistry()

def register_bash_gate(tool_id: str) -> AsyncGate:
    return _bash_registry.register(tool_id)

def register_tool_gate(tool_id: str) -> AsyncGate:
    return _tool_registry.register(tool_id)
```

---

### M-3 — Duplicate `usage_ratio` property in `token_budget.py` and `context_manager.py`

**Files:** `src/core/orchestration/token_budget.py` L27–30, `src/core/orchestration/context_manager.py` L59–62

Both `TokenBudget` and `ContextSnapshot` define an identical property:

```python
@property
def usage_ratio(self) -> float:
    if self.max_tokens <= 0:
        return 0.0
    return self.used_tokens / self.max_tokens
```

`context_manager.py` already imports `TokenBudget` and `TokenBudgetMonitor` from `token_budget.py` (re-exported via `__init__`). If `ContextSnapshot` is intended to be a view over the same concept as `TokenBudget`, it should either inherit from it or delegate:

```python
@property
def usage_ratio(self) -> float:
    return self._budget.usage_ratio  # if ContextSnapshot wraps a TokenBudget
```

If the two types must remain independent, the shared formula should at minimum be extracted to a module-level utility function to avoid silent divergence if the formula ever changes.

---

### M-4 — Broad `except Exception: pass` masking real errors in `registry_builder.py`

**File:** `src/core/orchestration/registry_builder.py`
**Lines:** throughout fallback registration block (~L78–390)

`ToolRegistry.register()` never raises — it silently overwrites. The `try/except Exception: pass` blocks scattered throughout the manual fallback registration block were originally intended to guard against optional attributes (e.g., `repo_tools.find_references` not existing on older builds). However:

1. Catching `Exception` instead of `AttributeError` hides genuine bugs (e.g., a typo in the attribute name, a broken import inside a tool module, a `TypeError` from a wrong signature).
2. The `pass` produces no log output, making registration failures invisible.

**Fix:** Replace `except Exception: pass` with `except AttributeError: pass` where the guard is purely for optional attributes, and add a `logger.debug()` or `logger.warning()` call so failures are traceable.

---

### L-1 — Non-thread-safe singleton in `token_budget.py`

**File:** `src/core/orchestration/token_budget.py`
**Lines:** 60, 74–76

`TokenBudgetMonitor` implements a singleton via:

```python
class TokenBudgetMonitor:
    _instance = None

    @classmethod
    def get_instance(cls) -> "TokenBudgetMonitor":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
```

This check-then-set pattern is not thread-safe. Two threads calling `get_instance()` simultaneously during the first call can both observe `_instance is None` and each create a separate instance. The orchestration layer is async (asyncio) but also uses `threading.Lock` elsewhere (e.g., `approval_gate.py`), so thread-safety matters.

**Fix:** Add a module-level `threading.Lock` and double-checked locking, or use `functools.lru_cache(maxsize=None)` on a module-level factory function.

---

### L-2 — `Behavior.ASK` differentiation in `permission_policy.py` — FALSE POSITIVE

**File:** `src/core/orchestration/permission_policy.py`

`Behavior.ASK` is actively used in `loop_guards.py:374` and `permission_gateway.py:421`.
The audit finding was incorrect — this is live code, not dead code. No fix needed.

---

### L-3 — Three independent deferred imports of `llm_manager` in `inference_loop.py` — DEFERRED

**File:** `src/core/orchestration/inference_loop.py`
**Lines:** 173, 349, 1009

Each import is in a separate function importing different symbols (`get_provider_manager`,
`get_provider_manager` + `set_active_context_length`, `call_model`). Deferred imports here
guard against a genuine circular import cycle (llm_manager ↔ orchestration layer). Consolidating
into a single lazy accessor would require refactoring all three call sites and is out of scope.

Or use a module-level `TYPE_CHECKING` guard for type annotations and a single lazy import at the start of the first function that needs it, caching the result in a module-level variable after first load.

---

### L-4 — Silent outer fallback in `registry_builder.py`

**File:** `src/core/orchestration/registry_builder.py`
**Lines:** 23, 52

The entire `build_registry()` delegation is wrapped in:

```python
try:
    from src.tools._registry import build_registry as _build
    ...
    return reg
except Exception:
    pass  # Fall through to manual registration below
```

If `build_registry` fails for any reason (broken tool module, import error, data-shape mismatch), the exception is silently swallowed and execution continues into the manual fallback with no log line. The resulting registry may be incomplete and callers have no way to know.

**Fix:** Add a `logger.warning("registry auto-discovery failed, using manual fallback: %s", e)` before `pass` so that fallback activations are visible in logs.

---

## Clean Files

The following files had no reportable findings under the audit scope:

| File | Notes |
|------|-------|
| `agent_types.py` | Clean. `AgentDefinition` dataclass and registry are well-structured; built-in agent list is data-driven. |
| `commands.py` | Clean. Slash-command registry is straightforward; no duplication or dead code found. |
| `dag_parser.py` | Clean. `PlanDAG` parsing is correct; AST analysis confirmed all imports are used. |
| `event_bus.py` | Clean. Thread-safe in-process pub/sub with correlation IDs; all imports confirmed used. |
| `event_log.py` | Clean. SQLite append-only log with proper WAL configuration and parameterised queries. |
| `loop_guards.py` | Clean. Pure functions; no side effects, no dead code found. |
| `message_manager.py` | Clean. Token-windowed message store; all imports confirmed used. |
| `permission_gateway.py` | Clean. Five-gate sequential check is clear; a prior audit already promoted deferred imports to module level (noted in comment at L38–40). |
| `permission_table.py` | Clean. Inline import in `_default_db_path` is a legitimate circular-import guard. |
| `session_lifecycle.py` | Clean. Graceful shutdown and state persistence logic is well-separated. |
| `session_registry.py` | Clean. Central session registry singleton with appropriate locking. |
| `shell_hooks.py` | Clean. Lazy settings load with `reload()` is a deliberate design; no issues found. |
| `tool_execution_pipeline.py` | Clean. Large file (1 104 lines) but well-decomposed; no structural duplication found within scope. |
