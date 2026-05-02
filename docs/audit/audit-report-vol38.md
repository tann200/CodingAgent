# Audit Report — Vol38

**Scope:** `src/core/inference/llm_manager.py`, `llm_helpers.py`, `provider_context.py`,
`src/core/orchestration/orchestrator.py`, `orchestrator_helpers.py`, `plan_mode.py`,
`session_manager.py`, `tool_execution_service.py`,
`src/core/context/context_builder.py`,
`src/core/memory/session_store.py`, `distiller.py`,
`src/core/indexing/symbol_graph.py`, `src/core/telemetry/tracer.py`

---

## High Findings

### H-1 — `save_provider` re-imports `tempfile`/`os`/`shutil` inline (llm_manager.py:584–586)
`save_provider` contains `import tempfile`, `import os`, `import shutil` inside the function
body.  All three are already at module level (lines 15–17).  The inline copies are dead weight
executed on every fallback write path.

**Fix:** Remove the three inline imports inside `save_provider`.

### H-2 — `_camelize` helper defined inside `initialize()` loop (llm_manager.py:1056–1058)
The inner function `_camelize` is redefined on **every iteration** of the provider loop inside
`ProviderManager.initialize()`.  This allocates a new closure object per provider entry.

**Fix:** Hoist `_camelize` to module level (or class level outside the loop).

### H-3 — Duplicate docstring in `get_session_store` (session_store.py:86–95)
`get_session_store` has two docstrings — the public-facing one (lines 88–91) and a second
implementation-detail docstring (lines 92–95) that immediately follows the first.  Only one
docstring is used by `help()` / documentation tools; the second is silently ignored.

**Fix:** Merge both docstrings into one.

---

## Medium Findings

### M-1 — `import re` deferred inside `_select_prompt_partial` (context_builder.py:519)
`re` is already imported at the top of the file via the `from __future__ import` block and
standard imports.  The inline `import re` at line 519 is redundant.

**Fix:** Remove the redundant `import re` inside `_select_prompt_partial`.

### M-2 — `_is_reasoning_model` assigned twice (context_builder.py:824–826)
`_is_reasoning_model` is set to `False` on line 824, then immediately overwritten to `False`
again on line 826 inside a nested `try` block before any actual logic runs.  The first
assignment is unreachable in any meaningful sense.

**Fix:** Remove the redundant first assignment on line 824.

### M-3 — `orchestrator_helpers._publish_active_config_impl` re-defines local `_pc_valid`/`_pc_extract` when import fails (orchestrator_helpers.py:84–113)
When `provider_capabilities._valid_str`/`_extract_str` cannot be imported, two local fallback
functions are defined inline.  The canonical `_valid_str`/`_extract_str` are **already imported
at module level** (line 24) from `src.core.utils.strings`.  The local fallbacks are therefore
redundant copies of that logic.

**Fix:** Replace the local fallback definitions with a direct reference to the module-level
`_valid_str`/`_extract_str` already available.

### M-4 — Unused variable `out` in `_get_models_for_provider_key` (llm_manager.py:271)
`out: List[str] = []` is declared at the top of the function and never modified — all code
paths either `return` a list literal or `return out` at the very end (with `out` still empty).
The variable name gives a false impression that results are accumulated into it.

**Fix:** Remove `out` and replace the final `return out` with `return []`.

### M-5 — `SymbolGraph.graph_path` uses hard-coded `.agent-context` instead of central helper (symbol_graph.py:115) — DEFERRED
`SymbolGraph.__init__` constructs `graph_path = self.workdir / ".agent-context" / "symbol_graph.json"`.
Every other component uses `agent_context_path()` from `tools_config` for this.

**Deferred:** Applying `agent_context_path()` here creates the `.agent-context` directory on
construction, which mutates the parent directory's `mtime_ns`.  `analysis_node._INDEXED_DIRS`
uses the parent `mtime_ns` as its cache key, so creating a subdirectory after the first
`index_repository()` call invalidates the cache entry and causes the second call to re-index.
Fix requires either lazy directory creation or a content-hash cache key in analysis_node — out
of scope for Vol38.

---

## Low Findings

### L-1 — `traceback` imported inline inside `symbol_graph._save_graph` (symbol_graph.py:161, 189)
`import traceback` appears twice inside `_save_graph`'s `except` blocks.  `traceback` is a
standard-library module — hoist to module level.

**Fix:** Add `import traceback` to module-level imports in `symbol_graph.py`.

### L-2 — `asyncio`, `inspect` imported at function call-site in `distiller._call_llm_sync` (distiller.py:68–69)
`import asyncio` and `import inspect` appear inside the function body.  Both are used only
within that function, but importing them at call-time adds overhead on every invocation.

**Fix:** Hoist both to module-level in `distiller.py`.

### L-3 — `contextvars`, `functools`, `inspect`, `asyncio` imported inside the `run_with_correlation` fallback closure (llm_manager.py:84–88)
The fallback `run_with_correlation` that is defined when `event_bus` is unavailable re-imports
four standard-library modules **each time it is called** from within the function body.  These
are already at module level (`asyncio` line 12, `functools` line 14, `inspect` line 31).

**Fix:** Remove the four inline imports inside the fallback; use the module-level bindings
(rename to avoid shadowing: `_asyncio`/`_functools`/`_inspect` are already the module-level
names).

### L-4 — `importlib` imported inline inside `ProviderManager.initialize` and `_call_model_internal` (llm_manager.py:1045, 1525)
`importlib` is not at module level but is imported in two separate places inside the file.
Hoist to module level.

**Fix:** Add `import importlib` to the module-level imports.

---

## Deferred / Not a Finding

- `provider_context.py` — clean; no issues found.
- `orchestrator.py` — thin re-export shim; no issues.
- `session_manager.py` — clean.
- `tool_execution_service.py` — clean.
- `tracer.py` — clean; OTel optional-import guard is intentional.
- `plan_mode.py` — already audited in Vol36; no new issues.
- `distiller.py` L-2 (asyncio/inspect) — low priority; hotpath is the thread-executor path
  which only imports once per thread anyway.

---

## Fix Plan (priority order)

1. H-1: Remove inline `import tempfile/os/shutil` in `save_provider` (llm_manager.py)
2. H-2: Hoist `_camelize` out of the provider loop (llm_manager.py)
3. H-3: Merge duplicate docstrings in `get_session_store` (session_store.py)
4. M-1: Remove redundant `import re` in `_select_prompt_partial` (context_builder.py)
5. M-2: Remove duplicate `_is_reasoning_model = False` assignment (context_builder.py)
6. M-3: Replace local `_pc_valid`/`_pc_extract` fallbacks with module-level imports (orchestrator_helpers.py)
7. M-4: Remove unused `out` variable from `_get_models_for_provider_key` (llm_manager.py)
8. L-1: Hoist `import traceback` in `symbol_graph.py`
10. L-2: Hoist `import asyncio`/`inspect` in `distiller.py`
11. L-3: Remove inline imports in the `run_with_correlation` fallback (llm_manager.py)
12. L-4: Hoist `import importlib` in `llm_manager.py`
