# CodingAgent

CodingAgent is a local Python coding agent with a Textual terminal UI and a headless CLI. It runs LLM-backed coding tasks against a selected working directory, exposing file, patch, shell, repository, LSP, planning, memory, and verification tools through an orchestration layer.

This document is generated from the live codebase. For architecture, see [ARCHITECTURE.md](ARCHITECTURE.md). For the current engineering assessment, see [docs/audit/2026-10-09-engineering-audit.md](docs/audit/2026-10-09-engineering-audit.md).

## Requirements

- Python 3.11 or later
- A configured model provider. Bundled provider defaults live in `src/config/providers.json` (including local Ollama, LM Studio, MLX, `llama_server`, plus cloud adapters for Groq [`qwen/qwen3.8-27b`, `openai/gpt-oss-20b`], Google Gemini via AI Studio OpenAI-compatible API [`gemini-2.5-flash`, `gemini-2.5-pro`], Anthropic [`claude-sonnet-4-6`, `claude-opus-4-6`], GitHub Copilot, and LiteLLM). API keys, active provider/model choices, and optional `policy_tier` / `model_policy_tiers` overrides are stored strictly outside the repository in user preferences (`~/.coding_agent/prefs.json`, `0o600`) or environment variables (`GROQ_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`).
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

`init` creates `.codingAgent/` with agent configuration, hooks, and a project instruction file. Use `codingagent --help` for the complete command surface. Key commands include `init`, `system-prompt`, `session`, `status`, `doctor`, `local-launch`, `context-report`, `bench-diff`, `skill`, `mcp`, and `diff`.

Diagnose local model engines (Ollama, LM Studio, MLX, llama-server, vLLM, LiteLLM), inspect zero-config local runtime auto-tuning (`n_ctx`, KV quant, RAM headroom, recommended flat toolset), size local `.gguf` files safely (`local-launch` uses a pure-stdlib DoS-bounded GGUF v2/v3 header reader with `O_NOFOLLOW` + regular-file verification), and validate native tool schemas (automatically probing the live discovered local model with a cold-load-safe 30s timeout):

```bash
codingagent doctor
codingagent doctor --json
codingagent doctor --provider ollama --validate-tools
codingagent local-launch /path/to/model.gguf --script
codingagent local-launch /path/to/model.gguf --json --memory-gb 16
codingagent context-report --tier compact-flat
codingagent bench-diff baseline.json candidate.json --threshold 0.02
```

List, inspect, and export progressive-disclosure `SKILL.md` skills (discovered in precedence order `.codingagent/skills/` > `.agents/skills/` > `.claude/skills/` > `~/.codingagent/skills/` > bundled; single-agent playbooks loaded on demand via `load_skill` over the 3 canonical flat toolsets `compact-flat` / `standard-flat` / `frontier-flat`, with opt-in `delegate_task(mode="scout"|"worktree")` excluded from `compact-flat`) for Agensi, Claude Code, and Cursor:

```bash
codingagent skill list
codingagent skill export polyglot-verify --target claude
codingagent skill export --all --target agensi
```

In the interactive Textual TUI (`codingagent`), the prompt opens directly for input (use `Ctrl+P` / `Ctrl+O` for the native Command Palette, `Ctrl+B` to toggle the responsive sidebar — auto-hidden below 110 columns — `Shift+Tab` to toggle Plan Mode, and `Ctrl+R` or `/sessions` to browse past sessions). Use slash commands in the prompt:
- `/status` — Inspect active provider, model, `ModelRuntimePolicy` tier, flat toolset, verification mode, parallel read limit, and character output ceiling.
- `/doctor` — Run local model reachability, latency, context estimation, and tool schema diagnosis directly inside chat.
- `/skills` or `/skills <name>` — List discovered workspace/user/bundled skills or inspect skill instructions; type `/<skill-name> <request>` to invoke a skill directly.
- `/theme [name]` — List or switch between all built-in Textual dark & light themes (persisted in TUI settings).
- `/advisor [status|model <name|off>|handoff on|off|reset]` — Configure the opt-in budgeted strategic advisor (`ask_advisor`, dormant by default, hard-capped at `max_uses=3` per session, with optional human handoff).
- `/btw <question>` — Run a parallel read-only side question (`read_file`, `grep`, `list_files`, `find_files`) over a frozen context snapshot without mutating main conversation history.
- `/queue [ls|pop|clear|<prompt>]` — Inspect, edit, or enqueue plain-text follow-up prompts replayed sequentially once idle.
- `/reasoning [off|low|medium|high|unset|status]` (aliases `/effort`, `/thinking`) — Control portable reasoning effort / thinking-token budgets across local (`llama-server`, Ollama) and cloud providers (also configurable headlessly via `--reasoning`).
- `/compact` — Trigger deterministic compaction of conversation context.

Headless and CI automation flags include:

```bash
# Live sanitized NDJSON lifecycle stream terminated by authoritative {type:"result", status, result, usage} (exit 0 only on complete)
codingagent --task "Run tests and fix failures" --output stream-json
# Ephemeral private session: zero SQLite, memory, or trace writes to .codingAgent/ while allowing workspace edits
codingagent --task "Refactor parser" --private
# Isolated timestamped git worktree with local squash-merge on exit (never auto-pushes; interactive conflict prompt)
codingagent --task "Add feature" --parallel
```

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

- The live execution engine uses a native async 6-stage `AgentRunner` (`initialize -> agent_loop -> [approval | verify | compact | finalize] -> END`) across all model profiles (`nano` `<8.5B` and `compact` `9-14B` with 10 flat tools, `standard` `15-70B` dense/MoE with 14 flat tools, and `frontier` with 18 flat tools); read-only `git_read` and `lsp` are dynamically omitted outside `.git` workspaces or without an installed LSP server, `check_background_task` and `read_tool_output` are omitted until a background task or evicted output exists in the session, and `frontier-flat` exposes `load_tool_group(group="git-write|memory|ast")` for on-demand specialized tools. `ModelRuntimePolicy` scales budgets, read-only wave parallelism, content-type tool output reduction (with on-demand `read_tool_output(handle)` inspection and Context-Aware Read Guard previews on `read_file`), and targeted vs. full verification rather than diverging pipeline topology.
- Session persistence uses a single canonical SQLite `SessionStore` (WAL + FTS5) with portable redacted JSONL export/import (`jsonl_io.py`), transactional file rollback via `RollbackManager`, on-demand `TurnGroup`-atomic compaction (`CompactionEngine`), and strictly opt-in `VectorStore` semantic embeddings (`CODINGAGENT_ENABLE_VECTOR_STORE=1`).
- Mutating task completion is governed by evidence-based completion contracts, polyglot verification with Deferred Rollback during bounded retries, and a fail-closed Transaction Settlement Guard in `finalize` that rolls back unverified snapshots on failure or cancellation.
- Code retrieval is symbol-oriented and is not guaranteed before every planning or editing path.
- HTTP task execution, when enabled, must remain loopback-bound and restricted to approved workspace roots; it is not a multi-tenant service boundary.

See the audit for evidence, severity, and the prioritized remediation roadmap.
