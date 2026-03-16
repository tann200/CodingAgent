# CodingAgent Architecture

> **Implementation Status**: Core implemented, gaps identified in `gap-analysis.md`

## Implementation Stages

| Stage | Status | Components |
|-------|--------|------------|
| Stage 1 - MVP Stabilization | ✅ Complete | Toolsets, Session Store |
| Stage 2 - Cognitive Agent | ✅ Complete | Symbol Graph, Sandbox, Self-Debug |
| Stage 3 - Advanced SWE | ✅ Complete | Trajectories, Dreams, Refactor, Review, Skills |

## Implementation Gaps (from gap-analysis.md)

| Gap | Description | Status |
|-----|-------------|--------|
| 1 | **AnalysisNode** - Separate node for repo exploration | ❌ Missing |
| 2 | **DebugNode + Retry** - Self-debugging with 3 retry limit | ❌ Missing |
| 3 | **Step Controller** - Enforce plan execution one step at a time | ❌ Missing |
| 4 | **Role Per-Node** - Role-specific prompts wired to each node | ❌ Missing |
| 5 | **Verification Branching** - Retry on failure, not always end | ❌ Missing |

### Current vs Target Pipeline

**Current (implemented):**
```
perception → planning → execution → verification → memory_sync → end
```

**Target (from gap-analysis.md):**
```
perception → analysis → planning → execution → step_controller → verification → (success→memory|debug→execution|exhausted→memory) → end
```

This document describes the core architecture of the local CodingAgent, including how prompts, context, orchestration, and tool handling are implemented.

## High-Level Flow

1. **User Interface**
   The user interacts via the Textual UI (TUI) running `src/ui/app.py` or the CLI entry points.
   The UI maintains the project workspace and dispatches user prompts to the **Orchestrator**.

2. **Orchestrator** (`src/core/orchestration/orchestrator.py`)
   The Orchestrator is the central brain of the system. It handles:
   - Initializing the LLM connection via `ProviderManager`.
   - Bootstrapping the **System Prompts** using `agent_brain.py`.
   - Managing message history and token windowing via `MessageManager`.
   - Executing the primary Action Loop (`run_agent_once`), which invokes the LangGraph cognitive pipeline.
   - Evaluating responses, validating Tool calls via `preflight_check`, executing them via `execute_tool`.
   - Role-based tool filtering via `RoleManager`.

 3. **LangGraph Cognitive Pipeline** (`src/core/orchestration/graph/`)
    The system uses a LangGraph state machine with the following nodes:
    - **perception_node**: Understands user request, classifies task, performs task decomposition for multi-step tasks
    - **planning_node**: Converts perception outputs into structured plans, manages decomposed task execution
    - **execution_node**: Enforces operational workflows (read_file-Before-edit_file, Sandbox constraints), executes plan steps
    - **verification_node**: Runs tests/linters/syntax checks on proposed edits
    - **memory_sync_node**: Persists distilled context to TASK_STATE.md, updates execution_trace.json
    
    **Flow:** 
    - Simple task: `perception → planning → execute → verification → memory_sync → end`
    - Multi-step task: `perception (decompose) → planning → execute → perception (next step) → ... → verification → memory_sync → end`
    
    **Task Decomposition:**
    - perception_node detects multi-step tasks using heuristics (e.g., "and", numbered lists, multiple action verbs)
    - When detected, calls LLM to split into independent steps stored in `current_plan`
    - execution_node generates tool calls for each step and advances through steps
    - Graph loops back to perception when more steps remain via `should_after_execution` conditional routing

4. **Message & Token Manager** (`src/core/orchestration/message_manager.py`)
   Because local models have strict sequence constraints (e.g. 8K tokens), the `MessageManager` tracks history and automatically shifts the sliding window (dropping oldest non-system interactions) when the conversation exceeds `max_tokens`.

5. **Agent Brain & Doctrine** (`src/core/orchestration/agent_brain.py`)
   Defines the agent's fundamental behaviors and constraints. The `load_system_prompt()` dynamically compiles a master system prompt at runtime using:
   - The selected Persona (e.g., `agents/coding_agent.md`) mapped to the `<system_role>` tag.
   - Core operating laws from `LAWS.md` mapped to `<core_laws>`.
   - Identity doctrine from `SOUL.md` mapped to `<operating_principles>`.
   - A rigid Output Schema requirement enforcing structured JSON envelopes for robust multi-step reasoning.

6. **ContextBuilder** (`src/core/context/context_builder.py`)
   Strict hierarchical prompt assembly with token budgeting:
   - Token quotas: Identity (12%), Role (12%), Tools (6%), Conversation (remaining)
   - Priority drop order: Conversation first, never Identity/Role
   - Truncation marker: `\n\n[TRUNCATED]` for overflow
   - Output format injection for XML tool blocks

7. **Tool Parser** (`src/core/orchestration/tool_parser.py`)
   Parses XML tool block format instead of JSON:
   ```xml
   <tool>
   name: edit_file
   args: {"path": "src/main.py", "patch": "..."}
   </tool>
   ```
   Supports both JSON args and YAML-like key-value lines.

8. **Context Distiller** (`src/core/memory/distiller.py`)
   Distills conversation history to `.agent-context/TASK_STATE.md` every 5 steps:
   - Output: `{"current_task", "completed_steps", "next_step"}`
   - LLM-based summarization with fallback on failure

9. **Loop Prevention**
   - Tracks `execution_trace.json` with tool+args pairs
   - Blocks repeated calls after 3 consecutive identical actions
   - Injects `[LOOP DETECTED]` system message forcing strategy change
   - Trace is cleared at start of each new task to prevent false positives

10. **Task Context Management**
   
   **Context Retention:**
   - `MessageManager` (`src/core/orchestration/message_manager.py`) stores conversation history
   - Automatically manages token window by dropping oldest non-system messages
   - System prompt is always preserved at the top of the message list
   
   **Task Isolation:**
   - Each user prompt starts a new task with a unique 8-character task ID
   - `start_new_task()` method clears message history between tasks
   - This prevents context bleed from previous conversations
   - Task ID is logged for debugging and tracing
   
   **Key Methods:**
   - `orchestrator.start_new_task()` - Generates new task ID, clears message history
   - `orchestrator.get_current_task_id()` - Returns current task ID
   - `orchestrator._clear_execution_trace()` - Clears loop detection trace
   - `cancel_event` - threading.Event passed to nodes for interruption

11. **Agent Interruption**
   - **ESC**: Graceful interrupt (sets cancel event)
   - **Double-ESC**: Force interrupt
   - **Ctrl+C**: Cancel request
   - Cancellation checked in `perception_node` and `execution_node` before each LLM call

10. **Agent Brain Structure** (`agent-brain/`)
     - `identity/` - Immutable core (SOUL.md, LAWS.md)
     - `roles/` - Behavioral templates (strategic.md, operational.md)
     - `skills/` - Modular capabilities (dry.md, context_hygiene.md)

11. **Tool Registry & Execution** (`src/tools/registry.py` & `file_tools.py`)
   Available capabilities are loaded globally via `registry.py` and the orchestrator's internal `ToolRegistry`. Tools are converted into a dense XML block `<available_tools>` directly injected into the system prompt.

   **Core Tools:**
   - `list_files`, `read_file`, `write_file`, `edit_file`, `delete_file` - File operations
   - `search_code`, `find_symbol`, `find_references` - Code search (via LanceDB)
   - `initialize_repo_intelligence` - Index repository for semantic search
   - `run_tests`, `run_linter`, `syntax_check` - Verification tools
   - `memory_search` - Search TASK_STATE.md and execution trace
   - `generate_patch`, `apply_patch` - Patch management

12. **Role Configuration** (`src/core/orchestration/role_config.py`)
   Defines role-based access control for multi-agent workflows:
   - **planner**: Allowed tools (read, search, list), denied (write, edit, delete)
   - **coder**: Full read/write access
   - **reviewer**: Read and verification tools only
   - **researcher**: Search and exploration tools

## Event System

**EventBus** (`src/core/orchestration/event_bus.py`) provides both topic-based and agent-based messaging:

- **Topic Subscriptions:** `orchestrator.startup`, `provider.models.cached`, `ui.notification`, `model.routing`, etc.
- **Agent Messaging:** `subscribe_to_agent()`, `publish_to_agent()` for multi-agent coordination
- **Message Priority:** LOW, NORMAL, HIGH, CRITICAL
- **Wildcard Subscriptions:** `*` receives all agent messages
- **Broadcast:** `broadcast_to_agents()` for coordinated messaging

## Hub-and-Spoke Architecture

**GraphFactory** (`src/core/orchestration/graph_factory.py`) provides dynamic graph composition:
- `create_planner_graph()` - Planning-focused workflow
- `create_coder_graph()` - Execution-focused workflow
- `create_reviewer_graph()` - Verification-focused workflow
- `create_researcher_graph()` - Search-focused workflow

**HubAndSpokeCoordinator** orchestrates multi-agent workflows:
- Agent registration with role assignment
- Task dispatch and queue management
- Result collection and agent status tracking

## Repository Intelligence

**Indexing** (`src/core/indexing/`):
- `repo_indexer.py`: Parses Python files, extracts classes/functions, builds symbol index
- `vector_store.py`: LanceDB-based semantic search using sentence-transformers

**Tools:**
- `initialize_repo_intelligence()` - Indexes repository to .agent-context/repo_index.json
- `search_code(query)` - Semantic code search
- `find_symbol(name)` - Locate exact symbol definitions
- `find_references(name)` - Find all usages of a symbol

## Memory System

**Working Memory:** MessageManager holds in-memory conversation history.

**Episodic Memory:**
- `.agent-context/TASK_STATE.md` - Distilled task summary
- `.agent-context/execution_trace.json` - Tool call log (for loop prevention)
- `.agent-context/usage.json` - Cost tracking (tokens, latency, tool calls)
- `.agent-context/checkpoints/` - State checkpoints for session recovery

## Tool Set (35 tools)

**File Operations:**
- `list_files` (alias: `fs.list`), `read_file` (alias: `fs.read`), `read_file_chunk`
- `write_file` (alias: `fs.write`), `edit_file`, `edit_by_line_range`, `delete_file`

**Pattern Search:**
- `grep` - Regex pattern search in files
- `search_code` - Semantic code search via vector store
- `find_symbol`, `find_references` - Symbol lookup

**Git Operations:**
- `get_git_diff` - Track changes

**Execution:**
- `bash(command)` - Shell command execution
- `glob(pattern)` - File pattern matching

**Verification:**
- `run_tests`, `run_linter`, `syntax_check`

**State Management:**
- `create_state_checkpoint` - Save session state
- `list_checkpoints`, `restore_state_checkpoint` - Recovery
- `diff_state` - Compare checkpoints

**Batched Operations:**
- `batched_file_read` - Read multiple files efficiently
- `multi_file_summary` - Get file info without full read

**Memory & Intelligence:**
- `memory_search` - Search TASK_STATE.md
- `initialize_repo_intelligence` - Build code index
- `analyze_repository` - Analyze repository structure

**Role Management:**
- `get_role`, `set_role` - Get/set current agent role

**Patch Management:**
- `generate_patch`, `apply_patch` - Patch file operations

**Testing:**
- `echo` - Test echo tool (for debugging)

**Memory:**
- `memory_search` - Search TASK_STATE.md
- `initialize_repo_intelligence` - Build code index

## Advanced Components

### Toolsets (`src/tools/toolsets/`)
- YAML-based tool grouping for role-based selection
- `coding.yaml` - Code editing tools
- `debug.yaml` - Debugging tools
- `review.yaml` - Code review tools
- `planning.yaml` - Planning tools

### Session Store (`src/core/memory/session_store.py`)
- SQLite-based conversation storage
- Tables: messages, tool_calls, errors, plans, decisions

### Symbol Graph (`src/core/indexing/symbol_graph.py`)
- AST-based code indexing
- `find_calls()` - Find function callers
- `find_tests_for_module()` - Find related tests
- Incremental updates on file changes

### Execution Sandbox (`src/core/orchestration/sandbox.py`)
- Temporary workspace for patch validation
- `validate_ast()` - Python AST validation
- `run_ruff()`, `run_mypy()`, `run_pytest()` - Validation tools

### Self-Debug Loop (`src/core/orchestration/sandbox.py`)
- Max 3 retry attempts
- Error analysis and fix suggestions
- Automatic re-testing

### Advanced Features (`src/core/memory/advanced_features.py`)
- **TrajectoryLogger**: Store agent runs for training
- **DreamConsolidator**: Memory consolidation
- **RefactoringAgent**: Code smell detection
- **ReviewAgent**: Patch review and feedback
- **SkillLearner**: Create new skills from success

**Semantic Memory:** LanceDB vector store for code symbol retrieval.

## Routing and Providers

- **ProviderManager** (`src/core/llm_manager.py`): Abstracts LM Studio, Ollama, or external APIs.
- **ModelRouter**: Predicts payload complexity to toggle between small/fast (7B-9B) vs larger (32B-70B) models.

## Reliability Features

- **Tool Contracts** (`src/core/orchestration/tool_contracts.py`): Pydantic validation for tool results.
- **Deterministic Mode**: Optional temperature=0, seed control for reproducible runs.
- **Loop Prevention**: Duplicate action detection, dead-end detection, retry limits.
- **Cost Tracking**: Tokens, latency, tool calls tracked in usage.json.

## Supported Roles

The system supports four agent roles with tool access control:
- `planner` - Task decomposition and planning
- `coder` - Code implementation
- `reviewer` - Validation and quality assurance
- `researcher` - Code exploration and discovery