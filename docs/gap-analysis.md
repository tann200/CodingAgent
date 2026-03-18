# Gap Analysis: Current Architecture vs. Target

This document lists remaining implementation tasks needed to reach the target architecture.

---

## Task Summary

| ID | Task | Priority | Status |
|----|------|----------|--------|
| T1 | Add AnalysisNode | P0 | ✅ Complete |
| T2 | Add DebugNode + Retry Logic | P0 | ✅ Complete |
| T3 | Add Step Controller | P0 | ✅ Complete |
| T4 | Wire Roles Per-Node | P1 | ✅ Complete |
| T5 | Add Dynamic Skills Injection | P1 | ✅ Complete |
| T6 | Add Plan Validator Node | P2 | ❌ |
| T7 | Wire Advanced Memory Features | P2 | ❌ |
| T8 | Add Incremental AST Indexing | P2 | ❌ |
| T9 | Tool Timeout Protection | P1 | ✅ Complete |
| T10 | Structured Verification Diagnostics | P1 | ✅ Complete |

---

## Completed Tasks

### T1: AnalysisNode (Priority P0) ✅

**Files created:**
- `src/core/orchestration/graph/nodes/analysis_node.py`

**Files modified:**
- `src/core/orchestration/graph/builder.py`
- `src/core/orchestration/graph/state.py`

**Acceptance criteria:** ✅

---

### T2: DebugNode + Retry Logic (Priority P0) ✅

**Files created:**
- `src/core/orchestration/graph/nodes/debug_node.py`

**Acceptance criteria:** ✅

---

### T3: Step Controller (Priority P0) ✅

**Files created:**
- `src/core/orchestration/graph/nodes/step_controller_node.py`

**Acceptance criteria:** ✅

---

### T4: Wire Roles Per-Node (Priority P1) ✅

**Files modified:**
- `src/core/orchestration/graph/nodes/analysis_node.py` → analyst role
- `src/core/orchestration/graph/nodes/debug_node.py` → debugger role
- `src/core/orchestration/graph/nodes/verification_node.py` → reviewer role
- `src/core/orchestration/graph/nodes/evaluation_node.py` → reviewer role
- `src/core/orchestration/graph/nodes/replan_node.py` → planner role

**Acceptance criteria:** ✅

---

### T5: Dynamic Skills Injection (Priority P1) ✅

**Implementation in nodes:**
- `perception_node`: context_hygiene skill for debug/fix/error tasks
- `execution_node`: dry skill when >2 relevant files

**Acceptance criteria:** ✅

---

### T6: Plan Validator Node (Priority P2) ✅

**Files created:**
- `src/core/orchestration/graph/nodes/plan_validator_node.py`

**Validations:**
- Plan has at least one step
- Plan references files
- Plan has verification step
- Steps are properly formatted

**Acceptance criteria:** ✅

---

### T9: Tool Timeout Protection (Priority P1) ✅

**Implementation in orchestrator.py:**
- Added `_get_tool_timeout()` method with configurable timeouts
- Default tools: 30s
- bash: 60s
- run_tests: 120s
- Uses signal-based timeout handling

**Acceptance criteria:** ✅

---

### T10: Structured Verification Diagnostics (Priority P1) ✅

**Already implemented in verification_tools.py:**
- `run_tests()` returns: status, passed, failed, failed_tests, errors, tracebacks
- `run_linter()` returns: status, error_count, warning_count, errors (with file, line, column, code, message)
- `syntax_check()` returns: status, checked_files, syntax_errors (with file, line, error)

**Acceptance criteria:** ✅

---

## Remaining Tasks

### T6: Plan Validator Node (Priority P2)

**Problem:** Weak plans can execute without validation.

**Solution:** Add validation node between Planning and Execution.

**Files to create:**
- `src/core/orchestration/graph/nodes/plan_validator_node.py`

**Validations:**
- Plan has verification step
- Plan references files
- Plan steps are ordered

**Acceptance criteria:**
- [ ] Invalid plans rejected
- [ ] Test: weak plan fails validation

---

### T7: Wire Advanced Memory Features (Priority P2)

**Problem:** Advanced features implemented but not wired.

**Solution:** Connect TrajectoryLogger, DreamConsolidator, RefactoringAgent, ReviewAgent.

| Feature | Action | Where to wire |
|---------|--------|---------------|
| TrajectoryLogger | Log each run | orchestrator.py after run_agent_once |
| DreamConsolidator | Schedule at session end | orchestrator.py on session end event |
| RefactoringAgent | Background proposer | New background task |
| ReviewAgent | After execution | After execution_node in graph |
| SkillLearner | After success | After successful verification |

**Acceptance criteria:**
- [ ] Trajectories logged to .agent-context/trajectories/
- [ ] ReviewAgent runs after edits

---

### T8: Incremental AST Indexing (Priority P2)

**Problem:** Full rescan on every run.

**Solution:** Add file watcher + hash comparison.

**Files to create:**
- `src/core/indexing/file_watcher.py`

**Logic:**
1. On file save, compute hash
2. Compare with last indexed hash
3. Only reparse if hash changed
4. Update symbol_graph.db incrementally

**Acceptance criteria:**
- [ ] File changes detected
- [ ] Only modified files reindexed
- [ ] Test: modify file, verify incremental update

---

### T9: Tool Timeout Protection (Priority P1)

**Problem:** Tools can hang indefinitely.

**Solution:** Add per-tool timeouts.

**Files to modify:**
- `src/core/orchestration/orchestrator.py`

**Timeouts:**
- Default tools: 30s
- bash: 60s
- run_tests: 120s
- Output truncation: 500 lines

**Acceptance criteria:**
- [ ] Long-running tools terminated
- [ ] Normalized error returned

---

### T10: Structured Verification Diagnostics (Priority P1)

**Problem:** Verification failures not structured.

**Solution:** Parse pytest output into diagnostics.

**Files to modify:**
- `src/tools/verification_tools.py`

**Output structure:**
```python
{
    "error_type": "test_failure",
    "file": "tests/test_x.py",
    "line": 42,
    "function": "test_login",
    "message": "AssertionError: ..."
}
```

**Acceptance criteria:**
- [ ] Structured diagnostics in verification_result
- [ ] DebugNode can consume diagnostics

---

## Architecture Comparison

### Before (Legacy)
```
perception → planning → execution → verification → memory_sync → end
```

### Current (Implemented)
```
perception → analysis → planning → execution → step_controller → verification → evaluation → (memory_sync|step_controller|end)
         ↓
       replan (on patch size violation)
         ↓
      step_controller
```

### Key Improvements
1. **AnalysisNode** - Repository exploration before planning
2. **DebugNode** - Self-healing with retry logic
3. **StepController** - Enforces plan execution
4. **ReplanNode** - Handles oversized patches
5. **EvaluationNode** - Task completion verification
6. **Node-Specific Roles** - analyst, debugger, reviewer, strategic, operational
7. **Dynamic Skills** - Context-aware skill injection
8. **EventBus Dashboard** - Real-time UI updates
9. **YAML-only Format** - Tool parser uses YAML exclusively

---

# End of Document
