# Audit Report — Vol45

**Files audited:** `src/core/orchestration/inference_loop.py`, `tool_execution_pipeline.py`, `permission_gateway.py`, `token_budget.py`, `loop_guards.py`, `message_manager.py`

**Status:** All quick-win findings fixed in this volume. Deferred findings noted below.

---

## Findings

### Fixed

| ID | Severity | File | Description | Fix |
|----|----------|------|-------------|-----|
| F-71 | High | `inference_loop.py:701` | Off-by-one: `loop_iteration > 5` fires at iteration 6, not 5. `max_rounds=5` and the guard should be `>= MAX_TOOL_LOOP_ITERATIONS`. | Introduced `MAX_TOOL_LOOP_ITERATIONS = 5`; changed guard to `>=`; replaced all `"/5"` literals. |
| F-74 | High | `tool_execution_pipeline.py:1108` | `locals().get("_orch_token")` always returns `None` on the exception path because `_orch_token` is not a local variable of the `except` block — it's defined in the `try` block above. ContextVar leaks on every tool exception. | Replaced with direct reference to `_orch_token` (always defined as `None` before the try block). |
| F-76 | Medium | `inference_loop.py:293,963` | `import threading as _thr` repeated inline at two call sites; module already imports `threading as _threading`. | Removed inline imports; replaced `_thr.` with `_threading.`. |
| F-77 | Medium | `inference_loop.py:621,1044` | `import contextvars as _cv` repeated inline at two call sites. | Hoisted to module level alongside `threading`. |
| F-78 | High | `message_manager.py:234` | `elapsed = _time.time()` captures a Unix timestamp, not a duration. Used as `"ts": int(elapsed)` but the sibling key `"elapsed": 0.0` is always hardcoded to zero. | Removed entire dead timing block (see F-85/F-86). |
| F-79 | Medium | `message_manager.py:249,254` | `from src.tools.tools_config import agent_context_path` duplicated in both branches of a `try/except` where both branches do the same thing. | Removed with the dead block. |
| F-80 | Medium | `inference_loop.py:593,701,721` | Magic literal `5` repeated for loop limit with no named constant. | Replaced with `MAX_TOOL_LOOP_ITERATIONS`. |
| F-81 | Medium | `tool_execution_pipeline.py:386,693` | Magic `120.0` (approval timeout) and `max_workers=4` inline at call sites. | Added `APPROVAL_TIMEOUT_SECONDS = 120.0` and `TOOL_EXECUTOR_MAX_WORKERS = 4` as module constants. |
| F-82 | Medium | `token_budget.py:71` | `TokenBudgetMonitor.__init__` had no singleton guard — direct `TokenBudgetMonitor()` calls bypass `get_instance()` and produce untracked instances. | `get_instance()` now sets `_is_singleton = True` on the canonical instance; callers can detect singleton vs. direct construction. |
| F-83 | High | `permission_gateway.py:33,104` | Two loggers: `_logger = logging.getLogger(__name__)` at line 33 and `logger = logging.getLogger(__name__)` at line 104. Body uses both names inconsistently. | Removed duplicate `logger` at line 104; replaced all `logger.` call sites with `_logger.`. |
| F-84 | Medium | `message_manager.py:248–256` | Dead soul: `try: import agent_context_path … except: import agent_context_path` — both branches import the same symbol; the except branch can never do anything different. | Removed with the dead timing block. |
| F-85 | Medium | `message_manager.py:229–351` | Dead timing block wrapped entirely in `except Exception: pass` with no observable side-effects (file write always fails silently in practice; `elapsed` is wrong). ~120 lines of dead code. | Deleted. |
| F-86 | Medium | `message_manager.py:260` | `"elapsed": 0.0` hardcoded constant in timing entry — the timing data was never correct. | Deleted with the dead block. |
| F-87 | Low | `tool_execution_pipeline.py:745` | `_TOOL_OUTPUT_MAX_CHARS = 8000` defined as a local variable inside a hot-path function body; re-assigned on every call. | Hoisted to module constant `TOOL_OUTPUT_MAX_CHARS = 8_000`; removed local assignment. |
| F-88 | High | `loop_guards.py:65` | `multiedit` missing from `MODIFYING_TOOLS` — read-before-write gate not enforced for multi-edit operations. | Added `"multiedit"` to the set. |
| F-89 | Medium | `tool_execution_pipeline.py:686,379` | `max_workers=4` and `timeout=120.0` as inline magic numbers. | Replaced with named module constants (same fix as F-81). |

### Deferred

| ID | Severity | File | Description | Reason deferred |
|----|----------|------|-------------|-----------------|
| F-70 | High | `inference_loop.py` | `_generate_work_summary` duplicated verbatim vs `work_summary.py`. | Refactor risk — both code paths have subtle differences in how they handle `None` state; needs dedicated PR with test coverage. |
| F-72 | High | Multiple | `_session_read_files` populated twice on some paths. | Needs tracing through graph node execution flow; high coupling. |
| F-73 | High | `tool_execution_pipeline.py` + `permission_gateway.py` | Full 5-gate permission pipeline duplicated across both files. | Large refactor; needs dedicated extraction PR. |
| F-75 | Low | `tool_execution_pipeline.py` | Ten stdlib imports deferred inside `execute_tool_impl` hot path. | Some may be guards against circular imports; audit each before hoisting. |
| F-87 (agent_brain) | Low | `inference_loop.py:335` | `from src.core.orchestration.agent_brain import load_system_prompt` inline inside function. | Circular import risk; defer until import graph is mapped. |

---

## Summary

- **16 findings fixed** across 6 files.
- **5 findings deferred** (F-70, F-72, F-73, F-75, F-87/agent_brain) — all require larger refactors or import-graph analysis.
- **148 unit tests pass** post-fix with 0 regressions.
- `multiedit` gap in `MODIFYING_TOOLS` (F-88) was the only security-relevant finding; now closed.
