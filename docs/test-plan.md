# CodingAgent Comprehensive Test Plan

**Version:** 1.0  
**Date:** March 18, 2026  
**Status:** Active

---

## 1. Test Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TEST PYRAMID                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│                              E2E Tests                                     │
│                           (10-15% of tests)                                 │
│                  Full workflow validation, real LLM calls                     │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│                        Integration Tests                                   │
│                         (20-25% of tests)                                  │
│              Component interaction, mocked LLM, partial flows              │
│                                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│                          Unit Tests                                        │
│                          (60-70% of tests)                                 │
│                  Individual functions, classes, edge cases                  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Test Organization

| Directory | Purpose | Run Time |
|-----------|---------|----------|
| `tests/unit/` | Fast, isolated tests | < 1 min |
| `tests/integration/` | Component interaction | 1-5 min |
| `tests/e2e/` | Full workflows | 5-30 min |

---

## 2. Unit Test Specifications

### 2.1 Core Orchestration Tests

#### Test File: `test_orchestrator.py` (Existing)
| Test | Description | Mock Level |
|------|-------------|------------|
| `test_tool_registry_and_preflight` | Verify tool registration and preflight checks | None |
| `test_execute_tool_echo` | Test tool execution | None |
| `test_generate_work_summary` | Test summary generation | None |
| `test_generate_work_summary_empty` | Edge case: empty state | None |
| `test_generate_work_summary_no_plan` | Edge case: no plan | None |

#### Test File: `test_orchestrator_rules.py` (Existing)
| Test | Description |
|------|-------------|
| `test_role_filtering` | Role-based tool filtering |
| `test_tool_contract_validation` | Tool contract enforcement |

#### NEW: `test_orchestrator_security.py`
```python
class TestOrchestratorSecurity:
    """Security-focused tests for orchestrator."""
    
    def test_sandbox_fail_closed(self):
        """Test that sandbox validation failures block writes."""
    
    def test_read_before_edit_enforcement(self):
        """Test read-before-edit for all modifying tools."""
    
    def test_loop_prevention(self):
        """Test loop detection mechanism."""
    
    def test_path_traversal_blocked(self):
        """Test path traversal protection."""
```

### 2.2 Graph Node Tests

#### Test File: `test_graph_builder_routing.py` (Existing + Enhanced)
| Test | Description |
|------|-------------|
| `test_route_after_perception_fast_path` | Fast-path routing |
| `test_route_after_perception_standard_path` | Standard routing |
| `test_should_after_planning_*` | Various routing conditions |
| `test_should_after_execution_*` | Execution routing |
| `test_should_after_evaluation_*` | Evaluation routing |

#### NEW: `test_graph_nodes.py`
```python
class TestPerceptionNode:
    """Tests for perception node."""
    
    @pytest.mark.asyncio
    async def test_perception_with_task_decomposition(self):
        """Test task decomposition for multi-step tasks."""
    
    @pytest.mark.asyncio
    async def test_perception_empty_response_handling(self):
        """Test handling of empty LLM responses."""
    
    @pytest.mark.asyncio
    async def test_perception_pre_retrieval(self):
        """Test pre-retrieval of repo context."""

class TestPlanningNode:
    """Tests for planning node."""
    
    @pytest.mark.asyncio
    async def test_plan_parsing_json(self):
        """Test JSON plan parsing."""
    
    @pytest.mark.asyncio
    async def test_plan_parsing_numbered_list(self):
        """Test numbered list parsing."""
    
    @pytest.mark.asyncio
    async def test_plan_parsing_bullet_list(self):
        """Test bullet list parsing."""
    
    @pytest.mark.asyncio
    async def test_plan_parsing_fallback(self):
        """Test fallback single-step parsing."""

class TestExecutionNode:
    """Tests for execution node."""
    
    @pytest.mark.asyncio
    async def test_execution_read_before_edit_all_tools(self):
        """Test read-before-edit for write_file, edit_file, delete_file."""
    
    @pytest.mark.asyncio
    async def test_execution_loop_detection(self):
        """Test loop detection integration."""
    
    @pytest.mark.asyncio
    async def test_execution_sandbox_preflight(self):
        """Test sandbox preflight integration."""

class TestAnalysisNode:
    """Tests for analysis node."""
    
    @pytest.mark.asyncio
    async def test_analysis_fast_path_bypass(self):
        """Test fast-path when next_action exists."""
    
    @pytest.mark.asyncio
    async def test_analysis_repo_summary(self):
        """Test repo summary generation."""
    
    @pytest.mark.asyncio
    async def test_analysis_retrieval(self):
        """Test retrieval of relevant files/symbols."""
```

### 2.3 Tool Tests

#### Test File: `test_tools_file_ops.py` (Existing)
| Test | Description |
|------|-------------|
| `test_safe_resolve_basic` | Basic path resolution |
| `test_safe_resolve_relative` | Relative path handling |
| `test_safe_resolve_outside_workdir` | Path traversal blocking |

#### NEW: `test_tools_security.py`
```python
class TestBashToolSecurity:
    """Security tests for bash tool."""
    
    def test_bash_restricted_commands_blocked(self):
        """Test that restricted commands are blocked."""
    
    def test_bash_shell_operators_blocked(self):
        """Test that shell operators are blocked."""
    
    def test_bash_safe_commands_allowed(self):
        """Test that safe commands are allowed."""
    
    def test_bash_npm_test_allowed(self):
        """Test npm test/run commands."""
    
    def test_bash_pip_install_blocked(self):
        """Test pip install is blocked."""

class TestWorkspaceGuardIntegration:
    """Tests for WorkspaceGuard integration."""
    
    def test_write_protected_file_blocked(self):
        """Test write to protected file is blocked."""
    
    def test_edit_protected_file_blocked(self):
        """Test edit to protected file is blocked."""
    
    def test_delete_protected_file_blocked(self):
        """Test delete of protected file is blocked."""
    
    def test_user_approval_overrides(self):
        """Test user_approved parameter bypasses guard."""
```

### 2.4 Context & Memory Tests

#### Test File: `test_context_builder.py` (Existing)
| Test | Description |
|------|-------------|
| `test_identity_injection` | Identity prompt injection |
| `test_role_injection` | Role prompt injection |
| `test_tools_injection` | Tools description injection |

#### NEW: `test_context_builder_robustness.py`
```python
class TestContextBuilderRobustness:
    """Robustness tests for context builder."""
    
    def test_no_duplicate_returns(self):
        """Verify no duplicate return statements."""
    
    def test_token_budget_enforcement(self):
        """Test token budget is respected."""
    
    def test_sanitization_all_inputs(self):
        """Test sanitization of all input types."""
    
    def test_retrieved_snippets_integration(self):
        """Test retrieved snippets are included."""

class TestMemorySystem:
    """Tests for memory system."""
    
    def test_message_windowing(self):
        """Test message windowing with max tokens."""
    
    def test_distiller_summary_cache(self):
        """Test distiller summary caching."""
    
    def test_session_persistence(self):
        """Test session data persistence."""
```

### 2.5 Indexing Tests

#### Test File: `test_repo_indexer.py` (Existing + Enhanced)
| Test | Description |
|------|-------------|
| `test_initial_indexing` | Initial indexing |
| `test_incremental_indexing_no_changes` | Incremental: no changes |
| `test_incremental_indexing_file_modified` | Incremental: modified file |
| `test_incremental_indexing_new_file` | Incremental: new file |
| `test_incremental_indexing_deleted_file` | Incremental: deleted file |
| `test_force_full_reindex` | Force reindex |
| `test_file_hash` | Hash computation |
| `test_get_index_stats` | Statistics retrieval |

#### NEW: `test_symbol_graph.py`
```python
class TestSymbolGraph:
    """Tests for symbol graph."""
    
    def test_parse_python_file(self):
        """Test Python file parsing."""
    
    def test_incremental_update(self):
        """Test incremental updates."""
    
    def test_import_tracking(self):
        """Test import relationship tracking."""
    
    def test_find_tests_for_module(self):
        """Test test file discovery."""
```

### 2.6 Plan Validator Tests

#### Test File: `test_plan_validator.py` + `test_plan_validator_enhanced.py` (Existing)
| Test | Description |
|------|-------------|
| `test_valid_plan_with_verification` | Valid plan passes |
| `test_valid_plan_no_verification_warning` | Warning for no verification |
| `test_empty_plan` | Empty plan fails |
| `test_strict_mode_requires_verification` | Strict mode enforcement |
| `test_read_before_edit_validation` | Read-before-edit check |
| `test_dangerous_operations_detected` | Dangerous op detection |
| `test_severity_levels` | Severity level assignment |

---

## 3. Integration Test Specifications

### 3.1 LangGraph Orchestrator Integration

#### Test File: `test_langgraph_orchestrator.py` (Existing)
| Test | Description | Mock Level |
|------|-------------|------------|
| `test_orchestrator_instantiation` | Basic instantiation | None |
| `test_orchestrator_graph_compiles` | Graph compilation | None |
| `test_orchestrator_run_with_mocked_llm` | Full run with mocks | Full |

#### NEW: `test_langgraph_integration.py`
```python
class TestLangGraphPipeline:
    """Integration tests for LangGraph pipeline."""
    
    @pytest.mark.asyncio
    async def test_fast_path_simple_task(self):
        """Test perception → execution fast path."""
        # Mock LLM to return tool call
        # Run agent
        # Verify bypasses analysis and planning
    
    @pytest.mark.asyncio
    async def test_full_pipeline_complex_task(self):
        """Test full perception → analysis → planning → execution."""
        # Mock LLM appropriately for each node
        # Verify full pipeline execution
    
    @pytest.mark.asyncio
    async def test_replan_on_patch_size_violation(self):
        """Test replan trigger on oversized patch."""
        # Mock execution to return oversized patch
        # Verify replan node is triggered
    
    @pytest.mark.asyncio
    async def test_debug_retry_on_failure(self):
        """Test debug node retry on verification failure."""
        # Mock verification to fail
        # Verify debug node is triggered
        # Verify max retries enforced
    
    @pytest.mark.asyncio
    async def test_evaluation_complete_workflow(self):
        """Test evaluation node routes correctly."""
        # Mock verification to pass
        # Verify evaluation routes to memory_sync
```

### 3.2 Tool Integration Tests

#### Test File: `test_tools_sandbox.py` (Existing)
| Test | Description |
|------|-------------|
| `test_sandbox_validate_ast` | AST validation |
| `test_sandbox_apply_patch` | Patch application |

#### NEW: `test_tools_integration.py`
```python
class TestToolIntegration:
    """Integration tests for tool system."""
    
    def test_file_read_write_cycle(self):
        """Test read → edit → verify cycle."""
        # Write file
        # Read it back
        # Edit it
        # Verify edit applied
    
    def test_tool_timeout_integration(self):
        """Test timeout enforcement."""
        # Register slow tool
        # Execute with timeout
        # Verify timeout error
    
    def test_tool_error_handling(self):
        """Test error propagation."""
        # Execute failing tool
        # Verify error handling
    
    def test_preflight_validation_chain(self):
        """Test preflight → execution → result chain."""
        # Verify all stages execute
```

### 3.3 Provider Integration Tests

#### Test File: `test_ollama_adapter.py`, `test_lm_studio_adapter.py` (Existing)
| Test | Description |
|------|-------------|
| `test_ollama_chat_completion` | Ollama chat |
| `test_lmstudio_chat_completion` | LM Studio chat |
| `test_model_fallback` | Fallback mechanism |

#### NEW: `test_provider_integration.py`
```python
class TestProviderIntegration:
    """Integration tests for LLM providers."""
    
    @pytest.mark.integration
    @pytest.mark.ollama
    async def test_ollama_connection(self):
        """Test Ollama connectivity."""
        # Skip if Ollama not available
    
    @pytest.mark.integration
    @pytest.mark.lmstudio  
    async def test_lmstudio_connection(self):
        """Test LM Studio connectivity."""
        # Skip if LM Studio not available
    
    def test_provider_fallback_chain(self):
        """Test fallback when primary fails."""
        # Mock primary failure
        # Verify fallback
    
    def test_model_caching(self):
        """Test model list caching."""
        # Call multiple times
        # Verify cache hit
```

### 3.4 Memory Integration Tests

#### NEW: `test_memory_integration.py`
```python
class TestMemoryIntegration:
    """Integration tests for memory system."""
    
    @pytest.mark.asyncio
    async def test_distiller_task_summary(self):
        """Test distiller creates task summary."""
        # Run agent with task
        # Verify TASK_STATE.md created
    
    @pytest.mark.asyncio
    async def test_session_persistence(self):
        """Test session data persists."""
        # Create session
        # Verify data saved
    
    @pytest.mark.asyncio
    async def test_message_history_windowing(self):
        """Test message windowing with real token limits."""
        # Add many messages
        # Verify oldest dropped
```

---

## 4. E2E Test Specifications

### 4.1 Basic Workflow E2E

#### NEW: `tests/e2e/test_basic_workflows.py`
```python
class TestBasicWorkflows:
    """End-to-end basic workflow tests."""
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_simple_file_read(self):
        """Test simple file read workflow."""
        # User: "Read main.py"
        # Verify: File content returned
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_simple_file_edit(self):
        """Test simple file edit workflow."""
        # User: "Add function to main.py"
        # Verify: File modified correctly
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_multi_step_task(self):
        """Test multi-step task workflow."""
        # User: "Create module with tests"
        # Verify: Module and test file created
```

### 4.2 Agent Behavior E2E

#### NEW: `tests/e2e/test_agent_behavior.py`
```python
class TestAgentBehavior:
    """End-to-end agent behavior tests."""
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_agent_respects_read_before_edit(self):
        """Test agent reads before editing."""
        # User: "Edit main.py"
        # Verify: read_file called before edit_file
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_agent_uses_verification(self):
        """Test agent runs verification."""
        # User: "Fix the bug"
        # Verify: Tests run after fix
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_agent_handles_errors_gracefully(self):
        """Test agent error handling."""
        # Provide impossible task
        # Verify graceful error
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_agent_loop_prevention(self):
        """Test agent doesn't loop indefinitely."""
        # Provide task that causes loops
        # Verify max rounds enforced
```

### 4.3 Scenario Benchmarks (SWE-bench style)

#### NEW: `tests/e2e/test_scenarios.py`
```python
class TestScenarioBenchmarks:
    """SWE-bench style scenario tests."""
    
    @pytest.mark.e2e
    @pytest.mark.slow
    @pytest.mark.scenario
    @pytest.mark.parametrize("scenario", [
        "bug_fix_simple",
        "feature_add",
        "refactor_rename",
        "test_write",
    ])
    async def test_scenario(self, scenario):
        """Run scenario benchmark."""
        # Load scenario
        # Run agent
        # Verify output matches expected
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_bug_fix_simple(self):
        """Test simple bug fix."""
        # Setup: Code with bug
        # Task: "Fix the bug"
        # Verify: Bug fixed, tests pass
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_feature_add(self):
        """Test adding a feature."""
        # Setup: Existing codebase
        # Task: "Add feature X"
        # Verify: Feature implemented
    
    @pytest.mark.e2e
    @pytest.mark.slow
    async def test_refactor_rename(self):
        """Test refactoring."""
        # Setup: Code to refactor
        # Task: "Rename X to Y"
        # Verify: All references updated
```

---

## 5. Test Fixtures & Helpers

### 5.1 Shared Fixtures

```python
# tests/fixtures/__init__.py

@pytest.fixture
def temp_workdir(tmp_path):
    """Create temporary working directory."""
    return tmp_path / "workspace"

@pytest.fixture
def mock_orchestrator(temp_workdir):
    """Create mocked orchestrator."""
    return Orchestrator(working_dir=str(temp_workdir))

@pytest.fixture
def sample_agent_state():
    """Create sample AgentState."""
    return AgentState(...)

@pytest.fixture
def mock_llm_response():
    """Create mock LLM response."""
    return {"choices": [{"message": {"content": "..."}}]}

@pytest.fixture
def sample_repo(tmp_path):
    """Create sample repository."""
    # Create Python files
    # Create test files
    return repo_path
```

### 5.2 Mock Utilities

```python
# tests/utils/mocks.py

class MockLLM:
    """Mock LLM for testing."""
    
    def __init__(self, responses: List[Dict]):
        self.responses = responses
        self.call_count = 0
    
    def __call__(self, *args, **kwargs):
        response = self.responses[self.call_count % len(self.responses)]
        self.call_count += 1
        return response

def create_mock_provider():
    """Create mock LLM provider."""
    pass
```

---

## 6. Test Execution

### 6.1 Running Tests

```bash
# Run all unit tests (fast)
pytest tests/unit/ -v

# Run unit tests for specific module
pytest tests/unit/test_orchestrator.py -v

# Run integration tests
pytest tests/integration/ -v

# Run E2E tests (slow)
pytest tests/e2e/ -v

# Run specific test
pytest tests/unit/test_orchestrator.py::test_execute_tool_echo -v

# Run with coverage
pytest tests/unit/ --cov=src --cov-report=html

# Run tests matching pattern
pytest tests/ -k "test_orchestrator" -v

# Skip slow tests
pytest tests/ -m "not slow" -v

# Run only E2E tests
pytest tests/ -m "e2e" -v
```

### 6.2 Test Markers

```python
@pytest.mark.unit           # Unit tests (default)
@pytest.mark.integration   # Integration tests  
@pytest.mark.e2e          # End-to-end tests
@pytest.mark.slow         # Slow running tests
@pytest.mark.ollama        # Requires Ollama
@pytest.mark.lmstudio     # Requires LM Studio
@pytest.mark.scenario     # Scenario benchmark
@pytest.mark.security      # Security tests
```

### 6.3 CI/CD Configuration

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run unit tests
        run: pytest tests/unit/ -v --tb=short
  
  integration-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Start Ollama
        run: |
          curl -fsSL https://ollama.ai/install.sh | sh
          ollama serve &
          sleep 5
      - name: Run integration tests
        run: pytest tests/integration/ -v --tb=short
  
  e2e-tests:
    runs-on: ubuntu-latest
    if: github.event_name == 'schedule'
    steps:
      - uses: actions/checkout@v4
      - name: Run E2E tests
        run: pytest tests/e2e/ -v --tb=short
```

---

## 7. Test Coverage Goals

### 7.1 Coverage Targets

| Category | Current | Target |
|----------|---------|--------|
| Unit Tests | ~70% | 80% |
| Integration Tests | ~40% | 60% |
| E2E Scenarios | 0% | 20% |

### 7.2 Critical Path Coverage

Must have tests for:
- [x] Tool execution chain
- [x] Graph node routing
- [x] Read-before-edit enforcement
- [x] Sandbox validation
- [x] Plan parsing
- [x] Token budgeting
- [ ] Full workflow (E2E)
- [ ] Error recovery paths

---

## 8. Test Maintenance

### 8.1 Adding New Tests

1. **Identify test type** (unit/integration/e2e)
2. **Add to appropriate file** or create new file
3. **Use appropriate fixtures**
4. **Add docstrings** explaining what is tested
5. **Run tests** to verify
6. **Update coverage** if adding new code paths

### 8.2 Test Review Checklist

- [ ] Tests run without errors
- [ ] Tests are isolated (no inter-dependencies)
- [ ] Tests have clear names
- [ ] Tests document expected behavior
- [ ] Edge cases are covered
- [ ] Error paths are tested

---

## 9. References

- Existing test files: `tests/unit/`, `tests/integration/`
- Test configuration: `pytest.ini`, `pyproject.toml`
- Fixtures: `tests/conftest.py`

---

*End of Test Plan*
