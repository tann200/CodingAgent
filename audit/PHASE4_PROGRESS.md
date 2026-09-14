# Phase 4 — Progress & Next-Task Analysis

**Scope:** Track Phase 4 of the audit roadmap (advanced features, Weeks 9-12), record completed items, and provide a concrete implementation analysis for the next task.
**Status:** 4.5 complete; pending **4.1, 4.2, 4.3, 4.4, 4.6, 4.7, 4.8**.

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
| 4.6 | Centralize tool constant sets | `src/tools/constants.py` | Low | Single source of truth |
| 4.7 | Make permission_kind explicit on all tools | All 55 tools in `src/tools/` | Medium | Improves permission precision |
| 4.8 | Add async VectorStore model loading | `vector_store.py` | Low | Prevents thread blocking |

Suggested next: small, self-contained items first (**4.6** centralize tool constants, then **4.8** async VectorStore loading) to build momentum, then the two medium refactors (4.7, 4.2), leaving the High-complexity items (4.3 fuzzing, 4.4 performance benchmarks, 4.1 perception refactor) for focused sessions. The evaluation framework (3.2/3.3/4.5) is available to benchmark any of these changes.