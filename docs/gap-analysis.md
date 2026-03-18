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
| T6 | Add Plan Validator Node | P2 | ✅ Complete |
| T7 | Wire Advanced Memory Features | P2 | ✅ Complete |
| T8 | Add Incremental AST Indexing | P2 | ✅ Complete |
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
- `enforce_warnings=True` by default (warnings treated as errors)

**Acceptance criteria:** ✅

---

### T7: Wire Advanced Memory Features (Priority P2) ✅

**Implementation in `memory_update_node.py`:**
- **TrajectoryLogger**: Logs successful runs for training data
- **DreamConsolidator**: Consolidates memories to prevent vector store growth
- **ReviewAgent**: Reviews patches for security issues and TODO comments
- **RefactoringAgent**: Detects code smells in modified Python files
- **SkillLearner**: Auto-creates skill files from successful tasks with 2+ tool calls

**Acceptance criteria:** ✅

---

### T8: Incremental AST Indexing (Priority P2) ✅

**Implementation in `repo_indexer.py`:**
- SHA256-based file hash tracking (version 3.0)
- Metadata file tracks indexed files (`repo_index_meta.json`)
- Only re-indexes changed files on subsequent runs
- Added support for 15+ languages via regex-based symbol extraction
- `SymbolGraph` wired in `analysis_node` Phase 2.4 for call-graph enrichment

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

> **All tasks complete.** All T1–T10 items have been implemented. No open items remain.

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
1. **AnalysisNode** - Repository exploration before planning (3-phase: VectorStore, SymbolGraph, ContextController)
2. **DebugNode** - Self-healing with retry logic and LLM root-cause analysis
3. **StepController** - Enforces plan execution one step at a time
4. **ReplanNode** - Handles oversized patches
5. **EvaluationNode** - Task completion verification
6. **PlanValidatorNode** - Validates plans before execution (enforce_warnings=True)
7. **Node-Specific Roles** - analyst, debugger, reviewer, strategic, operational
8. **Dynamic Skills** - Context-aware skill injection
9. **EventBus Dashboard** - Real-time UI updates
10. **YAML-only Format** - Tool parser uses YAML exclusively
11. **Advanced Memory Wired** - TrajectoryLogger, DreamConsolidator, ReviewAgent, RefactoringAgent, SkillLearner
12. **Multi-file Atomicity** - Step-level transactional snapshots via RollbackManager
13. **ContextController** - Token budget enforcement in analysis_node
14. **Multi-language Indexing** - 15+ languages (Python, JS/TS, Go, Rust, Java, etc.) via regex
15. **SessionStore Wired** - Tool calls, plans, errors persisted to SQLite (session.db)

---

# End of Document
