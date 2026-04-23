# CodingAgent Architecture

> **Status**: Production-ready — 3844+ tests passing, 0 Critical/High issues
> **Last Updated**: 2026-04-14

---

## 1. Source Directory Structure

```
src/
├── main.py                    # CLI entry point, argument parsing, headless/TUI dispatch
├── core/
│   ├── orchestration/          # Core agent orchestration
│   │   ├── orchestrator.py           # Main Orchestrator class
│   │   ├── orchestrator_bootstrap.py # 4-phase initialization (infra/providers/events/services)
│   │   ├── orchestrator_helpers.py   # Helper functions for orchestrator
│   │   ├── inference_loop.py         # Agent turn loop logic
│   │   ├── task_lifecycle.py         # Task lifecycle management
│   │   ├── session_manager.py        # Session lifecycle (create, fork, revert)
│   │   ├── session_store.py          # SQLite/JSONL persistence
│   │   ├── message_manager.py        # Message history + token windowing
│   │   ├── token_budget.py           # Token budgeting and limits
│   │   ├── session_cost_tracker.py   # Cost tracking and reporting
│   │   ├── project_settings.py       # Per-project configuration
│   │   ├── provider_capabilities.py # Model/provider capabilities
│   │   ├── role_config.py           # Role definitions and loading
│   │   ├── agent_brain.py            # Agent brain (role) management
│   │   ├── agent_types.py            # Agent type definitions
│   │   ├── agent_session_manager.py  # Session management with hydration
│   │   ├── event_bus.py              # Event bus for pub/sub
│   │   ├── event_log.py              # Event logging to SQLite
│   │   ├── loop_guards.py            # Doom-loop, alternating-loop detection
│   │   ├── permission_gateway.py    # Permission gating for tools
│   │   ├── permission_policy.py     # Permission policies
│   │   ├── approval_gate.py         # User approval flow
│   │   ├── tool_execution_pipeline.py # Tool execution with permissions
│   │   ├── tool_execution_service.py  # Tool execution service
│   │   ├── tool_registry.py          # Tool registry with timeouts
│   │   ├── tool_parser.py            # Tool call parsing (JSON/YAML)
│   │   ├── tool_preflight.py         # Pre-execution validation
│   │   ├── tool_result_formatter.py  # Tool result normalization
│   │   ├── tool_contracts.py         # Tool contracts/validation
│   │   ├── tool_hooks.py             # Tool pre/post hooks
│   │   ├── tool_constants.py        # Tool-related constants
│   │   ├── preview_service.py       # Diff preview service
│   │   ├── preview_coordinator.py   # Preview coordination
│   │   ├── rollback_manager.py      # Rollback/snapshot management
│   │   ├── snapshot_manager.py      # Git snapshot management
│   │   ├── file_lock_manager.py     # File locking for PRSW
│   │   ├── wave_coordinator.py     # Wave execution coordinator
│   │   ├── prsw_topics.py           # PRSW event topics
│   │   ├── dag_parser.py            # DAG parsing for plans
│   │   ├── graph_factory.py         # Graph factory
│   │   ├── execution_trace.py       # Execution trace logging
│   │   ├── deferred_init.py         # Deferred initialization
│   │   ├── plan_mode.py             # Plan mode (user approval)
│   │   ├── remote_skills.py         # Remote skills loading
│   │   ├── shell_hooks.py           # Shell hook execution
│   │   ├── session_watcher.py       # Session file watcher
│   │   ├── cross_session_bus.py     # Cross-session event bus
│   │   ├── mcp_stdio_server.py     # MCP STDIO server
│   │   ├── git_worktree_manager.py  # Git worktree isolation
│   │   ├── work_summary.py          # Work summary generation
│   │   ├── workspace_guard.py      # Workspace boundary guard
│   │   ├── session_lifecycle.py    # Session lifecycle events
│   │   ├── session_registry.py     # Session registry
│   │   ├── session_cost_tracker.py # Cost tracking
│   │   ├── instruction_loader.py   # System prompt loading
│   │   ├── provider_capabilities.py # Provider capabilities
│   │   │
│   │   └── graph/                  # LangGraph workflow
│   │       ├── state.py            # AgentState TypedDict
│   │       ├── builder.py          # Graph compilation + routing
│   │       └── nodes/
│   │           ├── perception_node.py      # Task parsing + tool generation
│   │           ├── analysis_node.py        # Repository intelligence
│   │           ├── planning_node.py        # Plan generation
│   │           ├── plan_validator_node.py  # Plan validation
│   │           ├── execution_node.py       # Tool execution
│   │           ├── step_controller_node.py # Step gating
│   │           ├── verification_node.py    # Test/lint verification
│   │           ├── evaluation_node.py      # Success/failure evaluation
│   │           ├── debug_node.py           # Debug loop
│   │           ├── replan_node.py           # Step splitting
│   │           ├── delegation_node.py      # Subagent spawning
│   │           ├── analyst_delegation_node.py # Analyst subagents
│   │           ├── memory_update_node.py   # Memory sync
│   │           ├── frontier_loop_node.py   # Tight LLM+tool loop (LARGE/FRONTIER)
│   │           ├── wait_for_user_node.py   # User input wait
│   │           └── node_utils.py           # Node utilities
│   │
│   ├── context/               # Context building
│   │   ├── context_builder.py      # Prompt building with tier awareness
│   │   ├── context_controller.py  # Context window control
│   │   └── instruction_files.py   # Instruction file loading
│   │
│   ├── memory/               # Memory system
│   │   ├── session_store.py       # SQLite session persistence
│   │   ├── jsonl_session_store.py # JSONL session store
│   │   ├── distiller.py           # LLM-based context distillation
│   │   ├── auto_compactor.py      # Deterministic auto-compaction (CP-6)
│   │   └── advanced_features.py   # Advanced memory features
│   │
│   ├── indexing/             # Repository intelligence
│   │   ├── repo_indexer.py        # SHA256-based indexing
│   │   ├── symbol_graph.py        # Symbol graph
│   │   ├── vector_store.py        # LanceDB vector store
│   │   ├── lsp_manager.py         # LSP manager
│   │   ├── lsp_client.py          # LSP client
│   │   └── lsp_context.py        # LSP context injection
│   │
│   ├── inference/            # LLM inference
│   │   ├── llm_manager.py        # LLM manager
│   │   ├── model_tiers.py        # Model tier classification
│   │   └── adapters/             # Provider adapters
│   │
│   ├── mcp/                 # MCP support
│   ├── auth/                # Authentication
│   ├── config_loader.py     # Config loading
│   ├── user_prefs.py        # User preferences
│   ├── paths.py             # Path utilities
│   ├── credentials.py       # Credentials management
│   ├── logger.py            # Logging
│   ├── settings/            # Settings
│   ├── prompts/             # Prompt templates
│   ├── utils/               # Utilities
│   └── plugin/              # Plugin system
│
├── tools/                   # Tool definitions
│   ├── _tool.py             # @tool decorator + ToolDefinition
│   ├── _registry.py         # ToolRegistry with auto-discovery
│   ├── _security.py         # Bash security constants
│   ├── _bash_exec.py        # Bash execution
│   ├── _file_io.py          # File I/O operations
│   ├── _edit_tools.py       # Edit operations
│   ├── _diff_gate.py        # Diff verification
│   ├── _approval.py         # Approval handling
│   ├── _path_utils.py       # Path utilities
│   ├── _result.py           # Result handling
│   ├── _truncate.py         # Truncation utilities
│   ├── file_tools.py        # File tools re-exports
│   ├── bash_security.py     # AST-level bash analysis
│   ├── sandbox.py           # Bubblewrap sandbox
│   ├── git_tools.py         # Git tools
│   ├── verification_tools.py # Verification tools
│   ├── subagent_tools.py    # Subagent spawning tools
│   ├── web_tools.py         # Web search/fetch
│   ├── memory_tools.py      # Memory tools
│   ├── state_tools.py       # State tools
│   ├── todo_tools.py        # Todo tools
│   ├── repo_tools.py        # Repository tools
│   ├── repo_analysis_tools.py # Repo analysis tools
│   ├── repo_summary.py      # Repo summary
│   ├── patch_tools.py       # Patch tools
│   ├── system_tools.py      # System tools
│   ├── project_tools.py     # Project tools
│   ├── interaction_tools.py # Interaction tools
│   ├── plan_mode_tools.py   # Plan mode tools
│   ├── skill_tools.py       # Skill tools
│   ├── rollback_tools.py    # Rollback tools
│   ├── guardrails.py       # Guardrails
│   ├── ast_tools.py         # AST tools
│   ├── lsp_tools.py         # LSP tools
│   ├── lint_dispatch.py     # Lint dispatch
│   ├── formatter.py        # Formatting
│   ├── role_tools.py        # Role tools
│   ├── symbol_reader.py    # Symbol reading
│   ├── batch_tools.py      # Batch tools
│   ├── permission_context.py # Permission context
│   ├── tools_config.py     # Tool configuration
│   ├── registry.py          # Legacy registry (backward compat)
│   └── toolsets/            # Toolset definitions
│
├── server/                  # HTTP server
│   └── app.py               # Server application
│
└── config/                  # Configuration
    └── toolsets/            # Toolset YAML files
```

---

## 2. Core Components

### 2.1 Orchestrator (`orchestrator.py`)

The main orchestrator class that coordinates all agent components:

- **Initialization**: Delegates to `orchestrator_bootstrap.py` for 4-phase setup
- **Tool Execution**: `execute_tool()` → `tool_execution_pipeline.py`
- **Agent Run**: `run_agent_once()` → `inference_loop.py`
- **Session Management**: Forks, reverts, resumes sessions

### 2.2 Orchestrator Bootstrap (`orchestrator_bootstrap.py`)

Four-phase initialization:

| Phase | Functions | Purpose |
|-------|------------|---------|
| 1. Infrastructure | `_init_infrastructure` | MessageManager, thread pools, managers |
| 2. Providers | `_init_providers` | Adapter selection, startup events |
| 3. Events | `_init_event_subscriptions` | Event bus subscriptions |
| 4. Services | `_init_services` | TokenBudgetMonitor, ContextController, etc. |

### 2.3 LangGraph Pipeline (`graph/`)

16-node graph for NANO/SMALL/MEDIUM tiers:

```
perception → analysis → planning → plan_validator → execution → verification → evaluation
                   ↓              ↓              ↓              ↓           ↓
              [delegation]   [analyst_delegation] [step_controller] [debug] [memory_sync]
```

8-node simplified graph for LARGE/FRONTIER:

```
perception → frontier_loop → verification → evaluation → memory_sync
                   ↓               ↓            ↓
                [debug]        [delegation]  [wait_for_user]
```

### 2.4 Model Tier System (`model_tiers.py`)

| Tier | Params | Context | Tools | Max Steps | Max Turns |
|------|--------|---------|-------|-----------|-----------|
| NANO | ≤7B | ≤4K | 8 | 4 | 15 |
| SMALL | 7-14B | 4-16K | 20 | 6 | 25 |
| MEDIUM | 14-70B | 16-128K | 35 | 10 | 40 |
| LARGE | >70B | >128K | 50 | 16 | 60 |
| FRONTIER | Cloud | >200K | 60 | 20 | 80 |

### 2.5 Tool System

- **Registration**: `@tool` decorator with metadata
- **Discovery**: `ToolRegistry.discover()` auto-discovers all tools
- **Execution**: `execute_tool_impl()` with permission gates
- **Security**: 5-layer security (patterns, restricted, safe, bash_security, sandbox)

---

## 3. Key Workflows

### 3.1 Agent Turn Flow

```
1. perception_node: Parse task → generate tool call
2. Route (per task complexity):
   - Simple: perception → execution → verification → evaluation → memory_sync
   - Complex: perception → analysis → planning → execution → verification → evaluation → memory_sync
3. memory_update_node: Distill context, update vector store
4. Return to step 1 for next turn
```

### 3.2 Tool Execution Flow

```
1. ToolRegistry.call() → look up tool
2. pre_execute() → permission gate check
3. Read-before-write: verify files_read if tool requires
4. Loop guard: check doom_loop detection
5. Execute tool with sandboxing
6. Normalize result
7. Post-execute: update cost tracker, execution trace
```

### 3.3 Session Fork/Revert Flow

```
Fork:
  1. Create snapshot via SnapshotManager
  2. Fork session in SessionStore
  3. Publish fork event

Revert:
  1. Load original snapshot
  2. Restore via git checkout
  3. Restore session state
```

---

## 4. Security Layers

| Layer | Implementation | Purpose |
|-------|---------------|---------|
| 1. Pattern Block | `_BASE_DANGEROUS_PATTERNS` | Block command injection, destructive cmds |
| 2. Restricted | `RESTRICTED_COMMANDS` | Require approval |
| 3. Safe | `SAFE_COMMANDS` | Auto-allow read-only |
| 4. Analysis | `bash_security.py` | AST-level risk analysis |
| 5. Sandbox | `sandbox.py` | bubblewrap containment (Linux) |

---

## 5. Memory System

| Component | Purpose |
|-----------|---------|
| `SessionStore` | SQLite persistence |
| `MessageManager` | Token windowing + compaction |
| `Distiller` | LLM-based context distillation |
| `AutoCompactor` | Deterministic compaction (CP-6) |
| `VectorStore` | LanceDB semantic search |

---

## 6. Repository Intelligence

| Component | Purpose |
|-----------|---------|
| `RepoIndexer` | SHA256-based incremental indexing |
| `SymbolGraph` | Code symbol indexing |
| `VectorStore` | Semantic search |
| `LSPManager` | LSP integration |
| `ContextController` | Context window management |

---

## 7. Test Coverage

| Category | Count |
|----------|-------|
| Unit tests | 3844 |
| Integration tests | Available |
| Benchmark tests | 7 |

---

## 8. Implementation Status

| Feature | Status |
|---------|--------|
| LangGraph pipeline | ✅ Complete |
| Multi-file atomic rollback | ✅ Complete |
| Advanced memory | ✅ Complete |
| Repository intelligence | ✅ Complete |
| PRSW (Parallel Reads, Sequential Writes) | ✅ Complete |
| DAG-based wave execution | ✅ Complete |
| Native Tool Support (frontier + local) | ✅ Complete |
| Role-Based Prompt Injection | ✅ Complete |
| Model tiers (NANO→FRONTIER) | ✅ Complete |
| MCP STDIO Server | ✅ Complete |
| Session fork/revert | ✅ Complete |
| Bubblewrap sandbox | ✅ Complete |
| Approval gate | ✅ Complete |
| Loop guards | ✅ Complete |
| --resume-session | ✅ Complete |

---

## 9. Audit History

| Volume | Date | Status |
|--------|------|--------|
| Vol28 | 2026-04-13 | 0 Critical, 0 High |
| Vol29 | 2026-04-14 | 0 Critical, 0 High |

---

## 10. Key Constants

### Loop Guards
- `DOOM_LOOP_THRESHOLD = 3` — identical tool call detection
- `ALTERNATING_LOOP_THRESHOLD = 3` — alternating tool detection
- `COOLDOWN_GAP = 3` — tool cooldown between same-tool calls
- `_MAX_ROUNDS_PLANNING = 15` — max planning rounds
- `_AUTONOMOUS_MAX_TOOL_CALLS = 100` — max tool calls in autonomous mode

### Tool Limits
- `_BASH_STDOUT_MAX = 16_384` bytes
- `_BASH_STDERR_MAX = 6_000` bytes
- `_TOOL_OUTPUT_MAX_BYTES = 50_000` (frontier loop)

### Sandbox Levels
- `off` — no sandboxing
- `workspace` — bwrap with workspace write (default)
- `full` — bwrap + network disabled

---

## 11. Configuration Files

| File | Purpose |
|------|---------|
| `.agent-context/config.json` | Project settings |
| `.agent-context/sessions.db` | SQLite session store |
| `.agent-context/memory/` | Vector store |
| `providers.json` | LLM provider config |
| `toolsets/*.yaml` | Toolset definitions |
| `config/agent-brain/roles/*.md` | Role prompts |

---

## 12. Dependencies

### Core
- `langgraph` — Graph workflow
- `langchain-core` — Core abstractions
- `tiktoken` — Token counting

### Providers
- `openai` — OpenAI API
- `anthropic` — Anthropic API
- `httpx` — HTTP client

### Storage
- `sqlite3` — Session persistence
- `lancedb` — Vector store

### UI
- `textual` — TUI framework
- `rich` — Rich terminal output

---

## 13. CLI Commands

```bash
# Headless mode
codingagent --task "fix the bug" --workdir /path/to/repo

# Resume session
codingagent --resume-session <session_id> --task "continue"

# Dry run
codingagent --dry-run --task "refactor"

# TUI mode
codingagent
```

---

## 14. Environment Variables

| Variable | Purpose |
|----------|---------|
| `CODINGAGENT_SANDBOX_LEVEL` | Sandbox level (off/workspace/full) |
| `CODINGAGENT_TRUSTED` | Trust mode (skip approval) |
| `CODING_AGENT_HTTP_SERVER` | Start HTTP server |
| `CODINGAGENT_MODEL` | Default model |
| `CODINGAGENT_PROVIDER` | Default provider |

---

## 15. API Reference

### Orchestrator Methods

```python
orch = Orchestrator(working_dir="/path")

# Run agent
result = orch.run_agent_once(
    system_prompt_name="operational",
    messages=[{"role": "user", "content": "fix the bug"}],
    tools={},
)

# Execute tool
result = orch.execute_tool({
    "name": "read_file",
    "arguments": {"path": "src/main.py"}
})

# Session management
orch.fork_session()
orch.revert_session(snapshot_id)
orch.compact_context()
```

### Tool Registration

```python
from src.tools import tool

@tool(side_effects=["write"], tags=["coding"])
def my_tool(param: str) -> dict:
    return {"ok": True}
```
