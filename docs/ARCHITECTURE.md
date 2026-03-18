# CodingAgent Architecture

> **Implementation Status**: Fully implemented — LangGraph 10-node pipeline, multi-file atomic rollback, advanced memory wired, repository intelligence, 493+ unit tests
> **Recent Updates**: Signal-based timeout fix (non-main-thread safety), multi-file edit atomicity (step transactions), ContextController wired, SkillLearner/SessionStore/plan validator wired, deterministic mode, telemetry layer

## Implementation Stages

| Stage | Status | Components |
|-------|--------|------------|
| Stage 1 - MVP Stabilization | ✅ Complete | Toolsets, Session Store |
| Stage 2 - Cognitive Agent | ✅ Complete | Symbol Graph, Sandbox, Self-Debug |
| Stage 3 - Advanced SWE | ✅ Complete | Trajectories, Dreams, Refactor, Review, Skills |
| Stage 4 - LangGraph Pipeline | ✅ Complete | Analysis, Replan, Evaluation nodes |
| Stage 5 - EventBus Dashboard | ✅ Complete | Real-time UI updates via events |
| Stage 6 - Security Hardening | ✅ Complete | Bash allowlist, sandbox fail-closed, symlink protection |
| Stage 7 - Fast-Path Routing | ✅ Complete | Conditional routing for simple tasks |
| Stage 8 - Core Stabilization | ✅ Complete | Context builder fix, robust plan parsing, WorkspaceGuard |
| Stage 9 - Incremental Indexing | ✅ Complete | SHA256 hash-based change detection, multi-language (15+) |
| Stage 10 - Repository Intelligence | ✅ Complete | ContextController wired, VectorStore fix, SymbolGraph enrichment |
| Stage 11 - Wiring Sprint | ✅ Complete | SkillLearner, SessionStore, plan validator defaults wired |
| Stage 12 - Multi-file Atomicity | ✅ Complete | Step transactions: begin/append/rollback via RollbackManager |
| Stage 13 - Deterministic Mode | ✅ Complete | temperature=0, seed param, ScenarioEvaluator for regression tests |
| Stage 14 - Test Coverage | ✅ Complete | 493+ unit tests across 16+ test files |
| Stage 15 - Thread Safety | ✅ Complete | Signal-based timeout guarded by main-thread check |

---

## Pipeline Overview

```
Fast-Path (simple 1-step task):
  perception → execution → verification → evaluation → (memory_sync | end)

Full Pipeline (complex multi-step task):
  perception → analysis → planning → execution → step_controller
            → verification → evaluation → (memory_sync | step_controller | end)

Patch-too-large path:
  execution → replan → step_controller → execution (smaller step)
```

**Conditional Routing (Fast-Path):**
`route_after_perception` checks if `next_action` is set after perception:
- Tool call ready → skip to `execution` (fast-path)
- No tool call → go through `analysis` + `planning` (full pipeline)

**Node Role Mapping:**

| Node | Role |
|------|------|
| `perception_node` | operational |
| `analysis_node` | analyst |
| `planning_node` | strategic |
| `execution_node` | operational |
| `debug_node` | debugger |
| `verification_node` | reviewer |
| `evaluation_node` | reviewer |
| `replan_node` | planner |

---

## High-Level Flow

1. **Entry Points**
   - `src/main.py` — delegates to `CodingAgentApp`; CLI entry point.
   - `src/ui/app.py` — `CodingAgentApp` wires EventBus, Orchestrator, and ProviderManager; chooses Textual vs headless mode.
   - `src/ui/textual_app_impl.py` — real Textual TUI implementation with sidebar, chat panel, and input box; thread-based agent dispatch.
   - `src/ui/textual_app.py` — minimal placeholder `TextualApp` shim; no-op in headless environments.

2. **Orchestrator** (`src/core/orchestration/orchestrator.py`)
   Central runtime. Handles:
   - LLM connection via `ProviderManager` (`llm_manager.py`).
   - System prompt bootstrap via `AgentBrainManager`.
   - Message history + token windowing via `MessageManager`.
   - Primary action loop (`run_agent_once`): compiles and invokes the LangGraph pipeline.
   - Tool execution via `execute_tool` with preflight, snapshots, signal-safe timeout, contract validation, and loop detection.
   - Role-based tool filtering; multi-file step transactions via `RollbackManager`.
   - **Thread-safe timeout**: `signal.SIGALRM` is only armed when executing in the main thread; skipped in `ThreadPoolExecutor` workers (the common TUI path).

3. **LangGraph Cognitive Pipeline** (`src/core/orchestration/graph/`)

   **Graph Builder** (`graph/builder.py`): compiles the LangGraph `StateGraph`, wires all nodes, defines conditional edges and routing functions. `compile_agent_graph()` is the entry point.

   **Graph State** (`graph/state.py`): `AgentState` TypedDict — the shared immutable state passed between all nodes.

   **Node files** (`graph/nodes/`):

   | File | Node | Description |
   |------|------|-------------|
   | `perception_node.py` | `perception_node` | Understands the user request, decomposes tasks, extracts tool calls from YAML. Uses `operational` role. Injects `context_hygiene` skill for debug/search tasks. |
   | `analysis_node.py` | `analysis_node` | Explores the repository before planning. Three phases: (1) VectorStore semantic search, (2) SymbolGraph call graph enrichment, (3) ContextController token budget enforcement. Runs `repo_summary()` at start. Uses `analyst` role. |
   | `planning_node.py` | `planning_node` | Converts perception outputs into a structured step-by-step plan. Cross-session plan persistence via `last_plan.json`. Uses `strategic` role. |
   | `execution_node.py` | `execution_node` | Executes plan steps. Calls `begin_step_transaction()`, dispatches tools, advances plan state. Intercepts `requires_split` flag for replan. Uses `operational` role; injects `dry` skill when >2 relevant files. |
   | `verification_node.py` | `verification_node` | Runs tests, linter, and syntax checks. On failure calls `rollback_step_transaction()` to atomically restore all files written in the step. Uses `reviewer` role. |
   | `evaluation_node.py` | `evaluation_node` | Post-verification review — decides if the goal is fully met. Routes to `memory_sync` (complete), `step_controller` (more work), or `end`. Uses `reviewer` role. |
   | `debug_node.py` | `debug_node` | Analyses verification failures and attempts fixes. Enforces max 3 retry limit; on exhaustion calls `rollback_manager.rollback()`. Uses `debugger` role. |
   | `replan_node.py` | `replan_node` | Splits oversized patches (>200 lines) into 2–3 smaller targeted steps. Uses `planner` role. |
   | `step_controller_node.py` | `step_controller_node` | Enforces single-step execution from the plan; gates next-step dispatch. |
   | `memory_update_node.py` | `memory_update_node` | Persists distilled context to `.agent-context/TASK_STATE.md`. |
   | `plan_validator_node.py` | `validate_plan()` | Standalone function validating a plan before execution: checks step count, file references, verification step (strict mode). |
   | `node_utils.py` | — | Shared utilities: `_resolve_orchestrator()` (robust config/state lookup), `_notify_provider_limit()` (UI event for provider errors). |
   | `workflow_nodes.py` | — | Re-export shim for backward compatibility — imports all nodes and re-exports them from one place. |

4. **Message & Token Manager** (`src/core/orchestration/message_manager.py`)
   Tracks conversation history. Auto-drops oldest non-system messages when the window exceeds `max_tokens` (sliding window). System prompt is always preserved.

5. **AgentBrainManager** (`src/core/orchestration/agent_brain.py`)
   In-memory cache for agent-brain configuration files. Key methods:
   - `get_identity(name)` — SOUL or LAWS content
   - `get_role(role_name)` — role prompt (strategic, operational, analyst, debugger, reviewer)
   - `get_skill(skill_name)` — skill content (dry, context_hygiene)
   - `compile_system_prompt(role_name)` — full system prompt with role + SOUL + LAWS

   Config loaded from `src/config/agent-brain/`:
   - `identity/LAWS.md`, `identity/SOUL.md` — immutable core
   - `roles/` — strategic, operational, analyst, debugger, reviewer
   - `skills/` — dry, context_hygiene (auto-created skills from SkillLearner go here too)

6. **ContextBuilder** (`src/core/context/context_builder.py`)
   Hierarchical prompt assembly with token budgeting:
   - Quotas: Identity (12%), Role (12%), Tools (6%), Conversation (remaining)
   - Drop order on overflow: conversation first, identity/role never dropped
   - Truncation marker: `\n\n[TRUNCATED]`
   - Injects YAML output format block and `active_skills` list

7. **ContextController** (`src/core/context/context_controller.py`)
   Token budget enforcement for repository context:
   - `prioritize_files()` — assigns relevance scores
   - `enforce_budget()` — trims to token limit
   - `get_relevant_snippets()` — extracts key lines
   - Wired in `analysis_node` Phase 3

8. **Tool Parser** (`src/core/orchestration/tool_parser.py`)
   Parses YAML tool blocks from model output. Supports:
   ```yaml
   name: edit_file
   arguments:
     path: src/main.py
     patch: "..."
   ```
   Also supports compact format (`tool_name:\n  arg: val`), `<think>` block stripping (LMStudio), and inline YAML. **XML `<tool>` tags are not supported.**

9. **GraphFactory** (`src/core/orchestration/graph_factory.py`)
   Hub-and-spoke dynamic graph composition. Creates role-specific graphs:
   - `create_planner_graph()` — planning-focused workflow
   - `create_coder_graph()` — execution-focused workflow
   - `create_reviewer_graph()` — verification-focused workflow
   - `create_researcher_graph()` — search-focused workflow
   Used by `subagent_tools.py` to spawn isolated subagents.

---

## Tools

### Tool Registry (`src/tools/registry.py`)
Central registry of named tools. Tools are small functions registered with metadata (description, side_effects). Converted to a YAML `<available_tools>` block injected into the system prompt.

### File Operations (`src/tools/file_tools.py`)
- `list_files` / `fs.list` — directory listing
- `read_file` / `fs.read` — read file content
- `read_file_chunk` — read a byte range of a file
- `write_file` / `fs.write` — write file (tiered bash allowlist enforced)
- `edit_file` — apply a patch/diff to a file
- `edit_by_line_range` — replace specific line ranges
- `delete_file` — delete a file
- `glob` — file pattern matching
- `batched_file_read` — read multiple files efficiently
- `multi_file_summary` — get file metadata without full reads

**Security:** Tiered bash allowlist (Tier 1: safe read-only, Tier 2: test/compile, Tier 3: restricted with `requires_approval`). Shell operators blocked pre-parse. Symlink path traversal protection via `os.path.realpath`.

### Search Tools (`src/tools/system_tools.py`)
- `grep(pattern, path)` — regex pattern search; uses system `grep` with pure-Python fallback. Constrained to workdir.

### Code Intelligence (`src/tools/repo_tools.py`)
- `initialize_repo_intelligence(workdir)` — indexes repo to `.agent-context/repo_index.json` + LanceDB vector store
- `search_code(query, workdir)` — semantic search over codebase via VectorStore
- `find_symbol(name)`, `find_references(name)` — symbol lookup via SymbolGraph

### Repository Analysis (`src/tools/repo_analysis_tools.py`)
- `analyze_repository(workdir)` — scans Python files, extracts module summaries and import relationships, writes `.agent-context/repo_memory.json`

### Repository Summary (`src/tools/repo_summary.py`)
- `repo_summary(workdir)` — fast overview of project structure; detects framework (FastAPI, Flask, React, etc.) and generates a tree overview. Used by `analysis_node` at startup.

### Symbol Reader (`src/tools/symbol_reader.py`)
- `SymbolReader.parse_symbols(file_path)` — AST-based extraction of function/class/method locations
- `read_symbol(file_path, symbol_name)` — read only the lines of a specific function or class (avoids loading full files)

### Verification Tools (`src/tools/verification_tools.py`)
- `run_tests(workdir, test_files)` — runs pytest with structured output (passed/failed counts, tracebacks)
- `run_linter(workdir)` — runs ruff/flake8
- `syntax_check(workdir)` — quick `py_compile` across repo

### State Tools (`src/tools/state_tools.py`)
- `create_state_checkpoint(...)` — saves agent state snapshot to `.agent-context/checkpoints/`
- `list_checkpoints(workdir)` — lists available checkpoints
- `restore_state_checkpoint(checkpoint_id, workdir)` — restores a checkpoint
- `diff_state(id1, id2, workdir)` — compares two checkpoints

### Patch Tools (`src/tools/patch_tools.py`)
- `generate_patch(path, new_content, workdir)` — generates unified diff between file and new content
- `apply_patch(path, patch, workdir)` — applies a unified diff patch

### Role Tools (`src/tools/role_tools.py`)
- `get_role()` — returns current in-memory role
- `set_role(role, orchestrator)` — sets role on in-memory holder and optionally on the orchestrator; publishes `role.change` event

### Memory Tools (`src/core/memory/memory_tools.py`)
- `memory_search(query, workdir)` — searches `TASK_STATE.md` and `execution_trace.json`; returns ranked matches (exact lines first, then trace entries by recency)

### Subagent Tools (`src/tools/subagent_tools.py`)
- `delegate_task(role, subtask_description, working_dir)` — spawns an isolated autonomous subagent via `GraphFactory` for a specific subtask, keeping the main agent's context window clean

### Git Tools
- `get_git_diff(workdir)` — returns `git diff` output for tracking changes

### Echo Tool
- `echo(message)` — test/debug echo tool

### Toolset Loader (`src/tools/toolsets/loader.py`)
Loads YAML toolset files; checks `src/config/toolsets/` first, falls back to `src/tools/toolsets/` for backward compatibility. Caches loaded toolsets.

**Toolsets** (`src/config/toolsets/` and `src/tools/toolsets/`):
- `coding.yaml`, `debug.yaml`, `review.yaml`, `planning.yaml`

---

## Inference Layer (`src/core/inference/`)

| File | Description |
|------|-------------|
| `llm_manager.py` | `ProviderManager` — provider registry, model discovery, `call_model()`, routing helpers. Singleton via `get_provider_manager()`. |
| `llm_client.py` | Abstract `LLMClient` base class — defines `generate()` and `agenerate()` interface. |
| `adapter_wrappers.py` | `AdapterWrapper` — wraps existing adapters into a uniform `generate()` API; normalizes model lists. |
| `adapters/lm_studio_adapter.py` | LM Studio HTTP adapter — calls `/v1/chat/completions`; minimal deps, test-friendly. |
| `adapters/ollama_adapter.py` | Ollama HTTP adapter — calls Ollama REST API; delegates config helpers to `llm_manager`. |
| `telemetry.py` | `publish_model_response()` — emits model response telemetry (tokens, latency) to EventBus. `with_telemetry()` decorator wraps adapter calls. |

**ModelRouter**: Predicts payload complexity to toggle between small/fast (7B-9B) vs larger (32B-70B) models based on task characteristics.

---

## Memory System

### Working Memory
`MessageManager` (`src/core/orchestration/message_manager.py`) — in-memory conversation history with sliding token window.

### Context Distiller (`src/core/memory/distiller.py`)
LLM-based summarization of conversation history every 5 steps → `.agent-context/TASK_STATE.md`. Output keys: `current_task`, `completed_steps`, `next_step`. Fallback on LLM failure.

### Session Store (`src/core/memory/session_store.py`)
SQLite-based persistence to `.agent-context/session.db`. Tables: `messages`, `tool_calls`, `errors`, `plans`, `decisions`. Wired in `orchestrator.execute_tool` (tool calls), `planning_node` (plans), `debug_node` (errors).

### Advanced Features (`src/core/memory/advanced_features.py`)
- `TrajectoryLogger` — stores successful run logs to `.agent-context/trajectories/`
- `DreamConsolidator` — background memory consolidation
- `RefactoringAgent` — code smell detection and suggestions
- `ReviewAgent` — patch review and feedback
- `SkillLearner` — auto-creates skill files in `src/config/agent-brain/skills/` from successful ≥2-tool tasks

### Episodic Memory files
| Path | Description |
|------|-------------|
| `.agent-context/TASK_STATE.md` | Distilled task summary |
| `.agent-context/execution_trace.json` | Tool call log for loop prevention |
| `.agent-context/usage.json` | Token/latency/tool call cost tracking |
| `.agent-context/checkpoints/` | State checkpoints (state_tools.py) |
| `.agent-context/session.db` | SQLite: tool calls, plans, errors, decisions |
| `.agent-context/snapshots/` | Pre-edit file snapshots (RollbackManager) |
| `.agent-context/trajectories/` | Successful run logs (TrajectoryLogger) |
| `.agent-context/repo_index.json` | Repository symbol index |
| `.agent-context/repo_memory.json` | Module summaries + dependency graph |
| `.agent-context/last_plan.json` | Cross-session plan persistence |
| `src/config/agent-brain/skills/` | Auto-created skill files (SkillLearner) |

---

## Repository Intelligence (`src/core/indexing/`)

| File | Description |
|------|-------------|
| `repo_indexer.py` | Multi-language regex parser (15+ languages). SHA256 hash-based incremental indexing. Saves metadata to `repo_index_meta.json`. Version 3.0. |
| `vector_store.py` | LanceDB-based semantic search using sentence-transformers. `vs.search(query, limit=N)`. |
| `symbol_graph.py` | AST-based call graph. `update_file(path)` for incremental updates; `find_calls()`, `find_tests_for_module()`. Wired in `analysis_node` Phase 2. |

---

## Orchestration Support (`src/core/orchestration/`)

| File | Description |
|------|-------------|
| `orchestrator.py` | Central runtime — action loop, tool execution, preflight, loop prevention, signal-safe timeout, multi-file transactions. |
| `agent_brain.py` | In-memory cache for agent-brain config files (roles, skills, identity). |
| `message_manager.py` | Conversation history with sliding token window. |
| `tool_parser.py` | YAML tool block parser (strips `<think>` blocks, handles compact/inline formats). |
| `event_bus.py` | Topic-based pub/sub + agent messaging; `get_event_bus()` singleton. |
| `graph_factory.py` | Role-specific graph composition via `GraphFactory`; used for subagent spawning. |
| `rollback_manager.py` | File snapshot + atomic rollback. `snapshot_files()`, `append_to_snapshot()`, `rollback()`, `cleanup_old_snapshots()`. |
| `sandbox.py` | `ExecutionSandbox` — temp workspace for patch validation (AST, ruff, mypy, pytest). `SelfDebugLoop` — max 3 retries with error analysis. |
| `workspace_guard.py` | Protected path patterns (`.git/`, `.env`, `pyproject.toml`, etc.) — blocks writes to critical files. |
| `role_config.py` | Role-based access control: planner (read-only), coder (full), reviewer (read+verify), researcher (search). `normalize_role()`, `CANONICAL_ROLES`, `ROLE_ALIASES`. |
| `tool_contracts.py` | Pydantic result schemas for specific tools (e.g. `ListFilesResult`). Validated in `execute_tool`. Includes `requires_split` flag for patch size guard. |
| `tool_schema.py` | Base `ToolContract` pydantic model: `{tool, args, result, error}`. |

---

## Event System (`src/core/orchestration/event_bus.py`)

Topic-based pub/sub with agent-level messaging:
- `subscribe(topic, handler)` / `publish(topic, payload)`
- `subscribe_to_agent()`, `publish_to_agent()` — multi-agent coordination
- `broadcast_to_agents()` — coordinated messaging
- Message priority: LOW, NORMAL, HIGH, CRITICAL
- Wildcard `*` receives all agent messages

### Dashboard Events

| Event | Publisher | Payload |
|-------|-----------|---------|
| `file.modified` | `orchestrator.execute_tool` | `{path, tool, workdir}` |
| `file.deleted` | `orchestrator.execute_tool` | `{path, workdir}` |
| `tool.execute.start` | `orchestrator.execute_tool` | `{tool, args, workdir}` |
| `tool.execute.finish` | `orchestrator.execute_tool` | `{tool, ok, workdir}` |
| `tool.execute.error` | `orchestrator.execute_tool` | `{tool, error, workdir}` |
| `tool.invoked` | `orchestrator.execute_tool` | `{tool, ts, workdir}` |
| `tool.preflight` | `orchestrator.preflight_check` | `{tool, ok}` |
| `plan.progress` | `execution_node` | `{current_step, total_steps, step_description, completed}` |
| `verification.complete` | `verification_node` | `{status, tests, linter, syntax}` |
| `model.routing` | `ProviderManager` | `{model, provider, complexity}` |
| `message.truncation` | `MessageManager` | `{dropped_count, remaining}` |
| `role.change` | `role_tools.set_role` | `{role}` |
| `ui.notification` | various | `{level, message, source}` |

---

## Telemetry (`src/core/telemetry/`)

| File | Description |
|------|-------------|
| `consumer.py` | `TelemetryConsumer` — subscribes to EventBus and writes JSON-lines telemetry to `output/telemetry.jsonl`. |
| `metrics.py` | `TelemetryMetrics` — in-memory Prometheus-style counters/gauges/histograms; subscribes to tool and model events. Dependency-free. |

---

## UI Layer (`src/ui/`)

| File | Description |
|------|-------------|
| `app.py` | `CodingAgentApp` — wires EventBus, Orchestrator, ProviderManager; chooses Textual vs headless. |
| `textual_app_impl.py` | Full Textual TUI — sidebar (provider/model), chat output, input box; thread-based agent dispatch; settings panel integration. |
| `textual_app.py` | Minimal placeholder `TextualApp` shim; no-op in headless environments. |
| `views/main_view.py` | `MainViewController` — dashboard: `ModifiedFilesPanel`, `TaskProgressPanel`, `ToolActivityPanel`; subscribes to EventBus for live data. Split-pane layout 65%/35%. |
| `views/settings_panel.py` | `SettingsPanelController` — provider/model selection, new session; UI-framework-agnostic; updates `providers.json` models field at runtime. |
| `views/provider_panel.py` | `ProviderPanelController` — displays providers/models, handles switching; subscribes to `provider.models.list`. |
| `components/log_panel.py` | `LogPanel` — subscribes to `log.new` EventBus event; collects log entries for display. |
| `styles/main.tcss` | Textual CSS stylesheet. |

---

## Core Utilities (`src/core/`)

| File | Description |
|------|-------------|
| `logger.py` | Thread-safe logging with audit support. `AuditEventType` enum (COMMAND_EXECUTION, FILE_WRITE, PATH_TRAVERSAL_BLOCKED, etc.). Queue-based async log delivery to TUI. |
| `startup.py` | `provider_health_check()` — async check of all registered providers for adapter presence and model availability. Called from `main.py`. |
| `user_prefs.py` | `UserPrefs` — loads/saves user preferences to `~/.config/codingagent/prefs.json`. Stores `selected_model_provider`, `selected_model_name`, `active_mode`. |

---

## Evaluation (`src/core/evaluation/`)

| File | Description |
|------|-------------|
| `scenario_evaluator.py` | `Scenario` dataclass for standardized test definitions. `ScenarioEvaluator` runs evaluation suites deterministically (works with `deterministic=True` Orchestrator mode: temperature=0, seed param). SWE-bench style. |

---

## Configuration (`src/config/`)

```
src/config/
├── agent-brain/
│   ├── identity/
│   │   ├── LAWS.md          # Core operating laws (immutable)
│   │   └── SOUL.md          # Operating principles (immutable)
│   ├── roles/
│   │   ├── strategic.md     # Task decomposition and planning
│   │   ├── operational.md   # Tool execution and implementation
│   │   ├── analyst.md       # Repository exploration
│   │   ├── debugger.md      # Debugging and issue analysis
│   │   └── reviewer.md      # Quality assurance
│   └── skills/
│       ├── dry.md           # Don't Repeat Yourself
│       └── context_hygiene.md  # Context management
├── toolsets/
│   ├── coding.yaml
│   ├── debug.yaml
│   ├── review.yaml
│   └── planning.yaml
├── providers.json           # Provider configurations (LM Studio, Ollama, etc.)
└── schema.json              # Provider config JSON schema
```

**Dynamic Skill Injection:**
- `perception_node`: injects `context_hygiene` when task contains debug/fix/error/search keywords
- `execution_node`: injects `dry` when `len(relevant_files) > 2`

---

## Scripts (`scripts/`)

| File | Description |
|------|-------------|
| `generate_system_map.py` | Generates `docs/system_map.md` ASCII tree + `scripts/tree.json`. Excludes audit, .agent-context, .venv, tests, output. |
| `run_tui.py` / `start_tui.py` | Launch the TUI application. |
| `simulate_tui.py` | Headless TUI simulation for testing. |
| `add_provider.py` | Add a new LLM provider to config. |
| `check_providers_and_models.py` | Health check all configured providers. |
| `diagnose_lmstudio.py` | LM Studio-specific connectivity diagnostics. |
| `analyze_tokens.py` | Token usage analysis from usage.json. |
| `refresh_summaries.py` | Regenerate repo summaries. |
| `run_generate.py` | Run code generation task via CLI. |
| `test_agent_stability.py` | Stability test — run multiple agent tasks and check for regressions. |
| `test_langgraph_node.py` | Isolated LangGraph node tests. |
| `test_llm_stability.py` | LLM provider stability tests. |
| `test_real_lmstudio.py` / `test_real_lmstudio_file_edit.py` | Integration tests against live LM Studio. |
| `test_tools.py` | Tool execution tests. |
| `validate_ollama.py` | Ollama adapter validation. |
| `list_prompts.py` | List all compiled system prompts. |
| `wait_for_model.py` | Poll until a model is available. |
| `fetch_ollama.py` | Pull models from Ollama registry. |
| `ensure_venv.sh` | Bootstrap virtual environment. |
| `run_tests_settings.py` | Run tests with custom settings. |

---

## Reliability Features

- **Tool Timeout Protection**: `signal.SIGALRM` armed only in main thread; worker threads (TUI path via `ThreadPoolExecutor`) run tools without signal-based timeout to avoid `ValueError`.
- **Tool Contracts** (`tool_contracts.py`): Pydantic validation for tool results.
- **Loop Prevention**: `execution_trace.json` tracks tool+args pairs; blocks repeated calls after 3 consecutive identical actions; injects `[LOOP DETECTED]` message.
- **Read-Before-Edit Guard**: `WRITE_TOOLS_REQUIRING_READ` frozenset (edit_file, write_file, edit_by_line_range, apply_patch) — write blocked if file not read first in current session.
- **Multi-file Atomicity**: Step-level transactional snapshots via `RollbackManager` — all files written in one step are bundled and atomically rolled back on verification failure.
- **WorkspaceGuard**: Blocks modifications to `.git/`, `.env`, `pyproject.toml`, `requirements.txt`, and other critical paths.
- **Deterministic Mode**: `deterministic=True` sets temperature=0 + seed for reproducible runs.
- **Cost Tracking**: Tokens, latency, tool calls tracked in `.agent-context/usage.json`.
- **Sandbox Fail-Closed**: Write operations blocked if AST validation fails (not just warned).

---

## Known Architecture Notes

- **`workflow_nodes.py`** is a pure re-export shim for backward compatibility. New code imports directly from individual node files.
- **`src/tools/toolsets/`** (legacy) and **`src/config/toolsets/`** (canonical) both exist; the loader prefers `src/config/toolsets/`.
- **`src/core/memory/session_store.py`** is the correct location; not `src/core/context/session_store.py`.
- **`plan_validator_node.py`** exposes a standalone `validate_plan()` function, not an async node — called directly by `planning_node` or `orchestrator` before executing a plan.
- The **`graph/nodes/__init__.py`** is empty; all node imports are explicit in `workflow_nodes.py` and `graph/builder.py`.
