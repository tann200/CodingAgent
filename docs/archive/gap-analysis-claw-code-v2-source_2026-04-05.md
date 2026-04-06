# Gap Analysis: claw-code-main vs CodingAgent
*Generated: 2026-04-03*

This document covers all 15 dimensions of the claw-code-main reference implementation compared
to CodingAgent's current state. Items are rated: ✅ Parity, ⚠️ Subpar, ❌ Missing.

---

## 1. Core Runtime / Orchestration

### claw-code-main
`ConversationRuntime<C: ApiClient, T: ToolExecutor>` — generic over provider and tool executor.
Fields: `session`, `api_client`, `tool_executor`, `permission_policy`, `system_prompt`,
`max_iterations`, `usage_tracker`, `hook_runner`. Entry point: `new_with_features()` builder.

### CodingAgent
LangGraph state machine (`AgentState` TypedDict) with 12 node pipeline. `Orchestrator` class
holds tool registry, adapter, event bus, and graph. More complex pipeline with specialised nodes
(analyst_delegation, plan_validator, step_controller, etc.).

### Gaps
| # | Item | Severity |
|---|------|----------|
| O-1 | `ConversationRuntime` has a `max_iterations` field enforced at the runtime level. CodingAgent enforces `max_turns` only inside `perception_node`. A cancelled graph or a node bypass could escape the cap. | ⚠️ |
| O-2 | claw uses typed generics (`ApiClient` + `ToolExecutor` traits) for swappable provider/executor; CodingAgent uses duck-typed adapter with no formal interface contract, making testing harder. | ⚠️ |
| O-3 | claw has a `bootstrap.rs` (`BootstrapPhase`, `BootstrapPlan`) for structured initialisation sequencing. CodingAgent's `__init__` does everything inline — no phased startup, so partial-init failures are opaque. | ⚠️ |

---

## 2. Tool System

### claw-code-main
`ToolSpec { name, description, input_schema: Value, required_permission: PermissionMode }`.
`GlobalToolRegistry::with_plugin_tools()` — discovers plugin tools and **detects name conflicts**
between builtin and plugin tools. `normalize_allowed_tools()` — resolves aliases globally at
startup. About 30 builtin tool specs.

### CodingAgent
`ToolRegistry` class with `@tool` decorator auto-discovery. `build_registry()` discovers
~17 builtin modules. `TOOL_ALIASES` dict in `tools_config.py`. PermissionLevel enum (5 values).

### Gaps
| # | Item | Severity |
|---|------|----------|
| T-1 | claw detects **plugin–builtin name conflicts** at registration time and raises an error. CodingAgent silently overwrites earlier registrations — a plugin could shadow `read_file` with no warning. | ❌ |
| T-2 | claw attaches `required_permission: PermissionMode` directly to each `ToolSpec`. CodingAgent's `TOOL_PERMISSIONS` dict is separate from tool definition — they can drift out of sync (tool added without a permissions entry). | ⚠️ |
| T-3 | claw resolves aliases **once at startup** in `normalize_allowed_tools()`. CodingAgent resolves aliases per-call in `execute_tool()` — missed in tool listings, schema export, and permission checks. | ⚠️ |
| T-4 | claw `ToolSpec.input_schema` is full JSON Schema. CodingAgent's `_minimal_schema()` is an approximation that misses `$defs`, `anyOf`, `minLength`, pattern constraints, etc. | ⚠️ |
| T-5 | claw edit operations surface diffs with hunk headers (`@@ -L,N +L,N @@`) and line numbers **before writing**, and the model's input format uses structured hunks. CodingAgent uses `old_string`/`new_string` pairs — the `file.diff.preview` event fires a diff to the EventBus but the model never sees a structured hunk in its input or result. | ⚠️ |
| T-6 | claw's glob reports `truncated: true` + `total_found: N` when results exceed the 10,000-file cap. CodingAgent's `glob` tool returns a plain list with no truncation signal — the model cannot tell it got partial results. | ❌ |

**T-5 — Structured patch hunks (P2)**
`src/tools/patch_tools.py` exists. Add `generate_unified_diff(path, old_content, new_content, context_lines=3) -> str` and call it in `edit_file_atomic` / `edit_by_line_range` to (a) populate the `file.diff.preview` event payload with a full hunk and (b) return the applied patch in the tool result so the model sees the actual diff on its next turn. Optionally add an `apply_patch` tool accepting a unified diff string.

**T-6 — Glob truncation reporting (P3)**
In `src/tools/repo_tools.py` add `MAX_GLOB_RESULTS = 10_000`. After collecting all matches return `{"files": results[:MAX_GLOB_RESULTS], "truncated": True, "total_found": len(results)}` when the cap is hit instead of a plain list.

---

## 3. Context Management / Compaction

### claw-code-main
`compact.rs` — `CompactionConfig { preserve_recent_messages: 4, max_estimated_tokens: 10_000 }`.
`should_compact()` checks both token count AND message count. Older messages replaced with a
structured System summary block (`<summary>…</summary>`) that includes tools mentioned, user
requests, and pending work. Merge logic combines multiple compaction rounds.

### CodingAgent
`distiller.py` — `distill_context()` compacts at ≥50 messages, writes checkpoint to
`.agent-context/compaction_checkpoint.md`. Summary injected into history as assistant message.

### Gaps
| # | Item | Severity |
|---|------|----------|
| C-1 | claw triggers on **token count** (10k tokens). CodingAgent triggers on **message count** (50). Message count is a poor proxy — a 50-message conversation with tiny tool results is fine; a 5-message conversation with huge file reads may overflow. | ⚠️ |
| C-2 | claw's summary format (`<summary>` XML block with structured categories) is injected as a **System message**, which most providers treat as authoritative context. CodingAgent injects as an assistant message, which is lower-priority and can be ignored by the model. | ⚠️ |
| C-3 | claw's `merge_compact_summaries()` combines multiple compaction rounds without duplication. CodingAgent does not deduplicate across compaction checkpoints — repeated summaries accumulate. | ⚠️ |
| C-4 | claw's `get_compact_continuation_message()` produces a user-turn continuation prompt after compaction so the model knows context was summarised. CodingAgent has no such signal. | ❌ |

---

## 4. Session Persistence

### claw-code-main
`session.rs` — `Session { version: u32, messages: Vec<ConversationMessage> }`, versioned JSON.
Sessions saved to `.agent-sessions/` with content-addressed filenames. `--resume-session <path>`
CLI flag to continue any prior session. `remote.rs` provides upstream proxy with session tokens.

### CodingAgent
`TextualAppBase._get_history_path()/_load_history()/_save_history()` with atomic writes (P3-10).
History saved per-project in `.agent/`. No session resume from CLI; sessions auto-resume on
app restart for the same working dir.

### Gaps
| # | Item | Severity |
|---|------|----------|
| S-1 | claw has **explicit versioned session files** (`version` field, content-addressable names). CodingAgent's history format has no version field — format changes will silently corrupt old sessions. | ❌ |
| S-2 | claw's `--resume-session <path>` allows resuming **any prior session** by path. CodingAgent only restores the most recent session for the working directory — no way to resume an older session. | ❌ |
| S-3 | claw's `remote.rs` supports **remote proxy sessions** with session tokens for team sharing. CodingAgent has no remote session sharing. | ❌ |

---

## 5. Authentication / OAuth

### claw-code-main
`oauth.rs` — full PKCE OAuth 2.0 flow: `generate_pkce_pair()` (S256 code challenge),
`generate_state()`, `OAuthAuthorizationRequest`, `OAuthTokenExchangeRequest`,
`OAuthRefreshRequest`, `parse_oauth_callback_query()`, `save_oauth_credentials()`,
`load_oauth_credentials()`. CLI `login`/`logout` actions.

### CodingAgent
Settings panel with API key input saved to `prefs.json`. No OAuth flow.

### Gaps
| # | Item | Severity |
|---|------|----------|
| A-1 | claw supports **PKCE OAuth 2.0** (code exchange, refresh, credential storage). CodingAgent only supports static API keys — no support for providers that require OAuth (e.g., Google Vertex, Azure AD, future Anthropic web auth). | ❌ |
| A-2 | claw has `login`/`logout` CLI commands. CodingAgent has no auth CLI commands — keys must be entered via TUI settings. | ❌ |
| A-3 | claw persists credentials securely with `save_oauth_credentials()`/`load_oauth_credentials()`. CodingAgent stores API keys in plaintext `prefs.json`. | ⚠️ |

---

## 6. Permissions

### claw-code-main
`PermissionMode` enum: `ReadOnly | WorkspaceWrite | DangerFullAccess | Prompt | Allow`.
`PermissionPolicy { active_mode, tool_requirements: BTreeMap<String, PermissionMode> }`.
`PermissionPrompter` trait — runtime prompting per tool. Each `ToolSpec` carries its
`required_permission`. Authorization: `active_mode >= required_mode` → allow.

### CodingAgent
`PermissionLevel` enum: `READ_ONLY | WORKSPACE_WRITE | DANGER | PROMPT | ALLOW` (added this session).
`TOOL_PERMISSIONS` dict, `get_tool_permission()`, `set_tool_permission()`. No `PermissionPrompter`
trait — no runtime prompting.

### Gaps
| # | Item | Severity |
|---|------|----------|
| P-1 | claw has a `PermissionPrompter` trait — when `Prompt` mode is active, the user is asked **per tool call**. CodingAgent has the `PROMPT` level in the enum but no wiring to actually prompt the user before dangerous tool calls. | ❌ |
| P-2 | claw's `PermissionPolicy.tool_requirements` maps **individual tools** to minimum required modes. CodingAgent's `TOOL_PERMISSIONS` does the same, but the orchestrator's `execute_tool()` does **not check the active permission mode** against the required level — it's unused. | ❌ |
| P-3 | claw has a `--permission-mode <mode>` CLI flag for setting active mode per-session. CodingAgent has no equivalent CLI flag (only sandbox level). | ⚠️ |

---

## 7. Sandboxing

### claw-code-main
`sandbox.rs` — `SandboxConfig { enabled, namespace_restrictions, network_isolation,
filesystem_mode: FilesystemIsolationMode }`. `FilesystemIsolationMode { Off, WorkspaceOnly,
AllowList { paths } }`. Separate network isolation flag.

### CodingAgent
`sandbox.py` — `run_sandboxed()` wraps in `bwrap` when available (added this session).
`sandbox_level`: "off" / "workspace" / "full". Falls back gracefully to `subprocess.run`.

### Gaps
| # | Item | Severity |
|---|------|----------|
| SB-1 | claw's `AllowList { paths }` filesystem mode allows **arbitrary additional bind mounts** (e.g., a specific secrets dir). CodingAgent's "workspace" mode is binary — no custom allowlist. | ⚠️ |
| SB-2 | claw has an explicit **network isolation** flag separate from filesystem mode. CodingAgent's bwrap invocation has no network namespacing flag. | ⚠️ |
| SB-3 | claw's sandbox config is part of `RuntimeFeatureConfig` and persists per-session. CodingAgent's sandbox level is a global module variable — concurrent sessions share one level. | ⚠️ |

---

## 8. Hooks

### claw-code-main
`hooks.rs` — `HookRunner { config: RuntimeHookConfig }`. `HookEvent::PreToolUse { name, input }`
and `PostToolUse { name, input, result }`. `HookRunResult::Allow | Deny { reason }`.
Registered in `ConversationRuntime::new_with_features()`. Full async execution with
structured allow/deny.

### CodingAgent
`tool_hooks.py` — `ToolHookRunner` (added this session). Reads `.agent/hooks.json` +
`~/.coding_agent/hooks.json`. `run_pre()` returns `HookResult(allowed, reason)`. `run_post()`.
fnmatch pattern matching per tool name.

### Gaps
| # | Item | Severity |
|---|------|----------|
| H-1 | claw's hooks are **async** (non-blocking). CodingAgent's hooks use `subprocess.run` (blocking). A slow hook (e.g., calling an external API) will block the event loop and freeze the TUI. | ⚠️ |
| H-2 | claw's `HookEvent` carries structured `input: Value` (JSON). CodingAgent passes args as a JSON-encoded string. Hook scripts get less structured data. | ⚠️ |
| H-3 | claw hooks are integrated into `ConversationRuntime` — they run even when the agent bypasses the orchestrator (e.g., via delegation). CodingAgent's hooks only fire inside `Orchestrator.execute_tool()` — delegation via `subagent_tools` may bypass them. | ⚠️ |

---

## 9. Configuration

### claw-code-main
`config.rs` — `ConfigLoader::discover(cwd)` walks upward finding config files per
`ConfigSource::User | Project | Local`. `RuntimeFeatureConfig` with optional fields for
hooks, plugins, MCP collection, sandbox, and OAuth. Three-layer hierarchy.

### CodingAgent
`config_loader.py` — `load_merged_config()` with 4-layer merge: bundled `providers.json` →
`~/.config/codingagent/config.json` → `.agent/config.json` → `.agent/config.local.json`
(added this session). `_deep_merge()` and `get()` shortcut.

### Gaps
| # | Item | Severity |
|---|------|----------|
| CF-1 | claw's `RuntimeFeatureConfig` gates entire subsystems (MCP, plugins, hooks, OAuth) — unneeded features don't initialise at all. CodingAgent always initialises all subsystems. | ⚠️ |
| CF-2 | claw's config is validated against typed structs at load time — bad config surfaces as a startup error. CodingAgent's config is an untyped dict — invalid values are silently ignored. | ⚠️ |
| CF-3 | claw's `ConfigSource` enum includes `User` (global, `~/.config/`) and `Project` + `Local` layers. CodingAgent has the same layers but uses plain strings for layer names with no schema validation. | ⚠️ |

---

## 10. MCP (Model Context Protocol)

### claw-code-main
`McpTransport` enum — 6 transport types: `Stdio | Sse | Http | Ws | Sdk | ManagedProxy`.
Per-transport config structs. OAuth on remote transports. `mcp_client.rs` + `mcp_stdio.rs`.
`McpServerManager` (referenced in config). Full JSON-RPC bidirectional communication.

### CodingAgent
`mcp_server.py` — MCP stdio server (inbound). Resources, prompts, sampling, completion all
implemented (P4-2). `CrossSessionBus` wiring. No outbound MCP client; no SSE/HTTP/WS transports.

### Gaps
| # | Item | Severity |
|---|------|----------|
| M-1 | CodingAgent only implements an **inbound MCP server** (other clients connect to it). claw implements an **outbound MCP client** — CodingAgent cannot connect to external MCP tool providers (e.g., databases, browser automation servers). | ❌ |
| M-2 | claw supports 6 MCP transports including SSE, HTTP, WebSocket, and managed proxy. CodingAgent supports only stdio (inbound). | ❌ |
| M-3 | claw's remote MCP transports support OAuth headers. CodingAgent's MCP has no auth. | ❌ |
| M-4 | claw has `McpServerManager` to lifecycle-manage multiple MCP servers. CodingAgent has one server instance — no multi-server management. | ❌ |

---

## 11. Memory

### claw-code-main
No explicit long-term memory system identified. Session persistence is the primary mechanism.
Compaction serves as implicit lossy memory.

### CodingAgent
`memory_tools.py` + `VectorStore` + `memory_update_node`. `distiller.py` adds to vector store
after compaction (P3-7). `MemoryEntry` with category/tags.

### Gaps
| # | Item | Severity |
|---|------|----------|
| ME-1 | CodingAgent's memory exceeds claw's — this is a CodingAgent **advantage**. No gap here. | ✅ |

---

## 12. Provider / Adapter Support

### claw-code-main
`api/src/lib.rs` — `ProviderClient` trait with `send_message()` and streaming. Targets
Anthropic API natively. `usage.rs` — `ModelPricing` with per-model rates for Haiku / Opus /
Sonnet (input, output, cache_creation, cache_read). `format_usd()` for display.

### CodingAgent
`OpenAICompatibleAdapter` base class + `LmStudioAdapter` + `OpenRouterAdapter`. Per-turn
cost display from `_usage_buffer`. No model pricing table — uses response metadata only.

### Gaps
| # | Item | Severity |
|---|------|----------|
| PR-1 | claw has a **model pricing table** (`pricing_for_model()`) with accurate per-model rates including cache creation/read costs. CodingAgent relies solely on the provider's response metadata — if the provider omits cost fields, cost display shows $0. | ⚠️ |
| PR-2 | claw targets **Anthropic native API** with typed `MessageRequest`/`MessageResponse`. CodingAgent uses OpenAI-compat layer — some Anthropic-specific features (extended thinking details, fine-grained cache stats) may be lost in translation. | ⚠️ |
| PR-3 | claw has no OpenRouter / LM Studio adapter. CodingAgent has **broader provider support** here. | ✅ |

---

## 12b. TUI

### claw-code-main
After each LLM response the TUI shows: input tokens, output tokens, cached tokens, and USD cost
for that turn plus running session totals. Shift+Enter / Ctrl+J inserts a newline in the REPL
input field for multi-paragraph prompts.

### CodingAgent
`telemetry.py` accumulates token counts to a rotating log file. The TUI log panel shows text
events but no structured token or cost readout per turn. The input widget is a single-line
Textual `Input` — no multiline support.

### Gaps
| # | Item | Severity |
|---|------|----------|
| TUI-1 | claw shows **per-turn token breakdown** (input / output / cache tokens + USD cost) directly in the TUI after each response. CodingAgent has no per-turn token display in the TUI — token data exists in `_usage_buffer` but is never rendered per-turn. **Add a `usage.turn_summary` EventBus event** published after each `call_model()` and a footer subscriber in `textual_app_impl.py`. | ⚠️ |
| TUI-2 | claw supports **multiline REPL input** (Shift+Enter inserts newline). CodingAgent uses a single-line Textual `Input` widget — users cannot write multi-paragraph prompts. **Swap to Textual `TextArea`** with `shift+enter` → newline, `enter` → submit. | ❌ |

**TUI-1 — Per-turn token display (P2)**
In `orchestrator.py`, after each `call_model()`, publish `event_bus.publish("usage.turn_summary", {"input_tokens": N, "output_tokens": N, "model": "...", "cost_usd": X})`. In `textual_app_impl.py`, subscribe and update a footer widget or append to `LogPanel`. The `_usage_buffer` already holds the data; this is a wiring change only.

**TUI-2 — Multiline REPL input (P3)**
Replace the single-line `Input` widget in `textual_app_impl.py` with Textual's `TextArea` (available since Textual ≥ 0.47). Bind `shift+enter` → insert `\n`; `enter` → submit. Adjust CSS in `src/ui/styles/` for `TextArea` sizing.

---

## 13. CLI

### claw-code-main
`CliAction` enum — 11 variants including: `init`, `login`, `logout`, `repl`, `prompt`,
`agents`, `skills`, `--system-prompt`, `--resume-session`, `--version`, `--help`.
`--output-format`, `--allowed-tools`, `--permission-mode` flags on `prompt`/`repl`.

### CodingAgent
`_parse_args()` — `--output-format pretty|json|raw`, `--task`, `--workdir`, `--sandbox-level`
(added this session). `main()` with headless path.

### Gaps
| # | Item | Severity |
|---|------|----------|
| CLI-1 | claw has `init` subcommand — scaffolds `.agent/` structure for a new project. CodingAgent has no `init` command. | ❌ |
| CLI-2 | claw has `login`/`logout` subcommands for OAuth credential management. CodingAgent has no CLI auth commands. | ❌ |
| CLI-3 | claw has `--allowed-tools <list>` flag — restricts which tools are available for a session. CodingAgent cannot restrict tools from CLI. | ❌ |
| CLI-4 | claw has `--permission-mode <mode>` CLI flag. CodingAgent has `--sandbox-level` but no permission mode flag. | ⚠️ |
| CLI-5 | claw has `--resume-session <path>` for explicit session continuity. CodingAgent restores most-recent session only. | ❌ |
| CLI-6 | claw has `--system-prompt <cwd> <date>` to print the resolved system prompt (debugging). CodingAgent has no equivalent. | ⚠️ |

---

## 14. Testing

### claw-code-main
Rust `#[cfg(test)]` unit tests throughout all modules. No visible Python test suite.
`tests/` directory exists but content was not deeply explored.

### CodingAgent
1758+ tests covering unit, integration, e2e, and benchmarks. CI matrix (macOS + Ubuntu).
Coverage reporting. Regression suites for each audit cycle.

### Gaps
| # | Item | Severity |
|---|------|----------|
| TS-1 | CodingAgent's test coverage is **substantially better** than claw. No gap here. | ✅ |

---

## 15. LSP / Symbol Context

### claw-code-main
`lsp/src/lib.rs` — `LspManager`, `SymbolLocation`. Referenced as `LspContextEnrichment` in
`prompt.rs`. A real LSP client manager (not just a symbol index).

### CodingAgent
`lsp_context.py` — queries `SymbolGraph.nodes` (regex-based index). Gated by config flag
or `CODINGAGENT_LSP_CONTEXT=1`. Returns `<lsp_context>` block with up to 50 symbols.

### Gaps
| # | Item | Severity |
|---|------|----------|
| LSP-1 | claw has a real **LSP client** (`LspManager`) that can request live diagnostics, hover info, and go-to-definition from a running language server. CodingAgent's `lsp_context.py` queries a static regex-built symbol index — no live diagnostics or type information. | ⚠️ |
| LSP-2 | claw integrates LSP context directly into `prompt.rs` as part of `ProjectContext`. CodingAgent's LSP context is bolted on via `build_runtime_context()` — not structured as part of a `ProjectContext` abstraction. | ⚠️ |

---

## Priority Matrix

### P0 — Security / Correctness (fix immediately)
| ID | Gap |
|----|-----|
| T-1 | Plugin-builtin name conflict detection missing |
| P-1 | `PROMPT` permission level defined but never prompts user |
| P-2 | `TOOL_PERMISSIONS` not checked against active permission mode in `execute_tool()` |
| A-3 | API keys stored in plaintext `prefs.json` |

### P1 — High Impact (next sprint)
| ID | Gap |
|----|-----|
| T-3 | Alias resolution not applied to tool listings / permission checks |
| C-1 | Compaction triggers on message count, not token count |
| C-4 | No compact-continuation signal after context summarisation |
| S-1 | Session files have no version field |
| S-2 | No `--resume-session` CLI support |
| M-1 | No outbound MCP client (cannot connect to external MCP providers) |
| CLI-3 | No `--allowed-tools` CLI flag |
| CLI-1 | No `init` subcommand |

### P2 — Quality Improvements
| ID | Gap |
|----|-----|
| O-1 | `max_turns` not enforced outside `perception_node` |
| H-1 | Hook scripts run synchronously (blocking event loop) |
| C-2 | Compact summary injected as assistant message instead of system |
| T-2 | `required_permission` not co-located with tool definition |
| T-5 | No structured patch hunks in edit tools — model sees `old_string`/`new_string`, not unified diffs |
| TUI-1 | No per-turn token breakdown in TUI — `usage_buffer` data never rendered per-turn |
| PR-1 | No model pricing table — cost display breaks if provider omits fields |
| SB-2 | No network isolation in sandbox |
| CF-2 | Config not validated against typed schema at load |

### P3 — Nice to Have
| ID | Gap |
|----|-----|
| T-6 | Glob returns plain list with no `truncated`/`total_found` fields |
| TUI-2 | Single-line REPL input — no multiline (Shift+Enter) support |
| A-1 | No OAuth 2.0 support |
| A-2 | No `login`/`logout` CLI commands |
| LSP-1 | Static symbol index vs live LSP client |
| S-3 | No remote/shared sessions |
| CLI-4 | No `--permission-mode` CLI flag |
| CLI-6 | No `--system-prompt` debug command |
| SB-1 | No custom filesystem allowlist for sandbox |

---

## Existing CodingAgent Advantages Over claw-code-main

These areas where CodingAgent is **ahead** of the reference:

| Area | CodingAgent Advantage |
|------|-----------------------|
| **Pipeline** | 12-node LangGraph pipeline with specialist roles (analyst, debugger, planner) vs flat `run_turn()` loop |
| **Memory** | Persistent vector store + `memory_update_node` + distillation integration |
| **TUI** | Full Textual TUI with log panels, plan display, diff preview, settings |
| **Testing** | 1758+ tests with CI matrix, benchmarks, e2e scenarios |
| **Provider breadth** | LM Studio + OpenRouter + any OpenAI-compat provider |
| **Planning** | Structured plan with DAG steps, wave execution, plan validation |
| **Tool breadth** | ~17 tool modules including git, AST, web, project, skill, batch |
| **Config layers** | 4-layer config with workspace/local overrides |

---

*Document written: 2026-04-03. Based on Explore agent analysis of `/Users/tann200/PycharmProjects/claw-code-main`.*
