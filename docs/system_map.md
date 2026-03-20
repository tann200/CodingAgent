# System Map

Generated: 2026-03-18 20:10:18Z

## Directory Structure with File Purposes

```
Repository: CodingAgent
```

### docs/

| File | Purpose |
|------|---------|
| `audit-instructions.md` | Instructions for conducting code audits |
| `ARCHITECTURE.md` | Comprehensive architecture documentation |
| `DEVELOPMENT.md` | Development guidelines |
| `MEMORY_ARCHITECTURE.md` | Memory system documentation |
| `system_map.md` | This file - directory structure with purposes |
| `test-coverage-analysis.md` | Test coverage analysis |
| `test-plan.md` | Testing strategy and plan |
| `tuispec.md` | Textual UI specification |

### scripts/

| File | Purpose |
|------|---------|
| `add_provider.py` | Add a new LLM provider to config |
| `analyze_tokens.py` | Token usage analysis from usage.json |
| `check_providers_and_models.py` | Health check all configured providers |
| `diagnose_lmstudio.py` | LM Studio-specific connectivity diagnostics |
| `ensure_venv.sh` | Bootstrap virtual environment |
| `fetch_ollama.py` | Pull models from Ollama registry |
| `generate_system_map.py` | Generates this file and tree.json |
| `list_prompts.py` | List all compiled system prompts |
| `refresh_summaries.py` | Regenerate repo summaries |
| `run_generate.py` | Run code generation task via CLI |
| `run_tests_settings.py` | Run tests with custom settings |
| `run_tui.py` / `start_tui.py` | Launch the TUI application |
| `simulate_tui.py` | Headless TUI simulation for testing |
| `test_agent_stability.py` | Stability test - multiple agent tasks |
| `test_langgraph_node.py` | Isolated LangGraph node tests |
| `test_llm_stability.py` | LLM provider stability tests |
| `test_real_lmstudio.py` | Integration tests against live LM Studio |
| `test_real_lmstudio_file_edit.py` | File edit integration tests |
| `test_tools.py` | Tool execution tests |
| `tree.json` | JSON representation of tree structure |
| `validate_ollama.py` | Ollama adapter validation |
| `wait_for_model.py` | Poll until a model is available |

### src/

#### src/config/

```
src/config/
├── agent-brain/           # Agent personality and behavior configuration
│   ├── identity/
│   │   ├── LAWS.md       # Core operating laws (immutable rules)
│   │   └── SOUL.md       # Operating principles
│   ├── roles/            # Agent role prompts for different tasks
│   │   ├── analyst.md    # Repository exploration role
│   │   ├── debugger.md   # Debugging role
│   │   ├── operational.md # Tool execution role
│   │   ├── reviewer.md   # QA role
│   │   └── strategic.md  # Planning role
│   └── skills/           # Dynamic skills auto-learned by agent
│       ├── context_hygiene.md  # Context management
│       └── dry.md        # Don't Repeat Yourself
├── toolsets/             # Tool definitions in YAML format
│   ├── coding.yaml
│   ├── debug.yaml
│   ├── planning.yaml
│   └── review.yaml
├── providers.json         # Provider configurations (LM Studio, Ollama)
└── schema.json            # Provider config JSON schema
```

| File/Directory | Purpose |
|----------------|---------|
| `agent-brain/` | Agent personality and behavior configuration |
| `agent-brain/identity/` | Immutable core identity files |
| `agent-brain/roles/` | Role-specific prompts for LLM calls |
| `agent-brain/skills/` | Auto-learned skills from successful tasks |
| `toolsets/` | Tool definitions in YAML format |
| `providers.json` | LLM provider configurations |
| `schema.json` | JSON schema for provider config validation |

#### src/core/

##### src/core/context/

| File | Purpose |
|------|---------|
| `context_builder.py` | Hierarchical prompt assembly with token budgeting |
| `context_controller.py` | Token budget enforcement for repository context |

##### src/core/evaluation/

| File | Purpose |
|------|---------|
| `scenario_evaluator.py` | Scenario-based evaluation framework for regression tests |

##### src/core/indexing/

| File | Purpose |
|------|---------|
| `repo_indexer.py` | Multi-language regex parser with SHA256 incremental indexing |
| `symbol_graph.py` | AST-based call graph for symbol lookup |
| `vector_store.py` | LanceDB-based semantic search |

##### src/core/inference/

| File | Purpose |
|------|---------|
| `llm_manager.py` | Provider registry and model discovery |
| `llm_client.py` | Abstract LLM client base class |
| `adapter_wrappers.py` | Wraps adapters into uniform generate() API |
| `telemetry.py` | Model response telemetry |
| `adapters/lm_studio_adapter.py` | LM Studio HTTP adapter |
| `adapters/ollama_adapter.py` | Ollama REST API adapter |

##### src/core/memory/

| File | Purpose |
|------|---------|
| `distiller.py` | LLM-based conversation summarization |
| `session_store.py` | SQLite-based session persistence |
| `memory_tools.py` | Memory search and retrieval tools |
| `advanced_features.py` | TrajectoryLogger, DreamConsolidator, SkillLearner |

##### src/core/orchestration/

| File | Purpose |
|------|---------|
| `orchestrator.py` | Central runtime - action loop, tool execution, preflight checks |
| `agent_brain.py` | In-memory cache for agent-brain config |
| `message_manager.py` | Conversation history with sliding token window |
| `event_bus.py` | Topic-based pub/sub for agent messaging |
| `graph_factory.py` | Role-specific graph composition for subagents |
| `rollback_manager.py` | File snapshots and atomic rollback |
| `sandbox.py` | Execution sandbox and self-debug loop |
| `tool_parser.py` | YAML tool block parser |
| `tool_contracts.py` | Pydantic result schemas for tools |
| `tool_schema.py` | Base ToolContract model |
| `workspace_guard.py` | Protected path patterns |
| `role_config.py` | Role-based access control |
| `schema.json` | Schema for orchestration config |

###### src/core/orchestration/graph/

| File | Purpose |
|------|---------|
| `state.py` | AgentState TypedDict - shared state between nodes |
| `builder.py` | LangGraph StateGraph compilation |

###### src/core/orchestration/graph/nodes/

| File | Purpose |
|------|---------|
| `perception_node.py` | Understand requests, decompose tasks, extract tool calls |
| `analysis_node.py` | Repository exploration via tools (VectorStore, SymbolGraph) |
| `planning_node.py` | Create step-by-step plans via LLM |
| `execution_node.py` | Execute plan steps via tool calls |
| `verification_node.py` | Run tests/linter/syntax checks |
| `evaluation_node.py` | Post-verification review - goal completion check |
| `debug_node.py` | Analyze failures, attempt fixes via LLM |
| `replan_node.py` | Split oversized patches into smaller steps |
| `step_controller_node.py` | Enforce single-step execution |
| `memory_update_node.py` | Persist context to TASK_STATE.md |
| `plan_validator_node.py` | Validate plan before execution |
| `node_utils.py` | Shared utilities (_resolve_orchestrator, _notify_provider_limit) |
| `workflow_nodes.py` | Re-export shim for backward compatibility |

##### src/core/telemetry/

| File | Purpose |
|------|---------|
| `consumer.py` | Telemetry consumer - writes to telemetry.jsonl |
| `metrics.py` | Prometheus-style counters/gauges/histograms |

##### src/core/

| File | Purpose |
|------|---------|
| `logger.py` | Thread-safe logging with audit support |
| `startup.py` | Provider health checks |
| `user_prefs.py` | User preferences persistence |

#### src/tools/

| File | Purpose |
|------|---------|
| `registry.py` | Central tool registry |
| `file_tools.py` | File operations (read, write, edit, delete, glob) |
| `system_tools.py` | grep, directory listing |
| `repo_tools.py` | Semantic search, symbol lookup |
| `repo_summary.py` | Fast repo overview and framework detection |
| `repo_analysis_tools.py` | Module summaries and dependency graph |
| `symbol_reader.py` | AST-based symbol extraction |
| `verification_tools.py` | pytest, ruff, syntax check |
| `state_tools.py` | Checkpoint creation and restoration |
| `patch_tools.py` | Patch generation and application |
| `role_tools.py` | Get/set agent role |
| `memory_tools.py` | Memory search (also in core/memory) |
| `subagent_tools.py` | Spawn isolated subagents |
| `toolsets/loader.py` | Load YAML toolset definitions |

#### src/ui/

| File | Purpose |
|------|---------|
| `app.py` | Main app - wires EventBus, Orchestrator, ProviderManager |
| `textual_app_impl.py` | Full Textual TUI with sidebar, chat, input |
| `textual_app.py` | Minimal placeholder for headless envs |
| `views/main_view.py` | Dashboard panels (files, progress, tools) |
| `views/settings_panel.py` | Settings UI (provider, model, session) |
| `views/provider_panel.py` | Provider/model display and switching |
| `components/log_panel.py` | Log collection and display |
| `styles/main.tcss` | Textual CSS stylesheet |

### src/main.py

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point - delegates to CodingAgentApp |

---

## Quick Reference

### Key Entry Points
- **CLI**: `src/main.py`
- **TUI**: `src/ui/textual_app_impl.py`
- **Headless**: `src/ui/app.py`

### Key Orchestration Files
- **Orchestrator**: `src/core/orchestration/orchestrator.py`
- **Graph Builder**: `src/core/orchestration/graph/builder.py`
- **State**: `src/core/orchestration/graph/state.py`

### Tool Categories
- **File Ops**: `src/tools/file_tools.py`
- **Code Intel**: `src/tools/repo_tools.py`, `src/tools/symbol_reader.py`
- **Verification**: `src/tools/verification_tools.py`
- **Memory**: `src/core/memory/distiller.py`, `src/core/memory/session_store.py`

### LLM Integration
- **Manager**: `src/core/inference/llm_manager.py`
- **Adapters**: `src/core/inference/adapters/`

### Agent Brain
- **Manager**: `src/core/orchestration/agent_brain.py`
- **Config**: `src/config/agent-brain/`
