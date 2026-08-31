# Phase 2 — Progress & Next-Task Analysis

**Scope:** Track Phase 2 of the audit roadmap (robustness improvements), record completed items, and provide a concrete implementation analysis for the next task.
**Status:** 2.1/2.2/2.3/2.5/2.6 complete; next task is **2.7**.

---

## Completed

| # | Item | Commit | Notes |
|---|------|--------|-------|
| 2.2 | Fix alias permission bypass | `2a3c46c` | Add `resolve_tool_alias()`; normalize at top of `execute_tool_impl` (only production execution path) before all permission/policy guards. Raised severity MEDIUM→HIGH during analysis (DANGER tools could skip the approval gate via `run`/`shell`/`cmd`). |
| 2.8 | delete_file no auto-approval | `2a3c46c` | Removed `delete_file` from `_WORKDIR_SAFE_TOOLS`; `_check_workdir_confinement` always requires approval. Deletion is irreversible. |
| 2.1 | Sandbox strict in autonomous mode | `2a3c46c` | `_enforcement_required()` = env flag OR `is_autonomous()`; refuse unsandboxed fallback when enforcement required. Interactive mode unchanged. |
| 2.3 | NodeResultValidationFailed counter | (this commit) | Increment `metrics` counters in `_default_publish_violation` (state_schemas.py): `graph.node_validation_failed` total + per-node + per-reason. Makes fail-open violations observable/aggregable (CF-5 prerequisite to tightening `_STATE_SCHEMAS_STRICT`). Tests added in `test_state_schemas.py`. |
| 2.5 | Guard per-round debug serialization | (this commit) | `perception_node.py` — wrapped both debug `logger.info` blocks in `logger.isEnabledFor(logging.INFO)` so the expensive `repr(resp)` runs only when INFO is enabled. |
| 2.6 | Remove duplicate `_is_success()` | (this commit) | `execution_routing.py` now imports the shared `_is_success` from `perception_routing.py` (already imported module) instead of redefining it. Regression test `test_success_helper_is_single_source_of_truth`. |

Tests: `tests/unit/test_phase2_security_hardening.py` (13 tests) + 2 counter tests in `test_state_schemas.py` + dedup regression test. mypy + ruff check clean.

---

## Completed — Item 2.3: NodeResultValidationFailed counter / metric

**Audit reference:** `COMPREHENSIVE_AUDIT_REPORT.md` item 2.3 (row 337) and CF-5 (row 56-60): *"Add schemas for all 16 nodes. Wire a `NodeResultValidationFailed` counter for observability."*
**Complexity:** Medium (now Low given existing infra)
**Impact:** Makes fail-open schema violations observable/aggregable — a prerequisite to eventually tightening `_STATE_SCHEMAS_STRICT`.

### Current state (what already exists)

- Phase 1 (item 1.6) added output schemas for all 16 nodes and wrapped them with `_validated(...)` in `builder.py` (full, fast-path, and frontier graphs).
- `wrap_node` → `_enforce` in `src/core/orchestration/graph/state_schemas.py` already:
  - validates each node result (`validate_node_result`),
  - logs violations at ERROR level,
  - publishes a typed `NodeResultValidationFailed` event per violation (`_default_publish_violation`, state_schemas.py:395-427).
- A thread-safe metrics store already exists: `src/core/observability/metrics.py` exposes the `metrics` singleton with `increment_counter(name)` and `snapshot()`.

### The gap

`NodeResultValidationFailed` is emitted as a one-off event and logged, but there is **no accumulated counter / aggregate**. There is no way to observe the *rate or distribution* of fail-open violations over time (per node, per reason) — which is what the audit means by "wire a counter for observability."

### Proposed implementation (well-scoped, low risk)

In `_default_publish_violation` (state_schemas.py:395), after publishing the typed event, call:

```python
from src.core.observability.metrics import metrics
metrics.increment_counter("graph.node_validation_failed")              # total
metrics.increment_counter(f"graph.node_validation_failed.node.{violation.node_name}")
metrics.increment_counter(f"graph.node_validation_failed.reason.{violation.reason}")
```

Guarded by try/except (metrics must never raise), consistent with the module's graceful-degradation convention.

- The total lets a telemetry consumer detect that schema violations are occurring.
- The per-node / per-reason keys let an operator pinpoint which schema is too strict/loose.
- All are readable via `metrics.snapshot()` (`get_metrics_snapshot`) and any existing telemetry that consumes the metrics store.

### Tests

- A test that produces a violation (e.g. call `wrap_node` with a node that returns an unknown key, or call `validate_node_result` on a known-bad result) and asserts `metrics.snapshot()["counters"]["graph.node_validation_failed"]` incremented.
- Ensure counters are read defensively (`0` when absent) so tests are order-independent; clear via `metrics.reset()` where needed.

### Blocker: None.

---

## Remaining Phase 2 items

| # | Item | Complexity | Recommendation |
|---|------|------------|----------------|
| 2.4 | Consolidate three pruning strategies | High | Dedicated effort — touches core context/pruning pipelines across `perception_node.py`, `tool_output_pruning.py`, `token_truncation.py`. Do as its own task, not mixed in. |
| 2.7 | Centralize routing magic numbers | Medium | All routing modules. Single source of truth for thresholds. |

**Suggested ordering:** next is **2.7** (medium), then **2.4** as a standalone high-complexity effort.
