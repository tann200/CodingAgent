# CodingAgent Architecture Reference

> **Purpose:** Comprehensive reference document covering the architecture, patterns,
> and practices of the CodingAgent repository. Structured to mirror
> `docs/claw-code-architecture.md` for direct comparison analysis.
> All file paths are relative to the `CodingAgent` repository root unless
> an absolute path is given.
>
> **Status:** Production-ready — 500+ tests passing, 0 Critical/High issues
> **Last Updated:** 2026-04-27
> **Atomic Writes:** Implemented in 13 files (see `docs/ATOMIC_WRITE_SUMMARY.md`)
> **Implementation Plan:** See `IMPLEMENTATION_PLAN.md` - All P0/P1 items complete
> **Architecture V2:** Context directory `.codingAgent`, tiered memory, FTS5 search

---

## Table of Contents

1. [Project Layout](#1-project-layout)
2. [Orchestration & Agent Pipeline](#2-orchestration--agent-pipeline)
3. [System Prompt Architecture](#3-system-prompt-architecture)
4. [Tool System](#4-tool-system)
5. [Context Management & Compaction](#5-context-management--compaction)
6. [Session & Memory Management](#6-session--memory-management)
7. [Configuration System](#7-configuration-system)
8. [MCP Integration](#8-mcp-integration)
9. [Permission & Security System](#9-permission--security-system)
10. [Provider & Model Abstraction](#10-provider--model-abstraction)
11. [EventBus & Observability](#11-eventbus--observability)
12. [TUI](#12-tui)
13. [Test Architecture](#13-test-architecture)
14. [Key Architectural Patterns](#14-key-architectural-patterns)
15. [Comparison Reference: CodingAgent vs Claw Code](#15-comparison-reference-codingagent-vs-claw-code)

---

## 1. Project Layout

```
CodingAgent/
├── src/                               # Main source package
│   ├── core/                          # Orchestration, inference, memory, context, indexing
│   │   ├── paths.py                   # OS-agnostic path utilities (localappdata vs per-user CodingAgent data dir — use src.core.paths.get_data_dir())
│   │   ├── orchestration/             # LangGraph pipeline + orchestrator + services
│   │   ├── inference/                 # LLM adapters, model tiers, tokenizer
│   │   │   ├── adapters/              # Ollama, OpenAI, Anthropic, Gemini
│   │   │   ├── model_capability_profile.py  # v2: ModelProfile, AgentMode
│   │   │   ├── hardware_capability_profile.py  # v2: HardwareProfile, VRAM detection
│   │   │   ├── runtime_profile.py     # v2: model × hardware merge
│   │   │   ├── workflow_selector.py    # v2: SINGLE_LOOP vs FRONTIER_LOOP
│   │   │   ├── kv_cache_governor.py   # v2: VRAM monitoring
│   │   │   ├── thinking_utils.py      # v2: thinking mode control
│   │   │   ├── tokenizer.py           # tiktoken + HuggingFace
│   │   │   └── model_tiers.py         # Legacy tier system (NANO→FRONTIER)
│   │   ├── context/                   # ContextBuilder, ContextController
│   │   ├── memory/                    # Distiller, SessionStore, AdvancedMemory
│   │   ├── indexing/                  # RepoIndexer, VectorStore, SymbolGraph, LSP
│   │   └── mcp/                       # MCP client
│   ├── tools/                         # 60+ agent tools
│   ├── config/                        # Provider configs, agent-brain (roles/skills/identity)
│   ├── server/                        # Minimal HTTP server
│   └── main.py                        # CLI entrypoint
├── tui/                               # Textual TUI application
│   └── src/ui/                        # Screens, bridge, controller, features
├── tests/                             # 240 tests
│   ├── unit/                          # 206 unit tests
│   ├── integration/                   # 25 integration tests
│   ├── e2e/                           # 3 end-to-end scenario tests
│   └── benchmarks/                    # 7 latency benchmarks
├── docs/                              # Architecture and design documents
├── scripts/                           # Dev scripts (run_tui, add_provider, etc.)
├── .codingAgent/                   # Per-workspace runtime state (SQLite, preferences.md, TODO.md, etc.)
├── pyproject.toml
├── requirements.txt
└── pytest.ini
```

### Source package tree

```
src/core/orchestration/
├── orchestrator.py                 # Orchestrator class (top-level wiring)
├── orchestrator_bootstrap.py       # Four-phase bootstrap (infrastructure → providers → events → services)
├── graph/
│   ├── state.py                    # AgentState TypedDict (~100 fields)
│   ├── builder.py                  # compile_agent_graph() + router functions
│   ├── graph_factory.py            # GraphFactory: planner / coder / reviewer / researcher variants
│   └── nodes/                      # 16 node files (one per cognitive role)
├── event_bus.py                    # Thread-safe in-process EventBus + ContextVar correlation IDs
├── tool_preflight.py               # Pre-execution validation (name, bash patterns, path containment, P3-D fuzzy)
├── task_lifecycle.py               # start_new_task_impl, restore_continue_state_impl
├── loop_guards.py                  # Stuck-loop + doom-loop detection
├── approval_gate.py                # Pre-tool user confirmation gate
├── plan_mode.py                    # PlanMode toggle with event publishing
├── preview_service.py              # File diff preview before write
├── agent_session_manager.py        # AgentSessionManager (state hydration)
├── agent_brain.py                  # AgentBrain: role → system prompt coordinator
├── instruction_loader.py           # Instruction file loader (SOUL, roles, skills)
├── mcp_stdio_server.py             # MCP server (expose tools to Claude Code/IDE)
├── rollback_manager.py             # Snapshot + restore working directory state
├── session_cost_tracker.py         # SessionCostTracker (D-10 service)
├── preview_coordinator.py          # PreviewCoordinator (D-10 service)
└── tool_execution_service.py       # ToolExecutionService (D-10 service)

src/core/inference/
├── llm_manager.py                  # call_model(), ProviderManager, model cache
├── model_tiers.py                  # ModelTier enum + classify_model() + per-tier limits
├── provider_context.py             # get_context_budget(), get_actual_context_window()
├── tokenizer.py                    # count_tokens() via tiktoken
├── thinking_utils.py               # strip_thinking() for reasoning models
├── telemetry.py                    # Token usage tracking
└── adapters/
    ├── openai_compat_adapter.py    # Base class (OpenAI REST compatible)
    ├── lm_studio_adapter.py
    ├── ollama_adapter.py
    ├── anthropic_adapter.py
    ├── github_copilot_adapter.py
    ├── groq_adapter.py
    ├── openrouter_adapter.py
    ├── litellm_adapter.py
    └── mock_adapter.py             # CI / unit tests

src/tools/
├── _registry.py                    # ToolRegistry (register, discover, call, OpenAI schema)
├── _tool.py                        # @tool decorator
├── _file_io.py                     # read_file, write_file, list_files, glob
├── _bash_exec.py                   # bash(), bash_readonly()
├── bash_security.py                # BashRiskLevel + analyze_bash_command()
├── guardrails.py                   # read-before-write + deny_write_patterns
├── git_tools.py                    # git_status/log/diff/commit/stash/restore
├── verification_tools.py           # run_tests, run_linter, syntax_check
├── todo_tools.py                   # manage_todo (create/update/complete steps)
├── subagent_tools.py               # delegate_task (spawns sub-session)
├── repo_tools.py                   # search_code, find_symbol, find_references
├── memory_tools.py                 # memory_search (vector store semantic search)
├── web_tools.py                    # web_search, read_web_page (SSRF-protected)
├── lsp_tools.py                    # lsp_diagnostics/references/definition/symbols/hover/rename
├── formatter.py                    # run_formatter (post-write code formatting)
└── ...                             # + 15 more tool modules
```

---

## 2. Orchestration & Agent Pipeline

### Architecture decision: LangGraph state machine

CodingAgent is built on **LangGraph** — a directed acyclic graph (DAG) library for
Python. Each node is an async function that receives `AgentState`, modifies it, and
returns the updated state. Edges (including conditional edges) route state to the
next node based on router functions.

This is the primary architectural distinction from claw code's simple loop. The pipeline
encodes domain knowledge about software engineering workflows as graph structure
(e.g., analysis before planning, verification after writes, debug before replan).

### AgentState (`src/core/orchestration/graph/state.py`)

The shared state TypedDict with ~100 fields:

```python
class AgentState(TypedDict):
    # Core task
    task: str
    working_dir: str
    system_prompt: str
    rounds: int
    errors: List[str]

    # Conversation history (append-only — LangGraph reducer)
    history: Annotated[List[Dict[str, Any]], operator.add]
    verified_reads: Annotated[List[str], operator.add]

    # Tool call output
    next_action: Dict[str, Any] | None   # Parsed tool call from LLM
    last_result: Dict[str, Any] | None   # Tool execution result
    last_tool_name: str | None

    # Planning
    current_plan: List[Dict[str, Any]] | None  # [{id, description, status}]
    current_step: int | None
    plan_resumed: bool | None
    plan_attempts: int | None
    replan_attempts: int | None
    task_decomposed: bool | None

    # Analysis
    analysis_summary: str | None
    relevant_files: List[str] | None
    key_symbols: List[str] | None
    analyst_findings: str | None
    repo_summary_data: str | None

    # Delegation
    delegations: List[Dict[str, Any]] | None
    delegation_depth: int | None
    delegation_results: Dict[str, Any] | None

    # Debug & recovery
    debug_attempts: int | None
    total_debug_attempts: int | None
    last_debug_error_type: str | None
    no_plan_fail_count: int | None

    # Verification & evaluation
    verification_passed: bool | None
    verification_result: Dict[str, Any] | None
    evaluation_result: str | None
    evaluation_llm_verdict: str | None

    # Tool budgets & loop guards
    tool_call_count: int | None
    max_tool_calls: int | None
    tool_last_used: Dict[str, float] | None    # Cooldown tracking

    # Context & memory
    _compacted_history: List[Dict[str, Any]] | None
    _compaction_last_round: int | None

    # Plan mode (human approval gate)
    plan_mode_enabled: bool | None
    awaiting_plan_approval: bool | None
    plan_mode_approved: bool | None

    # Parallel execution (DAG waves)
    plan_dag: Dict[str, Any] | None
    execution_waves: List[List[str]] | None
    current_wave: int | None

    # Preview (diff-before-write)
    preview_mode_enabled: bool | None
    preview_confirmed: bool | None

    # Model adaptation
    model_tier: str | None                # "nano" / "small" / "medium" / "large" / "frontier"
    session_cost_usd: float | None

    # State hydration & cost
    session_id: str | None
    parent_session_id: str | None
    plan_mode_enabled: bool | None

    # Git snapshots (S4-A)
    snapshots: List[str] | None

    # + ~40 more operational fields
```

`validate_state(state)` checks numeric bounds and type invariants at node entry.

### The 16-node cognitive pipeline

```
             ┌──────────────────────────────────────────────────────┐
             │                                                      │
User task ──▶ perception ──▶ analyst_delegation ──▶ analysis       │
                  │                (complex?)          │            │
                  │                                    ▼            │
                  │                              planning ◀─────────┤ replan
                  │                                    │            │
                  │                            plan_validator       │
                  │                             │         │         │
                  │                          (valid)  (invalid)     │
                  │                             │         │         │
                  └─────────── fast-path ───▶ execution ◀───────────┘
                                                   │
                                          step_controller
                                                   │
                                               (verify?)
                                               │      │
                                        verification   ╲
                                               │        ╲ (no verify)
                                          (pass/fail)    ╲
                                               │          ▼
                                         debug ◀──────▶ evaluation
                                               │              │
                                            (fixed)      (pass/fail)
                                               │              │
                                           execution    replan / memory_sync
                                                              │
                                                       memory_update ──▶ END
                                                              │
                                                       delegation (if needed)
```

**Fast path:** if `next_action` is set after perception AND the task is simple → execution
directly (skip analysis + planning). Complex tasks (W3) always go through analysis.

### Graph builder (`src/core/orchestration/graph/builder.py`)

```python
def compile_agent_graph(orchestrator, graph_type="standard") -> CompiledStateGraph:
    graph = StateGraph(AgentState)
    # Add all 16 nodes
    graph.add_node("perception",           perception_node)
    graph.add_node("analysis",             analysis_node)
    graph.add_node("analyst_delegation",   analyst_delegation_node)
    # ... 13 more nodes
    # Conditional routing edges
    graph.add_conditional_edges("perception",      route_after_perception)
    graph.add_conditional_edges("planning",        should_after_planning)
    graph.add_conditional_edges("plan_validator",  should_after_plan_validator)
    graph.add_conditional_edges("execution",       should_after_execution_with_replan)
    graph.add_conditional_edges("debug",           should_after_debug)
    graph.add_conditional_edges("evaluation",      should_after_evaluation)
    graph.add_conditional_edges("memory_sync",     should_after_memory_sync)
    return graph.compile()
```

**Key constants in builder.py:**
```python
_MAX_ROUNDS_PLANNING      = 15    # Force-end planning after this many rounds
_DEFAULT_MAX_TOOL_CALLS   = 30    # Standard graph tool budget
_AUTONOMOUS_MAX_TOOL_CALLS = 100  # Autonomous mode budget
_LOOP_GUARD_ROUNDS        = 10    # Stuck-loop detection threshold
```

### Orchestrator bootstrap (`src/core/orchestration/orchestrator_bootstrap.py`)

Four-phase bootstrap called from `Orchestrator.__init__()`:

```python
def bootstrap_orchestrator(orch):
    _init_infrastructure(orch)   # MessageManager, thread pools, RollbackManager,
                                 # FileLockManager, GitSnapshotManager, SessionStore,
                                 # LifecycleManager, SessionManager
    _init_providers(orch)        # Provider/adapter selection, startup event
    _init_event_subscriptions(orch)   # All EventBus subscriptions
    _init_services(orch)         # TokenBudgetMonitor, ContextController,
                                 # PreviewService, PlanMode, CostTracker,
                                 # ToolExecutionService, MCP server
```

### Node summary

| Node | File | Role prompt | Purpose |
|---|---|---|---|
| `perception_node` | `nodes/perception_node.py` | `operational` | LLM call; parse tool action; overflow detection; tier detection |
| `analysis_node` | `nodes/analysis_node.py` | none (deterministic) | Repo summary; symbol graph queries |
| `analyst_delegation_node` | `nodes/analyst_delegation_node.py` | none | Spawn analyst subagent for complex tasks |
| `planning_node` | `nodes/planning_node.py` | `strategic` | Build/resume plan; TTL-based cross-session resumption |
| `plan_validator_node` | `nodes/plan_validator_node.py` | none | Validate step count, tool names; skipped for LARGE/FRONTIER |
| `execution_node` | `nodes/execution_node.py` | `operational` | Dispatch tool calls; PRSW locks; output truncation |
| `step_controller_node` | `nodes/step_controller_node.py` | none | Decompose complex steps for small models |
| `verification_node` | `nodes/verification_node.py` | none | Verify side-effecting tools succeeded |
| `debug_node` | `nodes/debug_node.py` | `debugger` | Retry failed calls; JS/TS `node --check` |
| `replan_node` | `nodes/replan_node.py` | `strategic` | Re-plan on evaluation failure; increment `replan_attempts` |
| `evaluation_node` | `nodes/evaluation_node.py` | none | LLM semantic verdict on task success |
| `memory_update_node` | `nodes/memory_update_node.py` | none | Distill context; update vector store |
| `delegation_node` | `nodes/delegation_node.py` | none | CrossSessionBus subagent dispatch |
| `wait_for_user_node` | `nodes/wait_for_user_node.py` | none | Suspend for human plan approval / diff preview |

### Perception node internals (`nodes/perception_node.py`)

```python
async def _perception_node_impl(state, config):
    # 1. Classify model tier from active adapter
    model_tier = classify_model(adapter.model_name)
    state["model_tier"] = model_tier.value

    # 2. Build system prompt via ContextBuilder
    system_prompt = context_builder.build_system_prompt(
        role="operational", task=state["task"],
        tools=get_tools_for_role("operational"),
        model_tier=model_tier.value,
    )

    # 3. Assemble messages (history + compacted history if available)
    messages = _build_messages(state)

    # 4. Prune oversized tool outputs
    messages = _prune_tool_outputs(messages, budget=get_actual_context_window() * 0.6)

    # 5. LLM call
    response = await call_model(messages, tools=tools, ...)

    # 6. Overflow detection (OP-4)
    if is_overflow(response.errors):
        state["errors"] = ["context_overflow"]
        state["_compacted_history"] = messages[-6:]  # OVF-2
        return state  # route_after_perception → memory_sync

    # 7. Parse next_action from response
    state["next_action"] = parse_tool_call(response.content)
    state["history"].append({"role": "assistant", "content": response.content})
    return state
```

Key helper: `_prune_tool_outputs(history, budget)` — zeros out old `tool_execution_result`
content beyond the 40K token boundary, preserving messages with `metadata.preserve=True`
and the most recent 6 messages.

---

## 3. System Prompt Architecture

### ContextBuilder (`src/core/context/context_builder.py`)

```python
class ContextBuilder:
    # Module-level caches (thread-safe, max 256 entries each)
    _TEXT_CACHE:           Dict[str, tuple]   # path → (mtime, content)
    _JSON_CACHE:           Dict[str, tuple]   # path → (mtime, parsed)
    _STATIC_PROMPT_CACHE:  Dict[tuple, str]   # (role, skills, tools_hash, tier, ...) → prompt
    _DYNAMIC_ENV_CACHE:    Dict[tuple, str]   # (workdir, date_iso, git_head) → env block

    @classmethod
    def clear_cache(cls)           # Called at every start_new_task() (PB-2)
    @classmethod
    def invalidate_path(cls, path) # Remove specific path from cache after writes

    def build_system_prompt(
        role: str,
        task: str,
        tools: List[Dict],
        skills: List[str] = (),
        model_tier: str = "",
        simple_mode: bool = False,
    ) -> str
```

### Section injection order

```
[STATIC SECTION — cached by hash]
1.  SOUL identity block           src/config/agent-brain/identity/SOUL.md
2.  Role description              src/config/agent-brain/roles/<role>.md
3.  Active skills                 src/config/agent-brain/skills/<skill>.md  (one per skill)
4.  Tool list                     YAML (NANO/SMALL) or JSON (MEDIUM+)
5.  Model constraints block       <model_constraints> (NANO/SMALL only — P1-E)

[DYNAMIC BOUNDARY: "---DYNAMIC---"]
[DYNAMIC SECTION — cached per workdir/date/git-head]
6.  Environment block             date, git HEAD, workspace path
7.  TODO.md / PLAN.md progress    <task_progress> (if file exists)
8.  Per-project instructions      <project_config_instructions> from .codingAgent/config.json (OP-5)
9.  Prior session memories        <prior_context> from VectorStore (S9-A)
10. Task embedding                <task>...</task>
```

### Two-tier prompt caching (P3-A)

- **Static tier**: SOUL + role + skills + tools + constraints → cached by tuple-hash of inputs.
  Invalidated when `clear_cache()` is called (task boundary).
- **Dynamic tier**: env block (date + git HEAD) → cached per `(working_dir, date_iso, git_head)`.
  The boundary marker enables Anthropic prompt caching to cache the stable prefix.

### Tier adaptation

| Tier | Tool format | Tool count | simple_mode | model_constraints injected |
|---|---|---|---|---|
| NANO | YAML | 8 | True | Yes |
| SMALL | YAML | 20 | False | Yes |
| MEDIUM | JSON | 35 | False | No |
| LARGE | JSON | 50 | False | No |
| FRONTIER | JSON | 60 | False | No |

`_prune_tools(tools, tier)` keeps only the first `_TOOL_LIMITS[tier]` tools.
`_render_tools_for_tier(tools, tier)` formats as YAML for NANO/SMALL (less token overhead).

### Agent-brain files (`src/config/agent-brain/`)

```
identity/
└── SOUL.md               "Elite autonomous SE system. GSD. Action > explanation."

roles/
├── operational.md         Execute planned steps (most nodes use this)
├── operational-small.md   SMALL-model variant (shorter, step-by-step focus)
├── operational-gemma4.md  Gemma 4 model variant (avoids known quirks)
├── operational-frontier.md Frontier-model variant (parallel calls, extended reasoning)
├── strategic.md           Plan decomposition (planning_node, replan_node)
├── analyst.md             Codebase analysis (analyst_delegation_node)
├── debugger.md            Error recovery (debug_node)
├── reviewer.md            Code review
├── researcher.md          Codebase exploration
├── scout.md               Initial repo scanning
└── tester.md              Test generation/execution

skills/
├── code_review.md
├── refactor.md
├── write_tests.md
├── dry.md
├── security_review.md
├── stuck.md               Auto-recovery skill (S10-B)
├── debug_checklist.md
└── context_hygiene.md
```

Workspace-level skill overrides: `.agent/skills/*.md`, `.claude/skills/*.md` (workspace)

### Instruction file discovery (CP-11 + OP-5)

CodingAgent uses a robust discovery system that mirrors claw code's ancestor-walk:

1. **Ancestor walk (CP-11):** Walks from the current working directory up to the filesystem root, looking for `AGENTS.md` and `.agent/AGENTS.md` in each directory.
2. **Deduplication (CP-3):** Uses SHA-256 content hashing to ensure that the same instruction content is not injected multiple times (common when symlinks or repeated files exist).
3. **Budgeting:** Enforces a per-file character cap (4,000) and a total character cap (12,000) to prevent context bloat.
4. **Project context:** `.codingAgent/config.json#instructions` — per-project instruction strings (OP-5).
5. **Progress tracking:** `TODO.md` / `PLAN.md` in `.codingAgent/` — injected as `<task_progress>`.
6. **Bundled brain:** `src/config/agent-brain/` — bundled roles + skills.

Discovered files are rendered with scope labels (e.g., `AGENTS.md (scope: /path/to/dir)`) so the model understands the hierarchy.

---

## 4. Tool System

### Registration (`src/tools/_registry.py`)

```python
class ToolRegistry:
    def register(
        name: str,
        fn: Callable,
        side_effects: List[str] = (),  # ["write", "network", "execute"]
        description: str = "",
        tags: List[str] = (),
        origin: str = "builtin",
    ) -> None

    def discover(module) -> None          # Auto-discover @tool-decorated functions
    def call(tool_name, **kwargs) -> Any
    def deregister(name) -> None          # Used for project-level disable (OP-5)
    def filter_by_names(names) -> "ToolRegistry"
    def get_openai_functions() -> List[Dict]   # OpenAI function-call schema
```

`example_registry()` in `orchestrator.py` registers all 60+ builtin modules then merges
any MCP-discovered tools.

### @tool decorator (`src/tools/_tool.py`)

```python
@tool(side_effects=["write"], tags=["coding"])
def write_file(path: str, content: str, workdir: Path = None) -> Dict[str, Any]:
    ...
```

The decorator auto-extracts JSON Schema from type annotations, fills in description from
docstring, and registers `side_effects` and `tags` for preflight checks and toolset
filtering.

### Tool dispatch path

```
execution_node
  │
  ├─ preflight_check_impl(orch, tool_call)      ← tool_preflight.py
  │    ├─ name must be str and registered
  │    ├─ P3-D: fuzzy auto-correction (SMALL+, cutoff=0.85)
  │    ├─ bash: DANGEROUS_PATTERNS check
  │    └─ write tools: path inside working_dir
  │
  ├─ approval_gate.py (if MODIFYING_TOOLS)      ← human approval if enabled
  │
  ├─ ToolExecutionService.execute(tool_call)    ← D-10 service
  │    ├─ _execute_tool_with_locks()            ← PRSW FileLockManager
  │    └─ _truncate_tool_output(res)            ← OP-9 cap at 50 KB
  │
  └─ push result to history
```

### Built-in tool categories

| Category | Key tools | Side effects |
|---|---|---|
| File I/O | `read_file`, `write_file`, `edit_file`, `edit_by_line_range`, `list_files`, `glob` | write |
| Bash | `bash`, `bash_readonly` | execute |
| Git | `git_status`, `git_log`, `git_diff`, `git_commit`, `git_stash`, `git_restore` | write (commit) |
| Verification | `run_tests`, `run_linter`, `syntax_check` | none |
| Search | `search_code`, `find_symbol`, `find_references`, `grep_search` | none |
| Web | `web_search`, `read_web_page` | network |
| LSP | `lsp_diagnostics`, `lsp_references`, `lsp_definition`, `lsp_hover`, `lsp_rename` | none/write |
| Memory | `memory_search` | none |
| Sub-agents | `delegate_task` | execute |
| Todo | `manage_todo` | write |
| Rollback | `create_snapshot`, `restore_snapshot` | write |
| Plan mode | `enter_plan_mode`, `exit_plan_mode` | none |

### Toolset filtering (`src/tools/toolsets/`)

YAML toolset definitions map roles to tool subsets:

```yaml
# toolsets/coding.yaml
name: coding
tools:
  - read_file
  - write_file
  - edit_file
  - bash
  - run_tests
  - search_code
  # ...
```

`get_tools_for_role(role)` in `orchestrator.get_tools_for_role()` resolves the toolset
for a role, returns filtered tool list; falls back to full registry if toolset is missing
or has fewer than 3 registered tools (with a warning log — SCAN-4).

### Preflight check (`src/core/orchestration/tool_preflight.py`)

```python
def preflight_check_impl(orch, tool_call) -> {"ok": bool, "error": str | None}:
    # 1. Tool name must be str
    # 2. Tool must be registered — on miss:
    #    P3-D: for SMALL+ try difflib.get_close_matches(cutoff=0.85)
    #    if single confident match → auto-correct (log warning)
    #    else → return structured {"ok": False, "error": "tool_not_found", "suggestions": [...]}
    # 3. bash: normalize whitespace, check DANGEROUS_PATTERNS
    # 4. write tool + path arg: verify path is inside working_dir via Path.is_relative_to()
```

---

## 5. Context Management & Compaction

### Compaction strategies

CodingAgent employs two distinct compaction strategies:

1. **LLM-based prose compaction (OP-2):** Manual or threshold-triggered. Uses an LLM turn to distill long history into a structured prose summary with Discoveries, Accomplished, Relevant Files, and Current State sections.
2. **Deterministic auto-compaction (CP-6):** Token-count triggered. Fires automatically when a threshold (default: 10,000 tokens) is reached. Unlike OP-2, this is deterministic and synchronous, mirroring claw code's `compact.rs`. It identifies the compactable portion of history (excluding recent messages and system prompt) and applies rule-based pruning or distillation without requiring an LLM call.

### Distiller (`src/core/memory/distiller.py`)

Context compaction for long-running tasks:

```python
def compact_messages_to_prose(messages: List[Dict], working_dir: str) -> str:
    """OP-2: structured compaction prompt with sections."""
    ...

def distill_context(
    messages: List[Dict],
    working_dir: str,
    max_tokens: int = 6000,
    keep_recent: int = 6,
) -> Dict[str, Any]:
    # 1. Estimate token count via tokenizer.count_messages_tokens()
    # 2. If below threshold → return unchanged
    # 3. Split: keep last `keep_recent` messages intact
    # 4. Call compact_messages_to_prose() on older messages
    # 5. Return {"compacted_history": [summary_msg, ...recent_msgs]}
```

**OP-2 compaction prompt structure:**

```
You are summarising a coding agent conversation.

## Goal
<task>

## Instructions
Write a structured summary with these sections:
- ## Discoveries (what was found)
- ## Accomplished (steps completed)
- ## Relevant Files (paths touched)
- ## Current State (where we are)

Max 700 words. Plain prose. No JSON.
```

**OP-8 marker:** Output prefixed with `[COMPACTED] <one-line-summary>\n`.

**SCAN-6 fix:** Singleton `ThreadPoolExecutor` with `shutdown(wait=False)` + `future.cancel()`
on timeout to avoid thread leaks.

**P2-3 checkpoint:** At ≥50 messages, write `.codingAgent/compaction_checkpoint.md`
so a crashed session can resume from the last known state.

### Overflow detection (OP-4)

In `perception_node.py`, overflow is detected against the raw context window
(not the budget-limited value):

```python
from src.core.inference.provider_context import get_actual_context_window

# OP-4: use raw window, not fraction-capped budget
context_window = get_actual_context_window()
# If token count approaches context_window → set errors=["context_overflow"]
# route_after_perception → memory_sync (OVF-3)
# OVF-2: set _compacted_history to last 6 messages
```

### Preserve flag (OP-10)

Messages with `metadata.preserve = True` are never pruned by `_prune_tool_outputs()`:

```python
def _prune_tool_outputs(messages, budget):
    for msg in messages:
        if msg.get("metadata", {}).get("preserve"):
            running_tokens += _est(msg)
            continue  # never prune this message
        # prune logic ...
```

### Per-tier context fractions (P3-F)

```python
# provider_context.py
_TIER_CONTEXT_FRACTION = {
    "nano": 0.50, "small": 0.60,
    "medium": 0.70, "large": 0.75, "frontier": 0.80,
}

def get_context_budget(fraction=0.65, min_tokens=6000, max_tokens=131072,
                       model_tier="") -> int:
    if model_tier:
        fraction = _TIER_CONTEXT_FRACTION.get(model_tier.lower(), fraction)
    return max(min_tokens, min(int(ctx_len * fraction), max_tokens))
```

### Comparison summary

| | CodingAgent | Claw Code |
|---|---|---|
| Trigger | `Orchestrator.compact_context()` + overflow detection | Auto post-turn threshold check |
| Token counting | tiktoken (`count_messages_tokens()`) | `len/4` heuristic |
| Compaction prompt | Structured sections (OP-2) | Generic summarise call |
| Marker | `[COMPACTED]` prefix (OP-8) | None |
| Preserve flag | `metadata.preserve = True` (OP-10) | `preserve_recent_messages: usize` |
| Tier fraction | Per-tier fractions (P3-F) | No tier concept |
| Checkpoint | `compaction_checkpoint.md` at ≥50 msgs | `SessionCompaction` in `.jsonl` |

---

## 6. Session & Memory Management

### Message Manager (`src/core/orchestration/message_manager.py`)

Handles conversation history, token counting, and persistence.
- **Session versioning (CP-14):** Includes a `version` field (currently 1) in saved session JSON to enable future migration paths.
- **Migration hooks:** `from_dict()` includes hooks for applying v1→v2 migration logic.

### Session store (`src/core/memory/session_store.py`)

SQLite backend, schema version 1:

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT,
    role TEXT,
    content TEXT,
    timestamp REAL
);
CREATE TABLE tool_calls (session_id, tool_name, args_json, result_json, timestamp);
CREATE TABLE errors     (session_id, error_type, error_message, context_json, timestamp);
CREATE TABLE plans      (session_id, plan_json, status, timestamp);
CREATE TABLE decisions  (session_id, decision, rationale, timestamp);
CREATE TABLE session_children (parent_id, child_id, role, task);
CREATE TABLE session_snapshots (session_id, snapshot_id, state_json, timestamp);
```

Thread-safe: `threading.local()` per-thread connections (SCAN2-5).

**Fork/revert** (`fork_session`, `revert_session`) — mirrors claw code's `SessionFork`.

### Agent session manager (`src/core/orchestration/agent_session_manager.py`)

Hydrates LangGraph state from session store on task start:

```python
class AgentSessionManager:
    def update_session_state(session_id, task, message_history,
                              current_plan, current_step,
                              provider, model, files_read, files_modified)
    def sync_agent_session_state(adapter)  # Pull live adapter state into session
```

### Rollback manager (`src/core/orchestration/rollback_manager.py`)

File-level snapshot + restore for task rollback:

```python
class RollbackManager:
    def take_snapshot(session_id, files: List[Path]) -> str  # snapshot_id
    def restore_snapshot(snapshot_id) -> None
    current_snapshot: Optional[str]
```

### Advanced memory (`src/core/memory/advanced_features.py`)

Vector store integration (S9-A):

```python
class AdvancedMemoryFeatures:
    def add_memory(text: str, metadata: Dict) -> None     # Embeds + stores
    def search_memory(query: str, limit=10) -> List[Dict] # Semantic search
    def inject_prior_session_memories(messages, working_dir, task) -> None
        # Queries vector store, injects <prior_context> block into system prompt on round 0
```

### Session lifecycle (`task_lifecycle.py`)

`start_new_task_impl(orch)` resets 20+ fields at task start:

```python
def start_new_task_impl(orch) -> str:
    orch._current_task_id = uuid[:8]
    orch.msg_mgr.messages = []
    orch._session_read_files = set()
    orch._session_modified_files = set()
    orch._execution_trace_buffer = []
    rollback_manager.current_snapshot = None
    orch._pending_delegations = []
    PreviewService.get_instance().pending_previews.clear()
    ContextBuilder.clear_cache()
    clear_repo_summary_cache(working_dir)
    # Delete stale TODO.md, TASK_STATE.md (cross-session contamination guard)
    # Apply OP-5 project tool overrides
    # Update AgentSessionManager
    # Reset plan_mode, session title, agent mode
```

---

## 7. Configuration System

### Config loader (`src/core/config_loader.py`)

4-layer hierarchical merge (later overrides earlier):

```python
def load_merged_config(working_dir=None) -> Dict[str, Any]:
    layers = [
        "src/config/providers.json",              # bundled defaults
        "~/.config/codingagent/config.json",      # user-level
        "<cwd>/.agent/config.json",               # workspace (committable)
        "<cwd>/.agent/config.local.json",         # local overrides (gitignored)
    ]
    return reduce(_deep_merge, [load(f) for f in layers if exists(f)])
```

### Provider configuration (`src/config/providers.json`)

JSON array (P1-7 atomic write via `tempfile.mkstemp` + `os.replace`):

```json
[
  {
    "name": "lm_studio",
    "type": "lm_studio",
    "base_url": "http://localhost:1234/v1",
    "models": ["gemma-4-26b-a4b-it"],
    "supports_native_tools": true,
    "small_model": null,
    "active": true
  },
  {
    "name": "GitHub Copilot",
    "type": "github_copilot",
    "models": ["gpt-4o", "claude-3.5-sonnet"],
    "small_model": "gpt-4o-mini",
    "active": false
  }
]
```

### Per-project settings (CP-13)

`.agent/settings.json` — loaded by `src/core/orchestration/project_settings.py`. This mirrors claw code's 5-layer config but adapted for CodingAgent's Python runtime.

Supported locations (merged in order):
1. `{workdir}/.agent/settings.json` — project settings (committable)
2. `{workdir}/.agent/settings.local.json` — local overrides (gitignored)

Recognized keys:
- `model`: Model override for the project.
- `permissionMode`: CP-8 unified permission mode.
- `hooks`: Shell hooks configuration (CP-7).
- `maxTurns`, `budgetCeiling`, `maxLlmWaitSeconds`.

### Shell Hooks (CP-7)

Located in `src/core/orchestration/shell_hooks.py`. Allows executing arbitrary shell commands before and after tool usage.

- **PreToolUse:** Can block/deny tool calls if the hook exits with code `2`.
- **PostToolUse:** Can modify tool results or mark them as errors.
- **Environment:** Hooks receive `HOOK_TOOL_NAME`, `HOOK_TOOL_INPUT`, `HOOK_TOOL_OUTPUT`, etc.
- **JSON Stdin:** Full tool context is piped as JSON to the hook's stdin.

### Project-context legacy (OP-5)

`.codingAgent/config.json` — mtime-cached, loaded by `load_project_config()`. This was the original project-level config mechanism before CP-13 implementation. It is still used for:
- `instructions` (merged with CP-11 walk).
- `deny_write_patterns` (used by guardrails.py).

### Config watcher (S6-C)

`ConfigWatcher` uses `watchfiles` (optional dep) to watch config paths and publish
`config.reloaded` events on change, enabling live config reload without restart.

### MCP config (S3-B)

```python
def get_mcp_config(working_dir=None) -> Dict    # {"servers": {...}}
def get_mcp_servers(working_dir=None) -> Dict   # server name → definition
```

---

## 8. MCP Integration

### MCP client (`src/core/mcp/mcp_client.py`)

Full JSON-RPC 2.0 stdio MCP client (TASK-21/S3-A):

```python
class McpStdioClient:
    def __init__(name: str, cmd: List[str])
    async def connect()                    # spawn subprocess, initialize handshake
    async def list_tools() -> List[McpToolDefinition]
    async def call_tool(name, args) -> McpToolResult
    async def disconnect()
    async def register_tools(registry: ToolRegistry) -> None  # merge into registry
```

Tool names prefixed `mcp_<server>_<tool>` to prevent collision.

### MCP server (`src/core/orchestration/mcp_stdio_server.py`)

Exposes orchestrator tools to Claude Code / IDEs (P4-2):

```python
# Handles: initialize, tools/list, tools/call
# Also: resources/list, resources/read, prompts/list, prompts/get,
#        sampling/create, completion/complete
```

### TUI slash commands (S3-C)

`/mcp list|add|status` in TUI `chat_input.py` + `app.py`.

---

## 9. Permission & Security System

### Bash security (`src/tools/bash_security.py`)

AST-level bash risk analysis wired into `file_tools.py` bash handler:

```python
class BashRiskLevel(str, Enum):
    SAFE = "safe"
    WORKSPACE_WRITE = "workspace_write"
    DANGEROUS = "dangerous"   # requires approval gate
    BLOCKED = "blocked"       # never execute

def analyze_bash_command(cmd: str) -> Tuple[BashRiskLevel, List[str]]:
    ...

# BLOCKED patterns (P4-3):
_BLOCKED_PATTERNS = [
    r"\$\(", r"`",              # command substitution
    r"<\(",                     # process substitution
    r"\|\s*bash",               # pipe to shell
    r"\bdd\b", r"\bmkfs\b",     # destructive disk ops
    r":\(\)\{.*\}",             # fork bomb
    r"/dev/null.*>",            # null wipe
    r"(?:env|export)\s+\w+=.*(?:bash|sh|python)\s+-c",  # env-var prefix + shell
]

# DANGEROUS patterns:
_DANGEROUS_PATTERNS = [
    r"\bsudo\b", r"\bsu\b",
    r"\bcurl\b", r"\bwget\b", r"\bnc\b",
    r"\bpip\s+install\b", r"\bnpm\s+install\b",
    r"\brm\s+-r", r"\brmdir\b",
    r"\bchmod\b", r"\bchown\b",
    r"\bssh\b", r"\brsync\b",
]
```

### DANGEROUS_PATTERNS in preflight

`tool_preflight.py` also maintains `_BASH_DANGEROUS_PATTERNS` (string-based after whitespace
normalisation via `re.sub(r"\s+", " ")`) as a defence-in-depth layer before bash_security's
AST analysis.

### Permission Mode System (CP-8)

Unified permission levels enforced at tool execution time:
- `read_only`: Only read-only tools allowed.
- `workspace_write`: Default. Allows modifying files in the workspace.
- `danger`: Allows dangerous operations (sudo, network, delete).
- `prompt`: Ask user for every tool call.
- `allow`: Full bypass (autonomous mode).

Enforcement occurs in `PermissionGateway` and `ToolExecutionService`, which check the tool's `required_permission` against the active `permission_mode` loaded from CLI flags or project settings.

### Guardrails (`src/tools/guardrails.py`)


- **Read-before-write guard:** Tracks `_session_read_files`; blocks write to a file that
  was never read (prevents blind overwrites).
- **deny_write_patterns:** `fnmatch`-based patterns from OP-5 project config; checked in
  `_check_project_deny_write()` wired into `write_file()`.

### Approval gate (`src/core/orchestration/approval_gate.py`)

`MODIFYING_TOOLS` allowlist. When a tool in this set is about to execute and `preview_mode`
or `autonomous_mode` is off → publish `tool.preview_requested` event → TUI prompts user.

### Path containment (preflight)

`write` side-effect tools: `path` arg must be `is_relative_to(working_dir)`.

### Plan mode (P4-4)

`PlanMode.enabled` blocks write tools until plan is approved:

```python
# execute_tool() in ToolExecutionService:
if orchestrator.plan_mode.enabled and not approved:
    if tool_name in WRITE_TOOLS:
        raise ToolError("Plan mode: write blocked pending approval")
```

---

## 10. Provider & Model Abstraction

### Provider detection via config

Unlike claw code's model-name-prefix detection, CodingAgent uses explicit config:

```python
# providers.json:
{"name": "lm_studio", "type": "lm_studio", "base_url": "...", "active": true}
```

`get_provider_manager()` reads `providers.json`, instantiates the adapter for the `"active"`
entry, wraps it in `AdapterWrapper` for a unified `generate()` interface.

### Model tiers (`src/core/inference/model_tiers.py`)

```python
class ModelTier(str, Enum):
    NANO     = "nano"      # ≤7B  — 8 tools, 4 plan steps, 15 max turns, YAML tools
    SMALL    = "small"     # 7–14B — 20 tools, 6 plan steps, 25 max turns, YAML tools
    MEDIUM   = "medium"    # 14–70B — 35 tools, 10 steps, 40 turns
    LARGE    = "large"     # >70B  — 50 tools, 16 steps, 60 turns
    FRONTIER = "frontier"  # Cloud — 60 tools, 20 steps, 80 turns

def classify_model(model_name: str, context_window: int = 0) -> ModelTier:
    # 1. Frontier patterns: gpt-4, claude-opus, gemini-ultra, ...
    # 2. Gemma 4 variants: e4b→NANO, a4b→SMALL, 27b→LARGE
    # 3. Parameter count extraction: "7b"→NANO, "13b"→SMALL, etc.
    # 4. Context-window heuristics: >100K → LARGE
    # 5. Fallback: MEDIUM

def get_tool_limit(tier: ModelTier) -> int         # 8/20/35/50/60
def supports_native_tools(tier: ModelTier) -> bool # MEDIUM+ → True
def is_simple_mode(tier: ModelTier) -> bool        # NANO → True
def get_plan_step_limit(tier: ModelTier) -> int    # 4/6/10/16/20
def get_max_turns(tier: ModelTier) -> int          # 15/25/40/60/80
```

`model_tier` set per-turn in `perception_node.py` and propagated through all nodes.

### Adapter hierarchy

```
OpenAICompatibleAdapter (base class, openai_compat_adapter.py)
├── LmStudioAdapter        — localhost, short-name model resolution
├── OllamaAdapter          — localhost
├── GroqAdapter            — cloud, OpenAI-compat
├── OpenRouterAdapter      — cloud, /models discovery
└── LiteLLMAdapter         — proxy

AnthropicAdapter           — prompt caching via cache_control ephemeral
GitHubCopilotAdapter       — OAuth device-flow auth
MockAdapter                — CI / unit tests (script-driven responses)
```

`AdapterWrapper` normalises legacy dict/choice/message-style responses into unified
`generate(messages, tools, ...)` → `GenerateResult`.

### Retry (P2-1)

`OpenAICompatibleAdapter._chat_internal()` retries on 429/500/502/503/504 and
`ConnectionError` with exponential backoff: 3 attempts, 1 s → 2 s sleep.

---

## 11. EventBus & Observability

### EventBus (`src/core/orchestration/event_bus.py`)

```python
class EventBus:
    def subscribe(event_name, callback) -> None
    def unsubscribe(event_name, callback) -> None
    def publish(event_name, payload, correlation_id=None) -> None
        # auto-stamps dict payloads with correlation_id

    def subscribe_to_agent(agent_id, callback) -> None
    def publish_to_agent(agent_id, payload) -> None

# Correlation ID — ContextVar propagated across async/thread boundaries
def new_correlation_id() -> str
def run_with_correlation(loop, executor, fn, *args)  # D-07
```

### Key published events

| Event | Publisher | Payload |
|---|---|---|
| `plan.requested` | `planning_node` | plan steps, task |
| `plan.approved` / `plan.rejected` | `approval_gate` | — |
| `plan.progress` | `execution_node` | step index, description |
| `tool.preview_requested` | `preview_service` | diff content |
| `tool.executed` | `tool_execution_service` | tool name, result |
| `context.compacted` | `orchestrator` | summary |
| `config.reloaded` | `ConfigWatcher` | changed keys |
| `git.status` | `task_lifecycle` | branch, files |
| `agent.started` / `agent.finished` | `orchestrator` | task, cost |
| `provider.active` | bootstrap | provider name, model |

### Telemetry

`src/core/inference/telemetry.py` — per-turn token usage flushed once per task
(PB-4 buffer flush), published to `usage.recorded` event.

Execution trace: `_execution_trace_buffer` flushed to
`.codingAgent/execution_trace.json` at task end (`flush_execution_trace()`).

---

## 12. TUI

**Framework:** Textual (Python async TUI)

**Location:** `tui/src/ui/`

### Key components

| File | Purpose |
|---|---|
| `core_bridge.py` | Async bridge between TUI and `Orchestrator` |
| `app.py` | Main Textual `App` class; slash commands; session management |
| `controller.py` | Event-driven controller; routes agent events to widgets |
| `coordinator.py` | Session + tool execution coordination |
| `chat_input.py` | Input widget; `/` slash-command dispatch |
| `history_input.py` | History navigation input |
| `events.py` | Custom Textual message types for agent communication |
| `widgets.py` | Base widget classes |
| `bus.py` | Event bus for TUI internal events |
| `settings.py` | TUI settings management |
| `config_writer.py` | Configuration file writer |

#### TUI Components (`tui/src/ui/components/`)

| Component | Purpose |
|---|---|
| `artifact.py` | Code artifact rendering |
| `bash_block.py` | Bash command output block with expand/collapse |
| `cards.py` | Card-based UI elements |
| `diff_viewer.py` | Side-by-side diff viewer |
| `file_picker.py` | File/directory picker |
| `inline_tool.py` | Inline tool rendering |
| `stream_view.py` | Streaming output view |
| `subagent_progress.py` | Subagent progress display |
| `thinking.py` | Thinking/reasoning display |
| `todo_list.py` | Interactive TODO list |
| `console.py` | Console output |
| `history_input.py` | History navigation |

#### TUI Screens (`tui/src/ui/screens/`)

| Screen | Purpose |
|---|---|
| `timeline.py` | Execution timeline (perception → planning → execution nodes) |
| `session_list.py` | Active sessions; fork/revert |
| `settings/screen.py` | Provider selection, API key management |
| `oauth/screen.py` | GitHub Copilot device flow |

### TUI slash commands

`/help`, `/compact`, `/clear`, `/cost`, `/fork`, `/diff`, `/fast` (toggle fast mode),
`/model` (switch model), `/mcp list|add|status`, `/resume <session_id>`,
`/plan approve|reject`.

### Streaming rendering

`file.diff.preview` event fires **before** `p.write_text()` — TUI renders the proposed
diff for user review before the write is committed (F14).

`plan.progress` events update a progress tracker widget as plan steps complete.

---

## 13. Test Architecture

```
tests/
├── unit/           (~330 tests, 15+ test files)
│   ├── test_model_tiers.py          (24 tests)
│   ├── test_s0_items.py             (29 tests)
│   ├── test_bash_security.py
│   ├── test_context_builder.py
│   ├── test_tool_registry.py
│   ├── test_audit_vol*.py           (regression batches per audit volume)
│   └── ...
├── integration/    (~25 tests)
│   ├── test_crud_live.py            (10 live LM Studio tests — RUN_INTEGRATION=1)
│   ├── test_mock_adapter_integration.py (17 tests, MockAdapter MA-01–MA-05)
│   └── test_lm_studio_live_pipeline.py  (7 live tests)
├── e2e/            (3 tests)
│   └── test_agent_scenarios.py      (6 scenario tests, mock LLM, no live provider)
└── benchmarks/
    └── test_pipeline_benchmarks.py  (7 latency benchmarks)
```

**Run command:** `.venv/bin/pytest -p no:logging` (suppresses TUI log noise).

**Test patterns:**
- Unit tests use `MockAdapter(responses=[...], strict=True)` (S0-C) — no live provider
- Integration tests require `RUN_INTEGRATION=1` env var + running LM Studio instance
- E2E tests use mock LLM with fixture-based responses
- Regression tests named `test_audit_vol<N>.py` guard every audit fix batch

**CI matrix:** `[macos-latest, ubuntu-latest]`, `fail-fast: false`, nightly schedule +
`workflow_dispatch` with `run_integration` input. Coverage uploaded via `--cov=src`.

---

## 14. Key Architectural Patterns

### 1. LangGraph state machine

AgentState TypedDict is the single source of truth. Nodes are pure(ish) async functions:
receive state, return mutated copy. LangGraph handles routing, checkpointing, and
append-only fields (`Annotated[List, operator.add]`).

### 2. Four-tier service extraction (D-10)

Long Orchestrator methods extracted into dedicated service classes (`session_cost_tracker.py`,
`preview_coordinator.py`, `tool_execution_service.py`) — each injected at bootstrap, tested
independently.

### 3. Defense-in-depth security

Multiple independent layers check the same threat:
1. `bash_security.py` AST analysis
2. `BASH_DANGEROUS_PATTERNS` string check in preflight
3. `guardrails.py` read-before-write
4. `approval_gate.py` human confirmation gate
5. `PlanMode` write-blocking
6. `preflight_check_impl` path containment

### 4. Two-tier prompt caching

Static SOUL/role/skills/tools cached by hash (changes rarely).
Dynamic env block cached by `(workdir, date, git_head)` (changes per commit).
Boundary marker `---DYNAMIC---` enables Anthropic prompt cache to cache the stable prefix.

### 5. PRSW (Parallel Read, Sequential Write)

`FileLockManager` coordinates reads as shared + writes as exclusive. Multi-step plans
can read in parallel; writes serialize. Prevents race conditions when multiple execution
waves run in parallel.

### 6. Per-task state reset

`start_new_task_impl()` resets 20+ fields, clears caches, deletes stale TODO.md/TASK_STATE.md,
clears PreviewService queue, resets compaction checkpoint. Prevents cross-task state bleed.

### 7. Correlation IDs via ContextVar

`new_correlation_id()` stamps each agent turn. `run_with_correlation()` propagates the
ContextVar into executor threads so EventBus, tool calls, and LLM calls all carry the
same ID for distributed tracing.

### 8. Tier-adaptive pipeline

`ModelTier` gates: tool count, prompt format (YAML vs JSON), plan step limit, max turns,
context fraction. The same graph runs NANO (7B, 8 tools, YAML) and FRONTIER (cloud,
60 tools, JSON, parallel calls) with no code changes — just config propagation.

### 9. Graceful degradation

All optional features (tiktoken, LSP, watchfiles, vector store backend, OTel) are `try/except`-wrapped.
Missing optional deps → fallback behaviour, not startup failure.

### 10. Atomic file writes

All whole-file JSON writes use atomic primitives to prevent readers from observing partial writes:

```
1. Try: atomic_write_json(target, obj, logger) — central helper in src/core/io_utils.py
2. Fallback: mkstemp → write → f.flush()+os.fsync() → os.replace
3. Final fallback: Path.write_text (only if mkstemp fails)
```

Files hardened: `llm_manager.py`, `subagent_tools.py`, `orchestrator_helpers.py`, `planning_node.py`, `dag_parser.py`, `user_prefs.py`, `rollback_manager.py`, `repo_indexer.py`, `advanced_features.py`, `state_tools.py`, `_file_io.py`, `repo_analysis_tools.py`.

See `docs/ATOMIC_WRITE_SUMMARY.md` for full details.

### 10. Audit-driven development

Each bug fix is tracked in `docs/audit/audit-report-vol<N>.md` with a code fix, test,
and a `test_audit_vol<N>.py` regression guard. Vol1–Vol28 covering 300+ individual fixes.

---

## 15. Comparison Reference: CodingAgent vs Claw Code

### Philosophical split

| | CodingAgent | Claw Code |
|---|---|---|
| Language | Python | Rust |
| Framework | LangGraph DAG | Custom turn loop |
| Model of trust | Pipeline scaffolds every decision | Model trusted to plan/retry inline |
| Complexity | High (16 nodes, ~100-field state) | Low (single `run_turn()`) |
| Safety philosophy | Defense-in-depth (5+ independent layers) | Permission policy + hooks |
| Memory model | SQLite + vector store | `.jsonl` append-only file |
| Provider selection | Config-driven (`providers.json`) | Model-name prefix detection |

### Pipeline depth

CodingAgent encodes software engineering domain knowledge in the graph itself:
- Analyst delegation before planning → better plans for complex tasks
- Separate plan validation → catches hallucinated tool names before execution
- Verification node → catches write failures immediately
- Evaluation node → semantic quality check, not just exit-code success
- Debug loop → automatic retry with tailored error prompts per language

Claw code trusts the model to reason about all of these inline.

### Tier adaptation — unique to CodingAgent

`ModelTier` is absent in claw code. CodingAgent adapts: tool count, prompt format,
plan step budget, max turn count, and context fraction all vary by tier. This enables
the same codebase to run on a 1.5B embedded model and GPT-4o with no code changes.

### Security depth — unique to CodingAgent

CodingAgent has five independent security layers (AST bash analysis, pattern preflight,
guardrails, approval gate, plan mode blocking) vs claw code's single permission policy.
`deny_write_patterns` (per-project) and read-before-write guard have no claw code
equivalent.

### Implemented Patterns (Parity Achieved)

CodingAgent has achieved functional parity with claw code for all major features:

| Feature | CodingAgent Implementation | Claw Code Parity |
|---|---|---|
| Auto-compaction | `auto_compactor.py` (CP-6) | `compact.rs` |
| Shell hooks | `shell_hooks.py` (CP-7) | `hooks.rs` |
| Permission policy | `PermissionMode` system (CP-8) | `permissions.rs` |
| Instruction discovery | Ancestor walk + SHA-256 dedup (CP-11) | `prompt.rs` |
| Project settings | `.agent/settings.json` (CP-13) | `config.rs` |
| Caching boundary | `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` | Boundary sentinel |
| Cache token tracking | `cache_creation_input_tokens` in adapters | Tracked in `usage.rs` |
| LSP injection | `get_lsp_diagnostics_block` in system prompt | `with_lsp_context` |
| Mid-turn messaging | `send_user_message` tool (CP-15) | `SendUserMessage` |
| Session versioning | `SCHEMA_VERSION` in MessageManager (CP-14) | `Session { version }` |

### Key remaining distinctions

1. **Language:** Python (CodingAgent) vs Rust (Claw Code).
2. **Framework:** LangGraph DAG (CodingAgent) vs Custom turn loop (Claw Code).
3. **Complexity:** CodingAgent encodes domain knowledge (Verification/Debug nodes) in the graph structure.
4. **Security:** CodingAgent uses AST-based bash analysis + 5 layers of defense-in-depth.
5. **Tier adaptation:** CodingAgent adapts its entire toolset and prompt format to the model tier (NANO → FRONTIER).

---

## 16. Audit History

| Volume | Date | Status |
|--------|------|--------|
| Vol28 | 2026-04-13 | 0 Critical, 0 High |
| Vol29 | 2026-04-14 | 0 Critical, 0 High |
| Vol30 | 2026-04-18 | 0 Critical, 0 High |
| Vol31 | 2026-04-20 | 0 Critical, 0 High |
| Vol32 | 2026-04-25 | 0 Critical, 0 High (full audit) |
| Vol33 | 2026-04-26 | 0 Critical, 0 High (re-audit) |

See `docs/audit/audit-report-vol33.md` for latest audit details.

---

## 17. Implementation Status

| Feature | Status |
|---------|--------|
| LangGraph pipeline | ✅ Complete |
| Multi-file atomic rollback | ✅ Complete |
| Advanced memory | ✅ Complete |
| Repository intelligence | ✅ Complete |
| PRSW (Parallel Reads, Sequential Writes) | ✅ Complete |
| DAG-based wave execution | ✅ Complete |
| Native Tool Support (frontier + local) | ✅ Complete |
| Role-Based Prompt Injection | ✅ Complete |
| Model tiers (NANO→FRONTIER) | ✅ Complete |
| MCP STDIO Server | ✅ Complete |
| Session fork/revert | ✅ Complete |
| Bubblewrap sandbox | ✅ Complete |
| Approval gate | ✅ Complete |
| Loop guards | ✅ Complete |
| --resume-session | ✅ Complete |
| Atomic file writes (mkstemp+replace) | ✅ Complete |
| Per-tool permission policy | ✅ Complete |
| /undo command | ✅ Complete |

### v2 Architecture (2026-04-26) — Local Model Optimization

| Feature | Status | Notes |
|---------|--------|-------|
| Two-axis profiling (model × hardware) | ✅ Complete | `src/core/inference/model_capability_profile.py` |
| Hardware profile detection (VRAM) | ✅ Complete | `src/core/inference/hardware_capability_profile.py` |
| Runtime profile merge | ✅ Complete | `src/core/inference/runtime_profile.py` |
| Binary workflow (SMALL/MEDIUM+) | ✅ Complete | `src/core/inference/workflow_selector.py` |
| Lite mode single-loop graph | ✅ Complete | `graph/builder.py` _compile_lite_graph() |
| KV cache governor | ✅ Complete | `src/core/inference/kv_cache_governor.py` |
| --thinking CLI flag | ✅ Complete | `src/main.py`, `src/core/inference/thinking_utils.py` |
| Qwen3 XML tool parser | ✅ Complete | `src/core/orchestration/tool_parser.py` |
| HuggingFace tokenizer | ✅ Complete | `src/core/inference/tokenizer.py` |
| CPU-aware LSP concurrency | ✅ Complete | `src/core/indexing/lsp_manager.py` |
| LRU embedding cache | ✅ Complete | `src/core/indexing/vector_store.py` |

### v2 Architecture Spec

See `docs/ARCHITECTURE_V2.md` for the target architecture.

**Key v2 changes:**
- Binary workflow (LITE/standard/full) instead of 5-tier system
- Single-loop ReAct++ for small models (≤14B params)
- VRAM-aware context limits
- AgentMode: LITE (≤14B), STANDARD (14-70B), FULL (cloud)

---

## 18. Key Constants

### Loop Guards
- `DOOM_LOOP_THRESHOLD = 3` — identical tool call detection
- `ALTERNATING_LOOP_THRESHOLD = 3` — alternating tool detection
- `COOLDOWN_GAP = 3` — tool cooldown between same-tool calls
- `_MAX_ROUNDS_PLANNING = 15` — max planning rounds
- `_AUTONOMOUS_MAX_TOOL_CALLS = 100` — max tool calls in autonomous mode

### Tool Limits
- `_BASH_STDOUT_MAX = 16_384` bytes
- `_BASH_STDERR_MAX = 6_000` bytes
- `_TOOL_OUTPUT_MAX_BYTES = 50_000` (frontier loop)

### Sandbox Levels
- `off` — no sandboxing
- `workspace` — bwrap with workspace write (default)
- `full` — bwrap + network disabled

### Model Tiers

| Tier | Params | Context | Tools | Max Steps | Max Turns |
|------|--------|---------|-------|-----------|-----------|
| NANO | ≤7B | ≤4K | 8 | 4 | 15 |
| SMALL | 7-14B | 4-16K | 20 | 6 | 25 |
| MEDIUM | 14-70B | 16-128K | 35 | 10 | 40 |
| LARGE | >70B | >128K | 50 | 16 | 60 |
| FRONTIER | Cloud | >200K | 60 | 20 | 80 |

---

## 19. Environment Variables

| Variable | Purpose |
|----------|---------|
| `CODINGAGENT_SANDBOX_LEVEL` | Sandbox level (off/workspace/full) |
| `CODINGAGENT_TRUSTED` | Trust mode (skip approval) |
| `CODING_AGENT_HTTP_SERVER` | Start HTTP server |
| `CODINGAGENT_MODEL` | Default model |
| `CODINGAGENT_PROVIDER` | Default provider |
| `CODING_AGENT_SCHEDULER_HEARTBEAT` | Scheduler heartbeat interval |
| `CODING_AGENT_DISTILL_INTERVAL` | Default distill interval |
| `CODING_AGENT_ADMIN_TOKEN` | Admin HTTP endpoint token |

---

## 20. Configuration Files

| File | Purpose |
|------|---------|
| `.codingAgent/config.json` | Project settings |
| `.codingAgent/sessions.db` | SQLite session store |
| `.codingAgent/memory/` | Vector store |
| `providers.json` | LLM provider config |
| `toolsets/*.yaml` | Toolset definitions |
| `config/agent-brain/roles/*.md` | Role prompts |

---

*Document generated 2026-04-26. Source: `/Users/tann200/PycharmProjects/CodingAgent`.*
*Companion document: `docs/claw-code-architecture.md`.*
