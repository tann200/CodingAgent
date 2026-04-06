# Deep Dive: claw-code Architecture (v2)

**Date:** 2026-04-06
**Source:** `/Users/tann200/PycharmProjects/claw-code-main/`
**Status:** Reference document — rewritten from scratch after reading all source files. Replaces the v1 document which described a stale `claw_core` crate layout that no longer exists.

---

## 1. Repository Layout

The actual crate layout (verified against live source):

```
claw-code-main/
└── rust/
    └── crates/
        ├── runtime/          # Core loop, permissions, compaction, prompts, hooks, usage, sandbox, session, config, MCP, OAuth, bootstrap
        ├── tools/            # All built-in tool implementations + GlobalToolRegistry (4469 lines)
        ├── plugins/          # Plugin system: manifests, PluginTool, BuiltinPlugin, BundledPlugin, ExternalPlugin, PluginManager (2943 lines)
        ├── commands/         # Slash command handling (2667 lines)
        ├── api/              # LLM streaming API client, OpenAI-compat + claw provider, SSE, types
        ├── lsp/              # LSP client (client.rs, manager.rs, types.rs) with context enrichment
        ├── claw-cli/         # Binary entry point: main.rs (5090 lines), render.rs, input.rs, app.rs, init.rs, args.rs
        ├── server/           # Server mode
        └── compat-harness/   # Compatibility test harness
```

**What does NOT exist:**
- There is no `claw_core` crate — the old deep-dive doc was wrong.
- There is no `claw_cli` (underscore) crate — it is `claw-cli` (hyphen).
- Python files in `src/` are an incomplete scaffold. All analysis below is Rust only.

---

## 2. Core Loop: `ConversationRuntime`

**File:** `rust/crates/runtime/src/conversation.rs` (801 lines)

### Type structure

```rust
pub struct ConversationRuntime<C: ApiClient, T: ToolExecutor> {
    api_client: C,
    tool_executor: T,
    permission_policy: PermissionPolicy,
    hook_runner: HookRunner,
    system_prompt: String,
    session: Session,
    compaction_config: CompactionConfig,
    usage_tracker: UsageTracker,
    max_iterations: usize,
}
```

### `run_turn()` algorithm

```
1. push user ConversationMessage to session.messages
2. loop (up to max_iterations):
   a. call api_client.stream(system_prompt, messages) → stream of AssistantEvent
   b. collect events into text blocks + Vec<ToolUse>
   c. push AssistantMessage (with TokenUsage) to session.messages
   d. usage_tracker.record(token_usage)
   e. if pending_tool_uses.is_empty() → break
   f. for each tool_use in pending_tool_uses:
      i.   permission_policy.authorize(tool_use) → Allow / Deny
      ii.  hook_runner.run_pre_tool_use(tool_use) → may deny or append feedback
      iii. tool_executor.execute(tool_use) → ToolResult
      iv.  hook_runner.run_post_tool_use(tool_use, result) → may deny or append feedback
   g. push ToolResultMessage to session.messages
   h. continue loop
3. if session token usage exceeds compaction threshold → self.compact()
4. return final text
```

### Key design decisions vs. CodingAgent

| Aspect | claw-code | CodingAgent |
|--------|-----------|-------------|
| Loop driver | Tight `loop {}` in `run_turn()` | LangGraph StateGraph with conditional edges |
| Concurrency | OS thread per subagent | asyncio + ThreadPoolExecutor |
| State | `Session { version, messages }` | `AgentState` TypedDict (~65 fields) |
| Abort | Thread join / OS kill | asyncio cancellation |
| Max iterations | `usize::MAX` (no limit) for main; configurable per subagent | Configurable `max_iterations` (default 32) |
| Compaction | First-class `compact()` method, deterministic | `/compact` slash command, LLM-based prose distillation |

### `compact()` method

`ConversationRuntime::compact()` calls the compaction engine directly; it is **not a slash command**.
`UsageTracker::from_session()` is called at construction to reconstruct cumulative usage from any existing session messages (resume continuity).

---

## 3. Context Compaction Engine

**File:** `rust/crates/runtime/src/compact.rs` (702 lines)

### Config

```rust
pub struct CompactionConfig {
    pub preserve_recent_messages: usize,  // default: 4
    pub max_estimated_tokens: usize,      // default: 10_000
}
```

### Trigger: `should_compact()`

Compaction fires when the compactable portion of the context (all but the most recent `preserve_recent_messages`) exceeds `max_estimated_tokens`. Token counting is **deterministic character-based estimation** — no LLM call is needed.

### `compact_session()`

Replaces old messages with a structured `<summary>` block containing:

1. **Scope stats** — message count, tool invocations counted
2. **Tool names mentioned** — unique tool names from all tool-use blocks
3. **Recent user requests** — last N user message texts
4. **Pending work** — inferred from "todo"/"next"/"remaining" keywords in message text
5. **Key files** — paths extracted from tool inputs
6. **Current work** — last assistant text segment
7. **Full key timeline** — chronological list of (role, short-excerpt) pairs

### Incremental compaction: `merge_compact_summaries()`

On a second compaction, the existing summary is preserved under a "Previously compacted context" section; the new summary is added under "Newly compacted context". Summaries **stack** rather than collapse — this means the agent never loses a trace of earlier work at the cost of O(n) summary growth.

### Continuation constants

```rust
const COMPACT_CONTINUATION_PREAMBLE: &str = "...";
const COMPACT_RECENT_MESSAGES_NOTE: &str = "...";
const COMPACT_DIRECT_RESUME_INSTRUCTION: &str =
    "Continue the conversation from where it left off without asking the user
     any further questions. Resume directly — do not acknowledge the summary…";
```

### Format helpers

- `format_compact_summary()` — strips `<analysis>` tags, reformats `<summary>` tag content, collapses blank lines
- Token estimation is based on character count (`chars().count() / 4`)

### CodingAgent comparison

CodingAgent has `distiller.py` / `compact_messages_to_prose()` which:
- **Requires an LLM call** (costs tokens + latency)
- Is triggered by `/compact` slash command (not automatic)
- Produces prose, not structured sections

claw-code's approach is deterministic (no LLM cost), automatic (fires at threshold), and produces richer structured output.

---

## 4. System Prompt Builder

**File:** `rust/crates/runtime/src/prompt.rs` (795 lines)

### `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`

```rust
pub const SYSTEM_PROMPT_DYNAMIC_BOUNDARY: &str = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__";
```

This sentinel string divides the "static" upper portion of the system prompt (eligible for prompt caching) from the "dynamic" lower portion (changes each turn). The API client uses this boundary to set `cache_control` breakpoints appropriately.

### `dedupe_instruction_files()`

When walking ancestor directories for `CLAW.md` / `CLAW.local.md` / `.claw/CLAW.md` / `.claw/instructions.md`, identical-content files are deduplicated via `stable_content_hash()` (uses `std::collections::hash_map::DefaultHasher` — fast, not cryptographic).

### Per-file and total budgets

```rust
const MAX_INSTRUCTION_FILE_CHARS: usize = 4_000;   // per file
const MAX_TOTAL_INSTRUCTION_CHARS: usize = 12_000;  // across all files
```

### `ProjectContext::discover_with_git()`

Reads:
- `git status --short --branch` — branch + modified files
- `git diff --cached` — staged changes (separately labelled)
- `git diff` — unstaged changes (separately labelled)

### Section assembly order

```
1.  Intro paragraph
2.  Output style rules
3.  # System         (core operating rules)
4.  # Doing tasks    (task execution rules)
5.  # Executing actions with care
6.  __SYSTEM_PROMPT_DYNAMIC_BOUNDARY__
7.  # Environment context   (OS, shell, date, cwd, model name)
8.  # Project context       (git status + staged diff + unstaged diff)
9.  # Claw instructions     (per-file CLAW.md content, budget-capped)
10. # Runtime config        (permission mode, sandbox status)
11. append_sections         (subagent descriptor, LSP context enrichment)
```

### LSP injection

`SystemPromptBuilder::with_lsp_context(enrichment: &LspContextEnrichment)` appends an LSP section as an `append_section` containing:
- Current file diagnostics
- Workspace diagnostics
- Go-to-definition results
- Find-references results

---

## 5. Shell Hook System

**File:** `rust/crates/runtime/src/hooks.rs` (357 lines)
**Config parsed in:** `rust/crates/runtime/src/config.rs` (`RuntimeHookConfig`)

### Hook invocation

Hooks are **shell command strings** run via `sh -lc`. Not Python callables.

```json
// hooks section in .claw/settings.json
{
  "hooks": {
    "PreToolUse": ["./scripts/guard.sh"],
    "PostToolUse": ["./scripts/audit.sh"]
  }
}
```

### Exit code semantics

| Exit code | Pre-tool meaning | Post-tool meaning |
|-----------|-----------------|------------------|
| `0` | Allow, no feedback | Allow, no feedback |
| `2` | **Deny** — tool is blocked | **Deny** — result marked `is_error=true`, feedback appended |
| other | Warn — feedback prepended to tool output; tool proceeds | Warn — feedback appended to result; result proceeds |

### Payload (sent to hook's stdin as JSON)

```json
{
  "hook_event_name": "PreToolUse",
  "tool_name": "bash",
  "tool_input": { ... },
  "tool_input_json": "{ raw JSON string }",
  "tool_output": "...",
  "tool_result_is_error": false
}
```

### Env vars also set

`HOOK_EVENT`, `HOOK_TOOL_NAME`, `HOOK_TOOL_INPUT`, `HOOK_TOOL_IS_ERROR`, `HOOK_TOOL_OUTPUT`

### Plugin hooks

The `plugins` crate defines `PluginHooks { pre_tool_use: Vec<String>, post_tool_use: Vec<String> }` in `plugin.json` manifests. Plugin hooks are merged with settings hooks via `PluginHooks::merged_with()`.

### CodingAgent comparison

CodingAgent's `deferred_init.py` has a plugin hook system but:
- Only supports pre-tool hooks (no post-tool)
- Uses Python subprocess with dict/JSON protocol, not stdin-JSON
- Exit code semantics differ (no `2 = deny` convention)
- Not configurable from a settings file

---

## 6. Permission Policy

**File:** `rust/crates/runtime/src/permissions.rs` (232 lines)

### Permission modes

```rust
pub enum PermissionMode {
    ReadOnly,
    WorkspaceWrite,
    DangerFullAccess,
    Prompt,      // ask user for every tool call
    Allow,       // bypass all checks
}
```

### `PermissionPolicy`

Each tool has an associated `PermissionMode` (its minimum required level). The policy compares the tool's required level against the current session mode:

- `ReadOnly` session → denies `WorkspaceWrite` and `DangerFullAccess` tools
- `WorkspaceWrite` session → allows read + write; prompts for `DangerFullAccess` if a `prompter` is present
- `DangerFullAccess` session → allows everything
- `Prompt` session → asks user for every tool regardless of level
- `Allow` session → unconditionally permits every tool

### Runtime-configurable per-tool levels

`PermissionPolicy::with_tool_requirement()` allows per-tool permission level overrides at runtime. Tool permission levels are defined in `ToolSpec.required_permission` (one of `ReadOnly / WorkspaceWrite / DangerFullAccess`).

### Config file loading

`RuntimeConfig.permission_mode()` reads `permissionMode` (or legacy `permissions.defaultMode`) from `.claw/settings.json`. Aliases:
- `"default"` / `"plan"` / `"read-only"` → `ReadOnly`
- `"acceptEdits"` / `"auto"` / `"workspace-write"` → `WorkspaceWrite`
- `"dontAsk"` / `"danger-full-access"` → `DangerFullAccess`

### CodingAgent comparison

CodingAgent's `_security.py` uses a 3-tier bash allowlist (T1/T2/T3) hardcoded in `_BASE_DANGEROUS_PATTERNS`. There is no per-tool `PermissionMode` concept, no session-level permission policy object, and no way to configure permission escalation from a settings file.

---

## 7. Token Usage + Cost Tracking

**File:** `rust/crates/runtime/src/usage.rs` (310 lines)

### `TokenUsage`

```rust
pub struct TokenUsage {
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub cache_creation_input_tokens: u32,  // tokens written to prompt cache
    pub cache_read_input_tokens: u32,       // tokens read from prompt cache
}
```

Four distinct cost components: standard input, output, cache creation, cache read.

### `UsageTracker`

- `UsageTracker::new()` — fresh tracker
- `UsageTracker::from_session(messages)` — **reconstructs cumulative usage from existing session messages on resume**; usage continuity is automatic
- `record(usage: TokenUsage)` — accumulates per-turn usage
- Provides `total_cost_estimate()` via `pricing_for_model(model_name)` — model-name substring matching for haiku/sonnet/opus

### Serialization

`TokenUsage` is stored on every `ConversationMessage` (the `usage` field on assistant messages). Session serialization (`session.rs`) writes/reads all four token fields. This means usage survives a session save/load cycle.

### CodingAgent comparison

`session_cost_tracker.py`:
- Tracks `input_tokens` and `output_tokens` ✓
- Does **not** track `cache_creation_input_tokens` or `cache_read_input_tokens` ✗
- Does not reconstruct usage from session on resume ✗
- `flush()` wired (vol14 fix) ✓

---

## 8. Session Format

**File:** `rust/crates/runtime/src/session.rs` (436 lines)

### Shape

```rust
pub struct Session {
    pub version: u32,              // starts at 1, for future migrations
    pub messages: Vec<ConversationMessage>,
}

pub struct ConversationMessage {
    pub role: MessageRole,
    pub blocks: Vec<ContentBlock>,
    pub usage: Option<TokenUsage>,
}

pub enum ContentBlock {
    Text { text: String },
    ToolUse { id, name, input: String },
    ToolResult { tool_use_id, tool_name, output: String, is_error: bool },
}
```

### Roles

`System | User | Assistant | Tool` — `Tool` is a distinct fourth role, unlike OpenAI schema which uses `user` with `tool_result` content.

### Persistence

`Session::save_to_path()` / `Session::load_from_path()` — JSON file format. The `version` field enables forward-compatible migration logic.

### CodingAgent comparison

CodingAgent's `MessageManager` manages a `list[dict]` of messages (standard OpenAI role/content schema). There is no `version` field, no typed `ContentBlock` enum, and no per-message `usage` field.

---

## 9. Configuration System

**File:** `rust/crates/runtime/src/config.rs` (1294 lines)

### Config file search order

```
~/.claw.json                       (user, legacy)
~/.claw/settings.json              (user, canonical)
{cwd}/.claw.json                   (project, legacy)
{cwd}/.claw/settings.json          (project, canonical)
{cwd}/.claw/settings.local.json    (local, highest precedence)
```

Each file is `ConfigSource::User | Project | Local`. Later files override earlier ones (deep merge for nested objects; last-write-wins for scalars). MCP server configs are collected from all sources — project can add servers, local can override individual servers.

### `RuntimeFeatureConfig`

Everything is pulled from `~/.claw/settings.json` (and project overrides):

```rust
pub struct RuntimeFeatureConfig {
    hooks: RuntimeHookConfig,         // PreToolUse / PostToolUse shell commands
    plugins: RuntimePluginConfig,     // enabledPlugins, externalDirectories, etc.
    mcp: McpConfigCollection,         // mcpServers: { name: config }
    oauth: Option<OAuthConfig>,       // OAuth2 for provider auth
    model: Option<String>,            // default model override
    permission_mode: Option<ResolvedPermissionMode>,
    sandbox: SandboxConfig,           // sandbox settings
}
```

### MCP transport types

`McpServerConfig` enum supports: `Stdio | Sse | Http | Ws | Sdk | ManagedProxy`. Each has typed fields; `ManagedProxy` is for Claude.ai-proxied servers.

### OAuth support

Full OAuth2 config in `OAuthConfig { client_id, authorize_url, token_url, callback_port, manual_redirect_url, scopes }`. Per-MCP-server OAuth via `McpOAuthConfig` on each remote server config.

### CodingAgent comparison

CodingAgent has `src/config/providers.json` and a `UserPrefs` dataclass in `user_prefs.py` for per-user preferences. There is no:
- Project-level settings file (`.claw/settings.json` equivalent)
- Deep-merge across multiple config layers
- Hook configuration in a settings file
- Per-project permission mode
- Sandbox configuration file

---

## 10. Sandbox

**File:** `rust/crates/runtime/src/sandbox.rs` (364 lines)

### Modes

```rust
pub enum FilesystemIsolationMode {
    Off,
    WorkspaceOnly,   // default
    AllowList,       // explicit list of permitted mounts
}

pub struct SandboxConfig {
    pub enabled: Option<bool>,
    pub namespace_restrictions: Option<bool>,  // Linux unshare namespaces
    pub network_isolation: Option<bool>,
    pub filesystem_mode: Option<FilesystemIsolationMode>,
    pub allowed_mounts: Vec<String>,
}
```

### Linux sandbox execution

On Linux with `unshare` available, `build_linux_sandbox_command()` wraps shell commands in:
```
unshare --user --map-root-user --mount --ipc --pid --uts --fork [--net] sh -lc <command>
```

With env vars: `HOME` → `{cwd}/.sandbox-home`, `TMPDIR` → `{cwd}/.sandbox-tmp`, `CLAW_SANDBOX_FILESYSTEM_MODE`, `CLAW_SANDBOX_ALLOWED_MOUNTS`.

### Container detection

`detect_container_environment()` checks: `/.dockerenv`, `/run/.containerenv`, env vars (`CONTAINER`, `DOCKER`, `PODMAN`, `KUBERNETES_SERVICE_HOST`), `/proc/1/cgroup` patterns. Used to decide whether sandbox is supported.

### CodingAgent comparison

CodingAgent uses `DANGEROUS_PATTERNS` regex allowlist + `bash_readonly` flag in `_security.py` / `file_tools.py`. No `unshare`-based namespace isolation, no `filesystem_mode` config, no network isolation.

---

## 11. MCP Integration

**File:** `rust/crates/runtime/src/mcp.rs` (300 lines), `mcp_client.rs`, `mcp_stdio.rs`

### Tool naming convention

MCP tools are prefixed: `mcp__{server_name}__{tool_name}` where the server name and tool name are normalized: `[a-zA-Z0-9_-]` only, other characters → `_`.

For `claude.ai`-prefixed server names, consecutive underscores are collapsed and leading/trailing underscores trimmed.

### CCR proxy URL unwrapping

`unwrap_ccr_proxy_url()` detects Anthropic CCR proxy URLs (`/v2/session_ingress/shttp/mcp/` or `/v2/ccr-sessions/`) and extracts the real `mcp_url=` query parameter for stable server identity matching.

### Server identity hashing

`scoped_mcp_config_hash()` uses FNV-1a (`0xcbf29ce484222325` seed) to produce a stable 16-hex-char config hash. Used for change detection (start/stop servers on config change).

### CodingAgent comparison

CodingAgent has `mcp_stdio_server.py` (MCP STDIO server mode — **incoming** MCP calls to CodingAgent). claw-code's `mcp_client.rs` is an **outgoing** MCP client (CodingAgent calls external MCP servers). CodingAgent also has `mcp_servers.py` for outgoing MCP client connections (Stage 28).

---

## 12. LSP Integration

**Files:** `rust/crates/lsp/src/client.rs` (463 lines), `manager.rs` (191 lines)

### `LspClient`

Full async JSON-RPC LSP client (tokio):
- `connect()` — spawns LSP server process, initializes with `textDocument/publishDiagnostics` + `textDocument/definition` + `textDocument/references` capabilities
- `open_document()` / `change_document()` / `save_document()` / `close_document()` — document lifecycle
- `go_to_definition()` — returns `Vec<SymbolLocation>`
- `find_references()` — returns `Vec<SymbolLocation>` with deduplication
- `diagnostics_snapshot()` — returns current `BTreeMap<uri, Vec<Diagnostic>>`
- Incoming `textDocument/publishDiagnostics` notifications collected in real-time

### `LspManager`

Manages multiple LSP servers indexed by file extension. Lazy-connects on first use. `context_enrichment(path, position)` bundles diagnostics + definitions + references into `LspContextEnrichment` which is injected into the system prompt via `SystemPromptBuilder::with_lsp_context()`.

### CodingAgent comparison

CodingAgent has `lsp_client.py` which:
- Sends `textDocument/definition` and `textDocument/references` ✓
- Tracks diagnostics ✓
- Does **not** inject LSP context into the system prompt ✗ (claw-code injects via `with_lsp_context()`)

---

## 13. Plugin System

**File:** `rust/crates/plugins/src/lib.rs` (2943 lines)

### Plugin kinds

`PluginKind::Builtin | Bundled | External`

Plugins are discovered from:
- Bundled root (shipped with claw-code binary)
- User install root (`~/.claw/plugins/installed/`)
- External directories (configured in `settings.json`)

### Plugin manifest (`plugin.json`)

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "...",
  "permissions": ["read", "write", "execute"],
  "defaultEnabled": true,
  "hooks": { "PreToolUse": ["./hooks/pre.sh"], "PostToolUse": [] },
  "lifecycle": { "Init": ["./scripts/init.sh"], "Shutdown": [] },
  "tools": [{
    "name": "my_tool",
    "description": "...",
    "inputSchema": { ... },
    "command": "./tools/my_tool.sh",
    "requiredPermission": "read-only"
  }],
  "commands": [{ "name": "/my-cmd", "description": "...", "command": "./cmd.sh" }]
}
```

### `PluginTool.execute()`

Spawns the plugin tool command as a subprocess with:
- Input JSON sent on **stdin**
- Env vars: `CLAW_PLUGIN_ID`, `CLAW_PLUGIN_NAME`, `CLAW_TOOL_NAME`, `CLAW_TOOL_INPUT`, `CLAW_PLUGIN_ROOT`
- Stdout = tool result string
- Non-zero exit = `PluginError::CommandFailed`

### Plugin enable/disable

`RuntimePluginConfig.enabled_plugins` is a `BTreeMap<String, bool>`. Plugin IDs have the format `"plugin-name@kind"` (e.g. `"tool-guard@builtin"`). Configured via `settings.json`.

### Plugin lifecycle hooks

`Init` hooks run at startup; `Shutdown` hooks run at exit. Both are shell commands.

---

## 14. Built-in Tools

**File:** `rust/crates/tools/src/lib.rs` (4469 lines)

### `mvp_tool_specs()` — 16 built-in tools

| Tool | Permission | Description |
|------|-----------|-------------|
| `bash` | DangerFullAccess | Shell command execution, optional sandbox bypass |
| `read_file` | ReadOnly | Read file with offset/limit pagination |
| `write_file` | WorkspaceWrite | Write file |
| `edit_file` | WorkspaceWrite | String-replace edit with `replace_all` option |
| `glob_search` | ReadOnly | Glob pattern file search |
| `grep_search` | ReadOnly | Regex content search with context lines, offset, multiline |
| `WebFetch` | ReadOnly | Fetch URL + prompt |
| `WebSearch` | ReadOnly | Web search with domain filtering |
| `TodoWrite` | WorkspaceWrite | Update structured task list (has `activeForm` field) |
| `Skill` | ReadOnly | Load skill definition |
| `Agent` | DangerFullAccess | Spawn subagent |
| `ToolSearch` | ReadOnly | Search tool registry |
| `NotebookEdit` | WorkspaceWrite | Edit Jupyter notebook cell |
| `Sleep` | ReadOnly | Wait `duration_ms` |
| `SendUserMessage` (alias `Brief`) | ReadOnly | Send message to user with `status: normal\|proactive` |
| `Config` | WorkspaceWrite | Get/set Claw Code settings |
| `StructuredOutput` | ReadOnly | Return structured output |
| `REPL` | DangerFullAccess | Execute code in REPL subprocess |
| `PowerShell` | DangerFullAccess | Execute PowerShell command |

Total: **19 built-in tools** (some aliases: `SendUserMessage` / `Brief` → same handler).

### Tool name normalization

`normalize_tool_name()`: trim + replace `-` with `_` + to lowercase. Aliases: `read`→`read_file`, `write`→`write_file`, `edit`→`edit_file`, `glob`→`glob_search`, `grep`→`grep_search`.

### `GlobalToolRegistry`

Merges built-in tools + plugin tools. Validates no name conflicts. Exposes `definitions()`, `permission_specs()`, and `execute()`.

---

## 15. Bootstrap Phases

**File:** `rust/crates/runtime/src/bootstrap.rs` (56 lines)

Startup is decomposed into named phases for profiling:

```
CliEntry → FastPathVersion → StartupProfiler → SystemPromptFastPath →
ChromeMcpFastPath → DaemonWorkerFastPath → BridgeFastPath →
DaemonFastPath → BackgroundSessionFastPath → TemplateFastPath →
EnvironmentRunnerFastPath → MainRuntime
```

Fast-path phases exit early (before the full runtime initializes) for version check, system-prompt print, Chrome MCP bridge, daemon worker, etc.

---

## 16. Key Pattern Summary (for CodingAgent parity)

| Pattern | claw-code implementation | CodingAgent gap |
|---------|-------------------------|-----------------|
| Deterministic compaction | Token-count based, no LLM call, structured sections | LLM-based prose compaction only |
| Automatic compaction trigger | `should_compact()` checked after every turn | Only on manual `/compact` |
| Shell hooks with deny semantics | `exit 2` = deny, stdin-JSON payload, pre+post | Pre-only, no deny, no stdin-JSON |
| Plugin system | Manifest-based, lifecycle init/shutdown, tool + command registration | No equivalent |
| Per-tool permission policy | `PermissionMode` enum per tool, `PermissionPolicy` per session | Hardcoded bash allowlist only |
| Cache token tracking | `cache_creation_input_tokens` + `cache_read_input_tokens` | Not tracked |
| LSP → system prompt injection | `with_lsp_context()` adds diagnostics + defs + refs as prompt section | LSP client exists but not injected |
| `CLAW.md` project instructions | Discovered in all ancestor directories, deduplicated, budget-capped | No equivalent |
| `__DYNAMIC_BOUNDARY__` | Actual constant used to set `cache_control` breakpoint | Not implemented |
| Settings file layers | 5-file deep merge with User/Project/Local scopes | Single `providers.json` + `UserPrefs` |
| Session `version` field | Enables forward-compatible session migration | Not present |
| Usage reconstruction on resume | `UsageTracker::from_session()` re-sums from message history | Not implemented |
| Sandbox via `unshare` | Linux namespace isolation (user/mount/net/ipc/pid/uts) | Regex allowlist only |
| `Send/Brief` tool | Agent-to-user async messaging, `normal\|proactive` status | No equivalent |
| `REPL` / `PowerShell` tools | Language-agnostic REPL + Windows PowerShell | Not in built-in set |
