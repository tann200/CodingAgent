# Test Quality Analysis Report

**Date**: 2026-06-08  
**Total Tests Analyzed**: 4,399 tests across 372 test files  
**Analysis Scope**: Complete test suite (unit, integration, e2e, acceptance, benchmarks)

---

## Executive Summary

**Overall Test Suite Health**: ⚠️ **NEEDS IMPROVEMENT**

The test suite contains **4,393 passing tests** but suffers from significant quality issues:

- **~65+ low-value unit tests** identified (trivial imports, property setters, constant verification)
- **~80% of "integration" tests** are actually mock-heavy unit tests
- **Only ~10% true integration tests** that verify real component interaction
- **Critical workflow gaps** in e2e coverage (multi-turn conversations, delegation, error recovery)
- **251 deprecated tests** (5.7%) need cleanup

**Key Metrics:**
- Unit tests: 89.6% (3,953 tests) - **many are trivial**
- Integration tests: 3.8% (168 tests) - **most use excessive mocks**
- E2E tests: 0.6% (26 tests) - **limited scope, often skipped**
- Deprecated tests: 5.7% (251 tests) - **dead weight**

---

## Critical Findings

### 1. TRIVIAL TESTS - No Real Behavior Verification

**Impact**: ~40+ tests provide zero value, waste maintenance effort

#### Import-Only Tests
**Location**: `tests/unit/test_memory_system.py:9-123`

```python
def test_distiller_import(self):
    from src.core.memory import distiller
    assert distiller is not None

def test_session_store_import(self):
    from src.core.memory import session_store
    assert session_store is not None
```

**Problem**: 
- Tests only verify imports don't raise exceptions
- `assert X is not None` is meaningless - imports never return None
- Import failures would break ALL tests anyway

**Recommendation**: **DELETE** - 8 tests in this file alone

---

#### Property/Dataclass Tests
**Location**: `tests/unit/test_event_bus.py:84-95`, `tests/unit/test_session_registry.py:24-52`

```python
def test_agent_message_properties():
    msg = AgentMessage(
        agent_id="test",
        payload={"key": "value"},
        priority=MessagePriority.HIGH,
    )
    assert msg.agent_id == "test"
    assert msg.payload["key"] == "value"
```

**Problem**: Tests that constructor assigns parameters to properties - trivial dataclass behavior

**Recommendation**: **DELETE** - 4+ tests across multiple files

---

#### Constant Verification Tests
**Location**: `tests/unit/test_tool_constants.py:18-63`, `tests/unit/test_graph_factory.py:6-11`

```python
def test_contains_core_write_tools(self):
    for tool in ("edit_file", "write_file", "apply_patch"):
        assert tool in WRITE_TOOLS_REQUIRING_READ

def test_graph_factory_graph_types():
    assert GraphFactory.GRAPH_TYPES["planner"] == "planning"
```

**Problem**: Tests hardcoded dictionary/set values with no logic

**Recommendation**: **REDUCE** - Keep 1 structure verification test, delete membership checks (~15 tests)

---

### 2. MOCK-HEAVY "INTEGRATION" TESTS

**Impact**: False confidence from passing tests that don't verify real integration

#### Integration Tests That Are Actually Unit Tests

**Location**: `tests/integration/test_pipeline_mock.py:329-428` (PM-6)

```python
def test_pm6_fix_syntax_pipeline(tmp_path, monkeypatch):
    """Three-step fix_syntax scenario: read_file → edit_file → run_tests."""
    
    # Mock ALL the tools
    def run_tests_mock(**kwargs):
        return {"status": "ok", "output": "Tests passed"}
    
    def edit_file_mock(**kwargs):
        return {"status": "ok", "edited": True}
    
    orch.tool_registry.register("run_tests", run_tests_mock, [], "Run test suite")
    orch.tool_registry.register("edit_file", edit_file_mock, ["write"], "Edit file")
```

**Problem**:
- Claims to test "three-step pipeline"
- `edit_file` doesn't actually edit files
- `run_tests` doesn't run tests
- Only verifies graph routes through nodes in correct order
- **This is orchestration unit testing, not integration**

**Recommendation**: Move to `tests/unit/orchestration/`, create real integration test

---

**Location**: `tests/integration/test_delegation_mock.py:69-138` (DM-1, DM-2)

```python
mock_delegate = AsyncMock(return_value="Found 2 potential issues.")
with patch(f"{_MODULE}.delegate_task_async", mock_delegate):
    result = _run(delegation_node(state, config))

assert mock_delegate.call_count == 1
```

**Problem**:
- No actual delegation happens
- Tests function signature, not delegation behavior
- Missing: verify subagent spawns, executes, returns results

**Recommendation**: Keep as unit test, add true integration test in `tests/integration_real/`

---

**Location**: `tests/integration/test_ollama_adapter_integration.py:1-77`

```python
@patch("requests.get")
def test_get_models_from_api(self, mock_get):
    mock_response.json.return_value = {"models": [{"name": "qwen3.5:9b"}]}
```

**Problem**: HTTP requests are mocked - no actual Ollama integration

**Recommendation**: Move to `tests/unit/adapters/` - this is a unit test

---

#### Statistics on Mock Usage

Analyzed 15 integration test files:
- **~12 files** (80%) mock the LLM extensively (MockAdapter or patched `call_model`)
- **~10 files** (67%) mock tool execution
- **~2 files** (13%) test real component integration

**True integration tests** (good examples):
- `tests/integration/test_e2e_pipeline_smoke.py` - MEM-1 (lines 150-188): Real file tools + real cache
- `tests/integration/test_agent_loop_plaintext_tools.py`: Real orchestrator + real graph

---

### 3. TESTS WITHOUT MEANINGFUL ASSERTIONS

**Location**: `tests/unit/test_provider_panel.py:51-61`

```python
def test_provider_models_list_event_posts_message():
    bridge, bus, mock_app = _make_bridge()
    bus.publish("provider.models.list", {"provider": "lm_studio", "models": ["m1"]})
    # Ensure bridge did not error out
    assert bridge is not None
```

**Problem**: `assert bridge is not None` is always true - you can't call methods on None

**Recommendation**: **DELETE** or verify log output

---

**Location**: `tests/unit/test_dashboard.py:162-174`

```python
def test_log_new_publish_does_not_recurse(self):
    bus = EventBus()
    call_count = [0]
    
    def handler(payload):
        call_count[0] += 1
    
    bus.subscribe("log.new", handler)
    bus.publish("log.new", {"message": "hello"})
    assert call_count[0] == 1
```

**Problem**: Claims to prevent recursion but only checks handler ran once. Doesn't test recursive scenario.

**Recommendation**: **IMPROVE** or **DELETE**

---

### 4. TEST FRAMEWORK TESTS (Not Production Code Tests)

**Location**: `tests/unit/test_otel_wiring.py:13-51`

```python
def test_otel_extra_exists(self):
    data = self._load_pyproject()
    extras = data.get("project", {}).get("optional-dependencies", {})
    assert "otel" in extras

def test_otel_extra_includes_api(self):
    otel_deps = data["project"]["optional-dependencies"]["otel"]
    assert any("opentelemetry-api" in d for d in otel_deps)
```

**Problem**: Tests pyproject.toml structure, not application code

**Recommendation**: **MOVE** to CI lint checks or packaging validation

---

**Location**: `tests/unit/test_benchmark_baseline.py:32-87`

```python
def test_et3_1_baseline_json_valid():
    baseline = _load_baseline()
    for key in ("version", "thresholds", "regression_multiplier"):
        assert key in baseline

def test_et3_2_all_scenarios_covered():
    scenario_names = {s["name"] for s in module.SCENARIOS}
    missing = scenario_names - thresholds
    assert not missing
```

**Problem**: Tests benchmark data files, not production code

**Recommendation**: **MOVE** to benchmark infrastructure validation

---

**Location**: `tests/unit/test_builder_constants_usage.py:10-32`

```python
def test_check_no_plan_fast_path_uses_canonical_tool_sets():
    src = inspect.getsource(builder._check_no_plan_fast_path)
    code = "\n".join([l for l in src.splitlines() if not l.lstrip().startswith("#")])
    assert "read_only_tools = READ_ONLY_TOOLS" in code
```

**Problem**: String matching against source code - tests implementation, not behavior

**Recommendation**: **REPLACE** with behavioral test that verifies function uses correct tool sets

---

### 5. REDUNDANT TESTS

**Location**: `tests/unit/test_toolsets.py:22-35`

```python
def test_list_available_toolsets():
    toolsets = list_available_toolsets()
    assert "coding" in toolsets
    assert "debug" in toolsets

def test_get_tools_for_toolset():
    coding_tools = get_tools_for_toolset("coding")
    assert "read_file" in coding_tools
```

**Problem**: Second test implicitly verifies the first - redundant coverage

**Recommendation**: **MERGE** into single test

---

**Location**: `tests/unit/test_event_bus.py:4-35` (3 publish/subscribe tests)

**Problem**: All three tests verify same publish mechanism with trivial variations

**Recommendation**: **CONSOLIDATE** to 2 tests

---

### 6. EMPTY TEST FILES

**Files with 0 tests** (need implementation or deletion):
- `tests/unit/test_cross_session_bus.py`
- `tests/unit/test_delegate_task_recursion_limit.py`
- `tests/unit/test_instruction_files.py`
- `tests/unit/test_mcp_stdio_server.py`
- `tests/unit/test_model_tiers.py`
- `tests/unit/test_system_prompt_loading.py`

**Recommendation**: Either implement or delete these files

---

## E2E Test Coverage Analysis

### Current State: Limited Real E2E Tests

**E2E tests that verify actual workflows** (✅ Good):

1. **`test_real_llm_smoke.py`** (lines 1-104)
   - ✅ Real LLM (requires API key)
   - ✅ Real orchestrator
   - ✅ Real file creation
   - ⚠️ Skipped by default

2. **`test_acceptance/test_system_validation.py`** (lines 127-156)
   - ✅ Real guardrail enforcement
   - ✅ Multiple security checks
   - ✅ True acceptance test

3. **`test_basic_workflows.py:108-141`**
   - ✅ Real read-before-write enforcement
   - ✅ Real orchestrator

---

### Critical Missing Workflows

#### ❌ Multi-turn conversation with context
```python
# Should exist: tests/e2e/test_multi_turn_conversation.py
def test_e2e_multi_turn_with_clarification(tmp_path):
    """
    Turn 1: User asks ambiguous question
    Turn 2: User clarifies
    Verify: Context maintained across turns
    """
```

**Current gap**: No test verifies multi-turn conversation behavior

---

#### ❌ Real delegation workflow
```python
# Should exist: tests/integration_real/test_delegation_integration.py
def test_subagent_spawns_and_executes(tmp_path):
    """
    Main agent → spawns subagent → subagent executes → results merge
    """
```

**Current gap**: All delegation tests mock `delegate_task_async`

---

#### ❌ Complete bug-fix workflow
```python
# Should exist: tests/e2e/test_complete_bug_fix.py
def test_complete_bug_fix_workflow(tmp_path):
    """
    1. User reports bug
    2. Agent searches code (real search)
    3. Agent reads file (real read)
    4. Agent creates plan
    5. Agent edits file (real edit)
    6. Agent runs tests (real subprocess)
    7. Tests pass → completion
    """
```

**Current gap**: No end-to-end bug-fix workflow test

---

#### ❌ Error recovery scenario
```python
# Should exist: tests/e2e/test_error_recovery.py
def test_e2e_tool_failure_recovery(tmp_path):
    """
    Tool fails → Agent detects → Replans → Succeeds
    """
```

**Current gap**: `test_loop_prevention.py` only tests repetition blocking, not recovery

---

#### ❌ Planning → Execution → Verification cycle
```python
# Should exist: tests/e2e/test_plan_execute_verify.py
def test_plan_execution_verification_cycle(tmp_path):
    """
    Verify plan is created, steps executed in order, results verified
    """
```

**Current gap**: Tests mock either planning OR execution, not full cycle

---

## Quantified Impact

### Low-Value Tests Breakdown

| Category | Files Affected | Est. Tests | Recommendation |
|----------|---------------|-----------|----------------|
| Import-only tests | test_memory_system.py | 8 | DELETE |
| Mock-heavy "integration" | test_pipeline_mock.py, test_delegation_mock.py, test_ollama_adapter_integration.py, test_circuit_breaker.py, test_otel_wiring.py | 15+ | MOVE to unit tests |
| No assertions | test_provider_panel.py, test_dashboard.py | 2 | DELETE |
| Redundant tests | test_toolsets.py, test_event_bus.py | 5 | CONSOLIDATE |
| Test framework tests | test_otel_wiring.py, test_benchmark_baseline.py, test_builder_constants_usage.py | 12 | MOVE to CI/infra |
| Trivial property tests | test_event_bus.py, test_session_registry.py | 4 | DELETE |
| Constant verification | test_graph_factory.py, test_tool_constants.py, test_github_copilot_reexports.py | 15 | REDUCE |
| Minimal behavior | test_vector_store_stub.py, test_planning_result.py | 3 | IMPROVE or DELETE |
| Over-specified helpers | test_provider_probe_helpers.py | 10 | CONSOLIDATE |
| Empty test files | 6 files | 0 | DELETE or implement |

**Total: ~74+ low-value tests** across 30+ files

---

### Integration Test Reclassification

**Should be moved to `tests/unit/`**:
- `test_mock_adapter_integration.py` (all 17 tests)
- `test_delegation_mock.py` (all 6 tests)
- `test_ollama_adapter_integration.py` (all 3 tests)
- `test_pipeline_mock.py` (6 tests - orchestration logic)
- `test_langgraph_orchestrator.py` (3 tests - graph compilation)

**Total: ~35 tests misclassified as integration**

---

## Recommendations

### Priority 1: HIGH - Clean Up Low-Value Tests

**Immediate Actions:**

1. **Delete trivial tests** (~30 tests):
   - Import-only tests in `test_memory_system.py`
   - Property/dataclass tests
   - Tests with no meaningful assertions

2. **Reclassify mock-heavy integration tests** (~35 tests):
   - Move to `tests/unit/orchestration/`
   - Update test names to reflect they test orchestration logic, not integration

3. **Delete or fix empty test files** (6 files):
   - Either implement missing tests or remove placeholder files

**Estimated cleanup**: Remove/relocate ~70 tests, improve test suite signal-to-noise ratio

---

### Priority 2: MEDIUM - Add True Integration Tests

**Create `tests/integration_real/` directory**:

```python
# tests/integration_real/test_tool_chain_integration.py
def test_search_read_edit_chain(tmp_path):
    """Real search → read → edit without mocks."""
    orch = Orchestrator(working_dir=str(tmp_path))
    # Use real search_code, real read_file, real edit_file
    # MockAdapter for LLM only (deterministic responses)
```

**Create real delegation integration test**:

```python
# tests/integration_real/test_delegation_integration.py
def test_delegation_spawns_real_subagent(tmp_path):
    """Verify subagent actually spawns and executes."""
    # Don't mock delegate_task_async
    # Use scripted responses but real delegation infrastructure
```

**Expected impact**: Add 5-10 true integration tests covering critical workflows

---

### Priority 3: MEDIUM - Add Missing E2E Workflows

**Critical missing scenarios to add**:

1. **Multi-turn conversation** (`tests/e2e/test_multi_turn_conversation.py`)
2. **Error recovery** (`tests/e2e/test_error_recovery.py`)
3. **Complete bug-fix workflow** (`tests/e2e/test_complete_bug_fix.py`)
4. **Planning → Execution → Verification cycle** (`tests/e2e/test_plan_execute_verify.py`)
5. **Real delegation workflow** (`tests/integration_real/test_delegation_integration.py`)

**Expected impact**: Add 5-7 e2e tests, increase e2e coverage from 0.6% to ~1.5%

---

### Priority 4: LOW - Test Infrastructure Improvements

1. **Add test classification markers**:
   ```python
   @pytest.mark.unit_with_mocks  # For current "integration" tests
   @pytest.mark.integration_real # For new true integration tests
   @pytest.mark.e2e_real_llm     # For real LLM e2e tests
   ```

2. **Create `tests/README.md`** documenting:
   - Test categories and when to use each
   - How to write effective integration tests
   - Guidelines on mock usage

3. **Add docstrings to mock-heavy tests**:
   ```python
   # NOTE: This is a unit test of orchestration logic using mocks.
   # For true integration testing, see tests/integration_real/
   ```

4. **Set up local LLM for integration tests**:
   - Use Qwen3.5-9B or Gemma-4-4B for faster, deterministic integration tests
   - Avoid skipped tests due to missing API keys

---

## High-Quality Test Examples

**For contrast, here are examples of GOOD tests** found in the suite:

### ✅ Security Test with Real Exploits
**Location**: `tests/unit/test_state_tools.py:169-223`

```python
def test_path_traversal_blocked():
    """Tests actual path traversal attack vectors."""
    result = execute_tool({"name": "read_file", "path": "../../etc/passwd"})
    assert result.get("ok") is False
```

**Why it's good**: Tests real security behavior with actual exploit attempts

---

### ✅ Concurrency Test with Real Threads
**Location**: `tests/unit/test_file_lock.py:70-105`

```python
def test_concurrent_writes_no_data_loss():
    """10 threads writing simultaneously - verify no corruption."""
    threads = [Thread(target=write_worker) for _ in range(10)]
    for t in threads:
        t.start()
    # ... verify all writes succeeded without corruption
```

**Why it's good**: Tests real concurrency behavior with multiple threads

---

### ✅ State Machine Test
**Location**: `tests/unit/test_circuit_breaker.py:54-98`

```python
def test_circuit_breaker_state_transitions():
    """Tests CLOSED → OPEN → HALF_OPEN → CLOSED cycle."""
    # Verify state transitions with real circuit breaker logic
```

**Why it's good**: Tests actual state machine behavior with clear transitions

---

### ✅ Real Calculation Test
**Location**: `tests/unit/test_inference_v2.py`

```python
def test_token_budget_calculation():
    """Verifies token budget math with real numbers."""
    budget = calculate_budget(max_tokens=1000, used=300, reserve=100)
    assert budget == 600
```

**Why it's good**: Tests real calculations with concrete values

---

## Conclusion

**Test Suite Strengths**:
- ✅ High coverage (4,393 passing tests)
- ✅ Good security test coverage (bash security, path traversal, SSRF)
- ✅ Comprehensive adapter testing
- ✅ Some excellent concurrency and state machine tests

**Test Suite Weaknesses**:
- ❌ ~74+ trivial/low-value tests consuming maintenance effort
- ❌ 80% of "integration" tests are actually mock-heavy unit tests
- ❌ Only 0.6% true e2e tests (26 tests)
- ❌ Critical workflow gaps (delegation, multi-turn, error recovery)
- ❌ 251 deprecated tests (5.7%) need cleanup

**Overall Grade**: **C+ / B-**

The test suite provides good coverage but needs quality improvements:
1. Remove trivial tests
2. Reclassify mock-heavy integration tests
3. Add true integration and e2e tests
4. Clean up deprecated tests

**Estimated Effort**:
- Cleanup: 2-3 days
- Add integration tests: 3-5 days
- Add e2e tests: 5-7 days
- **Total: 2-3 weeks** for comprehensive test quality improvement

---

## Next Steps

Would you like me to:

1. **Start cleanup immediately** - Remove/reclassify low-value tests
2. **Create integration test templates** - Set up `tests/integration_real/` with examples
3. **Add missing e2e workflows** - Implement multi-turn, delegation, bug-fix tests
4. **Generate detailed cleanup plan** - File-by-file breakdown of changes needed
5. **Something else**?
