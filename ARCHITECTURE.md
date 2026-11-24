# Architecture

## System Boundary

CodingAgent is a local coding-agent application. Its public entry point is the `codingagent` console script, which resolves to `src.main:main`. It can run a Textual UI, execute a single headless task, or expose an optional local HTTP service. The intended unit of work is a task scoped to a caller-selected repository directory.

The process is not a security-isolated multi-tenant platform. Model providers, MCP servers, shell commands, project hooks, local session stores, and the optional HTTP server all run in the local user's environment.

## Runtime Flow

```text
CLI or Textual UI
        |
        v
src.main -> Orchestrator.run_agent_once() (single async graph invocation)
        |
        v
Unified 6-Node Graph Topology (2026 Canonical Architecture)
        |
        +--> initialize (deterministic ModelRuntimePolicy & capability resolution)
        |       |
        |       v
        +--> agent_loop <--------------------+--------------------+--------------------+
        |       | (outcome)                  | (approved)         | (deferred-rollback | (TurnGroup-atomic
        |       +--> approval ---------------+                    |  surgical retry)   |  compaction)
        |       +--> verify --------------------------------------+                    |
        |       |       | (passed or recovery budget exhausted)                        |
        |       +-------+--> finalize (Transaction Settlement Guard) -> END            |
        |       +--> compact ----------------------------------------------------------+
        |
        v
Orchestrator.execute_tool() (parallel read-only waves, serialized mutations)
        |
        v
Tool execution pipeline
        |
        v
Tool registry -> src/tools implementation
```

The live orchestration pipeline executes a single self-contained invocation of the unified 6-node graph topology (`initialize -> agent_loop -> [approval | verify | compact | finalize] -> END`) compiled by `src/core/orchestration/graph/builder.py:compile_canonical_graph()`. All 2026 model tiers share this identical topology while `ModelRuntimePolicy` (`src/core/orchestration/graph/runtime_policy.py`) scales immutable budgets, flat single-purpose primitive toolsets (`compact-flat` 12 tools for `<8.5B` `nano` and `9-14B` `compact`, `standard-flat` 18 tools for `15-70B` dense & MoE `standard`, `frontier-flat` 25 tools for `frontier`), read-only wave parallelism, and targeted vs. full verification. All legacy cognitive nodes and obsolete topology compilers have been retired.

## Components

### Entry Points And UI

- `src/main.py` parses CLI flags, initializes runtime settings, and selects headless or Textual execution.
- `tui/tui_src/ui/` implements the Textual client. `AgentBridge` is the boundary between UI state and core orchestration/events. `_app_slash_commands_mixin.py` provides interactive slash commands (`/status`, `/doctor`, `/skills`, `/compact`, `/fast`, `/provider`, `/model`) for in-session runtime policy monitoring, local model diagnostics, and skill inspection.
- `src/cli/` implements the headless `session`, `status`, `doctor`, `skill`, `mcp`, and `diff` subcommands.

### Orchestration

- `src/core/orchestration/orchestrator.py` owns the agent instance and exposes `run_agent_once()` and `execute_tool()`.
- `src/core/orchestration/inference_loop.py` builds the run state, resolves provider/model capabilities, embeds immutable runtime policy, invokes the native async `AgentRunner` (`src/core/orchestration/runner.py`) once per task, applies turn limits and cancellation handling, and returns the result.
- `src/core/orchestration/runner.py` executes the canonical 6-stage state machine (`initialize -> agent_loop -> [approval | verify | compact | finalize] -> END`) directly in Python without LangGraph `StateGraph` compilation or channel serialization warnings, enforcing strict `NodeOutputSchema` validation, `merge_or_replace_list` linear history reduction, `recursion_limit`, and redacted crash-recovery snapshots persisted to `SessionStore` / `checkpoint_saver`.
- `src/core/orchestration/graph/` contains slimmed state definitions (`AgentState`), canonical pipeline builder (`builder.py`), deterministic routing (`canonical_routing.py`), checkpointing, and the 6 cognitive stage nodes (with legacy `wait_for_user_node.py`, `memory_update_node.py`, and `dag_parser.py` retired).
- `src/core/orchestration/graph/runtime_policy.py` and `policy.py` define the authoritative target state schema (`runtime`, `run`, `pending_action`, `last_action`, `verification`) and 4-profile `ModelRuntimePolicy` (`nano`, `compact`, `standard`, `frontier`).
- `initialize_node.py` resolves runtime metadata, `ModelRuntimePolicy`, and flat toolset configuration deterministically without LLM calls.
- `frontier_loop_node.py` (aliased as `agent_loop_node`) and `frontier_dispatch.py` execute iterative native-tool loops with structured `assistant.tool_calls` + `role="tool"` history, signed `thinking_blocks` / `reasoning_content` preservation, KV-cache prefix stability, parallel read-only tool waves, `bash` file-mutation detection, duplicate failing-call short-circuiting, and in-loop recovery guidance.
- `approval_node.py` validates action digest integrity, suspends non-blockingly for user/plan approval, and resumes the approved `next_action` cleanly.
- `verification_node.py` and `verification_helpers.py` enforce SHA-256-deduplicated targeted or full polyglot verification (Python, JS/TS `package.json`, Rust `Cargo.toml`, Go `go.mod`, and sandboxed allowlisted `.codingagent/verify.yaml`) with **Deferred Rollback** during bounded recovery retries so models can apply surgical fixes against on-disk edits.
- `compact_node.py` performs 2-stage `TurnGroup`-atomic deterministic compaction (provenance-preserving tool-output stubbing followed by `<context_checkpoint>` turn-group folding) that never splits `assistant.tool_calls` from matching `role="tool"` messages.
- `finalize_node.py` enforces a fail-closed **Transaction Settlement Guard** (committing open snapshots only when verified or rolling back any unverified/failed/cancelled deferred-rollback edits), persists trajectories deterministically, and terminates to `END`.

Graph checkpoints can persist and rehydrate graph state. They improve crash recovery, but do not make arbitrary tool side effects transactional.

### Tool Execution And Safety

`src/core/orchestration/tool_execution_pipeline.py` is the normal enforcement point for model-initiated tool calls, coordinating four single-responsibility stages under `src/core/orchestration/pipeline/` (`preflight.py`, `permissions.py`, `dispatch.py`, `post_hooks.py`). It performs canonicalization, read-before-write and scope checks, permission checks, approval handling, contract validation, rollback snapshots where supported, hooks, timeout handling, event publication, and tracing. String tool output is capped via tier-adaptive truncation scaled dynamically by `ModelRuntimePolicy` (`nano`: 6,000 chars; `compact`: 12,000 chars; `standard`: 24,000 chars; `frontier`: 48,000 chars) before entering history and model context.

`src/tools/` provides the registered functions managed by a single authoritative `ToolRegistry` (`src/tools/_registry.py`; `src/core/orchestration/tool_registry.py` and `registry_builder.py` re-export and delegate directly). Important groups are:

- `_file_io.py`, `_edit_tools.py`, `patch_tools.py`, and `repo_write_tools.py` for workspace modification. Model-facing file edits standardize on three canonical primitives (`write_file`, `edit_file_atomic`, `apply_patch`), while legacy edit aliases (`edit_file`, `edit_by_line_range`, `multiedit`, `edit_code_block`) remain registered and executable with `schema_visibility="alias_only"`.
- `_bash_exec.py`, `bash_security.py`, and `sandbox.py` for command execution. `bash_security.analyze_bash_command()` + quote-aware `split_shell_segments()` is the single authoritative command analyzer across both `tool_preflight.py` and `_bash_exec.py`. Sandboxed execution isolates `/tmp` using `--tmpfs` (avoiding host temp data exposure) and fails closed by default if no backend is available or enforcing, unless explicitly opted out via `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1` or `CODINGAGENT_SANDBOX_LEVEL=off`. When an enforcing OS sandbox is active, `bash()` permits quoted metacharacters and safe read/test pipelines (`|`, `&&`, `||`, `;`) where every parsed segment passes full AST, tier, flag, secret-path, and network checks; unconfined fallback and `bash_readonly()` retain strict operator blocking as a fail-safe.
- `repo_read_tools.py`, `lsp_tools.py`, and `project_tools.py` for repository inspection.
- `verification_tools.py` (including consolidated auto-formatting via `format_file` and `run_formatter`) and `lint_dispatch.py` for validation.
- `memory_tools.py`, `todo_tools.py`, `plan_mode_tools.py`, and `subagent_tools.py` for agent state and workflow support.
- `src/config/toolsets/` (`compact-flat.yaml`, `standard-flat.yaml`, `frontier-flat.yaml`) defines the model-facing flat single-purpose primitive toolsets (12 / 18 / 25 tools) intersected fail-closed with role and `explore_mode` permissions. Track 1 `capability_tools.py` and `formatter.py` have been retired.

`ToolRegistry.call()` routes invocations through guarded orchestrator execution (`Orchestrator.execute_tool()`), ensuring read-before-write validation, workspace confinement, secret redaction, and transactional pre-call snapshots for all mutations (including unlinking newly created files/directories on rollback). Tool discovery fails closed if non-optional built-in modules fail to import, and toolset resolution fails closed if any configured tool is absent.

### Context, Memory, And Repository Intelligence

- `src/core/context/` constructs prompts from task messages, tool descriptions, skills, optional prior-session context, LSP context, and retrieved snippets.
- `src/core/memory/` uses a single canonical SQLite `SessionStore` (WAL + FTS5) with portable secret-redacted JSONL import/export (`jsonl_io.py`), an on-demand `TurnGroup`-atomic `CompactionEngine` (invoked only when context thresholds are crossed or on explicit `/compact`), deterministic first-turn session titles (LLM titles opt-in via `CODINGAGENT_LLM_SESSION_TITLES=1`), and sidecar diagnostics. File-level transactional snapshots and rollback (including lossless binary files) are unified in `RollbackManager`; `JsonlSessionStore`, `GitSnapshotManager`, and `advanced_features.py` have been retired.
- `src/core/indexing/` extracts symbols via `SymbolGraph` (with PageRank importance and call-graph context), maintains a repository index, wires live `LSPClient` `didChange` diagnostics into `ContextBuilder`, and keeps `VectorStore` semantic embeddings strictly opt-in (`CODINGAGENT_ENABLE_VECTOR_STORE=1` or project `vector_store.enabled: true`) so short tasks incur zero embedding/stub overhead.
- `src/config/agent-brain/roles/` and `src/config/agent-brain/skills/` supply prompt-time role and internal skill content. Reusable agent skills packaged for external agent runners (Agensi, Claude Code, Cursor) reside in `src/config/skills/<skill>/SKILL.md`.

### Providers And Evaluation

- `src/core/inference/` manages provider discovery, capability profiles, `KVCacheGovernor` calibration (including Apple Silicon headroom, `num_ctx`, `kv_cache_quant`, and MoE KV scaling), routing, token counting, streaming, retries, fallback, telemetry, and adapters (`AnthropicAdapter`, `OllamaAdapter`, `OpenAICompatAdapter`, `MlxAdapter`, `LlamaServerAdapter`). Anthropic and OpenAI/local adapters support prompt-cache prefix stability via `system_prompt_dynamic_boundary`.
- `src/config/providers.json` contains read-only bundled provider defaults (with inactive presets for local `mlx` and `llama_server`). Runtime active provider, active model, and endpoint overrides are stored exclusively in user-scoped `UserPrefs` (`~/.codingagent/prefs.json`) and layered over bundled defaults at load time.
- `src/core/evaluation/` includes 23 executable-verified scenarios, regression comparisons (failing on dropped baseline scenarios), comparison utilities, and SWE-bench grading harnesses. CI runs deterministic, acceptance, and mock-backed coverage by default; live-provider checks are scheduled or manually enabled.

### Events, Observability, And Server

- `src/core/messaging/` is the typed asynchronous event layer used by the UI bridge and server delivery (`BashApprovalRequired`, `SpawnPermissionRequired`, `ToolExecute*`).
- `src/core/telemetry/`, `src/core/observability/`, and `src/core/logger.py` collect traces (`OtelExporter` and `tracer.py` sharing a single `TracerProvider` and redacting secrets), metrics, and logs. OpenTelemetry support is optional.
- `src/server/` contains an optional FastAPI application for task submission, SSE, WebSockets, scheduler endpoints, and metrics. It is started only when `CODINGAGENT_HTTP_SERVER=true`, defaults to loopback, enforces startup exposure validation, WebSocket Origin validation, and constant-time metrics auth.

## State And Data

Project-local runtime configuration is read from `.codingAgent/`. User-level preferences, provider credentials, session history, checkpoints, indexes, summaries, metrics, and audit/trace records are managed by their respective core services. Never assume session data is safe to expose to an LLM or remote client without redaction.

Project-local configuration merges `.codingAgent/config.json` / `.codingAgent/config.local.json` and `.codingAgent/settings.json` / `.codingAgent/settings.local.json` (in that order). `codingagent init`, `codingagent mcp add`, and active configuration resolution all use `.codingAgent/`; the legacy `.agent` and `.localAgent` names are not used. Diagnostic step timings in `.codingAgent/timings.json` are capped to a bounded ring buffer (default `200` recent entries). Outbound MCP servers started synchronously via `Orchestrator.run_deferred_init_sync()` run on a dedicated persistent daemon event loop owned by `McpServerManager` and torn down cleanly in `Orchestrator.close()`.

## Trust Boundaries

- Provider output is untrusted and must remain subject to tool permissions and validation.
- Repository content, tool output, web content, prior-session memory, MCP responses, and hook output can contain prompt injection.
- Shell commands and project hooks are host-code execution. Sandbox availability is enforced fail-closed by default; commands are denied if no enforcing backend is present unless explicit unconfined opt-out is granted.
- Mutating file, patch, AST, LSP, and `git_restore` tools share a single pre-write guardrail (`src/tools/_file_io.py:_pre_write_check`) enforcing workspace confinement, `WorkspaceGuard`, project `deny_write` patterns, read-before-write, and binary checks before writes. Outbound remote MCP clients (`http`, `sse`, `ws`) validate URLs against SSRF/obfuscated-loopback targets via `src/tools/web_tools.py:validate_mcp_url` and reject unvalidated HTTP redirects.
- The optional server is a local control plane, not a general public API; task workdirs enforce an approved root allowlist (`CODINGAGENT_ALLOWED_WORKSPACE_ROOTS`, defaulting to the server's working directory), queue admission is bounded, and cancelled queued tasks abort before orchestrator construction.

## Design Constraints

- Keep new tools declarative and registered through the existing registry.
- Preserve the central tool execution path for all model-controlled actions.
- Enforce read-before-write, workspace boundaries, explicit permission metadata, and verifiable postconditions for mutations.
- Treat logs, traces, sessions, exports, and HTTP responses as potential secret-exposure paths.
- Add tests at the graph and tool-pipeline boundary whenever changing safety, routing, or completion semantics.
