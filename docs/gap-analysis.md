# Gap Analysis: Current Architecture vs. Target

This document lists all implementation tasks needed to reach the target architecture.

---

## Task Summary

| ID | Task | Priority | Status |
|----|------|----------|--------|
| T1 | Add AnalysisNode | P0 | ❌ |
| T2 | Add DebugNode + Retry Logic | P0 | ❌ |
| T3 | Add Step Controller | P0 | ❌ |
| T4 | Wire Roles Per-Node | P1 | ❌ |
| T5 | Add Dynamic Skills Injection | P1 | ❌ |
| T6 | Add Plan Validator Node | P2 | ❌ |
| T7 | Wire Advanced Memory Features | P2 | ❌ |
| T8 | Add Incremental AST Indexing | P2 | ❌ |
| T9 | Tool Timeout Protection | P1 | ❌ |
| T10 | Structured Verification Diagnostics | P1 | ❌ |

---

## T1: AnalysisNode (Priority P0)

**Problem:** No dedicated analysis phase - agents skip repo exploration.

**Solution:** Add `AnalysisNode` between Perception and Planning.

**Files to create:**
- `src/core/orchestration/graph/nodes/analysis_node.py`

**Files to modify:**
- `src/core/orchestration/graph/builder.py` - add edge from perception→analysis→planning

**State fields to add** (`state.py`):
- `analysis_summary: str`
- `relevant_files: List[str]`
- `key_symbols: List[str]`

**Tools allowed:** list_files, grep, search_code, find_symbol, read_file_chunk

**Role to create:** `agent-brain/roles/analyst.md`

**Acceptance criteria:**
- [ ] AnalysisNode exists and runs after PerceptionNode
- [ ] Outputs analysis_summary, relevant_files, key_symbols
- [ ] Unit tests pass

---

## T2: DebugNode + Retry Logic (Priority P0)

**Problem:** No automatic retry on verification failure.

**Solution:** Add DebugNode with max 3 retry attempts.

**Files to create:**
- `src/core/orchestration/graph/nodes/debug_node.py`

**Files to modify:**
- `src/core/orchestration/graph/builder.py` - add conditional edges from verification

**State fields to add:**
- `debug_attempts: int` (default 0)
- `max_debug_loops: int = 3`

**Role to create:** `agent-brain/roles/debugger.md`

**Logic:**
```
if verification.failure and debug_attempts < 3:
    goto DebugNode, increment debug_attempts
elif verification.failure and debug_attempts >= 3:
    goto memory_sync, report failure
```

**Acceptance criteria:**
- [ ] DebugNode handles verification failures
- [ ] Max 3 retries enforced
- [ ] Test: verify retry logic works

---

## T3: Step Controller (Priority P0)

**Problem:** Agents ignore their own plans, call tools randomly.

**Solution:** Add StepControllerNode to enforce single-step execution.

**Files to create:**
- `src/core/orchestration/graph/nodes/step_controller_node.py`

**Files to modify:**
- `src/core/orchestration/graph/builder.py` - add node after execution

**State fields to add:**
```python
plan: List[Dict[str, Any]] = [
    {"id": 1, "description": "...", "tool_hint": "...", "status": "pending"}
]
current_step: int = 0
```

**Logic:**
```
1. Get current_step from state
2. Read plan[current_step]
3. Update step status (pending → completed/failed)
4. Return next_action: "execution" or "verification"
```

**Acceptance criteria:**
- [ ] Only one step executed per node call
- [ ] Plan progress tracked in state
- [ ] Test: 3-step plan executes sequentially

---

## T4: Wire Roles Per-Node (Priority P1)

**Problem:** Roles are global, not node-specific.

**Solution:** Update each node to load and inject its role prompt.

**Files to modify:**
- `src/core/orchestration/graph/nodes/workflow_nodes.py`

**Node-Role mapping:**
| Node | Role File |
|------|------------|
| perception | N/A |
| analysis | analyst.md (new) |
| planning | strategic.md |
| execution | operational.md |
| verification | reviewer.md (exists) |
| debug | debugger.md (new) |

**Acceptance criteria:**
- [ ] Each node loads role from file
- [ ] Role prompt included in node's LLM call

---

## T5: Dynamic Skills Injection (Priority P1)

**Problem:** Skills not conditionally activated.

**Solution:** Add conditional skill activation based on context.

**Files to modify:**
- `src/core/orchestration/graph/nodes/workflow_nodes.py`

**Logic:**
```python
# In execution_node
if len(state.get("relevant_files", [])) > 3:
    active_skills.append("dry")

# In analysis_node
if repo_size > large_threshold:
    active_skills.append("context_hygiene")
```

**State field to add:**
- `active_skills: List[str]`

**Acceptance criteria:**
- [ ] Skills injected based on context
- [ ] Test: verify skill activation

---

## T6: Plan Validator Node (Priority P2)

**Problem:** Weak plans can execute.

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

## T7: Wire Advanced Memory Features (Priority P2)

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

## T8: Incremental AST Indexing (Priority P2)

**Problem:** Full rescan on every run.

**Solution:** Add file watcher + hash comparison.

**Files to create:**
- `src/core/indexing/file_watcher.py` (new)

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

## T9: Tool Timeout Protection (Priority P1)

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

## T10: Structured Verification Diagnostics (Priority P1)

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

## Current Architecture (Reference)

```
perception → planning → execution → verification → memory_sync → end
```

## Target Architecture

```
perception → analysis → planning → (optional: plan_validator) → 
execution → step_controller → verification → 
(success→memory | failure<3→debug→execution | failure≥3→memory) → end
```

---

# End of Document