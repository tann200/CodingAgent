# Claw Code Architecture Reference

> **Purpose:** Reference document for architectural patterns and practices in the
> `claw-code-main` repository. Intended for use as a comparison baseline against
> CodingAgent. All file paths are relative to the `claw-code-main` repository root.

---

## Table of Contents

1. [Project Layout](#1-project-layout)
2. [Orchestration & Agent Loop](#2-orchestration--agent-loop)
3. [System Prompt Architecture](#3-system-prompt-architecture)
4. [Tool System](#4-tool-system)
5. [Context Management & Compaction](#5-context-management--compaction)
6. [Session & Memory Management](#6-session--memory-management)
7. [Configuration System](#7-configuration-system)
8. [MCP Integration](#8-mcp-integration)
9. [Permission System](#9-permission-system)
10. [Provider Abstraction](#10-provider-abstraction)
11. [Plugin & Hook System](#11-plugin--hook-system)
12. [Key Rust Patterns](#12-key-rust-patterns)
13. [Comparison Reference: CodingAgent vs Claw Code](#13-comparison-reference-codingagent-vs-claw-code)

---

## 1. Project Layout

```
claw-code-main/
├── rust/                          # Primary runtime (Rust workspace)
│   ├── Cargo.toml                 # Workspace manifest
│   └── crates/
│       ├── rusty-claude-cli/      # Binary crate — CLI entry point + rendering
│       │   └── src/main.rs        # ~9 000 lines — REPL, streaming, argument parsing
│       ├── runtime/               # Core agent logic (~29 KB of .rs)
│       │   └── src/
│       │       ├── conversation.rs  # ConversationRuntime — main turn loop
│       │       ├── session.rs       # Session persistence + fork/compaction
│       │       ├── compact.rs       # Automatic context-window management
│       │       ├── prompt.rs        # SystemPromptBuilder + discover_instruction_files
│       │       ├── config.rs        # ConfigLoader — multi-source config merge
│       │       ├── permissions.rs   # PermissionPolicy + PermissionMode
│       │       ├── mcp_stdio.rs     # MCP client (JSON-RPC, McpServerManager)
│       │       ├── mcp_server.rs    # MCP server (expose tools externally)
│       │       ├── hooks.rs         # HookRunner (pre/post-tool lifecycle)
│       │       ├── task_registry.rs # Task tracking
│       │       ├── worker_boot.rs   # Sub-agent bootstrap
│       │       └── usage.rs         # Token usage tracking per session
│       ├── api/                   # Provider client abstraction
│       │   └── src/               # AnthropicClient, OpenAiCompatClient, streaming
│       ├── tools/                 # Built-in tool implementations
│       │   └── src/lib.rs         # execute_tool() dispatcher, mvp_tool_specs()
│       ├── commands/              # Slash command system (/help, /compact, /mcp …)
│       ├── plugins/               # Plugin lifecycle, PluginManager, HookRunner
│       ├── telemetry/             # SessionTracer, analytics events
│       ├── compat-harness/        # Parity test tooling (TypeScript manifest extraction)
│       └── mock-anthropic-service/ # Deterministic /v1/messages mock for testing
├── src/                           # Python reference/audit companion (NOT primary runtime)
├── docs/                          # Container and deployment docs
├── CLAUDE.md                      # Project-level agent instructions
├── PHILOSOPHY.md                  # Design framing and project intent
├── PARITY.md                      # Rust port migration status
└── ROADMAP.md                     # Active roadmap + cleanup backlog
```

**Crate dependency graph:**
```
rusty-claude-cli
  ├── api, commands, plugins, runtime, tools
tools
  ├── api, commands, plugins, runtime
runtime
  ├── plugins, telemetry
api
  └── telemetry
```

**Contrast with CodingAgent:**
- CodingAgent is a Python monorepo with one `src/` package tree.
- Claw code is a Rust workspace with nine purpose-focused crates.
- Separation of concerns is enforced at the crate (compilation unit) level rather than
  by Python module conventions.

---

## 2. Orchestration & Agent Loop

### Architecture decision: no state machine

Claw code does **not** use LangGraph or any explicit state machine. The pipeline is a
simple turn-based loop inside a single function.

### Turn loop (`runtime/src/conversation.rs:314`)

```
User input
  │
  ▼
ConversationRuntime::run_turn()
  │
  ├─ Push user message to session
  │
  ├─ ┌─ API loop ─────────────────────────────────────────────────────┐
  │  │  1. Assemble ApiRequest {system_prompt, messages}              │
  │  │  2. ApiClient::stream(request) → Vec<AssistantEvent>           │
  │  │  3. Collect assistant text + tool calls from events            │
  │  │  4. For each pending tool_use:                                 │
  │  │       a. Run pre-tool hook (may override input/deny)           │
  │  │       b. Evaluate PermissionPolicy (allow/ask/deny)            │
  │  │       c. ToolExecutor::execute(name, input) → String           │
  │  │       d. Run post-tool hook                                    │
  │  │       e. Push ToolResult to session                            │
  │  │  5. If no pending tools → exit API loop                        │
  │  └─────────────────────────────────────────────────────────────────┘
  │
  ├─ Check auto-compaction threshold
  │
  └─ Return TurnSummary {assistant_messages, tool_results, usage}
```

**Key signatures:**
```rust
// runtime/src/conversation.rs:57
pub trait ToolExecutor {
    fn execute(&mut self, tool_name: &str, input: &str) -> Result<String, ToolError>;
}

// runtime/src/conversation.rs:314
pub fn run_turn(
    &mut self,
    user_input: impl Into<String>,
    mut prompter: Option<&mut dyn PermissionPrompter>,
) -> Result<TurnSummary, RuntimeError>
```

### REPL loop (`rusty-claude-cli/src/main.rs:~2968`)

```
loop {
    readline(prompt)
    if slash_command → handle command
    else → LiveCli::run_turn_with_output(input)
}
```

### Comparison with CodingAgent

| Dimension | CodingAgent | Claw Code |
|---|---|---|
| Orchestrator | LangGraph state machine (14 nodes) | Linear `run_turn()` loop |
| State | `AgentState` TypedDict (~95 fields) | `ConversationRuntime` struct + `Session` |
| Routing | Conditional edge functions (`should_after_*`) | None — linear progression |
| Planning | Dedicated `planning_node` + `plan_validator_node` | None (model plans inline) |
| Debugging | Dedicated `debug_node` | None (retry is caller's responsibility) |
| Delegation | `analyst_delegation_node` + `delegation_node` | `Worker` sub-agents spawned via tool call |
| Recovery | `replan_node`, `step_controller` | None — exit on first error |
| Memory sync | `memory_sync` → `memory_update_node` | `compact_session()` called after turn |

The claw code approach is dramatically simpler: the LLM is trusted to plan, retry, and
self-correct without pipeline-level scaffolding.

---

## 3. System Prompt Architecture

### Builder pattern (`runtime/src/prompt.rs`)

```rust
// runtime/src/prompt.rs:95
pub struct SystemPromptBuilder {
    output_style: Option<String>,
    os_info: Option<String>,
    project_context: Option<ProjectContext>,
    runtime_config: Option<RuntimeFeatureConfig>,
    append_sections: Vec<String>,
}

impl SystemPromptBuilder {
    pub fn new() -> Self { ... }
    pub fn with_output_style(mut self, style: &str) -> Self { ... }
    pub fn with_os(mut self) -> Self { ... }
    pub fn with_project_context(mut self, ctx: ProjectContext) -> Self { ... }
    pub fn with_runtime_config(mut self, cfg: RuntimeFeatureConfig) -> Self { ... }
    pub fn build(self) -> Vec<String> { ... }  // Returns sections for multi-block prompt
}
```

### Section composition order

1. Introduction block (role description)
2. Output style (if configured)
3. System section (core capability description)
4. Project context:
   - Current date
   - Working directory
   - Git status snapshot
   - Last 5 commits
   - Git diff snapshot
   - Branch/tag context
5. **Discovered instruction files** (CLAUDE.md files, see below)
6. Tool definitions (from `mvp_tool_specs()`)
7. MCP tool definitions (if servers connected)
8. Plugin tool definitions
9. Config `append_sections`

### Instruction file discovery (`runtime/src/prompt.rs:203`)

```rust
pub fn discover_instruction_files(cwd: &Path) -> Vec<ContextFile> {
    // Walk ancestor directories: cwd → / (root)
    let mut dirs = Vec::new();
    let mut cursor = Some(cwd);
    while let Some(d) = cursor { dirs.push(d); cursor = d.parent(); }
    dirs.reverse();  // root first → cwd last

    // For each dir, check these candidates in order
    for dir in dirs {
        for candidate in ["CLAUDE.md", "CLAUDE.local.md", ".claw/CLAUDE.md", ".claw/instructions.md"] {
            if let Ok(content) = fs::read_to_string(dir.join(candidate)) {
                files.push(ContextFile { path, content });
            }
        }
    }

    // Caps: total 12 000 chars, per-file 4 000 chars
    // Dedup by SHA-256 content hash
    dedupe_instruction_files(files)
}
```

**Constants (`runtime/src/prompt.rs:40-44`):**
```rust
const MAX_INSTRUCTION_CHARS_TOTAL: usize = 12_000;
const MAX_INSTRUCTION_CHARS_PER_FILE: usize = 4_000;
const SYSTEM_PROMPT_DYNAMIC_BOUNDARY: &str = "---DYNAMIC---";
```

### Comparison with CodingAgent

| | CodingAgent | Claw Code |
|---|---|---|
| Prompt builder | `ContextBuilder` class (`context_builder.py`) | `SystemPromptBuilder` Rust struct |
| Instruction discovery | `src/config/agent-brain/` SOUL.md + role YAMLs | `discover_instruction_files()` ancestor walk |
| Role system | Per-node role YAMLs (operational, strategic, debugger…) | Single introduction block + config appends |
| Project instructions | `OP-5`: `.agent-context/config.json#instructions` | CLAUDE.md ancestors |
| Tier adaptation | `model_constraints` block for NANO/SMALL | No tier concept |
| Tool pruning | `_prune_tools()` per tier | Not tiered — full tool list always |

---

## 4. Tool System

### Registration (`tools/src/lib.rs:385`)

Tools are registered as `ToolSpec` values with JSON Schema input specifications:

```rust
pub struct ToolSpec {
    pub name: &'static str,
    pub description: &'static str,
    pub input_schema: serde_json::Value,  // JSON Schema object
    pub permission_required: PermissionKind,
}

pub fn mvp_tool_specs() -> Vec<ToolSpec> { ... }  // Returns all builtin specs
```

Additional tools are merged from:
- MCP servers via `McpServerManager::list_tools()`
- Plugins via `GlobalToolRegistry::with_plugin_tools()`
- LSP tools via `LspRegistry`

### Dispatch (`tools/src/lib.rs:1174`)

```rust
pub fn execute_tool(name: &str, input: &Value) -> Result<String, String> {
    match name {
        "bash"          => run_bash(serde_json::from_value(input.clone())?),
        "read_file"     => run_read_file(input),
        "write_file"    => run_write_file(input),
        "edit_file"     => run_edit_file(input),
        "glob_search"   => run_glob_search(input),
        "grep_search"   => run_grep_search(input),
        "WebFetch"      => run_web_fetch(input),
        "WebSearch"     => run_web_search(input),
        // ... 30+ additional tools
        _ => Err(format!("unknown tool: {name}")),
    }
}
```

No fuzzy name correction (cf. CodingAgent P3-D preflight). Unknown tools return an error immediately.

### Built-in tool categories

| Category | Tools |
|---|---|
| File I/O | `read_file`, `write_file`, `edit_file` |
| Search | `glob_search`, `grep_search`, `ToolSearch` |
| Execution | `bash`, `PowerShell` |
| Web | `WebFetch`, `WebSearch` |
| Sub-agents | `Agent`, `WorkerCreate`, `WorkerGet`, `WorkerObserve`, `WorkerSendPrompt` |
| Tasks | `TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`, `TaskStop`, `TaskOutput` |
| Skills | `Skill` |
| Planning | `EnterPlanMode`, `ExitPlanMode` |
| Interaction | `AskUserQuestion`, `SendUserMessage`, `Brief`, `Sleep` |
| UI | `StructuredOutput`, `REPL`, `Config` |
| Notebooks | `NotebookEdit` |

### Executor trait

```rust
// runtime/src/conversation.rs:57
pub trait ToolExecutor {
    fn execute(&mut self, tool_name: &str, input: &str) -> Result<String, ToolError>;
}
```

`CliToolExecutor` (main.rs:7823) wraps `GlobalToolRegistry`, enforces permissions, runs hooks, and dispatches to MCP if name is an MCP-prefixed tool.

### Comparison with CodingAgent

| | CodingAgent | Claw Code |
|---|---|---|
| Registration | `example_registry()` dict in `orchestrator.py` | `mvp_tool_specs()` Vec<ToolSpec> |
| Dispatch | `ToolExecutionService` + per-function handlers | `match name` in `execute_tool()` |
| Preflight | `preflight_check_impl()` — name, bash patterns, path containment | Permission policy check (allow/ask/deny) |
| Fuzzy correction | P3-D: difflib at cutoff=0.85 for SMALL+ | None |
| Toolsets | Per-role toolset YAMLs via `get_tools_for_role()` | Single unified tool list |
| Side effects | `side_effects: ["write"]` per tool | `permission_required: PermissionKind` per spec |

---

## 5. Context Management & Compaction

### Automatic compaction (`runtime/src/compact.rs`)

```rust
pub struct CompactionConfig {
    pub preserve_recent_messages: usize,  // default: 4
    pub max_estimated_tokens: usize,       // default: 100_000
}

pub struct CompactionResult {
    pub summary: String,
    pub formatted_summary: String,
    pub compacted_session: Session,
    pub removed_message_count: usize,
}

pub fn compact_session(
    session: &Session,
    config: CompactionConfig,
    summariser: impl Fn(&[ConversationMessage]) -> Result<String, RuntimeError>,
) -> Result<CompactionResult, RuntimeError>
```

**Algorithm:**
1. Estimate session token count (character-count heuristic)
2. If below threshold → skip
3. Preserve most recent N messages (`preserve_recent_messages`)
4. Walk backward from boundary to avoid splitting tool-use / tool-result pairs
5. Extract older messages → call `summariser` closure → receive prose summary
6. Replace older messages with a single System message containing the summary
7. Store `SessionCompaction { count, removed_message_count, summary }` in session metadata

**Trigger:** After every `run_turn()`, check `estimate_session_tokens(session) > threshold`.
Environment variable `CLAUDE_CODE_AUTO_COMPACT_INPUT_TOKENS` overrides the default 100 000.

**Token estimation:** Simple `sum(message.content.len()) / 4` heuristic
(no tiktoken; cf. CodingAgent S0-A tokenizer).

### Compaction prompt (OP-2 comparison)

Claw code does not prescribe a structured compaction prompt — the `summariser` closure is
caller-supplied. In the CLI binary the summariser calls the API with a generic
"summarize the following conversation" message.

CodingAgent's `distill.py` uses a structured template with sections:
`## Goal / ## Instructions / ## Discoveries / ## Accomplished / ## Relevant Files / ## Current State`
(prefixed with `[COMPACTED]` per OP-8).

### PRUNE_PROTECT / preserve flag

Claw code has no equivalent to CodingAgent's `metadata.preserve` flag (OP-10).
Preservation is implemented solely by `preserve_recent_messages` count.

### Comparison

| | CodingAgent | Claw Code |
|---|---|---|
| Trigger | `compact_context()` called from `Orchestrator` | Automatic post-turn check in `run_turn()` |
| Token counting | tiktoken (S0-A) with `len/3.5` fallback | `len/4` heuristic only |
| Compaction prompt | Structured sections (OP-2) | Generic summarise call |
| Marker | `[COMPACTED]` prefix (OP-8) | No marker |
| Preserve flag | `metadata.preserve = True` (OP-10) | `preserve_recent_messages: usize` |
| Overflow detection | `get_actual_context_window()` vs token count (OP-4) | Threshold env var |
| Checkpoint | `compaction_checkpoint.md` for cross-task guard | `SessionCompaction` in session JSON |

---

## 6. Session & Memory Management

### Session structure (`runtime/src/session.rs:89`)

```rust
pub struct Session {
    pub version: u32,
    pub session_id: String,
    pub created_at_ms: u64,
    pub updated_at_ms: u64,
    pub messages: Vec<ConversationMessage>,         // Full conversation history
    pub compaction: Option<SessionCompaction>,       // Last compaction metadata
    pub fork: Option<SessionFork>,                   // Fork parent reference
    pub workspace_root: Option<PathBuf>,             // Isolate by project
    pub prompt_history: Vec<SessionPromptEntry>,     // Slash command + input history
    pub last_health_check_ms: Option<u64>,
    pub model: Option<String>,
    persistence: Option<SessionPersistence>,         // File path + rotation
}
```

### Message representation

```rust
pub enum ContentBlock {
    Text   { text: String },
    ToolUse    { id: String, name: String, input: serde_json::Value },
    ToolResult { tool_use_id: String, tool_name: String, output: String, is_error: bool },
}

pub struct ConversationMessage {
    pub role: MessageRole,   // System | User | Assistant | Tool
    pub blocks: Vec<ContentBlock>,
    pub usage: Option<TokenUsage>,
}
```

### Persistence

- **Format:** JSON Lines (`.jsonl`) — one event per line, append-only
- **Location:** Platform data dir (`~/.local/share/opencode/` on Linux/macOS)
- **Rotation:** Roll to next file after 256 KB; keep at most 3 rotated files
- **Session resumption:** `Session::load()` replays events from `.jsonl` file

### Fork / revert

`SessionFork { parent_session_id, fork_point_message_index }` stored in session metadata.
`session_store.fork_session()` / `revert_session()` are mirrored in CodingAgent (S4-B/S5).

### Usage tracking (`runtime/src/usage.rs`)

```rust
pub struct TokenUsage {
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub cache_creation_input_tokens: Option<u32>,
    pub cache_read_input_tokens: Option<u32>,
}

pub struct UsageTracker { cumulative: TokenUsage }
```

Accumulated per-turn via `UsageTracker::add()`. Published to telemetry sink after each turn.

### Comparison with CodingAgent

| | CodingAgent | Claw Code |
|---|---|---|
| State persistence | `session_store.py` (SQLite/JSON) | `.jsonl` append-only file |
| Message format | `dict` with `role`/`content`/`metadata` | `ConversationMessage` enum blocks |
| Usage tracking | `SessionCostTracker` (D-10 service) | `UsageTracker` struct |
| Session isolation | `working_dir` field in AgentState | `workspace_root: Option<PathBuf>` in Session |
| Cross-task cleanup | `start_new_task_impl()` resets 12+ fields | N/A — each run is a fresh session |
| Vector memory | `VectorStore.add_memory()` after distillation (P3-7) | None |

---

## 7. Configuration System

### Hierarchy (`runtime/src/config.rs:242`)

Config is discovered by `ConfigLoader::discover(project_root)` and deep-merged in this order
(later entries win):

```
1. ~/.claw.json                          (legacy user)
2. ~/.local/share/opencode/settings.json (user settings)
3. <project>/.claw.json                  (project config)
4. <project>/.claw/settings.json         (project config alternate)
5. <project>/.claw/settings.local.json   (local machine overrides, gitignored)
```

### Schema (`runtime/src/config.rs:37`)

```rust
pub struct RuntimeFeatureConfig {
    pub hooks: RuntimeHookConfig,
    pub plugins: RuntimePluginConfig,
    pub mcp: McpConfigCollection,           // MCP server definitions
    pub oauth: Option<OAuthConfig>,
    pub model: Option<String>,
    pub aliases: BTreeMap<String, String>,  // Model aliases
    pub permission_mode: Option<ResolvedPermissionMode>,
    pub permission_rules: RuntimePermissionRuleConfig,
    pub sandbox: SandboxConfig,
    pub provider_fallbacks: ProviderFallbackConfig,
    pub trusted_roots: Vec<String>,
}
```

### Example project config (`.claw.json`):

```json
{
  "permissions": {
    "defaultMode": "dontAsk"
  },
  "model": "claude-sonnet-4-6",
  "mcp": {
    "servers": {
      "postgres": {
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres", "postgres://..."]
      }
    }
  },
  "hooks": {
    "PreToolUse": [{ "matcher": "bash", "command": "echo pre-bash" }]
  }
}
```

### Comparison with CodingAgent

| | CodingAgent | Claw Code |
|---|---|---|
| Project config file | `.agent-context/config.json` (OP-5) | `.claw.json` / `.claw/settings.json` |
| User config | `~/.config/codingagent/prefs.json` | `~/.local/share/opencode/settings.json` |
| Config merge | `load_merged_config()` Python dict merge | `ConfigLoader::discover()` deep merge |
| Dynamic reload | `ConfigWatcher` (watchfiles, S6-C) | No dynamic reload; restart required |
| Tool overrides | `tool_overrides: {name: bool}` per project | `PermissionMode` + deny rules |
| Deny write patterns | `deny_write_patterns: [glob]` (OP-5) | `permission_rules.deny` list |
| Model override | `get_project_model_override()` (OP-5) | `model: "..."` in config |

---

## 8. MCP Integration

### Dual role: client and server

Claw code implements both ends of the MCP protocol.

**As MCP client (`runtime/src/mcp_stdio.rs`):**

```rust
pub struct McpServerManager {
    servers: HashMap<String, McpServerState>,
    // ...
}

impl McpServerManager {
    pub async fn connect(&mut self, name: &str, config: &McpServerConfig) -> Result<()>
    pub async fn list_tools(&self, server: &str) -> Result<Vec<McpTool>>
    pub async fn call_tool(&self, server: &str, name: &str, args: &Value) -> Result<String>
}
```

**Startup sequence:**
1. Load MCP server configs from `RuntimeFeatureConfig::mcp`
2. Spawn server process (stdio) or open connection (WebSocket/HTTP/SSE)
3. Send `initialize` (JSON-RPC 2.0)
4. Call `tools/list` → register tools in global registry with `mcp_<server>_` prefix
5. On tool call → dispatch via `call_tool()` → JSON-RPC `tools/call`

**As MCP server (`runtime/src/mcp_server.rs`):**

```rust
pub struct McpServerConfig {
    pub tool_handler: Box<dyn Fn(&str, &Value) -> Result<String, String> + Send>,
}

pub fn run_mcp_server(config: McpServerConfig) -> Result<()>
// Listens on stdin/stdout, handles:
//   initialize, tools/list, tools/call
// Protocol version: "2025-03-26"
```

**Transport types supported:**
- `stdio` — subprocess stdin/stdout
- `websocket` — WebSocket connection
- `sse` — Server-Sent Events
- `http` — HTTP streaming
- `sdk` — SDK-managed
- `managed_proxy` — Proxied through a managed service

### Comparison with CodingAgent

| | CodingAgent | Claw Code |
|---|---|---|
| MCP client | `mcp_client.py` (JSON-RPC 2.0 stdio, S3-A/TASK-21) | `mcp_stdio.rs` McpServerManager |
| MCP server | `mcp_stdio_server.py` (orchestrator ref, P4-2) | `mcp_server.rs` |
| Discovery | `get_mcp_servers()` from config (S3-B) | `McpConfigCollection` from RuntimeConfig |
| Tool registration | `mcp_client.list_tools()` → tool registry | `McpServerManager::list_tools()` → GlobalToolRegistry |
| TUI commands | `/mcp list|add|status` (S3-C) | `/mcp` slash command via `commands` crate |

---

## 9. Permission System

### Model (`runtime/src/permissions.rs:99`)

```rust
pub enum PermissionMode {
    ReadOnly,          // No writes, no bash
    WorkspaceWrite,    // Writes inside workspace only
    DangerFullAccess,  // No restrictions
}

pub enum PermissionKind {
    ReadFile, WriteFile, ExecuteBash, NetworkFetch, // ...
}

pub struct PermissionPolicy {
    pub default_mode: PermissionMode,
    pub allow: Vec<PermissionRule>,   // Explicit allow patterns
    pub deny: Vec<PermissionRule>,    // Explicit deny patterns
    pub ask: Vec<PermissionRule>,     // Prompt user for these
}

pub enum PermissionDecision {
    Allow, Deny, Ask(PermissionPromptDecision),
}
```

### Evaluation flow

1. Check explicit `deny` rules → return `Deny` immediately if matched
2. Check explicit `allow` rules → return `Allow` if matched
3. Evaluate `default_mode`:
   - `ReadOnly` → deny writes/bash
   - `WorkspaceWrite` → allow writes inside workspace root
   - `DangerFullAccess` → allow all
4. If still unresolved and `ask` rule matches → call `PermissionPrompter::decide()`

### Per-tool permissions

Each `ToolSpec` carries `permission_required: PermissionKind`. Before dispatch:
```rust
let decision = policy.evaluate(tool_spec.permission_required, &input);
if decision == Deny { return Err(ToolError::PermissionDenied) }
if decision == Ask  { /* prompt user */ }
```

### Comparison with CodingAgent

| | CodingAgent | Claw Code |
|---|---|---|
| Permission model | `approval_gate.py` — MODIFYING_TOOLS allowlist | `PermissionPolicy` with allow/deny/ask rules |
| Bash restrictions | `BASH_DANGEROUS_PATTERNS` + preflight | `PermissionMode::ReadOnly` blocks bash |
| Path containment | `preflight_check_impl()` path is_relative_to | `WorkspaceWrite` → workspace_root boundary |
| User confirmation | `approval_gate.py` TUI prompt | `PermissionPrompter` trait |

---

## 10. Provider Abstraction

### Enum-based dispatch (`api/src/lib.rs`)

```rust
pub enum ProviderClient {
    Anthropic(AnthropicClient),
    OpenAiCompat(OpenAiCompatClient),
}

impl ProviderClient {
    pub fn stream(&mut self, req: ApiRequest) -> Result<MessageStream, ApiError> {
        match self {
            Self::Anthropic(c)    => c.stream(req),
            Self::OpenAiCompat(c) => c.stream(req),
        }
    }
}
```

### Provider detection

```rust
pub fn detect_provider_kind(model: &str) -> ProviderKind {
    if model.starts_with("openai/") || model.starts_with("gpt-") { ProviderKind::OpenAI }
    else if model.starts_with("grok") { ProviderKind::XAI }
    else if model.starts_with("qwen/") { ProviderKind::Dashscope }
    else if model.contains("ollama") { ProviderKind::Ollama }
    else { ProviderKind::Anthropic }  // Default
}
```

### Auth resolution

```rust
pub fn resolve_startup_auth_source() -> Result<AuthSource, ApiError> {
    // 1. ANTHROPIC_AUTH_TOKEN env var (OAuth bearer or proxy token)
    // 2. ANTHROPIC_API_KEY env var (direct API key)
    // Supports ANTHROPIC_BASE_URL override
}
```

### Model aliases

Built-in aliases (runtime/src/config.rs):
- `opus`   → `claude-opus-4-6`
- `sonnet` → `claude-sonnet-4-6`
- `haiku`  → `claude-haiku-4-5-20251213`

Custom aliases configurable in `.claw.json` under `aliases`.

### Comparison with CodingAgent

| | CodingAgent | Claw Code |
|---|---|---|
| Provider abstraction | `OpenAICompatibleAdapter` base class + subclasses | `ProviderClient` enum + trait dispatch |
| Provider auto-detect | Config `providers.json` array | `detect_provider_kind()` model name prefix |
| Model tiers | `ModelTier` enum (NANO→FRONTIER) + `classify_model()` | No tier concept |
| Fallback chain | `ProviderFallbackConfig` + `ModelRouter` | `provider_fallbacks` in `RuntimeFeatureConfig` |
| Auth | `credentials.py` (keyring + prefs.json) | `resolve_startup_auth_source()` env vars |
| Retry | Exponential backoff (P2-1): 3 attempts, 1 s/2 s | Not in scope (caller retries) |

---

## 11. Plugin & Hook System

### Plugin metadata (`plugins/src/lib.rs`)

```rust
pub struct PluginMetadata {
    pub name: String,
    pub version: String,
    pub description: String,
    pub author: String,
    pub tools: Vec<PluginTool>,         // Tools provided by this plugin
    pub hooks: PluginHooks,             // Hook handlers
    pub required_permissions: Vec<PermissionKind>,
}

pub struct PluginHooks {
    pub pre_tool_use: Option<String>,      // Shell command to run
    pub post_tool_use: Option<String>,
    pub post_tool_use_failure: Option<String>,
    pub init: Option<String>,
    pub shutdown: Option<String>,
}
```

### Hook execution (`runtime/src/hooks.rs`)

```rust
pub struct HookRunner { ... }

impl HookRunner {
    pub fn run_pre_tool_use(&self, tool_name: &str, input: &Value)
        -> Result<HookResult, HookError>
    pub fn run_post_tool_use(&self, tool_name: &str, output: &str)
        -> Result<(), HookError>
}

pub enum HookResult {
    Continue(Option<Value>),  // Proceed, optionally with modified input
    Block(String),             // Deny tool call with reason
}
```

Hooks run synchronously inline during `run_turn()`, before and after tool dispatch.

### Comparison with CodingAgent

CodingAgent does not have a plugin system. Hooks are partially handled through:
- `approval_gate.py` (pre-tool approval gate)
- `tool_execution_service.py` wrapping tool dispatch (D-10)
- No post-tool hooks

---

## 12. Key Rust Patterns

### Safety

```toml
# rust/Cargo.toml (workspace lint)
[workspace.lints.rust]
unsafe_code = "forbid"  # Zero unsafe code in any crate
```

### Error handling

Custom error types per crate with `thiserror` or manual `std::error::Error` impl:
- `RuntimeError` — top-level agent errors
- `ToolError { PermissionDenied, ExecutionFailed(String), UnknownTool(String) }`
- `SessionError`, `ConfigError`, `ApiError`

All functions return `Result<T, CustomError>`. Errors propagate with `?`. No `panic!` in
production paths.

### Trait-based polymorphism

Three key traits isolate testable boundaries:
```rust
pub trait ApiClient { fn stream(&mut self, req: ApiRequest) -> Result<...>; }
pub trait ToolExecutor { fn execute(&mut self, name: &str, input: &str) -> Result<...>; }
pub trait PermissionPrompter { fn decide(&mut self, req: &PermissionRequest) -> Decision; }
```

`ConversationRuntime<C: ApiClient, T: ToolExecutor>` is generic over both — swap in
`MockApiClient` and `MockToolExecutor` for tests.

### Async

- **Tokio** for async I/O (HTTP, file, MCP subprocesses)
- `ConversationRuntime` embeds `tokio::runtime::Runtime` — sync API surface for REPL
- MCP state shared as `Arc<Mutex<RuntimeMcpState>>`
- File I/O in async context (no blocking calls on Tokio thread pool)

### Builder pattern

Used for all multi-field initialization:
- `SystemPromptBuilder` — fluent prompt construction
- `ConfigLoader` — config discovery + merge
- `ConversationRuntime::builder()` — optional feature wiring

### Serialization

- `serde` + `serde_json` throughout
- Session format: `.jsonl` (append-only, line-per-event)
- Config: `.json` with `BTreeMap<String, Value>` for deep merge

---

## 13. Comparison Reference: CodingAgent vs Claw Code

### Architectural philosophy

| Dimension | CodingAgent | Claw Code |
|---|---|---|
| Language | Python | Rust |
| Framework | LangGraph (DAG state machine) | Custom turn loop |
| Complexity | High (14 nodes, ~95-field AgentState) | Low (single `run_turn()` function) |
| Planning | Explicit planning + validation pipeline | LLM plans inline |
| Debugging | Dedicated debug loop node | LLM self-corrects or errors |
| Memory | VectorStore + session compaction | Session compaction only |
| Tiering | ModelTier (NANO→FRONTIER) adapts prompts/tools | No tiering |
| Safety | Defense-in-depth preflight + guardrails | Permission policy + hooks |

### Where CodingAgent is more sophisticated

1. **Multi-node pipeline** with specialist roles (planner, debugger, analyst): more reliable for
   complex multi-step tasks but substantially more code surface.
2. **ModelTier adaptation**: prompt pruning, tool filtering, YAML-only mode for NANO — claw code
   sends full context/tool list regardless of model capability.
3. **Structured compaction**: `[COMPACTED]` markers + sectioned prompt (OP-2/OP-8) vs generic
   summarise call.
4. **Vector memory**: `VectorStore.add_memory()` after distillation enables semantic retrieval.
5. **Project config features**: `deny_write_patterns`, `tool_overrides`, per-task model override.
6. **Guardrail depth**: read-before-write guard, path traversal checks, DANGEROUS_PATTERNS, P3-D
   fuzzy tool correction.

### Where Claw Code is more sophisticated

1. **MCP first-class**: both client and server, multiple transport types, plugin-supplied tools.
2. **Permission system**: fine-grained `allow/deny/ask` rules per tool kind + mode.
3. **Plugin & hook system**: lifecycle hooks (pre/post tool), plugin-supplied tools, manager.
4. **Streaming rendering**: real-time markdown rendering to terminal via `MarkdownStreamState`.
5. **Crate isolation**: compile-time enforcement of module boundaries.
6. **Sub-agent Workers**: `WorkerCreate` / `WorkerSendPrompt` for parallel sub-agents.
7. **Session forking**: native fork/revert at session level.
8. **No unsafe code**: workspace-level `unsafe_code = "forbid"`.

### Key patterns to adopt (for CodingAgent reference)

| Claw Code Pattern | CodingAgent Equivalent / Gap |
|---|---|
| `discover_instruction_files()` ancestor walk | Manual CLAUDE.md path in `context_builder.py` |
| `PermissionPolicy` with `allow/deny/ask` rules | `approval_gate.py` is simpler allowlist |
| `compact_session()` post-turn automatic trigger | Manual `compact_context()` call |
| `ToolSpec.permission_required` per-tool | No per-tool permission metadata |
| `ConversationRuntime<C, T>` generic traits | Adapter + orchestrator tightly coupled |
| `.jsonl` session storage with rotation | SQLite / JSON flat files |
| Plugin hook `PreToolUse`/`PostToolUse` | No hook system (only approval gate) |

---

*Document generated 2026-04-11. Source repository: `/Users/tann200/PycharmProjects/claw-code-main`.*
*CodingAgent source: `/Users/tann200/PycharmProjects/CodingAgent`.*
