# Phase 3 — Progress & Next-Task Analysis

**Scope:** Track Phase 3 of the audit roadmap (capability improvements), record completed items, and provide a concrete implementation analysis for the next task.
**Status:** 3.1 complete; remaining items **3.2–3.8** pending.

---

## Scope Correction for 3.1 (important)

The audit item 3.1 ("Enable full graph `_USE_FULL_GRAPH = True`", `builder.py:94`) is **stale** relative to the current architecture:

- Production does **not** route through `compile_agent_graph()` / `_USE_FULL_GRAPH`. The production path is `get_compiled_graph_for_orchestrator` → tier-based graphs: `_compile_frontier_graph` ("capable" tier) or `_compile_lite_graph` ("lite"/"small"). The `_USE_FULL_GRAPH` flag and the fast-path 10-node graph are now effectively test-only (used by `tests/`, the legacy `_get_compiled_graph()` singleton, and the `GraphFactory` facade).
- The production frontier graph **already** has `analyst_delegation`, `debug`, and `delegation` nodes active. The only genuinely-missing capability was **replan**: the frontier graph had no `replan` node, so a `requires_split` tool result (oversized patch) could not be split into smaller steps — the frontier loop just kept the error in history.

**Decision (user-approved):** "Add replan to frontier graph" — restore the one genuinely-missing capability in the production tier graph, rather than flipping the dormant `_USE_FULL_GRAPH` flag.

## Completed

| # | Item | Notes |
|---|------|-------|
| 3.1 | Add replan to the frontier graph | Frontier loop now triggers replan on `requires_split`; new `replan` node split oversized steps and re-enters the loop. See below. |

### Item 3.1 details — Add replan to the frontier graph

**Files changed:**
- `src/core/orchestration/graph/nodes/frontier_loop_node.py` — after a tool batch, call `compute_replan_trigger(result=batch_last_result)` (the same patch-size guard semantics used by `execution_node`). When triggered, set `replan_required` + `action_failed=True` + `next_action=None` on exit and break the tight loop so the graph can route to the replan node. Imported `compute_replan_trigger` from `execution_plan.py` (no circular import).
- `src/core/orchestration/graph/tier_graph_routing.py` — `route_frontier_loop_exit` now returns `"replan"` when `replan_required` is set (checked after approval/overflow, before the verification fall-through). Added `route_replan_frontier(state, should_after_replan_fn=...)` mirroring `route_debug_frontier`: maps the full graph's `step_controller`/`perception` continuation targets to `frontier_loop`, and keeps `memory_sync` as the recovery-cap bail-out.
- `src/core/orchestration/graph/builder.py` — `_compile_frontier_graph` gains the `replan` node (wrapped with `_validated("replan", ...)` so its schema is enforced at the boundary) plus two edges: `frontier_loop → replan` (via `route_frontier_loop_exit`) and `replan → {frontier_loop, memory_sync}` (via `route_replan_frontier`).

**Routing caps:** respected because `should_after_replan` (delegated to by `route_replan_frontier`) checks `total_recovery_attempts` against the tier recovery cap, and `replan_node` itself enforces `max_replan_attempts` / `max_total_recovery_attempts`. Replan is therefore bounded — it cannot loop forever.

**Tests added/updated:**
- `tests/unit/test_tier_graph_routing.py` — `test_route_frontier_loop_exit_routes_to_replan_on_patch_size_trigger`, `test_route_replan_frontier_maps_continuation_to_frontier_loop`, `test_route_replan_frontier_bails_to_memory_sync_on_recovery_cap`.
- `tests/unit/test_frontier_analyst_coverage_gaps.py` — `TestFrontierGraphWiring.test_frontier_graph_includes_replan_node` (structural compile check) + `test_frontier_replan_routes_back_to_loop_and_bails_on_cap`.
- `tests/unit/test_frontier_tool_output_truncation.py` — `test_frontier_loop_breaks_to_replan_on_requires_split` (drives the node end-to-end with a `requires_split` tool result; asserts `replan_required`, `action_failed`, `next_action`).

**Gates:** ruff + mypy clean on changed files (the one mypy error in `frontier_loop_node.py:458` is pre-existing on `main`). Affected unit suites + fast-path integration green.

---

## Remaining (next-task candidates)

| # | Item | Location | Complexity | Notes |
|---|------|----------|------------|-------|
| 3.2 | Build evaluation framework | New `src/evaluation/` | High | Systematic quality measurement |
| 3.3 | Add SWE-bench integration | New evaluation harness | High | Industry-standard benchmarking; depends on 3.2 |
| 3.4 | Implement graph-state checkpointing | `inference_loop.py` + LangGraph checkpointer | High | Automatic crash recovery |
| 3.5 | Consolidate duplicate skill directories | `src/config/skills/` → `agent-brain/skills/` | Medium | Single authoritative skill set |
| 3.6 | Remove/update stub roles | `researcher.md`, `scout.md`, `tester.md` | Low | Eliminates defunct code |
| 3.7 | Add CLI feature parity with TUI | `src/main.py` | Medium | Headless/scriptable usage |
| 3.8 | Reconcile documentation test baselines | All docs | Low | Single authoritative count |

Suggested next: **3.6 (stub roles)** — Low complexity, minimal risk, and produces a fast win before the High-complexity evaluation/checkpointing items.