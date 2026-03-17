# Comprehensive System Audit Report

**Audit Date:** March 17, 2026  
**Auditor:** Automated System Audit  
**System:** CodingAgent - Local LLM Coding Assistant

---

## 1. Executive Summary

The CodingAgent is a **locally-hosted LLM coding assistant** with a LangGraph-based cognitive pipeline, tool execution sandbox, and repository intelligence capabilities. The system demonstrates a solid foundation with modular architecture, safety mechanisms (read-before-edit, sandbox path validation), and memory management.

**Overall Assessment: PRODUCTION-READY WITH SIGNIFICANT GAPS**

The system is functional for basic tasks but has **critical workflow gaps** that prevent reliable autonomous operation. The most critical issues are:
- **Missing AnalysisNode** - No dedicated repo exploration before planning
- **Missing DebugNode** - No automatic retry on verification failure
- **Missing Step Controller** - Agents can ignore their own plans
- **Unrestricted shell execution** - Security vulnerability in bash tool

**Readiness Score:** 5.5/10

---

## 2. Architecture Strengths

### 2.1 Solid Foundation Components
- **LangGraph Pipeline:** Well-structured 5-node graph (perception → planning → execution → verification → memory_sync)
- **Modular Tool Registry:** Clean separation of tools via registry pattern
- **Event Bus:** Proper pub/sub for component communication
- **Memory Tiering:** 3-tier memory (in-memory, working memory, execution trace)

### 2.2 Safety Mechanisms (Implemented)
- **Read-Before-Edit Enforcement:** Blocks edit_file without prior read_file (orchestrator.py:743-754)
- **Sandbox Path Validation:** Preflight check prevents path traversal (orchestrator.py:699-724)
- **Loop Prevention:** Duplicate action detection after 3 attempts (orchestrator.py:936-965)
- **Task Isolation:** Unique task IDs prevent context bleed (orchestrator.py:1024-1035)

### 2.3 Memory System
- **MessageManager:** Token-aware conversation management with sliding window
- **Context Distiller:** LLM-based summarization to TASK_STATE.md every 5 steps
- **Execution Trace:** JSON logging of all tool calls for debugging
- **Symbol Graph:** AST-based code indexing with incremental updates

### 2.4 Tool System
- **Tool Contracts:** Pydantic validation for tool results
- **Tool Timeout:** 30-second default for bash commands
- **Verification Tools:** run_tests, run_linter, syntax_check integrated
- **Role-based Access Control:** Basic reviewer role restrictions

### 2.5 Observability
- **Structured Logging:** Comprehensive logging in all nodes
- **Telemetry:** Tool call tracking in usage.json
- **Event Publishing:** UI notifications for provider status

---

## 3. Critical Architectural Flaws

### Issue 1: Missing AnalysisNode (CRITICAL)
**Location:** `src/core/orchestration/graph/`  
**Impact:** Agents skip repository exploration entirely before planning

**Details:** 
- The gap-analysis.md (T1) explicitly identifies this as P0 priority
- Current pipeline: `perception → planning` - no analysis phase
- Agent cannot discover relevant files, symbols, or repository structure before planning
- Retrieval happens only AFTER perception in `perception_node` (workflow_nodes.py:215-299), not before

**Evidence:**
```
# Current flow (builder.py:109-132)
perception → planning → execution → verification → memory_sync → end
```

**Recommendation:** Add AnalysisNode between perception and planning with:
- Tools: list_files, grep, search_code, find_symbol
- Output: analysis_summary, relevant_files, key_symbols

---

### Issue 2: Missing DebugNode with Retry Logic (CRITICAL)
**Location:** `src/core/orchestration/graph/`  
**Impact:** Verification failures cause immediate termination without retry

**Details:**
- gap-analysis.md (T2) specifies max 3 retry attempts
- Current verification_node always goes to memory_sync (builder.py:129)
- No conditional routing on verification failure
- SelfDebugLoop exists in sandbox.py but is never invoked

**Evidence:**
```python
# builder.py:129 - Always flows to memory
workflow.add_edge("verification", "memory_sync")
```

**Recommendation:** Implement conditional routing:
- verification.failure + attempts < 3 → DebugNode
- verification.failure + attempts >= 3 → memory_sync (report failure)

---

### Issue 3: Missing Step Controller (CRITICAL)
**Location:** `src/core/orchestration/graph/nodes/workflow_nodes.py`  
**Impact:** Agents can ignore their own plans and call tools randomly

**Details:**
- gap-analysis.md (T3) identifies this as P0
- current_plan exists but enforcement is weak
- execution_node generates tools even without plan adherence
- No single-step execution enforcement

**Evidence:**
```python
# workflow_nodes.py:584-635 - Generates tool even when no action provided
if not action and current_plan and current_step < len(current_plan):
    # LLM generates arbitrary tool for step
```

**Recommendation:** Add StepControllerNode that:
- Tracks plan progress in state
- Enforces one-step-per-execution
- Validates tool matches step intent

---

### Issue 4: No Verification Branching (CRITICAL)
**Location:** `src/core/orchestration/graph/builder.py:129`  
**Impact:** Failed verification immediately ends workflow

**Details:**
- Target architecture: `verification → (success→memory | failure<3→debug | failure≥3→memory)`
- Current: verification always flows to memory_sync
- No retry mechanism on test/lint failure

**Recommendation:** Add conditional edges from verification node

---

## 4. High-Risk Safety Issues

### Issue 5: Unrestricted Shell Execution (HIGH)
**Location:** `src/tools/file_tools.py:146-193`  
**Risk:** Shell injection, arbitrary command execution

**Details:**
- Line 167-172: White-list check exists BUT...
- Line 177: Uses `shell=True` which allows command chaining
- Attacker can bypass whitelist with: `ls; rm -rf /`
- Timeout is 30 seconds - long enough for damage

**Evidence:**
```python
# file_tools.py:177
result = subprocess.run(
    command,
    shell=True,  # DANGEROUS: defeats whitelist
    ...
)
```

**Recommendation:** 
1. Use `shell=False` with list arguments
2. Implement command allow-listing at execution time
3. Add audit logging for all bash calls

---

### Issue 6: Sandbox Bypass via Path Traversal (HIGH)
**Location:** `src/core/orchestration/sandbox.py`  
**Risk:** Agent can escape sandbox

**Details:**
- Sandbox exists but NOT integrated with tool execution
- ExecutionSandbox._create_temp_workspace copies files but...
- ...orchestrator.execute_tool calls file_tools directly, NOT through sandbox
- No enforcement that edits go through sandbox validation first

**Evidence:**
```python
# orchestrator.py:743 - Direct tool call, no sandbox
res = tool["fn"](**args)  # Bypasses sandbox entirely
```

**Recommendation:** Route all file modifications through ExecutionSandbox

---

### Issue 7: No Tool Timeout Protection (HIGH)
**Location:** `src/core/orchestration/orchestrator.py`  
**Risk:** Tools can hang indefinitely

**Details:**
- gap-analysis.md (T9) specifies timeouts per tool type
- bash tool has 30s timeout but other tools don't
- run_tests, run_linter have no timeout in orchestrator
- No per-tool timeout configuration

**Recommendation:** Implement per-tool timeout configuration:
- Default tools: 30s
- bash: 60s  
- run_tests: 120s

---

### Issue 8: No Role Per-Node Wiring (HIGH)
**Location:** `src/core/orchestration/graph/nodes/workflow_nodes.py`  
**Impact:** Roles are global, not node-specific

**Details:**
- gap-analysis.md (T4) requires role-specific prompts per node
- Current: same system prompt used for all nodes
- No role injection between perception → planning → execution
- Role restrictions only at tool execution (line 768-777)

**Evidence:**
```python
# workflow_nodes.py:309-318 - Same context for all nodes
messages = builder.build_prompt(
    identity=state["system_prompt"],  # No node-specific role
    ...
)
```

**Recommendation:** Wire roles per-node:
- perception: N/A
- analysis: analyst.md (new)
- planning: strategic.md
- execution: operational.md
- verification: reviewer.md

---

## 5. Major Missing Capabilities

### Issue 9: No Dynamic Skills Injection (HIGH)
**Location:** `src/core/orchestration/graph/nodes/workflow_nodes.py`  
**Impact:** Skills not conditionally activated

**Details:**
- Skills exist: dry.md, context_hygiene.md
- gap-analysis.md (T5) requires conditional activation
- Currently active_skills is always empty list (line 312)

**Evidence:**
```python
# workflow_nodes.py:312
active_skills=[],  # Always empty
```

**Recommendation:** Implement conditional activation:
```python
if len(state.get("relevant_files", [])) > 3:
    active_skills.append("dry")
```

---

### Issue 10: No Plan Validator Node (MEDIUM)
**Location:** `src/core/orchestration/graph/`  
**Impact:** Weak plans execute without validation

**Details:**
- gap-analysis.md (T6) specifies plan validation
- No validation between planning and execution
- Plans can: miss verification steps, not reference files, be unordered

**Recommendation:** Add PlanValidatorNode that checks:
- Plan has verification step
- Plan references files
- Plan steps are ordered

---

### Issue 11: No Incremental AST Indexing (MEDIUM)
**Location:** `src/core/indexing/symbol_graph.py`  
**Impact:** Full rescan on every run

**Details:**
- gap-analysis.md (T8) specifies incremental updates
- Hash comparison exists but not utilized for updates
- File watcher not implemented

**Recommendation:** Add file watcher + hash comparison

---

### Issue 12: No Structured Verification Diagnostics (MEDIUM)
**Location:** `src/tools/verification_tools.py`  
**Impact:** Verification failures not parseable by DebugNode

**Details:**
- gap-analysis.md (T10) specifies structured output
- Current output is raw pytest/ruff output
- DebugNode cannot consume for automatic fixing

**Recommendation:** Parse into structured format:
```python
{
    "error_type": "test_failure",
    "file": "tests/test_x.py",
    "line": 42,
    "message": "AssertionError: ..."
}
```

---

## 6. Workflow Reliability Issues

### Issue 13: Task Decomposition is Fragile (MEDIUM)
**Location:** `src/core/orchestration/graph/nodes/workflow_nodes.py:118-213`  
**Impact:** Heuristic-based decomposition can fail

**Details:**
- Regex-based detection for multi-step tasks
- LLM decomposition can produce invalid JSON
- No fallback plan structure validation

**Evidence:**
```python
# workflow_nodes.py:126-143
multi_step_indicators = [
    re.search(r"\band\b", task, re.IGNORECASE),
    # ... weak heuristics
]
```

---

### Issue 14: No Dead-End Detection (MEDIUM)
**Location:** `src/core/orchestration/graph/`  
**Impact:** Agent can get stuck in loops

**Details:**
- Loop prevention exists but only checks repeated tool calls
- No detection for: infinite perception loops, verification loops
- Max rounds (15) is the only safeguard

**Recommendation:** Add explicit dead-end states

---

### Issue 15: Planning Not Deterministic (MEDIUM)
**Location:** `src/core/orchestration/graph/nodes/workflow_nodes.py:437-536`  
**Impact:** Same task can produce different plans

**Details:**
- Fallback planner uses LLM without deterministic settings
- Plan output is unstructured text parsing
- No plan consistency guarantees

---

## 7. Tool System Weaknesses

### Issue 16: Tool Discoverability (LOW)
**Location:** `src/tools/registry.py`  
**Impact:** Tools listed but not categorized

**Details:**
- 35+ tools but no grouping/filtering
- Tools should be grouped: file_ops, search, verification, state
- No tool documentation auto-generation

---

### Issue 17: find_references is Naive (MEDIUM)
**Location:** `src/tools/repo_tools.py:50-76`  
**Impact:** Simple substring match, not true reference analysis

**Evidence:**
```python
# repo_tools.py:70-71
if name in text:  # Substring match, not AST analysis
    refs.append(...)
```

---

## 8. Repository Awareness Gaps

### Issue 18: Retrieval Not Integrated with Planning (HIGH)
**Location:** `src/core/orchestration/graph/nodes/workflow_nodes.py:215-299`  
**Impact:** Retrieval happens in perception, not used for planning

**Details:**
- search_code, find_symbol called in perception
- Results added to context but planning doesn't explicitly use
- No structured "analysis" phase to compile findings

---

### Issue 19: Symbol Graph Not Fully Utilized (MEDIUM)
**Location:** `src/core/indexing/symbol_graph.py`  
**Impact:** Advanced features unused

**Details:**
- Graph exists with nodes/edges
- But find_tests_for_module() likely unused
- No call graph analysis

---

## 9. Memory System Evaluation

### Issue 20: Advanced Memory Features Not Wired (MEDIUM)
**Location:** `src/core/memory/advanced_features.py`  
**Impact:** Implemented but never called

**Details:**
- TrajectoryLogger: Exists, not logged to
- DreamConsolidator: Exists, never runs
- RefactoringAgent: Exists, never invoked
- ReviewAgent: Exists, never runs after edits
- SkillLearner: Exists, never learns

**Evidence:**
```python
# gap-analysis.md T7 specifies wiring
| Feature      | Where to wire                  |
|--------------|--------------------------------|
| TrajectoryLogger | orchestrator.py after run_agent_once |
| DreamConsolidator | orchestrator.py on session end |
```

---

### Issue 21: Context Window Protection (LOW)
**Location:** `src/core/orchestration/message_manager.py`  
**Impact:** Token budget could be better managed

**Details:**
- 4000 token default may be too small for large repos
- No prioritization of which messages to keep
- System prompt always preserved (correct)

---

## 10. Evaluation and Testing Gaps

### Issue 22: No Scenario Evaluation Framework (MEDIUM)
**Location:** `tests/`  
**Impact:** Cannot measure agent reliability

**Details:**
- Unit tests exist (70+ files)
- No SWE-bench style evaluation
- No benchmark for: success rate, edit accuracy
- Integration tests require external LLM

---

### Issue 23: No Regression Tests for Edits (MEDIUM)
**Location:** `tests/unit/test_edit_file_unified_diff.py`  
**Impact:** Cannot verify edit correctness

**Details:**
- Tests exist for patch application
- No golden dataset of correct edits
- No automated comparison

---

## 11. Usability Problems

### Issue 24: Configuration Complexity (MEDIUM)
**Location:** `src/core/llm_manager.py`, `src/core/user_prefs.py`  
**Impact:** High onboarding friction

**Details:**
- Requires manual LM Studio/Ollama setup
- No auto-detection of available providers
- Model configuration is manual

---

### Issue 25: Debugging Workflow (LOW)
**Location:** `src/core/orchestration/`  
**Impact:** Hard to diagnose failures

**Details:**
- Logs are verbose but scattered
- No structured failure classification
- No visual representation of graph state

---

## 12. Performance Bottlenecks

### Issue 26: Context Rebuilt Every Perception (MEDIUM)
**Location:** `src/core/orchestration/graph/nodes/workflow_nodes.py:302-318`  
**Impact:** Redundant prompt assembly

**Details:**
- ContextBuilder called every perception_node
- No caching of system prompt
- Token estimation on every call

---

### Issue 27: Vector Store Not Optimized (LOW)
**Location:** `src/core/indexing/vector_store.py`  
**Impact:** Semantic search may be slow

**Details:**
- LanceDB used but no batch indexing
- No caching of search results
- Index rebuilt each run

---

## 13. Over-Engineered Components

### Issue 28: Hub-and-Spoke Architecture Unused (LOW)
**Location:** `src/core/orchestration/graph_factory.py`  
**Impact:** Complex multi-agent system not utilized

**Details:**
- GraphFactory creates planner/coder/reviewer/researcher graphs
- HubAndSpokeCoordinator defined
- No actual multi-agent workflows

---

### Issue 29: Advanced Features Dormant (LOW)
**Location:** `src/core/memory/advanced_features.py`  
**Impact:** Code written but never called

**Details:**
- RefactoringAgent: 178 lines of code
- ReviewAgent: Not instantiated
- SkillLearner: Not wired

---

### Issue 30: Session Store Unused (LOW)
**Location:** `src/core/memory/session_store.py`  
**Impact:** SQLite persistence not integrated

**Details:**
- Tables defined: messages, tool_calls, errors, plans, decisions
- No writes to session store
- Only in-memory MessageManager used

---

## 14. Prioritized Fix List

### Phase 1 — Critical Stability Fixes

| Priority | Issue | Location | Effort | Impact |
|----------|-------|----------|--------|--------|
| P0 | Fix bash shell=True | file_tools.py:177 | Low | HIGH |
| P0 | Add AnalysisNode | builder.py + new file | Medium | HIGH |
| P0 | Add DebugNode + retry | builder.py + new file | Medium | HIGH |
| P0 | Add Step Controller | workflow_nodes.py | Medium | HIGH |
| P1 | Wire verification branching | builder.py:129 | Low | HIGH |
| P1 | Integrate sandbox for edits | orchestrator.py:743 | Medium | HIGH |

### Phase 2 — Robustness Improvements

| Priority | Issue | Location | Effort | Impact |
|----------|-------|----------|--------|--------|
| P1 | Add tool timeout protection | orchestrator.py | Low | Medium |
| P1 | Wire roles per-node | workflow_nodes.py | Medium | Medium |
| P1 | Dynamic skills injection | workflow_nodes.py | Medium | Medium |
| P2 | Add Plan Validator | builder.py + new file | Medium | Medium |
| P2 | Structured verification | verification_tools.py | Medium | Medium |
| P2 | Incremental AST indexing | symbol_graph.py | Medium | Low |

### Phase 3 — Capability Improvements

| Priority | Issue | Location | Effort | Impact |
|----------|-------|----------|--------|--------|
| P2 | Wire advanced memory | orchestrator.py | Medium | Medium |
| P2 | Improve find_references | repo_tools.py | Medium | Medium |
| P3 | Hub-and-spoke integration | graph_factory.py | High | Low |
| P3 | Scenario evaluation | tests/ | High | Medium |

### Phase 4 — Advanced Features

| Priority | Issue | Location | Effort | Impact |
|----------|-------|----------|--------|--------|
| P3 | Multi-agent workflows | graph_factory.py | High | Low |
| P3 | Auto-skill learning | advanced_features.py | High | Low |
| P3 | Session store integration | session_store.py | Medium | Low |

---

## 15. Recommendations Summary

### Immediate Actions (Before Production Use)
1. **FIX: Change bash to shell=False** - Critical security fix
2. **ADD: AnalysisNode** - Required for repo-aware planning
3. **ADD: DebugNode with retry** - Required for reliability
4. **ADD: Step Controller** - Required for deterministic execution
5. **INTEGRATE: Sandbox for file edits** - Enforce validation

### Short-term (1-2 Sprints)
1. Wire roles per-node
2. Add tool timeout protection
3. Implement dynamic skills injection
4. Add verification branching
5. Wire advanced memory features

### Medium-term (1-2 Quarters)
1. Build scenario evaluation framework
2. Implement multi-agent workflows
3. Add comprehensive benchmarks
4. Optimize performance

---

## Appendix: Key File References

| Component | File Path | Lines |
|-----------|-----------|-------|
| Orchestrator | src/core/orchestration/orchestrator.py | 1313 |
| Graph Builder | src/core/orchestration/graph/builder.py | 134 |
| Workflow Nodes | src/core/orchestration/graph/nodes/workflow_nodes.py | 929 |
| File Tools | src/tools/file_tools.py | 208 |
| Sandbox | src/core/orchestration/sandbox.py | 284 |
| Memory Distiller | src/core/memory/distiller.py | 174 |
| Advanced Features | src/core/memory/advanced_features.py | 328 |
| Symbol Graph | src/core/indexing/symbol_graph.py | 270 |
| Gap Analysis | docs/gap-analysis.md | 299 |
| Architecture | docs/ARCHITECTURE.md | 305 |

---

**End of Audit Report**
