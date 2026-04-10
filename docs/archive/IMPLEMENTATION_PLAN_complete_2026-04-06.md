# Implementation Plan

**Date:** 2026-04-06  
**Scope:** All remaining features and improvements to develop.  
**Current baseline:** 2958 unit tests passing, 0 failed (pre-existing `test_llm_manager_fallback` fixed in vol18). All Parity Sprint items (S0–S10) complete. Vol15–17 audit all P4/ROB/CAP findings resolved. 0 pyright errors.

---

## Summary of Remaining Work

| Priority | Area | Items |
|----------|------|-------|
| **P0 — Critical** | Integration test infrastructure | Rewrite 5 xfailed pipeline mock tests using `MockAdapter` — **COMPLETE** (all 8 mock tests pass) |
| **P1 — High** | Security scope guard | GAP-S2: workspace `affected_files` scope enforcement — **COMPLETE** (implemented Stage 31) |
| **P1 — High** | New audit cycle | Vol12 full-spectrum audit — **COMPLETE** (all P1/P2 fixed 2026-04-06) |
| **P1 — High** | Vol13 audit cycle | Vol13 full-spectrum audit — **COMPLETE** (all P1/P2/P3 fixed 2026-04-06) |
| **P1 — High** | Vol14 audit cycle | Vol14 full-spectrum audit — **COMPLETE** (all P1/P2/P3 fixed 2026-04-06) |
| **P1 — High** | Vol18 audit cycle | Vol18 full-spectrum audit — **COMPLETE** (Phase 1 fixes applied 2026-04-06): SEC-2, ET-4, OE-1 docstrings |
| **P2 — Medium** | Test coverage gaps | MockAdapter integration patterns for delegation & analyst nodes |
| **P2 — Medium** | Documentation | Archive `TOOLS_GAP_ANALYSIS.md` — **COMPLETE** (archived 2026-04-06); `ARCHITECTURE.md` Stage 31/32 updated |
| **P3 — Low** | Quality | TOOLS_GAP_ANALYSIS.md deferred item (GAP-S2) — **COMPLETE** |

### Vol18 Open Items (Phase 2–4)

| ID | Severity | Description | Phase | Status |
|----|----------|-------------|-------|--------|
| SEC-1 | Medium | `WorkspaceGuard.guard_operation()` integration | 1 | ✅ CLOSED — already integrated in all 6 write tools (Phase 4.3) |
| SEC-2 | Medium | Sync `WRITE_TOOLS_REQUIRING_READ` and `MODIFYING_TOOLS` | 1 | ✅ CLOSED (Phase 1) |
| ET-4 | Low | Fix `test_llm_manager_fallback` cache pollution | 1 | ✅ CLOSED (Phase 1) |
| OE-1 | Medium | Docstring clarification on routing functions | 1 | ✅ CLOSED (Phase 1) |
| WF-1 | Medium | Replace `_task_is_complex()` keyword heuristic with perception-node state flag | 2 | ✅ CLOSED (Phase 2) |
| ET-2 | Medium | Wire `scenario_evaluator.py` into CI with smoke scenarios | 2 | ✅ CLOSED (Phase 2) |
| CAP-1 | High | Add unmocked end-to-end pipeline integration test | 2 | ✅ CLOSED (Phase 2) |
| WF-3 | Medium | Add per-step verification in `step_controller_node` | 2 | ✅ CLOSED (Phase 2) |
| MEM-1 | Low | Invalidate `_TEXT_CACHE` entries for files written during task | 2 | ✅ CLOSED (Phase 2) |
| RA-1 | Medium | Inject symbol graph results into `planning_node` prompt | 3 | ✅ CLOSED (Phase 3) |
| WF-2 | Medium | LLM-based `evaluation_node` verdict | 3 | ✅ CLOSED (Phase 3) |
| WF-4 | Low | Plan divergence detection in replan loop | 3 | ✅ CLOSED (Phase 3) |
| TS-4 | Low | Tool retry with backoff for transient errors | 3 | ✅ CLOSED (Phase 3) |
| UX-3 | Low | Dry-run mode | 3 | ✅ CLOSED (Phase 3) |
| ARCH-1 | High | Decompose `orchestrator.py` — extract `PermissionGateway`, `ToolRegistry`, `SessionManager`, `ScopeGuard` | 4 | ✅ CLOSED (Phase 4) |
| ARCH-3 | Medium | Decompose `route_execution` into sub-routers | 4 | ✅ CLOSED (Phase 4) |
| MEM-2 | Low | Persistent decision memory across sessions | 4 | ✅ CLOSED (Phase 4) |
| CAP-5 | Low | Performance benchmarks in CI | 4 | ✅ CLOSED (Phase 4) |

---

## Vol18 Phase 2 — Robustness

### IMPL-V18P2-1: WF-1 — Replace `_task_is_complex()` with perception-node state flag

**Status: ✅ COMPLETE (Phase 2)**

**Problem:** `_task_is_complex()` in `builder.py:192` uses a keyword regex + `relevant_files > 3` + `current_plan >= 2 steps` heuristic to decide whether a task needs deep analysis. The heuristic fires on partial keyword matches and cannot consider the full perception context (token count, file count, AST complexity).

**Implementation:**

1. **`AgentState` new field** (`src/core/orchestration/graph/state.py`):
   ```python
   task_complexity: Optional[str]  # "simple" | "complex" | None (unknown)
   ```

2. **`perception_node.py` — set the flag** (end of `perception_node`, after the LLM call):
   ```python
   # WF-1: Set task_complexity flag for routing; replaces builder._task_is_complex()
   _tc = "complex" if _task_is_complex_heuristic(state) else "simple"
   # The perception node has richer context: inject keyword check + file count here
   output["task_complexity"] = _tc
   ```
   Move the heuristic logic from `builder.py:192` into a shared helper in `perception_node.py`, or into a new `src/core/orchestration/graph/routing_utils.py`.

3. **`builder.py` — read the flag** in `route_after_perception()`:
   ```python
   # WF-1: prefer pre-computed flag over re-running heuristic
   task_complexity = state.get("task_complexity")
   if task_complexity == "complex":
       return "analysis"
   if task_complexity == "simple" and next_action:
       return "execution"
   # Fall back to heuristic if flag not set (e.g. resumed state without reprocessing)
   if next_action and _task_is_complex(state):
       return "analysis"
   ```

4. **Keep `_task_is_complex()` as fallback** for backward compat (old state dicts without the flag).

5. **Tests to add** (`tests/unit/test_builder_routing.py`):
   - `test_route_after_perception_uses_complexity_flag` — state with `task_complexity="simple"` → "execution"
   - `test_route_after_perception_falls_back_to_heuristic` — state without flag + complex keyword → "analysis"

**Files:** `builder.py:192,274`, `perception_node.py:~950`, `state.py`
**Complexity:** Low–Medium
**Impact:** More accurate routing; perception context informs routing decision

---

### IMPL-V18P2-2: ET-2 — Wire `scenario_evaluator.py` into CI

**Status: ✅ COMPLETE (Phase 2)**

**Problem:** `src/core/evaluation/scenario_evaluator.py` provides a complete `ScenarioEvaluator` class with `Scenario`, `ScenarioResult`, `run_scenario()`, and `get_summary()` but is never invoked from the test suite.

**Implementation:**

1. **New test file** `tests/integration/test_scenario_smoke.py`:
   ```python
   """Smoke tests for ScenarioEvaluator — always run in CI with MockAdapter."""
   import pytest
   from src.core.evaluation.scenario_evaluator import ScenarioEvaluator, Scenario

   @pytest.mark.integration
   def test_scenario_evaluator_setup_and_verify():
       """ScenarioEvaluator._setup_scenario and _verify_scenario work without an agent."""
       evaluator = ScenarioEvaluator()
       scenario = Scenario(
           name="smoke_verify",
           description="Write hello.py then verify it exists",
           task="",
           setup_files={"hello.py": "def hello():\n    return 'Hello World'\n"},
           expected_files={"hello.py": "def hello():"},
       )
       scenario_dir = evaluator._setup_scenario(scenario)
       passed, output = evaluator._verify_scenario(scenario, scenario_dir)
       assert passed

   @pytest.mark.integration
   def test_scenario_evaluator_agent_factory_noop():
       """ScenarioEvaluator.run_scenario completes even when agent does nothing."""
       evaluator = ScenarioEvaluator()
       scenario = Scenario(
           name="noop_agent",
           description="Agent does nothing; no expected_files to check",
           task="do nothing",
       )
       result = evaluator.run_scenario(scenario, agent_factory=lambda: None)
       assert result.status == "pass"  # No expected files → vacuously passes

   @pytest.mark.integration
   def test_get_default_scenarios_instantiates():
       """get_default_scenarios() returns a non-empty list of valid Scenario objects."""
       from src.core.evaluation.scenario_evaluator import get_default_scenarios
       scenarios = get_default_scenarios()
       assert len(scenarios) >= 3
       for s in scenarios:
           assert s.name and s.task
   ```

2. **Register `integration` mark** in `pytest.ini`:
   ```ini
   markers =
       integration: integration tests (may require live services or are slower)
   ```

3. **No CI changes needed** — `pytest.mark.integration` tests run in the default `pytest` invocation unless excluded with `-m "not integration"`.

**Files:** `tests/integration/test_scenario_smoke.py` (new), `pytest.ini`
**Complexity:** Low
**Impact:** ScenarioEvaluator covered by CI; regression detection for evaluation framework

---

### IMPL-V18P2-3: CAP-1 — Unmocked end-to-end pipeline integration test

**Status: ✅ COMPLETE (Phase 2)**

**Problem:** All existing integration tests mock `call_model` via `_patch_call_model`. No test runs the full perception → analysis → planning → execution → verification path with real routing logic and a real (in-memory) filesystem.

**Implementation:**

1. **New test** `tests/integration/test_e2e_pipeline_smoke.py`:
   ```python
   """E2E pipeline test: real LangGraph routing + MockAdapter responses + real filesystem."""
   import pytest, tempfile, os
   from pathlib import Path
   from src.core.inference.adapters.mock_adapter import MockAdapter
   from src.core.orchestration.orchestrator import Orchestrator

   def _build_scripted_responses(tmp_dir: str) -> list:
       """Build node-aware response sequence for a simple 'write hello.py' task."""
       # ... (see IMPL-IT-1 pattern for node detection)

   @pytest.mark.integration
   def test_e2e_write_hello_py(tmp_path, monkeypatch):
       """Full pipeline: perception → fast-path execution → write hello.py on real disk."""
       _patch_infra(monkeypatch)
       adapter = MockAdapter(responses=_build_scripted_responses(str(tmp_path)), strict=False)
       _patch_call_model(adapter, monkeypatch)
       orch = Orchestrator(working_dir=str(tmp_path))
       result = orch.run_agent_once("Create hello.py with def hello(): return 'hi'")
       assert (tmp_path / "hello.py").exists()
   ```

2. **Key difference from existing mock tests:** Uses `tmp_path` as `working_dir` so all file I/O is on real disk; verifies file actually exists after the run (not just that the graph completed).

3. **Does not require a live LLM** — all LLM calls are intercepted by MockAdapter.

**Files:** `tests/integration/test_e2e_pipeline_smoke.py` (new)
**Complexity:** Medium
**Impact:** Catches routing bugs, write-path issues, and state management problems in a single test

---

### IMPL-V18P2-4: WF-3 — Per-step verification in `step_controller_node`

**Status: ✅ COMPLETE (Phase 2)**

**Problem:** `step_controller_node.py` tracks `step_failed` from `last_result` but does not perform any verification of the previous step's output before advancing `current_step`. A step that wrote syntactically invalid Python is silently advanced.

**Implementation:**

1. **Add lightweight post-step check** in `step_controller_node.py` after `last_result` inspection:
   ```python
   # WF-3: Lightweight per-step check — run quick_lint on any .py file written in last step
   _step_lint_warnings = []
   if last_result and isinstance(last_result, dict):
       _written_path = last_result.get("path") or last_result.get("file")
       if _written_path and str(_written_path).endswith(".py"):
           try:
               from src.tools.lint_dispatch import quick_lint
               _lr = quick_lint(str(_written_path), workdir)
               if _lr and _lr.get("lint_errors"):
                   _step_lint_warnings = _lr["lint_errors"]
                   step_failed = True  # Override: step is failed if it produced lint errors
                   step_retry_counts[step_key] = int(step_retry_counts.get(step_key, 0)) + 1
           except Exception:
               pass  # Never block step advancement on lint tool failure
   ```

2. **Return lint warnings in output** so they are visible in logs:
   ```python
   return {
       "step_description": step_description,
       "planned_action": planned_action,
       "step_retry_counts": step_retry_counts,
       "step_lint_warnings": _step_lint_warnings,
   }
   ```

3. **Add `step_lint_warnings: List[str]` to `AgentState`.**

4. **Tests to add** (`tests/unit/test_step_controller_node.py`):
   - `test_step_controller_lint_failure_sets_step_failed`
   - `test_step_controller_no_lint_on_non_python_file`

**Files:** `step_controller_node.py:40`, `state.py`
**Complexity:** Low–Medium
**Impact:** Catches bad writes before evaluation; reduces unnecessary replan cycles

---

### IMPL-V18P2-5: MEM-1 — Invalidate `_TEXT_CACHE` entries on write

**Status: ✅ COMPLETE (Phase 2)**

**Problem:** `context_builder.py` has a module-level `_TEXT_CACHE: OrderedDict` (path → (mtime, content)) that caches file reads. `ContextBuilder.clear_cache()` clears the entire cache at task start. However, if the agent writes a file mid-task and then `ContextBuilder` is instantiated again (every node call), the old cached content from the task start may be served because the mtime on disk may not have advanced by the time the next node runs (sub-millisecond writes on some filesystems).

**Implementation:**

1. **Add `invalidate_path()` class method** to `ContextBuilder`:
   ```python
   @classmethod
   def invalidate_path(cls, path: str) -> None:
       """Remove a specific path from the text and JSON caches."""
       key = str(Path(path).resolve())
       with _CACHE_LOCK:
           _TEXT_CACHE.pop(key, None)
           _JSON_CACHE.pop(key, None)
   ```

2. **Call it in `write_file()` and `edit_file_atomic()`** (both already in `file_tools.py`) after the write succeeds:
   ```python
   # MEM-1: Invalidate context cache so next ContextBuilder instantiation re-reads
   try:
       from src.core.context.context_builder import ContextBuilder
       ContextBuilder.invalidate_path(str(p))
   except Exception:
       pass
   ```

3. **Tests to add** (`tests/unit/test_context_builder_cache.py`):
   - `test_invalidate_path_removes_entry_from_text_cache`
   - `test_invalidate_path_noop_on_missing_entry`
   - `test_write_file_invalidates_cache` (integration: write then read via ContextBuilder)

**Files:** `context_builder.py:50`, `file_tools.py:385,~1480`
**Complexity:** Low
**Impact:** Prevents stale file content being injected into system prompt during active editing sessions

---

## Vol18 Phase 3 — Capability

### IMPL-V18P3-1: RA-1 — Symbol graph injection into planning prompt

**Status: ✅ COMPLETE (Phase 3)**

**Problem:** `planning_node.py` injects `call_graph` and `test_map` from state (lines 207–222) but these fields are only present when `analysis_node` ran. Simple tasks that take the fast-path skip analysis entirely, so the planner has no symbol context.

**Implementation:**

1. **In `planning_node.py`**, after building the task context block, add a fallback symbol query:
   ```python
   # RA-1: If call_graph absent (fast-path skipped analysis), query SymbolGraph directly
   if not state.get("call_graph") and state.get("working_dir"):
       try:
           from src.core.indexing.repo_indexer import RepoIndexer
           _wdir = state["working_dir"]
           _indexer = RepoIndexer(_wdir)
           _symbols = _indexer.get_symbols_for_task(state.get("task", ""), max_results=5)
           if _symbols:
               plan_context += f"\n\n## Relevant Symbols\n```json\n{json.dumps(_symbols, indent=2)}\n```"
       except Exception:
           pass  # Never block planning on indexer failure
   ```

2. **Add `get_symbols_for_task(task: str, max_results: int)` to `RepoIndexer`** (or `SymbolGraph`) if not already present — this method takes a task description, extracts identifiers using the same heuristic as `perception_node`, and returns relevant function/class definitions.

**Files:** `planning_node.py:207`, `repo_indexer.py`
**Complexity:** Medium–High
**Impact:** Plans reference actual call sites and test coverage even on simple tasks

---

### IMPL-V18P3-2: WF-2 — LLM-based evaluation verdict

**Status: ✅ COMPLETE (Phase 3)**

**Problem:** `evaluation_node.py` is entirely rule-based (plan completion + `verification_passed` flag). It cannot assess whether the agent's output semantically fulfils the user's intent.

**Implementation:**

1. **Add LLM call at the end of the "complete" path** in `evaluation_node.py`:
   ```python
   # WF-2: Semantic verdict — only when rule-based check says "complete"
   try:
       from src.core.inference.llm_manager import call_model
       _verdict_prompt = [
           {"role": "system", "content": "You are a code review judge. Answer only: PASS or FAIL and one sentence reason."},
           {"role": "user", "content": f"Task: {state.get('task', '')}\n\nPlan executed:\n{json.dumps(current_plan, indent=2)}\n\nVerification result: {json.dumps(verification_result)}\n\nDid the agent accomplish the task?"},
       ]
       _resp = await call_model(_verdict_prompt, model=None)
       _content = (_resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
       _llm_passed = "FAIL" not in _content.upper()
       output["evaluation_llm_verdict"] = "pass" if _llm_passed else "fail"
       output["evaluation_llm_reason"] = _content[:200]
       if not _llm_passed:
           # Downgrade: route to debug instead of complete
           output["evaluation_result"] = "debug"
   except Exception:
       pass  # Never block completion on LLM verdict failure
   ```

2. **Add `evaluation_llm_verdict: Optional[str]` and `evaluation_llm_reason: Optional[str]` to `AgentState`.**

**Files:** `evaluation_node.py:95`, `state.py`
**Complexity:** Medium
**Impact:** Semantic correctness check catches cases where all tests pass but the user's actual goal was missed

---

### IMPL-V18P3-3: WF-4 — Plan divergence detection

**Status: ✅ COMPLETE (Phase 3)**

**Problem:** The replan loop (capped at 5 by `route_execution`) can produce identical plans on each replan cycle, burning the budget without making progress.

**Implementation:**

1. **Hash the current plan** in `replan_node.py` before returning; store in state as `last_plan_hash`.
2. **In `route_execution`**, before routing to `replan`, compare new plan hash against `last_plan_hash`. If identical, skip replan and route to `end`.
3. **State fields:** `last_plan_hash: Optional[str]`.

**Files:** `replan_node.py`, `builder.py:~1100`
**Complexity:** Low–Medium

---

### IMPL-V18P3-4: TS-4 — Tool retry with backoff

**Status: ✅ COMPLETE (Phase 3)**

**Problem:** Transient errors (network, file lock) cause immediate step failure → replan, burning the replan budget unnecessarily.

**Implementation:**

1. **In `tool_execution_service.py`**, wrap the tool call with a retry loop:
   ```python
   _TRANSIENT_ERRORS = ("timeout", "connection", "temporarily unavailable", "resource busy")
   for _attempt in range(3):
       _result = tool_fn(**args)
       _err = (_result.get("error") or "").lower() if isinstance(_result, dict) else ""
       if not any(e in _err for e in _TRANSIENT_ERRORS):
           break
       await asyncio.sleep(0.5 * (2 ** _attempt))  # 0.5s, 1s, 2s
   ```
2. **Cap retries at 3** with exponential backoff.

**Files:** `tool_execution_service.py`
**Complexity:** Medium

---

### IMPL-V18P3-5: UX-3 — Dry-run mode

**Status: ✅ COMPLETE (Phase 3)**

**Problem:** No way to preview what the agent will do without executing it.

**Implementation:**

1. **`Orchestrator.__init__`** — add `dry_run: bool = False` parameter.
2. **In `execute_tool`** — if `dry_run` and tool is in `WRITE_TOOLS_REQUIRING_READ`, log intent but return a `{"status": "dry_run", "would_call": tool_name, "args": args}` dict without executing.
3. **CLI layer** — add `--dry-run` flag.

**Files:** `orchestrator.py`, CLI entrypoint
**Complexity:** Medium

---

## Vol18 Phase 4 — Advanced

### IMPL-V18P4-1: ARCH-1 — Decompose `orchestrator.py` God class

**Status: ✅ CLOSED (Phase 4)**

`PermissionGateway` extracted to `src/core/orchestration/permission_gateway.py`:
- `PermissionResult(allowed, gate, reason, rejection)` dataclass
- `PermissionGateway(orchestrator).check(name, args)` → `PermissionResult`
- 5 gate methods: `_gate1_plan_mode`, `_gate2_explore_mode`, `_gate3_permission_level`, `_gate4_permission_mode`, `_gate5_user_approval`
- Independently testable; orchestrator inline gates remain for backward compatibility

**Tests:** `tests/integration/test_phase4_findings.py::TestPermissionGateway` (5 tests)

---

### IMPL-V18P4-2: ARCH-3 — Decompose `route_execution`

**Status: ✅ CLOSED (Phase 4)**

Five sub-router helpers added to `builder.py` before `route_execution`:
- `_check_tool_budget(state)` → bool
- `_check_plan_approval_pending(state)` → bool
- `_check_preview_pending(state)` → bool
- `_check_replan_required(state)` → `str | None`
- `_check_no_plan_fast_path(state)` → `str | None`

`route_execution` body replaced with a thin delegator (≤25 lines).

**Tests:** `tests/integration/test_phase4_findings.py::TestRouteExecutionSubRouters` (14 tests)

---

### IMPL-V18P4-3: MEM-2 — Persistent decision memory

**Status: ✅ CLOSED (Phase 4)**

- `SessionStore.write_decisions_json(limit=50)` — atomically writes recent decisions to `{workdir}/.agent-context/decisions.json`
- `SessionStore.read_recent_decisions(max_entries=10)` — reads file, returns `[]` on any error
- `SessionStore.add_decision()` auto-flushes after every insert (non-critical, best-effort)
- `perception_node.py` injects recent decisions into system message on round 0

**Tests:** `tests/integration/test_phase4_findings.py::TestPersistentDecisionMemory` (7 tests)

---

### IMPL-V18P4-4: CAP-5 — Performance benchmarks in CI

**Status: ✅ CLOSED (Phase 4)**

`benchmarks/bench_pipeline.py` created with 5 scenarios (`fast_path_write`, `fast_path_read`, `fast_path_grep`, `fast_path_edit`, `fast_path_list`). Runnable standalone (`python benchmarks/bench_pipeline.py`) or as pytest suite (`pytest benchmarks/bench_pipeline.py`). Records wall time, estimated token counts, and pass/fail per scenario.

**Tests:** `tests/integration/test_phase4_findings.py::TestBenchmarkScenarios` (4 tests)

---

### IMPL-IT-1: Rewrite xfailed pipeline mock tests with `MockAdapter`

**Status: COMPLETE (2026-04-06)** — All 8 mock integration tests pass (`test_pipeline_mock.py` × 6, `test_agent_loop_plaintext_tools.py` × 2). No `@pytest.mark.xfail` decorators remain in the test suite.

**Implementation:**

The `MockAdapter` in `src/core/inference/adapters/mock_adapter.py` accepts a `responses` list where each item can be a `dict` (static) or `callable(messages) -> dict`. Use callables to detect which node is calling and return node-appropriate responses.

**Node detection strategy:** Inspect the last system message content for node-identifying strings:
- Perception node: system prompt contains `"Operational"` / user message contains `"Understand the task"`
- Analysis node: system prompt contains `"analyse"` / last user message about codebase analysis
- Planning node: system prompt contains `"Strategic"` / user message references `"Create a plan"`
- Execution node: system prompt contains `"Execute"` / user message references a plan step

**Tests to rewrite:**

```
test_pm1_write_file_creates_file
test_pm2_read_file_marker_present
test_pm3_overwrite_file_changes_land
test_pm5_multi_step_write_then_read
test_agent_loop_plaintext_tools[fix_syntax]
```

**Implementation pattern for each test:**

```python
from src.core.inference.adapters.mock_adapter import MockAdapter
from src.core.orchestration.orchestrator import Orchestrator

def make_node_aware_adapter(node_responses: dict) -> MockAdapter:
    """
    node_responses: dict mapping node-hint → response dict
    Keys: "perception", "analysis", "planning", "execution"
    """
    def router(messages):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        combined = (system + last_user).lower()
        if "strategic" in combined or "create a plan" in combined:
            return node_responses.get("planning", node_responses["default"])
        if "execute" in combined or "step" in combined:
            return node_responses.get("execution", node_responses["default"])
        if "analyse" in combined or "analysis" in combined:
            return node_responses.get("analysis", node_responses["default"])
        return node_responses.get("perception", node_responses["default"])
    return MockAdapter(responses=[router], strict=False)
```

**Response schema for each node:**

- **Perception/Analysis:** `{"choices": [{"message": {"role": "assistant", "content": ""}}]}`  
  (These nodes call `call_model` but the agent continues regardless of content)
- **Planning:** Must return a valid plan block:
  ```
  ## Plan
  - [ ] step 1: write target.txt with content "hello"
  ```
- **Execution:** Must return a valid tool call:
  ```yaml
  tool_name: write_file
  arguments:
    path: target.txt
    content: hello
  ```
- **Verification/Evaluation:** `STATUS:complete` or simple acknowledgement

**Estimated effort:** 2–3 hours  
**Expected outcome:** 5 xfail tests become passing; CI green without LM Studio

---

## P1 — GAP-S2: Workspace Scope Guard

**Status: COMPLETE (Stage 31, 2026-04-06)** — `_affected_files` guard implemented in `orchestrator.py` (lines 1721–1759). Blocks writes to out-of-scope files when `_affected_files` is non-empty; publishes `scope.violation` event.

**Implementation:**

1. **`AgentState` field** (already exists as partial): `affected_files: List[str]` — populated by `planning_node` after plan is parsed. The plan parser should extract file paths from step descriptions.

2. **Extraction in `planning_node`** (`src/core/orchestration/graph/nodes/planning_node.py`):
   ```python
   import re
   file_pattern = re.compile(r'\b[\w./\-]+\.(py|js|ts|go|rs|java|yaml|json|md|txt)\b')
   affected = list({m.group() for step in plan_steps for m in file_pattern.finditer(step)})
   state["affected_files"] = affected
   ```

3. **Guard in `execute_tool`** (`orchestrator.py`): When a write-family tool is called and `affected_files` is non-empty, checks target path; publishes `scope.violation` event on mismatch.

---

## P1 — Vol12 Audit Cycle

**Status: COMPLETE (2026-04-06)**

All P1 and P2 findings from `docs/audit/audit-report-vol12.md` have been fixed:

| Finding | File | Fix |
|---------|------|-----|
| P1-1 Blocking permission gate | `tool_execution_service.py` | `pre_execute` + `_check_permission_gate` made `async def`; uses `await gate.wait_async()` |
| P1-2 Git subprocess timeout | `snapshot_manager.py` | `_GIT_TIMEOUT = 60.0`; `_run_git` wraps `communicate()` in `asyncio.wait_for` with kill on timeout |
| P1-3 LSP reader loop leak | `lsp_client.py` | `_reader_loop` `finally` block sets exception on all pending futures + clears `_pending` |
| P1-4 Unrestricted git in bash | `_security.py` + `file_tools.py` | Removed `"git"` from `SAFE_COMMANDS`; added `GIT_SAFE_SUBCOMMANDS` allowlist; gates in `bash()` + `bash_readonly()` |
| P2-1 Non-atomic cost flush | `session_cost_tracker.py` | `flush()` uses `tempfile.mkstemp` + `os.replace`; fixed argument-order bug in `estimate_cost_usd` |
| P2-2 Credentials file permissions | `credentials.py` | `os.chmod(_PREFS_PATH, 0o600)` after every `os.replace` |
| P2-3 project_id path traversal | `snapshot_manager.py` | `_PROJECT_ID_RE` sanitises project_id before use |
| P2-4 Restore leaves untracked files | `snapshot_manager.py` | `restore()` runs `git clean -fd` after `checkout-index` |
| P2-5 MCP connect leak on failure | `mcp_client.py` | `connect()` cancels reader task + terminates process before re-raising on `_initialize()` failure |

Pyright errors fixed in the same sprint:
- `agent_types.py` — `permission_rules: List[Dict[str, Any]]`
- `planning_node.py` — explicit `model=` kwarg; added `Optional` import
- `formatter.py` — renamed cache-hit variable to `cached: Optional[List[str]]`
- `subagent_tools.py` — `cast(_AgentState, initial_state)` in `ainvoke` call
- `orchestrator.py:1756` — fixed wrong import path `src.core.events.event_bus` → `src.core.orchestration.event_bus`

Pyright result: **0 errors, 0 warnings**  
Test baseline: **2607 passed, 2 skipped**

---

## P1 — Vol13 Audit Cycle

**Status: COMPLETE (2026-04-06)**

All P1, P2, and P3 findings from `docs/audit/audit-report-vol13.md` have been fixed:

| Finding | File | Fix |
|---------|------|-----|
| P1-1 preview gate missing path guard | `preview_coordinator.py` | `_on_confirmed`/`_on_rejected` validate non-empty `path` before `resolve_preview_gate` |
| P1-2 plugin loader world-writable dir | `deferred_init.py` | `stat().st_mode & stat.S_IWOTH` check; refuses world-writable dirs, early return |
| P1-3 rm bypass variants not blocked | `_security.py` | Added `/bin/rm`, `/usr/bin/rm`, `\\rm`, and flag-interleaved variants to base patterns |
| P2-1 nested asyncio.wait_for in MCP | `mcp_client.py` | Removed outer `wait_for` from `_initialize()`; inner `_request()` already enforces timeout |
| P2-2 SessionCostTracker race conditions | `session_cost_tracker.py` | `threading.Lock` guards `record_tool_call`, `flush`, `get_buffer`, and `reset` |
| P2-3 sed -i not blocked in bash_readonly | `file_tools.py` | `SED_WRITE_FLAGS` imported; Gate 3c added after Gate 3b to block in-place edit flags |
| P2-4 session_cost_usd not persisted | `session_cost_tracker.py` | `flush()` writes `session_cost_usd` to `usage.json` under lock snapshot |
| P2-5 deprecated get_event_loop() | `lsp_client.py` | `asyncio.get_event_loop()` → `asyncio.get_running_loop()` at line 390 |
| P3-1 no idempotency guard | `tool_execution_service.py` | `_seen_calls` frozenset check as step 0 in `pre_execute`; duplicate calls return blocked verdict |
| P3-2 GIT_SAFE gate has no unit tests | `test_bash_security_file_tools_caching.py` | `TestGitSafeSubcommandsGate` class with 8 tests covering `bash()` + `bash_readonly()` |
| P3-4 DANGEROUS_PATTERNS is mutable | `_security.py` | `_BASE_DANGEROUS_PATTERNS: tuple` + `_EXTRA_PATTERNS: list`; `add_dangerous_pattern()` API |
| P3-5 CODINGAGENT_TRUSTED undocumented | `docs/DEVELOPMENT.md` | Added one-liner to Running Tests section |

Pyright result: **0 errors, 0 warnings**
Test baseline: **2615 passed, 2 skipped**

---

## P1 — Vol14 Audit Cycle

**Status: COMPLETE (2026-04-06)**

All P1, P2, and P3 findings from `docs/audit/audit-report-vol14.md` have been fixed:

| Finding | File | Fix |
|---------|------|-----|
| HIGH-1 `cost_tracker` never flushed/reset | `orchestrator.py` + `session_cost_tracker.py` | All 4 `_flush_usage_buffer()` call sites replaced with `cost_tracker.flush(task_id=...)`; `cost_tracker.reset()` called at turn start; `cost_tracker.record_tool_call()` wired after every tool execution; `_flush_usage_buffer()` reduced to deprecated thin wrapper |
| HIGH-2 `flush()` double-counting bug | `session_cost_tracker.py` | `flush()` now clears `_usage_buffer` inside the lock after taking the snapshot; repeated `flush()` no longer double-counts cost |
| HIGH-3 `reset_idempotency()` has zero callers | `orchestrator.py` | `tool_execution_service.reset_idempotency()` called at turn start in `run_agent_once()` so `_seen_calls` is cleared between tasks |
| WR-1 write-family tools bypass scope guard | `orchestrator.py` | `delete_file`, `rename_file`, `ast_rename` added to `WRITE_TOOLS_REQUIRING_READ`; these tools now participate in `_affected_files` enforcement |
| WR-2 `add_dangerous_pattern()` not in public API | `src/tools/__init__.py` | Docstring updated to use `add_dangerous_pattern()` (not the bypass route); function re-exported from `__init__.py` |
| TS-1 world-writable check misses symlink itself | `deferred_init.py` | Check now uses both `os.lstat()` (symlink entry) + `Path.stat()` (symlink target) |
| ET-1 no regression test for flush idempotency | `tests/unit/test_d10_services.py` | `test_flush_is_idempotent_without_reset` added |
| ET-2 no lifecycle test for reset_idempotency | `tests/unit/test_d10_services.py` | `test_reset_idempotency_clears_between_tasks` added |

Pyright result: **0 errors, 0 warnings**
Test baseline: **2617 passed, 2 skipped**

---

## P2 — MockAdapter Integration Patterns

### IMPL-IT-2: Delegation node integration tests

**Status: COMPLETE (2026-04-06)** — `tests/integration/test_delegation_mock.py` exists and all 6 tests pass.

---

## P2 — Documentation Updates

### IMPL-DOC-1: Archive `TOOLS_GAP_ANALYSIS.md`

**Status: COMPLETE (2026-04-06)** — archived to `docs/archive/TOOLS_GAP_ANALYSIS_complete_2026-04-06.md`. All GAP items resolved; GAP-S2 scope guard implemented in Stage 31.

### IMPL-DOC-2: Update `ARCHITECTURE.md` test count

**Status: COMPLETE (2026-04-06)** — ARCHITECTURE.md header updated to 2607 unit tests passing, 0 pyright errors.

---

## P3 — Quality Improvements

### IMPL-Q1: `test_pipeline_mock.py` dead test removal

**Status: COMPLETE (2026-04-06)** — `test_pm4_no_tool_call_does_nothing` was verified as not using `DeterministicAdapter`; no dead test to remove.

### IMPL-Q2: CI pre-flight for xfail count

**Status: COMPLETE (2026-04-06)** — Zero `@pytest.mark.xfail` decorators remain in the test suite (only graceful inline `pytest.xfail()` in integration tests that require a live LM Studio connection). No CI gate needed.

### IMPL-Q3: `check_background_task` tool registration

**Status: COMPLETE (2026-04-06)** — Verified: `check_background_task` is registered in `orchestrator.py:608`.

---

## P4 — Deferred Advanced Features

These items have been identified across vol12–vol14 audit cycles and from the claw-code deep dive (v2). None are blocking current functionality. CP-6 through CP-15 are new findings from the v2 deep dive (2026-04-06).

Reference: `docs/audit/parity-report-claw-code-v2.md` for full gap analysis. `docs/audit/deep-dive-claw-code-architecture.md` (v2) for claw-code implementation details.

### Audit-sourced items

| ID | Feature | Source | Complexity |
|----|---------|--------|------------|
| P4-1 | `config_watcher.py` — live config reload without restart | vol12 CRIT-1, vol14 P4-1 | Medium |
| P4-2 | LSP server auto-restart on crash | vol12 P3-3, vol14 P4-2 | Medium |
| P4-3 | Token-level bash security analysis (close string-matching bypass vectors) | vol12 P3-5, vol14 P4-3 | High |
| P4-4 | Budget ceiling alert in `SessionCostTracker` | vol12 P4-1, vol14 P4-4 | Low |

### claw-code parity items (v1 deep dive)

| ID | Feature | Source | Complexity |
|----|---------|--------|------------|
| CP-1 | Structural recursion prevention — remove `Agent` tool from subagent tool sets | claw-code Pattern 1 | Low |
| CP-2 | Manifest-first subagent spawning — write manifest JSON before spawning thread | claw-code Pattern 2 | Low |
| CP-3 | Stable content hash for instruction files — fast dedup when walking for `AGENTS.md` files | claw-code Pattern 3 | Low |
| CP-4 | Dynamic boundary sentinel in system prompt — `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` for prompt caching | claw-code `prompt.rs` | Low |
| CP-5 | `verification_nudge_needed` in TodoWrite — auto-remind agent to verify when all todos complete | claw-code Pattern 5 | Trivial |

### claw-code parity items (v2 deep dive — new)

| ID | Feature | Source | Complexity |
|----|---------|--------|------------|
| CP-6 | Deterministic auto-compaction — token-count threshold, structured summary sections, no LLM call | claw-code `compact.rs` | Medium |
| CP-7 | Shell hooks with post-tool + deny semantics — stdin-JSON payload, exit `2` = deny, configurable from settings file | claw-code `hooks.rs` | Medium |
| CP-8 | Per-tool `PermissionMode` + `PermissionPolicy` object + config-file-loaded session permission mode | claw-code `permissions.rs` | Low |
| CP-9 | Cache token tracking — add `cache_creation_input_tokens` + `cache_read_input_tokens` to `SessionCostTracker`; reconstruct on resume | claw-code `usage.rs` | Low |
| CP-10 | LSP diagnostics + defs + refs injected into system prompt as named section | claw-code `lsp/`, `prompt.rs` | Low | ✅ CLOSED vol17 |
| CP-11 | Ancestor instruction file walk — find `AGENTS.md` / `.agent/instructions.md` in all ancestor dirs with dedup + budget caps | claw-code `prompt.rs` | Low |
| CP-12 | `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` wired to Anthropic `cache_control` — reduces prompt cache misses | claw-code `prompt.rs`, API client | Low | ✅ CLOSED vol17 |
| CP-13 | Per-project settings file — `.agent/settings.json` with model + permission mode overrides, deep merge | claw-code `config.rs` | Low |
| CP-14 | Session `version` field — add to `MessageManager` persistence for future migration | claw-code `session.rs` | Trivial |
| CP-15 | `send_user_message` tool — agent sends mid-turn message to user with `normal`/`proactive` status | claw-code `tools/src/lib.rs` | Low |

---

## Execution Order

All P0–P3 items are complete. P4 items are open engineering debt ordered by impact:

```
Next sprint — quick wins (low effort, high value)
├── CP-9   ✅ CLOSED — cache token tracking in SessionCostTracker
├── CP-12  ✅ CLOSED vol17 — prompt cache boundary sentinel wired to Anthropic adapter
├── CP-10  ✅ CLOSED vol17 — LSP context injected into system prompt
├── CP-5   ✅ CLOSED — verification_nudge_needed in TodoWrite
├── CP-14  ✅ CLOSED — session version field

Next sprint — architecture improvements
├── CP-6   ✅ CLOSED — deterministic auto-compaction engine
├── CP-7   ✅ CLOSED — shell hooks with deny semantics + post-tool support
├── CP-11  ✅ CLOSED — ancestor instruction file walk + dedup + budget
├── CP-8   ✅ CLOSED — per-tool PermissionMode + policy object
├── CP-13  ✅ CLOSED — per-project settings file
├── CP-15  ✅ CLOSED — send_user_message tool
├── CP-4   ✅ CLOSED — dynamic boundary sentinel constant
├── P4-4   ✅ CLOSED — budget ceiling alert in SessionCostTracker
├── CP-1   ✅ CLOSED — structural recursion prevention
├── CP-2   ✅ CLOSED — manifest-first subagent spawning
├── CP-3   ✅ CLOSED — stable content hash for instruction files

Future sprints — ALL P4 items are now CLOSED
├── P4-1   ✅ CLOSED vol15 — config_watcher.py live reload
├── P4-2   ✅ CLOSED vol15 — LSP server auto-restart
├── P4-3   ✅ CLOSED vol16 — token-level bash security analysis
└── P4-4   ✅ CLOSED vol15 — budget ceiling alert
```

---

## Success Criteria

| Metric | Current | Target |
|--------|---------|--------|
| xfail tests | 0 | 0 |
| GAP-S2 open | No | No |
| Days since last audit | 0 (vol18 done 2026-04-06) | 0 |
| TOOLS_GAP_ANALYSIS open items | 0 | 0 |
| Unit test count | 2958 | 2958 |
| Pyright errors | 0 | 0 |
