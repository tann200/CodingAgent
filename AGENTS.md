# CodingAgent Contributor Guide

## Source Of Truth

- Treat the live implementation and tests as authoritative.
- `docs/audit/audit-instructions.md` defines the audit scope and required report structure.
- `README.md` and `ARCHITECTURE.md` describe the current implementation and must be updated when behavior, commands, boundaries, or safety guarantees change.
- Do not restore deleted historical documentation without validating every claim against current code.

## Environment

- Python 3.11 or newer is required.
- Install development and server dependencies with `python -m pip install -e '.[dev,server]'`.
- The installed command is `codingagent` and maps to `src.main:main`.
- Use `python -m pytest`, `python -m ruff`, and `python -m mypy` so the active environment is explicit.
- `pyproject.toml` is the dependency source of truth. `requirements.txt` supports a legacy startup script and may differ.

## Development Commands

```bash
python -m pytest tests/unit -q -p no:logging
python -m pytest tests/integration/test_langgraph_orchestrator.py tests/integration/test_fast_path_graph_e2e.py -q -p no:logging
python -m ruff check src tests --select=E,F,W --ignore=E501
python -m mypy src/server src/core/inference/provider_utils.py src/core/orchestration/tool_contracts.py src/core/orchestration/tool_parser.py --ignore-missing-imports --follow-imports=silent --disable-error-code=import-untyped --no-error-summary
```

Run focused tests for every changed module. Do not require a live provider for ordinary tests. Mark or isolate network and provider-dependent tests.

## Architecture Rules

- Keep `src/main.py` as the dispatch boundary. Keep Textual UI code under `tui/` and do not import UI modules into core orchestration.
- Model-controlled calls must go through `Orchestrator.execute_tool()` and `tool_execution_pipeline.py`. Do not call registered tool functions or `ToolRegistry.call()` from a new agent integration.
- New mutating tools require explicit permission metadata, workspace confinement, read-before-write behavior where applicable, input validation, audit/trace events, and rollback behavior or an explicit non-revertible contract.
- New shell paths must use the shared bash security and sandbox facilities. Do not add raw `subprocess` execution for model-generated input.
- Preserve cancellation, timeout, token-budget, and loop-limit behavior in graph changes.
- Use typed event definitions in `src/core/messaging/` for cross-component communication rather than direct UI or server coupling.
- Treat provider responses, repository content, memory, web/MCP content, and hook output as untrusted input.

## Security Rules

- Never commit or log API keys, OAuth tokens, passwords, credentials, private keys, or raw sensitive tool output.
- Do not put secrets into prompts, session stores, execution traces, telemetry, exports, or HTTP responses.
- Fail closed when a permission, sandbox, validation, or workspace-boundary dependency cannot enforce its contract.
- Do not broaden server exposure without authentication, a workdir allowlist, bounded queues, and tests.
- Revalidate redirect targets for any network-fetch implementation; validate both initial and final destinations.

## Documentation Rules

- Update `README.md` when installation, commands, configuration, or operational safety changes.
- Update `ARCHITECTURE.md` when a runtime path, component boundary, graph selection, persistence mechanism, or trust boundary changes.
- Update `docs/audit/2026-10-09-engineering-audit.md` only when resolving or reassessing an audit item; include source evidence and tests.
- Keep documentation factual. State limitations rather than presenting unfinished or disabled systems as available capabilities.

## Current High-Risk Areas

- The full plan/step/debug graph is disabled; the live non-lite path uses a frontier loop.
- Shell sandboxing can fall back to unrestricted host execution when enforcement is unavailable.
- Completion can be inferred from model text without objective verification.
- Server task workdirs are not constrained to an explicit approved root.
- Read paths and traces require careful secret handling.

Any change in these areas needs focused regression coverage and a review of the audit report.
