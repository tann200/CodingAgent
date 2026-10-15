# Architecture

## System Boundary

CodingAgent is a local coding-agent application. Its public entry point is the `codingagent` console script, which resolves to `src.main:main`. It can run a Textual UI, execute a single headless task, or expose an optional local HTTP service. The intended unit of work is a task scoped to a caller-selected repository directory.

The process is not a security-isolated multi-tenant platform. Model providers, MCP servers, shell commands, project hooks, local session stores, and the optional HTTP server all run in the local user's environment.

## Runtime Flow

```text
CLI or Textual UI
        |
        v
src.main -> Orchestrator.run_agent_once()
        |
        v
Tier graph factory
  lite/small: reduced graph
  other tiers: frontier graph
        |
        +--> perception
        +--> analysis for complex tasks
        +--> frontier LLM/tool loop
        +--> verification and evaluation
        +--> memory synchronization
        |
        v
Orchestrator.execute_tool()
        |
        v
Tool execution pipeline
        |
        v
Tool registry -> src/tools implementation
```

The live tier factory is selected from `src/core/orchestration/inference_loop.py`. The `frontier` route is the default live workflow for tiers other than `lite` and `small`. The older full graph remains implemented in `src/core/orchestration/graph/builder.py`, but `_USE_FULL_GRAPH` is disabled; its explicit planning, step control, replan, debug, and delegation nodes are therefore not the normal live path.

## Components

### Entry Points And UI

- `src/main.py` parses CLI flags, initializes runtime settings, and selects headless or Textual execution.
- `tui/tui_src/ui/` implements the Textual client. `AgentBridge` is the boundary between UI state and core orchestration/events.
- `src/cli/` implements the headless `session`, `status`, `mcp`, and `diff` subcommands.

### Orchestration

- `src/core/orchestration/orchestrator.py` owns the agent instance and exposes `run_agent_once()` and `execute_tool()`.
- `src/core/orchestration/inference_loop.py` builds the run state, selects a graph, applies limits and cancellation handling, and returns the result.
- `src/core/orchestration/graph/` contains state definitions, graph builders, routing, checkpointing, and nodes.
- `frontier_loop_node.py` performs iterative provider calls and translates provider output into centralized tool executions.
- `verification_node.py`, `evaluation_node.py`, and `memory_update_node.py` process the result after the loop.

Graph checkpoints can persist and rehydrate graph state. They improve crash recovery, but do not make arbitrary tool side effects transactional.

### Tool Execution And Safety

`src/core/orchestration/tool_execution_pipeline.py` is the normal enforcement point for model-initiated tool calls. It performs canonicalization, read-before-write and scope checks, permission checks, approval handling, contract validation, rollback snapshots where supported, hooks, timeout handling, event publication, and tracing.

`src/tools/` provides the registered functions. Important groups are:

- `_file_io.py`, `_edit_tools.py`, `patch_tools.py`, and `repo_write_tools.py` for workspace modification.
- `_bash_exec.py`, `bash_security.py`, and `sandbox.py` for command execution.
- `repo_read_tools.py`, `lsp_tools.py`, and `project_tools.py` for repository inspection.
- `verification_tools.py`, `lint_dispatch.py`, and `formatter.py` for validation.
- `memory_tools.py`, `todo_tools.py`, `plan_mode_tools.py`, and `subagent_tools.py` for agent state and workflow support.

Direct `ToolRegistry.call()` does not provide the complete pipeline guarantees. New application integrations must invoke tools through the orchestrator pipeline, not the registry directly.

### Context, Memory, And Repository Intelligence

- `src/core/context/` constructs prompts from task messages, tool descriptions, skills, optional prior-session context, LSP context, and retrieved snippets.
- `src/core/memory/` provides JSONL and SQLite session stores, summaries/distillation, compaction, snapshots, and sidecar diagnostics.
- `src/core/indexing/` extracts symbols and maintains a repository index, with optional embedding support. Current fallback retrieval is symbol and path oriented rather than a complete semantic code-chunk index.
- `src/config/agent-brain/roles/` and `src/config/agent-brain/skills/` supply prompt-time role and skill content.

### Providers And Evaluation

- `src/core/inference/` manages provider discovery, capability profiles, routing, token counting, streaming, retries, fallback, telemetry, and adapters.
- `src/config/providers.json` contains provider definitions.
- `src/core/evaluation/` includes scenario, regression, comparison, and SWE-bench-oriented utilities. CI runs deterministic and mock-backed coverage by default; live-provider checks are scheduled or manually enabled.

### Events, Observability, And Server

- `src/core/messaging/` is the typed asynchronous event layer used by the UI bridge and server delivery.
- `src/core/telemetry/`, `src/core/observability/`, and `src/core/logger.py` collect traces, metrics, and logs. OpenTelemetry support is optional.
- `src/server/` contains an optional FastAPI application for task submission, SSE, WebSockets, scheduler endpoints, and metrics. It is started only when `CODINGAGENT_HTTP_SERVER=true` and defaults to loopback.

## State And Data

Project-local runtime configuration is read from `.codingAgent/`. User-level preferences, provider credentials, session history, checkpoints, indexes, summaries, metrics, and audit/trace records are managed by their respective core services. Never assume session data is safe to expose to an LLM or remote client without redaction.

Project-local configuration is resolved from `.codingAgent/config.json`. `codingagent init`, `codingagent mcp add`, and active configuration resolution all use that directory; the legacy `.agent` and `.localAgent` names are not used.

## Trust Boundaries

- Provider output is untrusted and must remain subject to tool permissions and validation.
- Repository content, tool output, web content, prior-session memory, MCP responses, and hook output can contain prompt injection.
- Shell commands and project hooks are host-code execution. Sandbox availability must be verified, not assumed.
- The optional server is a local control plane, not a general public API; task workdirs enforce an approved root allowlist (`CODINGAGENT_ALLOWED_WORKSPACE_ROOTS`, defaulting to the server's working directory), queue admission is bounded, and cancelled queued tasks abort before orchestrator construction.

## Design Constraints

- Keep new tools declarative and registered through the existing registry.
- Preserve the central tool execution path for all model-controlled actions.
- Enforce read-before-write, workspace boundaries, explicit permission metadata, and verifiable postconditions for mutations.
- Treat logs, traces, sessions, exports, and HTTP responses as potential secret-exposure paths.
- Add tests at the graph and tool-pipeline boundary whenever changing safety, routing, or completion semantics.
