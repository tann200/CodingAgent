# Gap Analysis: CodingAgent vs. claw-code-main (TS) & opencode

**Date:** 2026-04-03
**Scope:** All dimensions relevant to an open-source coding agent system with proper orchestration, tools, determinism, and scalability from 9B models to frontier models.
**Reference systems:**
- **CodingAgent** — `/Users/tann200/PycharmProjects/CodingAgent/src/` (this project)
- **claw-code (TS original)** — `/Users/tann200/PycharmProjects/claw-code-main/archive/claw_code_ts_snapshot/` (the TypeScript source behind the Python port scaffold)
- **opencode** — `/Users/tann200/WebstormProjects/opencode/` (TypeScript/Go, Vercel AI SDK v5)

---

## Executive Summary

CodingAgent has a strong architectural skeleton — a LangGraph state machine, a well-structured tool registry, tiered prompt assembly, and a working TUI bridge. Against the two reference systems it has clear, well-bounded gaps in six critical areas: **(1) model-tier routing, (2) MCP client capability, (3) structured session events / event sourcing, (4) LSP integration, (5) context-window token accuracy, and (6) UI interactivity / permission UX.** None of these gaps are architectural rewrites; all are additive or corrective extensions on top of the existing foundation. The mitigation plan at the end of this document provides a prioritised sprint-based execution path.

---

## 1. Orchestration

### CodingAgent
- **LangGraph `StateGraph`** compiled to singleton; 15 async nodes; 12 named router functions
- Turn guards: `max_turns`, `max_tool_calls`, `step_retry_counts`, doom-loop fingerprint ring buffer
- DAG parser (`dag_parser.py`) + `execution_waves` for PRSW scheduling; async file-lock manager
- Plan mode gate (write tools blocked until user approves); preview mode (diff before commit)
- Fast-path routing: `_task_is_complex()` skips 6 LLM calls for simple tasks
- `cancel_event` polled every 0.2s; propagated through all LLM calls

### claw-code (TS)
- Full recursive agent loop: LLM → tool_use blocks → execute → append results → re-LLM
- Plan mode is a **modal UI state** (Enter/Exit plan mode tools), not just a flag
- `ultraplan` command — enhanced planning beyond standard `/plan`
- Task CRUD via tools: `TaskCreateTool`, `TaskUpdateTool`, `TaskStopTool` — LLM manages its own task list
- Worktree tools: `EnterWorktreeTool` / `ExitWorktreeTool` — isolated parallel branches using git worktrees
- Multi-agent swarm: `spawnMultiAgent`, `agentSwarmsEnabled` feature flag, `scheduleRemoteAgents` skill
- `commands/rewind/` — session state rewind mid-execution
- `commands/thinkback/` — replay a session's reasoning chain

### opencode
- `SessionPrompt.runLoop` — event-sourced loop; every step appended to SQLite as an immutable event
- **Fork / revert / share** sessions: `POST /session/:id/fork`, `POST /session/:id/revert/{unrevert}`
- ULID-ordered event log — deterministic replay from any checkpoint
- `Session.abort()` — hard mid-stream interrupt via `Effect.Deferred` (not polled; truly async)
- Plan mode: `plan.txt` and `plan-reminder-anthropic.txt` system prompt injection; `ExitPlanMode` tool
- Model-specific system prompt routing: 9 prompt templates keyed by provider family

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| No mid-stream LLM abort (only poll every 0.2s) | Medium | opencode uses `Effect.Deferred` for instant cancellation |
| No session fork / revert / rewind | Medium | opencode has full fork/revert; claw has rewind |
| No git worktree isolation per task | Medium | claw has `EnterWorktreeTool`/`ExitWorktreeTool` |
| No multi-agent swarm scheduler | Low-Medium | claw: `spawnMultiAgent`; useful for large codebases |
| Task CRUD tools (LLM-managed task list) missing | Low | claw's `TaskCreateTool` pattern is valuable for transparency |
| Fast-path heuristic is keyword-only | Low | No model-capability-aware routing |

---

## 2. LLM / Provider Layer

### CodingAgent
- 5 adapters: Ollama, LM Studio, OpenRouter, OpenAI-compat, GitHub Copilot
- Circuit breaker per provider (3 failures → OPEN, 60s recovery)
- SSE streaming with `<think>` tag splitting and `is_reasoning` flag per chunk
- `thinking_utils.py`: `/no_think` for Qwen3, 2× budget for DeepSeek-R1-Distill
- `deterministic=True` mode: `temperature=0.0 + seed`
- **Token counting: `len(content) // 4` — rough estimate only**
- No cost tracking or per-session cost accumulation
- No multi-provider fallback on circuit-open
- No prompt caching (prefix caching)

### claw-code (TS)
- Full Anthropic API client with HybridTransport (SSE + WebSocket)
- Speculative prefetching (`services/api/speculation.ts`) — pre-computes likely next tokens
- Model migration system (`migrations/` — 11 scripts) — handles model version transitions with data migrations
- Rate limit proactive warning (`useRateLimitWarningNotification`) before hitting limits
- `apiPreconnect.ts` — pre-connection warmup before first LLM call
- `/model`, `/fast`, `/effort` commands — runtime model switching and effort level tuning

### opencode
- **21+ providers** via Vercel AI SDK v5 (Anthropic, OpenAI, Google, Mistral, Bedrock, Azure, Groq, DeepSeek, Cerebras, xAI, Ollama, LM Studio, OpenRouter, GitHub Copilot, Vertex AI, etc.)
- **`getSmallModel()`** — explicit small-model accessor; `schema stripping` for models that don't support structured output
- Per-provider system prompt routing: `default.txt`, `anthropic.txt`, `gpt.txt`, `gemini.txt`, `beast.txt`, `codex.txt`, `trinity.txt`, `kimi.txt`, `copilot-gpt-5.txt`
- `Model.cost()` — per-turn cost calculation surfaced in the UI
- Real tokenizer per provider via AI SDK (not character-count approximations)
- Prompt caching headers for Anthropic (cache-control: ephemeral)
- `Model.list()` — live model discovery from provider APIs
- `continue_loop_on_deny` — experimental flag to continue agent loop on tool denial

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| Token counting is `len//4` (±25% error) | **High** | Breaks budget enforcement for small-context models; use tiktoken or provider-native tokenizers |
| No cost tracking / display | High | opencode surfaces per-turn cost; important for user trust |
| No per-provider system prompt templates | **High** | opencode has 9 templates; CodingAgent uses one monolithic role .md regardless of provider |
| No model-tier routing (`getSmallModel()`) | **High** | Core requirement: 9B → frontier routing; currently missing |
| No prompt caching (Anthropic prefix cache) | Medium | Significant latency/cost reduction for large system prompts |
| No multi-provider automatic fallback | Medium | Circuit breaker opens but no retry on next provider |
| No runtime model switching command | Medium | claw: `/model`, `/fast`; useful for interactive sessions |
| No rate-limit proactive warning | Low | claw tracks consumption trajectory |

---

## 3. Tool System

### CodingAgent
- ~60 tools; dual registry (`@tool` decorator + legacy)
- `PermissionLevel` enum (READ_ONLY → WORKSPACE_WRITE → DANGER → PROMPT → ALLOW)
- TIER3 bash gate; bubblewrap sandbox (3 levels, degrades gracefully on macOS)
- Tool contracts (Pydantic), `MAX_PATCH_LINES=200`, read-before-write enforcement
- Role-based tool restriction per role `.md`
- Doom-loop detection; tool cooldown (`_COOLDOWN_GAP=3`)
- Prompt injection guard (F8)
- Native function calling (JSON) + YAML fallback for local models

### claw-code (TS)
- 184 tool implementations in the TS original
- **`sedEditParser` + `sedValidation` + `readOnlyValidation`** — layered pre-execution pipeline for bash
- **`shouldUseSandbox`** — dynamic per-command sandbox decision
- **`SyntheticOutputTool`** — inject synthetic LLM outputs (for testing)
- **`AskUserQuestionTool`** — structured human-in-loop
- **`BriefTool`** with `upload`/`attachments` — document/attachment ingestion
- **`agentMemory` + `agentMemorySnapshot`** — per-agent memory surviving turn boundaries
- `NotebookEditTool` — Jupyter notebook editing
- `LSPTool` with `symbolContext`, `formatters`, `schemas` — LSP-native tool
- **`WebFetchTool.preapproved.ts`** — static allow-list for URLs

### opencode
- 20+ tools; core: `read`, `write`, `edit`, `glob`, `grep`, `bash`, `webfetch`, `websearch`, `lsp_*`
- **AST-level bash security scanning** (`bash.ts`): identifies destructive operations by AST analysis before execution, not by string prefix matching
- **LSP tools built-in**: `lsp_diagnostics`, `lsp_hover`, `lsp_references`, `lsp_rename`, `lsp_symbols`, `lsp_completions`, `lsp_code_action`, `lsp_definition` — 25+ language servers auto-downloaded
- `Effect.Deferred` permission gate — blocks tool call until user approves/denies; non-polling
- Per-tool permission level (read / write / dangerous) mapped in config
- `TodoWrite` built-in (for Anthropic prompt) — task tracking within conversation
- `Task` tool (Anthropic) — spawns sub-agent for exploration/research
- `patch` tool — apply unified diffs directly
- Experimental `batch_tool` — parallel multi-tool execution in one LLM turn

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| No LSP tools (diagnostics, hover, references, rename, go-to-def) | **High** | opencode has 8+ LSP tools; 25 language servers; fundamental for code quality |
| Bash security is string-prefix matching, not AST | **High** | opencode uses AST analysis; prefix matching misses `$(curl ...)` injection etc. |
| Permission gate is polling `Event.wait()` | Medium | opencode: `Effect.Deferred` — non-polling, instant approval/denial |
| No notebook editing tool | Low | claw: `NotebookEditTool`; niche but useful |
| No attachment/document ingestion | Low | claw: `BriefTool` with uploads |
| No per-agent persistent memory tool | Low | claw: `agentMemory` + `agentMemorySnapshot` |
| No URL allow-list for web fetch | Low | claw: `preapproved.ts` |

---

## 4. Memory / Context

### CodingAgent
- `ContextBuilder`: tiered prompt assembly, LRU file cache, dynamic skill injection
- `distill_context()`: LLM-based compaction at ~6000 token threshold; keeps 6 recent messages; writes `TASK_STATE.md`
- `TokenBudgetMonitor`: warn at 70%, compact at 85% (5-turn cooldown)
- LanceDB vector store: `index_code()`, `search()`, `add_memory()`, `search_memories()`
- Pre-retrieval in perception: concurrent `search_code`, `find_symbol`, `find_references`, `find_tests_for_module`
- Dual session stores: SQLite WAL + JSON snapshots

### claw-code (TS)
- **`memdir`** — filesystem-based memory directory with per-memory aging, relevance scoring, team-shared paths
- **`SessionMemory` service** — dedicated session memory with prompt templates
- **`AgentSummary`** — per-agent conversation summarization
- User-invocable `/compact` command — user controls context window management
- Full assistant message storage (CodingAgent only stores user prompts in transcript)
- Team memory paths (`teamMemPaths`) — shared across multi-agent swarm

### opencode
- **Overflow → compaction → pruning pipeline**: context overflow triggers LLM-based summarization; pruning falls back to message dropping only if summarization fails
- `Summarize.summarize()` — dedicated summarization service (not ad-hoc in distiller)
- **Cross-session memory**: `search_memories()` injects prior-session summaries at session start (confirmed call path)
- **Real tokenizer** per provider — compaction fires at exact token thresholds, not character approximations
- `compact` command: user-invokable in the TUI
- No `memdir` equivalent; uses SQLite event log for session history

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| Cross-session memory not injected at session start | **High** | `search_memories()` exists but no confirmed call path; opencode confirms injection |
| Token counting inaccuracy undermines compaction trigger | **High** | Compaction may fire too early or too late; affects all model sizes |
| No user-invokable `/compact` command | Medium | claw and opencode both surface this to the user |
| Full conversation (not just user prompts) should be stored | Medium | Full pairs needed for accurate replay and summarization |
| No per-agent persistent memory (cross-turn, cross-session) | Low | claw's `agentMemory` pattern is useful for multi-agent scenarios |
| Team memory / shared memory for multi-agent | Low | claw's `teamMemPaths` — relevant if swarm execution is added |

---

## 5. Planning

### CodingAgent
- `planning_node` (LLM-driven, `strategic` role), `plan_validator_node`, `step_controller_node`, `replan_node`, `evaluation_node`
- Plan persistence: `last_plan.json`; plan resume with `plan_resumed` flag
- Plan mode gate: user approves before writes
- DAG with `execution_waves` (PRSW scheduling)
- ACP-compatible `plan.progress` EventBus events

### claw-code (TS)
- `EnterPlanModeTool` / `ExitPlanModeTool` — modal plan state as tool calls, not flags
- `planAgent` — dedicated sub-agent for planning only
- `/ultraplan` — enhanced planning mode
- `TaskCreateTool` / `TaskUpdateTool` / `TaskStopTool` — LLM-managed task CRUD
- Plan steps are user-editable before execution begins (plan mode is interactive)
- Plan diffing via `/diff` command

### opencode
- Plan mode via system prompt injection (`plan.txt`): full read-only constraint; 5-phase workflow
- Model-specific plan prompts: `plan-reminder-anthropic.txt` with Explore/Plan/Code sub-phases
- `build-switch.txt` — explicit transition prompt from plan to build mode
- `max-steps.txt` — injected when step limit reached; disables all tools
- No DAG; linear step execution
- No plan CRUD tools

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| Users cannot edit plan steps before approval | Medium | claw supports interactive plan editing; improves human oversight |
| No plan diffing (before/after replan comparison) | Medium | Both peers support this |
| No model-specific plan prompts | Medium | opencode has 5-phase Anthropic plan + Gemini 5-phase; CodingAgent uses one |
| No `max-steps` injection when step limit reached | Low | opencode injects a text-only prompt when steps exhausted |
| No explicit plan schema validation (Pydantic) | Low | Plan structure validated heuristically, not via strict schema |

---

## 6. Determinism

### CodingAgent
- `verification_node`: pytest/ruff/eslint/tsc after every side-effecting tool
- `RollbackManager`: file-level atomic rollback with SHA-256 checksums (`rollback_step_transaction()`)
- `temperature=0.0 + seed` in deterministic mode
- Doom-loop fingerprint ring buffer
- Verified reads tracking (three-layer: `files_read`, `verified_reads`, `_session_read_files`)

### claw-code (TS)
- `verificationAgent` — dedicated LLM sub-agent whose sole role is verifying outputs of other agents
- Layered BashTool validation pipeline: `sedValidation`, `readOnlyValidation`, `pathValidation`, `modeValidation`, `destructiveCommandWarning`
- `/diff` command with color-diff renderer
- `file-index` native module for change tracking

### opencode
- **Event-sourced SQLite log** — every step is an immutable event; ULID ordering; full replay from any point
- **Git snapshot system** — bare git repo; `git write-tree` captures workspace state before every LLM message; `revert` restores via `git read-tree`; `unrevert` re-applies
- **Workspace-level atomic rollback** — snapshot covers entire workspace, not just individual files
- Verification: runs configured `check` command (lint/typecheck/test) after edits
- No dedicated verification sub-agent

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| Rollback is file-level only (not workspace-level) | **High** | opencode: git-based full workspace snapshot before every message |
| No immutable event log / event sourcing | **High** | opencode: SQLite ULID-ordered event log enables full replay and audit |
| No workspace snapshot before task start | High | Full revert capability requires full pre-task snapshot |
| Verification is in-process (no isolation) | Medium | Tests run in live workspace; no container isolation |
| No dedicated verification sub-agent | Low | claw's `verificationAgent` pattern is useful for complex multi-step tasks |

---

## 7. Small Model Scalability (9B → Frontier)

### CodingAgent
- `supports_native_tools` flag in `providers.json`: YAML tool blocks for local models, JSON function calling for frontier
- Fast-path routing: `_task_is_complex()` (keyword heuristic) skips 6 LLM calls for simple tasks
- `thinking_utils.py`: `/no_think`, 2× budget for reasoning models, `strip_thinking()`
- 8 role Markdown files; `get_context_budget()` adapts to model context window
- `_DummyModel` in VectorStore (SHA-256) — works without SentenceTransformer
- `empty_response_count` guard: recovers from blank outputs common with 9B models
- **No model-tier routing** — same pipeline regardless of model capability
- **No dynamic tool list pruning** based on token budget
- **No per-model-size prompt variants** — all roles use same `.md` regardless of context length

### claw-code (TS)
- `simple_mode` flag: reduces tool set to 3 (Bash, FileRead, FileEdit)
- `/fast` command: switches to faster/smaller model at runtime
- `/effort` command: adjusts thinking/effort level
- `/model` command: runtime model switching
- Model migrations (11 scripts): state migration when model versions change
- `speculation.ts`: speculative prefetching for reduced latency
- `toolLimits.ts`: model-specific tool limits

### opencode
- **`getSmallModel()`**: explicit small-model accessor; routes lightweight tasks to cheap models
- **Per-provider system prompt templates** (9 total): `trinity.txt` enforces one tool per message (for weak models); `beast.txt` enables autonomous mode
- **Schema stripping**: models that don't support structured output get simplified prompts
- **`max-steps.txt`**: injected when step limit reached; disables all tools, forces text-only summary
- **Dynamic tool list**: tools are filtered per-turn by capability flags
- Model-specific `getModel()` with `tinker()` for provider-specific parameters
- `continue_loop_on_deny` experimental: adapts loop behavior per model capability

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| No model-tier routing (small vs frontier model) | **Critical** | Core project goal; both peers solve this differently; CodingAgent has zero routing |
| No per-provider/per-model-family prompt templates | **High** | opencode has 9; single role .md is insufficient for 9B models |
| No dynamic tool list pruning by token budget | **High** | 9B models have 4-8K context; 60+ tools in prompt is too expensive |
| No `simple_mode` equivalent (reduced tool set) | High | claw's 3-tool simple mode is a practical baseline |
| No runtime model switching command | Medium | claw's `/model`, `/fast` — important for interactive use |
| No schema stripping for models without tool support | Medium | opencode strips JSON schema for weak models |
| `_task_is_complex()` is keyword-only heuristic | Medium | Does not consider model capability |

---

## 8. MCP (Model Context Protocol)

### CodingAgent
- `mcp_stdio_server.py` (638 lines): JSON-RPC 2.0 STDIO server; bridges EventBus for IDE integration
- Agent exposes itself **as an MCP server only**
- No MCP **client** — cannot connect to external MCP servers
- No dynamic MCP tool registration into `_registry.py`

### claw-code (TS)
- Full MCP **client AND server** (bidirectional)
- `MCPTool`, `McpAuthTool`, `ListMcpResourcesTool`, `ReadMcpResourceTool` — MCP as first-class tools
- `/mcp` slash command with `addCommand` UI for managing MCP servers
- `skills/mcpSkillBuilders.ts` — wraps MCP tools as skills dynamically
- `McpAuthTool` — authentication to external MCP servers within the conversation flow
- `useMcpConnectivityStatus` notification hook

### opencode
- Full MCP client: stdio + HTTP/SSE transport; **OAuth 2.0 PKCE** auth
- MCP servers registered in config (`mcp` section); auto-started and managed
- MCP tools dynamically registered as agent tools at session start
- `ListMcpResourcesTool`, `ReadMcpResourceTool` — access MCP resources
- `/mcp` management route: `GET /mcp`, `GET /mcp/:id`, `POST /mcp` (add), `DELETE /mcp/:id`
- `useMcpConnectivityStatus` — TUI shows real-time MCP server connectivity

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| No MCP client — cannot consume external MCP tools | **High** | Both peers are full MCP clients; this is now a baseline expectation |
| No OAuth 2.0 PKCE for MCP auth | High | opencode: full PKCE flow; required for cloud MCP servers |
| No dynamic MCP tool registration into tool registry | High | MCP tools must be first-class registered tools, not side effects |
| MCP server-side compliance incomplete | Medium | `sampling`, `prompts`, tool call lifecycle not fully verified |
| No in-TUI MCP server management UI | Medium | Both peers surface MCP management in the UI |

---

## 9. Plugin / Extension System

### CodingAgent
- `plugin_tools` in `.agent/config.json` — dotted module paths; `build_registry()` registers with `origin="plugin"`
- Conflict detection: plugin cannot override builtin tool names
- Pre/post shell hooks: `.agent/hooks/pre_tool.sh` / `post_tool.sh`
- Skills system: `src/config/agent-brain/skills/*.md`; `load_skill` tool; dynamic injection per node
- Role Markdown files: drop `.md` in `agent-brain/roles/`, picked up without recompile

### claw-code (TS)
- Full plugin **marketplace**: Browse, Discover, Add Marketplaces, Manage Plugins, Manage Marketplaces, Plugin Trust Warning, Plugin Options dialogs
- Hot-reload plugins at runtime (`reload-plugins` command)
- 15 bundled skills: `batch`, `debug`, `loop`, `remember`, `verify`, `verifyContent`, `stuck`, `skillify`, `scheduleRemoteAgents`, etc.
- `skillify` skill: meta-programming — creates new skills from conversation
- `stuck` skill: auto-recovery when agent is in a loop
- MCP tools → skills (two-layer extension: MCP tool → skill → command)
- Trust gating for plugin init

### opencode
- **Hooks system**: `before_session`, `after_session`, `file_changed` hooks in config
- Built-in auth plugins: GitHub Copilot OAuth, Anthropic OAuth
- Skills: `src/skill/` directory; `GET /instance/skill` endpoint
- No plugin marketplace
- MCP tools are the primary extension mechanism

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| Pre/post hook execution path not verified end-to-end | High | `tool_hooks.py` scaffolded; actual dispatch unconfirmed |
| No `stuck` auto-recovery skill | Medium | claw's `stuck` skill is a practical safety net |
| No `skillify` meta-skill | Low | Lower priority for initial release |
| No plugin marketplace | Low | Nice-to-have for community growth; not needed for v1 |
| No hot-reload of plugins | Low | Useful for plugin development workflow |

---

## 10. Config System

### CodingAgent
- `providers.json` (array), `.agent/config.json` (per-workspace), `schema.json` (JSON Schema validation)
- 5 toolset YAMLs; 8 role Markdown files; `SOUL.md` + `LAWS.md`
- `config_loader.py`: central reader; `tools_config.py`: module-level mutable config
- Env var overrides: `CODINGAGENT_AUTONOMOUS`, `CODINGAGENT_SANDBOX_LEVEL`
- `codingagent init`: scaffolds `.agent/config.json`
- **No hot-reload** (restart required for `providers.json` changes)
- **No per-model routing config** ("use model X for planning, model Y for execution")
- **No global user-level profile** (only per-workspace)

### opencode
- 3-tier config hierarchy: global (`~/.config/opencode/`), project (`.opencode/`), managed (highest priority, org-scoped)
- Live reload (config file watching)
- 30+ config fields including per-agent model override (`Config.Agent.model`)
- `OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT`, `OPENCODE_PERMISSION` env vars
- Auto-migration from legacy TOML format
- `$schema` auto-injection
- Remote/managed config from `opencode.ai` console (org-level policy enforcement)

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| No per-model-per-role config ("planning uses model X") | **High** | Core to model-tier routing requirement |
| No config hot-reload | Medium | Must restart to change providers; friction for local model experimentation |
| No global user-level config profile | Medium | Every workspace must repeat settings |
| No org-level managed config | Low | Useful for team deployments |

---

## 11. Error Handling

### CodingAgent
- Multi-layer retry: per-step (cap 3), plan (cap 3), replan (cap 5), debug (cap 9), empty-response (cap 3)
- Circuit breaker per provider (CLOSED → OPEN → HALF_OPEN)
- `_fallback_compact()` on LLM distillation failure
- `validate_state()` non-fatal state validator
- All errors returned as dicts (`status: "error"`, `ok: False`) — no typed error classes
- No per-request exponential backoff

### opencode
- **`NamedError`** — typed error hierarchy; all errors have names and structured payloads
- **`SessionRetry`** exception — distinguishes retryable from fatal errors
- Exponential backoff on transient provider failures (via AI SDK retry policy)
- Abort controller (`Effect.Deferred`) — cancellation is a first-class concern
- Rate limit proactive warning (tracks consumption trajectory)
- `max-steps.txt` injection on step limit — graceful degradation, not hard failure

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| No typed error hierarchy (all strings) | High | Makes programmatic error handling and testing fragile |
| No per-request exponential backoff before circuit open | Medium | Current: 3 failures → open circuit; should retry with backoff first |
| No rate-limit proactive warning | Low | opencode tracks consumption trajectory |

---

## 12. Session Management

### CodingAgent
- Dual stores: SQLite WAL (messages, tool_calls, errors, plans, decisions) + JSON snapshots
- `StoredSession` with `version`, `session_id`, `task_name`, `working_dir`
- Plan persistence: `last_plan.json`; `plan_resumed` flag
- TUI history persistence: TUI history file under the user's data directory (see ``src.core.paths.get_data_dir()``)
- `session_registry.py`, `session_watcher.py`, `cross_session_bus.py`, `agent_session_manager.py`
- **No session fork / revert / rewind**
- **Dual stores are redundant** (no clear authority for resumption)
- **Full assistant messages not stored in transcript** (user prompts only)

### opencode
- **Event-sourced single SQLite store** — one authoritative store
- Session operations: fork, revert, unrevert, share (generate shareable link), export
- `GET /session/:id/diff` — diff of all files modified in the session
- Session summarization: `POST /session/:id/summarize` — auto-title generation
- `/session` listing with search

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| Dual redundant session stores (no clear authority) | High | Consolidate to single authoritative SQLite store |
| No session fork / revert / branch | High | Required for safe experimentation without losing work |
| Full conversation pairs not stored | Medium | Only user prompts stored in transcript; assistant responses missing |
| No session diff endpoint | Medium | opencode's `GET /session/:id/diff` is very useful |
| No session summarization / auto-title | Low | opencode auto-generates session titles |

---

## 13. Security / Permissions

### CodingAgent
- `PermissionLevel` enum; `_ACTIVE_PERMISSION_MODE` singleton
- Autonomous mode bypasses all approval gates
- `ToolPermissionContext`: `--allowed-tools`, `--deny-tool`, `--deny-prefix` CLI filters
- Bubblewrap sandbox (degrades to plain subprocess on macOS)
- TIER3 bash gate (prefix-based)
- `workspace_guard.py`: path traversal prevention
- Context sanitization: `_sanitize_text()`
- Prompt injection guard (F8)
- Role-based tool restriction

### claw-code (TS)
- **Three-tier permission handler**: coordinator / interactive / swarmWorker — context-sensitive permissions
- `preapproved.ts` for WebFetch — static URL allow-list
- `permissionLogging.ts` — audit log of permission decisions
- `BypassPermissionsModeDialog` — explicit UI escape hatch with friction
- `TestingPermissionTool` — permissions testable as first-class concern
- `shouldUseSandbox` — dynamic per-command sandbox decision

### opencode
- **AST-level bash security** — not prefix matching; structural analysis of shell AST
- `Effect.Deferred` — permission gate is async, non-polling; instant response
- Per-tool permission level in config (`read` / `write` / `dangerous`)
- `OPENCODE_PERMISSION` env var for permission overrides
- Sandbox: bubblewrap on Linux; macOS relies on workspace guard only
- No audit log of permission decisions (same gap as CodingAgent)

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| Bash security is string-prefix (not AST) | **High** | Misses injection via `$(...)`, heredocs, etc. |
| Permission gate is polling (not event-driven) | Medium | opencode: `Effect.Deferred`; instant; non-blocking |
| No permission audit log | Medium | Both peers lack this too; still a gap worth filling |
| macOS has no sandbox (plain subprocess) | Medium | Same limitation in opencode; shared gap |
| `_AUTONOMOUS_MODE` bypasses ALL gates | Medium | No secondary confirmation for DANGER tools in autonomous mode |
| No time-limited per-tool approvals | Low | Per-call only; "approve for next 5 minutes" pattern missing |

---

## 14. LSP Integration

### CodingAgent
- `src/core/indexing/lsp_context.py` — file exists; not fully implemented
- `symbol_graph.py` — `find_tests_for_module`, regex-based symbol graph
- **No LSP tools exposed to the LLM** — no `lsp_diagnostics`, `lsp_hover`, `lsp_references`
- No language server auto-download or management

### claw-code (TS)
- `LSPTool` with `symbolContext`, `formatters`, `schemas`
- LSP integration for symbol resolution and formatting

### opencode
- **Full LSP client** (`src/lsp/`) — 25+ language servers auto-downloaded via Mason registry
- 8 LSP tools exposed to the LLM: `lsp_diagnostics`, `lsp_hover`, `lsp_references`, `lsp_rename`, `lsp_symbols`, `lsp_completions`, `lsp_code_action`, `lsp_definition`
- Language server lifecycle: auto-install, auto-start per-workspace, status monitoring (`GET /instance/lsp`)
- Formatters: auto-configured per language, run on file write

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| No LSP tools for the LLM | **Critical** | LSP diagnostics/references dramatically improve code quality for any model size; opencode treats this as baseline |
| No language server management | High | Auto-download + lifecycle management is expected in a modern coding agent |
| `lsp_context.py` stub not wired | High | Exists but not connected to any node or tool |
| No formatter auto-run on write | Medium | opencode auto-runs formatters; important for deterministic output |

---

## 15. Testing Infrastructure

### CodingAgent
- 4-tier: `unit/`, `integration/`, `e2e/`, `benchmarks/`
- `_reset_compiled_graph()`, `ContextBuilder.clear_cache()` for test isolation
- `_DummyModel` for vector store; `mock_engine.py` for TUI
- Pydantic fallback stub
- **No golden-file / snapshot regression tests**
- **No mock LLM adapter at `call_model()` level** — e2e tests need real provider

### opencode
- Comprehensive unit tests per module
- `mock LLM adapter` — all agent tests run against a mock provider; no real API calls in CI
- Snapshot tests for system prompt templates
- `GET /doc` OpenAPI spec — enables contract testing

### Gaps (CodingAgent vs peers)

| Gap | Severity | Notes |
|---|---|---|
| No mock LLM adapter at `call_model()` level | High | Cannot run full graph integration tests in CI without real provider |
| No golden-file / snapshot regression tests | Medium | Particularly important for system prompt stability |
| No contract tests for EventBus event schema | Low | Events are untyped; schema drift is silent |

---

## Summary Gap Matrix

| # | Dimension | CodingAgent Grade | Most Critical Gap | Peer Advantage |
|---|---|---|---|---|
| 1 | Orchestration | B+ | No session fork/revert; no mid-stream abort | opencode event sourcing |
| 2 | LLM/Provider | C+ | Token counting inaccuracy; no per-provider prompts; no cost tracking | opencode AI SDK v5 |
| 3 | Tool System | B+ | No LSP tools; bash security is string-prefix only | opencode LSP tools + AST bash |
| 4 | Memory/Context | B | Cross-session recall unconfirmed; token count drives compaction wrongly | opencode real tokenizer |
| 5 | Planning | B | No user-editable plan steps; no model-specific plan prompts | opencode 5-phase Anthropic plan |
| 6 | Determinism | B- | File-level rollback only; no event log | opencode git snapshots + event sourcing |
| 7 | **Small Model Scaling** | **D** | **No model-tier routing; no per-model prompts; no dynamic tool pruning** | **opencode `getSmallModel()` + 9 templates** |
| 8 | **MCP Client** | **D** | **No MCP client — cannot consume any external MCP server** | **Both peers are full MCP clients** |
| 9 | Plugin/Extension | B- | Hook dispatch unverified; no `stuck` skill | claw bundled skills |
| 10 | Config | C+ | No model-per-role config; no hot-reload | opencode 3-tier + live reload |
| 11 | Error Handling | B | No typed errors; no per-request backoff | opencode `NamedError` + `SessionRetry` |
| 12 | Session Management | C+ | Dual redundant stores; no fork/revert | opencode event sourcing + fork |
| 13 | Security/Permissions | B | AST bash missing; permission gate is polling | opencode AST + `Effect.Deferred` |
| 14 | **LSP Integration** | **F** | **No LSP tools; stub not wired** | **opencode: 25 LSPs + 8 tools** |
| 15 | Testing | B- | No mock LLM adapter for CI; no golden files | opencode: mock provider in all tests |

**Priority clusters for mitigation:**
- **P0 (blocking for stated goal):** Small model routing (#7), LSP tools (#14), MCP client (#8)
- **P1 (correctness):** Token counting (#2/#4), git workspace snapshots (#6), typed errors (#11)
- **P2 (user trust / UX):** Cost tracking (#2), session fork/revert (#12), per-provider prompts (#2)
- **P3 (polish):** Config hot-reload (#10), hook verification (#9), golden file tests (#15)
