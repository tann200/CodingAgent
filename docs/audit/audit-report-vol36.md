# Audit Report — Vol36

**Scope:** `src/core/orchestration/graph/nodes/` (all node files) + `node_utils.py`
**Status:** All findings resolved and committed.

---

## Findings & Resolutions

### High

| ID | Finding | Resolution | Commit |
|----|---------|------------|--------|
| H-01 | 19 files contained inline copies of `_valid_str`/`_extract_str` | All now import from `src.core.utils.strings`; `provider_capabilities.py` updated too | Prior session |
| H-02 | 3-tier provider/model resolution block duplicated in 5 node files | `resolve_provider_capabilities(orchestrator)` added to `provider_capabilities.py`; all nodes delegate to it | Prior session |
| H-03 | `_span_node` no-op wrapper duplicated identically in `planning_node`, `execution_node`, `perception_node` | Consolidated into `node_utils.span_node`; all three nodes import from there | `40dc175` |

### Medium

| ID | Finding | Resolution | Commit |
|----|---------|------------|--------|
| M-01 | `locals()` guard in `analysis_node` return dict (`"analysis_failed" in locals()`) | Variable always assigned before use; guard removed | `40dc175` |
| M-02 | `_plan_is_resumable` defined inside function body in `planning_node`, capturing `state` via closure | Promoted to module level; `resume_session` passed as explicit bool param | `40dc175` |
| M-04 | `_await_llm_task` only assigned when `llm_helpers` is available; callers had no fallback | Added `async def _await_llm_task` fallback (`await task`) when import fails | `40dc175` |
| M-05 | `call_model` in `replan_node` called without `provider`/`model` despite having resolved `provider_capabilities` | Now passes `provider_capabilities.get("provider")` and `.get("model")` | `40dc175` |
| M-06 | `call_model` imported inside the function body on every call in `frontier_loop_node` | Already resolved: module-level proxy function defers the import once | `40dc175` (N/A — already correct) |
| M-07 | `_failed` helper defined twice inside `evaluation_node` (once per branch) | Hoisted to module level; both inline definitions removed | `40dc175` |
| M-09 | `_SKIP_WORDS` set defined inside function body in `analysis_node` on every call | Hoisted to module level constant | `40dc175` |

### Low

| ID | Finding | Resolution | Commit |
|----|---------|------------|--------|
| L-02 | `SIDE_EFFECT_TOOLS` set in `verification_node` was identical to module-level `_WRITE_TOOLS_ALWAYS_VERIFY` | Inline set removed; references updated to `_WRITE_TOOLS_ALWAYS_VERIFY` | `40dc175` |
| L-03 | `import re as _re` inside function body in `plan_validator_node` | Hoisted to module-level `import re`; inline alias removed | `40dc175` |
| L-04 | `step_retry_counts` keys stored as `str` by `_inc_step_retry`, but state deserialization may mix str/int | `_inc_step_retry` now normalises all keys to `int` on read; lookup updated to check both | `40dc175` |
| L-05 | `import threading as _thr` inside function body in `planning_node`, `evaluation_node`, `debug_node` | Hoisted to module-level `import threading` in all three files | `40dc175` |
| L-06 | Silent `except ImportError: pass` on distiller import in `memory_update_node` | Now logs `WARNING` with the exception message so missing-dependency failures are visible | `40dc175` |
| L-07 | Silent `except Exception: pass` on event_bus publish failure in `node_utils` | Now logs `DEBUG` with the exception so failures are traceable without noise | `40dc175` |

---

## Test Results

- **3065 passed, 1 skipped** across `tests/unit/` (excluding two pre-existing hanging tests:
  `test_graph_nodes.py::TestMemoryUpdateNode::test_memory_update_basic` and
  `test_scheduler_http_endpoints.py` — both timeout on threading waits, confirmed pre-existing).
- All 11 modified node modules import cleanly.
- `tests/integration/test_agent_loop_plaintext_tools.py` pre-existing failure confirmed on clean HEAD; unrelated to Vol36 changes.
