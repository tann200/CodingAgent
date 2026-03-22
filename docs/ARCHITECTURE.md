# CodingAgent Architecture

> **Implementation Status**: Fully implemented — LangGraph 13-node pipeline, multi-file atomic rollback, advanced memory wired, repository intelligence, 1040+ unit tests
> **Recent Updates (2026-03)**: Analyst delegation node added (vol9); tool cooldown enforcement + files_read O(1) dict in execution_node; plan_validator now routes to planning (not perception) on failure; delegation results injected into conversation history (C4); prompt injection guard in perception_node (F8); ThreadPoolExecutor-based tool timeouts (C1); thinking-token stripping for Qwen3/DeepSeek-R1; multi-language SymbolGraph (JS/TS/Go/Rust/Java); telemetry log rotation
> **Audit Fixes (2026-03)**: 9 audit cycles completed (vol1–vol5). All Critical and High severity findings resolved. See Security Audit Fixes section.

## Implementation Stages

| Stage | Status | Components |
|-------|--------|------------|
| Stage 1 - MVP Stabilization | ✅ Complete | Toolsets, Session Store |
| Stage 2 - Cognitive Agent | ✅ Complete | Symbol Graph, Sandbox, Self-Debug |
| Stage 3 - Advanced SWE | ✅ Complete | Trajectories, Dreams, Refactor, Review, Skills |
| Stage 4 - LangGraph Pipeline | ✅ Complete | Analysis, Replan, Evaluation nodes |
| Stage 5 - EventBus Dashboard | ✅ Complete | Real-time UI updates via events |
| Stage 6 - Security Hardening | ✅ Complete | Bash allowlist, sandbox fail-closed, symlink protection |
| Stage 7 - Fast-Path Routing | ✅ Complete | Conditional routing for simple tasks |
| Stage 8 - Core Stabilization | ✅ Complete | Context builder fix, robust plan parsing, WorkspaceGuard |
| Stage 9 - Incremental Indexing | ✅ Complete | SHA256 hash-based change detection, multi-language (15+) |
| Stage 10 - Repository Intelligence | ✅ Complete | ContextController wired, VectorStore fix, SymbolGraph enrichment |
| Stage 11 - Wiring Sprint | ✅ Complete | SkillLearner, SessionStore, plan validator defaults wired |
| Stage 12 - Multi-file Atomicity | ✅ Complete | Step transactions: begin/append/rollback via RollbackManager |
| Stage 13 - Deterministic Mode | ✅ Complete | temperature=0, seed param, ScenarioEvaluator for regression tests |
| Stage 14 - Test Coverage | ✅ Complete | 470+ unit tests across 16 test files |
| Stage 15 - Thread Safety | ✅ Complete | Signal-based timeout guarded by main-thread check |
| Stage 16 - Delegation & Parallel Memory | ✅ Complete | Delegation node for subagent spawning, parallel memory ops, auto-save methods |
| Stage 17 - Security Audit Fixes | ✅ Complete | All Critical and High severity audit findings resolved |
| Stage 18 - Multi-Cycle Hardening | ✅ Complete | Audit vol2–vol5: 100+ fixes across loop safety, tool reliability, TUI race conditions, prompt injection |
| Stage 19 - Analyst Delegation | ✅ Complete | `analyst_delegation_node` gates complex tasks; findings injected into planning (vol9) |
| Stage 20 - Tool Cooldown & Read Tracking | ✅ Complete | `tool_last_used` cooldown map, `files_read` O(1) dict, cooldown gap enforcement in execution_node |
| Stage 21 - Thinking-Token Optimization | ✅ Complete | `thinking_utils.py`: strip `<think>` blocks, model-aware max_tokens budget, /no_think injection |

---

## Security & Stability Audit Fixes (vol1–vol5, 2026-03)

Nine audit cycles completed. All Critical and High severity findings are resolved. Full reports: `docs/audit/audit-report.md`, `audit-report-vol2.md` … `audit-report-vol5.md`.

### Critical Fixes (selected)

| Finding | Fix | Location |
|---------|-----|----------|
| C1 (vol5) — Tool timeout no-op in TUI | `ThreadPoolExecutor` + `future.result(timeout=n)` replaces `signal.SIGALRM` | `orchestrator.py` |
| C2 (vol5) — Sandbox validates old file | `ast.parse(new_content)` validates new content directly | `orchestrator.py` |
| C3 (vol5) — analysis fast-path nullifies W3 | `_task_is_complex()` gate added; fast-path skipped for complex tasks | `analysis_node.py` |
| C4 (vol5) — Delegation results write-only | Results injected as system messages into `history` | `delegation_node.py` |
| C5 (vol5) — EventBus double delivery | Dedup via `called` set in `publish_to_agent` | `event_bus.py` |
| delegation loop (vol1) | `delegation → END` direct edge; removed `memory_sync` routing | `graph/builder.py` |
| debug_node unreachable (vol1) | `evaluation_node` returns `"debug"` on failure; edge wired | `graph/builder.py` |
| plan_validator infinite loop (vol1) | `enforce_warnings=False` default; round≥8 cap | `plan_validator_node.py` |
| debug_node missing await (vol2) | `resp = await call_model(...)` | `debug_node.py` |
| debug_attempts double-increment (vol2) | Removed `+1` from `evaluation_node`; `debug_node` owns counter | `evaluation_node.py` |
| orchestrator loop (vol13) | `handled` check now matches `"tool_execution_result"` in content | `orchestrator.py` |

### High-Risk Fixes (selected)

| Finding | Fix |
|---------|-----|
| H1 (vol5) — sed -i position-independent detection | `startswith("-i")` + bundled-flag scan (`-ni`, `-rni`, `--in-place=`) |
| H2 (vol5) — Prompt injection via tool result | F8: perception_node rejects tool blocks matching user-role history |
| H3 (vol5) — Concurrent send_prompt() | `_agent_lock` mutex + `_agent_running` flag; input disabled while running |
| H4 (vol5) — plan_validator → perception waste | F10: routes directly to `planning` on failure (saves 2 LLM calls) |
| H6 (vol5) — Dead state fields | `tool_last_used` and `files_read` re-added with active functionality |
| H9 (vol5) — debug_attempts reset per round | `debug_attempts`, `total_debug_attempts`, `step_retry_counts` propagated across rounds |
| NEW-1 (vol2) — debug_node missing await | Fixed; entire debug/fix loop was silently broken |
| NEW-6 (vol2) — perception decomposition resets rounds | Returns `rounds + 1` instead of `0` |
| F1 (vol3) — execution_node extra LLM call | Uses `planned_action` directly; skips LLM call when action pre-set |
| F8 (vol3) — `_INDEXED_DIRS` stale cache | Keyed by `(resolved_path, mtime_ns)` tuple |
| F15 (vol3) — `_TEXT_CACHE` LRU eviction | Max 256 entries; module-level static dict |

### AgentState Fields Added (vol2–vol5)

| Field | Type | Purpose |
|-------|------|---------|
| `original_task` | `Optional[str]` | Task before step-level decomposition |
| `step_description` | `Optional[str]` | Current step hint from step_controller |
| `planned_action` | `Optional[Dict]` | Pre-set tool action from planning |
| `plan_validation` | `Optional[Dict]` | Result dict from plan_validator_node |
| `plan_enforce_warnings` | `Optional[bool]` | External override for plan validator |
| `plan_strict_mode` | `Optional[bool]` | External override for plan validator |
| `task_history` | `Optional[List]` | State snapshot history for rollback |
| `step_retry_counts` | `Optional[Dict[str, int]]` | Per-step retry counter |
| `tool_last_used` | `Optional[Dict[str, int]]` | Cooldown map: `"tool:path"` → count at last call |
| `files_read` | `Optional[Dict[str, bool]]` | O(1) read-before-edit lookup: resolved path → True |
| `analyst_findings` | `Optional[str]` | Analyst subagent output injected into planning |
| `plan_resumed` | `Optional[bool]` | Set when stale plan is resumed from `last_plan.json` |

---

## Pipeline Overview

```
Fast-Path (simple 1-step task):
  perception → execution → verification → evaluation → (memory_sync | delegation | END)

Full Pipeline (complex multi-step task):
  perception → analysis → [analyst_delegation →] planning → plan_validator
            → execution → step_controller → verification → evaluation
            → (memory_sync | delegation | step_controller | END)

Plan validation failure (F10 fix):
  plan_validator → planning  (direct re-planning, saves 2 LLM calls vs old → perception path)

Patch-too-large path:
  execution → replan → step_controller → execution (smaller step)

Verification failure / debug path:
  verification → evaluation → debug → (execution | END)
```

**Conditional Routing (Fast-Path / W3):**
`route_after_perception` checks `next_action` and task complexity:
- Tool call ready + **simple task** → skip to `execution` (fast-path)
- Tool call ready + **complex task** (refactor/rewrite/multi-step keyword, >3 relevant files, or 2+ step plan) → force through `analysis` (W3 fix)
- No tool call → `analysis` (full pipeline)

`should_after_analysis` checks task complexity:
- Complex task → `analyst_delegation` → `planning`
- Simple task → `planning` directly

**Node Role Mapping:**

| Node | Role | LLM Calls | Notes |
|------|------|-----------|-------|
| `perception_node` | `operational` | ✅ Yes | Task parsing, tool call generation; F8 prompt injection guard rejects reflected YAML tool blocks from user-role history |
| `planning_node` | `strategic` | ✅ Yes | Structured JSON plan via LLM; `max_tokens=3000`; fallback plan on parse failure; injects `analyst_findings` when present |
| `debug_node` | `debugger` | ✅ Yes | Error analysis and fix generation; resets counter on error-type change |
| `replan_node` | `strategic` | ✅ Yes | Step splitting for oversized patches (>200 lines) |
| `analysis_node` | N/A | ❌ Tool-based | VectorStore + SymbolGraph + ContextController; fast-path bypassed for complex tasks (C3 fix); `_INDEXED_DIRS` keyed by `(path, mtime_ns)` |
| `analyst_delegation_node` | `analyst` | ✅ Yes | Spawned for complex tasks only; injects `<findings>` into `analyst_findings`; result feeds `planning_node` |
| `execution_node` | `operational` | ⚠️ Optional | Uses `planned_action` when set (F1); enforces read-before-edit via `files_read` O(1) dict + `verified_reads` fallback; tool cooldown via `tool_last_used` (COOLDOWN_GAP=3); LLM call only if no pre-set action |
| `verification_node` | N/A | ❌ Tool-based | pytest/ruff/tsc/jest — deterministic; rollback on failure |
| `evaluation_node` | N/A | ❌ State-based | Routes to `debug` on failure (bounded by `debug_attempts`); never routes directly to step_controller on failure |
| `step_controller_node` | N/A | ❌ State-based | Step gating; failed step retries via `execution` not `verification` |
| `plan_validator_node` | N/A | ❌ State-based | Plan structure validation; on failure routes to `planning` (F10 fix — saves 2 LLM calls); emergency round≥8 guard forces execution |
| `delegation_node` | N/A | ❌ Spawns subagents | C4 fix: results injected into conversation history (not write-only); `delegation_results` also kept in state for backward compat |
| `memory_update_node` | N/A | ❌ Tool-based | Distillation + parallel memory ops via `asyncio.gather()` |

**Subagent Roles** (via `delegate_task` tool):

| Role | Canonical Name | Best For |
|------|---------------|----------|
| `analyst` (alias: `researcher`) | `analyst` | Deep repo exploration before planning |
| `operational` (alias: `coder`) | `operational` | Isolated code implementation |
| `strategic` (alias: `planner`) | `strategic` | Subtask decomposition |
| `reviewer` | `reviewer` | Post-execution QA, code review |
| `debugger` | `debugger` | Root-cause analysis in isolation |

---

## High-Level Flow

1. **Entry Points**
   - `src/main.py` — delegates to `CodingAgentApp`; CLI entry point.
   - `src/ui/app.py` — `CodingAgentApp` wires EventBus, Orchestrator, and ProviderManager; chooses Textual vs headless mode.
   - `src/ui/textual_app_impl.py` — real Textual TUI implementation with sidebar, chat panel, and input box; thread-based agent dispatch.
   - `src/ui/textual_app.py` — minimal placeholder `TextualApp` shim; no-op in headless environments.

2. **Orchestrator** (`src/core/orchestration/orchestrator.py`)
   Central runtime. Handles:
   - LLM connection via `ProviderManager` (`llm_manager.py`).
   - System prompt bootstrap via `AgentBrainManager`.
   - Message history + token windowing via `MessageManager`.
   - Primary action loop (`run_agent_once`): compiles and invokes the LangGraph pipeline.
   - Tool execution via `execute_tool` with preflight, snapshots, signal-safe timeout, contract validation, and loop detection.
   - Role-based tool filtering; multi-file step transactions via `RollbackManager`.
   - **Thread-safe timeout**: `execute_tool` uses `concurrent.futures.ThreadPoolExecutor` + `future.result(timeout=n)` — works from any thread including the TUI daemon thread. (C1: old `signal.SIGALRM` approach was a no-op in non-main threads.)

3. **LangGraph Cognitive Pipeline** (`src/core/orchestration/graph/`)

   **Graph Builder** (`graph/builder.py`): compiles the LangGraph `StateGraph`, wires all nodes, defines conditional edges and routing functions. `compile_agent_graph()` is the entry point.

   **Graph State** (`graph/state.py`): `AgentState` TypedDict — the shared immutable state passed between all nodes.

   **Node files** (`graph/nodes/`):

   | File | Node | Description |
   |------|------|-------------|
   | `perception_node.py` | `perception_node` | Understands the user request, decomposes tasks, extracts tool calls from YAML. Uses `operational` role. Injects `context_hygiene` skill for debug/search tasks. F8 prompt injection guard: rejects tool blocks whose `name:` appears verbatim in any user-role history message. |
   | `analysis_node.py` | `analysis_node` | Explores the repository before planning via tool calls (no LLM). Three phases: (1) VectorStore semantic search, (2) SymbolGraph call graph enrichment, (3) ContextController token budget enforcement. Runs `repo_summary()` at start. Fast-path bypass suppressed for complex tasks (C3). `_INDEXED_DIRS` keyed by `(path, mtime_ns)` to avoid stale cache across sessions (F15). |
   | `analyst_delegation_node.py` | `analyst_delegation_node` | Spawned for complex tasks only (vol9 #56). Delegates deep repo analysis to `analyst` subagent. Stores `<findings>` in `state["analyst_findings"]`; `planning_node` injects findings into its LLM prompt. |
   | `planning_node.py` | `planning_node` | Converts perception/analysis outputs into a structured step-by-step plan via LLM. `max_tokens=3000` (P5). Injects `analyst_findings` when present. Guaranteed fallback plan on parse failure (F7). Cross-session plan persistence via `last_plan.json`. Uses `strategic` role. |
   | `execution_node.py` | `execution_node` | Executes plan steps via tool calls. Uses `planned_action` when set (F1 — eliminates extra LLM call per step). Read-before-edit enforced via `files_read` O(1) dict + `verified_reads` list + `_session_read_files`. Tool cooldown: `tool_last_used` map blocks repeated identical read-tool calls within `COOLDOWN_GAP=3` executions. Calls `begin_step_transaction()`, dispatches tools, advances plan state. Intercepts `requires_split` flag for replan. |
   | `verification_node.py` | `verification_node` | Runs tests, linter, and syntax checks via tool calls. On failure calls `rollback_step_transaction()` to atomically restore all files written in the step. |
   | `evaluation_node.py` | `evaluation_node` | Post-verification routing. Routes to `debug` on verification failure (bounded by `debug_attempts < max_debug_attempts`), `memory_sync` on completion, or `step_controller` for more steps. |
   | `debug_node.py` | `debug_node` | Analyses verification failures and attempts fixes via LLM. Resets counter when `error_type` changes. On exhaustion calls `rollback_manager.rollback()`. Uses `debugger` role. |
   | `replan_node.py` | `replan_node` | Splits oversized patches (>200 lines) into 2–3 smaller targeted steps via LLM. Uses `strategic` role. Deep-copies step dicts to prevent aliasing bugs. |
   | `step_controller_node.py` | `step_controller_node` | Enforces single-step execution from the plan; gates next-step dispatch. Failed steps retry via `execution` (not `verification`). |
   | `delegation_node.py` | `delegation_node` | Spawns subagents for independent parallel tasks. Reads `state["delegations"]`, uses `asyncio.gather()`. C4 fix: results injected as system messages into `history` so downstream nodes can read them; `delegation_results` also stored in state. |
   | `memory_update_node.py` | `memory_update_node` | Persists distilled context to `.agent-context/TASK_STATE.md`. Parallelizes all memory operations via `asyncio.gather()`: TrajectoryLogger, DreamConsolidator, ReviewAgent, RefactoringAgent. |
   | `plan_validator_node.py` | `validate_plan()` | Validates plan structure before execution: step count, file references, verification step (strict mode). On failure routes to `planning` directly (F10 — saves 2 LLM calls). Emergency round≥8 guard forces execution to break infinite loops. |
   | `node_utils.py` | — | Shared utilities: `_resolve_orchestrator()` (robust config/state lookup), `_notify_provider_limit()` (UI event for provider errors). |
   | `workflow_nodes.py` | — | Re-export shim for backward compatibility — imports all nodes and re-exports them from one place. |

4. **Message & Token Manager** (`src/core/orchestration/message_manager.py`)
   Tracks conversation history. Auto-drops oldest non-system messages when the window exceeds `max_tokens` (sliding window). System prompt is always preserved.

5. **AgentBrainManager** (`src/core/orchestration/agent_brain.py`)
   In-memory cache for agent-brain configuration files. Key methods:
   - `get_identity(name)` — SOUL or LAWS content
   - `get_role(role_name)` — role prompt (strategic, operational, analyst, debugger, reviewer)
   - `get_skill(skill_name)` — skill content (dry, context_hygiene)
   - `compile_system_prompt(role_name)` — full system prompt with role + SOUL + LAWS

   Config loaded from `src/config/agent-brain/`:
   - `identity/LAWS.md`, `identity/SOUL.md` — immutable core
   - `roles/` — strategic, operational, analyst, debugger, reviewer
   - `skills/` — dry, context_hygiene (auto-created skills from SkillLearner go here too)

6. **ContextBuilder** (`src/core/context/context_builder.py`)
   Hierarchical prompt assembly with token budgeting:
   - Quotas: Identity (12%), Role (12%), Tools (6%), Conversation (remaining)
   - Drop order on overflow: conversation first, identity/role never dropped
   - Truncation marker: `\n\n[TRUNCATED]`
   - Injects YAML output format block and `active_skills` list

7. **ContextController** (`src/core/context/context_controller.py`)
   Token budget enforcement for repository context:
   - `prioritize_files()` — assigns relevance scores
   - `enforce_budget()` — trims to token limit
   - `get_relevant_snippets()` — extracts key lines
   - Wired in `analysis_node` Phase 3

8. **Tool Parser** (`src/core/orchestration/tool_parser.py`)
   Parses YAML tool blocks from model output. Supports:
   ```yaml
   name: edit_file
   arguments:
     path: src/main.py
     patch: "..."
   ```
   Also supports compact format (`tool_name:\n  arg: val`), `<think>` block stripping (LMStudio), and inline YAML. **XML `<tool>` tags are not supported.**

9. **GraphFactory** (`src/core/orchestration/graph_factory.py`)
   Hub-and-spoke dynamic graph composition. Creates role-specific graphs:
   - `create_planner_graph()` — planning-focused workflow
   - `create_coder_graph()` — execution-focused workflow
   - `create_reviewer_graph()` — verification-focused workflow
   - `create_researcher_graph()` — search-focused workflow
   Used by `subagent_tools.py` to spawn isolated subagents.

---

## Tools

### Tool Registry (`src/tools/registry.py`)
Central registry of named tools. Tools are small functions registered with metadata (description, side_effects). Converted to a YAML `<available_tools>` block injected into the system prompt.

### File Operations (`src/tools/file_tools.py`)
- `list_files` / `fs.list` — directory listing
- `read_file` / `fs.read` — read file content
- `read_file_chunk` — read a byte range of a file
- `write_file` / `fs.write` — write file (tiered bash allowlist enforced)
- `edit_file` — apply a patch/diff to a file
- `edit_file_atomic` — replace an exact unique string in a file (like Claude Code's `Edit` tool)
- `edit_by_line_range` — replace specific line ranges
- `delete_file` — delete a file
- `glob` — file pattern matching; rejects `..` traversal patterns (F13); all returned paths are relative to workdir
- `batched_file_read` — read multiple files efficiently
- `multi_file_summary` — get file metadata without full reads
- `bash` — shell execution with allowlist; `sed -i` blocked in all forms (`-ni`, `--in-place=`); `DANGEROUS_PATTERNS` applied once, whitespace-normalised (F12)

**Security:** Tiered bash allowlist (Tier 1: safe read-only, Tier 2: test/compile, Tier 3: restricted with `requires_approval`). Shell operators blocked pre-parse. `safe_resolve()` shared utility for all path validation. `sed -i` bundled-flag detection handles `-ni`, `-rni`, `--in-place[=...]`.

### TODO Tools (`src/tools/todo_tools.py`)
- `manage_todo(action, workdir, steps, step_id, description)` — manage `TODO.md` task tracker
- Actions: `create` (write plan), `check` (mark step done), `update` (modify step), `read` (return current state), `clear`
- `planning_node` writes `TODO.md` on plan creation; `execution_node` checks off completed steps
- `ContextBuilder` injects `TODO.md` as `<task_progress>` into the system prompt

### Search Tools (`src/tools/system_tools.py`)
- `grep(pattern, path)` — regex pattern search; uses system `grep` with pure-Python fallback. Constrained to workdir.

### Code Intelligence (`src/tools/repo_tools.py`)
- `initialize_repo_intelligence(workdir)` — indexes repo to `.agent-context/repo_index.json` + LanceDB vector store
- `search_code(query, workdir)` — semantic search over codebase via VectorStore
- `find_symbol(name)`, `find_references(name)` — symbol lookup via SymbolGraph

### Repository Analysis (`src/tools/repo_analysis_tools.py`)
- `analyze_repository(workdir)` — scans Python files, extracts module summaries and import relationships, writes `.agent-context/repo_memory.json`

### Repository Summary (`src/tools/repo_summary.py`)
- `repo_summary(workdir)` — fast overview of project structure; detects framework (FastAPI, Flask, React, etc.) and generates a tree overview. Used by `analysis_node` at startup.

### Symbol Reader (`src/tools/symbol_reader.py`)
- `SymbolReader.parse_symbols(file_path)` — AST-based extraction of function/class/method locations
- `read_symbol(file_path, symbol_name)` — read only the lines of a specific function or class (avoids loading full files)

### Verification Tools (`src/tools/verification_tools.py`)
- `run_tests(workdir, test_files)` — runs pytest with structured output (passed/failed counts, tracebacks)
- `run_linter(workdir)` — runs ruff/flake8
- `syntax_check(workdir)` — quick `py_compile` across repo
- `run_js_tests(workdir)` — auto-detects jest/vitest/mocha from `package.json` and runs them via npx
- `run_ts_check(workdir)` — TypeScript type-check via `tsc --noEmit`
- `run_eslint(workdir, paths)` — ESLint with compact output parsing

**Auto-detection:** `verification_node` checks for `package.json` at startup. JS/TS projects automatically use the JS test suite instead of pytest/ruff.

### State Tools (`src/tools/state_tools.py`)
- `create_state_checkpoint(...)` — saves agent state snapshot to `.agent-context/checkpoints/`
- `list_checkpoints(workdir)` — lists available checkpoints
- `restore_state_checkpoint(checkpoint_id, workdir)` — restores a checkpoint
- `diff_state(id1, id2, workdir)` — compares two checkpoints

### Patch Tools (`src/tools/patch_tools.py`)
- `generate_patch(path, new_content, workdir)` — generates unified diff between file and new content
- `apply_patch(path, patch, workdir)` — applies a unified diff patch

### Role Tools (`src/tools/role_tools.py`)
- `get_role()` — returns current in-memory role
- `set_role(role, orchestrator)` — sets role on in-memory holder and optionally on the orchestrator; publishes `role.change` event

### Memory Tools (`src/core/memory/memory_tools.py`)
- `memory_search(query, workdir)` — searches `TASK_STATE.md` and `execution_trace.json`; returns ranked matches (exact lines first, then trace entries by recency)

### Subagent Tools (`src/tools/subagent_tools.py`)
- `delegate_task(role, subtask_description, working_dir)` — spawns an isolated autonomous subagent via `GraphFactory` for a specific subtask, keeping the main agent's context window clean

### Git Tools
- `get_git_diff(workdir)` — returns `git diff` output for tracking changes

### Echo Tool
- `echo(message)` — test/debug echo tool

### Toolset Loader (`src/tools/toolsets/loader.py`)
Loads YAML toolset files; checks `src/config/toolsets/` first, falls back to `src/tools/toolsets/` for backward compatibility. Caches loaded toolsets.

**Toolsets** (`src/config/toolsets/` and `src/tools/toolsets/`):
- `coding.yaml`, `debug.yaml`, `review.yaml`, `planning.yaml`

---

## Inference Layer (`src/core/inference/`)

| File | Description |
|------|-------------|
| `llm_manager.py` | `ProviderManager` — provider registry, model discovery, `call_model()`, routing helpers. Singleton via `get_provider_manager()`. |
| `llm_client.py` | Abstract `LLMClient` base class — defines `generate()` and `agenerate()` interface. |
| `adapter_wrappers.py` | `AdapterWrapper` — wraps existing adapters into a uniform `generate()` API; normalizes model lists. |
| `adapters/lm_studio_adapter.py` | LM Studio HTTP adapter — calls `/v1/chat/completions`; minimal deps, test-friendly. |
| `adapters/ollama_adapter.py` | Ollama HTTP adapter — calls Ollama REST API; delegates config helpers to `llm_manager`. |
| `telemetry.py` | `publish_model_response()` — emits model response telemetry (tokens, latency) to EventBus. `with_telemetry()` decorator wraps adapter calls. |
| `thinking_utils.py` | Model-agnostic thinking-token utilities: `is_reasoning_model()`, `supports_no_think()`, `strip_thinking()` (strips `<think>…</think>`), `budget_max_tokens()` (doubles budget for DeepSeek-R1), `get_active_model_id()`. Applied in `distiller.py` and `perception_node.py`. |
| `provider_context.py` | `get_context_budget()` — dynamic token budget based on active provider's context window. |

**ModelRouter**: Predicts payload complexity to toggle between small/fast (7B-9B) vs larger (32B-70B) models based on task characteristics.

---

## Memory System

### Working Memory
`MessageManager` (`src/core/orchestration/message_manager.py`) — in-memory conversation history with sliding token window.

### Context Distiller (`src/core/memory/distiller.py`)
LLM-based summarization of conversation history every 5 steps → `.agent-context/TASK_STATE.md`. Output keys: `current_task`, `completed_steps`, `next_step`. Fallback on LLM failure.

### Session Store (`src/core/memory/session_store.py`)
SQLite-based persistence to `.agent-context/session.db`. Tables: `messages`, `tool_calls`, `errors`, `plans`, `decisions`. Wired in `orchestrator.execute_tool` (tool calls), `planning_node` (plans), `debug_node` (errors).

### Advanced Features (`src/core/memory/advanced_features.py`)
- `TrajectoryLogger` — stores successful run logs to `.agent-context/trajectories/`
- `DreamConsolidator` — background memory consolidation
- `RefactoringAgent` — code smell detection and suggestions; `save_smells()` writes to `.agent-context/code_smells.json`
- `ReviewAgent` — patch review and feedback; `save_review()` writes to `.agent-context/last_review.json`
- `SkillLearner` — auto-creates skill files in `src/config/agent-brain/skills/` from successful ≥2-tool tasks

**Parallel Memory Operations:**
`memory_update_node` runs all memory operations concurrently via `asyncio.gather()`:
- TrajectoryLogger (file I/O)
- DreamConsolidator (background consolidation)
- ReviewAgent (async via ThreadPoolExecutor)
- RefactoringAgent (parallel file analysis)

### Episodic Memory files
| Path | Description |
|------|-------------|
| `.agent-context/TASK_STATE.md` | Distilled task summary |
| `.agent-context/execution_trace.json` | Tool call log for loop prevention |
| `.agent-context/usage.json` | Token/latency/tool call cost tracking |
| `.agent-context/checkpoints/` | State checkpoints (state_tools.py) |
| `.agent-context/session.db` | SQLite: tool calls, plans, errors, decisions |
| `.agent-context/snapshots/` | Pre-edit file snapshots (RollbackManager) |
| `.agent-context/trajectories/` | Successful run logs (TrajectoryLogger) |
| `.agent-context/repo_index.json` | Repository symbol index |
| `.agent-context/repo_memory.json` | Module summaries + dependency graph |
| `.agent-context/last_plan.json` | Cross-session plan persistence |
| `src/config/agent-brain/skills/` | Auto-created skill files (SkillLearner) |

---

## Repository Intelligence (`src/core/indexing/`)

| File | Description |
|------|-------------|
| `repo_indexer.py` | Multi-language regex parser (15+ languages). SHA256 hash-based incremental indexing. Saves metadata to `repo_index_meta.json`. Version 3.0. |
| `vector_store.py` | LanceDB-based semantic search using sentence-transformers. `vs.search(query, limit=N)`. |
| `symbol_graph.py` | AST-based call graph. `update_file(path)` for incremental updates; `find_calls()`, `find_tests_for_module()`. Wired in `analysis_node` Phase 2. |

---

## Orchestration Support (`src/core/orchestration/`)

| File | Description |
|------|-------------|
| `orchestrator.py` | Central runtime — action loop, tool execution, preflight, loop prevention, signal-safe timeout, multi-file transactions. |
| `agent_brain.py` | In-memory cache for agent-brain config files (roles, skills, identity). |
| `message_manager.py` | Conversation history with sliding token window. |
| `tool_parser.py` | YAML tool block parser (strips `<think>` blocks, handles compact/inline formats). |
| `event_bus.py` | Topic-based pub/sub + agent messaging; `get_event_bus()` singleton. |
| `graph_factory.py` | Role-specific graph composition via `GraphFactory`; used for subagent spawning. |
| `rollback_manager.py` | File snapshot + atomic rollback. `snapshot_files()`, `append_to_snapshot()`, `rollback()`, `cleanup_old_snapshots()`. |
| `sandbox.py` | `ExecutionSandbox` — temp workspace for patch validation (AST, ruff, mypy, pytest). `SelfDebugLoop` — max 3 retries with error analysis. |
| `workspace_guard.py` | Protected path patterns (`.git/`, `.env`, `pyproject.toml`, etc.) — blocks writes to critical files. |
| `role_config.py` | Role-based access control: planner (read-only), coder (full), reviewer (read+verify), researcher (search). `normalize_role()`, `CANONICAL_ROLES`, `ROLE_ALIASES`. |
| `tool_contracts.py` | Pydantic result schemas for specific tools (e.g. `ListFilesResult`). Validated in `execute_tool`. Includes `requires_split` flag for patch size guard. |
| `tool_schema.py` | Base `ToolContract` pydantic model: `{tool, args, result, error}`. |

---

## Event System (`src/core/orchestration/event_bus.py`)

Topic-based pub/sub with agent-level messaging:
- `subscribe(topic, handler)` / `publish(topic, payload)`
- `subscribe_to_agent()`, `publish_to_agent()` — multi-agent coordination
- `broadcast_to_agents()` — coordinated messaging
- Message priority: LOW, NORMAL, HIGH, CRITICAL
- Wildcard `*` receives all agent messages

### Dashboard Events

| Event | Publisher | Payload |
|-------|-----------|---------|
| `file.modified` | `orchestrator.execute_tool` | `{path, tool, workdir}` |
| `file.deleted` | `orchestrator.execute_tool` | `{path, workdir}` |
| `tool.execute.start` | `orchestrator.execute_tool` | `{tool, args, workdir}` |
| `tool.execute.finish` | `orchestrator.execute_tool` | `{tool, ok, workdir}` |
| `tool.execute.error` | `orchestrator.execute_tool` | `{tool, error, workdir}` |
| `tool.invoked` | `orchestrator.execute_tool` | `{tool, ts, workdir}` |
| `tool.preflight` | `orchestrator.preflight_check` | `{tool, ok}` |
| `plan.progress` | `execution_node` | `{current_step, total_steps, step_description, completed}` |
| `verification.complete` | `verification_node` | `{status, tests, linter, syntax}` |
| `model.routing` | `ProviderManager` | `{model, provider, complexity}` |
| `message.truncation` | `MessageManager` | `{dropped_count, remaining}` |
| `role.change` | `role_tools.set_role` | `{role}` |
| `ui.notification` | various | `{level, message, source}` |

---

## Telemetry (`src/core/telemetry/`)

| File | Description |
|------|-------------|
| `consumer.py` | `TelemetryConsumer` — subscribes to EventBus and writes JSON-lines telemetry to `output/telemetry.jsonl`. |
| `metrics.py` | `TelemetryMetrics` — in-memory Prometheus-style counters/gauges/histograms; subscribes to tool and model events. Dependency-free. |

---

## UI Layer (`src/ui/`)

| File | Description |
|------|-------------|
| `app.py` | `CodingAgentApp` — wires EventBus, Orchestrator, ProviderManager; chooses Textual vs headless. |
| `textual_app_impl.py` | Full Textual TUI — sidebar (provider/model), chat output, input box; thread-based agent dispatch; settings panel integration. |
| `textual_app.py` | Minimal placeholder `TextualApp` shim; no-op in headless environments. |
| `views/main_view.py` | `MainViewController` — dashboard: `ModifiedFilesPanel`, `TaskProgressPanel`, `ToolActivityPanel`; subscribes to EventBus for live data. Split-pane layout 65%/35%. |
| `views/settings_panel.py` | `SettingsPanelController` — provider/model selection, new session; UI-framework-agnostic; updates `providers.json` models field at runtime. |
| `views/provider_panel.py` | `ProviderPanelController` — displays providers/models, handles switching; subscribes to `provider.models.list`. |
| `components/log_panel.py` | `LogPanel` — subscribes to `log.new` EventBus event; collects log entries for display. |
| `styles/main.tcss` | Textual CSS stylesheet. |

---

## Core Utilities (`src/core/`)

| File | Description |
|------|-------------|
| `logger.py` | Thread-safe logging with audit support. `AuditEventType` enum (COMMAND_EXECUTION, FILE_WRITE, PATH_TRAVERSAL_BLOCKED, etc.). Queue-based async log delivery to TUI. |
| `startup.py` | `provider_health_check()` — async check of all registered providers for adapter presence and model availability. Called from `main.py`. |
| `user_prefs.py` | `UserPrefs` — loads/saves user preferences to `~/.config/codingagent/prefs.json`. Stores `selected_model_provider`, `selected_model_name`, `active_mode`. |

---

## Evaluation (`src/core/evaluation/`)

| File | Description |
|------|-------------|
| `scenario_evaluator.py` | `Scenario` dataclass for standardized test definitions. `ScenarioEvaluator` runs evaluation suites deterministically (works with `deterministic=True` Orchestrator mode: temperature=0, seed param). SWE-bench style. |

---

## Configuration (`src/config/`)

```
src/config/
├── agent-brain/
│   ├── identity/
│   │   ├── LAWS.md          # Core operating laws (immutable)
│   │   └── SOUL.md          # Operating principles (immutable)
│   ├── roles/
│   │   ├── strategic.md     # Task decomposition and planning
│   │   ├── operational.md   # Tool execution and implementation
│   │   ├── analyst.md       # Repository exploration
│   │   ├── debugger.md      # Debugging and issue analysis
│   │   └── reviewer.md      # Quality assurance
│   └── skills/
│       ├── dry.md           # Don't Repeat Yourself
│       └── context_hygiene.md  # Context management
├── toolsets/
│   ├── coding.yaml
│   ├── debug.yaml
│   ├── review.yaml
│   └── planning.yaml
├── providers.json           # Provider configurations (LM Studio, Ollama, etc.)
└── schema.json              # Provider config JSON schema
```

**Dynamic Skill Injection:**
- `perception_node`: injects `context_hygiene` when task contains debug/fix/error/search keywords
- `execution_node`: injects `dry` when `len(relevant_files) > 2`

---

## Scripts (`scripts/`)

| File | Description |
|------|-------------|
| `generate_system_map.py` | Generates `docs/system_map.md` ASCII tree + `scripts/tree.json`. Excludes audit, .agent-context, .venv, tests, output. |
| `run_tui.py` / `start_tui.py` | Launch the TUI application. |
| `simulate_tui.py` | Headless TUI simulation for testing. |
| `add_provider.py` | Add a new LLM provider to config. |
| `check_providers_and_models.py` | Health check all configured providers. |
| `diagnose_lmstudio.py` | LM Studio-specific connectivity diagnostics. |
| `analyze_tokens.py` | Token usage analysis from usage.json. |
| `refresh_summaries.py` | Regenerate repo summaries. |
| `run_generate.py` | Run code generation task via CLI. |
| `test_agent_stability.py` | Stability test — run multiple agent tasks and check for regressions. |
| `test_langgraph_node.py` | Isolated LangGraph node tests. |
| `test_llm_stability.py` | LLM provider stability tests. |
| `test_real_lmstudio.py` / `test_real_lmstudio_file_edit.py` | Integration tests against live LM Studio. |
| `test_tools.py` | Tool execution tests. |
| `validate_ollama.py` | Ollama adapter validation. |
| `list_prompts.py` | List all compiled system prompts. |
| `wait_for_model.py` | Poll until a model is available. |
| `fetch_ollama.py` | Pull models from Ollama registry. |
| `ensure_venv.sh` | Bootstrap virtual environment. |
| `run_tests_settings.py` | Run tests with custom settings. |

---

## Reliability Features

- **Tool Timeout Protection**: `ThreadPoolExecutor` + `future.result(timeout=n)` — thread-safe; works from TUI daemon thread. (C1 fix: replaced `signal.SIGALRM` which was a no-op outside the main thread.)
- **Tool Contracts** (`tool_contracts.py`): Pydantic validation for tool results.
- **Loop Prevention**: `execution_trace.json` tracks tool+args pairs; blocks repeated calls after 3 consecutive identical actions; injects `[LOOP DETECTED]` message.
- **Tool Cooldown**: `tool_last_used` dict in `AgentState` tracks last execution count per `"tool_name:path"` key; blocks identical read-tool calls within `COOLDOWN_GAP=3` executions to prevent context spam.
- **Read-Before-Edit Guard**: `files_read` O(1) dict + `verified_reads` list + `_session_read_files` set — three-tier fallback ensures file is read before any modifying tool can write it.
- **Multi-file Atomicity**: Step-level transactional snapshots via `RollbackManager` — all files written in one step are bundled and atomically rolled back on verification failure.
- **WorkspaceGuard**: Blocks modifications to `.git/`, `.env`, `pyproject.toml`, `requirements.txt`, and other critical paths.
- **Deterministic Mode**: `deterministic=True` sets temperature=0 + seed for reproducible runs.
- **Cost Tracking**: Tokens, latency, tool calls tracked in `.agent-context/usage.json`.
- **AST Sandbox**: `execute_tool` validates `new_content` via `ast.parse(new_content)` directly — validates the new content being written, not the old file on disk. (C2 fix.)
- **Prompt Injection Guard**: `perception_node` rejects any tool block whose `name:` value appears verbatim in a user-role history message. (F8.)
- **Thinking-Token Stripping**: `thinking_utils.strip_thinking()` removes `<think>…</think>` blocks from all LLM responses. `budget_max_tokens()` doubles budget for DeepSeek-R1 (cannot suppress thinking). `/no_think` injected into prompts for Qwen3/QwQ models.
- **Compiled Graph Singleton**: `_get_compiled_graph()` compiles the LangGraph pipeline once at module level — not per invocation. (P1 fix.)

---

## Known Architecture Notes

- **`workflow_nodes.py`** is a pure re-export shim for backward compatibility. New code imports directly from individual node files.
- **`src/tools/toolsets/`** (legacy) and **`src/config/toolsets/`** (canonical) both exist; the loader prefers `src/config/toolsets/`.
- **`src/core/memory/session_store.py`** is the correct location; not `src/core/context/session_store.py`.
- **`plan_validator_node.py`** exposes a standalone `validate_plan()` function, not an async node — called directly by `planning_node` or `orchestrator` before executing a plan.
- The **`graph/nodes/__init__.py`** is empty; all node imports are explicit in `workflow_nodes.py` and `graph/builder.py`.
- **`sandbox.py`** (`ExecutionSandbox`) is instantiated in `execute_tool` only for AST validation — `SelfDebugLoop` inside it is never used at runtime (O1: over-engineered dead code).
- **`advanced_features.py`** (`src/core/memory/`) is not imported by any orchestration node — it is a standalone utility file. `TrajectoryLogger`, `DreamConsolidator`, `ReviewAgent`, `RefactoringAgent`, and `SkillLearner` are all instantiated directly in `memory_update_node`.
- **`providers.json`** must be an array `[{...}]` not a top-level object. The settings panel and provider loader both require array format.
- **`replan_node`** uses the `strategic` role (not `planner` — which does not exist). Subagent `GraphFactory` creates `researcher`, `coder`, `reviewer`, `planner` graphs via canonical-name mapping.
- **Correlation IDs**: `new_correlation_id()` is minted per agent turn in `orchestrator.run_agent_once()`; `event_bus.publish()` auto-stamps dict payloads with `_correlation_id`; `call_model()` logs `cid=` for end-to-end tracing.
