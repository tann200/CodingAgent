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

`init` creates `.codingAgent/` with agent configuration, hooks, and a project instruction file. Use `codingagent --help` for the complete command surface. Key commands include `init`, `system-prompt`, `session`, `status`, `mcp`, and `diff`.

## Safety Model

Normal agent tool calls go through a centralized execution pipeline that applies tool normalization, permission checks, read-before-write rules, workspace checks, approval handling, validation, snapshots, hooks, and telemetry. Use the least permissive mode that satisfies the task:

```bash
codingagent --task "Inspect this repository" --permission-mode read_only
codingagent --task "Update one test" --permission-mode workspace_write
codingagent --task "Plan an upgrade" --dry-run
```

`--autonomous` does not approve gated tools unless their canonical names are explicitly passed with `--autonomous-approve`. Do not use `--autonomous-approve '*'` unless you accept every gated action.

Shell containment is not currently a reliable security boundary on hosts without an enforcing sandbox backend. Treat shell-capable and server-enabled deployments as trusted, local use only until the high-severity audit findings are resolved.

## Test And Quality Commands

```bash
python -m pytest tests/unit -q -p no:logging
python -m pytest tests/integration/test_langgraph_orchestrator.py tests/integration/test_fast_path_graph_e2e.py -q -p no:logging
python -m ruff check src tests --select=E,F,W --ignore=E501
python -m mypy src/server src/core/inference/provider_utils.py src/core/orchestration/tool_contracts.py src/core/orchestration/tool_parser.py --ignore-missing-imports --follow-imports=silent --disable-error-code=import-untyped --no-error-summary
```

The CI workflow defines the maintained quality, mock integration, mock E2E, platform smoke, benchmark, and optional live-provider jobs. Full test collection requires the `server` extras because server tests import FastAPI and Uvicorn.

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

- The non-lite live graph is an LLM-directed frontier loop; the full plan/step/debug/delegation graph is present but disabled.
- A text-only model response can currently be treated as completion without objective repository verification.
- Code retrieval is symbol-oriented and is not guaranteed before every planning or editing path.
- HTTP task execution, when enabled, must remain loopback-bound and trusted; it is not a multi-tenant service boundary.

See the audit for evidence, severity, and the prioritized remediation roadmap.
