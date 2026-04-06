# Orchestration & Agent Architecture Gap Analysis

**Date:** 2026-04-04
**Scope:** CodingAgent vs OpenCode (TypeScript/Bun) and claw-code-main (Rust)
**Topics:** Orchestration loop, agent definitions, subagent spawning, tools architecture, system prompts, multi-tiered orchestration

---

## 0. Executive Summary

CodingAgent has a functional LangGraph-based orchestration loop and a rich tool set, but is missing several architectural patterns that make OpenCode and claw-code-main more robust and capable. The most impactful gaps are:

1. **No named, typed agent definitions** — CodingAgent uses role YAML configs but has no programmatic agent type system with per-agent tool allowlists, prompt overrides, or mode differentiation
2. **No recursive subagent spawning** — `delegate_task` shells out but does not recursively enter the orchestration loop; there is no parent/child session model
3. **No multi-tier system prompt construction** — a single monolithic prompt is built per turn; there is no provider-adaptive, cache-aware, layered prompt assembly
4. **No doom-loop detection** — the agent can repeat identical tool calls indefinitely
5. **No plan↔build mode switching** — plan mode exists as an orchestrator flag but is not implemented as a true agent-type transition with prompt switching
6. **No per-agent tool permission matrix** — tools are controlled by YAML toolsets per role, not by a runtime permission evaluator with wildcard rules and interactive approval

---

## 1. Orchestration Loop

### OpenCode Pattern

**File:** `packages/opencode/src/session/prompt.ts` → `runLoop()`

OpenCode's loop is a bare `while (true)` that re-fetches full message history from SQLite each iteration and checks for pending subtasks, compaction triggers, and context overflow before dispatching to the LLM. It is stateless between iterations — each loop body is a fresh read of the world. The loop breaks on non-tool-call finish reasons.

Key structural properties:
- `Runner` state machine manages busy/idle/shell states — rejects new prompts when already running
- Abort/cancellation interrupts the fiber via `Runner.cancel()`
- Context overflow is checked before each LLM call; compaction is triggered inline
- Max steps enforcement injects a `max-steps.txt` reminder and disables tools
- Doom-loop detection: if the last 3 consecutive tool calls are identical (same name + same JSON input), `permission.ask("doom_loop")` surfaces an interactive approval prompt
- Retry policy: exponential backoff with provider `retry-after` header support; sets session status to `{ type: "retry", attempt, message, next }` during wait

### Claw Code Pattern

**File:** `rust/crates/runtime/src/conversation.rs` → `ConversationRuntime::run_turn()`

Claw Code's loop is also a bare `loop {}` — simpler than OpenCode. Per iteration: push user message (first iteration only), call `api_client.stream()`, build `AssistantMessage` from events, run pre-hook → permission check → tool execute → post-hook, push tool result, continue. Breaks when no tool uses are returned. Max iterations defaults to `usize::MAX`.

### CodingAgent Current State

**File:** `src/core/orchestration/orchestrator.py`, LangGraph pipeline nodes

CodingAgent uses LangGraph's directed graph with named nodes. The flow is:
`input_node → planner_node → execution_node → verification_node → output_node`

The "loop" is the LangGraph `while` conditional edge on `execution_node` — it re-enters `execution_node` while `AgentState.continue_execution` is True. This is functionally equivalent to a `while` loop but graph-structured.

### Gaps vs Reference Implementations

| Feature | OpenCode | Claw Code | CodingAgent | Gap |
|---|---|---|---|---|
| Stateless loop (full history reload) | Yes — SQLite reload per iteration | Yes — session object mutated in place | Partial — LangGraph state passed through; no full reload | Low risk; LangGraph state is equivalent |
| Doom-loop detection | Yes — last 3 identical tool calls trigger approval | No | No (`_COOLDOWN_GAP` inline, not doom-loop detection) | **MISSING** |
| Max steps enforcement | Yes — disables tools, injects reminder | Yes — `max_iterations = usize::MAX` (unlimited) | Yes — `max_turns` pre-graph check | Partial — no mid-run tool disabling |
| Abort / cancellation | Fiber cancellation via `Runner.cancel()` | Not implemented in Rust | Keyboard interrupt via Escape | Partial |
| Retry with backoff | Exponential + header-aware + session status broadcast | Not implemented | `@async_retry` decorator | Partial — no session status broadcast |
| Busy/idle state broadcast | `session.status` bus event on every transition | No | `agent.status` EventBus events | Equivalent |
| Context overflow check | Per-iteration token count vs model limit | No | Token-based compaction trigger | Equivalent |

**Action items:**
- **ORCH-01 (P1):** Implement doom-loop detection: track last N tool call (name, input_hash) tuples in `AgentState`; if last 3 are identical, publish `tool.doom_loop_detected` event and pause for user approval
- **ORCH-02 (P2):** Extract `_COOLDOWN_GAP` and loop guard logic into `src/core/orchestration/loop_guards.py` as a proper module
- **ORCH-03 (P2):** Add mid-run max steps enforcement: when step count reaches `max_turns - 2`, inject a warning into the next system message block and stop offering write tools

---

## 2. Agent Definitions & Typing

### OpenCode Pattern

**File:** `packages/opencode/src/agent/agent.ts`

OpenCode defines agents as typed records with explicit fields:

```typescript
{
  id: string,
  name: string,
  description: string,
  prompt?: string,          // completely replaces provider prompt if set
  mode: "primary" | "subagent" | "all",
  hidden?: boolean,
  model?: { providerID, modelID },
  temperature?: number,
  permissions?: Permission.Ruleset,  // per-agent tool allow/deny rules
}
```

Built-in agents: `build` (default, full access), `plan` (read-only, plan mode), `general` (subagent, multi-step research, no `todowrite`), `explore` (subagent, read-only exploration, custom prompt), `compaction` (internal, no tools), `title` (internal, temperature=0.5), `summary` (internal).

Custom agents can be loaded from `cfg.agent` entries. `Agent.generate()` uses the LLM to synthesise a new agent definition (name, whenToUse, systemPrompt) from a user description.

### Claw Code Pattern

**File:** `rust/crates/tools/src/lib.rs` → `build_agent_system_prompt()` (line 1642), `SubagentToolExecutor`

Claw Code implements subagent types as string labels matched in `build_agent_runtime()`. Each type maps to:
- A filtered tool allowlist (`SubagentToolExecutor` holds `allowed_tools: BTreeSet<String>`)
- A system prompt prefix: `"You are a background sub-agent of type '{subagent_type}'. Work only on the delegated task..."`

Built-in types: `Explore` (read-only: read_file, glob, grep, bash, WebFetch), `Plan` (TodoWrite + StructuredOutput), `Verification` (read + bash), `claw-guide` (read + Skill), `statusline-setup` (read + write + bash), default (all tools).

### CodingAgent Current State

**Files:** `src/config/roles/`, `src/core/orchestration/roles.py` (assumed), `src/tools/toolsets/*.yaml`

CodingAgent uses YAML-based role definitions (`analyst`, `coder`, `reviewer`, `planner`) that map to toolset YAML files. Roles are strings used to look up toolset configurations. There is no programmatic agent type with:
- Per-agent prompt override (distinct from role system prompt)
- `mode` (primary vs subagent)
- Per-agent permission ruleset
- `temperature` override

### Gaps vs Reference Implementations

| Feature | OpenCode | Claw Code | CodingAgent | Gap |
|---|---|---|---|---|
| Typed agent definitions | Full typed record with all fields | String label + tool allowlist | YAML toolset config only | **MISSING typed agent objects** |
| Per-agent prompt override | Yes — `agent.prompt` replaces provider prompt | Yes — prefix added in `build_agent_system_prompt()` | No — roles use same system prompt builder | **MISSING** |
| Per-agent temperature | Yes | Yes (via model config) | No | Missing |
| Per-agent permission ruleset | Yes — wildcard allow/deny rules | Yes — allowlist per type | YAML toolset (static) | **PARTIALLY MISSING** |
| Agent mode (primary/subagent) | Yes — controls availability as delegation target | Implicit via type | No | Missing |
| Dynamic agent generation | `Agent.generate()` from natural language | No | No | Missing |
| Built-in specialised agents | build, plan, general, explore, compaction, title, summary | Explore, Plan, Verification, claw-guide | analyst, coder, reviewer, planner (YAML) | Partial |

**Action items:**
- **AGENT-01 (P1):** Define a `AgentDefinition` dataclass in `src/core/orchestration/agent_types.py`:
  ```python
  @dataclass
  class AgentDefinition:
      id: str
      name: str
      description: str
      mode: Literal["primary", "subagent"]
      prompt_override: str | None = None
      toolset: str | None = None
      allowed_tools: set[str] | None = None
      temperature: float | None = None
  ```
- **AGENT-02 (P1):** Create built-in agent definitions mirroring OpenCode: `build` (full access), `explore` (read-only with custom prompt), `plan` (write-restricted), `verification` (test/lint only)
- **AGENT-03 (P2):** Wire `AgentDefinition.prompt_override` into the system prompt builder so it replaces the base prompt section for subagent calls
- **AGENT-04 (P3):** Implement `Agent.generate()` — use LLM to synthesise a new `AgentDefinition` from a natural-language description; persist to user config

---

## 3. Subagent Spawning

### OpenCode Pattern

**File:** `packages/opencode/src/tool/task.ts`

The `task` tool creates a child session via `Session.create({ parentID: ctx.sessionID })` and calls `SessionPrompt.prompt()` recursively on the child session ID. This fully re-enters the `runLoop` in a child context. The child session:
- Has its own message history in SQLite
- Auto-denies `todowrite` and `task` tools unless the agent's permissions allow them (prevents unbounded recursion by default)
- Returns the last `TextPart` from its message history as the tool output
- Includes `task_id` in the output so the parent can resume the same child session later

Permission check: `ctx.ask({ permission: "task" })` before spawning. Doom-loop guard applies.

`@agent-name` mention syntax in user messages creates a `SubtaskPart` which is detected in `runLoop` and calls `handleSubtask()` with `bypassAgentCheck: true` — direct spawn without explicit tool call.

### Claw Code Pattern

**File:** `rust/crates/tools/src/lib.rs` → `execute_agent()` (line 1505)

The `Agent` tool spawns a real OS thread: `std::thread::Builder::new().spawn(move || { run_agent_job() })`. The spawned thread creates a full nested `ConversationRuntime` with:
- A `SubagentToolExecutor` that enforces the type's allowlist
- A system prompt prefix prepended to the same base prompt
- Agent output written to `{agent_store_dir}/{agent_id}.md` and `.json`

Default subagent model: `claude-opus-4-6`. Default max iterations: 32.

Claw Code spawns in OS threads rather than nested async sessions — this means subagents run concurrently but the parent blocks waiting for the thread to join.

### CodingAgent Current State

**File:** `src/tools/subagent_tools.py`

CodingAgent has `delegate_task` and `list_subagent_roles` tools. `delegate_task` delegates work to a sub-agent role but does **not** recursively enter the orchestration loop — it creates a new `Orchestrator` instance and calls its run method synchronously. There is:
- No parent/child session model (child has no `parentID`)
- No automatic tool restriction on the child
- No `task_id` for resuming child sessions
- No `@agent` mention syntax
- No concurrent subagent execution

### Gaps vs Reference Implementations

| Feature | OpenCode | Claw Code | CodingAgent | Gap |
|---|---|---|---|---|
| Recursive loop re-entry | Yes — `SessionPrompt.prompt()` recursion | Yes — full nested `ConversationRuntime` | No — new Orchestrator instance, not loop re-entry | **MISSING** |
| Parent/child session linking | Yes — `parentID` in SQLite | No (OS threads, no session hierarchy) | No | **MISSING** |
| Automatic tool restriction on child | Yes — auto-deny `todowrite`/`task` unless permitted | Yes — `SubagentToolExecutor` allowlist | No | **MISSING** |
| Resumable child sessions | Yes — `task_id` returned, parent can re-invoke | No | No | **MISSING** |
| Concurrent subagent execution | No — sequential in loop | Yes — OS threads | No | Missing |
| `@mention` syntax for agent delegation | Yes — `SubtaskPart` parsed from user messages | No | No | Missing |
| Permission check before spawn | Yes — `ctx.ask({ permission: "task" })` | `PermissionMode::DangerFullAccess` | No gate | **MISSING** |
| Agent output persistence | Stored in child session history | Written to `.md`/`.json` files | Not persisted | Missing |

**Action items:**
- **SPAWN-01 (P0):** Refactor `delegate_task` to recursively call the orchestration loop rather than creating a sibling Orchestrator. Pass `parent_session_id` into the child `AgentState` for lineage tracking.
- **SPAWN-02 (P1):** Apply `AgentDefinition.allowed_tools` as a runtime filter in `execute_tool()` when running in a delegated context — reject calls to tools outside the subagent's allowlist
- **SPAWN-03 (P1):** Return a `child_session_id` from `delegate_task` so parent can resume the same child. Store child session metadata in `SessionStore`.
- **SPAWN-04 (P2):** Add permission gate before spawning: check if current agent is allowed to spawn subagents; publish `spawn.permission_required` event for TUI approval
- **SPAWN-05 (P3):** Implement `@agent-name` mention parsing in the input processing node — detect `@agentname prompt` prefix and convert to a subagent delegation call

---

## 4. Tools Architecture

### OpenCode Pattern

**File:** `packages/opencode/src/tool/registry.ts`

`ToolRegistry.tools(model, agent)` dynamically filters the global tool list per (model, agent) pair:
- `apply_patch` tool is only registered for `gpt-*` models; when active, `edit` and `write` are excluded
- `batch` tool gated behind `cfg.experimental.batch_tool === true`
- `websearch`/`codesearch` gated behind `OPENCODE_ENABLE_EXA=true` or `opencode` provider
- `lsp` tool gated behind `OPENCODE_EXPERIMENTAL_LSP_TOOL=true`
- Agent permission ruleset can deny any tool by name or glob pattern
- Truncation service: tool outputs exceeding 2000 lines / 50 KB are written to a temp file; a hint is returned instead; the hint adapts based on whether the agent has the `task` tool (suggests delegation vs. suggests Grep/Read)

Tool output truncation hint from `tool/truncate.ts`:
- If agent has `task` tool → `"Output truncated. Consider delegating to explore agent."`
- Otherwise → `"Output truncated. Use Grep/Read with offset to continue."`

**All 19 OpenCode tools not currently in CodingAgent:**
- `multiedit` — batch edits to a single file in one call
- `lsp` — go-to-definition, find-references, hover via LSP
- `codesearch` — semantic code search via Exa
- `websearch` — web search via Exa (distinct from DuckDuckGo)
- `batch` — concurrent execution of up to 25 tool calls (experimental)
- `apply_patch` — multi-file OpenAI-style patch format (model-specific)
- `skill` — load a SKILL.md file by name
- `plan_enter` / `plan_exit` — mode switching tools
- `question` — structured multi-part user question (blocks for response)
- `invalid` — fallback for malformed tool calls (tool repair)
- `todowrite` — full todo list replacement (vs. `manage_todo` add/update)

### Claw Code Pattern

**File:** `rust/crates/tools/src/lib.rs`

19 registered tools. Notable tools not in CodingAgent:
- `ToolSearch` — semantic search over the tool registry itself (agent can discover tools)
- `NotebookEdit` — Jupyter notebook cell editing
- `REPL` — code execution in a sandboxed REPL (Python, JS, etc.)
- `PowerShell` — Windows-specific shell
- `Sleep` — explicit wait
- `SendUserMessage` / `Brief` — asynchronous status update to user without blocking
- `StructuredOutput` — structured JSON output extraction
- `Config` — read/write agent configuration settings at runtime

Permission tiers per tool:
- `ReadOnly` — read_file, glob_search, grep_search, WebFetch, WebSearch, TodoWrite, Skill, ToolSearch, Sleep, SendUserMessage, StructuredOutput
- `WorkspaceWrite` — write_file, edit_file, NotebookEdit, Config
- `DangerFullAccess` — bash, Agent, REPL, PowerShell

### CodingAgent Current State

59 tools across 16 modules. Key architectural properties:
- Tools registered via `@tool(tags=[...])` decorator in `_registry.py`
- Toolsets are YAML files mapping role → tool name list
- No runtime tool filtering based on agent type or model
- No tool output truncation service (truncation is per-tool, ad hoc)
- No tool repair for malformed LLM calls
- No `batch` / concurrent tool execution
- No LSP integration tools
- No REPL tool (bash is the execution primitive)

### Gaps vs Reference Implementations

| Feature | OpenCode | Claw Code | CodingAgent | Gap |
|---|---|---|---|---|
| Dynamic tool filtering per (model, agent) | Yes — registry filter | Yes — SubagentToolExecutor allowlist | No — YAML toolset, static | **MISSING** |
| Centralised output truncation | Yes — `Truncate.output()`, adaptive hints | No | Ad hoc per tool | **MISSING** |
| Tool repair for malformed calls | Yes — `experimental_repairToolCall`, `invalid` fallback tool | No | No | **MISSING** |
| Concurrent tool execution | Yes — `batch` tool (experimental) | No | No | Missing |
| LSP tools | Yes — `lsp` tool | No | No | Missing |
| REPL tool | No | Yes — `REPL` tool | No | Missing |
| Tool self-discovery | No | Yes — `ToolSearch` | `list_subagent_roles` only | Partial |
| SendUserMessage (async status) | `question` tool (blocks) | `SendUserMessage`/`Brief` (non-blocking) | `ask_user` (blocks) | Partial |
| Skill system | Yes — `skill` tool + SKILL.md | Yes — `Skill` tool | No | **MISSING** |
| Plan mode tools | `plan_enter`/`plan_exit` | No | No | **MISSING** |

**Action items:**
- **TOOLS-01 (P1):** Implement centralised `Truncate.output(text, agent_context)` in `src/tools/_truncate.py` — uniform 2000 line / 100 KB limit, adaptive hint based on available tools
- **TOOLS-02 (P1):** Add `multiedit` tool to `file_tools.py` — accepts list of `{path, old_string, new_string}` dicts, applies sequentially, returns aggregated results
- **TOOLS-03 (P1):** Implement tool repair: wrap all LLM tool calls in a repair handler that lowercases tool names and maps unknowns to a `tool_not_found` error result instead of raising
- **TOOLS-04 (P2):** Add `ToolSearch` equivalent: `search_tools(query)` in `src/tools/system_tools.py` — searches the tool registry by name + tag similarity
- **TOOLS-05 (P2):** Implement skill system: `load_skill(name)` reads `SKILL.md` files from a configurable skill directory; return content as tool output
- **TOOLS-06 (P2):** Add runtime tool filter: in `execute_tool()`, check `AgentState.active_agent.allowed_tools` before dispatch; return `tool_not_permitted` error for out-of-allowlist calls
- **TOOLS-07 (P3):** Add `batch_tool` — accepts list of tool call dicts, runs concurrently via `asyncio.gather`, returns list of results
- **TOOLS-08 (P3):** Implement `plan_enter` / `plan_exit` as real tool calls that transition `AgentState.agent_mode` between `"plan"` and `"build"`, triggering prompt and toolset rebuild

---

## 5. System Prompts

### OpenCode Pattern

**Files:** `packages/opencode/src/session/system.ts`, `packages/opencode/src/session/prompt/*.txt`, `packages/opencode/src/session/instruction.ts`

OpenCode assembles the system prompt as a **two-part array** on every turn:

```
Part 0 (static, cache-eligible):
  base prompt (selected by model ID pattern: anthropic.txt / gpt.txt / gemini.txt / etc.)
  + plan reminder (if plan mode)
  + max steps reminder (if step limit hit)
  + build-switch reminder (if just transitioned from plan)

Part 1 (dynamic, cache-busted):
  environment block (model, cwd, git branch, platform, date)
  + skills list (available skill names)
  + instruction files (AGENTS.md / CLAUDE.md discovery walking up to workspace root)
```

The two-part structure enables Anthropic prompt caching: the static Part 0 is cached; Part 1 changes every turn and is not cached.

Model-adaptive prompts — 9 distinct base prompts:
- `anthropic.txt` — TodoWrite-first, Task tool emphasis, `file:line` references, parallel tool use encouraged
- `beast.txt` — GPT-o1/o3: fully autonomous, mandatory web research, memory file at `.github/instructions/`
- `gpt.txt` — GPT-4: `apply_patch` required, commentary/final channel structure
- `copilot-gpt-5.txt` — GitHub Copilot GPT-5: `gptAgentInstructions` XML + structured workflow
- `gemini.txt` — Gemini: core mandates + new application workflow + security rules
- `trinity.txt` — Trinity: one-tool-per-message, sequential only
- `kimi.txt` — Kimi: AGENTS.md guidance, working directory safety
- `codex.txt` — OpenAI Codex: file reference link format, `apply_patch` preferred
- `default.txt` — fallback: concise ≤4 lines

Per-agent prompt overrides:
- `explore` agent: completely replaces base prompt with `explore.txt`; no instruction files injected
- `compaction` agent: `compaction.txt` only + env block; no tools; no instructions
- `title` / `summary` agents: their own short prompts; no instructions

Instruction file discovery (`InstructionPrompt.resolve`):
- Walks from the target file's directory up to workspace root
- Collects `AGENTS.md` / `CLAUDE.md` / `CONTEXT.md` files
- Injects each file prefixed with `"Instructions from: <path>\n"`
- Also walks global `~/.config/opencode/AGENTS.md`
- Called dynamically by the `read` tool as files are accessed

### Claw Code Pattern

**File:** `rust/crates/runtime/src/prompt.rs` → `load_system_prompt()`

Claw Code builds a prompt as `Vec<String>` sections:
1. Intro — "You are an interactive agent..."
2. System — tool/permission behaviors
3. Doing tasks — code quality rules
4. Actions — reversibility guidance
5. `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` marker — separates static from dynamic
6. Environment — model family, cwd, date, platform
7. Project context — cwd, date, instruction file count, git status, git diff
8. Instruction files — `CLAW.md`, `CLAW.local.md`, `.claw/CLAW.md`, `.claw/instructions.md`
9. Config — loaded settings
10. LSP context (optional)

Instruction file discovery: `discover_instruction_files()` walks ancestor directories looking for `CLAW.md` variants. Deduplicates by content hash. Limits: 4,000 chars per file, 12,000 chars total budget.

No model-adaptive prompts — same prompt structure for all providers.

### CodingAgent Current State

**File:** `src/core/orchestration/system_prompt_builder.py` (assumed) or inline in orchestrator

CodingAgent has:
- A single system prompt template per role (analyst, coder, reviewer, planner)
- `AGENTS.md` loading from working directory (confirmed by previous audit)
- Environment block (cwd, date) injected into prompt
- No model-adaptive sections
- No two-part cache-eligible structure
- No per-agent prompt overrides (roles are static YAML)
- No dynamic instruction file discovery triggered by `read_file` calls

### Gaps vs Reference Implementations

| Feature | OpenCode | Claw Code | CodingAgent | Gap |
|---|---|---|---|---|
| Model-adaptive base prompts | Yes — 9 distinct templates by model ID | No — single template | No | **MISSING** |
| Two-part cache-eligible structure | Yes — static/dynamic split | Yes — `DYNAMIC_BOUNDARY` marker | No | **MISSING** |
| Per-agent prompt override | Yes — `agent.prompt` replaces base | Yes — subagent prefix | No | **MISSING** |
| AGENTS.md discovery | Yes — walks up dir tree, triggered by `read` | Yes — walks up, deduplicates | Partial — reads from cwd | **PARTIAL** |
| Instruction file budget limit | Implicit (Anthropic context limit) | 4,000 chars/file, 12,000 total | No | Missing |
| Plan mode prompt injection | Yes — `plan.txt` or `plan-reminder-anthropic.txt` | No | No | **MISSING** |
| Max steps prompt injection | Yes — `max-steps.txt` disables tools | No | No | Missing |
| Build-switch reminder | Yes — `build-switch.txt` injected on plan→build | No | No | Missing |
| Environment block | Yes — model, cwd, git branch, platform, date | Yes — similar | Yes — cwd, date | Partial — missing model name, git branch, platform |
| Global instruction files | `~/.config/opencode/AGENTS.md` | No | No | Missing |

**Action items:**
- **PROMPT-01 (P1):** Implement a `SystemPromptBuilder` in `src/core/prompts/` with a two-part structure: static base section + dynamic environment/instructions section. Mark the boundary for future cache-control headers.
- **PROMPT-02 (P1):** Add model-adaptive base prompt selection: maintain at minimum 3 templates (`anthropic.txt`, `openai.txt`, `default.txt`) in `src/config/agent-brain/prompts/`. Select by `provider_id` pattern match.
- **PROMPT-03 (P1):** Expand environment block to include: model name, provider ID, git branch, platform (darwin/linux/windows), today's date, workspace root.
- **PROMPT-04 (P1):** Implement plan mode prompt injection: when `AgentState.plan_mode == True`, append a `plan_reminder.txt` block to the system prompt that restricts write operations and describes the 5-phase plan workflow.
- **PROMPT-05 (P2):** Implement recursive AGENTS.md discovery: walk from `workdir` up to workspace root collecting `AGENTS.md` and `CLAW.md` files; apply per-file character budget (4,000 chars) and total budget (12,000 chars).
- **PROMPT-06 (P2):** Wire `AgentDefinition.prompt_override` into `SystemPromptBuilder`: if the active agent has a `prompt_override`, replace the base prompt section entirely.
- **PROMPT-07 (P3):** Add max steps reminder injection: when `steps_taken >= max_turns - 2`, append a `max_steps.txt` warning block and remove write tools from the active toolset.

---

## 6. Multi-Tiered Orchestration

### OpenCode Pattern

Three tiers:

**Tier 1 — Interactive session (build/plan agent)**
User → TUI → HTTP server → `SessionPrompt.prompt()` → `runLoop()`

**Tier 2 — Subagent (general/explore)**
`task` tool → `Session.create({ parentID })` → `SessionPrompt.prompt()` recursively on child session

**Tier 3 — Internal utility agents (compaction/title/summary)**
`SessionPrompt.command()` → one-shot LLM call, no tool loop, no instruction injection

Key properties:
- Tiers 2 and 3 are implemented as the same code path — `SessionPrompt.prompt()` and `SessionPrompt.command()` — just with different agent configurations
- Tier 2 children can spawn Tier 2 grandchildren unless `task: deny` is in their permission ruleset (default)
- Plan mode is a Tier 1 variation: same `runLoop()` but different agent config (restricted tools, plan prompt)
- The plan → build transition is a runtime agent swap: `session.agentID` is updated to `"build"`, loop continues

Parent/child session tree is queryable: `Session.children(sessionID)` → all child sessions. Visible in the TUI under the parent session.

### Claw Code Pattern

Two tiers:

**Tier 1 — Interactive REPL (`LiveCli`)**
`LiveCli` → `ConversationRuntime<DefaultRuntimeClient, CliToolExecutor>`

**Tier 2 — Subagent (OS thread)**
`Agent` tool → `std::thread::spawn()` → `ConversationRuntime<ProviderRuntimeClient, SubagentToolExecutor>`

No Tier 3 equivalent (no internal utility agents). No coordinator tier above Tier 1 (Python stubs only — not implemented in Rust).

The TypeScript original reportedly had a `coordinator` tier above the assistant and a `buddy/team memory sync` system — these were explicitly **not ported** to Rust (per PARITY.md).

### CodingAgent Current State

**Files:** `src/core/orchestration/orchestrator.py`, `src/tools/subagent_tools.py`

One effective tier:
**Tier 1 — LangGraph pipeline**
User → TUI → `Orchestrator.run()` → LangGraph nodes

**Pseudo Tier 2 — Delegation (incomplete)**
`delegate_task` → new `Orchestrator()` instance → `.run()` synchronously
Problems: no shared state, no `parentID`, no tool restriction on child, blocks the parent thread

There is an `analyst_delegation_node` in the LangGraph graph that routes complex queries to an analyst role, but this is a within-graph node transition, not a true subagent with its own message history.

### Gaps vs Reference Implementations

| Feature | OpenCode | Claw Code | CodingAgent | Gap |
|---|---|---|---|---|
| True recursive tier | Yes — `SessionPrompt.prompt()` recursion | Yes — nested `ConversationRuntime` | No — new Orchestrator, no lineage | **CRITICAL GAP** |
| Internal utility agents (Tier 3) | Yes — compaction, title, summary via `command()` | No | Compaction exists as inline logic | Partial |
| Plan mode as agent-type transition | Yes — `session.agentID` swap + prompt rebuild | No | `plan_mode` flag, no prompt swap | **MISSING** |
| Session hierarchy visibility | Yes — `Session.children()`, visible in TUI | No | No | Missing |
| Concurrent subagents | No (sequential task tool) | Yes (OS threads) | No | Missing |
| Coordinator tier above assistant | No (deprecated; moved to Tier 1 agent) | Not ported | `analyst_delegation_node` (partial) | Not needed |

**Action items:**
- **MULTI-01 (P0):** This is the same as SPAWN-01 above — it is the single most architecturally important gap. Recursive loop re-entry for delegated tasks with proper parent/child session model.
- **MULTI-02 (P1):** Implement `plan_mode` as a true agent-type transition: when `plan_enter` tool is called, swap `AgentState.active_agent` to `AgentDefinition("plan")` and rebuild the system prompt with `plan_reminder.txt`. When `plan_exit` is called, swap back to `build` agent and inject `build_switch.txt` into the next system block.
- **MULTI-03 (P2):** Implement internal utility agents as `SessionPrompt.command()` equivalents: one-shot LLM calls with no tool loop for compaction summary generation and session title generation. Currently these are either inline or missing.
- **MULTI-04 (P2):** Expose session hierarchy in `SessionStore`: add `parent_session_id` to session metadata. `Session.children(session_id)` returns all child session IDs. Surface in TUI as a tree under the parent session entry.

---

## 7. Permission System

### OpenCode Pattern

**File:** `packages/opencode/src/permission/`

Three-layer permission system:
1. **Config rules** — `cfg.permission` defines a `Ruleset`: array of `{ pattern: string, behavior: "allow" | "deny" | "ask" }` rules. Wildcard patterns like `"bash(git *)"`, `"write(*)"`, `"task"`. Last-matching rule wins.
2. **Per-agent ruleset** — each agent definition can have its own `Permission.Ruleset` that overrides or augments the config rules
3. **Interactive approval** — when a rule resolves to `"ask"`, `permission.ask()` publishes a `tool.permission_required` event and blocks until the TUI replies with grant/deny

For bash commands specifically, `BashArity.prefix(tokens)` extracts the canonical command prefix (e.g., `git checkout` from `git checkout main`) using tree-sitter AST parsing. This canonical prefix is matched against the permission patterns — so you can write rules like `"bash(git checkout *)"` that match all git checkout commands.

Doom-loop guard: last 3 identical tool calls → `permission.ask("doom_loop")`.

### Claw Code Pattern

**File:** `rust/crates/runtime/src/permissions.rs`

Simple four-level enum: `ReadOnly < WorkspaceWrite < DangerFullAccess < Prompt`. Each tool declares its required level. The policy compares active mode vs required level:
- If active mode ≥ required → allow
- If active mode is `Prompt` or active is `WorkspaceWrite` but required is `DangerFullAccess` → delegate to `PermissionPrompter::decide()`
- Otherwise → deny

Default: `DangerFullAccess` (no prompting). Config labels: `"plan"` / `"read-only"` → `ReadOnly`, `"acceptEdits"` → `WorkspaceWrite`, `"dontAsk"` → `DangerFullAccess`.

### CodingAgent Current State

**File:** `src/core/orchestration/orchestrator.py` → `execute_tool()`

CodingAgent has:
- `PermissionLevel` enum checked in `execute_tool()` (from previous audit — `TASK-01` done)
- `plan_mode` flag that blocks write tools when enabled
- `register_tool_gate` hook for custom approval gates
- Diff preview gate in `file_tools.py` — `_pending_previews` threading.Event gate
- No wildcard pattern rules
- No config-level permission ruleset
- No tree-sitter bash command parsing for fine-grained bash permission matching

### Gaps vs Reference Implementations

| Feature | OpenCode | Claw Code | CodingAgent | Gap |
|---|---|---|---|---|
| Config-level permission rules | Yes — wildcard `allow`/`deny`/`ask` rules | Yes — named modes | No | **MISSING** |
| Per-agent permission override | Yes | Yes — SubagentToolExecutor allowlist | No | **MISSING** |
| Interactive approval gate | Yes — `permission.ask()` → TUI dialog | Yes — `PermissionPrompter` | Partial — diff preview gate only | **PARTIAL** |
| Bash command prefix parsing | Yes — tree-sitter AST | No — tool-level only | No | Missing |
| Doom-loop permission gate | Yes | No | No | **MISSING** |

**Action items:**
- **PERM-01 (P1):** Implement a config-level permission ruleset: `src/core/orchestration/permission_policy.py` with `PermissionPolicy(rules: list[PermissionRule])`. Each rule: `{pattern: str, behavior: "allow"|"deny"|"ask"}`. `evaluate(tool_name, input)` uses last-matching-wins. Load from `~/.config/codingagent/permissions.json`.
- **PERM-02 (P1):** Wire doom-loop gate into `PermissionPolicy`: when `doom_loop_detected` fires, evaluate against `"doom_loop"` pattern — default `"ask"`.
- **PERM-03 (P2):** Add per-agent permission override: `AgentDefinition.permission_rules` extends the global policy for the duration of that agent's execution.
- **PERM-04 (P3):** Implement bash prefix extraction: use `shlex.split()` as a lightweight substitute for tree-sitter to extract the canonical command prefix (`git checkout` from `git checkout main`). Apply permission rules against this prefix.

---

## 8. Priority & Effort Summary

| ID | Gap | Priority | Effort | Blocked By |
|---|---|---|---|---|
| SPAWN-01 | Recursive loop re-entry for delegation | P0 | XL | — |
| MULTI-01 | Same as SPAWN-01 | P0 | — | — |
| AGENT-01 | `AgentDefinition` dataclass | P1 | S | — |
| AGENT-02 | Built-in agent definitions | P1 | S | AGENT-01 |
| SPAWN-02 | Apply `allowed_tools` in `execute_tool()` | P1 | S | AGENT-01 |
| SPAWN-03 | Child session persistence + `child_session_id` | P1 | M | SPAWN-01 |
| ORCH-01 | Doom-loop detection | P1 | S | — |
| TOOLS-01 | Centralised `Truncate.output()` | P1 | S | — |
| TOOLS-02 | `multiedit` tool | P1 | S | — |
| TOOLS-03 | Tool repair for malformed calls | P1 | S | — |
| PROMPT-01 | Two-part `SystemPromptBuilder` | P1 | M | — |
| PROMPT-02 | Model-adaptive base prompts | P1 | M | PROMPT-01 |
| PROMPT-03 | Expanded environment block | P1 | S | PROMPT-01 |
| PROMPT-04 | Plan mode prompt injection | P1 | S | PROMPT-01, AGENT-02 |
| PERM-01 | Config-level permission ruleset | P1 | M | — |
| PERM-02 | Doom-loop permission gate | P1 | S | ORCH-01, PERM-01 |
| MULTI-02 | `plan_enter`/`plan_exit` agent-type transition | P1 | M | AGENT-02, PROMPT-04 |
| AGENT-03 | Wire `prompt_override` into builder | P2 | S | AGENT-01, PROMPT-01 |
| SPAWN-04 | Spawn permission gate | P2 | S | PERM-01, SPAWN-01 |
| ORCH-02 | Extract loop guards to `loop_guards.py` | P2 | S | — |
| ORCH-03 | Mid-run max steps enforcement | P2 | S | PROMPT-01 |
| TOOLS-04 | `ToolSearch` / `search_tools()` | P2 | S | — |
| TOOLS-05 | Skill system (`load_skill`) | P2 | M | — |
| TOOLS-06 | Runtime tool filter in `execute_tool()` | P2 | S | AGENT-01, SPAWN-02 |
| PROMPT-05 | Recursive AGENTS.md discovery | P2 | M | — |
| PROMPT-06 | `prompt_override` in builder | P2 | S | AGENT-01, PROMPT-01 |
| MULTI-03 | Internal utility agents (compaction title gen) | P2 | M | AGENT-02 |
| MULTI-04 | Session hierarchy in SessionStore + TUI | P2 | M | SPAWN-03 |
| PERM-03 | Per-agent permission override | P2 | S | AGENT-01, PERM-01 |
| AGENT-04 | `Agent.generate()` from natural language | P3 | L | AGENT-01, PROMPT-02 |
| SPAWN-05 | `@agent-name` mention parsing | P3 | M | SPAWN-01 |
| TOOLS-07 | `batch_tool` concurrent execution | P3 | M | — |
| TOOLS-08 | `plan_enter`/`plan_exit` tools | P3 | S | MULTI-02 |
| PROMPT-07 | Max steps reminder injection | P3 | S | PROMPT-01, ORCH-03 |
| PERM-04 | Bash prefix extraction (`shlex`) | P3 | S | PERM-01 |

**Effort key:** XS < 1h · S 1–2h · M 3–6h · L 1–2d · XL 3–5d

---

## 9. Recommended Implementation Order (Sprints)

### Sprint A — Foundation (unblock everything else)
1. **AGENT-01** — `AgentDefinition` dataclass (S)
2. **AGENT-02** — Built-in agents: `build`, `explore`, `plan`, `verification` (S)
3. **PROMPT-01** — Two-part `SystemPromptBuilder` (M)
4. **PERM-01** — `PermissionPolicy` with wildcard rules (M)

### Sprint B — Orchestration Loop Hardening
5. **ORCH-01** — Doom-loop detection in `AgentState` (S)
6. **PERM-02** — Doom-loop permission gate (S)
7. **ORCH-02** — Extract loop guards to `loop_guards.py` (S)
8. **TOOLS-01** — Centralised `Truncate.output()` (S)
9. **TOOLS-03** — Tool repair for malformed calls (S)

### Sprint C — Subagent Spawning (largest item)
10. **SPAWN-01** — Recursive loop re-entry with parent/child session model (XL)
11. **SPAWN-02** — `allowed_tools` enforcement in `execute_tool()` (S)
12. **SPAWN-03** — Child session persistence + `child_session_id` return (M)
13. **SPAWN-04** — Spawn permission gate (S)

### Sprint D — Prompt Quality
14. **PROMPT-02** — Model-adaptive base prompts (anthropic/openai/default) (M)
15. **PROMPT-03** — Expanded environment block (M — now S, based on PROMPT-01)
16. **PROMPT-04** — Plan mode prompt injection (S)
17. **PROMPT-05** — Recursive AGENTS.md discovery (M)
18. **MULTI-02** — `plan_enter`/`plan_exit` agent-type transition (M)

### Sprint E — Polish & Missing Tools
19. **TOOLS-02** — `multiedit` tool (S)
20. **TOOLS-04** — `search_tools()` (S)
21. **TOOLS-05** — Skill system (M)
22. **TOOLS-06** — Runtime tool filter in `execute_tool()` (S)
23. **MULTI-03** — Compaction title/summary internal agents (M)
24. **MULTI-04** — Session hierarchy in SessionStore + TUI (M)
