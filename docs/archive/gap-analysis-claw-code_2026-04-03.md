# Gap Analysis: claw-code-main vs CodingAgent

**Date:** 2026-04-02
**Reference:** claw-code-main
**Target:** CodingAgent (this repo)

---

## 1. Executive Summary

CodingAgent has a significantly more sophisticated orchestration architecture (LangGraph state machine, role-based nodes, delegation, wave execution) than claw-code-main's simple imperative loop, and a richer tool registry (62 tools vs a smaller core set). However, claw-code-main excels in three areas where CodingAgent has clear gaps: **runtime prompt enrichment** (live git context, project-local instruction files), **security isolation** (bubblewrap sandboxing, pre/post tool hooks), and **developer-facing UX** (per-turn token/cost display, session persistence, slash commands, streaming output). These gaps collectively reduce CodingAgent's trustworthiness for unsupervised runs and its usability during interactive sessions. Closing P1 and P2 gaps would bring the two systems to parity on the features that matter most for production use.

---

## 2. Gap Table

| Area | Gap | claw-code approach | Priority |
|---|---|---|---|
| Prompts | No live git context in system prompt | `git status --short --branch`, `git diff --cached`, `git diff` injected per turn | P1 |
| Prompts | No project-local instruction file discovery | Walks upward from cwd for CLAW.md / .claw/instructions.md / CLAW.local.md; 4 KB per file, 12 KB total cap, deduped by content hash | P1 |
| Prompts | No dynamic boundary marker in prompts | `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` separates static role text from runtime context | P2 |
| Prompts | No LSP context (diagnostics, symbols) in prompt | Optional workspace-symbols + diagnostics block injected | P3 |
| Security | No bash sandbox / filesystem isolation | bubblewrap (`bwrap`): filesystem isolation, network isolation, PID namespace | P1 |
| Security | No pre/post tool hooks | Subprocess hooks executed before/after every tool call; hook can deny call | P2 |
| Security | No per-tool permission model | ReadOnly / WorkspaceWrite / DangerFullAccess / Prompt / Allow granularity per tool | P2 |
| Tooling | No structured patch hunks with line numbers | Edit operations surface diffs with hunk metadata and line numbers before applying | P2 |
| Tooling | Glob has no truncation reporting | Reports `truncated: true` + count when >10,000 files matched | P3 |
| Tooling | No plugin/external tool registration | External tools registered and validated against builtins at startup | P3 |
| Tooling | No tool aliases | `read` → `read_file`, `write` → `write_file`, etc. normalised transparently | P4 |
| Orchestration | No session persistence | Full session (messages, state, tool history) saved/loaded as versioned JSON | P1 |
| Orchestration | No USD cost tracking | Per-model pricing table; cost estimated and displayed per turn | P2 |
| Orchestration | No explicit iteration (turn) limit | Configurable max iterations enforced; doom-loop is tool-call-count only | P2 |
| Orchestration | No per-token breakdown per turn logged to TUI | Token usage tracked per turn with per-model breakdown | P2 |
| TUI | No streaming text display | Streaming token deltas rendered incrementally in terminal | P1 |
| TUI | No per-turn usage summary in TUI | Input/output tokens + USD cost shown after each turn | P2 |
| TUI | No /status or /compact slash commands | /help, /status (turns, compaction, model, usage), /compact (manual trigger) | P2 |
| TUI | No multiline REPL input (Shift+Enter) | Shift+Enter / Ctrl+J inserts newline in REPL | P3 |
| TUI | No output format selection | Pretty / Json / Raw selectable at runtime | P4 |
| Config | No hierarchical project-level config | default → system → user → workspace (.claw/config.json) → local (.claw/config.local.json) | P2 |
| Config | No per-workspace config file | `.claw/config.json` in repo root controls workspace-level overrides | P2 |

---

## 3. Detailed Findings

---

### 3.1 [P1] No live git context in system prompt

**What claw-code does**
Before each API call the orchestrator runs three git commands and appends their output to the system prompt:
- `git status --short --branch` (current branch + dirty files)
- `git diff --cached` (staged changes)
- `git diff` (unstaged changes)

This gives the model an accurate, current picture of repository state without relying on explicit tool calls.

**What CodingAgent does**
`perception_node.py` collects a `repo_summary_data` snapshot, but git state is not injected into the *system prompt* — it is placed in the conversation history as a user-turn message when needed. The `git_tools.py` toolset exists but the model must decide to call it. Role files (`src/config/agent-brain/roles/operational.md`, `strategic.md`) are static and contain no runtime placeholders.

**Implementation suggestion**
Add a `_build_git_context_block() -> str` helper to `src/core/orchestration/agent_brain.py` that runs the three git commands via `subprocess.run` with a timeout and returns a fenced block. Call it from the `build_system_prompt()` method (or equivalent) already invoked in `src/core/orchestration/orchestrator.py` before each LLM call, and append the result after the static role text. Limit the git diff output to ~2,000 chars total to avoid flooding the context window. Guard with `try/except` so non-git directories are silent.

```
src/core/orchestration/agent_brain.py       — add _build_git_context_block()
src/core/orchestration/orchestrator.py      — call it when assembling messages for call_model()
```

---

### 3.2 [P1] No project-local instruction file discovery

**What claw-code does**
At session start, claw-code walks from the working directory upward to the filesystem root looking for any of: `CLAW.md`, `.claw/instructions.md`, `CLAW.local.md`. Each found file is read, content-hashed for deduplication, and included in the system prompt up to 4,000 chars per file and 12,000 chars total.

**What CodingAgent does**
There is no equivalent. The only project customisation is the static role files in `src/config/agent-brain/roles/`. Users cannot drop a file in their repo to adjust agent behaviour for that project.

**Implementation suggestion**
Add `src/core/orchestration/instruction_loader.py` with a `load_project_instructions(cwd: Path) -> str` function. The function walks upward collecting `AGENT.md`, `.agent/instructions.md`, `AGENT.local.md` (choosing names that do not conflict with claw-code installations). Apply a 4,000-char budget per file and a 12,000-char combined cap. Deduplicate by `hashlib.sha256`. Call the function from `agent_brain.py` or `orchestrator.py::start_new_task()` and prepend the result to the assembled system prompt with a clear `--- project instructions ---` header.

```
src/core/orchestration/instruction_loader.py    — NEW: file discovery + budget logic
src/core/orchestration/agent_brain.py           — call load_project_instructions() at session init
src/core/orchestration/orchestrator.py          — pass loaded instructions into start_new_task() state
```

---

### 3.3 [P2] No dynamic boundary marker in prompts

**What claw-code does**
A sentinel string `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` is embedded in the static role template. At runtime the text before the sentinel is the immutable static section; everything after it is replaced with current runtime context (git state, project instructions, tool results). This makes it easy to audit exactly what the model sees as "trusted static instructions" vs "dynamic context".

**What CodingAgent does**
Role files in `src/config/agent-brain/roles/` are concatenated as-is with no marker. There is no programmatic boundary between the fixed role definition and the per-turn runtime context appended by `ContextBuilder` (`src/core/context/`).

**Implementation suggestion**
Add the sentinel as a constant in `src/core/orchestration/agent_brain.py`:
```python
DYNAMIC_BOUNDARY = "<!-- DYNAMIC_CONTEXT_START -->"
```
Append `DYNAMIC_BOUNDARY` at the end of every static role file (one-line edit to each `.md` in `src/config/agent-brain/roles/`). In the method that assembles the final system prompt, split on the sentinel and replace everything after it with the current runtime block. This is a low-cost audit and debugging aid.

```
src/config/agent-brain/roles/operational.md    — append sentinel line
src/config/agent-brain/roles/strategic.md      — append sentinel line
src/config/agent-brain/roles/analyst.md        — append sentinel line
src/config/agent-brain/roles/debugger.md       — append sentinel line
src/config/agent-brain/roles/reviewer.md       — append sentinel line
src/core/orchestration/agent_brain.py          — add DYNAMIC_BOUNDARY constant + split logic
```

---

### 3.4 [P3] No LSP context injection

**What claw-code does**
Optionally injects workspace symbol list and LSP diagnostics (warnings/errors) directly into the system prompt, giving the model a pre-computed semantic view of the codebase without requiring AST tool calls.

**What CodingAgent does**
`src/core/indexing/` contains a `SymbolGraph` which builds a call graph and symbol index, and `src/tools/ast_tools.py` exposes symbol lookup. However this information is not proactively injected into the prompt — the model must call the tools to retrieve it.

**Implementation suggestion**
Add a `get_lsp_context_block(workdir: Path, budget_chars: int = 2000) -> str` helper in `src/core/indexing/` that queries the existing `SymbolGraph` for top-level symbols and any cached diagnostics, formats them as a brief fenced block, and returns it. Wire it into `agent_brain.py` as an optional enrichment controlled by a feature flag in `src/config/providers.json` or `prefs.json`. This is optional (P3) because the tool-call path already works.

```
src/core/indexing/lsp_context.py             — NEW: get_lsp_context_block()
src/core/orchestration/agent_brain.py        — optional injection gated by feature flag
```

---

### 3.5 [P1] No bash sandbox / filesystem isolation

**What claw-code does**
Bash commands are run inside a `bubblewrap` (`bwrap`) container: read-only bind-mount of system dirs, writable mount only for the workspace, network disabled by default, separate PID namespace. Strictness is configurable (off / workspace-only / full-isolation).

**What CodingAgent does**
`src/tools/guardrails.py` and `src/tools/_security.py` implement a pattern-matching denylist (`DANGEROUS_PATTERNS`, `SAFE_COMMANDS`). There is no OS-level isolation. A bash command that passes the regex filter runs with the full process privileges of the agent.

**Implementation suggestion**
Add a `src/tools/sandbox.py` module with a `run_sandboxed(cmd: str, cwd: Path, timeout: float, network: bool = False) -> subprocess.CompletedProcess` function. Detect `bwrap` availability at startup via `shutil.which("bwrap")`; if available, prepend the bubblewrap invocation:
```
bwrap --ro-bind / / --bind <cwd> <cwd> --dev /dev --proc /proc --unshare-pid --unshare-net -- bash -c <cmd>
```
If `bwrap` is not available, fall back to the current `subprocess.run` path with a warning logged. Wire `run_sandboxed` into `src/tools/system_tools.py` (the `bash` tool implementation) replacing the current `subprocess.run` call. Add a `sandbox_level` key to `providers.json` schema: `"off"` / `"workspace"` / `"full"`.

```
src/tools/sandbox.py                         — NEW: run_sandboxed()
src/tools/system_tools.py                    — replace subprocess.run with run_sandboxed()
src/config/providers.json                    — add "sandbox_level" field
src/config/schema.json                       — update schema
```

---

### 3.6 [P2] No pre/post tool hooks

**What claw-code does**
Before and after every tool call, claw-code invokes a configurable list of subprocess hooks. A pre-hook receives the tool name and arguments and can return `deny` to block execution. A post-hook receives the result. This enables audit logging, policy enforcement, and external approval workflows without modifying core code.

**What CodingAgent does**
The `EventBus` (`src/core/orchestration/event_bus.py`) publishes `tool.call.start` / `tool.call.finish` events, but these are fire-and-forget notifications. No hook can block a tool call in flight. Permission gating exists for destructive tools (`src/core/orchestration/plan_mode.py`) but is not a general hook mechanism.

**Implementation suggestion**
Add a `ToolHookManager` class in `src/core/orchestration/tool_hook_manager.py` with:
- `register_pre_hook(fn: Callable[[str, dict], bool | None])` — returning `False` denies the call
- `register_post_hook(fn: Callable[[str, dict, Any], None])`
- `run_pre_hooks(tool_name, args) -> bool` — returns `True` if all hooks allow
- `run_post_hooks(tool_name, args, result) -> None`

Wire `run_pre_hooks` into `src/core/orchestration/orchestrator.py::execute_tool()` immediately before tool dispatch. Wire `run_post_hooks` after the result is obtained. Subprocess hooks (external scripts) can be registered at startup by reading a `hooks:` list from a new `.agent/hooks.json` config file.

```
src/core/orchestration/tool_hook_manager.py  — NEW: ToolHookManager
src/core/orchestration/orchestrator.py       — call run_pre_hooks / run_post_hooks in execute_tool()
```

---

### 3.7 [P2] No per-tool permission model (granular)

**What claw-code does**
Each tool has a permission level: `ReadOnly`, `WorkspaceWrite`, `DangerFullAccess`, `Prompt` (ask user), or `Allow` (always allow). The permission can be overridden per-tool in config. This is more expressive than a binary allow/block.

**What CodingAgent does**
There is a binary permission gate for destructive tools via `plan_mode.py` and the `is_modifying` flag in `src/tools/tools_config.py`. There is no per-tool granularity for read-only vs. workspace-scoped write vs. dangerous.

**Implementation suggestion**
Extend `src/tools/tools_config.py` to add a `PermissionLevel` enum (`READ_ONLY`, `WORKSPACE_WRITE`, `DANGER`, `PROMPT`, `ALLOW`) and a `TOOL_PERMISSIONS: dict[str, PermissionLevel]` mapping. Consult this mapping in `src/core/orchestration/orchestrator.py::execute_tool()` to decide whether to auto-allow, prompt via the TUI modal (`src/ui/components/`), or block. Persist per-session overrides in `prefs.json`.

```
src/tools/tools_config.py                    — add PermissionLevel enum + TOOL_PERMISSIONS dict
src/core/orchestration/orchestrator.py       — consult TOOL_PERMISSIONS in execute_tool()
src/core/user_prefs.py                       — load/save per-tool permission overrides
```

---

### 3.8 [P2] Structured patch hunks with line numbers

**What claw-code does**
Edit operations present the proposed diff with hunk headers (`@@ -L,N +L,N @@`) and line numbers before writing. The model sees and generates structured hunks rather than a replacement blob.

**What CodingAgent does**
`src/tools/file_tools.py` has `edit_file_atomic` and `edit_by_line_range`. The `file.diff.preview` event (added in Vol4) fires a diff to the EventBus before writing, but the model's *input* format for edits is still full `old_string` / `new_string` pairs (or line ranges). There is no structured hunk format in either the tool's input schema or its confirmation display.

**Implementation suggestion**
Add a `generate_unified_diff(path, old_content, new_content, context_lines=3) -> str` utility in `src/tools/patch_tools.py` (the file already exists). Call it in `edit_file_atomic` and `edit_by_line_range` both (a) as part of the `file.diff.preview` event payload and (b) returned in the tool result so the model's next turn sees the actual patch applied. Optionally add a new `apply_patch` tool that accepts a unified diff string as input, which more closely matches the claw-code input format.

```
src/tools/patch_tools.py                     — add generate_unified_diff() + apply_patch tool
src/tools/file_tools.py                      — call generate_unified_diff() in edit_file_atomic/edit_by_line_range
```

---

### 3.9 [P3] Glob has no truncation reporting

**What claw-code does**
Glob caps results at 10,000 files and includes a `truncated: true` flag in the response when the cap is hit, along with the total match count. The model can adjust its pattern.

**What CodingAgent does**
`src/tools/repo_tools.py` implements the `glob` tool. It applies safe_resolve filtering and returns results as a list but does not report whether results were truncated or what the total un-truncated count is.

**Implementation suggestion**
In `src/tools/repo_tools.py`, after collecting all matches, check `len(results) > MAX_GLOB_RESULTS` (add `MAX_GLOB_RESULTS = 10_000` constant). If truncated, return a dict `{"files": results[:MAX_GLOB_RESULTS], "truncated": True, "total_found": len(results)}` instead of a plain list.

```
src/tools/repo_tools.py                      — add truncation cap + truncated/total_found fields
```

---

### 3.10 [P3] No plugin/external tool registration

**What claw-code does**
At startup, claw-code reads a `plugins:` list from config. Each entry is an external executable or Python module. Tools are validated against a schema (name, description, input_schema) and registered alongside builtins. Conflicts with builtin names are rejected.

**What CodingAgent does**
`src/tools/registry.py` and `src/tools/_registry.py` provide a `ToolRegistry`. Tools are registered at import time using decorators. There is no runtime loading from external paths.

**Implementation suggestion**
Add `load_plugin_tools(plugin_paths: list[str]) -> list[Tool]` in `src/tools/registry.py`. Each plugin path points to a Python module exposing a `TOOLS: list[Tool]` attribute. Validate each tool has a unique name not in the builtin registry. Load via `importlib.import_module` with a try/except. Call from `src/core/orchestration/orchestrator.py::example_registry()` after reading a `plugin_tools` key from `providers.json`.

```
src/tools/registry.py                        — add load_plugin_tools()
src/core/orchestration/orchestrator.py       — call load_plugin_tools() in example_registry()
src/config/providers.json                    — add optional "plugin_tools" array field
```

---

### 3.11 [P4] No tool aliases

**What claw-code does**
Common short names (`read`, `write`, `edit`) are transparently mapped to canonical tool names (`read_file`, `write_file`, `edit_file`). This prevents model errors when the LLM uses a shorter name.

**What CodingAgent does**
Tool names in the registry are exact-match. If the LLM emits `read` instead of `read_file`, the call fails.

**Implementation suggestion**
Add an `ALIASES: dict[str, str]` mapping in `src/tools/tools_config.py`. In `src/core/orchestration/orchestrator.py::execute_tool()`, normalise the tool name via `ALIASES.get(name, name)` before lookup. This is a one-time two-line change after defining the aliases dict.

```
src/tools/tools_config.py                    — add ALIASES dict
src/core/orchestration/orchestrator.py       — normalise name in execute_tool()
```

---

### 3.12 [P1] No session persistence

**What claw-code does**
At end of session the full message history, tool call log, token usage, and model config are serialised to a versioned JSON file. On next launch the user can resume or inspect past sessions.

**What CodingAgent does**
`src/core/orchestration/orchestrator.py::start_new_task()` resets all state. `AgentSessionManager` (`src/core/orchestration/agent_session_manager.py`) tracks in-memory sessions but there is no serialisation to disk across process restarts. The TUI history (input history) is persisted via `TextualAppBase._save_history()` but agent state (messages, plan, tool results) is not.

**Implementation suggestion**
Add `src/core/orchestration/session_store.py` with `save_session(session_id, state: AgentState, messages: list) -> Path` (writes `~/.agent/sessions/<session_id>.json`) and `load_session(session_id) -> tuple[AgentState, list]`. Call `save_session` in `orchestrator.py::run_agent_once()` at task completion (alongside the existing `flush_execution_trace()` call). Add a `--resume <session_id>` CLI flag in `src/main.py` and a `/sessions` slash command in the TUI. Use `json` with a custom encoder for `Path` and `datetime` objects.

```
src/core/orchestration/session_store.py      — NEW: save_session / load_session
src/core/orchestration/orchestrator.py       — call save_session() after task completion
src/main.py                                  — add --resume CLI flag
src/ui/textual_app_impl.py                   — add /sessions TUI command
```

---

### 3.13 [P2] No USD cost tracking

**What claw-code does**
A pricing table maps model IDs to (input_price_per_1k, output_price_per_1k) in USD. After each API response the cost is computed from token counts and accumulated. The TUI shows the running total.

**What CodingAgent does**
`src/core/inference/telemetry.py` and `src/core/orchestration/token_budget.py` track token counts. There is no USD conversion. The TUI does not show cost.

**Implementation suggestion**
Add `PRICING: dict[str, tuple[float, float]]` (input, output per 1K tokens) in `src/core/inference/provider_context.py`. After each `call_model()` invocation in `src/core/orchestration/orchestrator.py`, compute `cost = (input_tokens/1000 * price_in) + (output_tokens/1000 * price_out)` and publish a `usage.cost` EventBus event. Accumulate in `orchestrator._session_cost: float`. Display in the TUI's log panel via a subscriber in `src/ui/textual_app_impl.py`.

```
src/core/inference/provider_context.py       — add PRICING table
src/core/orchestration/orchestrator.py       — compute + publish usage.cost after call_model()
src/ui/textual_app_impl.py                   — subscribe to usage.cost and update status bar
```

---

### 3.14 [P2] No explicit iteration (turn) limit

**What claw-code does**
A `max_iterations` config value caps the number of agent turns regardless of tool call count. When reached the agent stops and reports the limit hit.

**What CodingAgent does**
The doom-loop detection (3 consecutive identical fingerprints) and the 30-tool-call cap (`tool_call_count` in `AgentState`, enforced in `src/core/orchestration/graph/builder.py`) provide bounded execution, but there is no independent *turn* counter. A long task with many short tool calls could exhaust the tool budget before making meaningful progress.

**Implementation suggestion**
Add `turn_count: int` and `max_turns: int` to `AgentState` (`src/core/orchestration/orchestrator.py::initial_state()`). Increment `turn_count` in `src/core/orchestration/graph/nodes/perception_node.py` at the start of each perception pass. Add a guard in `src/core/orchestration/graph/builder.py`'s routing function (after perception) that routes to `END` if `turn_count >= max_turns`, publishing a `task.turn_limit` EventBus event. Default `max_turns` to 50; make it configurable via `providers.json`.

```
src/core/orchestration/orchestrator.py       — add turn_count / max_turns to initial_state()
src/core/orchestration/graph/nodes/perception_node.py — increment turn_count
src/core/orchestration/graph/builder.py      — add max_turns guard in routing
src/config/providers.json                    — add "max_turns" field
```

---

### 3.15 [P2] No per-turn token breakdown in TUI

**What claw-code does**
After each LLM response the TUI shows: input tokens, output tokens, cached tokens (if applicable), and USD cost for that turn, plus running session totals.

**What CodingAgent does**
`src/core/inference/telemetry.py` accumulates token counts and writes them to a rotating log file. The TUI log panel shows text events but no structured token or cost readout per turn.

**Implementation suggestion**
In `src/core/orchestration/orchestrator.py`, after each `call_model()` call, publish a `usage.turn_summary` event with `{"input_tokens": N, "output_tokens": N, "model": "...", "cost_usd": X}`. In `src/ui/textual_app_impl.py`, subscribe to `usage.turn_summary` and update a footer widget or log entry in the existing `LogPanel` (`src/ui/components/`). The `EventBus` subscription pattern already used for other events makes this a small wiring change.

```
src/core/orchestration/orchestrator.py       — publish usage.turn_summary after call_model()
src/ui/textual_app_impl.py                   — subscribe + display in LogPanel/footer
src/ui/components/                           — optionally add a StatusBar widget
```

---

### 3.16 [P1] No streaming text display in TUI

**What claw-code does**
Token deltas from the streaming API response are displayed incrementally in the terminal as they arrive, giving immediate visual feedback that the model is working.

**What CodingAgent does**
The LM Studio / OpenRouter adapters in `src/core/inference/adapters/` support SSE streaming (added in Vol4) and the `openai_compat_adapter.py` can yield token chunks. However the TUI shows only the final assembled response — there is no live token-by-token rendering. The `src/ui/textual_app_impl.py` response display waits for the full string.

**Implementation suggestion**
Publish a `response.token_delta` EventBus event for each streamed chunk in `src/core/inference/adapters/openai_compat_adapter.py::generate()` (the streaming path already exists — add `event_bus.publish("response.token_delta", {"delta": chunk_text})` inside the chunk loop). In `src/ui/textual_app_impl.py`, subscribe to `response.token_delta` and append text to a `RichLog` or `TextArea` widget in real time. On `response.complete`, finalise the display.

```
src/core/inference/adapters/openai_compat_adapter.py — publish response.token_delta in streaming loop
src/ui/textual_app_impl.py                           — subscribe + render streaming text widget
src/ui/components/                                   — optionally add StreamingTextPanel
```

---

### 3.17 [P2] No /status or /compact slash commands in TUI

**What claw-code does**
- `/status` — shows current turn count, compaction status, active model, session token usage
- `/compact` — manually triggers message compaction
- `/help` — lists all slash commands

**What CodingAgent does**
The Textual TUI has no slash command parser in the input box. The plan review modal and permission modal exist as separate UI components but are triggered programmatically, not by user commands. Manual compaction is not exposed.

**Implementation suggestion**
In `src/ui/textual_app_impl.py`, intercept input strings beginning with `/` in the message submit handler. Dispatch to a `handle_slash_command(cmd: str)` method. Implement:
- `/help` — posts a formatted help string to LogPanel
- `/status` — reads `orchestrator.agent_state` fields (`turn_count`, `tool_call_count`, model name, session tokens) and posts a summary
- `/compact` — calls `distiller.distill_context()` directly and posts confirmation

The existing `EventBus` and direct orchestrator reference in the TUI make this straightforward.

```
src/ui/textual_app_impl.py                   — add handle_slash_command() + input interception
src/core/orchestration/orchestrator.py       — expose get_session_status() helper method
```

---

### 3.18 [P3] No multiline REPL input (Shift+Enter)

**What claw-code does**
Shift+Enter or Ctrl+J inserts a literal newline in the input field, allowing multi-paragraph prompts before submission.

**What CodingAgent does**
Textual's `Input` widget (used in `src/ui/textual_app_impl.py`) does not support multiline by default. The TUI likely uses a single-line `Input` for the task prompt.

**Implementation suggestion**
Replace the single-line `Input` widget with Textual's `TextArea` widget (available in Textual ≥0.47) in `src/ui/textual_app_impl.py`. Bind `shift+enter` to insert `\n` and `enter` alone to submit. This is a widget swap with a key binding update.

```
src/ui/textual_app_impl.py                   — replace Input with TextArea, add key bindings
src/ui/styles/                               — adjust CSS for TextArea sizing
```

---

### 3.19 [P4] No output format selection (Pretty/Json/Raw)

**What claw-code does**
The user can select Pretty (rich-formatted), Json (structured JSON of the full turn), or Raw (plain text) output at runtime via a flag or slash command.

**What CodingAgent does**
Output format is fixed (Textual rich panel rendering). There is no machine-readable JSON output mode useful for scripting or piping.

**Implementation suggestion**
Add a `--output-format {pretty,json,raw}` CLI flag to `src/main.py`. In `json` mode, bypass the Textual TUI entirely and instead print each turn as a JSON object to stdout: `{"turn": N, "response": "...", "tools_called": [...], "tokens": {...}}`. In `raw` mode, print only the assistant text. The existing `src/core/orchestration/orchestrator.py` already has all required data.

```
src/main.py                                  — add --output-format flag
src/core/orchestration/orchestrator.py       — add json/raw output path alongside TUI path
```

---

### 3.20 [P2] No hierarchical / per-workspace config

**What claw-code does**
Config is merged in order: hardcoded defaults → system-wide (`/etc/claw/config.json`) → user (`~/.claw/config.json`) → workspace (`./.claw/config.json`) → local (`./.claw/config.local.json`, git-ignored). Later layers override earlier ones. This lets teams commit a shared `.claw/config.json` while individuals keep personal overrides locally.

**What CodingAgent does**
`providers.json` (in `src/config/`) is a single file bundled with the repo. `prefs.json` is a user-level file. There is no workspace-level config that can be committed per-project.

**Implementation suggestion**
Add a `src/core/config_loader.py` module with a `load_merged_config() -> dict` function. The merge order:
1. `src/config/providers.json` (defaults, bundled)
2. `~/.config/codingagent/config.json` (user)
3. `<cwd>/.agent/config.json` (workspace, committable)
4. `<cwd>/.agent/config.local.json` (workspace-local, add to .gitignore)

Deep-merge dicts; last layer wins for scalar values. Replace direct `providers.json` reads in `src/core/orchestration/orchestrator.py` and `src/core/inference/adapters/` with a call to `load_merged_config()`.

```
src/core/config_loader.py                    — NEW: load_merged_config() with deep-merge
src/core/orchestration/orchestrator.py       — replace providers.json reads with load_merged_config()
src/core/inference/adapters/openai_compat_adapter.py — use load_merged_config()
```

---

## 4. Implementation Roadmap

Ordered by priority (P1 first), then by effort within priority tier.

| # | Gap | Effort | Priority | Key Files |
|---|---|---|---|---|
| 1 | Live git context in system prompt | S | P1 | `agent_brain.py`, `orchestrator.py` |
| 2 | Session persistence (save/load) | M | P1 | `session_store.py` (NEW), `orchestrator.py`, `main.py` |
| 3 | Project-local instruction file discovery | M | P1 | `instruction_loader.py` (NEW), `agent_brain.py` |
| 4 | Bash sandbox via bubblewrap | M | P1 | `sandbox.py` (NEW), `system_tools.py` |
| 5 | Streaming text display in TUI | M | P1 | `openai_compat_adapter.py`, `textual_app_impl.py` |
| 6 | Dynamic boundary marker in prompts | S | P2 | role `.md` files, `agent_brain.py` |
| 7 | Per-turn token + cost display in TUI | S | P2 | `orchestrator.py`, `textual_app_impl.py` |
| 8 | USD cost tracking | S | P2 | `provider_context.py`, `orchestrator.py` |
| 9 | Pre/post tool hooks | M | P2 | `tool_hook_manager.py` (NEW), `orchestrator.py` |
| 10 | Per-tool permission model (granular) | M | P2 | `tools_config.py`, `orchestrator.py`, `user_prefs.py` |
| 11 | /status and /compact slash commands | S | P2 | `textual_app_impl.py`, `orchestrator.py` |
| 12 | Explicit iteration (turn) limit | S | P2 | `orchestrator.py`, `graph/builder.py`, `perception_node.py` |
| 13 | Hierarchical / per-workspace config | M | P2 | `config_loader.py` (NEW), `orchestrator.py`, adapters |
| 14 | Structured patch hunks in edit tools | M | P2 | `patch_tools.py`, `file_tools.py` |
| 15 | Multiline REPL input (Shift+Enter) | S | P3 | `textual_app_impl.py`, `styles/` |
| 16 | Glob truncation reporting | S | P3 | `repo_tools.py` |
| 17 | Plugin/external tool registration | L | P3 | `registry.py`, `orchestrator.py`, `providers.json` |
| 18 | LSP context injection | L | P3 | `lsp_context.py` (NEW), `agent_brain.py` |
| 19 | Tool aliases | S | P4 | `tools_config.py`, `orchestrator.py` |
| 20 | Output format selection (json/raw) | S | P4 | `main.py`, `orchestrator.py` |

**Effort key:** S = small (<1 hr), M = medium (2–4 hr), L = large (>4 hr)

---

*End of gap analysis.*
