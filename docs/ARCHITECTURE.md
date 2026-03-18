# CodingAgent Architecture

> **Implementation Status**: Core implemented with LangGraph pipeline, EventBus dashboard, and role-based nodes
> **Recent Updates**: Fixed infinite loop bug, TUI responsiveness, message duplication, tool result visibility, security fixes (bash allowlist, sandbox fail-closed, symlink traversal), fast-path routing

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

## Implementation Gaps (from gap-analysis.md)

| Gap | Description | Status |
|-----|-------------|--------|
| 1 | **AnalysisNode** - Separate node for repo exploration | ✅ Complete |
| 2 | **DebugNode + Retry** - Self-debugging with 3 retry limit | ✅ Complete |
| 3 | **Step Controller** - Enforce plan execution one step at a time | ✅ Complete |
| 4 | **Role Per-Node** - Role-specific prompts wired to each node | ✅ Complete |
| 5 | **Dynamic Skills** - Context-aware skill injection per node | ✅ Complete |
| 6 | **Verification Branching** - Retry on failure, not always end | ✅ Complete |
| 7 | **ReplanNode** - Split oversized patches into smaller steps | ✅ Complete |
| 8 | **EvaluationNode** - Post-verification task completion check | ✅ Complete |
| 9 | **EventBus Dashboard** - Real-time UI updates for file/tool/plan events | ✅ Complete |

### Current Pipeline (Fully Implemented)

```
Fast-Path (simple 1-step task):
perception → execution → verification → evaluation → (memory_sync|end)

Full Pipeline (complex multi-step task):
perception → analysis → planning → execution → step_controller → verification → evaluation → (memory_sync|step_controller|end)
          ↓
        replan (on patch size violation)
          ↓
       step_controller
```

**Conditional Routing (Fast-Path):**
The `route_after_perception` function checks if `next_action` exists after perception:
- If a tool call is ready → Skip to execution (fast-path)
- If no tool call → Go through analysis and planning (full pipeline)

**Node Role Mapping:**
- `perception_node` → operational role
- `analysis_node` → analyst role
- `planning_node` → strategic role
- `execution_node` → operational role
- `debug_node` → debugger role
- `verification_node` → reviewer role
- `evaluation_node` → reviewer role
- `replan_node` → planner role

This document describes the core architecture of the local CodingAgent, including how prompts, context, orchestration, and tool handling are implemented.

## High-Level Flow

1. **User Interface**
   The user interacts via the Textual UI (TUI) running `src/ui/app.py` or the CLI entry points.
   The UI maintains the project workspace and dispatches user prompts to the **Orchestrator**.

2. **Orchestrator** (`src/core/orchestration/orchestrator.py`)
   The Orchestrator is the central brain of the system. It handles:
   - Initializing the LLM connection via `ProviderManager` in `src/core/inference/llm_manager.py`.
   - Bootstrapping the **System Prompts** using `AgentBrainManager` in `agent_brain.py`.
   - Managing message history and token windowing via `MessageManager`.
   - Executing the primary Action Loop (`run_agent_once`), which invokes the LangGraph cognitive pipeline.
   - Evaluating responses, validating Tool calls via `preflight_check`, executing them via `execute_tool`.
   - Role-based tool filtering via `RoleManager`.

3. **LangGraph Cognitive Pipeline** (`src/core/orchestration/graph/`)
   The system uses a LangGraph state machine with isolated node files in `src/core/orchestration/graph/nodes/`:
   - **perception_node** (`perception_node.py`): Understands user request, performs task decomposition, uses `operational` role with dynamic `context_hygiene` skill injection for debugging/searching tasks.
   - **analysis_node** (`analysis_node.py`): Explores repository to gather relevant context before planning. Automatically executes `repo_summary()` at start and injects summary into context.
   - **planning_node** (`planning_node.py`): Converts perception outputs into structured plans using `strategic` role.
   - **execution_node** (`execution_node.py`): Enforces operational workflows, executes plan steps, uses `operational` role with dynamic `dry` skill injection when `len(relevant_files) > 2`. **Includes patch size guard** - intercepts `requires_split: True` from tool_contracts.py and triggers replan.
   - **verification_node** (`verification_node.py`): Runs tests/linters/syntax checks on proposed edits.
   - **evaluation_node** (`evaluation_node.py`): Post-verification review to decide if task goal is fully met. Routes to memory_sync (complete), step_controller (more work), or end.
   - **replan_node** (`replan_node.py`): Handles patch size violations by splitting oversized steps into 2-3 smaller, granular steps. Uses `planner` role.
   - **memory_update_node** (`memory_update_node.py`): Persists distilled context to TASK_STATE.md.
   - **debug_node** (`debug_node.py`): Analyzes verification failures and attempts fixes with retry limit, uses `operational` role.
   - **step_controller_node** (`step_controller_node.py`): Enforces single-step execution from the plan.
   - **node_utils** (`node_utils.py`): Shared utilities for orchestrator resolution and provider notifications.
    
   **Conditional Routing:**
   - `route_after_perception`: Fast-path routing - if next_action exists, skip to execution
   
   **Flow:** 
   - Fast-path (simple task): `perception → execution → verification → evaluation → memory_sync → end`
   - Full pipeline: `perception → analysis → planning → execute → verification → evaluation → memory_sync → end`
   - Multi-step task: `perception (decompose) → planning → execute → perception (next step) → ... → verification → evaluation → memory_sync → end`
   - Patch too large: `execution → replan → step_controller → execute (smaller step)`

4. **Message & Token Manager** (`src/core/orchestration/message_manager.py`)
   Because local models have strict sequence constraints (e.g. 8K tokens), the `MessageManager` tracks history and automatically shifts the sliding window (dropping oldest non-system interactions) when the conversation exceeds `max_tokens`.

5. **AgentBrainManager** (`src/core/orchestration/agent_brain.py`)
   In-memory caching system for agent-brain configuration. Provides fast access to:
   - `get_identity(name)`: Returns SOUL or LAWS content
   - `get_role(role_name)`: Returns role content (strategic, operational)
   - `get_skill(skill_name)`: Returns skill content (dry, context_hygiene)
   - `compile_system_prompt(role_name)`: Compiles full system prompt with role + SOUL + LAWS
   
   All content is loaded once at initialization from `src/config/agent-brain/`:
   - `identity/` - Immutable core (SOUL.md, LAWS.md)
   - `roles/` - Behavioral templates (strategic.md, operational.md)
   - `skills/` - Modular capabilities (dry.md, context_hygiene.md)

6. **ContextBuilder** (`src/core/context/context_builder.py`)
   Strict hierarchical prompt assembly with token budgeting:
   - Token quotas: Identity (12%), Role (12%), Tools (6%), Conversation (remaining)
   - Priority drop order: Conversation first, never Identity/Role
   - Truncation marker: `\n\n[TRUNCATED]` for overflow
    - Output format injection for YAML tool blocks
    - Accepts `active_skills` list for dynamic skill injection

7. **Tool Parser** (`src/core/orchestration/tool_parser.py`)
   Parses YAML tool block format only (XML format is deprecated):
   ```yaml
   name: edit_file
   arguments:
     path: src/main.py
     patch: "..."
   ```
   Also supports compact YAML format:
   ```yaml
   edit_file:
     path: src/main.py
     content: "new content"
   ```
   Supports both JSON args and YAML-like key-value lines.
   **NOTE:** XML `<tool>` tags are no longer supported. Agents must use YAML format.

8. **Context Distiller** (`src/core/memory/distiller.py`)
   Distills conversation history to `.agent-context/TASK_STATE.md` every 5 steps:
   - Output: `{"current_task", "completed_steps", "next_step"}`
   - LLM-based summarization with fallback on failure

9. **Loop Prevention**
   - Tracks `execution_trace.json` with tool+args pairs
   - Blocks repeated calls after 3 consecutive identical actions
   - Injects `[LOOP DETECTED]` system message forcing strategy change
   - Trace is cleared at start of each new task to prevent false positives

10. **Infinite Loop Fix (Context Builder)**
   - **CRITICAL:** Code blocks are NO LONGER stripped from conversation history
   - Previously, `_sanitize_text()` removed fenced code blocks with regex, replacing them with `[CODE BLOCK REMOVED]`
   - This caused the agent to lose track of its tool calls, generating the same response repeatedly
   - Fix: Code blocks are now preserved in history, allowing the agent to see its previous tool invocations

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
   - **Interrupt Polling**: cancel_event checked every 0.2s during LLM generation in:
     - `perception_node`
     - `planning_node`
     - `execution_node`
   - Enables responsive UI without blocking socket calls

12. **Agent Brain Configuration** (`src/config/agent-brain/`)
   The agent-brain directory has been relocated from repository root to `src/config/agent-brain/`:
   - `identity/LAWS.md` - Core operating laws
   - `identity/SOUL.md` - Operating principles
   - `roles/strategic.md` - Planning and decomposition role
   - `roles/operational.md` - Execution and tool usage role
   - `roles/analyst.md` - Repository exploration and intelligence gathering
   - `roles/debugger.md` - Debugging and issue analysis
   - `roles/reviewer.md` - Quality assurance and post-verification review
   - `skills/dry.md` - Don't Repeat Yourself skill
   - `skills/context_hygiene.md` - Context management skill

   **Dynamic Skill Injection:**
   - `perception_node`: Injects `context_hygiene` skill when task contains debug/fix/error/search keywords
   - `execution_node`: Injects `dry` skill when `len(relevant_files) > 2`

   **Node-Specific Role Wiring:**
   - `planning_node`: Uses `strategic` role for task decomposition
   - `execution_node`, `perception_node`: Use `operational` role for tool execution
   - `analysis_node`: Uses `analyst` role for repository exploration
   - `debug_node`: Uses `debugger` role for issue analysis
   - `verification_node`, `evaluation_node`: Use `reviewer` role for QA

13. **Tool Registry & Execution** (`src/tools/registry.py` & `file_tools.py`)
   Available capabilities are loaded globally via `registry.py` and the orchestrator's internal `ToolRegistry`. Tools are converted into a dense YAML block `<available_tools>` directly injected into the system prompt.

   **Core Tools:**
   - `list_files`, `read_file`, `write_file`, `edit_file`, `delete_file` - File operations
   - `search_code`, `find_symbol`, `find_references` - Code search (via LanceDB)
   - `initialize_repo_intelligence` - Index repository for semantic search
   - `run_tests`, `run_linter`, `syntax_check` - Verification tools
   - `memory_search` - Search TASK_STATE.md and execution trace
   - `generate_patch`, `apply_patch` - Patch management

14. **Role Configuration** (`src/core/orchestration/role_config.py`)
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

### Dashboard Events (Real-time UI Updates)

The system publishes events for the TUI dashboard:

| Event | Publisher | Payload |
|-------|-----------|---------|
| `file.modified` | orchestrator.execute_tool | `{path, tool, workdir}` |
| `file.deleted` | orchestrator.execute_tool | `{path, workdir}` |
| `tool.execute.start` | orchestrator.execute_tool | `{tool, args, workdir}` |
| `tool.execute.finish` | orchestrator.execute_tool | `{tool, ok, workdir}` |
| `tool.execute.error` | orchestrator.execute_tool | `{tool, error, workdir}` |
| `plan.progress` | execution_node | `{current_step, total_steps, completed}` |
| `verification.complete` | verification_node | `{status, tests, linter, syntax}` |

### Dashboard Widgets

**MainViewController** (`src/ui/views/main_view.py`) subscribes to events and maintains:

- `modified_files`: Dict of file paths with timestamps and actions
- `tool_activity`: List of recent tool calls (max 10)
- `plan_progress`: Current plan execution status
- `verification_status`: Last verification result

The TUI features:
- **Split-pane layout**: 65%/35% with fixed 35-column sidebar
- **Prompt Echo**: User input immediately displayed in chat before processing
- **Interrupt Polling**: ESC cancels mid-generation without UI freeze

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

## Advanced Components

### Toolsets (`src/config/toolsets/`)

Configuration-only toolsets are stored under `src/config/toolsets/` as YAML files. These are static definitions that group tools and set role permissions. At runtime the system prefers this `src/config/toolsets/` location but retains a fallback to `src/tools/toolsets/` for backward compatibility.

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

## Inference Layer

**LLM Bounded Context** (`src/core/inference/`):
The inference module is self-contained within `src/core/inference/`:
- `llm_manager.py` - Provider registry, model discovery, call_model
- `llm_client.py` - LLM client interface
- `adapter_wrappers.py` - Adapter wrappers
- `telemetry.py` - Inference telemetry
- `adapters/` - Provider adapters (lm_studio_adapter.py, ollama_adapter.py)

## Routing and Providers

- **ProviderManager** (`src/core/inference/llm_manager.py`): Abstracts LM Studio, Ollama, or external APIs.
- **ModelRouter**: Predicts payload complexity to toggle between small/fast (7B-9B) vs larger (32B-70B) models.

## Reliability Features

- **Tool Contracts** (`src/core/orchestration/tool_contracts.py`): Pydantic validation for tool results.
- **Deterministic Mode**: Optional temperature=0, seed control for reproducible runs.
- **Loop Prevention**: Duplicate action detection, dead-end detection, retry limits.
- **Cost Tracking**: Tokens, latency, tool calls tracked in usage.json.

## Supported Roles

The system supports five agent roles with tool access control (stored in `src/config/agent-brain/roles/`):

| Role | File | Node | Focus |
|------|------|------|-------|
| `strategic` | strategic.md | planning_node | Task decomposition and planning |
| `operational` | operational.md | perception_node, execution_node | Tool execution and implementation |
| `analyst` | analyst.md | analysis_node | Repository exploration and intelligence |
| `debugger` | debugger.md | debug_node | Debugging and issue analysis |
| `reviewer` | reviewer.md | verification_node, evaluation_node | Quality assurance |

## Recent Bug Fixes

### Infinite Loop Bug
Fixed an issue where the agent would spin indefinitely without executing tools. Root causes addressed:
- **Message Duplication**: LangGraph's `operator.add` reducer was causing exponential message growth (2→4→8→16...) because nodes were mutating history in-place then returning it. Fixed by having nodes return only new messages rather than full history.
- **Tool Results Filtered Out**: ContextBuilder was filtering out "tool" role messages, so the LLM never saw tool execution results. Fixed by changing execution_node to return tool results as "user" role.
- **Empty Responses**: When LM Studio returned empty content, it was being added to history which confused the model. Fixed by skipping empty content.
- **Parsing Bug**: The perception_node had logic that skipped parsing after tool results, preventing valid YAML from being parsed. Fixed by handling `<think>` thinking blocks before YAML code blocks.

### TUI Responsiveness
- **UI Not Updating**: The UI relies on `msg_mgr.append()` to know when to print new content. Added explicit sync calls in perception_node and execution_node to forward LLM output and tool results to TUI.
- **ESC Interrupt**: Fixed interrupt logic that checked `self._agent_thread` but it was never being set. Now properly sets thread reference for cancel event checking.

### Integration Tests
- Fixed import paths in integration tests (src.adapters → src.core.inference.adapters)
- Fixed mocking approach using proper `unittest.mock.patch` targets
