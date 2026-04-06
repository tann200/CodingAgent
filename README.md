# CodingAgent

A local-first autonomous coding agent built on LangGraph. Runs on local LLMs (LM Studio, Ollama) or cloud providers (OpenRouter, OpenAI, Anthropic, GitHub Copilot). No cloud dependency required.

## Features

- **LangGraph pipeline** — multi-node cognitive pipeline (perception → analysis → planning → execution → verification → evaluation)
- **60+ tools** auto-discovered via `@tool` decorator across 16 modules: file ops, git, web, AST, repo search, verification, memory, subagents, and more
- **Production TUI** — Textual-based terminal UI with flicker-free 100+ tok/s streaming, command palette, settings screen, diff preview, slash commands, session timeline
- **Multi-agent delegation** — `delegate_task` spawns isolated role-specific subagents (analyst, operational, strategic, reviewer, debugger); PRSW parallel read / sequential write coordination
- **Security hardening** — bwrap sandbox, bash allowlist (3-tier), workspace scope guard, read-before-write enforcement, SSRF protection, path traversal blocks
- **Memory system** — SQLite session store, LanceDB vector search, context distiller, trajectory logger, skill learner
- **Repository intelligence** — incremental SHA-256 indexing (15+ languages), symbol graph, semantic code search
- **GitHub Copilot auth** — OAuth device flow; also supports LM Studio, Ollama, OpenRouter, OpenAI, Anthropic
- **16 audit cycles** completed; all Critical/High/Medium findings resolved

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
# Launch the Textual TUI (recommended)
bash start_tui.sh

# Headless agent run
python scripts/run_generate.py --task "your task" --working-dir /path/to/repo
```

## Configuration

Provider configuration lives in `src/config/providers.json`. Must be a JSON array:

```json
[
  {
    "name": "lm_studio_local",
    "type": "lm_studio",
    "base_url": "http://localhost:1234/v1",
    "models": ["qwen/qwen3.5-9b"]
  },
  {
    "name": "openrouter",
    "type": "openrouter",
    "models": ["anthropic/claude-3.5-sonnet"]
  },
  {
    "name": "github_copilot",
    "type": "github_copilot"
  }
]
```

Supported provider types: `lm_studio`, `ollama`, `openrouter`, `openai`, `anthropic`, `github_copilot`.

API keys for cloud providers are stored in `~/.config/codingagent/prefs.json` (permissions `0o600`) via the TUI settings panel or `SaveProviderCredentials` event. GitHub Copilot uses OAuth device flow — click **Login with GitHub** in the TUI settings screen.

## LLM Adapters

| Adapter | File | Notes |
|---------|------|-------|
| LM Studio | `src/core/inference/adapters/lm_studio_adapter.py` | Local HTTP, short-name model resolution |
| Ollama | `src/core/inference/adapters/ollama_adapter.py` | Local HTTP |
| OpenRouter | `src/core/inference/adapters/openrouter_adapter.py` | Cloud, `/models` discovery |
| OpenAI-compat | `src/core/inference/adapters/openai_compat_adapter.py` | Base class for all REST adapters |
| Anthropic | `src/core/inference/adapters/anthropic_adapter.py` | Prompt caching via `cache_control` |
| GitHub Copilot | `src/core/inference/adapters/github_copilot_adapter.py` | OAuth device flow auth |
| Mock | `src/core/inference/adapters/mock_adapter.py` | Unit tests / CI |

## Tools

60+ tools auto-discovered via `build_registry()`. Key modules:

| Module | Tools |
|--------|-------|
| `file_tools` | `read_file`, `write_file`, `edit_file_atomic`, `bash`, `bash_readonly`, `glob`, `tail_log_file`, `create_directory`, `batched_file_read` |
| `git_tools` | `git_status`, `git_log`, `git_diff`, `git_commit`, `git_stash`, `git_restore` |
| `web_tools` | `web_search`, `read_web_page` (SSRF-protected) |
| `ast_tools` | `ast_rename`, `ast_list_symbols` |
| `interaction_tools` | `ask_user`, `submit_plan_for_review`, `send_user_message` |
| `repo_tools` | `search_code`, `find_symbol`, `find_references` |
| `verification_tools` | `run_tests`, `run_linter`, `syntax_check`, `run_js_tests`, `run_ts_check` |
| `subagent_tools` | `delegate_task`, `list_subagent_roles` |
| `memory_tools` | `memory_search` |
| `todo_tools` | `manage_todo` |
| `lsp_tools` | `lsp_diagnostics`, `lsp_goto_definition`, `lsp_references` |
| `batch_tools` | `batch_tool_calls` (parallel tool dispatch) |
| `sandbox` | `run_sandboxed` (bwrap wrapper) |

**Read-before-write guardrail**: All write tools enforce that existing files must be read before modification. Dual-tracked via `ContextVar` + global lock-protected set (`src/tools/guardrails.py`).

**Post-write auto-lint**: Every write triggers a fast syntax check for the modified file's language (10 s timeout, never raises) — Python via `py_compile`, JS/TS via `node --check`, Go via `go build`, Rust via `rustc`.

## TUI

The production TUI lives in `tui/` and is implemented with [Textual](https://github.com/Textualize/textual).

**Key bindings:**

| Binding | Action |
|---------|--------|
| `ctrl+o` | Command palette |
| `ctrl+s` | Settings screen |
| `ctrl+l` | Toggle console log panel |
| `tab` | Cycle agent roles |
| `Esc Esc` | Interrupt agent |
| `ctrl+q` | Quit |

**Slash commands:** `/help`, `/clear`, `/new`, `/compact`, `/continue`, `/interrupt`, `/status`, `/fast`, `/provider`, `/model`, `/settings`, `/sessions`, `/timeline`, `/diff`, `/fork`, `/mcp`, `/quit`

Architecture: fully decoupled — UI communicates with backend exclusively via typed `Message` subclasses in `bus.py` (backend → UI) and `events.py` (UI → backend). The TUI never imports `src/core` directly; all data flows through `core_bridge.py`.

## Multi-Agent Delegation

```python
# In agent tool calls:
delegate_task(
    role="analyst",           # analyst | operational | strategic | reviewer | debugger
    subtask_description="...",
    working_dir="/path/to/repo",
)
```

Subagents run in isolated LangGraph graphs with role-specific tool sets. Depth is bounded at 3 via ContextVar (not forgeable by subprocesses). Manifests are written before spawning.

## Tests

```bash
# Unit tests (no live LLM required)
.venv/bin/pytest tests/unit -q -p no:logging

# Integration tests (requires local provider running)
RUN_INTEGRATION=1 .venv/bin/pytest tests/integration -q
```

Set `CODINGAGENT_TRUSTED=1` to enable MCP server, hooks, and plugins in automated environments.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — full pipeline, nodes, tools, memory, security, TUI, inference
- [Development Guide](docs/DEVELOPMENT.md) — tool registry, provider setup, test workflows
- [TUI Specification](docs/TUI_SPEC.md) — complete TUI system spec
- [Codebase Findings](docs/CODEBASE_FINDINGS.md) — audit scan results and fix status (all 46 findings resolved)
- [Implementation Plan](docs/IMPLEMENTATION_PLAN.md) — per-finding fix tables for all audit cycles
- [System Map](docs/system_map.md) — generated file tree
