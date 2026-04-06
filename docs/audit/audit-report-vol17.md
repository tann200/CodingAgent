# Audit Report — Vol17

**Date:** 2026-04-06
**Auditor:** OpenCode agent (claude-sonnet-4.6)
**Scope:** Close the two remaining claw-code parity gaps (CP-10, CP-12) that were open at the end of vol16; run incremental audit of all changes introduced this cycle.
**Baseline:** 2908 passed, 2 skipped, 1 failed (pre-existing `test_llm_manager_fallback`)

---

## Executive Summary

Both remaining claw-code parity items are now CLOSED.  CP-12 (Anthropic
`cache_control` wiring) required a new native Anthropic adapter.  CP-10 (live
LSP diagnostics auto-injection) required extending `LSPClient` with a
synchronous diagnostics cache and wiring it into `build_runtime_context()`.
47 new unit tests were added; no regressions introduced.

---

## Items Closed This Cycle

### CP-12 — Anthropic `cache_control` Wiring for System Prompt Sentinel

**Root cause:** The `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` sentinel in the system
prompt was only stripped by `OpenAICompatibleAdapter._preprocess_messages()`.
No adapter actually set `cache_control: {"type": "ephemeral"}` on the static
block for native Anthropic endpoints.  The base-class docstring explicitly noted
this gap with a TODO comment.

**Fix — new file:** `src/core/inference/adapters/anthropic_adapter.py`

`AnthropicAdapter` subclasses `OpenAICompatibleAdapter` and:

1. Overrides `_headers()` — uses `x-api-key` (not `Authorization: Bearer`),
   adds `anthropic-version: 2023-06-01` and `anthropic-beta: prompt-caching-2024-07-31`.
2. Overrides `_preprocess_messages()` — splits any system message on the
   sentinel into two content blocks:
   - **Static block** (before sentinel): `{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}` when ≥ 512 chars; no `cache_control` for smaller blocks (below Anthropic's effective caching threshold).
   - **Dynamic block** (after sentinel): `{"type": "text", "text": "..."}` — no `cache_control` so it is re-evaluated every turn.
3. API key resolved from constructor → `UserPrefs` → `ANTHROPIC_API_KEY` env var.
4. `ProviderManager` convention: `type = "anthropic"` in `providers.json` will
   auto-load this adapter via `AnthropicAdapter` / `Adapter` aliases.

**New test file:** `tests/unit/test_anthropic_adapter_cp12.py` (24 tests)

| Group | Tests |
|-------|-------|
| `_preprocess_messages` split & cache_control | 12 |
| Instantiation & key resolution | 6 |
| Headers | 5 |
| ProviderManager alias | 1 |

All 24 tests pass.  `python -m pyright src/core/inference/adapters/anthropic_adapter.py` → 0 errors.

---

### CP-10 — Auto-Inject LSP Diagnostics into Dynamic System Prompt

**Root cause:** Live LSP diagnostics (type errors, lint warnings) were only
available when the agent explicitly called the `lsp_diagnostics` tool.  The
`build_runtime_context()` function in `instruction_loader.py` injected the
static symbol index but had no path to surface cached diagnostic results.

The challenge: `LSPManager.get_client()` is async but `build_runtime_context()`
is synchronous.  Starting a new event loop from a sync context is unsafe (may
deadlock inside an async outer loop).

**Fix — three files changed:**

#### `src/core/indexing/lsp_client.py`

1. Added `_diagnostics_cache: Dict[str, List[Diagnostic]]` to `LSPClient.__init__`.
2. `get_diagnostics()` now stores pull results in the cache (and falls back to
   the cache when the server is unavailable rather than returning `[]`).
3. `_reader_loop()` now handles server-push `textDocument/publishDiagnostics`
   notifications (messages without `id`) and stores them in the cache.
4. New sync method `get_cached_diagnostics(uri) → List[Diagnostic]` returns a
   copy of the cached list — callable from synchronous code without any await.
5. `_DummyLSPClient.get_cached_diagnostics()` added (always returns `[]`).

#### `src/core/indexing/lsp_context.py`

Added `get_lsp_diagnostics_block(workdir, files, budget_chars)`:
- Iterates all cached `LSPClient._diagnostics_cache` dictionaries synchronously
  via `mgr._clients`.
- Filters to severity ≤ 2 (errors and warnings only; info/hints excluded).
- Caps at `_MAX_DIAG_FILES = 10` files and `_MAX_DIAG_PER_FILE = 20` entries.
- Makes paths relative to `workdir` for readability.
- Truncates to `budget_chars` (default 2000).
- Returns `<lsp_diagnostics>...</lsp_diagnostics>` or `""` (feature-gated by
  the same `CODINGAGENT_LSP_CONTEXT=1` / `lsp_context.enabled` flag).

#### `src/core/orchestration/instruction_loader.py`

`build_runtime_context()` now calls `get_lsp_diagnostics_block()` after the
LSP symbol index block.  Both calls are wrapped in `try/except` so neither can
break prompt assembly.

**New test file:** `tests/unit/test_lsp_diagnostics_cp10.py` (23 tests)

| Group | Tests |
|-------|-------|
| `get_lsp_diagnostics_block` (enabled/disabled, filtering, caps, truncation) | 13 |
| `LSPClient` cache (pull, unavailable fallback, push notification, copy) | 6 |
| `_DummyLSPClient` | 2 |
| `instruction_loader` integration | 2 |

All 23 tests pass.  `python -m pyright src/core/indexing/lsp_client.py src/core/indexing/lsp_context.py src/core/orchestration/instruction_loader.py` → 0 errors.

---

## Pyright Verification

```
python -m pyright src/core/inference/adapters/anthropic_adapter.py \
                  src/core/indexing/lsp_client.py \
                  src/core/indexing/lsp_context.py \
                  src/core/orchestration/instruction_loader.py
0 errors, 0 warnings, 0 informations
```

---

## Test Suite Summary

| Metric | Vol16 Baseline | Vol17 |
|--------|---------------|-------|
| Passed | 2908 | **2955** (+47) |
| Skipped | 2 | 2 |
| Failed | 1 (pre-existing) | 1 (pre-existing) |

New test files added this cycle:

| File | Tests | Coverage |
|------|-------|---------|
| `tests/unit/test_anthropic_adapter_cp12.py` | 24 | CP-12: Anthropic adapter, `cache_control` wiring, headers, key resolution |
| `tests/unit/test_lsp_diagnostics_cp10.py` | 23 | CP-10: diagnostics cache, push/pull, `get_lsp_diagnostics_block`, loader integration |

---

## Parity Status: All CP Items Closed

| ID | Description | Status |
|----|-------------|--------|
| CP-3 | SHA-256 dedup in instruction loader | ✅ Closed (vol ≤ 15) |
| CP-4 | Dynamic boundary sentinel constant | ✅ Closed (vol ≤ 15) |
| CP-5 | `verification_nudge_needed` flag | ✅ Closed (vol ≤ 15) |
| CP-6 | Deterministic auto-compaction | ✅ Closed (vol ≤ 15) |
| CP-7 | Shell hooks + deny semantics | ✅ Closed (vol ≤ 15) |
| CP-8 | Per-tool `PermissionMode` | ✅ Closed (vol ≤ 15) |
| CP-9 | Cache token tracking | ✅ Closed (vol ≤ 15) |
| CP-10 | Auto-inject LSP diagnostics | ✅ **Closed (vol17)** |
| CP-11 | Ancestor instruction walk | ✅ Closed (vol ≤ 15) |
| CP-12 | Anthropic `cache_control` wiring | ✅ **Closed (vol17)** |
| CP-14 | Session version field | ✅ Closed (vol ≤ 15) |
| CP-15 | `send_user_message` | ✅ Closed (vol ≤ 15) |

---

## Open Items

None. All claw-code parity items (CP-3 through CP-15) are closed.  All P4
robustness/capability items from the vol15 roadmap are closed.
