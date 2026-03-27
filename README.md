# CodingAgent

A local-first autonomous coding agent built on LangGraph. Runs entirely on local LLMs (LM Studio, Ollama) or cloud providers (OpenRouter). No cloud dependency required.

## Requirements

Python 3.11 (`pyproject.toml` pins `>=3.11,<3.12`).

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
pip install -e .[dev]
```

## Running

```bash
# Launch TUI
python scripts/run_tui.py

# Headless agent run
python scripts/run_generate.py --task "your task" --working-dir /path/to/repo
```

## Configuration

Provider configuration lives in `src/config/providers.json`. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for setup details.

```json
[
  {
    "name": "lm_studio_local",
    "type": "lm_studio",
    "base_url": "http://localhost:1234/v1",
    "models": ["qwen/qwen3.5-9b"]
  }
]
```

Supported provider types: `lm_studio`, `ollama`, `openrouter`.

## LLM Adapters

| Adapter | File |
|---------|------|
| LM Studio | `src/core/inference/adapters/lm_studio_adapter.py` |
| Ollama | `src/core/inference/adapters/ollama_adapter.py` |
| OpenRouter | `src/core/inference/adapters/openrouter_adapter.py` |

All extend `OpenAICompatibleAdapter` (`src/core/inference/adapters/openai_compat_adapter.py`).

## Tests

```bash
# Unit tests (no live LLM required)
.venv/bin/pytest tests/unit -q -p no:logging

# Integration tests (requires local provider running)
RUN_INTEGRATION=1 .venv/bin/pytest tests/integration -q
```

## Tools

60 tools auto-discovered via `build_registry()`. Key tool modules:

| Module | Tools |
|--------|-------|
| `file_tools` | `read_file`, `write_file`, `edit_file`, `edit_file_atomic`, `bash`, `bash_readonly`, `glob`, `tail_log_file`, `create_directory` |
| `git_tools` | `git_status`, `git_log`, `git_diff`, `git_commit`, `git_stash`, `git_restore` |
| `web_tools` | `web_search`, `read_web_page` (SSRF-protected) |
| `ast_tools` | `ast_rename`, `ast_list_symbols` |
| `interaction_tools` | `ask_user`, `submit_plan_for_review` |
| `repo_tools` | `search_code`, `find_symbol`, `find_references` |
| `repo_analysis_tools` | `analyze_repository` (Python, JS/TS, Go, Rust) |
| `verification_tools` | `run_tests`, `run_linter`, `syntax_check`, `run_js_tests`, `run_ts_check` |
| `project_tools` | `fingerprint_tech_stack` |
| `memory_tools` | `memory_search` |
| `todo_tools` | `manage_todo` |

**Read-before-write guardrail**: All write tools enforce that existing files must be read before modification. Dual-tracked via `contextvars.ContextVar` + global lock-protected set (`src/tools/guardrails.py`).

**Post-write auto-lint**: Every write triggers a fast syntax check for the modified file's language (`src/tools/lint_dispatch.py`).

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — pipeline, nodes, tools, memory system
- [Development Guide](docs/DEVELOPMENT.md) — workflows, tool registry, provider setup
- [Gap Analysis](docs/TOOLS_GAP_ANALYSIS.md) — comparison with LocalCodingAgent, implementation plan
- [System Map](docs/system_map.md) — generated file tree
