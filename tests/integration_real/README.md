# True Integration Tests

This directory contains **real integration tests** that verify multiple components working together with minimal mocking.

## Philosophy

**Integration tests should integrate real components**, not orchestrate mocks.

### ✅ Good Integration Test
```python
def test_tool_chain_integration(tmp_path):
    """Test search → read → edit chain with REAL tools."""
    orch = Orchestrator(working_dir=str(tmp_path))
    
    # Create test file
    (tmp_path / "app.py").write_text("def old_name(): pass")
    
    # Use DeterministicAdapter for predictable LLM responses
    adapter = DeterministicAdapter([
        {"role": "assistant", "tool_calls": [
            {"name": "search_code", "args": {"pattern": "old_name"}}
        ]},
        {"role": "assistant", "tool_calls": [
            {"name": "read_file", "args": {"path": "app.py"}}
        ]},
        {"role": "assistant", "tool_calls": [
            {"name": "edit_file", "args": {
                "path": "app.py",
                "old_string": "old_name",
                "new_string": "new_name"
            }}
        ]}
    ])
    
    result = orch.run_with_adapter(adapter)
    
    # Verify REAL file modification
    assert "new_name" in (tmp_path / "app.py").read_text()
```

**What makes this good:**
- ✅ Real `search_code` tool execution
- ✅ Real `read_file` tool execution
- ✅ Real `edit_file` tool execution (actual file modification)
- ✅ Real orchestrator graph execution
- ⚠️ Only LLM is mocked (with deterministic responses)

---

### ❌ Bad "Integration" Test (belongs in unit tests)
```python
def test_pipeline_mock(tmp_path):
    """Test pipeline with mocked tools."""
    
    # Mock ALL the tools
    def edit_file_mock(**kwargs):
        return {"status": "ok", "edited": True}
    
    def run_tests_mock(**kwargs):
        return {"status": "ok", "output": "Tests passed"}
    
    orch = Orchestrator(working_dir=str(tmp_path))
    orch.tool_registry.register("edit_file", edit_file_mock, ...)
    orch.tool_registry.register("run_tests", run_tests_mock, ...)
    
    # This only tests graph routing, not real integration
```

**What makes this bad:**
- ❌ No real tool execution
- ❌ Only tests orchestration logic (belongs in unit tests)
- ❌ Provides false confidence about integration

---

## Test Categories

### 1. **Tool Chain Integration**
Tests that verify multiple tools work together in sequence.

Examples:
- `search_code` → `read_file` → `edit_file`
- `read_file` → `analyze` → `write_file`
- `bash` command → verify output → next command

**File**: `test_tool_chain_integration.py`

---

### 2. **Real Delegation Integration**
Tests that verify subagent spawning and result merging.

Requirements:
- Don't mock `delegate_task_async`
- Use real delegation infrastructure
- Verify subagent actually spawns and executes
- Verify results merge into parent agent state

**File**: `test_delegation_integration.py`

---

### 3. **Cache Invalidation Integration**
Tests that verify caching behavior across real operations.

Examples:
- File write invalidates context cache
- Tool execution updates session state
- Memory compaction triggers correctly

**File**: `test_cache_integration.py`

---

### 4. **Permission/Guardrail Integration**
Tests that verify permission checks work with real orchestrator and tools.

Examples:
- Read-before-write enforcement
- Path traversal blocking
- Permission gateway integration

**File**: `test_permission_integration.py`

---

## Guidelines

### When to Write Integration Tests Here

Write integration tests here when:
1. Testing behavior that spans multiple components
2. Testing real file operations, tool execution, or state changes
3. Testing caching, invalidation, or cross-component coordination
4. Verifying that components work together correctly

### When NOT to Write Integration Tests Here

Don't write integration tests here when:
1. Testing a single function or class (use unit tests)
2. Mocking all external dependencies (use unit tests)
3. Testing orchestration routing logic only (use unit tests)
4. Testing configuration parsing (use unit tests)

### Mocking Policy

**Acceptable to mock:**
- ✅ LLM calls (use `DeterministicAdapter` or `MockAdapter`)
- ✅ External APIs (GitHub, network requests)
- ✅ Slow operations (when testing behavior, not performance)

**Not acceptable to mock:**
- ❌ Tool execution (file read/write, bash, etc.)
- ❌ Core orchestration logic
- ❌ State management
- ❌ Event bus / subscription system
- ❌ Delegation infrastructure

### Naming Convention

```python
def test_<component1>_<component2>_integration(tmp_path):
    """Brief description of what's being integrated."""
```

Examples:
- `test_search_read_edit_integration`
- `test_delegation_result_merging_integration`
- `test_file_write_cache_invalidation_integration`

---

## Running Integration Tests

```bash
# Run all integration tests
pytest tests/integration_real/ -v

# Run specific integration test
pytest tests/integration_real/test_tool_chain_integration.py::test_search_read_edit_integration -v

# Run with coverage
pytest tests/integration_real/ --cov=src.core.orchestration --cov-report=html
```

---

## Contributing

When adding a new integration test:

1. **Check if it truly tests integration** - If you're mocking most components, it's probably a unit test
2. **Use descriptive names** - Test name should indicate what components are integrated
3. **Add docstring** - Explain what integration behavior is being verified
4. **Verify real behavior** - Assertions should check actual state changes, not mock calls
5. **Keep tests focused** - Test one integration path per test function

---

## Examples

See:
- `test_tool_chain_integration.py` - Tool chain integration examples
- `test_delegation_integration.py` - Real delegation testing
- `test_cache_integration.py` - Cache invalidation integration

For comparison with mock-heavy tests (now in `tests/unit/orchestration/`):
- `tests/unit/orchestration/test_pipeline_orchestration.py` - Pipeline routing unit tests
- `tests/unit/orchestration/test_delegation_node_unit.py` - Delegation node unit tests
