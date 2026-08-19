# CodingAgent — Developer Guide

> **Test baseline:** 3537 unit tests passing  
> **Audit status:** 0 Critical, 0 High issues  
> **Last updated:** 2026-05-13

This guide is the authoritative technical reference for contributors and
integrators. For a quick-start setup see [`LOCAL_SETUP.md`](LOCAL_SETUP.md);
for high-level architecture see [`codingagent-architecture.md`](codingagent-architecture.md).

---

## Contents

1. [Project Overview](#1-project-overview)
2. [Repository Layout](#2-repository-layout)
3. [Orchestration Flow](#3-orchestration-flow)
4. [Agent Roles](#4-agent-roles)
5. [Subagent Delegation](#5-subagent-delegation)
6. [Tool System](#6-tool-system)
7. [Graph Nodes Reference](#7-graph-nodes-reference)
8. [AgentState Reference](#8-agentstate-reference)
9. [Configuration System](#9-configuration-system)
10. [Testing](#10-testing)
11. [CI Pipeline](#11-ci-pipeline)
12. [Conventions and Key Constants](#12-conventions-and-key-constants)

---

## 1. Project Overview

CodingAgent is a **local-first autonomous coding agent** built on
[LangGraph](https://github.com/langchain-ai/langgraph). It runs fully offline
with local models (LM Studio, Ollama) or against cloud providers (OpenAI,
Anthropic, GitHub Copilot, OpenRouter, Groq, LiteLLM).

**Supported surfaces:**

| Surface | How to start |
|---------|-------------|
| TUI | `python -m src.main` |
| CLI (headless) | `python -m src.main --task "..."` |
| HTTP / SSE / WebSocket | `CODING_AGENT_HTTP_SERVER=1 python -m src.main` |
| Scheduler (background) | imported by orchestrator; cron-style triggers |

---

## 2. Repository Layout

```
src/
├── main.py                        CLI / TUI entry point
├── core/
│   ├── orchestration/             Agent brain (72 files)
│   │   ├── orchestrator.py        Main Orchestrator class
│   │   ├── orchestrator_bootstrap.py  Init phases
│   │   ├── graph/
│   │   │   ├── builder.py         Compiles LangGraph graph
│   │   │   ├── state.py           AgentState TypedDict
│   │   │   ├── nodes/             Node implementations (30+ files)
│   │   │   ├── *_routing.py       Conditional edge functions
│   │   │   └── ...
│   │   ├── role_config.py         Canonical roles, aliases, SM-1 binding
│   │   ├── task_lifecycle.py      get_tools_for_role_impl
│   │   ├── project_settings.py    5-layer settings loader
│   │   ├── agent_brain.py         System-prompt compiler
│   │   ├── loop_guards.py         Doom-loop detection
│   │   ├── approval_gate.py       User-approval gating
│   │   └── ...
│   ├── context/                   Prompt / context assembly
│   ├── memory/                    Session store, distiller, compactor
│   ├── indexing/                  Incremental repo indexer, symbol graph
│   ├── inference/                 LLM adapters, model tiers, provider manager
│   ├── mcp/                       Model Context Protocol client + stdio server
│   ├── scheduler/                 Background task scheduler
│   └── telemetry/                 Tracer, metrics store
├── tools/
│   ├── _tool.py                   @tool decorator, PermissionKind
│   ├── _registry.py               Auto-discovery ToolRegistry
│   ├── subagent_tools.py          delegate_task, delegate_task_async
│   ├── subagent_payloads.py       Subagent initial-state builder
│   └── *.py                       ~20 feature tool modules
├── config/
│   ├── providers.json             Provider definitions
│   ├── toolsets/                  Role-to-tool YAML manifests
│   ├── agent-brain/               System-prompt markdown files
│   │   ├── identity/              LAWS.md, SOUL.md
│   │   ├── roles/                 Per-role .md files
│   │   └── skills/                Skill .md files
│   └── formatters.yaml            Tool output formatters
├── server/                        FastAPI HTTP server
└── tui/                           Textual TUI components
tests/
├── unit/                          ~3537 tests, no LLM required
├── integration/                   Mock + live-provider tests
├── e2e/                           End-to-end scenario tests
├── benchmarks/                    7 benchmark tests
└── acceptance/                    Acceptance-level tests
docs/                              Architecture, audit reports, guides
.codingAgent/                      Per-workspace runtime state
.agent/                            Project settings (settings.json)
```

---

## 3. Orchestration Flow

### Bootstrap sequence (`orchestrator_bootstrap.py`)

```
_init_infrastructure   → MessageManager, thread pools, snapshot manager
_init_providers        → provider manager, active LLM adapter
_init_event_subscriptions → EventBus wiring (TUI, server, telemetry)
_init_services         → token budget, plan mode, tool execution service,
                          MCP client, cost tracker, session store,
                          lifecycle managers
```

### Pipeline routes (three tiers)

```
NANO / trivial fast-path:
  perception → execution → verification → evaluation → memory_sync

SMALL / MEDIUM full pipeline:
  perception → analysis → [analyst_delegation] → planning
  → plan_validator → execution → step_controller
  → verification → evaluation → memory_sync

LARGE / FRONTIER frontier loop:
  perception → frontier_loop → verification → evaluation → memory_sync
```

### Node traversal (full pipeline)

| # | Node | Key action |
|---|------|-----------|
| 1 | `perception` | Parse user turn, select immediate action; routes to fast-path, full pipeline, or frontier loop |
| 2 | `analysis` | Gather repo context, relevant files, key symbols |
| 3 | `analyst_delegation` | (optional) Spawn analyst subagent(s) for deep research |
| 4 | `planning` | Build or revise a plan DAG with ordered steps |
| 5 | `plan_validator` | Validate plan structure; pause for user approval if `plan_mode_enabled` |
| 6 | `execution` | Execute a tool call; enforces read-before-write, write-queue locking |
| 7 | `step_controller` | Load next step; gate progression; route to verification when step done |
| 8 | `verification` | Run tests/linters; populate `verification_result` |
| 9 | `evaluation` | LLM-judge completion; route to memory_sync, debug, or step_controller |
| 10 | `debug` | Produce repair action after failure |
| 11 | `replan` | Reduce/restructure plan after failure or complexity mismatch |
| 12 | `memory_sync` | Persist state; determine delegation or finish |
| 13 | `delegation` | Fire-and-forget subagent delegation after memory sync |
| 14 | `wait_for_user` | Approval / preview / plan-mode pause |
| 15 | `frontier_loop` | Tight agentic loop for LARGE/FRONTIER models |

### Routing modules

| File | Covers |
|------|--------|
| `graph/perception_routing.py` | `route_after_perception` |
| `graph/analysis_routing.py` | `should_after_analysis` |
| `graph/planning_routing.py` | `should_after_plan_validator`, `should_after_step_controller` |
| `graph/execution_routing.py` | `route_execution`, `should_after_evaluation`, `should_after_debug`, `should_after_replan` |
| `graph/session_routing.py` | `route_after_wait_for_user`, `should_after_memory_sync` |
| `graph/tier_graph_routing.py` | PRSW detection, read/write role sets |

---

## 4. Agent Roles

Canonical roles and aliases are defined in
`src/core/orchestration/role_config.py`.

### Canonical roles

| Role | Description | Model binding (SM-1) |
|------|-------------|---------------------|
| `analyst` | Read-only codebase exploration | small model |
| `strategic` | Task decomposition and planning | small model |
| `operational` | Code implementation and edits | provider default |
| `reviewer` | Code review, tests, QA | small model |
| `debugger` | Root-cause analysis and targeted fixes | provider default |

### Role aliases

All aliases are normalised to their canonical equivalent by
`normalize_role(role)` before any downstream use.

| Alias(es) | Canonical |
|-----------|-----------|
| `planner`, `plan`, `planning` | `strategic` |
| `coder`, `developer`, `coding` | `operational` |
| `researcher`, `analysis` | `analyst` |
| `review`, `audit` | `reviewer` |
| `debug` | `debugger` |

### Adding a custom role

1. Create `src/config/agent-brain/roles/<role>.md` with the system-prompt
   content for that role.
2. Add the canonical name to `CANONICAL_ROLES` in `role_config.py`.
3. Add an entry to `CANONICAL_ROLE_CONFIGS` mapping the name to a
   `ROLE_CONFIGS` entry.
4. Add the role to the appropriate set in `delegation_node.py`:
   `READ_ONLY_ROLES` (read-only) or `WRITE_ROLES` (can write files).

---

## 5. Subagent Delegation

**Implementation:** `src/tools/subagent_tools.py`

### `delegate_task`

```python
delegate_task(
    role: str,                    # canonical name or alias
    subtask_description: str,     # detailed instructions
    working_dir: str | None,      # defaults to current dir
    allowed_tools: list | None,   # explicit tool allowlist; overrides role default
    model: str | None,            # overrides SM-1 role binding
    task_id: str | None,          # resume prior subagent session
) -> str
```

Full lifecycle:

1. Validate and canonicalize `role`
2. Check `_DELEGATION_DEPTH_VAR` ≥ `_MAX_DELEGATION_DEPTH` → refuse
3. Compile system prompt via `AgentBrainManager`
4. Resolve tool policy (`allowed_tools` > registry > role defaults)
5. Resolve model via SM-1 binding
6. Build `initial_state` via `build_subagent_initial_state()`
7. Write subagent manifest to `.codingAgent/subagent_manifests/` before spawning
8. Run compiled LangGraph graph in a `ThreadPoolExecutor` (300 s timeout)
9. Update manifest, persist child session, roll up cost, publish
   `delegation.finish` event

### `delegate_task_async`

```python
delegate_task_async(
    role, subtask_description, working_dir, allowed_tools, model
) -> Coroutine[str]
```

Async wrapper that runs `delegate_task` in a thread pool via
`concurrent.futures.ThreadPoolExecutor`. `ContextVar` values are propagated
into the child thread via `copy_context()`.

### Depth limiting

| Mechanism | Location |
|-----------|----------|
| `_DELEGATION_DEPTH_VAR` (ContextVar) | `subagent_tools.py:78` — authoritative |
| `_MAX_DELEGATION_DEPTH = 3` | `subagent_tools.py:79` |
| Secondary check via `state["delegation_depth"]` | `delegation_node.py` — cross-session fallback only |
| Depth guard in `analyst_delegation_node` | checked before spawning parallel/single analysts |

When depth ≥ 3, the call returns an error string immediately without spawning
a new subagent.

### Stale signal isolation (FAULT-08)

The following fields are **not** copied from parent to child subagent state
even when resuming a prior session, to prevent workflow-phase signals from
bleeding across the boundary:

- `plan_mode_approved`
- `plan_validation`
- `verification_result`
- `evaluation_result`

---

## 6. Tool System

### `@tool` decorator

```python
from src.tools import tool
from src.tools._tool import PermissionKind

@tool(
    side_effects=["write"],                # "read", "write", "execute", or []
    tags=["coding"],                       # used by toolset YAML filtering
    permission_kind=PermissionKind.WRITE_FILE,
)
def my_tool(param: str) -> dict:
    """Description shown to the LLM as the function schema."""
    return {"ok": True}
```

**`PermissionKind` values:**
`READ_FILE`, `WRITE_FILE`, `EXECUTE_BASH`, `NETWORK`, `GIT_READ`,
`GIT_WRITE`, `DELEGATE`, `LSP_READ`, `LSP_WRITE`, `PLAN`, `NONE`

### Auto-discovery

`src/tools/_registry.py` holds `_BUILTIN_MODULES` — the list of all built-in
tool module names. On `build_registry()` the registry imports each module and
collects functions marked with the `TOOL_ATTR` sentinel.

When adding a tool in a **new** module, add the module name to
`_BUILTIN_MODULES`. For tools in existing modules no change is required.

### Toolset YAMLs (`src/config/toolsets/`)

Each YAML lists the exact tool names available to a role. Five files map to
the five canonical roles:

| File | Role | Character |
|------|------|-----------|
| `coding.yaml` | `operational` | Full set ~50 tools: read/write/edit/delete/patch/LSP/git/bash/web/tests |
| `analysis.yaml` | `analyst` | Read-only: file reads, search, git-read, web, memory |
| `planning.yaml` | `strategic` | Read + delegation + state/checkpoint + web + memory |
| `review.yaml` | `reviewer` | Read + test/lint/diff + git-read + web |
| `debug.yaml` | `debugger` | Read + limited edit + tests/lint/LSP + bash + git + snapshots |

#### Adding a tool to a toolset

Add the tool's function name to the appropriate `src/config/toolsets/*.yaml`.
The unit test `tests/unit/test_toolset_coverage.py` asserts that every
registered non-test tool appears in at least one YAML — CI will fail if you
add a tool without updating a YAML.

### Toolset loader (`src/config/toolsets/loader.py`)

| Function | Purpose |
|----------|---------|
| `load_toolset(name)` | Load YAML by name; results cached in `_cache` |
| `get_toolset_for_role(role)` | Map role name (with synonyms) to toolset name |
| `get_tools_for_role(role)` | Convenience: returns `list[str]` of tool names |
| `load_toolset_for_model(name, model)` | Model-aware loading: small models get YAML, big models get JSON; separate `_format_cache` |
| `clear_cache()` | Invalidate both caches |

### Tool execution pipeline

```
LLM output
  → tool_parser.py        extract tool name + args
  → tool_preflight.py     pre-flight checks (schema, sandbox level)
  → approval_gate.py      user approval if needed
  → loop_guards.py        doom-loop detection, cooldown, diff gate
  → FileLockManager       PRSW read/write lock acquisition
  → ToolExecutionService  registry.call()
  → tool_result_formatter.py
  → history + last_result written to AgentState
```

### Model tiers and tool counts

| Tier | Parameter scale | Tools | Max turns |
|------|-----------------|-------|-----------|
| NANO | ≤ 7B | 8 | 15 |
| SMALL | 7–14B | 20 | 25 |
| MEDIUM | 14–70B | 35 | 40 |
| LARGE | > 70B | 50 | 60 |
| FRONTIER | Cloud | 60 | 80 |

---

## 7. Graph Nodes Reference

All files in `src/core/orchestration/graph/nodes/`.

| File | Primary function | Notes |
|------|-----------------|-------|
| `perception_node.py` | `perception_node` | Entry point for every turn; selects action; **hard line limit: 1020** |
| `perception_runtime.py` | `_select_perception_role` | 5-role mapping; `load_toolset_for_model` |
| `perception_messages.py` | message assembly helpers | |
| `perception_parsing.py` | tool call extraction | |
| `perception_post_call.py` | post-call state updates | |
| `perception_result.py` | structures inference result | |
| `perception_retrieval.py` | context snippet retrieval | |
| `perception_no_tool.py` | no-tool response handler | |
| `perception_compaction.py` | history compaction trigger | |
| `analysis_node.py` | `analysis_node` | Repo context, relevant files, symbols |
| `analyst_delegation_node.py` | `analyst_delegation_node` | Parallel (FRONTIER/LARGE) or single analyst; depth-guarded |
| `planning_node.py` | `planning_node` | Build/revise plan DAG |
| `planning_fast_paths.py` | fast-path bypasses | |
| `planning_helpers.py` | prompt assembly, step generation | |
| `planning_prompt.py` | prompt templates | |
| `planning_result.py` | parse/validate planning output | |
| `plan_validator_node.py` | `plan_validator_node` | Validate plan; route to `wait_for_user` if approval needed |
| `execution_node.py` | `execution_node` | Execute tool call; native JSON + text-format support |
| `execution_guards.py` | pre-execution safety checks | read-before-write enforcement |
| `execution_helpers.py` | tool invocation helpers | syntax gate, step iteration |
| `step_controller_node.py` | `step_controller_node` | Load next plan step; gate progression |
| `verification_node.py` | `verification_node` | Tests/linters; writes `verification_result` |
| `evaluation_node.py` | `evaluation_node` | LLM-judge; writes `evaluation_result` |
| `debug_node.py` | `debug_node` | Repair action after failure |
| `replan_node.py` | `replan_node` | Reduce/restructure plan |
| `memory_update_node.py` | `memory_update_node` | Persist state; decide delegation or finish |
| `delegation_node.py` | `delegation_node` | Terminal subagent fire-and-forget |
| `frontier_loop_node.py` | `frontier_loop_node` | Tight loop for LARGE/FRONTIER |
| `wait_for_user_node.py` | `wait_for_user_node` | Human approval/preview pause |
| `node_utils.py` | `_resolve_orchestrator` + helpers | Shared utilities |
| `tool_output_truncation.py` | output truncation | Caps oversized tool output before history append |

---

## 8. AgentState Reference

Defined as a flat `TypedDict` in `src/core/orchestration/graph/state.py`.

### Section overview

| Section | Representative fields |
|---------|----------------------|
| Core task | `task`, `original_task`, `working_dir`, `session_id`, `turn_count`, `max_turns`, `model_tier`, `agent_mode` |
| Conversation history | `history` *(merge reducer)*, `verified_reads`, `task_history`, `recent_tool_calls`, `errors` |
| Plan & step | `current_plan`, `current_step`, `step_description`, `planned_action`, `plan_dag`, `execution_waves`, `plan_attempts`, `replan_attempts`, `task_complexity`, `step_retry_counts` |
| Plan approval/preview | `plan_validation`, `plan_mode_enabled`, `plan_mode_approved`, `awaiting_plan_approval`, `preview_mode_enabled`, `preview_confirmed`, `pending_preview_id` |
| Tool execution | `next_action`, `last_result`, `last_tool_name`, `action_failed`, `tool_call_count`, `files_read`, `snapshots` |
| Debug & recovery | `debug_attempts`, `max_debug_attempts`, `total_recovery_attempts`, `last_error_code`, `needs_clarification` |
| Verification | `verification_passed`, `verification_result`, `evaluation_result`, `evaluation_llm_verdict`, `evaluation_llm_reason` |
| Analysis & context | `analysis_summary`, `relevant_files`, `key_symbols`, `analyst_findings`, `repo_summary_data`, `call_graph`, `test_map` |
| Delegation | `delegation_results`, `delegations`, `delegation_depth` |
| Memory | `_should_distill`, `_force_compact`, `_compacted_history`, `last_compact_at` |
| Cost & telemetry | `session_cost_usd` |

### List reducer

`history` and `verified_reads` use a custom `merge_or_replace_list` reducer.
Wrap a list in `ReplaceList` (via `replace_state_list()`) to trigger a full
replacement rather than an append — used by compaction paths.

### Validation

`validate_state(state)` runs at node entry. It checks numeric field types,
`current_step` bounds, and `turn_count <= max_turns`. It returns a list of
issue strings and logs warnings; it never raises.

---

## 9. Configuration System

### Provider configuration (`src/config/providers.json`)

```json
[{
  "name": "lm_studio",
  "type": "lm_studio",
  "base_url": "http://localhost:1234/v1",
  "models": ["gemma-4-e4b-it"],
  "active": true
}]
```

Supported `type` values: `lm_studio`, `ollama`, `openrouter`, `openai`,
`anthropic`, `github_copilot`, `groq`, `litellm`

### Project settings (`src/core/orchestration/project_settings.py`)

5-layer merge (CP-13/CP-8). Files loaded in order (later wins on scalar
conflicts; nested dicts are deep-merged):

1. `.codingAgent/settings.json` — committed project settings
2. `.codingAgent/settings.local.json` — local overrides (gitignored)

Never raises — missing or malformed files yield `ProjectSettings` defaults.

**Recognised keys:**

| JSON key | Python field | Default | Purpose |
|----------|-------------|---------|---------|
| `model` | `model` | `None` | LLM override for this project |
| `permissionMode` | `permission_mode` | `"prompt"` | `read_only` / `workspace_write` / `danger_full_access` / `prompt` / `allow` |
| `maxTurns` | `max_turns` | `80` | Per-session turn limit |
| `budgetCeiling` | `budget_ceiling_usd` | `None` | Max session spend (USD) |
| `hooks` | `hooks` | `{}` | `PreToolUse`/`PostToolUse` shell hook commands |
| `mcpServers` | `mcp_servers` | `{}` | MCP server definitions |
| `enableSemanticEvaluation` | `enable_semantic_evaluation` | `True` | Toggle LLM semantic judge (WF-2) |
| `maxLlmWaitSeconds` | `max_llm_wait_seconds` | `120` | Hard LLM call timeout in planning_node |

`watch_project_settings(workdir, callback)` polls every 2 s and reloads on
mtime change.

### Role configuration (`src/core/orchestration/role_config.py`)

Key exports:

| Symbol | Type | Purpose |
|--------|------|---------|
| `CANONICAL_ROLES` | `list[str]` | `["analyst", "strategic", "operational", "reviewer", "debugger"]` |
| `ROLE_ALIASES` | `dict` | Alias → canonical mapping |
| `CANONICAL_ROLE_CONFIGS` | `dict` | Canonical name → `ROLE_CONFIGS` entry |
| `normalize_role(role)` | `str` | Normalise any name; default `"operational"` |
| `get_default_model_for_role(role)` | `str \| None` | SM-1 binding; `None` → provider default |
| `RoleManager` | class | Stateful role tracker with history |

### Agent brain (`src/config/agent-brain/`)

```
agent-brain/
  identity/
    LAWS.md     behavioral laws
    SOUL.md     core identity and values
  roles/
    analyst.md, strategic.md, operational.md, reviewer.md, debugger.md
    operational-frontier.md, operational-small.md, operational-gemma4.md
    tester.md, scout.md, researcher.md
  skills/
    code_review.md, debug_checklist.md, refactor.md, write_tests.md,
    security_review.md, dry.md, context_hygiene.md, stuck.md
```

`AgentBrainManager.compile_system_prompt(role)` concatenates identity +
role-specific markdown.

---

## 10. Testing

### Test categories

| Directory | LLM required | CI job |
|-----------|-------------|--------|
| `tests/unit/` | No | `unit-tests` (always) |
| `tests/integration/` | Mock: no / Live: yes | `integration-mock` (always); `live-provider-checks` (schedule/dispatch) |
| `tests/e2e/` | Mock scenarios: no | `e2e-mock` (always) |
| `tests/benchmarks/` | No | `benchmarks` (main/schedule/dispatch) |
| `tests/acceptance/` | Depends | Not in CI by default |

### Running tests

```bash
# Baseline — all unit tests
pytest tests/unit -q --no-header

# With coverage
pytest tests/unit -q --cov=src --cov-report=term-missing

# Specific file
pytest tests/unit/test_tools_file_io.py -v

# Integration (mock-backed, no provider needed)
pytest tests/integration/test_mock_adapter_integration.py \
       tests/integration/test_delegation_mock.py -q

# Integration (live provider)
RUN_INTEGRATION=1 pytest tests/integration -q

# E2E
pytest tests/e2e/test_agent_scenarios.py tests/e2e/test_basic_workflows.py -q

# Benchmarks
pytest tests/benchmarks -v
```

### xfail policy

CI enforces a maximum of **10** combined `xfail` + `xpass` results in the
`unit-tests` job. Exceeding the threshold fails CI. Keep `xfail` markers
temporary and tracked; never use them as a permanent mask for real failures.

### Module size enforcement

`tests/unit/test_module_sizes.py` enforces per-file line limits. Currently:

| File | Limit |
|------|-------|
| `perception_node.py` | 1020 lines |

Adding lines beyond the limit fails CI. Split the file or extract helpers if
the limit is approached.

---

## 11. CI Pipeline

Defined in `.github/workflows/ci.yml`.

**Triggers:** push/PR to `main`; nightly schedule (`0 2 * * *`); manual
dispatch with optional `run_integration` flag.

| Job | Platform | Always runs? | Depends on | What it does |
|-----|----------|--------------|-----------|-------------|
| `quality` | ubuntu | yes | — | `requires-python` check; ruff lint (`E,F,W`); mypy on selected modules |
| `unit-tests` | ubuntu | yes | — | `pytest tests/unit -q --cov=src`; xfail count check; uploads `coverage.xml` |
| `platform-smoke` | ubuntu + macOS | yes | — | `test_sandbox.py`, `test_shell_hooks.py`, `test_task_lifecycle.py` on both platforms |
| `integration-mock` | ubuntu | yes | `unit-tests` | Mock-backed integration tests |
| `e2e-mock` | ubuntu | yes | `unit-tests` | Mock-scenario E2E tests |
| `benchmarks` | ubuntu | main/schedule/dispatch only | `unit-tests` | `pytest tests/benchmarks` |
| `live-provider-checks` | ubuntu | schedule/dispatch only | `unit-tests` | Live-provider integration + E2E CRUD tests |

`CI=true` is set globally by GitHub Actions and affects test behaviour
(auto-trusted mode, sandbox level).

---

## 12. Conventions and Key Constants

### Code style

| Rule | Value |
|------|-------|
| Line length | 88 characters (black + ruff) |
| Python target | 3.11 (`py311`) |
| Formatter | `black` |
| Linter | `ruff` — checks `E,F,W`; `E501` ignored in CI |
| Type checker | `mypy` (partial; `--ignore-missing-imports`) |

Per-file ruff ignores:

```toml
"tests/**" = ["E402", "F841", "E741", "E731", "F821", "E702", "F401"]
"scripts/**" = ["E402", "F841"]
"tui/**" = ["E741", "F841", "F401", "E402"]
```

### Key constants

| Constant | File | Value |
|----------|------|-------|
| `_MAX_DELEGATION_DEPTH` | `subagent_tools.py` | `3` — max subagent nesting |
| `_DELEGATION_DEPTH_VAR` | `subagent_tools.py` | ContextVar — authoritative depth counter |
| `DOOM_LOOP_THRESHOLD` | `loop_guards.py` | Max identical consecutive tool calls |
| `COOLDOWN_GAP` | `loop_guards.py` | Min turns between repeated tool calls |
| `_AUTONOMOUS_MAX_TOOL_CALLS` | `builder.py` | `100` — default tool call budget |
| `_TOOL_LIMITS` | `model_tiers.py` | Tools per tier (8/20/35/50/60) |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `CODINGAGENT_SANDBOX_LEVEL` | `off` / `workspace` / `full` (bubblewrap) |
| `CODINGAGENT_TRUSTED` | Skip approval prompts (used in CI) |
| `CODING_AGENT_HTTP_SERVER` | Start FastAPI HTTP server |
| `CODINGAGENT_LOG_LEVEL` | e.g. `DEBUG` |
| `RUN_INTEGRATION` | `1` — enable live-provider integration tests |
| `CI` | Set by GitHub Actions |

### Audit comment IDs

Source code uses short tracking IDs in comments to link back to audit reports
in `docs/audit/`. Common prefixes:

| Prefix | Area |
|--------|------|
| `CP-*` | Core pipeline changes |
| `HR-*` | Hardening / security fixes |
| `SM-*` | Model selection |
| `SPAWN-*` | Subagent spawning |
| `WF-*` | Workflow improvements |
| `PERF-*` | Performance |
| `GAP-*` | Capability gaps (frontier) |
| `FAULT-*` | Subagent fault fixes |
| `NEW-*` | New features |
| `F-*` | Individual bug fixes |

### Security model (5 layers)

1. **Pattern block** — `_BASE_DANGEROUS_PATTERNS` in `_security.py`
2. **Restricted commands** — require user approval
3. **Safe commands** — auto-allowed allowlist
4. **AST-level analysis** — `bash_security.py` inspects shell AST
5. **Sandbox** — `sandbox.py` with bubblewrap (`bwrap`) for full isolation

---

## See also

- [`DEVELOPMENT.md`](DEVELOPMENT.md) — quick-start setup and component recipes
- [`LOCAL_SETUP.md`](LOCAL_SETUP.md) — first-run provider setup
- [`TOOLSETS.md`](TOOLSETS.md) — toolset YAML specification
- [`SANDBOX.md`](SANDBOX.md) — bubblewrap sandbox details
- [`SCHEDULER.md`](SCHEDULER.md) — background scheduler
- [`codingagent-architecture.md`](codingagent-architecture.md) — architecture deep-dive
- [`docs/audit/`](audit/) — individual audit reports
