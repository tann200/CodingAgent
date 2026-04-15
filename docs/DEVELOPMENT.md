# Development Guide

> **Test Baseline:** 3844 unit tests passing  
> **Audit Status:** 0 Critical, 0 High issues  
> **Last Updated:** 2026-04-14

## Getting Started

### Prerequisites
- Python 3.11+
- LM Studio, Ollama, or cloud API keys for LLM inference

### Setup

```bash
# Clone and install
git clone https://github.com/<user>/CodingAgent.git
cd CodingAgent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure provider
# Edit src/config/providers.json with your provider settings

# Run
python -m src.main
```

---

## Project Structure

```
src/
├── main.py                    # CLI entry point
├── core/
│   ├── orchestration/         # Agent orchestration
│   │   ├── orchestrator.py   # Main class
│   │   ├── graph/            # LangGraph nodes
│   │   └── *.py              # Managers, services
│   ├── context/              # Prompt building
│   ├── memory/               # Session/vector stores
│   ├── indexing/             # Repo intelligence
│   └── inference/            # LLM adapters
└── tools/                    # Tool definitions
    ├── _tool.py              # @tool decorator
    ├── _registry.py          # Auto-discovery
    └── *.py                  # Tool modules
```

---

## Core Concepts

### 1. Tool System

Tools are defined with the `@tool` decorator:

```python
from src.tools import tool

@tool(
    side_effects=["write"],           # Permission category
    tags=["coding"],                   # Toolset tags
    permission_kind=PermissionKind.WRITE_FILE
)
def my_tool(param: str) -> dict:
    """Tool description for LLM."""
    return {"ok": True}
```

**Permission Kinds:**
- `READ_FILE` — read operations
- `WRITE_FILE` — write/edit operations
- `EXECUTE_BASH` — shell commands
- `NETWORK` — web requests
- `GIT_READ` / `GIT_WRITE` — git operations
- `DELEGATE` — subagent spawning
- `LSP_READ` / `LSP_WRITE` — LSP operations
- `PLAN` — plan mode

### 2. Graph Nodes

The 16-node LangGraph pipeline:

| Node | Purpose |
|------|---------|
| `perception_node` | Parse task → generate tool call |
| `analysis_node` | Repository intelligence |
| `planning_node` | Generate execution plan |
| `plan_validator_node` | Validate plan structure |
| `execution_node` | Execute tool calls |
| `step_controller_node` | Gate step progression |
| `verification_node` | Run tests/linters |
| `evaluation_node` | Evaluate success/failure |
| `debug_node` | Debug failed steps |
| `replan_node` | Split oversized patches |
| `delegation_node` | Spawn subagents |
| `analyst_delegation_node` | Spawn analyst subagents |
| `memory_update_node` | Sync memory |
| `frontier_loop_node` | Tight loop (LARGE/FRONTIER) |
| `wait_for_user_node` | Wait for input |
| `node_utils` | Shared utilities |

### 3. Model Tiers

Classify models to optimize behavior:

```python
from src.core.inference.model_tiers import classify_model, ModelTier

tier = classify_model("gpt-4o")  # → ModelTier.FRONTIER
```

| Tier | Tools | Behavior |
|------|-------|----------|
| NANO | 8 | YAML tools, simple_mode |
| SMALL | 20 | Full pipeline, minimal prompts |
| MEDIUM | 35 | JSON tools, standard |
| LARGE | 50 | Skip validation |
| FRONTIER | 60 | frontier_loop_node |

### 4. Session Management

```python
# Fork session for experimental changes
session_id = orchestrator.fork_session()

# Revert to prior state
orchestrator.revert_session(snapshot_id)

# Resume from previous session
orchestrator.load_session(session_id)
```

---

## Adding Components

### Adding a Tool

1. Create or edit tool module in `src/tools/`
2. Use `@tool` decorator
3. Register in `src/tools/_registry.py` `_BUILTIN_MODULES` (if new module)

```python
# src/tools/my_tools.py
from src.tools import tool
from typing import Dict

@tool(tags=["custom"])
def custom_tool(arg: str) -> Dict:
    """Tool description."""
    return {"ok": True, "arg": arg}
```

### Adding a Graph Node

1. Create node function in `src/core/orchestration/graph/nodes/`
2. Add to graph in `src/core/orchestration/graph/builder.py`

```python
async def my_node(state: StateLike, config: RunnableConfig):
    # Process state
    return {"key": "value"}
```

### Adding a Provider Adapter

1. Create adapter in `src/core/inference/adapters/`
2. Inherit from `OpenAICompatibleAdapter` or base class
3. Register in `src/core/inference/llm_manager.py`

---

## Testing

### Running Tests

```bash
# Unit tests (no LLM)
pytest tests/unit -q

# Specific file
pytest tests/unit/test_tools_file_io.py -v

# Integration (requires provider)
RUN_INTEGRATION=1 pytest tests/integration -q

# Benchmarks
pytest tests/benchmarks -v
```

### Writing Tests

```python
# tests/unit/test_example.py
import pytest

def test_example():
    assert True
```

### Test Fixtures

```python
@pytest.fixture
def temp_workdir(tmp_path):
    """Create temporary working directory."""
    return tmp_path
```

---

## Code Quality

### Type Checking

```bash
pyright src/
```

### Linting

```bash
ruff check src/
```

### Pre-commit

```bash
pip install pre-commit
pre-commit install
```

---

## Debugging

### Enable Logging

```bash
# Via environment
CODINGAGENT_LOG_LEVEL=DEBUG python -m src.main
```

### Event Bus

Subscribe to events:

```python
event_bus.subscribe("tool.execute.start", lambda data: print(data))
```

### Execution Trace

```python
trace = orchestrator._read_execution_trace()
```

---

## Common Tasks

### Configure New Provider

Edit `src/config/providers.json`:

```json
{
  "name": "my_provider",
  "type": "openai_compat",
  "base_url": "http://localhost:11434/v1",
  "models": ["my-model"],
  "active": true
}
```

### Add Custom Role

1. Create `config/agent-brain/roles/custom-role.md`
2. Use in code via `role_name="custom-role"`

### Modify Tool Permissions

Edit `src/tools/tools_config.py` - `TOOL_PERMISSIONS` dict

---

## Architecture References

| Document | Description |
|----------|-------------|
| `docs/ARCHITECTURE.md` | Full system architecture |
| `docs/audit/` | Audit reports |
| `docs/CODEBASE_FINDINGS.md` | Known issues |

---

## Troubleshooting

### Import Errors
- Ensure `pip install -e .` was run
- Check Python version (3.11+)

### LLM Not Responding
- Verify provider is running
- Check `src/config/providers.json` configuration
- Check API key in `~/.config/codingagent/prefs.json`

### Test Failures
- Check test output for specific failures
- Run with `-v` flag for details
- Verify no network required for unit tests

### Type Errors
- Run `pyright` to identify issues
- Check imports are correct
- Verify type annotations

---

## Contributing

1. Run tests: `pytest tests/unit -q`
2. Type check: `pyright src/`
3. Add tests for new features
4. Update documentation
5. Open PR

---

## Key Constants

| Constant | File | Purpose |
|----------|------|---------|
| `DOOM_LOOP_THRESHOLD` | `loop_guards.py` | Max identical tool calls |
| `COOLDOWN_GAP` | `loop_guards.py` | Tool cooldown |
| `_AUTONOMOUS_MAX_TOOL_CALLS` | `builder.py` | Max tool calls |
| `_TOOL_LIMITS` | `model_tiers.py` | Tools per tier |
