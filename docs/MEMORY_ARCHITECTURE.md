# Memory & Orchestration Architecture

This document describes the current implementation of memory management and orchestration in the CodingAgent and how it relates to the long-term plan in `memory-implementation.md`.

## 1. Current Memory Management

The system uses a **Hybrid Tiered Memory** approach, combining in-memory conversation tracking with persistent file-based state.

### Tier 1: In-Memory (Short-Term)
- **Component:** `MessageManager` (`src/core/orchestration/message_manager.py`)
- **Function:** Stores the raw chronological conversation history.
- **Truncation:** Enforces a `max_tokens` limit by dropping oldest non-system messages.
- **Token Estimation:** Uses `tiktoken` (GPT-4 encoding) with a regex fallback.

### Tier 2: Working Memory (Persistent Distillation)
- **Location:** `.agent-context/TASK_STATE.md`
- **Component:** `src/core/memory/distiller.py`
- **Function:** Every 5 tool execution rounds, the Orchestrator triggers `distill_context()`. This sends the recent history to a fast LLM to summarize the `current_task`, `completed_steps`, and `next_step`.
- **Purpose:** Allows the agent to "remember" long-term goals even after raw messages are truncated.

### Tier 3: Execution Trace (Long-Term Log)
- **Location:** `.agent-context/execution_trace.json`
- **Function:** Records every tool call, its arguments, and a summary of the result.
- **Persistence:** Serialized to disk using a custom encoder to handle complex objects (like Path).
- **Usage:** Used for **Loop Prevention**. If the same tool/args combination is detected 3 times in the last 5 steps, the Orchestrator blocks execution and forces a strategy change.

## 2. Orchestration: Current vs. LangGraph

### Current State (Orchestrator.py)
The system currently uses a **Linear Adaptive Loop**:
1.  **Context Building:** `ContextBuilder` assembles a tiered XML prompt (Identity > Role > Task > Tools > Conversation).
2.  **Inference:** Single LLM call to get thoughts and tool requests.
3.  **Parsing:** Regex-based XML tool parser extracts actions.
4.  **Execution:** Tools are executed synchronously.
5.  **Feedback:** Results are injected back as `user` messages.
6.  **Looping:** Continues for up to 10 rounds per task.

### Planned State (LangGraph)
The `memory-implementation.md` plan calls for a **7-Node Cognitive Pipeline**:
- **Perception:** Structured JSON facts from input.
- **Understanding:** Goal interpretation.
- **Planning:** Task graph generation (`PLANS.md`).
- **Execution:** Fast-brain tool calling.
- **Verification:** Sandbox execution with AST and Tests.
- **Reflection:** Multi-agent review.
- **Memory Update:** Final persistence.

**Status:** The current `Orchestrator` is a "Node-Ready" monolithic loop. It has the hooks for most of these stages (Distillation for Memory Update, Trace for Reflection, XML parsing for Execution) but does not yet separate them into discrete graph nodes.

## 3. Persistent Memory Optimization

### Handling Documentation & File Changes
To prevent documentation and large file contents from bloating the context:
1.  **Summarization:** When an agent reads a large file, the `MessageManager` or a "Memory Subagent" should summarize the findings into Tier 2 (`TASK_STATE.md`).
2.  **Referencing:** Instead of keeping the full file content in history, the agent should refer to the file by path. If the model needs to "re-read", it uses the tool again, allowing the `ContextBuilder` to handle truncation of the previous read.

## 4. Safety & Operational Rules

The Orchestrator enforces several hard rules to ensure stability and correctness:

### Read-Before-Edit Enforcement
To prevent the agent from making edits based on stale or assumed knowledge, the Orchestrator tracks which files have been read in the current session. An `edit_file` call will be blocked with a programmatic error if the target file has not been `read_file`'d first.

### Sandbox Path Validation (Preflight)
Every tool with a `write` side-effect (e.g., `write_file`, `delete_file`, `edit_file`) undergoes a **Preflight Check**. This ensures that all resolved paths remain strictly within the designated `working_dir`. Any attempt to use `..` or absolute paths to escape the sandbox is blocked and reported as a security violation.

### Tool Result Feedback Loop
All tool executions (success or failure) are wrapped in a standard JSON response format and injected back into the conversation history. This ensures the agent is aware of whether its actions (like file creation or patching) actually succeeded on the physical disk.

## 5. Subagent Implementation

While not currently implemented as separate processes, the Orchestrator can be extended to spawn subagents by:
1.  Creating a specialized `AgentRole` (e.g. `memory_manager.md`).
2.  Calling `run_agent_once` with that role and a subset of tools.
3.  Returning the subagent's findings to the main loop.

## 5. Comparison with `memory-implementation.md`

| Feature | Status | Note |
| :--- | :--- | :--- |
| `.agent-context/` Scaffold | **DONE** | Created on Orchestrator boot. |
| `TASK_STATE.md` | **DONE** | Active and updated via distillation. |
| `ContextBuilder` | **DONE** | Tiered XML prompt assembly implemented. |
| Loop Prevention | **DONE** | Trace-based block implemented. |
| Unified LLM Client | **DONE** | `LLMClient` interface and telemetry. |
| Vector Store (Tier 4) | **PENDING** | Not yet implemented. |
| Symbol Graph (Tier 5) | **PENDING** | AST indexing not yet started. |
| Execution Sandbox | **PARTIAL** | Basic `/tmp/` isolation exists but not full virtualization. |
| LangGraph Pipeline | **PENDING** | Still a monolithic loop in `Orchestrator`. |
