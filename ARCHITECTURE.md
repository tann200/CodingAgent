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
Unified 7-Node Graph Topology (Canonical Architecture)
        |
        +--> initialize (deterministic tier & immutable policy resolution)
        |       |
        |       v
        +--> agent_loop <--------------------+---------------+---------------+
        |       | (outcome)                  | (approved)    | (replacement) | (compacted)
        |       +--> approval ---------------+               |               |
        |       +--> recover --------------------------------+               |
        |       +--> compact ------------------------------------------------+
        |       +--> verify
        |       |       | (failed & budget)
        |       |       +--> recover
        |       | (passed / terminal)
        |       v
        +--> finalize -> END
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

The live orchestration pipeline uses the unified 7-node graph topology (`initialize -> agent_loop -> [approval | verify | recover | compact | finalize] -> END`) compiled by `src/core/orchestration/graph/builder.py:compile_canonical_graph()`. Both 15-30B medium and frontier models share this identical functional topology; tier classification determines immutable budgets, allowed capabilities (`medium-core` 18 tools vs `frontier-full` 27 tools), and execution policies rather than topology divergence. Legacy topology compilers remain available via `CODINGAGENT_UNIFIED_GRAPH=0` for transitional test harnesses.

## Components

### Entry Points And UI

- `src/main.py` parses CLI flags, initializes runtime settings, and selects headless or Textual execution.
- `tui/tui_src/ui/` implements the Textual client. `AgentBridge` is the boundary between UI state and core orchestration/events.
- `src/cli/` implements the headless `session`, `status`, `mcp`, and `diff` subcommands.

### Orchestration

- `src/core/orchestration/orchestrator.py` owns the agent instance and exposes `run_agent_once()` and `execute_tool()`.
- `src/core/orchestration/inference_loop.py` builds the run state, resolves provider/model tier, embeds immutable runtime policy, compiles the canonical graph, applies turn limits and cancellation handling, and returns the result.
- `src/core/orchestration/graph/` contains state definitions, canonical graph builder, routing, checkpointing, and cognitive nodes.
- `src/core/orchestration/graph/policy.py` defines the authoritative target state schema (`runtime`, `run`, `pending_action`, `last_action`, `verification`) and tier policies.
- `initialize_node.py` resolves runtime metadata and prepares state without LLM calls.
- `frontier_loop_node.py` (aliased as `agent_loop_node`) performs iterative model-tool loops and emits normalized `last_action` envelopes.
- `approval_node.py` validates action digest integrity and gates modifying actions.
- `verification_node.py` enforces evidence-based validation for every mutation regardless of tier.
- `recover_node.py` unifies replanning and debugging driven by error classification and failed action signatures, rejecting duplicate failed signatures.
- `compact_node.py` performs deterministic-first history reduction with bounded compaction limits.
- `finalize_node.py` synchronizes durable memory, writes trajectories, and terminates the pipeline without re-entering the agent loop.

Graph checkpoints can persist and rehydrate graph state. They improve crash recovery, but do not make arbitrary tool side effects transactional.

### Tool Execution And Safety

`src/core/orchestration/tool_execution_pipeline.py` is the normal enforcement point for model-initiated tool calls. It performs canonicalization, read-before-write and scope checks, permission checks, approval handling, contract validation, rollback snapshots where supported, hooks, timeout handling, event publication, and tracing.

`src/tools/` provides the registered functions. Important groups are:

- `_file_io.py`, `_edit_tools.py`, `patch_tools.py`, and `repo_write_tools.py` for workspace modification.
- `_bash_exec.py`, `bash_security.py`, and `sandbox.py` for command execution. Sandboxed execution isolates `/tmp` using `--tmpfs` (avoiding host temp data exposure) and fails closed by default if no backend is available or enforcing, unless explicitly opted out via `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1` or `CODINGAGENT_SANDBOX_LEVEL=off`.
- `repo_read_tools.py`, `lsp_tools.py`, and `project_tools.py` for repository inspection.
- `verification_tools.py`, `lint_dispatch.py`, and `formatter.py` for validation.
- `memory_tools.py`, `todo_tools.py`, `plan_mode_tools.py`, and `subagent_tools.py` for agent state and workflow support.
- `_capabilities.py` and `capability_tools.py` define the consolidated 27 target capability tools, strict schemas, operation enums, tier assignments (`medium-core` 18 tools, `frontier-full` 27 tools), and backward-compatible adapters for all legacy tool names.

`ToolRegistry.call()` routes invocations through guarded orchestrator execution (`Orchestrator.execute_tool()`), ensuring read-before-write validation, workspace confinement, secret redaction, and transactional pre-call snapshots for all mutations (including unlinking newly created files/directories on rollback). Tool discovery fails closed if non-optional built-in modules fail to import, and toolset resolution fails closed if any configured tool is absent.

### Context, Memory, And Repository Intelligence

- `src/core/context/` constructs prompts from task messages, tool descriptions, skills, optional prior-session context, LSP context, and retrieved snippets.
- `src/core/memory/` provides JSONL and SQLite session stores, summaries/distillation, compaction, snapshots, and sidecar diagnostics.
- `src/core/indexing/` extracts symbols and maintains a repository index, with optional embedding support. Current fallback retrieval is symbol and path oriented rather than a complete semantic code-chunk index.
- `src/config/agent-brain/roles/` and `src/config/agent-brain/skills/` supply prompt-time role and skill content.

### Providers And Evaluation

- `src/core/inference/` manages provider discovery, capability profiles, routing, token counting, streaming, retries, fallback, telemetry, and adapters. Anthropic adapter maps natively to `/v1/messages` with prompt-cache boundary support, required token budgeting, tool schema conversion, and response normalization.
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
- Shell commands and project hooks are host-code execution. Sandbox availability is enforced fail-closed by default; commands are denied if no enforcing backend is present unless explicit unconfined opt-out is granted.
- The optional server is a local control plane, not a general public API; task workdirs enforce an approved root allowlist (`CODINGAGENT_ALLOWED_WORKSPACE_ROOTS`, defaulting to the server's working directory), queue admission is bounded, and cancelled queued tasks abort before orchestrator construction.

## Design Constraints

- Keep new tools declarative and registered through the existing registry.
- Preserve the central tool execution path for all model-controlled actions.
- Enforce read-before-write, workspace boundaries, explicit permission metadata, and verifiable postconditions for mutations.
- Treat logs, traces, sessions, exports, and HTTP responses as potential secret-exposure paths.
- Add tests at the graph and tool-pipeline boundary whenever changing safety, routing, or completion semantics.
