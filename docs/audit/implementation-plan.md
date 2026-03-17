# Implementation Plan: Next-Generation Coding Agent

**Date:** March 17, 2026  
**Based on:** User Requirements & Current System Analysis

---

## Executive Summary

This document outlines the implementation plan for 8 major system upgrades to transform the current agent into a top-tier coding assistant. Each section analyzes the current state, required changes, and provides a concrete implementation roadmap.

**Current System State (as of March 17, 2026):**
| Component | Status | Location |
|-----------|--------|----------|
| AnalysisNode | ✅ EXISTS | `src/core/orchestration/graph/nodes/workflow_nodes.py:51-118` |
| StepController | ✅ EXISTS | `src/core/orchestration/graph/nodes/workflow_nodes.py:1160-1210` |
| DebugNode | ✅ EXISTS | `src/core/orchestration/graph/nodes/workflow_nodes.py:1025-1100` |
| Symbol graph | ✅ EXISTS | `src/core/indexing/symbol_graph.py` |
| Vector search | ✅ EXISTS | `src/core/indexing/vector_store.py` |
| Verification branching | ✅ EXISTS | `src/core/orchestration/graph/builder.py:103-145` |
| Shell injection fix | ✅ EXISTS | `src/tools/file_tools.py:146-210` |
| Sandbox enforcement | ✅ EXISTS | `src/core/orchestration/orchestrator.py:779-810` |
| Read-before-edit | ✅ EXISTS | `src/core/orchestration/orchestrator.py:743-754` |
| Tool cooldowns (state) | ✅ EXISTS | `src/core/orchestration/graph/state.py` |
| Patch-based editing | ❌ NOT IMPLEMENTED | - |
| Deterministic tool contracts | ⚠️ PARTIAL | only basic validation |
| Test targeting | ❌ NOT IMPLEMENTED | runs all tests |
| Debug classification | ❌ NOT IMPLEMENTED | generic retries |
| Full execution tracing | ❌ NOT IMPLEMENTED | basic logs only |
| Scenario evaluation | ❌ NOT IMPLEMENTED | - |

---

## 1. Repo Intelligence Layer (Critical)

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| Symbol graph | ✅ EXISTS | `src/core/indexing/symbol_graph.py:14-270` |
| Vector search | ✅ EXISTS | `src/core/indexing/vector_store.py:44-157` |
| Basic analysis_node | ✅ EXISTS | `src/core/orchestration/graph/nodes/workflow_nodes.py:51-118` |
| search_code tool | ✅ EXISTS | `src/tools/repo_tools.py:21-30` |
| find_symbol tool | ✅ EXISTS | `src/tools/repo_tools.py:32-47` |
| Dependency graph | ❌ NOT IMPLEMENTED | - |
| Entrypoint detection | ❌ NOT IMPLEMENTED | - |
| Test-implementation mapping | ❌ NOT IMPLEMENTED | - |

### Implementation Plan

#### 1.1 Enhance Symbol Graph → Dependency Graph
**File:** `src/core/indexing/symbol_graph.py`

```python
class DependencyGraph:
    """Tracks import relationships between modules."""
    
    def add_import(self, from_file: str, to_module: str):
        """Record that from_file imports to_module."""
        
    def get_dependents(self, module: str) -> List[str]:
        """Get all files that depend on module."""
        
    def get_dependencies(self, module: str) -> List[str]:
        """Get all modules that module depends on."""
        
    def build_from_symbols(self, repo_index: Dict) -> "DependencyGraph":
        """Build from existing symbol index."""
```

#### 1.2 Add Entrypoint Detection
**New File:** `src/core/indexing/entrypoint_detector.py`

```python
def detect_entrypoints(workdir: str) -> List[Dict[str, Any]]:
    """Identify project entrypoints.
    
    Returns:
        - CLI scripts (argparse, click, typer)
        - __main__.py files
        - setup.py / pyproject.toml
        - FastAPI/Flask app definitions
    """
```

#### 1.3 Test-Implementation Mapper
**New File:** `src/core/indexing/test_mapper.py`

```python
def map_tests_to_implementation(
    edited_files: List[str],
    symbol_graph: SymbolGraph,
    dependency_graph: DependencyGraph
) -> List[str]:
    """Find tests relevant to edited files.
    
    Logic:
    1. For each edited file, find direct test files
    2. Also find tests for dependent modules
    3. Return prioritized test list
    """
```

#### 1.4 Enhanced AnalysisNode
**File:** `src/core/orchestration/graph/nodes/workflow_nodes.py`

```python
async def analysis_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """Enhanced analysis with full repo intelligence."""
    
    # Existing: search_code, find_symbol, glob
    
    # NEW: Build dependency graph
    dep_graph = build_dependency_graph(working_dir)
    
    # NEW: Detect entrypoints
    entrypoints = detect_entrypoints(working_dir)
    
    # NEW: Map relevant tests
    relevant_tests = map_tests_to_implementation(
        relevant_files, symbol_graph, dep_graph
    )
    
    return {
        "analysis_summary": ...,
        "relevant_files": ...,
        "key_symbols": ...,
        "dependency_graph": dep_graph,        # NEW
        "entrypoints": entrypoints,           # NEW
        "test_targets": relevant_tests,       # NEW
    }
```

#### 1.5 New State Fields
**File:** `src/core/orchestration/graph/state.py`

```python
class AgentState(TypedDict):
    # ... existing fields ...
    
    # Enhanced repo intelligence
    dependency_graph: Optional[Dict[str, Any]]
    entrypoints: Optional[List[Dict[str, str]]]
    test_targets: Optional[List[str]]
```

### Priority: **P0** (Critical)
**Estimated Effort:** 3-4 days

---

## 2. Step-Level Plan Enforcement

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| step_controller_node | ✅ EXISTS | `src/core/orchestration/graph/nodes/workflow_nodes.py:1160-1210` |
| current_plan tracking | ✅ EXISTS | `src/core/orchestration/graph/state.py:31` |
| current_step tracking | ✅ EXISTS | `src/core/orchestration/graph/state.py:32` |
| PlanStep contract | ❌ NOT IMPLEMENTED | needs TypedDict |
| step_verification_node | ❌ NOT IMPLEMENTED | - |
| Mid-run plan protection | ❌ NOT IMPLEMENTED | LLM can rewrite plan |

### Implementation Plan

#### 2.1 Strict Step Contract
**File:** `src/core/orchestration/graph/state.py`

```python
class PlanStep(TypedDict):
    """Strict plan step definition."""
    step_number: int
    action: str          # read_file | edit_file | write_file | run_tests | etc
    target_file: str     # Specific file path
    goal: str           # What this step achieves
    verification: str   # How to verify success
    completed: bool     # Step completion status
```

#### 2.2 Step-Level Verification
**New Node:** `src/core/orchestration/graph/nodes/step_verification_node.py`

```python
async def step_verification_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """Verify that the current step achieved its goal.
    
    After each step execution:
    1. Check if step.goal was achieved
    2. If not, retry or fail
    3. Mark step.completed = True
    4. Only then proceed to next step
    """
```

#### 2.3 Prevent Mid-Run Plan Rewrite
**File:** `src/core/orchestration/graph/nodes/execution_node.py`

```python
# Add guard: if LLM tries to modify plan mid-execution, reject it
if "current_plan" in tool_args:
    return {
        "status": "error", 
        "error": "Cannot modify plan mid-execution. Complete current step first."
    }
```

#### 2.4 Updated Graph Flow
```
planning → execution → step_verification → (success→next_step | fail→debug)
```

### Priority: **P0** (Critical)
**Estimated Effort:** 2-3 days

---

## 3. Patch-Based Editing (Major Upgrade)

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| edit_file (unified diff) | ✅ EXISTS | `src/tools/file_tools.py:101-143` |
| write_file | ✅ EXISTS | `src/tools/file_tools.py:23-29` |
| patch generation tool | ❌ NOT IMPLEMENTED | - |
| validate_patch | ❌ NOT IMPLEMENTED | - |

### Implementation Plan

#### 3.1 Patch Generator Tool
**New File:** `src/tools/patch_tools.py`

```python
def generate_patch(
    original_file: str,
    edited_content: str,
    diff_context: int = 3
) -> str:
    """Generate unified diff patch from original and edited content."""
    
def apply_patch(file_path: str, patch: str) -> Dict[str, Any]:
    """Apply a patch to a file safely."""
    
def validate_patch(patch: str, file_path: str) -> Dict[str, Any]:
    """Validate patch before application.
    
    Checks:
    - Valid unified diff format
    - No conflicting hunks
    - File exists
    """
```

#### 3.2 Replace write_file with patch generation
**File:** `src/core/orchestration/graph/nodes/execution_node.py`

```python
# Instead of:
# write_file(path, content)

# New flow:
# 1. read_file(path) → original
# 2. generate_patch(original, new_content) → patch
# 3. apply_patch(path, patch)
```

#### 3.3 Tool Contract Update
**File:** `src/core/orchestration/tool_registry.py`

```python
# Deprecate write_file for code changes
# Prefer: edit_file with patch

TOOL_CONTRACTS = {
    "edit_file": {
        "required": ["path", "patch"],  # Changed from "content"
        "patch_format": "unified_diff",  # NEW constraint
    }
}
```

### Priority: **P1** (High)
**Estimated Effort:** 2 days

---

## 4. Deterministic Tool Contracts

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| Basic tool contracts | ✅ EXISTS | `src/core/orchestration/tool_contracts.py:1-80` |
| ToolRegistry | ✅ EXISTS | `src/core/orchestration/orchestrator.py:83-106` |
| Strict edit_file schema | ❌ NOT IMPLEMENTED | - |
| Schema enforcement | ⚠️ PARTIAL | no `extra = "forbid"` |

### Implementation Plan

#### 4.1 Strict Schema for edit_file
**File:** `src/core/orchestration/tool_contracts.py`

```python
from pydantic import BaseModel, Field

class EditFileContract(BaseModel):
    """Strict contract for file editing."""
    
    path: str = Field(..., description="Target file path")
    patch: str = Field(..., description="Unified diff patch")
    
    # Constraints
    class Config:
        extra = "forbid"  # Reject unknown fields
        
def validate_edit_contract(path: str, patch: str) -> ValidationResult:
    """Validate edit meets safety criteria."""
    
    # Max 200 lines changed
    # No delete entire file
    # Must be unified diff format
```

#### 4.2 Automatic Schema Validation
**File:** `src/core/orchestration/orchestrator.py`

```python
def execute_tool(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
    name = tool_call.get("name")
    
    # Enforce strict contract
    if name == "edit_file":
        try:
            contract = EditFileContract(**tool_call.get("arguments", {}))
        except ValidationError as e:
            return {"ok": False, "error": f"Contract violation: {e}"}
    
    # Existing validation continues...
```

### Priority: **P1** (High)
**Estimated Effort:** 1 day

---

## 5. Automatic Test Targeting

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| run_tests | ✅ EXISTS | `src/tools/verification_tools.py:7-15` |
| run_linter | ✅ EXISTS | `src/tools/verification_tools.py:18-26` |
| syntax_check | ✅ EXISTS | `src/tools/verification_tools.py:29-46` |
| Test mapper | ❌ NOT IMPLEMENTED | - |
| Targeted testing | ❌ NOT IMPLEMENTED | runs all tests |

### Implementation Plan

#### 5.1 Test Mapper Implementation
**File:** `src/core/indexing/test_mapper.py` (from Section 1)

```python
def find_tests_for_file(file_path: str) -> List[str]:
    """Find test files that test this file.
    
    Strategies:
    1. Same directory: file.py → test_file.py
    2. tests/ directory: tests/test_module.py
    3. By import: if file uses unittest/pytest
    """
    
def find_tests_for_symbol(symbol_name: str) -> List[str]:
    """Find tests that reference this symbol."""
```

#### 5.2 Integration with Verification
**File:** `src/tools/verification_tools.py`

```python
def run_targeted_tests(workdir: str, target_files: List[str]) -> Dict[str, Any]:
    """Run only tests relevant to target files."""
    
    test_files = []
    for f in target_files:
        tests = find_tests_for_file(f)
        test_files.extend(tests)
    
    if not test_files:
        # Fallback to all tests
        return run_tests(workdir)
    
    # Run specific tests
    proc = subprocess.run(
        ['pytest', '-q', *test_files],
        cwd=workdir,
        capture_output=True,
        text=True
    )
    return {...}
```

#### 5.3 Updated Verification Node
**File:** `src/core/orchestration/graph/nodes/verification_node.py`

```python
async def verification_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """Run targeted verification based on edited files."""
    
    edited_files = state.get("edited_files", [])
    test_targets = state.get("test_targets", [])
    
    # Use targeted testing if available
    if test_targets:
        test_result = run_targeted_tests(working_dir, test_targets)
    else:
        test_result = run_tests(working_dir)
    
    return {"verification_result": {...}}
```

### Priority: **P1** (High)
**Estimated Effort:** 1-2 days

---

## 6. Structured Debugging Engine

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| DebugNode | ✅ EXISTS | `src/core/orchestration/graph/nodes/workflow_nodes.py:1025-1100` |
| max_debug_attempts | ✅ EXISTS | `src/core/orchestration/graph/state.py:40` |
| debug_attempts tracking | ✅ EXISTS | `src/core/orchestration/graph/state.py:39` |
| Failure classifier | ❌ NOT IMPLEMENTED | - |
| Fix strategies | ❌ NOT IMPLEMENTED | generic only |
| FailureType enum | ❌ NOT IMPLEMENTED | - |

### Implementation Plan

#### 6.1 Failure Classifier
**New File:** `src/core/orchestration/debug/failure_classifier.py`

```python
from enum import Enum

class FailureType(Enum):
    SYNTAX_ERROR = "syntax_error"
    LINT_ERROR = "lint_error"
    TEST_FAILURE = "test_failure"
    MISSING_IMPORT = "missing_import"
    RUNTIME_ERROR = "runtime_error"
    ASSERTION_ERROR = "assertion_error"
    ATTRIBUTE_ERROR = "attribute_error"
    TYPE_ERROR = "type_error"
    UNKNOWN = "unknown"

def classify_failure(error_output: str) -> FailureType:
    """Classify failure type from error output."""
    
    error_lower = error_output.lower()
    
    if "syntaxerror" in error_lower:
        return FailureType.SYNTAX_ERROR
    elif "import error" in error_lower or "modulenotfound" in error_lower:
        return FailureType.MISSING_IMPORT
    elif "test" in error_lower and "fail" in error_lower:
        return FailureType.TEST_FAILURE
    # ... etc
```

#### 6.2 Strategy Selector
**New File:** `src/core/orchestration/debug/fix_strategies.py`

```python
def get_fix_strategy(failure_type: FailureType) -> FixStrategy:
    """Return appropriate fix strategy for failure type."""
    
    strategies = {
        FailureType.SYNTAX_ERROR: fix_syntax,
        FailureType.LINT_ERROR: auto_format,
        FailureType.TEST_FAILURE: inspect_and_fix_test,
        FailureType.MISSING_IMPORT: add_import,
        FailureType.RUNTIME_ERROR: analyze_traceback,
        # ...
    }
    return strategies.get(failure_type, generic_fix)
```

#### 6.3 Enhanced DebugNode
**File:** `src/core/orchestration/graph/nodes/workflow_nodes.py`

```python
async def debug_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """Enhanced debugging with failure classification."""
    
    error_output = extract_error(state.get("last_result"))
    
    # Classify failure
    failure_type = classify_failure(error_output)
    
    # Get appropriate strategy
    strategy = get_fix_strategy(failure_type)
    
    # Generate targeted fix
    fix = strategy(error_output, state.get("task"))
    
    return {
        "next_action": fix,
        "failure_type": failure_type.value,  # Log for analysis
    }
```

### Priority: **P1** (High)
**Estimated Effort:** 2 days

---

## 7. Full Execution Tracing

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| Execution trace logging | ⚠️ PARTIAL | `src/core/memory/advanced_features.py:11-50` (TrajectoryLogger) |
| TASK_STATE.md | ✅ EXISTS | `src/core/memory/distiller.py:115-131` |
| Tool call logging | ⚠️ PARTIAL | via orchestrator logs |
| Structured trace schema | ❌ NOT IMPLEMENTED | - |
| Trace recorder class | ❌ NOT IMPLEMENTED | - |
| execution_trace.json | ❌ NOT IMPLEMENTED | - |

### Implementation Plan

#### 7.1 Trace Schema
**New File:** `src/core/orchestration/trace.py`

```python
from datetime import datetime
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

class ToolCall(BaseModel):
    timestamp: str
    tool_name: str
    arguments: Dict[str, Any]
    result: Dict[str, Any]
    duration_ms: int

class StepRecord(BaseModel):
    step_number: int
    action: str
    target_file: Optional[str]
    goal: str
    verification: str
    status: str  # success | failure
    tool_calls: List[ToolCall]

class ExecutionTrace(BaseModel):
    run_id: str
    timestamp: str
    task: str
    plan: List[Dict[str, Any]]
    steps: List[StepRecord]
    final_status: str  # success | failure | timeout
    total_tool_calls: int
    total_duration_ms: int
```

#### 7.2 Trace Recorder
**New File:** `src/core/orchestration/trace.py`

```python
class TraceRecorder:
    """Records execution trace for analysis."""
    
    def __init__(self, workdir: str):
        self.workdir = Path(workdir)
        self.trace: ExecutionTrace = {}
        self.current_step: Optional[StepRecord] = None
        
    def start_run(self, task: str, plan: List[Dict]):
        """Initialize trace for new run."""
        
    def record_tool_call(self, tool_name: str, args: Dict, result: Dict):
        """Record tool call."""
        
    def start_step(self, step: Dict):
        """Start new step."""
        
    def end_step(self, status: str):
        """Complete step."""
        
    def finalize(self, final_status: str):
        """Write trace to disk."""
        
    def save(self):
        """Save trace to execution_trace.json"""
```

#### 7.3 Integrate with Orchestrator
**File:** `src/core/orchestration/orchestrator.py`

```python
class Orchestrator:
    def __init__(self, ...):
        # ... existing init
        self.trace_recorder = TraceRecorder(working_dir)
        
    def run_agent_once(self, prompt: str):
        # Initialize trace
        self.trace_recorder.start_run(prompt, current_plan)
        
        # ... existing execution
        
        # Record tool calls
        self.trace_recorder.record_tool_call(tool_name, args, result)
        
        # Finalize
        self.trace_recorder.finalize(final_status)
```

### Priority: **P2** (Medium)
**Estimated Effort:** 2 days

---

## 8. Scenario Evaluation System

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| Unit tests | ✅ EXISTS | `tests/unit/` (70+ files) |
| Integration tests | ✅ EXISTS | `tests/integration/` |
| Test fixtures | ✅ EXISTS | `tests/conftest.py` |
| Scenario benchmarks | ❌ NOT IMPLEMENTED | - |
| Scenario runner | ❌ NOT IMPLEMENTED | - |
| Success rate metrics | ❌ NOT IMPLEMENTED | - |

### Implementation Plan

#### 8.1 Scenario Definition
**New Directory:** `tests/scenarios/`

```
tests/scenarios/
├── fix_failing_test/
│   ├── task.txt          # Task description
│   ├── repo_snapshot/    # Initial repo state
│   └── expected.json     # Expected outcome
├── add_cli_argument/
│   ├── task.txt
│   ├── repo_snapshot/
│   └── expected.json
├── refactor_duplicate/
│   ├── task.txt
│   ├── repo_snapshot/
│   └── expected.json
└── ...
```

#### 8.2 Scenario Runner
**New File:** `tests/scenarios/run_scenarios.py`

```python
import subprocess
import json
from pathlib import Path
from dataclasses import dataclass

@dataclass
class ScenarioResult:
    scenario: str
    success: bool
    tool_calls: int
    duration_seconds: float
    error: Optional[str]

def run_scenario(scenario_dir: str) -> ScenarioResult:
    """Run a single scenario and measure results."""
    
    # 1. Restore repo snapshot
    # 2. Execute agent with task
    # 3. Evaluate outcome
    # 4. Return metrics

def evaluate_all_scenarios() -> Dict[str, ScenarioResult]:
    """Run all scenarios and produce report."""
    
    scenarios = Path("tests/scenarios").glob("*/")
    results = {}
    
    for scenario in scenarios:
        results[scenario.name] = run_scenario(scenario)
    
    # Generate report
    success_rate = sum(r.success for r in results.values()) / len(results)
    avg_tool_calls = sum(r.tool_calls for r in results.values()) / len(results)
    
    return {
        "results": results,
        "success_rate": success_rate,
        "avg_tool_calls": avg_tool_calls,
    }
```

#### 8.3 Initial Scenario Set
Create 10 basic scenarios:

1. **fix_failing_test** - Fix a deliberately broken test
2. **add_cli_argument** - Add new CLI argument using argparse
3. **refactor_duplicate** - Extract duplicated code into function
4. **add_logging** - Add logging to a function
5. **implement_todo** - Implement a TODO comment
6. **fix_import_error** - Fix missing import
7. **add_type_hints** - Add type hints to function
8. **write_test** - Write test for existing function
9. **fix_syntax_error** - Fix syntax error
10. **optimize_loop** - Optimize slow loop

### Priority: **P2** (Medium)
**Estimated Effort:** 3-4 days

---

## Implementation Roadmap

### Phase 1: Core Intelligence (Week 1-2)
| Task | Effort | Priority |
|------|--------|----------|
| Enhance AnalysisNode with dependency graph | 2 days | P0 |
| Add entrypoint detection | 1 day | P0 |
| Implement test mapper | 1 day | P0 |
| Step-level plan enforcement | 2 days | P0 |

### Phase 2: Safety & Reliability (Week 2-3)
| Task | Effort | Priority |
|------|--------|----------|
| Patch-based editing | 2 days | P1 |
| Deterministic tool contracts | 1 day | P1 |
| Automatic test targeting | 1 day | P1 |
| Structured debugging | 2 days | P1 |

### Phase 3: Observability (Week 3-4)
| Task | Effort | Priority |
|------|--------|----------|
| Full execution tracing | 2 days | P2 |
| Scenario evaluation | 3 days | P2 |

---

## Files to Modify/Create Summary

### New Files to Create
```
src/core/orchestration/debug/
├── __init__.py
├── failure_classifier.py
└── fix_strategies.py

src/core/indexing/
├── entrypoint_detector.py
└── test_mapper.py

src/tools/
└── patch_tools.py

src/core/orchestration/
├── trace.py
└── step_verification_node.py

tests/scenarios/
├── run_scenarios.py
├── fix_failing_test/
├── add_cli_argument/
└── ... (7 more)
```

### Files to Modify
```
src/core/orchestration/graph/
├── nodes/workflow_nodes.py      # Enhanced analysis_node, debug_node
├── builder.py                   # Add step_verification node
└── state.py                    # New fields

src/core/orchestration/
├── orchestrator.py              # Trace integration, strict contracts
└── tool_contracts.py            # Deterministic schemas

src/tools/
└── verification_tools.py       # Targeted testing

src/core/indexing/
└── symbol_graph.py              # Dependency tracking
```

---

## Implementation Status Summary (as of March 17, 2026)

### What EXISTS (Ready to Build Upon)
| Component | File | Lines | Status |
|-----------|------|-------|--------|
| AnalysisNode | `src/core/orchestration/graph/nodes/workflow_nodes.py` | 51-118 | ✅ READY |
| StepController | `src/core/orchestration/graph/nodes/workflow_nodes.py` | 1160-1210 | ✅ READY |
| DebugNode | `src/core/orchestration/graph/nodes/workflow_nodes.py` | 1025-1100 | ✅ READY |
| SymbolGraph | `src/core/indexing/symbol_graph.py` | 14-270 | ✅ READY |
| VectorStore | `src/core/orchestration/graph/state.py` | 1-60 | ✅ READY |
| Graph Builder | `src/core/orchestration/graph/builder.py` | 1-288 | ✅ READY |
| Tool Registry | `src/core/orchestration/orchestrator.py` | 83-106 | ✅ READY |
| Verification tools | `src/tools/verification_tools.py` | 1-46 | ✅ READY |
| TrajectoryLogger | `src/core/memory/advanced_features.py` | 11-50 | ✅ READY |

### What NEEDS Implementation
| Component | Priority | Effort | New File Required |
|-----------|----------|--------|-------------------|
| DependencyGraph class | P0 | 2 days | `src/core/indexing/dependency_graph.py` |
| Entrypoint detector | P0 | 1 day | `src/core/indexing/entrypoint_detector.py` |
| Test mapper | P0 | 1 day | `src/core/indexing/test_mapper.py` |
| PlanStep TypedDict | P0 | 0.5 day | `src/core/orchestration/graph/state.py` |
| Step verification node | P0 | 1 day | `src/core/orchestration/graph/nodes/step_verification_node.py` |
| Mid-run plan protection | P0 | 0.5 day | `src/core/orchestration/graph/nodes/execution_node.py` |
| Patch tools | P1 | 2 days | `src/tools/patch_tools.py` |
| Deterministic contracts | P1 | 1 day | `src/core/orchestration/tool_contracts.py` |
| Targeted test runner | P1 | 1 day | `src/tools/verification_tools.py` |
| Failure classifier | P1 | 1 day | `src/core/orchestration/debug/failure_classifier.py` |
| Fix strategies | P1 | 1 day | `src/core/orchestration/debug/fix_strategies.py` |
| Trace recorder | P2 | 2 days | `src/core/orchestration/trace.py` |
| Scenario runner | P2 | 3 days | `tests/scenarios/run_scenarios.py` |

### Quick Win Implementations (Low Effort, High Impact)
| Implementation | Effort | Impact | File to Modify |
|----------------|--------|--------|----------------|
| Add dependency tracking to SymbolGraph | 1 day | High | `src/core/indexing/symbol_graph.py` |
| Add failure_type to DebugNode output | 0.5 day | Medium | `src/core/orchestration/graph/nodes/workflow_nodes.py` |
| Add max_edit_lines validation | 0.5 day | Medium | `src/core/orchestration/tool_contracts.py` |
| Wire test_targets to verification | 1 day | High | `src/tools/verification_tools.py` |

---

## Phase 3: Missing Critical Components (User-Identified Gaps)

### Current State - CODE MAPPING

| Component | Status | File:Line |
|-----------|--------|-----------|
| ContextBuilder | ✅ EXISTS | `src/core/context/context_builder.py:1-400` |
| MessageManager | ✅ EXISTS | `src/core/orchestration/message_manager.py:1-100` |
| ContextHygiene skill | ✅ EXISTS | `src/core/memory/skills/context_hygiene.md` |
| read_file (full) | ✅ EXISTS | `src/tools/file_tools.py:32-50` |
| read_file_chunk | ✅ EXISTS | `src/tools/file_tools.py:52-65` |
| ContextController | ❌ NOT IMPLEMENTED | auto-enforcement needed |
| File chunking system | ❌ NOT IMPLEMENTED | symbol-based reading |
| Plan revision loop | ❌ NOT IMPLEMENTED | - |
| Tool result normalization | ❌ NOT IMPLEMENTED | - |
| Workspace safety guard | ❌ NOT IMPLEMENTED | - |

---

## 9. Context Budget Controller (Critical)

### Why It Matters
Local LLM coding agents fail primarily because of context overload. Without automatic enforcement, local models will degrade quickly.

### Implementation Plan

#### 9.1 ContextController Class
**New File:** `src/core/context/context_controller.py`

```python
class ContextController:
    """Manages context budget to prevent token overflow."""
    
    def __init__(self, max_context_tokens: int = 6000):
        self.max_context_tokens = max_context_tokens
        
    def enforce_budget(
        self, 
        files_to_include: List[Dict],
        conversation_history: List[Dict]
    ) -> List[Dict]:
        """Drop least relevant files, summarize large files."""
        
        # 1. Estimate current context size
        # 2. If > max, prioritize by relevance score
        # 3. Drop lowest scored files
        # 4. Summarize large files (>500 lines)
        
    def summarize_file(self, file_path: str) -> str:
        """Generate file summary for context."""
        
    def get_relevant_snippets(
        self, 
        file_path: str, 
        query: str,
        max_tokens: int = 500
    ) -> List[str]:
        """Extract relevant code sections based on query."""
```

#### 9.2 Integration with ContextBuilder
**File:** `src/core/context/context_builder.py`

```python
def build_prompt(self, ...):
    # NEW: Enforce context budget before building
    controller = ContextController(max_context_tokens=6000)
    relevant_files = controller.enforce_budget(
        files_to_include, 
        conversation
    )
    # Existing logic continues with budget-enforced files
```

### Priority: **P0** (Critical)
**Estimated Effort:** 1-2 days

---

## 10. File Chunking System (Symbol-Based Reading)

### Why It Matters
Agents should never load entire files blindly. Cursor and Claude Code rely heavily on symbol-level context loading.

### Implementation Plan

#### 10.1 Enhanced read_file with Line Ranges
**File:** `src/tools/file_tools.py`

```python
def read_file(
    path: str, 
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    symbol: Optional[str] = None,
    summarize: bool = False,
    workdir: Path = DEFAULT_WORKDIR
) -> Dict[str, Any]:
    """Read file with optional line range or symbol-based selection."""
    
    # If symbol provided, find its line range
    if symbol:
        start_line, end_line = find_symbol_lines(path, symbol)
    
    # If range provided, read only that section
    if start_line and end_line:
        lines = full_content.splitlines()
        content = "\n".join(lines[start_line:end_line])
```

#### 10.2 Symbol-Based Reading Tool
**New File:** `src/tools/symbol_reader.py`

```python
def read_symbol(symbol_name: str, workdir: str) -> Dict[str, Any]:
    """Find and read the definition of a symbol.
    
    Uses symbol graph to locate:
    - Function definition
    - Class definition
    - Constant definition
    """
    
def read_function(function_name: str, workdir: str) -> Dict[str, Any]:
    """Read a specific function's code."""
    
def read_class(class_name: str, workdir: str) -> Dict[str, Any]:
    """Read a specific class's code."""
```

### Priority: **P0** (Critical)
**Estimated Effort:** 1-2 days

---

## 11. Plan Revision Loop

### Why It Matters
Agents need controlled replanning when:
- 3 debug failures occur
- Verification fails
- Missing dependency discovered

### Implementation Plan

#### 11.1 ReplanNode
**New Node:** `src/core/orchestration/graph/nodes/replan_node.py`

```python
async def replan_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """Controlled replanning when stuck.
    
    Trigger conditions:
    - 3 consecutive debug failures
    - verification failure after all retries
    - New dependency discovered
    """
    
    debug_failures = state.get("debug_failures", 0)
    verification_passed = state.get("verification_passed")
    
    should_replan = (
        debug_failures >= 3 or 
        (verification_passed == False and state.get("rounds", 0) > 5)
    )
    
    if should_replan:
        # Generate new plan from scratch
        return {
            "current_plan": generate_new_plan(state),
            "current_step": 0,
            "debug_failures": 0,  # Reset on replan
        }
    
    return {"current_plan": state.get("current_plan")}
```

#### 11.2 Updated Graph Flow
```
debug → replan → step_controller
           ↓ (if replanning triggered)
planning → (fresh plan)
```

### Priority: **P1** (High)
**Estimated Effort:** 1 day

---

## 12. Tool Result Normalization

### Why It Matters
Structured tool outputs allow the agent to reason much better about results.

### Implementation Plan

#### 12.1 Normalized run_tests Output
**File:** `src/tools/verification_tools.py`

```python
def run_tests(workdir: str) -> Dict[str, Any]:
    """Run tests and return STRUCTURED output."""
    
    proc = subprocess.run(
        ['pytest', '-v', '--tb=short'],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120
    )
    
    # Parse pytest output into structured format
    return {
        "status": "ok" if proc.returncode == 0 else "fail",
        "passed": extract_passed_count(proc.stdout),
        "failed": extract_failed_count(proc.stdout),
        "failed_tests": extract_failed_tests(proc.stdout),
        "passed_tests": extract_passed_tests(proc.stdout),
        "errors": extract_errors(proc.stdout),
        "tracebacks": extract_tracebacks(proc.stdout),
        "summary": proc.stdout[-500:],  # Last 500 chars for context
    }
```

#### 12.2 Normalized run_linter Output
**File:** `src/tools/verification_tools.py`

```python
def run_linter(workdir: str) -> Dict[str, Any]:
    """Run linter and return STRUCTURED output."""
    
    # Similar structure:
    return {
        "status": "ok" if proc.returncode == 0 else "fail",
        "error_count": ...,
        "errors": [
            {"file": "src/foo.py", "line": 10, "message": "..."}
        ],
        "warning_count": ...,
    }
```

### Priority: **P1** (High)
**Estimated Effort:** 1 day

---

## 13. Workspace Safety Guard

### Why It Matters
Agents should never modify critical files without explicit approval.

### Implementation Plan

#### 13.1 Protected Files List
**New File:** `src/core/orchestration/workspace_guard.py`

```python
PROTECTED_PATTERNS = [
    ".git/",
    ".env",
    "requirements.txt",
    "package-lock.json",
    "pyproject.toml",
    "setup.py",
    "Cargo.lock",
    "go.sum",
    ".npmrc",
    ".pypirc",
]

def is_protected(path: str) -> bool:
    """Check if path matches protected pattern."""
    
def require_approval(path: str) -> bool:
    """Return True if explicit user approval required."""
    
def guard_write_operation(tool_name: str, path: str) -> Dict[str, Any]:
    """Validate write operations against protected files."""
    
    if is_protected(path):
        return {
            "status": "error",
            "error": f"Protected file: {path}. Explicit user approval required.",
            "requires_approval": True,
        }
```

#### 13.2 Integration with Orchestrator
**File:** `src/core/orchestration/orchestrator.py`

```python
def execute_tool(self, tool_call: Dict[str, Any]) -> Dict[str, Any]:
    # NEW: Check workspace guard before write operations
    if tool_name in ["write_file", "edit_file"]:
        guard_result = guard_write_operation(tool_name, path_arg)
        if guard_result.get("status") == "error":
            return guard_result
    
    # Existing logic continues
```

### Priority: **P1** (High)
**Estimated Effort:** 0.5 day

---

## 14. Automatic Repo Summary (Quick Win)

### Why It Matters
Before analysis, agents need a quick overview of the repo structure.

### Implementation Plan

#### 14.1 Repo Summary Tool
**New File:** `src/tools/repo_summary.py`

```python
def generate_repo_summary(workdir: str) -> Dict[str, Any]:
    """Generate quick repo overview."""
    
    return {
        "framework": detect_framework(workdir),  # FastAPI, Flask, Django, etc.
        "languages": detect_languages(workdir),   # Python, JS, Go, etc.
        "test_framework": detect_test_framework(workdir),  # pytest, unittest, etc.
        "entrypoints": detect_entrypoints(workdir),  # main.py, app.py, etc.
        "modules": list_subdirectories(workdir),
        "dependency_files": find_dependency_files(workdir),
    }
```

#### 14.2 Wire to Analysis Node
**File:** `src/core/orchestration/graph/nodes/workflow_nodes.py`

```python
async def analysis_node(state: AgentState, config: Any) -> Dict[str, Any]:
    # NEW: Run at start of analysis
    repo_summary = generate_repo_summary(working_dir)
    
    # Existing analysis...
    
    return {
        "repo_summary": repo_summary,  # NEW
        # ... existing fields
    }
```

### Priority: **P1** (High)
**Estimated Effort:** 0.5 day

---

## 15. Edit Size Guard (Quick Win)

### Why It Matters
Prevents catastrophic huge rewrites.

### Implementation Plan

**File:** `src/core/orchestration/tool_contracts.py`

```python
MAX_PATCH_LINES = 200

def validate_patch_size(patch: str) -> Dict[str, Any]:
    """Validate patch doesn't exceed size limit."""
    
    added_lines = patch.count("+")
    removed_lines = patch.count("-")
    total_changes = added_lines + removed_lines
    
    if total_changes > MAX_PATCH_LINES:
        return {
            "status": "error",
            "error": f"Patch too large ({total_changes} lines). "
                    f"Max allowed: {MAX_PATCH_LINES}. "
                    f"Split into smaller edits.",
            "requires_split": True,
        }
    
    return {"status": "ok"}
```

### Priority: **P1** (High)
**Estimated Effort:** 0.5 day

---

## Corrected Target Architecture

```
User Task
   ↓
Perception
   ↓
Repo Summary (NEW)
   ↓
Analysis + Repo Intelligence
   ↓
Planning
   ↓
Step Controller
   ↓
Execution
   ↓
Patch Generator (NEW)
   ↓
Patch Validator (NEW)
   ↓
Patch Apply
   ↓
Verification
   ↓
Debug
   ↓
Replan (NEW)
   ↓
Context Budget Controller (NEW)
   ↓
Memory
```

---

## Final Capability Estimate

| Capability | Level | Notes |
|------------|-------|-------|
| Architecture | 9/10 | Comprehensive pipeline |
| Reliability | 8.5/10 | With context budget + replan |
| Safety | 9/10 | Sandbox + workspace guard + protected files |
| Debugging | 8/10 | With failure classification |
| Local LLM compatibility | 9/10 | Context budget prevents overflow |

---

## Complete Implementation Checklist

### Phase 1: Core (Already Done)
- [x] AnalysisNode
- [x] StepController  
- [x] DebugNode
- [x] Verification branching

### Phase 2: Safety & Reliability (From Original Plan)
- [ ] DependencyGraph class - `src/core/indexing/dependency_graph.py`
- [ ] Entrypoint detector - `src/core/indexing/entrypoint_detector.py`
- [ ] Test mapper - `src/core/indexing/test_mapper.py`
- [ ] PlanStep TypedDict - `src/core/orchestration/graph/state.py`
- [ ] Step verification node - `src/core/orchestration/graph/nodes/step_verification_node.py`
- [ ] Patch tools - `src/tools/patch_tools.py`
- [ ] Deterministic contracts - `src/core/orchestration/tool_contracts.py`
- [ ] Targeted test runner - `src/tools/verification_tools.py`
- [ ] Failure classifier - `src/core/orchestration/debug/failure_classifier.py`

### Phase 3: Missing Critical Components (NEW)
- [ ] ContextController - `src/core/context/context_controller.py`
- [ ] File chunking system - `src/tools/file_tools.py`, `src/tools/symbol_reader.py`
- [ ] ReplanNode - `src/core/orchestration/graph/nodes/replan_node.py`
- [ ] Tool result normalization - `src/tools/verification_tools.py`
- [ ] Workspace safety guard - `src/core/orchestration/workspace_guard.py`
- [ ] Repo summary tool - `src/tools/repo_summary.py`
- [ ] Edit size guard - `src/core/orchestration/tool_contracts.py`

---

**End of Implementation Plan**
