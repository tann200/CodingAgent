# CodingAgent

CodingAgent is a local Python coding agent with a Textual terminal UI and a headless CLI. It runs LLM-backed coding tasks against a selected working directory, exposing file, patch, shell, repository, LSP, planning, memory, and verification tools through an orchestration layer.

This document is generated from the live codebase. For architecture, see [ARCHITECTURE.md](ARCHITECTURE.md). For the current engineering assessment, see [docs/audit/2026-10-09-engineering-audit.md](docs/audit/2026-10-09-engineering-audit.md).

## Requirements

- Python 3.11 or later
- A configured model provider. Provider configuration is loaded from `src/config/providers.json` and user preferences.
- Optional server dependencies for HTTP/SSE/WebSocket support: `pip install -e '.[server]'`

Install the application and development dependencies:

```bash
python -m pip install -e '.[dev,server]'
```

The project metadata is authoritative for dependencies. `requirements.txt` exists for the legacy startup script and is not guaranteed to match the package constraints.

## Run

Launch the terminal UI:

```bash
codingagent
```

Run one task without the UI:

```bash
codingagent --task "Explain the current repository structure" --workdir /path/to/repository
```

Start a project-local agent workspace:

```bash
codingagent init --dir /path/to/repository
```

`init` creates `.codingAgent/` with agent configuration, hooks, and a project instruction file. Use `codingagent --help` for the complete command surface. Key commands include `init`, `system-prompt`, `session`, `status`, `doctor`, `context-report`, `bench-diff`, `skill`, `mcp`, and `diff`.

Diagnose local model engines (Ollama, LM Studio, MLX, llama-server, vLLM, LiteLLM), inspect zero-config local runtime auto-tuning (`n_ctx`, KV quant, RAM headroom, recommended flat toolset), and validate native tool schemas (automatically probing the live discovered local model with a cold-load-safe 30s timeout):

```bash
codingagent doctor
codingagent doctor --json
codingagent doctor --provider ollama --validate-tools
codingagent context-report --tier compact-flat
codingagent bench-diff baseline.json candidate.json --threshold 0.02
```

List, inspect, and export progressive-disclosure `SKILL.md` skills (discovered in precedence order `.codingagent/skills/` > `.agents/skills/` > `.claude/skills/` > `~/.codingagent/skills/` > bundled) for Agensi, Claude Code, and Cursor:

```bash
codingagent skill list
codingagent skill export polyglot-verify --target claude
codingagent skill export --all --target agensi
```

In the interactive Textual TUI (`codingagent`), the prompt opens directly for input (use `Ctrl+R` or `/sessions` to browse past sessions, or set `show_sessions_on_startup: true` in TUI settings). Use slash commands in the prompt:
- `/status` — Inspect active provider, model, `ModelRuntimePolicy` tier, flat toolset, verification mode, parallel read limit, and character output ceiling.
- `/doctor` — Run local model reachability, latency, context estimation, and tool schema diagnosis directly inside chat.
- `/skills` or `/skills <name>` — List discovered workspace/user/bundled skills or inspect skill instructions; type `/<skill-name> <request>` to invoke a skill directly.
- `/reasoning [off|low|medium|high|unset|status]` (aliases `/effort`, `/thinking`) — Control portable reasoning effort / thinking-token budgets across local (`llama-server`, Ollama) and cloud providers (also configurable headlessly via `--reasoning`).
- `/compact` — Trigger deterministic compaction of conversation context.

Project instructions are loaded from ancestor `AGENTS.md` / `CLAUDE.md` files plus root `ARCHITECTURE.md` (capped at 3,000 chars), and nested subtree `AGENTS.md` / `CLAUDE.md` files are loaded lazily (deduplicated by SHA-256 and wrapped in nonce-fenced `<project_instructions>` envelopes) when reading or editing files beneath the workspace root.

## Safety Model

Normal agent tool calls go through a centralized execution pipeline that applies tool normalization, permission checks, read-before-write rules, workspace checks, approval handling, validation, snapshots, hooks, and telemetry. Tool output strings are dynamically capped using tier-adaptive truncation scaled to the active `ModelRuntimePolicy` (nano: 6,000 chars; compact: 12,000 chars; standard: 24,000 chars; frontier: 48,000 chars). Use the least permissive mode that satisfies the task:

```bash
codingagent --task "Inspect this repository" --permission-mode read_only
codingagent --task "Update one test" --permission-mode workspace_write
codingagent --task "Plan an upgrade" --dry-run
```

`--autonomous` does not approve gated tools unless their canonical names are explicitly passed with `--autonomous-approve`. Do not use `--autonomous-approve '*'` unless you accept every gated action.

Shell containment fails closed by default when an enforcing sandbox backend is unavailable (or can be configured via `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1`). Inside enforcing sandboxes (`bwrap` / macOS `sandbox-exec`), host credential directories (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`, `$XDG_CONFIG_HOME/{gh,gcloud,op,sops/age}`) are masked by default; pass `--sandbox-expose <path>` (repeatable) to expose a specific path read-only when required. File reads/writes and `grep` enforce lexical + realpath checks blocking secret files (while keeping `.env.example`/`.sample`/`.template` and `*.pub` readable), `.git` internals, dangling symlink writes, and multi-hardlink (`st_nlink > 1`) files. Treat server-enabled deployments as trusted local use only.

## Test And Quality Commands

```bash
python -m pytest tests/unit -q -p no:logging
python -m pytest tests/integration -q -p no:logging -m "not real_llm and not lmstudio and not ollama"
python -m pytest tests/acceptance -q -p no:logging
python -m ruff check src tests tui --select=E,F,W --ignore=E501
python -m mypy src tui/tui_src --ignore-missing-imports --follow-imports=silent --disable-error-code=import-untyped --no-error-summary
```

The CI workflow defines the maintained quality (including TUI single-source tree enforcement), mock integration, mock E2E, acceptance, platform smoke, benchmark, and optional live-provider jobs. Full test collection requires the `server` extras because server tests import FastAPI and Uvicorn.

## Repository Layout

- `src/main.py`: installed CLI entry point and TUI/headless dispatch.
- `src/core/`: orchestration, graph workflows, providers, memory, indexing, evaluation, telemetry, and configuration.
- `src/tools/`: registered tool implementations and local safety helpers.
- `src/server/`: optional FastAPI task, event-streaming, scheduler, and WebSocket interfaces.
- `tui/`: Textual UI and bridge to the core event bus.
- `src/config/`: providers, toolsets, roles, skills, formatters, and LSP configuration.
- `tests/`: unit, integration, E2E, benchmark, acceptance, and real-provider test suites.
- `docs/audit/`: audit instructions and the current source-derived audit.

## Current Limitations

- The live execution engine uses a native async 6-stage `AgentRunner` (`initialize -> agent_loop -> [approval | verify | compact | finalize] -> END`) across all model profiles (`nano` `<8.5B` and `compact` `9-14B` with 13 flat tools, `standard` `15-70B` dense/MoE with 19 flat tools, and `frontier` with 26 flat tools); `ModelRuntimePolicy` scales budgets, read-only wave parallelism, content-type tool output reduction (with on-demand `read_tool_output(handle)` inspection and Context-Aware Read Guard previews on `read_file`), and targeted vs. full verification rather than diverging pipeline topology.
- Session persistence uses a single canonical SQLite `SessionStore` (WAL + FTS5) with portable redacted JSONL export/import (`jsonl_io.py`), transactional file rollback via `RollbackManager`, on-demand `TurnGroup`-atomic compaction (`CompactionEngine`), and strictly opt-in `VectorStore` semantic embeddings (`CODINGAGENT_ENABLE_VECTOR_STORE=1`).
- Mutating task completion is governed by evidence-based completion contracts, polyglot verification with Deferred Rollback during bounded retries, and a fail-closed Transaction Settlement Guard in `finalize` that rolls back unverified snapshots on failure or cancellation.
- Code retrieval is symbol-oriented and is not guaranteed before every planning or editing path.
- HTTP task execution, when enabled, must remain loopback-bound and restricted to approved workspace roots; it is not a multi-tenant service boundary.

See the audit for evidence, severity, and the prioritized remediation roadmap.
