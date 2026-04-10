# Code Quality Audit — `src/core/`

**Date:** 2026-04-06
**Scope:** All Python files under `src/core/` (103 files audited)
**Categories:**
1. TODO / FIXME / HACK / XXX comments
2. `raise NotImplementedError` or stub/placeholder implementations
3. Dead code / unreachable branches
4. Known bugs called out in comments (`BUG:`, `BROKEN:`, `WORKAROUND:`)
5. `# noqa` / `# type: ignore` indicating known type issues
6. Hardcoded placeholder values
7. Unnecessary `import` inside function bodies
8. Obvious logic errors
9. Redundant / wasteful code patterns
10. Incomplete async/await patterns

---

## `src/core/config_loader.py`

| Line | Category | Description |
|------|----------|-------------|
| 211 | #5 noqa / type: ignore | `# type: ignore[import]  # noqa: F401  # type: ignore[import]` — duplicate `type: ignore` annotation on the same line; the second one is a no-op |
| 404 | #9 Redundant | `json.loads(raw) if isinstance(json.loads(raw), list) else []` — calls `json.loads(raw)` twice; the second parse is redundant and wasteful |

---

## `src/core/logger.py`

| Line | Category | Description |
|------|----------|-------------|
| 108–118 | #7 Import inside function | `import sys` and `import logging` appear inside the `log()` method body — these are standard library modules that should be top-level imports |

---

## `src/core/startup.py`

| Line | Category | Description |
|------|----------|-------------|
| 62 | #8 Logic error | Bare `raise` inside `except Exception` after catching `asyncio.TimeoutError` re-raises all non-timeout exceptions without any logging, silently swallowing context about what went wrong |

---

## `src/core/user_prefs.py`

| Line | Category | Description |
|------|----------|-------------|
| ~45–80 | #8 Logic error | `selected_model_provider`, `selected_model_name`, and `active_mode` are defined both as plain instance attributes in `__init__` AND as `@property` descriptors. The `__init__` assignments invoke the property setters (writing to `self.data`), but the getters also read from `self.data` — creating a redundant double-write on construction and masking the fact that direct attribute assignment silently goes through the setter |

---

## `src/core/context/context_builder.py`

| Line | Category | Description |
|------|----------|-------------|
| 572 | #7 Import inside function | `import logging as _logging` inside the `build_prompt()` function body — should be a module-level import |
| 662 | #5 type: ignore | `from src.core.indexing.lsp_context import ...  # type: ignore[import]` — known type resolution issue |

---

## `src/core/context/context_controller.py`

| Line | Category | Description |
|------|----------|-------------|
| 232–234 | #3 Dead code | `ContextController.extract_relevant_snippets = ContextController.extract_relevant_snippets` — a self-assignment at module level that is a complete no-op |
| 235 | #5 type: ignore | `# type: ignore[attr-defined]` on the alias assignment confirms the type checker rejects this pattern |

---

## `src/core/indexing/vector_store.py`

| Line | Category | Description |
|------|----------|-------------|
| 191–192 | #7 Import inside function | `from typing import List as PyList` inside the `add_memory()` method body — unnecessary; `List` is already importable at module level |

---

## `src/core/indexing/repo_indexer.py`

| Line | Category | Description |
|------|----------|-------------|
| 478–480 | #8 Logic error | `get_symbols_for_task()` reads from `file_entry.get("symbols", [])` but the index stores symbols at `repo_index["symbols"]` (top-level list), not nested under individual file entries. The lookup will always return `[]`, effectively making the function a no-op for symbol retrieval |

---

## `src/core/indexing/symbol_graph.py`

| Line | Category | Description |
|------|----------|-------------|
| 337–338 | #7 Import inside function | `import re` inside the `find_calls()` method body — `re` is a standard library module and should be a module-level import |

---

## `src/core/indexing/lsp_manager.py`

| Line | Category | Description |
|------|----------|-------------|
| 171 | #8 Logic error / #10 Async | `_MANAGER_LOCK: asyncio.Lock | None = None` is lazily created in `_get_manager_lock()`, but `get_lsp_manager()` accesses the shared `_MANAGERS` dict without ever acquiring the lock. In concurrent async contexts this creates an unguarded race condition on `_MANAGERS` |
| 56 | #5 type: ignore | `import yaml  # type: ignore[import]` — optional dependency, expected |

---

## `src/core/inference/adapters/github_copilot_auth.py`

| Line | Category | Description |
|------|----------|-------------|
| 30 | #6 Hardcoded value | `GITHUB_CLIENT_ID = "Ov23li8tweQw6odWQebz"` — OAuth client ID committed in plain text to source. Even if intentional (public app), this should be in config or env |

---

## `src/core/inference/adapters/ollama_adapter.py`

| Line | Category | Description |
|------|----------|-------------|
| 283 | #8 Logic error | Uses `print("Error: No models to load.")` and `print("Error: Unable to find a model.")` instead of the logger — errors are written to stdout rather than the structured log stream, breaking structured log aggregation |

---

## `src/core/inference/adapters/lm_studio_adapter.py`

| Line | Category | Description |
|------|----------|-------------|
| ~40–80 | #9 Redundant | `providers.json` is loaded twice in `__init__`: once via `_providers_from_config()` and again when building `self.provider`. The second load duplicates I/O for the same file on every instantiation |

---

## `src/core/inference/adapters/openai_compat_adapter.py`

| Line | Category | Description |
|------|----------|-------------|
| 329–338 | #7 Import inside function | `import os as _os` inside the `_chat_internal()` method body — should be a module-level import |
| 341–344 | #4 Known bug / workaround | `tools` list is unconditionally copied to the `functions` key in the request body. The comment acknowledges this is a workaround for OpenAI compatibility, but it breaks providers that do not understand the `functions` key |

---

## `src/core/inference/llm_manager.py`

| Line | Category | Description |
|------|----------|-------------|
| 993, 1009 | #7 Import inside function | `from functools import partial` and `from functools import partial as _partial` appear inside the `_call_model_internal()` async function body |
| 1020, 1035 | #7 Import inside function | Two more `from functools import partial` / `from functools import partial as _partial` inside the same `_call_model_internal()` function — four redundant intra-function imports of the same symbol |
| 1165 | #7 Import inside function | `import json as _json` inside the `_consume_sse_stream()` function body |
| 1428–1432 | #7 Import inside function | `from src.core.orchestration.token_budget import get_token_budget_monitor` inside `call_model()` — deferred import inside a hot path |

---

## `src/core/inference/telemetry.py`

| Line | Category | Description |
|------|----------|-------------|
| 67–68 | #7 Import inside function | `from src.core.orchestration.event_bus import get_event_bus` inside the `wrapper()` closure inside `with_telemetry()` — called on every instrumented LLM call |

---

## `src/core/memory/advanced_features.py`

| Line | Category | Description |
|------|----------|-------------|
| 172 | #7 Import inside function | `import ast` inside the `detect_code_smells()` method body |

---

## `src/core/memory/distiller.py`

| Line | Category | Description |
|------|----------|-------------|
| 35–38 | #7 Import inside function | `import asyncio`, `import inspect`, `import concurrent.futures`, and `from src.core.inference.llm_manager import call_model` all inside `_call_llm_sync()` — these are called every time distillation runs |
| 308 | #7 Import inside function | `import re` inside `distill_context()` |
| 353 | #7 Import inside function | `import json as _json` inside `distill_context()`, shadowing the top-level `import json` that is already present |
| 496 | #7 Import inside function | `from src.core.orchestration.agent_types import get_agent_registry` inside `call_internal_agent()` |

---

## `src/core/memory/session_store.py`

| Line | Category | Description |
|------|----------|-------------|
| 357–359 | #7 Import inside function | `import json as _json`, `import os as _os`, `import tempfile as _tempfile` inside `write_decisions_json()` — all are already imported at the module level or are standard library; aliases serve no purpose |
| 415 | #7 Import inside function | `import json as _json` inside `read_recent_decisions()` — the top-level `import json` is already available |
| 573 | #7 Import inside function | `import uuid as _uuid` inside `fork_session()` |

---

## `src/core/orchestration/orchestrator.py`

| Line | Category | Description |
|------|----------|-------------|
| 98–100 | #7 Import inside function | `import json as _json`, `import datetime as _dt`, `from pathlib import Path as _Path` inside `_write_permission_audit()` — standard-library modules imported on every audit write |
| 123, 142, 253 | #7 Import inside function | `import subprocess as _sp` imported inside three separate functions; should be a module-level import |
| 360–361 | #7 Import inside function | `import inspect` and `import re` inside a function body |
| 1262 | #7 Import inside function | `import threading as _threading` inside `initialize_async()` |
| 1549 | #7 Import inside function | `import difflib as _dl` inside an inline helper |
| 1573 | #7 Import inside function | `import re as _re` inside a nested function |
| 1630 | #7 Import inside function | `from datetime import datetime` inside a function body |

---

## `src/core/orchestration/cross_session_bus.py`

| Line | Category | Description |
|------|----------|-------------|
| 82 | #8 Logic error | `print(f"Files from {msg.sender_session_id}: {msg.payload}")` is a debug-leftover `print()` statement in a usage example embedded inside a docstring/comment block, but it's inside actual code — at runtime this prints to stdout rather than using the logger |

---

## `src/core/orchestration/tool_parser.py`

| Line | Category | Description |
|------|----------|-------------|
| 87–88 | #7 Import inside function | `import yaml` and `import datetime` inside a parsing helper |

---

## `src/core/orchestration/rollback_manager.py`

| Line | Category | Description |
|------|----------|-------------|
| 61 | #7 Import inside function | `import hashlib` inside a method body |
| 156, 162 | #5 type: ignore | `# type: ignore[index]` and `# type: ignore[call-overload]` on snapshot dict accesses — indicates a structural mismatch between the declared type and actual usage |

---

## `src/core/orchestration/permission_gateway.py`

| Line | Category | Description |
|------|----------|-------------|
| 106–224 | #7 Import inside function | Eight separate deferred imports of `PlanMode`, `is_tool_allowed_for_role`, `PERMISSION_REQUIRED_TOOLS`, `get_tool_permission`, `PermissionLevel`, `is_autonomous`, and `AsyncGate` — all inside `check_permission()`, which is on the hot path for every tool call |

---

## `src/core/orchestration/graph/nodes/perception_node.py`

| Line | Category | Description |
|------|----------|-------------|
| 61, 214, 322, 351, 356, 459, 497, 604, 628, 808, 1058 | #7 Import inside function | Eleven deferred imports inside `perception_node()` — the most import-heavy node; every invocation re-imports `project_settings`, `SymbolGraph`, `MODIFYING_TOOLS`, `auto_compactor`, `config_loader`, `Path`, `classify_model`, `get_context_budget`, `json`, and `_task_is_complex` |

---

## `src/core/orchestration/graph/builder.py`

| Line | Category | Description |
|------|----------|-------------|
| 1288, 1293 | #5 type: ignore | `# type: ignore[return-value]` on two routing function returns — the return types are incompatible with the declared signature |
| 835, 1100–1101, 1355 | #7 Import inside function | `wait_for_user_node`, `json as _json_wf4`, `hashlib as _hashlib_wf4`, and `get_token_budget_monitor` imported inside functions |

---

## `src/core/orchestration/graph/nodes/memory_update_node.py`

| Line | Category | Description |
|------|----------|-------------|
| 15–16 | #5 type: ignore | `distill_context = None  # type: ignore[assignment]` and `compact_messages_to_prose = None  # type: ignore[assignment]` — optional import fallback that intentionally sets module-level callables to `None`, bypassing type safety |

---

## `src/core/orchestration/graph/nodes/delegation_node.py`

| Line | Category | Description |
|------|----------|-------------|
| 305 | #5 type: ignore | `# type: ignore[union-attr]` on topic value extraction — a known None-safety gap |

---

## `src/core/orchestration/session_store.py` (orchestration)

| Line | Category | Description |
|------|----------|-------------|
| *(no new issues)* | — | Clean file |

---

## `src/core/orchestration/mcp_stdio_server.py`

| Line | Category | Description |
|------|----------|-------------|
| 470, 535, 604 | #8 Logic error | `print(output, flush=True)` and similar — MCP stdio server intentionally uses `print()` for the wire protocol (JSON-RPC over stdout). These are correct by design; however they are mixed with `logger.xxx` calls in the same file with no clear separation, making it easy to accidentally log MCP traffic via the logger and corrupt the stdio stream |
| 358 | #10 Async | `resp = _asyncio.run(...)` inside what appears to be a sync wrapper — if called from a context that already has a running event loop this will raise `RuntimeError` |

---

## `src/core/orchestration/approval_gate.py`

| Line | Category | Description |
|------|----------|-------------|
| 90 | #5 type: ignore | `self._async_event = None  # type: ignore[assignment]` — type declared as `asyncio.Event` but initialized to `None` |

---

## `src/core/orchestration/graph/state.py`

| Line | Category | Description |
|------|----------|-------------|
| 253, 260, 261, 275, 276 | #5 type: ignore | Multiple `# type: ignore[call-overload]` and `# type: ignore[return-value]` on TypedDict `.get()` calls — indicates the state dict accesses are not type-safe and could return unexpected types |

---

## Summary by Category

| Category | Count | Files Affected |
|----------|-------|----------------|
| #5 `# type: ignore` / `# noqa` (known type issues) | 30+ | `orchestrator.py`, `rollback_manager.py`, `builder.py`, `state.py`, `delegation_node.py`, `approval_gate.py`, `memory_update_node.py`, `ollama_adapter.py`, `vector_store.py`, `context_controller.py`, `config_loader.py`, others |
| #7 Import inside function body | 50+ | `orchestrator.py` (13+), `perception_node.py` (11), `permission_gateway.py` (8), `distiller.py` (5), `memory_update_node.py`, `session_store.py`, `llm_manager.py` (5), others |
| #9 Redundant / wasteful code | 4 | `config_loader.py`, `lm_studio_adapter.py`, `openai_compat_adapter.py`, `context_controller.py` |
| #8 Logic errors | 5 | `repo_indexer.py`, `lsp_manager.py`, `startup.py`, `user_prefs.py`, `ollama_adapter.py` |
| #6 Hardcoded values | 1 | `github_copilot_auth.py` |
| #4 Known-bug workaround | 1 | `openai_compat_adapter.py` |
| #3 Dead code | 1 | `context_controller.py` |
| #10 Incomplete async/await | 1 | `mcp_stdio_server.py` |

---

## High-Priority Findings

1. **`repo_indexer.py:478–480`** — `get_symbols_for_task()` always returns empty; symbols are stored at the wrong level of the index. Silent data loss.
2. **`lsp_manager.py:171`** — Race condition: `_MANAGERS` dict accessed without holding `_MANAGER_LOCK` in concurrent async contexts.
3. **`github_copilot_auth.py:30`** — OAuth client ID in source code.
4. **`user_prefs.py`** — Property-and-attribute double-definition causes double-write on construction and will confuse any code that introspects the class.
5. **`startup.py:62`** — Bare `raise` swallows error context for all non-timeout exceptions.
6. **`config_loader.py:404`** — Double `json.loads(raw)` — unnecessary parse on a potentially large config blob.
7. **`llm_manager.py:993–1035`** — Four redundant intra-function imports of `partial` inside the hot-path `_call_model_internal()` async function.
