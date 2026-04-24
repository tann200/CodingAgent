# CodingAgent

A local-first autonomous coding agent built on LangGraph. Runs on local LLMs (LM Studio, Ollama) or cloud providers (OpenRouter, OpenAI, Anthropic, GitHub Copilot, Groq, LiteLLM). No cloud dependency required.

## Features

- **LangGraph pipeline** — 16-node cognitive pipeline (perception → analysis → planning → execution → verification → evaluation)
- **60+ tools** — auto-discovered via `@tool` decorator: file ops, git, web, AST, repo search, verification, memory, subagents
- **Production TUI** — Textual-based terminal UI with streaming, per-tool icons, diff preview, slash commands
- **Multi-agent delegation** — `delegate_task` spawns role-specific subagents (analyst, operational, strategic, reviewer, debugger)
- **Security** — bwrap sandbox, 3-tier bash allowlist, workspace scope guard, read-before-write enforcement
- **Memory** — SQLite session store, vector store-based semantic search, context distiller, auto-compactor
- **Repository intelligence** — SHA-256 incremental indexing, symbol graph, semantic code search
- **Model tiers** — NANO (8 tools) → FRONTIER (60 tools) based on model capability
- **Session fork/revert** — branch sessions for experimental changes
- **Audit history** — 29+ cycles completed, 0 Critical/High issues

## Requirements

Python 3.11+

## Quick Start

```bash
# Install
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# Run TUI (recommended)
python -m src.main

# Headless mode
python -m src.main --task "fix the bug" --workdir /path/to/repo
```

## Configuration

Edit `src/config/providers.json`:

```json
[
  {
    "name": "lm_studio",
    "type": "lm_studio",
    "base_url": "http://localhost:1234/v1",
    "models": ["gemma-4-e4b-it"],
    "active": true
  }
]
```

Supported: `lm_studio`, `ollama`, `openrouter`, `openai`, `anthropic`, `github_copilot`, `groq`, `litellm`

## CLI Usage

```bash
# Basic usage
python -m src.main --task "your task"

# Resume session
python -m src.main --resume-session <session_id> --task "continue"

# Dry run (preview only)
python -m src.main --dry-run --task "refactor"

# Output formats
python -m src.main --task "fix bug" --output-format json
```

## Architecture

See `docs/ARCHITECTURE.md` for complete documentation.

### Key Components

| Component | File | Purpose |
|-----------|------|---------|
| Orchestrator | `src/core/orchestration/orchestrator.py` | Main agent class |
| Graph Builder | `src/core/orchestration/graph/builder.py` | LangGraph compilation |
| Tool Registry | `src/tools/_registry.py` | Tool auto-discovery |
| Context Builder | `src/core/context/context_builder.py` | Prompt building |
| Model Tiers | `src/core/inference/model_tiers.py` | Tier classification |

### Pipeline

```
Fast-path: perception → execution → verification → evaluation → memory_sync
Full: perception → analysis → planning → plan_validator → execution → verification → evaluation → memory_sync
Frontier: perception → frontier_loop → verification → evaluation → memory_sync
```

## Testing

```bash
# Unit tests (no LLM required)
pytest tests/unit -q

# With specific test
pytest tests/unit/test_tools_file_io.py -v

# Benchmark tests
pytest tests/benchmarks -v
```

## Development

### Adding a Tool

```python
from src.tools import tool

@tool(side_effects=["write"], tags=["coding"])
def my_tool(param: str) -> dict:
    """Description shown to the LLM."""
    return {"ok": True, "result": param}
```

### Running Tests

```bash
# All unit tests
pytest tests/unit -q

# Integration tests (requires running provider)
RUN_INTEGRATION=1 pytest tests/integration -q
```

### Code Quality

```bash
# Type checking
pyright src/

# Format check
ruff check src/
```

## Documentation

| Document | Description |
|----------|-------------|
| `docs/ARCHITECTURE.md` | Full system architecture |
| `docs/DEVELOPMENT.md` | Developer guide |
| `docs/audit/` | Audit reports (vol1–vol29) |
| `docs/TODO_METRICS.md` | How to enable and use TODO metrics (Prometheus) |
| `docs/CODEBASE_FINDINGS.md` | Known issues |

## Model Tiers

| Tier | Params | Tools | Max Turns |
|------|--------|-------|-----------|
| NANO | ≤7B | 8 | 15 |
| SMALL | 7-14B | 20 | 25 |
| MEDIUM | 14-70B | 35 | 40 |
| LARGE | >70B | 50 | 60 |
| FRONTIER | Cloud | 60 | 80 |

## Security

5-layer security model:
1. Pattern block (`_BASE_DANGEROUS_PATTERNS`)
2. Restricted commands (approval required)
3. Safe commands (auto-allowed)
4. AST-level analysis (`bash_security.py`)
5. Sandbox (`sandbox.py` with bubblewrap)

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `CODINGAGENT_SANDBOX_LEVEL` | off/workspace/full |
| `CODINGAGENT_TRUSTED` | Skip approval in CI |
| `CODING_AGENT_HTTP_SERVER` | Start HTTP server |

## Test Baseline

- **3844** unit tests passing
- **7** benchmark tests
- **0** Critical issues
- **0** High issues
