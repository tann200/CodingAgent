# Phase 4 — Progress & Next-Task Analysis

**Scope:** Track Phase 4 of the audit roadmap (advanced features, Weeks 9-12), record completed items, and provide a concrete implementation analysis for the next task.
**Status:** 4.5 + 4.6 + 4.7 + 4.8 complete; pending **4.1, 4.2, 4.3, 4.4**.

---

## Completed — 4.7 Explicit permission_kind on All Tools

Every built-in `@tool` now declares its semantic permission category at
definition time instead of relying on the `side_effects`-implied default, so
`ToolRegistry.get_permission_kind()` and the permission gateway resolve precise
permission-table kinds.

**Files:**
- 15 tool modules updated (33 decorators): `_bash_exec` (`bash`,
  `bash_readonly` → EXECUTE_BASH, `check_background_task` → NONE),
  `_edit_tools` (write tools → WRITE_FILE), `ast_tools` (`ast_rename` →
  LSP_WRITE, `ast_list_symbols` → LSP_READ), `batch_tools` (batch →
  NONE), `interaction_tools` (`ask_user`/`send_user_message` → NONE,
  `submit_plan_for_review` → PLAN), `patch_tools` (`apply_patch`/
  `edit_code_block` → WRITE_FILE, `generate_patch` → READ_FILE),
  `project_tools` (README_FINGERPRINT → READ_FILE), `repo_read_tools`
  (`find_files`/`search_code`/`analyze_repository` → READ_FILE,
  `find_symbol`/`find_references` → LSP_READ), `repo_write_tools` →
  WRITE_FILE, `rollback_tools` (`revert_last_tool` → WRITE_FILE,
  `list_snapshots` → READ_FILE), `skill_tools` (`load_skill` → READ_FILE,
  `list_skills` → NONE), `state_tools` (write → WRITE_FILE, reads →
  READ_FILE), `system_tools` (`get_git_diff` → GIT_READ, others →
  READ_FILE), `todo_tools` → WRITE_FILE, `verification_tools`
  (`run_js_tests`/`run_ts_check`/`run_eslint`/`run_tests_legacy` →
  EXECUTE_BASH).  LSP dict-schema tools already carried plain-string
  `permission_kind`.
- `src/tools/_tool.py` — new `ToolDefinition.permission_kind_explicit: bool`
  (True when `permission_kind=` was passed to `@tool`), set in the decorator.
- `tests/unit/test_tool_permission_kind_explicit.py` (new, 4 tests) —
  exhaustive registry scan asserting every decorated builtin is explicit
  (aliases excluded) and contract spot-checks; decends the `side_effects`
  inference path (implicit stays False).

**Design notes:**
- `permission_kind` is the semantic category (its orthogonal to the runtime
  `PermissionLevel` granularity in `tools_config.TOOL_PERMISSIONS`).
- The exhaustive test fails if a future `@tool` is added without explicit
  `permission_kind` — the audit invariant is enforced, not just documented.

**Gates:** ruff + mypy clean. Full gate suite green — **4,830 tests collecting**
(4,826 unit pass + 1 unit skip + 3 fast-path integration; exit 0).

---

## Completed — 4.8 Async VectorStore Model Loading

The sentence-transformers model load (network/disk + CPU init, ~80 MB) used to
block whatever thread called the first `search`/`encode`.  This item makes
model loading async/background so no caller thread blocks on it.

**Files:**
- `src/core/indexing/vector_store.py`
  - `_load_st_model()` — extracted single-flight loader guarded by
    `_ST_MODEL_LOCK`; concurrent callers share one load (at most one
    `SentenceTransformer(...)` per process).
  - `_get_st_model_ready()` — non-blocking readiness probe (never triggers a
    load); the encode/search/search_memories hot paths now use it instead of
    the blocking `_get_st_model()`, degrading to the SHA-256 stub / token
    search during warm-up instead of blocking the calling thread.
  - `_preload_st_model()` — idempotent daemon-thread prefetch kicked off from
    `VectorStore.__init__`, so the first real use usually finds the model warm.
  - `get_st_model_async()` — event-loop-safe model load via
    `asyncio.to_thread`.
  - `VectorStore` async wrappers: `asearch`, `aindex_code`, `aadd_memory`,
    `asearch_memories` (each `asyncio.to_thread` over the sync body — the
    ``vector``-stripping and dedup/rotation contracts are unchanged).
- `tests/unit/test_vector_store_async.py` (new, 6 tests) — non-blocking
  readiness, off-event-loop load, real single-flight under 8 concurrent
  callers (fake `sentence_transformers` module injected via `sys.modules`),
  daemon preload idempotency, async/sync wrapper parity.

**Design notes:**
- Backward compatible: the blocking `_get_st_model()` is retained for callers
  that want guaranteed semantic vectors; module `__all__` now also exports
  `get_st_model_async`.
- Deterministic in CI (no `sentence_transformers` installed): every ST path
  degrades gracefully on error, so stub/token behavior is unchanged.

**Gates:** ruff + mypy clean. Full gate suite green — **4,826 tests collecting**
(4,822 unit pass + 1 unit skip + 3 fast-path integration; exit 0).

---

## Completed — 4.6 Centralize Tool Constant Sets

Removed the second-source-of-truth tool-classification sets scattered across
`permission_gateway.py`, `loop_guards.py`, `tools_config.py`, and
`tool_constants.py`.  Audit location was `src/tools/constants.py`.

**Files:**
- `src/tools/constants.py` (new, canonical) — `WRITE_TOOLS_REQUIRING_READ`,
  `MODIFYING_TOOLS` (derived: base | `multiedit`), `DRY_RUN_BLOCKED_TOOLS`
  (write set | exec/git side-effects), `PERMISSION_REQUIRED_TOOLS`,
  `PERM_ORDER`, `WORKDIR_SAFE_TOOLS`, `FILE_TOOLS`, and the canonical
  `TOOL_ALIASES` map.  Pure-data leaf: no logging/IO/mutable state.
- `src/core/orchestration/tool_constants.py` — re-exports `WRITE_TOOLS_REQUIRING_READ`/`MODIFYING_TOOLS`/`DRY_RUN_BLOCKED_TOOLS`/`PERMISSION_REQUIRED_TOOLS`/`PERM_ORDER` from `src.tools.constants`; keeps the `_write_permission_audit` helper (audit layer) local.
- `src/core/orchestration/loop_guards.py` — `MODIFYING_TOOLS` now imports from
  `src.tools.constants` (was a private literal with SEC-2 sync comments).
- `src/core/orchestration/permission_gateway.py` — `_WORKDIR_SAFE_TOOLS`
  and `PermissionGateway._FILE_TOOLS` now bind the canonical sets.
- `src/tools/tools_config.py` — `TOOL_ALIASES` re-exported from canonical
  (shallow-copied dict so runtime mutation stays local to tools_config).

**Verification:** all identity-based tests still pass (`test_compat_reexports`,
`test_tool_constants`, `test_jsts_support_tool_registry_preretrieval` identity
assertion, `test_phase2_security_hardening` `delete_file`-not-autoconfirmed).
Added `TestCanonicalCentralization` (6 tests) pinning the canonical→re-export
object identity and derivation invariants.

**Gates:** ruff + mypy clean. Full gate suite green — **4,826 tests collecting**
(4,822 unit pass + 1 unit skip + 3 fast-path integration; exit 0).

---

## Completed — 4.5 Model Comparison Evaluation

Added provider/model comparison to the evaluation framework delivered in Phase 3 (3.2/3.3), so the same scenario suite can be run across multiple agent factories and ranked.

**Files:**
- `src/core/evaluation/compare.py` (new) — `run_models(factories, scenarios, *, samples, workdir)` runs each factory's suite with **per-model isolated run dirs** (`<workdir>/model_runs/<label>`) so one model's produced files can't leak into another's grading; `compare_models(runs)` builds per-scenario statuses/winners, per-model pass rates, best-first ranking, and tie counts — and accepts both `ScenarioResult` objects and SWE-bench style dicts (`scenario_name`/`status`); `save_comparison(path, runs, comparison, metadata=…)` writes a JSON report; `_print_comparison` renders the table.
- `src/core/evaluation/cli.py` — new `compare` subcommand: repeatable `--model label=module:callable` (label optional, defaults to the factory spec), `--scenarios`/`--category`/`--difficulty` filters, `--samples` pass@k, `--workdir`, `--output <report.json>`.
- `src/core/evaluation/__init__.py` — exports `compare_models`/`run_models`/`save_comparison`.
- `tests/unit/test_model_comparison.py` (new, 10 tests) — ranking/winner/tie logic, missing-scenario handling, SWE-bench-dict compatibility, JSON report export, end-to-end `run_models` (passing vs noop factory; runs isolated), samples (pass@k), CLI run + report + required-`--model` enforcement, package exports.

**Design notes:**
- Model labels are arbitrary; the factory spec is resolved the same way as other subcommands (`module:callable` → `importlib`, `default` → Orchestrator).
- Comparison units are best-of-`n` scenario statuses (pass if any attempt passed), consistent with the 3.2 regression baseline semantics, so pass@k variance does not skew rankings.
- Composability: `compare_models` works on result dicts, so a future `compare swebench` (comparing models on SWE-bench instances) needs no new comparison logic — SWE-bench runner results are already dicts with `scenario_name`/`status`.

**Gates:** ruff + mypy clean (`src/core/evaluation/` + all eval test files). Full gate suite green — **4,814 tests passing** (4,804 + 10 new; exit 0).

---

## Pending (next-task candidates)

| # | Item | Location | Complexity | Notes |
|---|------|----------|------------|-------|
| 4.1 | Refactor perception node (reduce fragmentation) | `perception_node.py` + helpers | High | Reduces maintenance burden |
| 4.2 | Remove duplicate defensive fallbacks | Multiple files | Medium | Eliminates second source of truth |
| 4.3 | Add fuzz testing / property-based tests | `tests/` | High | Explores edge cases systematically |
| 4.4 | Add performance benchmark suite | `tests/benchmarks/` | Medium | Tracks performance regressions |

Suggested next: **4.2** remove duplicate defensive fallbacks (medium; the 4.6 centralization already aligned several second sources of truth, and any permission/toollist duplication now has a canonical home to collapse into), then a High-complexity item or the remaining **4.1/4.3/4.4**. The evaluation framework (3.2/3.3/4.5) is available to benchmark any of these changes.