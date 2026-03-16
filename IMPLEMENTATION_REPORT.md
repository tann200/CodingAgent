# CodingAgent Implementation Report

This document provides a comprehensive overview of the CodingAgent system for architectural review and roadmap planning.

---

# 1. Repository Overview

*   **Repository Purpose:** An autonomous software engineering agent designed to operate on a local codebase. It uses local LLMs to understand tasks, use tools to interact with the filesystem, and solve software development problems.
*   **Current Development Stage:** The core MVP is complete. The system has been refactored from a monolithic `while` loop to a `LangGraph`-driven state machine that enforces operational workflows.
*   **Supported Models:** Primarily local models served via **LM Studio** and **Ollama**.
*   **Programming Language:** Python 3.9+
*   **Main Dependencies:** `langgraph`, `textual`, `fastapi`, `uvicorn`, `pytest`.

## Repository Structure

The repository is structured with a clear separation of concerns: core logic, UI, tools, and agent configuration.

```text
Repository: CodingAgent

├── agent-brain
│   ├── identity
│   │   ├── LAWS.md
│   │   └── SOUL.md
│   ├── roles
│   │   ├── operational.md
│   │   └── strategic.md
│   └── skills
│       ├── context_hygiene.md
│       └── dry.md
├── docs
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── FINAL_AUDIT_REPORT.md
│   ├── memory-implementation.md
│   ├── MEMORY_ARCHITECTURE.md
│   ├── mvp-tasklist.md
│   ├── NEW_AUDIT_INSTRUCTIONS.md
│   ├── system_capability_report.md
│   ├── system_map.md
│   ├── tooloptimization.md
│   ├── tuispec.md
│   └── unified_plan.md
├── scripts
│   ├── add_provider.py
│   ├── analyze_tokens.py
│   ├── check_providers_and_models.py
│   ├── diagnose_lmstudio.py
│   ├── fetch_ollama.py
│   ├── generate_system_map.py
│   ├── list_prompts.py
│   ├── run_generate.py
│   ├── run_tests_settings.py
│   ├── run_tui.py
│   ├── simulate_tui.py
│   ├── start_tui.py
│   ├── test_agent_stability.py
│   ├── test_langgraph_node.py
│   ├── test_llm_stability.py
│   ├── test_real_lmstudio.py
│   ├── test_real_lmstudio_file_edit.py
│   ├── test_tools.py
│   ├── tree.json
│   ├── validate_ollama.py
│   └── wait_for_model.py
├── src
│   ├── adapters
│   │   ├── lm_studio_adapter.py
│   │   └── ollama_adapter.py
│   ├── config
│   │   └── providers.json
│   ├── core
│   │   ├── context
│   │   │   └── context_builder.py
│   │   ├── inference
│   │   │   ├── __init__.py
│   │   │   ├── adapter_wrappers.py
│   │   │   ├── llm_client.py
│   │   │   └── telemetry.py
│   │   ├── memory
│   │   │   └── distiller.py
│   │   ├── orchestration
│   │   │   ├── graph
│   │   │   │   ├── nodes
│   │   │   │   │   └── ...
│   │   │   │   ├── builder.py
│   │   │   │   └── state.py
│   │   │   ├── agent_brain.py
│   │   │   ├── event_bus.py
│   │   │   ├── message_manager.py
│   │   │   ├── orchestrator.py
│   │   │   ├── schema.json
│   │   │   └── tool_parser.py
│   │   ├── telemetry
│   │   │   ├── consumer.py
│   │   │   └── metrics.py
│   │   ├── llm_manager.py
│   │   ├── logger.py
│   │   ├── startup.py
│   │   └── user_prefs.py
│   ├── data
│   ├── tools
│   │   ├── file_tools.py
│   │   ├── registry.py
│   │   └── system_tools.py
│   ├── ui
│   │   ├── components
│   │   │   ├── __init__.py
│   │   │   └── log_panel.py
│   │   ├── styles
│   │   │   ├── main.tcss
│   │   │   └── README.md
│   │   ├── views
│   │   │   ├── __init__.py
│   │   │   ├── main_view.py
│   │   │   ├── provider_panel.py
│   │   │   └── settings_panel.py
│   │   ├── __init__.py
│   │   ├── app.py
│   │   ├── README.md
│   │   ├── textual_app.py
│   │   └── textual_app_impl.py
│   ├── main.py
│   └── tmp_app_started.log
├── pyproject.toml
├── README.md
├── requirements.txt
```

---

# 2. Core Architecture

The system uses a `LangGraph`-based cognitive pipeline managed by a central `Orchestrator`. This design separates reasoning, execution, and memory management into discrete, enforceable steps.

```
+----------------+      +---------------------+      +----------------+
|    User Task   |----->|     Orchestrator    |----->|      TUI       |
+----------------+      | (run_agent_once)    |      +----------------+
                        +---------------------+
                                  |
                                  v
+--------------------------------------------------------------------+
|                      LangGraph Cognitive Pipeline                  |
|                                                                    |
|  +----------------+      +-----------------+      +---------------+ |
|  | PerceptionNode |----->|  ExecutionNode  |----->|  MemoryNode   | |
|  | (LLM Call)     |      |  (Tool Use)     |      | (Distillation)| |
|  +----------------+      +-----------------+      +---------------+ |
|         ^                        | (Enforces Rules)        |         |
|         |                        |                         |         |
|         +------------------------+-------------------------+         |
|                  (Loop for next step)                                |
+--------------------------------------------------------------------+
```

*   **Entry Point:** `main.py` launches `src/ui/app.py`, which instantiates the `Orchestrator` and the `Textual`-based TUI.
*   **Agent Loop:** `Orchestrator.run_agent_once` invokes the LangGraph state machine. The graph cycles through `Perception` (thinking), `Execution` (acting), and `Memory` (remembering).
*   **Model Interaction:** The `PerceptionNode` uses `llm_manager.py` to call the active LLM (e.g., LM Studio). `ContextBuilder` assembles a token-budgeted prompt.
*   **Filesystem Interaction:** The `ExecutionNode` calls tools from `file_tools.py` which are sandboxed to the specified `working_dir`.

---

# 3. Agent Loop Implementation

*   **File Location:** `src/core/orchestration/orchestrator.py`
*   **Main Class:** `Orchestrator`
*   **Execution Flow:** The `run_agent_once` method is now a lightweight wrapper that prepares the initial state and invokes the LangGraph pipeline. The core logic resides in the graph nodes.

The state machine cycles through three main nodes:
1.  **`perception_node`:** Assembles the context and calls the LLM to get the next thought or tool call.
2.  **`execution_node`:** Validates and runs the requested tool, enforcing runtime rules like "Read-Before-Edit".
3.  **`memory_update_node`:** Summarizes the history into `TASK_STATE.md` for long-term memory.

### Code Excerpt: The Core Graph Logic

This excerpt from `src/core/orchestration/graph/nodes/workflow_nodes.py` shows how the `execution_node` programmatically enforces the "Read-Before-Edit" rule.

```python
# src/core/orchestration/graph/nodes/workflow_nodes.py

async def execution_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Execution Layer: Programmatically enforces Operational Workflows.
    Specifically: Read-Before-Edit and Sandbox constraints.
    """
    orchestrator = config.get("configurable", {}).get("orchestrator")
    action = state["next_action"]
    
    if not action:
        return {"last_result": None}
        
    tool_name = action["name"]
    args = action.get("arguments", {})
    path_arg = args.get("path") or args.get("file_path")
    
    # Workflow Enforcement 1: Read-Before-Edit
    if tool_name == "edit_file" and path_arg:
        try:
            resolved = str((Path(state["working_dir"]) / path_arg).resolve())
            # Check both the state (immutability) AND the orchestrator (current session)
            if resolved not in state["verified_reads"] and resolved not in orchestrator._session_read_files:
                err_msg = f"Logic violation: You must read '{path_arg}' before editing it."
                return {
                    "last_result": {"ok": False, "error": err_msg},
                    "history": [{"role": "user", "content": json.dumps({"tool_execution_result": {"ok": False, "error": err_msg}})}],
                    "next_action": None # Reset to force re-planning
                }
        except Exception:
            pass
            
    # Workflow Enforcement 2: Sandbox Preflight
    preflight = orchestrator.preflight_check(action)
    if not preflight.get("ok"):
        return {
            "last_result": preflight,
            "history": [{"role": "user", "content": f"[SANDBOX VIOLATION] {preflight.get('error')}"}],
            "next_action": None
        }

    # Execute tool
    res = orchestrator.execute_tool(action)
    
    # ... (rest of the function)
```

*   **Step Limit:** Yes, a hard-coded safety cap of 15 rounds per task.
*   **Timeout:** Yes, network timeouts are handled at the `LLMClient` adapter level (default is 120s).
*   **Failure Handling:** Failures (like the `Logic violation` above) are caught, converted into structured error messages, and injected back into the agent's history to force it to self-correct.

---

# 4. Model Integration

*   **Provider:** The system is designed for local models and supports **LM Studio** and **Ollama** out-of-the-box via adapters.
*   **API Wrapper:** A unified `LLMClient` interface is defined in `src/core/inference/llm_client.py`. Adapters like `src/adapters/lm_studio_adapter.py` implement this interface.
*   **Prompt Construction:** `src/core/context/context_builder.py` assembles a tiered prompt using XML tags (`<identity>`, `<role>`, `<available_tools>`, etc.) for maximum compatibility with instruction-tuned models.
*   **Token Limits:** The `ContextBuilder` enforces a hard token budget, dropping the oldest conversational turns first to prevent context overflow.

### Code Excerpt: Model Interface

```python
# src/core/inference/llm_client.py

class LLMClient(ABC):
    """Abstract base class for a generic LLM client, providing a unified interface."""

    @abstractmethod
    def generate(self,
                 messages: List[Dict[str, str]],
                 model: Optional[str] = None,
                 stream: bool = False,
                 temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None,
                 top_p: Optional[float] = None,
                 stop: Optional[List[str]] = None,
                 **kwargs) -> Union[Dict[str, Any], Iterator[Dict[str, Any]]]:
        """Generate a response from the model."""
        pass
```

---

# 5. Tool System

The agent interacts with the environment exclusively through a registry of tools.

| Tool Name     | Purpose                                   | Input Format                             | Output Format                            | File Location             |
|---------------|-------------------------------------------|------------------------------------------|------------------------------------------|---------------------------|
| `list_files`  | List files in a directory                 | `{"path": "directory_path"}`             | `{"status": "ok", "items": [...]}`       | `file_tools.py`           |
| `read_file`   | Read the full contents of a file          | `{"path": "file_path"}`                  | `{"status": "ok", "content": "..."}`     | `file_tools.py`           |
| `write_file`  | Create or overwrite a file                | `{"path": "file_path", "content": "..."}`| `{"status": "ok", "path": "..."}`        | `file_tools.py`           |
| `edit_file`   | Apply a unified diff patch to a file      | `{"path": "file_path", "patch": "..."}`  | `{"status": "ok", "path": "..."}`        | `file_tools.py`           |
| `delete_file` | Delete a file or directory                | `{"path": "path_to_delete"}`             | `{"status": "ok"}`                       | `file_tools.py`           |

*   **Registration:** Tools are registered in the `Orchestrator`'s `ToolRegistry`. The default `example_registry()` function wires up the core file system tools.
*   **Selection:** The agent selects a tool by generating a specific XML block (`<tool>\nname: ...\nargs: {...}\n</tool>`), which is parsed from its response by `src/core/orchestration/tool_parser.py`.
*   **Responses:** All tool responses are structured as JSON dictionaries, indicating success (`"ok": true`) or failure and providing a result or an error message.

---

# 6. Memory System

The system uses a hybrid, multi-tiered memory architecture to achieve session persistence and handle long contexts.

*   **Working Memory (In-Memory):** A `MessageManager` list holds the raw `user` and `assistant` messages for the current session. It is the first to be truncated when token limits are reached.
*   **Episodic Memory (File-based):**
    *   **`execution_trace.json`:** A log of every tool call, its arguments, and a summary of the result. It is written after every successful tool execution. It is read by the **Loop Prevention** mechanism.
    *   **`TASK_STATE.md`:** A distilled summary of the conversation history. A background process (`distill_context`) runs after each tool call to summarize the state (`Current Task`, `Completed Steps`, `Next Step`), preventing context loss.
*   **Semantic Memory:** Not yet implemented. The plan is to use a vector store like LanceDB.

### Code Excerpt: Memory Write (Distillation)

```python
# src/core/memory/distiller.py

def distill_context(messages: List[Dict[str,str]], ..., working_dir: Optional[Path] = None) -> Dict[str, Any]:
    # ... (LLM call to summarize messages) ...
    
    if distilled_state and working_dir:
        try:
            task_state_path = working_dir / ".agent-context" / "TASK_STATE.md"
            lines = [
                "# Current Task",
                distilled_state.get("current_task", "None"),
                "",
                "# Completed Steps"
            ]
            for step in distilled_state.get("completed_steps", []):
                lines.append(f"- {step}")
            lines.extend(["", "# Next Step", distilled_state.get("next_step", "None")])
            
            task_state_path.write_text("\n".join(lines))
        except Exception as e:
            logger.error(f"Failed to write TASK_STATE.md: {e}")
```

---

# 7. Repository Interaction

*   **File Reading/Editing:** Done exclusively through the `read_file`, `write_file`, and `edit_file` tools.
*   **Diff Generation:** The agent is expected to generate unified diffs for the `edit_file` tool.
*   **Commit Handling:** Not implemented.
*   **Repo Search:** A basic `grep` tool exists in `system_tools.py` but is not enabled by default.

---

# 8. Execution Environment

*   **Shell Execution:** No general-purpose shell tool is exposed by default for security.
*   **Test Execution:** Not implemented as a dedicated tool.
*   **Sandboxing:** Yes. All tools in `file_tools.py` operate within a sandboxed `working_dir` (defaults to `./output`). The `_safe_resolve` function prevents path traversal attacks (`../`).

---

# 9. Error Handling

*   **Tool Failures:** The `execute_tool` wrapper in the `Orchestrator` catches all exceptions and returns a structured error `{"ok": False, "error": "..."}`.
*   **Runtime Exceptions:** The LangGraph invocation in `run_agent_once` is wrapped in a `try...except` block that logs failures and returns an error to the caller.
*   **Model Errors:** Adapters are responsible for catching HTTP errors or API failures and returning structured error responses.
*   **Retry Logic:** The primary form of retry is the "Read-Before-Edit" rule enforced by the graph. If a tool fails, the error is passed back to the LLM, giving it an opportunity to self-correct on the next turn.

---

# 10. Observability

*   **Logs:** The system uses standard Python logging, configured in `src/core/logger.py`.
*   **Step Traces:** Agent thoughts are extracted from `<think>` tags and displayed in the TUI.
*   **Tool Traces:** `output/.agent-context/execution_trace.json` provides a persistent, machine-readable log of all tool calls.
*   **Token Usage:** The `Orchestrator` emits `model.usage` events, which are captured and displayed by the TUI.

---

# 11. Current Limitations

*   **No Code Indexing:** The agent lacks semantic understanding of the codebase. It cannot "go to definition" or "find references."
*   **No True Sandbox:** File system operations are sandboxed, but the agent cannot safely execute code it generates.
*   **Monolithic Graph:** The current graph is linear. It lacks sophisticated planning, branching, or multi-agent review loops.
*   **Basic Memory Retrieval:** Memory is based on recent conversation and a simple distilled state file. There is no mechanism to retrieve information from older sessions or based on semantic similarity.

---

# 12. Future Architecture Plans

The long-term vision is documented in `docs/memory-implementation.md` and involves evolving into a multi-agent, hub-and-spoke architecture.

*   **Cognitive Pipeline:** Refactor the current linear graph into the full 7-stage pipeline (Perception, Understanding, Planning, Execution, Verification, Reflection, Memory Update).
*   **Multi-Agent Coordination:** Introduce specialized agents for different roles (e.g., a "Planner" that generates the task list and a "Reviewer" that validates code changes).
*   **Advanced Memory:** Implement Tier 4 (Vector Store for semantic facts) and Tier 5 (AST-based Symbol Graph for code intelligence).
*   **Execution Sandbox:** Build a proper sandbox for executing generated code and tests safely.

---

# 13. Critical Files

*   `src/core/orchestration/orchestrator.py`: The main class that manages the agent and its lifecycle.
*   `src/core/orchestration/graph/builder.py`: Defines the LangGraph state machine topology.
*   `src/core/orchestration/graph/nodes/workflow_nodes.py`: Contains the core logic for the Perception, Execution, and Memory nodes.
*   `src/core/context/context_builder.py`: Responsible for all prompt engineering and token budgeting.
*   `src/tools/file_tools.py`: Implements all filesystem interactions and sandboxing.
*   `src/main.py`: The application entrypoint.

---

# 14. Execution Example

**Input Task:** `"Please edit TEST.md. Change its content to 'new content'."` (Assuming `TEST.md` contains `"initial content"`).

### Agent Execution Trace

1.  **Perception:** LLM decides to edit the file directly.
    *   **LLM Output:** `<tool>name: edit_file, args: {"path": "TEST.md", ...}</tool>`
2.  **Execution (Rejected):** The `execution_node` checks its `verified_reads` state. Since `TEST.md` has not been read, it blocks the action.
    *   **Tool Result:** `{"ok": False, "error": "Logic violation: You must read 'TEST.md' before editing it."}`
3.  **Perception:** The agent receives the error and re-evaluates.
    *   **LLM Output:** `<think>I made a mistake. I must read the file first.</think><tool>name: read_file, args: {"path": "TEST.md"}</tool>`
4.  **Execution (Success):** The `execution_node` runs `read_file`. The file's path is added to `verified_reads`.
    *   **Tool Result:** `{"ok": True, "result": {"status": "ok", "content": "initial content"}}`
5.  **Perception:** The agent now has the file content and can create a valid patch.
    *   **LLM Output:** `<tool>name: edit_file, args: {"path": "TEST.md", "patch": "..."}</tool>`
6.  **Execution (Success):** The `execution_node` verifies that `TEST.md` is in `verified_reads` and allows the edit to proceed.
    *   **Tool Result:** `{"ok": True, "result": {"status": "ok"}}`
7.  **Perception:** The agent confirms the task is complete.
    *   **LLM Output:** `The file TEST.md has been updated.`
8.  **End.**
