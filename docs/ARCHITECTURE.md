# CodingAgent Architecture

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
   - Executing the primary Action Loop (`run_agent_once`), evaluating responses, validating Tool calls via `preflight_check`, executing them via `execute_tool`, and passing the results back to the LLM context.

3. **Message & Token Manager** (`src/core/orchestration/message_manager.py`)
   Because local models have strict sequence constraints (e.g. 8K tokens), the `MessageManager` tracks history and automatically shifts the sliding window (dropping oldest non-system interactions) when the conversation exceeds `max_tokens`.

4. **Agent Brain & Doctrine** (`src/core/orchestration/agent_brain.py`)
   Defines the agent's fundamental behaviors and constraints. The `load_system_prompt()` dynamically compiles a master system prompt at runtime using:
   - The selected Persona (e.g., `agents/coding_agent.md`) mapped to the `<system_role>` tag.
   - Core operating laws from `LAWS.md` mapped to `<core_laws>`.
   - Identity doctrine from `SOUL.md` mapped to `<operating_principles>`.
   - A rigid Output Schema requirement enforcing structured JSON envelopes for robust multi-step reasoning.

5. **Tool Registry & Execution** (`src/tools/registry.py` & `file_tools.py`)
   Available capabilities are loaded globally via `registry.py` and the orchestrator's internal `ToolRegistry`. Instead of bleeding tokens via nested OpenAI-compatible JSON Schemas for local models, the tools are converted into a dense, TypeScript-like XML block `<available_tools>` directly injected into the system prompt.

6. **LangGraph Subagents** (`src/core/orchestration/langgraph_node.py`)
   Provides a stateless runner loop for delegated subagents (like planners or codebase explorers). It receives an isolated context, executes `call_model`, extracts JSON structures securely, iterates on tool usage independently of the main thread, and yields a finalized task payload.

## Event System

Telemetry, UI updates, and Provider health statuses are entirely decoupled via `EventBus` (`src/core/orchestration/event_bus.py`).
- **Topics:** `orchestrator.startup`, `provider.models.cached`, `ui.notification`, `model.routing`, etc.
- **Subscribers:** The UI or telemetry loggers can freely bind to the single EventBus to react to deep underlying tool errors or LLM network timeouts without directly coupling to the logic layer.

## Routing and Providers

- **ProviderManager** (`src/core/llm_manager.py`): Abstracts whether the user has LM Studio, Ollama, or an external API connected. Scans and probes health via background threads.
- **ModelRouter**: Predicts payload complexity by estimating word counts to intelligently toggle between small/fast (7B-9B) vs larger models (32B-70B) natively.
