# Mitigation Plan: CodingAgent — Parity with opencode / claw-code

**Date:** 2026-04-03
**Source:** Gap analysis at `docs/audit/gap-analysis-vs-opencode-claw.md`
**Goal:** An open-source coding agent with proper orchestration, tools, determinism, and scalability from 9B models to frontier models.

---

## Guiding Principles

1. **Additive, not rewriting.** Every sprint item builds on the existing LangGraph graph, tool registry, and provider layer. No existing node is deleted unless it becomes dead code after a replacement is in place.
2. **Test before merge.** Each item must include a unit test (mock LLM where needed) and must not reduce the current test pass count.
3. **Small-model first.** When a feature has two implementation paths (one requiring a frontier model, one not), the 9B-capable path ships first.
4. **Open by default.** All provider integrations, tool implementations, and config schemas must work without proprietary API keys. Ollama / LM Studio are the reference local targets.
5. **No external runtime dependencies added without a fallback stub.** New optional deps (tiktoken, LSP server binaries) must have graceful degradation paths.

---

## Sprint Overview

| Sprint | Theme | Duration | P-level |
|---|---|---|---|
| **S0** | Token accuracy + typed errors + mock LLM | 1 week | P0 / P1 |
| **S1** | Model-tier routing + per-model prompt templates | 1 week | P0 |
| **S2** | LSP tools (diagnostics + references + symbols) | 2 weeks | P0 |
| **S3** | MCP client (stdio + HTTP/SSE) | 2 weeks | P0 |
| **S4** | Git workspace snapshots + event log | 1 week | P1 |
| **S5** | Session fork / revert + session diff | 1 week | P1 |
| **S6** | Cost tracking + per-provider prompts + hot-reload config | 1 week | P2 |
| **S7** | AST bash security + non-polling permission gate | 1 week | P2 |
| **S8** | Dynamic tool pruning + schema stripping | 1 week | P0 (model scaling) |
| **S9** | Cross-session memory injection + `/compact` command | 1 week | P2 |
| **S10** | Hook verification + `stuck` skill + golden file tests | 1 week | P3 |

---

## Sprint S0 — Token Accuracy, Typed Errors, Mock LLM

**Rationale:** The token counting error (±25%) is a silent correctness bug that affects every model size. Typed errors are needed before adding new error paths in S1+. The mock LLM adapter unblocks CI for all subsequent sprints.

### S0-A: Replace `len(content) // 4` with accurate token counting

**Files:** `src/core/inference/llm_manager.py`, `src/core/orchestration/token_budget.py`, `src/core/memory/distiller.py`, `src/core/inference/provider_context.py`

**Implementation:**
```python
# src/core/inference/tokenizer.py  (NEW)
from __future__ import annotations
from typing import Optional
import hashlib

def count_tokens(text: str, model_hint: Optional[str] = None) -> int:
    """Return token count for text.

    Priority:
    1. tiktoken (OpenAI models, available offline)
    2. transformers AutoTokenizer (HuggingFace models)
    3. Character heuristic: len(text) // 3.5  (more accurate than //4)

    model_hint is a model name string used to select the tiktoken encoding.
    Falls back gracefully if no matching encoding is found.
    """
    try:
        import tiktoken
        enc_name = _tiktoken_encoding_for_model(model_hint or "gpt-4o")
        enc = tiktoken.get_encoding(enc_name)
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        pass
    # Fallback: 3.5 chars per token (better approximation for English code)
    return max(1, int(len(text) / 3.5))

def _tiktoken_encoding_for_model(model: str) -> str:
    model = model.lower()
    if "gpt-4o" in model or "gpt-4" in model or "o1" in model or "o3" in model:
        return "o200k_base"
    if "gpt-3.5" in model or "gpt-4-turbo" in model:
        return "cl100k_base"
    # Local models (Ollama, LM Studio): use cl100k as proxy
    return "cl100k_base"
```

- Replace all `len(x) // 4` calls with `count_tokens(x, model_hint=active_model)`
- `TokenBudgetMonitor` must receive `model_hint` at construction or from `AgentState.model_name`
- Add `tiktoken` to `requirements.txt` as optional: `tiktoken>=0.7.0; python_version>="3.9"`
- `_DummyTokenizer` fallback (character-based) must pass existing tests without tiktoken installed

**Test:** `tests/unit/test_tokenizer.py` — verify count for known strings; verify fallback path.

### S0-B: Typed error hierarchy

**File:** `src/core/errors.py` (NEW)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

class ErrorCode(Enum):
    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    TOOL_FAILURE = "tool_failure"
    PARSE_FAILURE = "parse_failure"
    PERMISSION_DENIED = "permission_denied"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    VALIDATION_ERROR = "validation_error"
    BUDGET_EXCEEDED = "budget_exceeded"
    MAX_TURNS_REACHED = "max_turns_reached"
    DOOM_LOOP = "doom_loop"
    UNKNOWN = "unknown"

@dataclass
class AgentError(Exception):
    code: ErrorCode
    message: str
    retryable: bool = True
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"error_code": self.code.value, "message": self.message,
                "retryable": self.retryable, **self.context}
```

- All `{"status": "error", "message": "..."}` returns in nodes must construct an `AgentError` and call `.to_dict()`
- `AgentState` gets an optional `last_error: Optional[AgentError]` field (not in `history` reducer, just direct assignment)
- Existing string-based error returns remain valid during transition; typed errors are additive

**Test:** `tests/unit/test_errors.py` — verify serialization, `retryable` flag propagation.

### S0-C: Mock LLM adapter for CI

**File:** `src/core/inference/adapters/mock_adapter.py` (NEW)

```python
class MockAdapter:
    """Deterministic mock LLM adapter for unit and integration tests.

    Responses are driven by a script: a list of strings or callables.
    When the script is exhausted, returns the last entry or raises if strict=True.
    """
    def __init__(self, responses: list[str | Callable], strict: bool = False): ...
    def call(self, messages, tools=None, **kwargs) -> dict: ...
    def stream(self, messages, tools=None, **kwargs) -> Iterator[dict]: ...
    def get_models_from_api(self) -> dict: ...
    REQUIRES_API_KEY = False
```

- Register `type: "mock"` in `providers.json` schema (optional; only for test configs)
- `conftest.py` fixture: `mock_llm_adapter(responses=[...])` injects MockAdapter into ProviderManager
- Enables full node-level integration tests without any provider credentials

**Test:** `tests/unit/test_mock_adapter.py`; update 3 existing integration-style tests to use mock adapter.

---

## Sprint S1 — Model-Tier Routing + Per-Model Prompt Templates

**Rationale:** The core stated goal — "scalability from 9B to frontier models" — is unaddressed without explicit routing. This sprint is the single highest-impact change.

### S1-A: Model capability tiers

**File:** `src/core/inference/model_tiers.py` (NEW)

```python
from enum import Enum
from typing import Optional

class ModelTier(Enum):
    NANO = "nano"       # ≤7B params / ≤4K context; YAML tools; simple_mode
    SMALL = "small"     # 7–14B / 4–16K context; YAML tools; full pipeline
    MEDIUM = "medium"   # 14–70B / 16–128K context; JSON tools; full pipeline
    LARGE = "large"     # >70B / >128K context; JSON tools; parallel tool calls
    FRONTIER = "frontier" # Cloud frontier (GPT-4o, Claude Opus, Gemini Ultra)

def classify_model(model_name: str, context_window: int) -> ModelTier:
    """Classify a model into a tier based on name heuristics and context window."""
    ...

def get_tool_limit(tier: ModelTier) -> int:
    """Max tools to include in context for this tier."""
    return {NANO: 8, SMALL: 20, MEDIUM: 35, LARGE: 50, FRONTIER: 60}[tier]

def get_simple_mode(tier: ModelTier) -> bool:
    return tier == ModelTier.NANO
```

- `providers.json` gets optional `tier` field; if absent, `classify_model()` infers from name + context_window
- `AgentState` gets `model_tier: Optional[ModelTier]` field set in `perception_node`
- `ContextBuilder.build()` accepts `tier` and uses it to prune tool list via `get_tool_limit()`

**Config extension:**
```jsonc
// .agent/config.json — new optional fields
{
  "model_routing": {
    "planning_model": "ollama/qwen3:14b",    // used by planning_node
    "execution_model": "ollama/qwen3:7b",    // used by execution_node
    "analysis_model": "ollama/qwen3:14b",    // used by analysis_node
    "verification_model": null               // fallback: use execution_model
  }
}
```

- `orchestrator.py` reads `model_routing` from config at startup; passes per-node model override into `AgentState.current_model`
- Each node that calls LLM reads `state.get("current_model") or self.default_model`

### S1-B: Per-provider / per-model-family system prompt templates

**Directory:** `src/config/agent-brain/prompts/` (NEW)

Create provider-keyed system prompt partials injected by `ContextBuilder` based on detected provider family:

| File | Injects when |
|---|---|
| `prompts/local-small.md` | `tier in (NANO, SMALL)` — strict one-tool-per-message rule, explicit YAML format reminder |
| `prompts/local-medium.md` | `tier == MEDIUM` — relaxed but still YAML |
| `prompts/anthropic.md` | provider == "github_copilot" or "anthropic" — TodoWrite, parallel tool calls |
| `prompts/openai.md` | provider family == "openai" — JSON function calling style |
| `prompts/reasoning.md` | `is_reasoning_model()` — chain-of-thought budget instructions |
| `prompts/max-steps.md` | `step_count >= max_turns - 2` — disable tools, force summary |

- `ContextBuilder.build()` selects the matching partial and appends it to the system prompt after the role `.md`
- Partials are plain Markdown; no code required to add a new one

### S1-C: Dynamic tool list pruning

**File:** `src/core/context/context_builder.py`

```python
def _select_tools(self, registry: ToolRegistry, tier: ModelTier,
                  token_budget_remaining: int) -> list[Tool]:
    """Return a tool list that fits within token_budget_remaining.

    Ordering: core tools (read, write, edit, bash, grep, glob) first,
    then role-appropriate tools, then supplementary tools — drop from tail.
    """
    limit = get_tool_limit(tier)
    tools = registry.get_core_tools() + registry.get_role_tools(self.role)
    tools = tools[:limit]
    # Token-budget pruning: estimate tool description tokens and drop if over
    while tools and count_tokens(self._render_tools(tools)) > token_budget_remaining:
        tools = tools[:-1]
    return tools
```

- `ToolRegistry` gets `get_core_tools()` and `get_role_tools(role)` methods to support priority ordering
- For NANO tier, only the 8 core tools are included (equivalent to claw's `simple_mode`)

**Test:** `tests/unit/test_model_tiers.py`; `tests/unit/test_context_builder_tool_pruning.py`

---

## Sprint S2 — LSP Tools

**Rationale:** LSP diagnostics dramatically improve code quality for every model size. A 9B model that can read `lsp_diagnostics` can self-correct without requiring a second LLM call.

### S2-A: LSP client wrapper

**File:** `src/core/indexing/lsp_client.py` (NEW — replaces stub `lsp_context.py`)

```python
class LSPClient:
    """Async LSP client wrapping a language server subprocess.

    Uses JSON-RPC 2.0 over stdio. One client instance per workspace per language.
    Lifecycle: start() → initialize() → [tool calls] → shutdown()
    """
    def __init__(self, server_cmd: list[str], workspace_root: Path): ...
    async def start(self) -> None: ...
    async def get_diagnostics(self, file_uri: str) -> list[Diagnostic]: ...
    async def get_hover(self, file_uri: str, line: int, col: int) -> str: ...
    async def get_references(self, file_uri: str, line: int, col: int) -> list[Location]: ...
    async def get_definition(self, file_uri: str, line: int, col: int) -> list[Location]: ...
    async def get_symbols(self, file_uri: str) -> list[DocumentSymbol]: ...
    async def rename(self, file_uri: str, line: int, col: int, new_name: str) -> WorkspaceEdit: ...
    async def shutdown(self) -> None: ...
```

- Uses `asyncio.subprocess.create_subprocess_exec()` for the server process
- Language-to-server mapping stored in `src/config/lsp_servers.yaml` (NEW):
  ```yaml
  python:
    cmd: ["pylsp"]
    install: ["pip install python-lsp-server"]
    fallback: ["pyright", "--stdio"]
  typescript:
    cmd: ["typescript-language-server", "--stdio"]
    install: ["npm install -g typescript-language-server typescript"]
  rust:
    cmd: ["rust-analyzer"]
  go:
    cmd: ["gopls"]
  ```
- `LSPManager` (`src/core/indexing/lsp_manager.py`) — singleton; manages one `LSPClient` per language per workspace; lazy-start; auto-detect language from file extension
- `_DummyLSPClient` stub returns empty lists — allows tool execution without LSP servers installed

### S2-B: LSP tools exposed to the LLM

**File:** `src/tools/lsp_tools.py` (NEW)

```python
@tool(name="lsp_diagnostics", permission=PermissionLevel.READ_ONLY,
      description="Get lint/type errors for a file from the language server.")
async def lsp_diagnostics(path: str) -> ToolResult: ...

@tool(name="lsp_references", permission=PermissionLevel.READ_ONLY,
      description="Find all references to the symbol at the given line/column.")
async def lsp_references(path: str, line: int, col: int) -> ToolResult: ...

@tool(name="lsp_definition", permission=PermissionLevel.READ_ONLY,
      description="Go to the definition of the symbol at the given line/column.")
async def lsp_definition(path: str, line: int, col: int) -> ToolResult: ...

@tool(name="lsp_symbols", permission=PermissionLevel.READ_ONLY,
      description="List all symbols (functions, classes, variables) in a file.")
async def lsp_symbols(path: str) -> ToolResult: ...

@tool(name="lsp_hover", permission=PermissionLevel.READ_ONLY,
      description="Get type information and documentation for symbol at position.")
async def lsp_hover(path: str, line: int, col: int) -> ToolResult: ...

@tool(name="lsp_rename", permission=PermissionLevel.WORKSPACE_WRITE,
      description="Rename a symbol and all its references across the workspace.")
async def lsp_rename(path: str, line: int, col: int, new_name: str) -> ToolResult: ...
```

- All tools: `_DummyLSPClient` fallback returns `ToolResult(ok=True, output="LSP unavailable — install language server")` — never error out
- `verification_node` calls `lsp_diagnostics` after every file write in addition to pytest/ruff
- Register in `toolsets/coding.yaml` and `toolsets/debug.yaml`

### S2-C: Auto-formatter on write

**File:** `src/tools/file_tools.py` (extend `write_file` and `edit_file_atomic`)

- After successful write, check `src/config/formatters.yaml` for the file extension's formatter command
- Run formatter if available; treat failure as warning (not error)
- `formatters.yaml`:
  ```yaml
  .py: ["black", "--quiet", "{file}"]
  .ts: ["prettier", "--write", "{file}"]
  .tsx: ["prettier", "--write", "{file}"]
  .go: ["gofmt", "-w", "{file}"]
  .rs: ["rustfmt", "{file}"]
  ```

**Test:** `tests/unit/test_lsp_tools.py` (with DummyLSPClient)

---

## Sprint S3 — MCP Client (stdio + HTTP/SSE)

**Rationale:** Without MCP client support, CodingAgent cannot consume any of the growing ecosystem of MCP servers (databases, APIs, file systems, external tools). This is now a baseline expectation.

### S3-A: MCP client

**File:** `src/core/mcp/client.py` (NEW)

```python
class MCPClient:
    """MCP 1.0 client — connects to an MCP server and exposes its tools.

    Transports:
    - stdio: launches server_cmd as subprocess, communicates via stdin/stdout
    - http_sse: connects to server_url, uses SSE for server-to-client events

    Authentication:
    - None (anonymous)
    - Bearer token
    - OAuth 2.0 PKCE (for servers that require it)
    """
    def __init__(self, name: str, transport: Literal["stdio", "http_sse"],
                 server_cmd: Optional[list[str]] = None,
                 server_url: Optional[str] = None,
                 auth: Optional[MCPAuth] = None): ...

    async def connect(self) -> None: ...
    async def list_tools(self) -> list[MCPToolDefinition]: ...
    async def call_tool(self, name: str, arguments: dict) -> MCPToolResult: ...
    async def list_resources(self) -> list[MCPResource]: ...
    async def read_resource(self, uri: str) -> MCPResource: ...
    async def disconnect(self) -> None: ...
```

**File:** `src/core/mcp/manager.py` (NEW)

```python
class MCPManager:
    """Singleton; manages lifecycle of all configured MCP servers.

    Reads .agent/config.json `mcp` section:
      {
        "mcp": {
          "filesystem": {"transport": "stdio", "cmd": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
          "github":     {"transport": "http_sse", "url": "https://mcp.github.com/sse"}
        }
      }

    On startup: connects all servers, dynamically registers their tools into ToolRegistry.
    On tool call: routes to the correct MCPClient.
    """
    async def start_all(self) -> None: ...
    async def register_tools(self, registry: ToolRegistry) -> None: ...
    async def stop_all(self) -> None: ...
```

- MCP tools registered with `origin="mcp"` and `provider_name=server_name` in ToolRegistry
- Tools appear in the LLM's tool list like native tools
- `mcp_stdio_server.py` remains as-is (agent still exposes itself as MCP server)
- `_MCPToolWrapper` wraps each `MCPToolDefinition` as a `@tool`-decorated function using `functools.wraps`

### S3-B: Config schema extension

```jsonc
// .agent/config.json
{
  "mcp": {
    "<server_name>": {
      "transport": "stdio" | "http_sse",
      "cmd": ["..."],           // stdio only
      "url": "https://...",     // http_sse only
      "auth": {
        "type": "none" | "bearer" | "oauth_pkce",
        "token": "...",         // bearer
        "client_id": "..."      // oauth_pkce
      },
      "enabled": true
    }
  }
}
```

- Update `src/config/schema.json` to include `mcp` section
- Update `codingagent init` to scaffold an example `mcp` section

### S3-C: In-TUI MCP management commands

- `/mcp list` — lists connected servers and their tools
- `/mcp add <name> <transport> <cmd_or_url>` — adds and connects a new server at runtime
- `/mcp status` — shows connectivity status (connected / disconnected / error)

**Test:** `tests/unit/test_mcp_client.py` (mock MCP server via asyncio pipes); `tests/unit/test_mcp_manager.py`

---

## Sprint S4 — Git Workspace Snapshots + Event Log

**Rationale:** File-level rollback (SHA-256 checksums) is not sufficient for complex multi-file refactors. A git-based snapshot provides full workspace recovery from any point.

### S4-A: Git snapshot system

**File:** `src/core/orchestration/snapshot_manager.py` (NEW — extends RollbackManager)

```python
class GitSnapshotManager:
    """Captures and restores full workspace state using a bare git repo.

    Approach (same as opencode):
    - Bare repo at .agent-context/snapshots.git
    - Before every LLM message: git write-tree → store tree SHA in AgentState.snapshots[]
    - Revert: git read-tree <tree-sha> + git checkout-index -f -a
    - Does NOT create commits (write-tree only) — no pollution of the user's history
    """
    def __init__(self, workspace: Path): ...
    async def capture(self) -> str: ...           # returns tree SHA
    async def restore(self, tree_sha: str) -> None: ...
    async def diff_from(self, tree_sha: str) -> str: ...  # unified diff
    def list_snapshots(self) -> list[SnapshotEntry]: ...
```

- `AgentState` gets `snapshots: list[str]` (list of tree SHAs, appended per message)
- `perception_node` calls `snapshot_manager.capture()` before each LLM call
- `RollbackManager` (existing file-level) remains for step-level rollback; `GitSnapshotManager` is session-level
- `revert_session()` CLI command restores the last snapshot

### S4-B: Immutable event log

**File:** `src/core/orchestration/event_log.py` (NEW — extends existing session_store.py)

```python
class EventLog:
    """Append-only SQLite log of all agent events.

    Schema:
      CREATE TABLE events (
        id       TEXT PRIMARY KEY,   -- ULID
        session  TEXT NOT NULL,
        kind     TEXT NOT NULL,      -- node_enter, llm_call, tool_call, tool_result,
                                     --   plan_step, snapshot, error, user_message, assistant_message
        ts       REAL NOT NULL,      -- Unix epoch
        payload  TEXT NOT NULL       -- JSON
      )
    """
    def append(self, session_id: str, kind: str, payload: dict) -> str: ...  # returns ULID
    def replay(self, session_id: str, up_to: Optional[str] = None) -> list[Event]: ...
    def get_diff(self, session_id: str) -> str: ...  # all file changes in session
```

- Every node in the graph calls `event_log.append()` at entry and exit
- All LLM calls appended as `llm_call` events (prompt hash, model, token counts, latency)
- All tool calls appended as `tool_call` + `tool_result` pairs
- This replaces the redundant second JSON snapshot store; SQLite WAL remains as the single authority

**Test:** `tests/unit/test_snapshot_manager.py` (requires git); `tests/unit/test_event_log.py`

---

## Sprint S5 — Session Fork / Revert + Session Diff

**Rationale:** Safe experimentation without losing work is a table-stakes feature for a coding agent.

### S5-A: Session fork

**File:** `src/core/orchestration/session_store.py` (extend)

```python
async def fork_session(session_id: str, fork_point: Optional[str] = None) -> str:
    """Create a new session branching from session_id.

    fork_point: ULID event ID to fork from (if None, forks from latest state).
    Returns: new session_id.

    Implementation:
    1. Copy all events up to fork_point into new session in event_log
    2. Restore workspace snapshot at fork_point (via GitSnapshotManager)
    3. Return new session_id
    """
```

### S5-B: Session revert

```python
async def revert_session(session_id: str, to_snapshot: Optional[str] = None) -> None:
    """Restore workspace to the most recent (or specified) snapshot in this session."""
```

### S5-C: Session diff endpoint and TUI command

- `AgentState.session_id` → `snapshot_manager.diff_from(snapshots[0])` gives full session diff
- `/diff` TUI slash command: prints unified diff of all files changed since session start
- EventLog `get_diff()` method reconstructs diff from event log

**Test:** `tests/unit/test_session_fork.py`

---

## Sprint S6 — Cost Tracking + Per-Provider Prompts + Config Hot-Reload

### S6-A: Cost tracking

**File:** `src/core/inference/provider_context.py` (extend)

```python
PRICING: dict[str, tuple[float, float]] = {
    # (input $/1M tokens, output $/1M tokens)
    "gpt-4o": (5.0, 15.0),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-3": (0.25, 1.25),
    "gemini-2.0-flash": (0.10, 0.40),
    "deepseek-r1": (0.55, 2.19),
    # Local models: $0 cost
    "*": (0.0, 0.0),
}

def estimate_turn_cost(model: str, input_tokens: int, output_tokens: int) -> float: ...
```

- `AgentState` gets `session_cost_usd: float` (cumulative)
- Every LLM call in `llm_manager.call_model()` adds to `session_cost_usd`
- TUI status bar shows current session cost (already has token count display)
- EventLog `llm_call` events include `cost_usd` field

### S6-B: Per-provider system prompt templates (wire to ContextBuilder)

This implements the template files created in S1-B:

- `ContextBuilder.build()` detects provider family from `ProviderManager.get_active_provider()`
- Injects the matching partial from `src/config/agent-brain/prompts/`
- Template selection logic:
  ```python
  family = detect_provider_family(provider_name, model_name)
  tier = state.get("model_tier", ModelTier.MEDIUM)
  prompt_partial = load_prompt_partial(family, tier)
  ```

### S6-C: Config hot-reload

**File:** `src/core/config_loader.py` (extend)

- Use `watchfiles` (or `watchdog`) to watch `providers.json` and `.agent/config.json`
- On change: call `ProviderManager.reload()` and `ToolRegistry.reload()`
- Publish `config.reloaded` EventBus event
- `watchfiles` is optional: `watchfiles>=0.21; python_version>="3.9"`
- Without `watchfiles`, reload only happens at startup (current behavior preserved)

---

## Sprint S7 — AST Bash Security + Non-Polling Permission Gate

### S7-A: AST-level bash security analysis

**File:** `src/tools/bash_security.py` (NEW — replaces TIER3 prefix matching as primary check)

```python
import ast as _ast
import shlex

class BashRiskLevel(Enum):
    SAFE = "safe"
    WORKSPACE_WRITE = "workspace_write"
    DANGEROUS = "dangerous"
    BLOCKED = "blocked"

def analyze_bash_command(cmd: str) -> tuple[BashRiskLevel, list[str]]:
    """Analyze a shell command string and return (risk_level, reasons).

    Analysis steps:
    1. shlex.split() tokenization
    2. Detect dangerous patterns via regex on tokens:
       - Command substitution: $(...)  `...`
       - Process substitution: <(...)  >(...)
       - Pipe to shell: | bash  | sh  | zsh
       - Destructive core utils: rm -rf, dd, mkfs, fdisk, shred
       - Network exfiltration: curl/wget with pipe to shell
       - Privilege escalation: sudo, su, doas
       - Environment manipulation: export, unset, env -i
    3. Score each pattern; aggregate to risk level
    """
```

- `TIER3_PREFIXES` list is kept as a fast pre-filter but `analyze_bash_command()` is the authoritative gate
- `execute_bash` calls `analyze_bash_command()` before `Event.wait()` approval gate
- BLOCKED commands return `ToolResult(ok=False, error=AgentError(PERMISSION_DENIED, ...))`

### S7-B: Non-polling permission gate

**File:** `src/tools/_approval.py` (replace `threading.Event` with `asyncio.Event`)

Current: `gate_event.wait(timeout=300.0)` (blocks thread)
Target: `await gate_event.wait()` (releases thread while waiting)

- Both the bash gate and the file preview gate must be converted
- TUI sends approval via `await gate_event.set()` from the UI thread
- This requires the approval call path to be inside an `asyncio.run()` context — verify in `orchestrator.execute_tool()`
- `asyncio.wait_for(gate_event.wait(), timeout=300.0)` for deadline

---

## Sprint S8 — Dynamic Tool Pruning + Schema Stripping (Complete Model Scaling)

This sprint completes the model-tier routing work started in S1.

### S8-A: Schema stripping for models without JSON schema support

**File:** `src/core/context/context_builder.py`

```python
def _render_tools_for_tier(self, tools: list[Tool], tier: ModelTier) -> str:
    if tier in (ModelTier.NANO, ModelTier.SMALL):
        # YAML format, no JSON schema — just name + description + required args
        return self._render_yaml_tools_minimal(tools)
    elif supports_native_tools(self.provider_name, self.model_name):
        return self._render_json_schema_tools(tools)
    else:
        return self._render_yaml_tools_full(tools)
```

### S8-B: `simple_mode` equivalent for NANO tier

- NANO tier: automatically sets `supports_native_tools=False`, limits to 8 core tools, uses `local-small.md` prompt partial
- One-tool-per-message rule enforced in `execution_node` for NANO tier: if model returns multiple tool calls, only the first is executed; remainder queued for next turn

### S8-C: `/fast` and `/model` slash commands

**File:** `tui/src/ui/components/chat_input.py` (extend `SLASH_COMMANDS`)

```python
"/fast":  "Switch to the fastest/smallest configured model for this session",
"/model": "Switch to a different model (usage: /model ollama/qwen3:14b)",
```

- `/fast` sets `state.model_tier = ModelTier.NANO` and switches to `model_routing.nano_model` from config
- `/model <name>` hot-swaps `state.current_model`; validates against ProviderManager
- Both publish `model.switched` EventBus event so TUI status bar updates

---

## Sprint S9 — Cross-Session Memory Injection + `/compact` Command

### S9-A: Confirmed cross-session memory injection at session start

**File:** `src/core/context/context_builder.py`

```python
async def inject_prior_session_memories(self, task: str) -> str:
    """Search vector store for summaries from prior sessions relevant to task.
    Returns a formatted <prior_context> block or empty string if nothing found.
    """
    if not self.vector_store:
        return ""
    results = await self.vector_store.search_memories(query=task, k=3)
    if not results:
        return ""
    lines = ["<prior_context>", "Relevant context from previous sessions:"]
    for r in results:
        lines.append(f"- {r.text[:200]}")
    lines.append("</prior_context>")
    return "\n".join(lines)
```

- Called in `perception_node` on round 0 only; result injected into system prompt after SOUL.md
- `distill_context()` must call `vector_store.add_memory(summary, session_id)` at end of every compaction (verify this path end-to-end)

### S9-B: `/compact` user command

**TUI slash command:** `/compact` — triggers `distill_context()` immediately regardless of token threshold

- Useful for long sessions where the user knows context is getting stale
- Publishes `context.compacted` EventBus event; TUI shows "Context compacted" status message

---

## Sprint S10 — Hook Verification + `stuck` Skill + Golden File Tests

### S10-A: Verify and fix tool hook dispatch

**File:** `src/core/orchestration/tool_hooks.py`

- Read the current implementation and verify `pre_tool.sh` and `post_tool.sh` are reliably called
- Fix any dispatch gaps found
- Add integration test: `tests/integration/test_tool_hooks.py` — installs a shell hook that writes to a temp file; verifies it is called before and after a tool execution

### S10-B: `stuck` auto-recovery skill

**File:** `src/config/agent-brain/skills/stuck.md` (NEW)

```markdown
# Stuck Recovery

When you detect you are repeating the same actions without progress:
1. Stop immediately and describe the obstacle clearly.
2. Try a fundamentally different approach (different tool, different file, different search strategy).
3. If still stuck after 2 attempts, ask the user for clarification.
4. Never retry the exact same failing action more than once.
```

- `execution_node` injects `stuck` skill when `doom_loop_detected` flag is True (before returning error)
- This gives the model one more chance with explicit recovery instructions before hard-stopping

### S10-C: Golden file tests for system prompts

**File:** `tests/unit/test_system_prompts_golden.py` (NEW)

```python
import hashlib, json
from pathlib import Path
from src.core.context.context_builder import ContextBuilder

GOLDEN_DIR = Path("tests/fixtures/golden_prompts/")

@pytest.mark.parametrize("role,tier", [
    ("operational", "NANO"), ("strategic", "FRONTIER"),
    ("debugger", "SMALL"), ("analyst", "MEDIUM"),
])
def test_system_prompt_stable(role, tier, tmp_path):
    """Verify that system prompt content is stable (no accidental regressions)."""
    cb = ContextBuilder(role=role, working_dir=tmp_path)
    prompt = cb.build_system_prompt(tier=ModelTier[tier])
    sha = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    golden_file = GOLDEN_DIR / f"{role}_{tier}.sha"
    if not golden_file.exists():
        golden_file.write_text(sha)   # first run: generate baseline
        return
    assert golden_file.read_text() == sha, (
        f"System prompt for {role}/{tier} changed unexpectedly. "
        f"If intentional, delete {golden_file} and re-run to regenerate."
    )
```

### S10-D: Mock LLM adapter in existing integration tests

- Retrofit 5 key integration tests in `tests/integration/` to use `MockAdapter` from S0-C
- These tests currently require a running Ollama/LM Studio instance; after retrofit they run in CI

---

## Success Criteria

At completion of all sprints, CodingAgent should meet these benchmarks:

| Criterion | Target |
|---|---|
| 9B model (Qwen3-7B on Ollama) can complete a simple CRUD feature end-to-end | Yes (S1 + S8) |
| 9B model uses ≤8 tools in context (no token overflow) | Yes (S1-C + S8-A) |
| LSP diagnostics surfaced to LLM after every file write | Yes (S2-B) |
| MCP server (e.g., `@modelcontextprotocol/server-filesystem`) usable from `.agent/config.json` | Yes (S3) |
| Full session revert (any file, any time) | Yes (S4-A + S5-B) |
| Token count accuracy within ±5% | Yes (S0-A with tiktoken) |
| CI runs all graph integration tests without any LLM provider credentials | Yes (S0-C) |
| Session cost displayed in TUI status bar | Yes (S6-A) |
| Bash injection via `$(...)` blocked before execution | Yes (S7-A) |
| Cross-session relevant memories injected at session start | Yes (S9-A) |
| System prompt stability verified in CI | Yes (S10-C) |

---

## Dependency Map

```
S0 (token accuracy, typed errors, mock LLM)
├── S1 (model tiers, prompt templates)
│   ├── S8 (schema stripping, /fast command)
│   └── S6-B (per-provider prompts wired)
├── S2 (LSP tools)
├── S3 (MCP client)
└── S4 (git snapshots, event log)
    └── S5 (fork/revert)

S6-A (cost tracking) — standalone
S6-C (config hot-reload) — standalone
S7 (AST bash, async gate) — standalone
S9 (memory injection, /compact) — requires S0-A (token accuracy)
S10 (hooks, stuck skill, golden tests) — requires S0-C (mock LLM)
```

**Critical path:** S0 → S1 → S8 (model scaling chain, 3 weeks)
**Independent track:** S2, S3, S4+S5 (can run in parallel with the model scaling chain)
