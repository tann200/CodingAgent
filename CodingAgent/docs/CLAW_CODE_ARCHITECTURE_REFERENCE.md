# Claw Code Architecture Reference

> Source-verified reference built from reading the actual claw-code repository
> at `/Users/tann200/PycharmProjects/claw-code`.
> All citations include the real file path within that repo.

---

## Documentation Index (online)

| Topic | URL |
|-------|-----|
| Architecture Overview | https://claw-code.codes/architecture |
| Tool System | https://claw-code.codes/tool-system |
| Slash Commands | https://claw-code.codes/commands |
| Rust Runtime | https://claw-code.codes/rust-runtime |
| Query Engine | https://claw-code.codes/query-engine |
| Session Management | https://claw-code.codes/session-management |
| Permissions | https://claw-code.codes/permissions |
| MCP Integration | https://claw-code.codes/mcp-integration |
| API Client | https://claw-code.codes/api-client |
| GitHub Source | https://github.com/instructkr/claw-code |

---

## 1. Overall Architecture — Dual-Layer Design

**Source:** `rust/Cargo.toml`, `src/`, `rust/crates/`

Claw Code splits execution across two language layers. Python owns
orchestration logic; Rust owns all latency-sensitive paths.

```
User Terminal (REPL / stdin)
        |
  rusty-claude-cli (Rust binary)
  rust/crates/rusty-claude-cli/src/{main,render,input,init}.rs
  — crossterm raw-mode input, syntect highlighting, pulldown_cmark markdown,
    braille spinner (10 frames)
        |
  Python Orchestration Layer
  src/{bootstrap_graph,query_engine,runtime,commands,tools,tool_pool,
       session_store,transcript,permissions,models,context,parity_audit,…}.py
  — 68+ modules, single responsibility per file
        |
  compat-harness (Rust crate)
  rust/crates/compat-harness/
  — stable Python ↔ Rust calling-convention bridge
        |
  Rust Performance Layer
  rust/crates/{api,runtime,tools,commands}/
  — API client, bash/file execution, permission enforcement, session,
    compaction, hooks, MCP, sandbox, SSE, OAuth, usage tracking
        |
  Provider APIs (Anthropic, OpenAI-compat, xAI/Grok)
```

**9 Rust crates** (not 6 as the website implies):
`api`, `commands`, `compat-harness`, `mock-anthropic-service`, `plugins`,
`runtime`, `rusty-claude-cli`, `telemetry`, `tools`

---

## 2. Bootstrap

### Python side — `src/bootstrap_graph.py`

Seven sequential stages declared as a frozen dataclass:

```python
BootstrapGraph.stages = (
    'top-level prefetch side effects',
    'warning handler and environment guards',
    'CLI parser and pre-action trust gate',
    'setup() + commands/agents parallel load',
    'deferred init after trust',
    'mode routing: local / remote / ssh / teleport / direct-connect / deep-link',
    'query engine submit loop',
)
```

### Rust side — `rust/crates/runtime/src/bootstrap.rs`

12-phase `BootstrapPhase` enum with deduplication-preserving order:
`CliEntry → FastPathVersion → StartupProfiler → SystemPromptFastPath →
ChromeMcpFastPath → DaemonWorkerFastPath → BridgeFastPath → DaemonFastPath →
BackgroundSessionFastPath → TemplateFastPath → EnvironmentRunnerFastPath →
MainRuntime`

Many phases are **fast paths** that exit early if their condition is met
(e.g. `--version` flag exits at `FastPathVersion`).

---

## 3. Query Engine — `src/query_engine.py`

```python
@dataclass(frozen=True)
class QueryEngineConfig:
    max_turns: int = 8
    max_budget_tokens: int = 2000
    compact_after_turns: int = 12
    structured_output: bool = False
    structured_retry_limit: int = 2
```

`QueryEnginePort` exposes `submit_message()` which:
1. Checks `len(mutable_messages) >= max_turns` → early exit with
   `stop_reason='max_turns_reached'`
2. Builds `TurnResult` with matched commands, tools, permission denials, usage

**Rust side** (`rust/crates/runtime/src/conversation.rs`):

```
DEFAULT_AUTO_COMPACTION_INPUT_TOKENS_THRESHOLD = 100_000
AUTO_COMPACTION_THRESHOLD_ENV_VAR = "CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS"
```

The Rust `ConversationRuntime` compacts on token count (100k input tokens),
not turn count. Turn count is the Python-layer gate.

---

## 4. Tool System

### Python layer — `src/tools.py` + `src/tool_pool.py`

```python
@dataclass(frozen=True)
class ToolExecution:
    name: str
    source_hint: str
    payload: str
    handled: bool
    message: str
```

- `load_tool_snapshot()` — reads `src/reference_data/tools_snapshot.json`,
  cached with `@lru_cache(maxsize=1)`
- `get_tools(simple_mode, include_mcp, permission_context)` — filters:
  simple_mode restricts to `{BashTool, FileReadTool, FileEditTool}`
- `find_tools(query, limit=20)` — substring search

`ToolPool.as_markdown()` renders `tools[:15]` — the cap is in the markdown
render, not the tool list itself.

### Rust layer — `rust/crates/tools/src/lib.rs` (10306 lines)

```rust
pub struct ToolSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub input_schema: Value,         // full serde_json JSON Schema
    pub required_permission: PermissionMode,
}
```

**Static global registries** (one per subsystem, initialized once via `OnceLock`):
- `global_lsp_registry()` — `LspRegistry`
- `global_mcp_registry()` — `McpToolRegistry`
- `global_team_registry()` — `TeamRegistry`
- `global_cron_registry()` — `CronRegistry`
- `global_task_registry()` — `TaskRegistry`
- `global_worker_registry()` — `WorkerRegistry`

File ops constants (`rust/crates/runtime/src/file_ops.rs`):
- `MAX_READ_SIZE = 10 MB`
- `MAX_WRITE_SIZE = 10 MB`
- Ignored dirs: `.git`, `node_modules`, `.build`, `target`, `dist`, `coverage`
- Binary file detection: scan first 8192 bytes for NUL bytes

---

## 5. Permission System

### Python — `src/permissions.py`

```python
@dataclass(frozen=True)
class ToolPermissionContext:
    deny_names: frozenset[str]       # exact lowercased tool names
    deny_prefixes: tuple[str, ...]   # lowercased prefix matches
    workspace_scope: WorkspacePathScope | None
    cwd: Path | None

    def blocks(self, tool_name: str) -> bool: ...
    def validate_payload_scope(self, tool_name, payload) -> PathScopeDecision: ...
```

Scope-checked tools: any name containing `bash`, `shell`, `powershell`,
`fileread`, `filewrite`, `fileedit`.

### Rust — `rust/crates/runtime/src/permissions.rs`

```rust
pub enum PermissionMode {
    ReadOnly,
    WorkspaceWrite,
    DangerFullAccess,
    Prompt,
    Allow,
}

pub enum PermissionOverride { Allow, Deny, Ask }

pub struct PermissionContext {
    override_decision: Option<PermissionOverride>,
    override_reason: Option<String>,
}

pub struct PermissionRequest {
    pub tool_name: String,
    pub input: String,
    pub current_mode: PermissionMode,
    pub required_mode: PermissionMode,
    pub reason: Option<String>,
}
```

Five permission modes (not three as the website states): `ReadOnly`,
`WorkspaceWrite`, `DangerFullAccess`, `Prompt`, `Allow`.

---

## 6. Session & Compaction

### Python session store — `src/session_store.py`

```python
@dataclass(frozen=True)
class StoredSession:
    session_id: str
    messages: tuple[str, ...]   # immutable after persist
    input_tokens: int
    output_tokens: int
```

Saved to `.port_sessions/{session_id}.json`.

### Python transcript — `src/transcript.py`

```python
class TranscriptStore:
    entries: list[str]
    flushed: bool

    def compact(self, keep_last: int = 10) -> None: ...
    def replay(self) -> tuple[str, ...]: ...
    def flush(self) -> None: ...
```

### Rust compaction — `rust/crates/runtime/src/compact.rs`

```rust
pub struct CompactionConfig {
    pub preserve_recent_messages: usize,  // default: 4
    pub max_estimated_tokens: usize,      // default: 10_000
}
```

String constants (identical to `src/core/memory/auto_compactor.py` in CodingAgent — confirmed direct port):

```rust
const COMPACT_CONTINUATION_PREAMBLE: &str =
    "This session is being continued from a previous conversation ...";
const COMPACT_RECENT_MESSAGES_NOTE: &str = "Recent messages are preserved verbatim.";
const COMPACT_DIRECT_RESUME_INSTRUCTION: &str =
    "Continue the conversation from where it left off ...";
```

Token estimation: `len(text) / 4 + 1` (heuristic, same in Python port).

`should_compact()` only examines the **compactable portion** (after any
existing summary prefix) — prevents double-counting on repeated compactions.

---

## 7. System Prompt Assembly — `rust/crates/runtime/src/prompt.rs`

```rust
pub const SYSTEM_PROMPT_DYNAMIC_BOUNDARY: &str = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__";
pub const FRONTIER_MODEL_NAME: &str = "Claude Opus 4.6";
const MAX_INSTRUCTION_FILE_CHARS: usize = 4_000;
const MAX_TOTAL_INSTRUCTION_CHARS: usize = 12_000;
```

`ModelFamilyIdentity` enum: `Claude` (label: "Claude Opus 4.6") vs
`Generic` (label: "an AI assistant") — allows non-Anthropic deployment.

Instruction file discovery walks ancestor chain for `CLAUDE.md`,
`CLAUDE.local.md`, `.claude/CLAUDE.md`. Content-hash deduplication
prevents repeated includes across symlinked monorepo dirs.

---

## 8. API Client — `rust/crates/api/src/client.rs`

```rust
pub enum ProviderClient {
    Anthropic(AnthropicClient),
    Xai(OpenAiCompatClient),
    OpenAi(OpenAiCompatClient),
}
```

Three provider kinds (not one): Anthropic native, OpenAI-compatible, xAI/Grok.
DashScope (Alibaba qwen-*) uses OpenAI wire format but DashScope config
(`DASHSCOPE_API_KEY`, `dashscope.aliyuncs.com`).

Prompt cache support: `AnthropicClient` has `PromptCache` + `PromptCacheStats`
tracking; xAI and OpenAI clients do not.

---

## 9. Hooks — `rust/crates/runtime/src/hooks.rs`

```rust
pub enum HookEvent {
    PreToolUse,
    PostToolUse,
    PostToolUseFailure,
}
```

Hook commands are shell commands configured in `RuntimeHookConfig`.
`HOOK_PREVIEW_CHAR_LIMIT = 160` chars shown in UI before hook runs.
Hooks can return `PermissionOverride` (Allow / Deny / Ask) to gate tool execution.

---

## 10. Bash Execution — `rust/crates/runtime/src/bash.rs`

```rust
pub struct BashCommandInput {
    pub command: String,
    pub timeout: Option<u64>,
    pub description: Option<String>,
    pub run_in_background: Option<bool>,
    pub dangerously_disable_sandbox: Option<bool>,
    pub namespace_restrictions: Option<bool>,
    pub isolate_network: Option<bool>,
    pub filesystem_mode: Option<FilesystemIsolationMode>,
    pub allowed_mounts: Option<Vec<String>>,
}
```

Uses `tokio::process::Command` (async) with `tokio::time::timeout`.

Sandbox (`rust/crates/runtime/src/sandbox.rs`):
```rust
pub enum FilesystemIsolationMode { Off, WorkspaceOnly, AllowList }
```

---

## 11. MCP Integration — `rust/crates/runtime/src/mcp.rs`

Tool naming: `mcp__{server}__{tool}` where both server and tool names are
normalized (non-alphanum chars → `_`).

Special handling for `claude.ai ` prefix servers (CloudAI proxy URL unwrapping
via `CCR_PROXY_PATH_MARKERS`).

---

## 12. Usage & Cost — `rust/crates/runtime/src/usage.rs`

```rust
// Default: Sonnet-tier pricing
const DEFAULT_INPUT_COST_PER_MILLION: f64 = 15.0;
const DEFAULT_OUTPUT_COST_PER_MILLION: f64 = 75.0;
const DEFAULT_CACHE_CREATION_COST_PER_MILLION: f64 = 18.75;
const DEFAULT_CACHE_READ_COST_PER_MILLION: f64 = 1.5;

pub struct TokenUsage {
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub cache_creation_input_tokens: u32,
    pub cache_read_input_tokens: u32,
}
```

Four token counters (not two) — cache creation and cache read are tracked
separately for cost calculation.

---

## 13. Key Patterns — Source Verified

| Pattern | File | Detail |
|---------|------|--------|
| Frozen dataclasses everywhere | `session_store.py`, `tool_pool.py`, `query_engine.py` | Immutable value objects |
| `lru_cache(maxsize=1)` on snapshot loads | `tools.py` | Zero-cost repeated calls |
| `OnceLock` for global Rust registries | `tools/src/lib.rs` | Thread-safe, lazy init |
| Token heuristic `len // 4 + 1` | `compact.rs`, `auto_compactor.py` | Same formula in both langs |
| Workspace path scope validation | `permissions.py`, `file_ops.rs` | Path traversal prevention |
| Content-hash dedup for instructions | `prompt.rs` | SHA-based, not string compare |
| Provider fallback chain | `config.rs` `ProviderFallbackConfig` | Ordered retry on 429/5xx |
| Hook override gate | `hooks.rs` | Pre/Post tool use + failure hooks |
| 5 permission modes | `permissions.rs` | ReadOnly/WorkspaceWrite/DangerFullAccess/Prompt/Allow |
| Prompt cache tracking | `api/src/client.rs` | Anthropic-only, stats exposed |
