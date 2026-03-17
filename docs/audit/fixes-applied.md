# System Audit Fixes Applied

**Date:** March 17, 2026  
**Status:** COMPLETED

---

## Phase 1: Critical Reliability Fixes (COMPLETED)

### 1. Shell Injection Fix ✅
**File:** `src/tools/file_tools.py`

**Changes:**
- Changed `shell=True` to `shell=False`
- Added dangerous pattern detection before command parsing
- Blocks operators: `&&`, `||`, `;`, `|`, `>`, `>>`, `<`, `$(`, `` ` ``
- Blocks destructive commands: `rm -rf`, `del`, `format`, etc.
- Expanded allowed commands list

**Security Impact:** CRITICAL - Prevents shell injection attacks

---

### 2. AnalysisNode ✅
**Files:** 
- `src/core/orchestration/graph/nodes/workflow_nodes.py` (new node)
- `src/core/orchestration/graph/builder.py` (added to graph)
- `src/core/orchestration/graph/state.py` (new state fields)

**Changes:**
- New `analysis_node()` that runs between perception and planning
- Uses repository intelligence tools: `search_code`, `find_symbol`, `glob`
- Outputs: `analysis_summary`, `relevant_files`, `key_symbols`

**Graph Flow:**
```
perception → analysis → planning → execution → step_controller → verification
```

---

### 3. DebugNode with Retry Logic ✅
**Files:**
- `src/core/orchestration/graph/nodes/workflow_nodes.py` (new node)
- `src/core/orchestration/graph/builder.py` (conditional routing)
- `src/core/orchestration/graph/state.py` (debug fields)

**Changes:**
- New `debug_node()` analyzes verification failures
- Max 3 retry attempts
- Generates fix tool calls automatically
- Conditional routing: `verification → (success→memory | failure→debug→execution)`

---

### 4. Step Controller ✅
**Files:**
- `src/core/orchestration/graph/nodes/workflow_nodes.py` (new node)
- `src/core/orchestration/graph/builder.py` (conditional routing)

**Changes:**
- New `step_controller_node()` enforces single-step execution
- Tracks plan progress with `current_plan` and `current_step`
- Prevents tool thrashing and random edits

---

### 5. Verification Branching ✅
**File:** `src/core/orchestration/graph/builder.py`

**Changes:**
- Conditional routing after verification:
  - Success → `memory_sync`
  - Failure + retries remaining → `debug`
  - Failure + max retries → `end`

---

### 6. Sandbox Enforcement ✅
**File:** `src/core/orchestration/orchestrator.py`

**Changes:**
- AST validation for Python files before write operations
- Called in `execute_tool()` before write operations
- Prevents syntax errors from being written

---

## Phase 2: Reliability Hardening (COMPLETED)

### 7. Repo-Aware Planning ✅
**File:** `src/core/orchestration/graph/nodes/workflow_nodes.py`

**Changes:**
- Planning prompt now includes repository context:
  - Relevant files from analysis
  - Key symbols discovered
  - Analysis summary
- Forces explicit file references in plan steps

---

### 8. Read Before Edit Enforcement ✅
**File:** `src/core/orchestration/orchestrator.py`

**Changes:**
- Tracks files read in `_session_read_files` set
- Blocks `edit_file` if file not previously read
- Clear error message: "You must read '{path}' before editing"

---

### 9. Tool Cooldowns & Budgeting (State Added) ✅
**File:** `src/core/orchestration/graph/state.py`

**New State Fields:**
- `tool_last_used: Dict[str, int]` - tracks when tools were last used
- `tool_call_count: int` - total tool calls this session
- `max_tool_calls: int` - limit (default: 30)
- `files_read: Dict[str, bool]` - tracks read files

---

## New State Fields Summary

```python
class AgentState(TypedDict):
    # ... existing fields ...
    
    # Analysis phase
    analysis_summary: Optional[str]
    relevant_files: Optional[List[str]]
    key_symbols: Optional[List[str]]
    
    # Debug retry
    debug_attempts: Optional[int]
    max_debug_attempts: int
    
    # Verification
    verification_passed: Optional[bool]
    verification_result: Optional[Dict[str, Any]]
    
    # Step controller
    step_controller_enabled: bool
    
    # Task decomposition
    task_decomposed: Optional[bool]
    
    # Tool management
    tool_last_used: Optional[Dict[str, int]]
    tool_call_count: int
    max_tool_calls: int
    files_read: Optional[Dict[str, bool]]
```

---

## Tests Added

**File:** `tests/unit/test_audit_fixes.py`

- `TestShellInjectionFix` - 4 tests for bash security
- `TestAnalysisNode` - 2 tests for analysis node
- `TestDebugNode` - 2 tests for debug retry
- `TestStepController` - 2 tests for step enforcement
- `TestVerificationBranching` - 3 tests for routing
- `TestGraphBuilder` - 2 tests for graph structure
- `TestStateFields` - 1 test for state typing
- `TestSandboxEnforcement` - 1 test for sandbox

**Total:** 17 tests - ALL PASSING

---

## Graph Architecture

```
                        ┌─────────────┐
                        │  perception │
                        └──────┬──────┘
                               │
                        ┌──────▼──────┐
                        │   analysis  │ (NEW)
                        └──────┬──────┘
                               │
                        ┌──────▼──────┐
                        │   planning  │
                        └──────┬──────┘
                               │
                  ┌────────────┼────────────┐
                  │            │            │
           ┌──────▼──────┐     │     ┌──────▼──────┐
           │  execution  │     │     │ memory_sync │
           └──────┬──────┘     │     └─────────────┘
                  │            │
        ┌─────────┼─────────┐   │
        │         │         │   │
 ┌──────▼──┐ ┌───▼────┐ ┌──▼──────┐
 │ perception│ │step_   │ │  end    │
 │          │ │controller   │          │
 └──────────┘ └───┬────┘ └──────────┘
                 │
          ┌──────▼──────┐
          │verification │
          └──────┬──────┘
                 │
       ┌─────────┼─────────┐
       │         │         │
┌──────▼──┐ ┌───▼────┐ ┌──▼──────┐
│memory_  │ │ debug  │ │  end    │
│sync     │ │ (NEW)  │ │         │
└─────────┘ └───┬────┘ └──────────┘
               │
        ┌──────▼──────┐
        │ execution   │
        │ (retry)     │
        └─────────────┘
```

---

## Remaining Work

### Phase 3: Observability
- [ ] Full execution trace logging to trace.json
- [ ] Plan persistence to .agent-context/PLAN.md
- [ ] Tool failure explanation improvements

### Phase 4: Developer Experience
- [ ] Explain tool failures clearly
- [ ] Add summarize_repo() tool

### Phase 5: Evaluation System
- [ ] Scenario-based tests (SWE-bench style)
- [ ] Success rate measurement
- [ ] Benchmark tool calls and runtime

---

**End of Fix Report**
