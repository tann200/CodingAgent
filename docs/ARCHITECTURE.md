# CodingAgent Architecture

> **Implementation Status**: Fully implemented — LangGraph pipeline, multi-file atomic rollback, advanced memory, repository intelligence, PRSW (Parallel Reads, Sequential Writes), DAG-based wave execution, Native Tool Support (frontier + local models), Role-Based Prompt Injection, Hardcoded Temperature Routing, ACP/MCP Compliance (GAP 1-3), Textual TUI, GitHub Copilot OAuth, approval gate, loop guards, event log, shell hooks, sandbox (bwrap), MCP client, LSP integration, session fork/revert, snapshot manager, permission policy, model tiers, tokenizer, auto-compactor, subagent TUI visibility, orchestrator decomposition, file_tools decomposition, Groq/LiteLLM adapters, 3229+ unit tests passing.
> **Recent Updates (2026-04)**: Vol19–Vol27 + CODEBASE_FINDINGS audit pass: orchestrator decomposed into `inference_loop.py`, `task_lifecycle.py`, `tool_execution_pipeline.py`, `tool_registry.py`, `orchestrator_bootstrap.py`; `file_tools.py` decomposed into `_bash_exec.py`, `_file_io.py`, `_edit_tools.py`, `_diff_gate.py`; Groq and LiteLLM adapters added; subagent TUI visibility (SUBAGENT-VIS) fully implemented; per-tool icons and diff rendering in TUI; queued-message indicator; `/undo` command; `x-initiator` dynamic header for GitHub Copilot; all TUI/Copilot bugs from `BUGS.md` resolved.
> **Audit Fixes (2026-04)**: 27+ audit cycles completed (vol1–vol27 + scan-2 + CODEBASE_FINDINGS). All Critical, High, and Medium severity findings resolved. Last validation: 2026-04-11. Open low-priority items tracked in `docs/CODEBASE_FINDINGS.md` §8.

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
| Stage 11 - PRSW (Parallel Reads, Sequential Writes) | ✅ Complete | FileLockManager, WaveCoordinator, PRSW events, delegation_node updates |
| Stage 12 - DAG-Based Execution | ✅ Complete | DAG parser, wave computation, execution_waves in state |
| Stage 13 - Native Tool Support | ✅ Complete | `supports_native_tools` flag, format-aware ContextBuilder, native tool_calls parsing |
| Stage 14 - Wiring Sprint | ✅ Complete | SkillLearner, SessionStore, plan validator defaults wired |
| Stage 15 - Multi-file Atomicity | ✅ Complete | Step transactions: begin/append/rollback via RollbackManager |
| Stage 16 - Deterministic Mode | ✅ Complete | temperature=0, seed param, ScenarioEvaluator for regression tests |
| Stage 17 - Thread Safety | ✅ Complete | ThreadPoolExecutor timeout replaces signal.SIGALRM |
| Stage 18 - Delegation & Parallel Memory | ✅ Complete | Delegation node for subagent spawning, parallel memory ops, auto-save methods |
| Stage 19 - Security Audit Fixes | ✅ Complete | All Critical and High severity audit findings resolved (vol1–vol9) |
| Stage 20 - Analyst Delegation | ✅ Complete | `analyst_delegation_node` gates complex tasks; findings injected into planning |
| Stage 21 - Tool Cooldown & Read Tracking | ✅ Complete | `tool_last_used` cooldown map, `files_read` O(1) dict, cooldown gap enforcement |
| Stage 22 - Thinking-Token Optimization | ✅ Complete | `thinking_utils.py`: strip `<think>` blocks, model-aware max_tokens budget, /no_think injection |
| Stage 23 - Role Blurring Fix | ✅ Complete | ContextBuilder accepts `role_name`; loads single role; eliminates prompt contamination |
| Stage 24 - Temperature Routing | ✅ Complete | Hardcoded `temperature=0.3` in planning_node, `temperature=0.0` in execution_node |
| Stage 25 - Anti-Yap & Few-Shot | ✅ Complete | SOUL.md strict formatting + operational.md YAML execution examples |
| Stage 26 - Session State Hydration (GAP 1) | ✅ Complete | `session.request_state` / `session.hydrated` handshake; AgentSessionManager |
| Stage 27 - ACP Payload Schema (GAP 2) | ✅ Complete | `tool.execute.*` events use ACP schema: `sessionUpdate`, `toolCallId`, `status`, `content` |
| Stage 28 - MCP STDIO Server (GAP 3) | ✅ Complete | `mcp_stdio_server.py` bridges EventBus to stdin/stdout JSON-RPC; supports IDE integration |
| Stage 29 - Structured Planning Context | ✅ Complete | P3-1 call_graph/test_map JSON from analysis→planning; P3-2 test pre-retrieval; P3-6 few-shot DAG examples; P3-10 TUI history persistence |
| Stage 30 - Tool System Overhaul | ✅ Complete | `@tool` decorator + `build_registry()` auto-discovery (60 tools across 16 modules); 7 new modules: web, AST, interaction, guardrails, post-write lint, memory search, tech-stack fingerprint; gap analysis vs LocalCodingAgent; 17 bugs found and fixed |
| Stage 31 - Scope Guard & Provider Enrichment | ✅ Complete | GAP-S2 workspace scope guard: `affected_files` from planning, runtime `ask_user` expansion, `scope.violation` events; S6-B `get_provider_capabilities()` enriched with `provider_family`/`model`/`provider_name`; S5-A/B session fork/revert; S5-C slash diff-preview; IMPL-IT-2 delegation mock integration tests; vol12 infrastructure audit |
| Stage 32 - Vol12 Audit Fixes | ✅ Complete | All 9 P1/P2 vol12 findings fixed: async permission gate, git timeout, LSP reader leak, git subcommand allowlist, atomic cost flush, credentials chmod, project_id sanitisation, restore git-clean, MCP connect cleanup; 5 pyright errors resolved; 0 pyright errors; 2607 tests passing |
| Stage 33 - Vol13 Audit Fixes | ✅ Complete | All Vol13 P1/P2/P3 findings fixed: sed Gate 3c (`bash_readonly` blocks `sed -i`), tool call idempotency guard, immutable `_BASE_DANGEROUS_PATTERNS` + `add_dangerous_pattern()`, `asyncio.get_running_loop()` fix in LSP client, `reset()` lock coverage in SessionCostTracker, GIT_SAFE gate unit tests, `CODINGAGENT_TRUSTED` doc; 0 pyright errors; 2615 tests passing |
| Stage 34 - Vol14 Audit Fixes | ✅ Complete | All Vol14 P1/P2/P3 findings fixed: `cost_tracker.flush()`/`reset()`/`record_tool_call()` wired in orchestrator (HIGH-1), `flush()` clears buffer inside lock after snapshot (HIGH-2), `reset_idempotency()` called at turn start (HIGH-3), `delete_file`/`rename_file`/`ast_rename` added to `WRITE_TOOLS_REQUIRING_READ` (WR-1), `add_dangerous_pattern` re-exported + docstring fixed (WR-2), dual `lstat`/`stat` world-writable symlink check (TS-1), regression tests for flush idempotency and idempotency lifecycle (ET-1/ET-2); 0 pyright errors; 2617 tests passing |
| Stage 35 - Vol15–16 Audit Fixes | ✅ Complete | P4-2 LSP auto-restart (exponential backoff, `_shutting_down` guard, `get_client()` re-start on dead client); P4-3 bash security bypass closures (env-prefix + shell, absolute-path shell -c); P4-4 budget ceiling alert wired end-to-end (`ProjectSettings.budget_ceiling_usd` → `orchestrator.py` → `SessionCostTracker`); ROB-1 test suite (26 tests), ROB-2 test suite (23 tests), CAP-1 project settings tests (8 new); 0 pyright errors; 2908 tests passing |
| Stage 36 - Scan-2 Audit Fixes + TUI Rewrite | ✅ Complete | HIGH-3: removed `os.environ["CODINGAGENT_DELEGATION_DEPTH"]` write (race condition across concurrent delegations; ContextVar propagation is sole mechanism); MED-8: replaced 4 `assert self._conn is not None` with `RuntimeError` in `event_log.py`; CP-10: LSP diagnostics injected into system prompt via `lsp_context.py` + `instruction_loader.py`; CP-12: `AnthropicAdapter._preprocess_messages()` inserts `cache_control: {"type": "ephemeral"}` at static/dynamic boundary; vol17–vol18 findings resolved; full Textual TUI rewrite (`tui/`); GitHub Copilot OAuth adapter; all 46 scan-2 findings resolved |

---

## GAP Compliance (ACP/MCP)

### GAP 1: Session State Hydration
- TUI subscribes to `session.request_state` on mount
- AgentSessionManager publishes `session.hydrated` with full state
- `_sync_session_state()` called after tool execution in orchestrator

### GAP 2: ACP Payload Standardization
- `tool.execute.start`: `sessionUpdate: "tool_call_update"`, `toolCallId`, `title`, `status: "in_progress"`, `rawInput`
- `tool.execute.finish`: `status: "completed"`, `content: [{"type": "text", "text": ...}]`, `rawOutput`
- `tool.execute.error`: `status: "failed"`, `content`, `error`
- `plan.progress`: `sessionUpdate: "plan_progress"`, `planId`, `currentStep`, `totalSteps`

### GAP 3: MCP STDIO Server
- Bridges EventBus to stdin/stdout JSON-RPC 2.0
- Supports: `initialize`, `session/request_state`, `tools/list`, `tools/call`, `resources/*`, `prompts/*`
- Forwards all EventBus events as JSON-RPC notifications

### GAP-S2: Workspace Scope Guard
- `planning_node._extract_affected_files(steps)` extracts file paths from plan steps into `AgentState["affected_files"]`
- `execution_node` propagates `affected_files` to `orchestrator._affected_files` before each tool call
- `Orchestrator.execute_tool()` blocks writes to files outside the allowed set; emits `scope.violation` events
- `ask_user` approval can expand `affected_files` at runtime by extracting paths from the answer text
- Guard is bypassed when `affected_files` is empty/None (pre-plan fast-path)

---

## Security & Stability Audit Fixes (vol1–vol18 + scan-2, 2026-03/04)

Eighteen audit cycles completed plus a full scan-2 pass. All Critical, High, and Medium severity findings are resolved. Latest reports: `docs/audit/audit-report-vol17.md`, `docs/audit/audit-report-vol18.md`.

### Scan-2 Fixes (2026-04-06)

| Finding | Fix | Location |
|---------|-----|----------|
| HIGH-3 — delegation depth env-race | Removed `os.environ["CODINGAGENT_DELEGATION_DEPTH"] = str(depth + 1)`; ContextVar `_delegation_depth` via `_child_ctx` is sole propagation mechanism; comment updated | `src/tools/subagent_tools.py:29-31, 375` |
| MED-8 — bare `assert` on DB connection (4 sites) | Replaced all 4 `assert self._conn is not None` with `if self._conn is None: raise RuntimeError("EventLog: database connection is not open")` | `src/core/orchestration/event_log.py` |
| CP-10 — LSP diagnostics not injected | `_diagnostics_cache` in `LSPClient`; `get_lsp_diagnostics_block()` in `lsp_context.py`; wired into `instruction_loader.build_runtime_context()` | `lsp_context.py`, `instruction_loader.py` |
| CP-12 — Anthropic cache_control missing | New `AnthropicAdapter` with `_preprocess_messages()` override; `cache_control: {"type": "ephemeral"}` on static block at `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` sentinel | `anthropic_adapter.py` |

### Vol17–Vol18 Fixes (2026-04-06)

### Vol17–Vol18 Fixes (2026-04-06)

| Finding | Fix | Location |
|---------|-----|----------|
| P4-2 — LSP auto-restart | Exponential backoff (1s, 2s, 4s, 8s, 16s, cap 30s); `_shutting_down` flag prevents restart loops; `get_client()` detects dead client and re-initialises | `lsp_client.py` |
| CP-10 — LSP diagnostics injection | `_diagnostics_cache` dict in `LSPClient`; `get_lsp_diagnostics_block()` in `lsp_context.py`; called from `instruction_loader.build_runtime_context()` | `lsp_context.py`, `instruction_loader.py` |
| CP-12 — Anthropic cache sentinel | `AnthropicAdapter` extends `OpenAICompatibleAdapter`; `_preprocess_messages()` splits messages at `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` and adds `cache_control: {"type": "ephemeral"}` to the last static block | `anthropic_adapter.py` |
| ROB-1 — LSP restart regression suite | 26-test suite covering restart triggers, backoff timing, `_shutting_down` guard | `tests/unit/test_lsp_auto_restart.py` |
| ROB-2 — ConfigWatcher regression suite | 23-test suite covering live reload, debounce, error handling | `tests/unit/test_config_watcher.py` |

### Vol14 Fixes (2026-04-06)

| Finding | Fix | Location |
|---------|-----|----------|
| HIGH-1 — `cost_tracker.flush()`/`reset()` never called | All 4 `_flush_usage_buffer()` call sites replaced with `cost_tracker.flush(task_id=...)`; `cost_tracker.reset()` called at turn start; `cost_tracker.record_tool_call()` wired after tool execution; `_flush_usage_buffer()` reduced to deprecated thin wrapper | `orchestrator.py` |
| HIGH-2 — `flush()` double-counting bug | `flush()` clears `_usage_buffer` inside lock after taking snapshot; repeated `flush()` without `reset()` no longer double-counts | `session_cost_tracker.py` |
| HIGH-3 — `reset_idempotency()` has zero callers | `tool_execution_service.reset_idempotency()` called at turn start in `run_agent_once()` so `_seen_calls` is cleared between tasks | `orchestrator.py` |
| WR-1 — `delete_file`/`rename_file`/`ast_rename` bypass scope guard | Added to `WRITE_TOOLS_REQUIRING_READ` set; these tools now participate in `_affected_files` enforcement | `orchestrator.py` |
| WR-2 — `add_dangerous_pattern()` not in public API docs | `src/tools/__init__.py` docstring updated to use `add_dangerous_pattern()`; function re-exported from `__init__.py` | `src/tools/__init__.py` |
| TS-1 — world-writable check missed symlink itself | Check now uses both `os.lstat()` (symlink entry) and `Path.stat()` (symlink target); catches world-writable symlink pointing to safe directory | `deferred_init.py` |
| ET-1 — no regression test for flush idempotency | `test_flush_is_idempotent_without_reset` added | `tests/unit/test_d10_services.py` |
| ET-2 — no lifecycle test for reset_idempotency | `test_reset_idempotency_clears_between_tasks` added | `tests/unit/test_d10_services.py` |

### Vol13 Fixes (2026-04-06)

| Finding | Fix | Location |
|---------|-----|----------|
| P1-1 — preview gate missing path guard | `_on_confirmed`/`_on_rejected` validate non-empty `path` before `resolve_preview_gate`; logs warning if missing | `preview_coordinator.py` |
| P1-2 — plugin loader world-writable dir | Checks `stat().st_mode & stat.S_IWOTH`; refuses world-writable dirs, sets `plugin_dir = None`, early return | `deferred_init.py` |
| P1-3 — rm bypass variants not blocked | Added `/bin/rm`, `/usr/bin/rm`, `\\rm`, `rm -v -r`, `rm -v -f`, `rm --recursive`, `rm --force` to base patterns | `_security.py` |
| P2-1 — nested asyncio.wait_for in MCP | Removed outer `wait_for` wrapper from `_initialize()`; inner `_request()` already enforces timeout | `mcp_client.py` |
| P2-2 — SessionCostTracker race conditions | `threading.Lock` guards `record_tool_call`, `flush`, `get_buffer`, `reset` | `session_cost_tracker.py` |
| P2-3 — sed -i not blocked in bash_readonly | Added `SED_WRITE_FLAGS` import + Gate 3c after Gate 3b; blocks `-i` / `--in-place` tokens | `file_tools.py` |
| P2-4 — session_cost_usd not persisted | `flush()` writes `session_cost_usd` to `usage.json` under lock snapshot | `session_cost_tracker.py` |
| P2-5 — deprecated get_event_loop() | `asyncio.get_event_loop()` → `asyncio.get_running_loop()` | `lsp_client.py:390` |
| P3-1 — no idempotency guard for tool calls | `_seen_calls` set checked as step 0 in `pre_execute`; duplicate exact calls return blocked verdict | `tool_execution_service.py` |
| P3-2 — GIT_SAFE gate has no unit tests | Added `TestGitSafeSubcommandsGate` (8 tests) covering `bash()` + `bash_readonly()` | `test_bash_security_file_tools_caching.py` |
| P3-4 — DANGEROUS_PATTERNS is mutable | Refactored to `_BASE_DANGEROUS_PATTERNS: tuple` + `_EXTRA_PATTERNS: list`; `add_dangerous_pattern()` API | `_security.py` |
| P3-5 — CODINGAGENT_TRUSTED undocumented | Added one-liner to Running Tests section | `docs/DEVELOPMENT.md` |

### Vol12 Fixes (2026-04-06)

| Finding | Fix | Location |
|---------|-----|----------|
| P1-1 — Blocking permission gate | `pre_execute` + `_check_permission_gate` made `async def`; `await gate.wait_async()` | `tool_execution_service.py` |
| P1-2 — Git subprocess timeout | `_GIT_TIMEOUT = 60.0`; `asyncio.wait_for` + kill on timeout | `snapshot_manager.py` |
| P1-3 — LSP reader loop future leak | `_reader_loop` `finally` sets exception on all pending futures | `lsp_client.py` |
| P1-4 — Unrestricted git in bash | Removed `"git"` from `SAFE_COMMANDS`; `GIT_SAFE_SUBCOMMANDS` allowlist; gates in `bash()` + `bash_readonly()` | `_security.py`, `file_tools.py` |
| P2-1 — Non-atomic cost flush | `flush()` uses `mkstemp` + `os.replace`; fixed `estimate_cost_usd` arg order | `session_cost_tracker.py` |
| P2-2 — Credentials file permissions | `os.chmod(_PREFS_PATH, 0o600)` after every write | `credentials.py` |
| P2-3 — project_id path traversal | `_PROJECT_ID_RE` sanitises id before use in git commands | `snapshot_manager.py` |
| P2-4 — Restore leaves untracked files | `restore()` runs `git clean -fd` after `checkout-index` | `snapshot_manager.py` |
| P2-5 — MCP connect leak on failure | Cancels reader task + terminates process before re-raising | `mcp_client.py` |

Full per-finding tables for vol1–vol11 are in `docs/archive/`. Per-finding tables for vol12–vol14 are in `docs/IMPLEMENTATION_PLAN.md` and the corresponding `docs/audit/audit-report-vol*.md` files.

---

## Deferred Features (Open Engineering Debt)

These items have been identified across multiple audit cycles but remain unimplemented. Open low-priority code quality items are tracked in `docs/CODEBASE_FINDINGS.md` §8.

| ID | Feature | Status | Source |
|----|---------|--------|--------|
| P4-1 | `config_watcher.py` — live config reload without restart | ✅ CLOSED vol15 | vol12 CRIT-1 → vol13 P4-1 → vol14 P4-1 |
| P4-2 | LSP server auto-restart on crash | ✅ CLOSED vol15 | vol12 P3-3 → vol14 P4-2 |
| P4-3 | Token-level bash security analysis | ✅ CLOSED vol16 | vol12 P3-5 → vol14 P4-3 |
| P4-4 | Budget ceiling alert in `SessionCostTracker` | ✅ CLOSED vol15 | vol12 P4-1 → vol14 P4-4 |
| CP-1 | Structural recursion prevention — remove `Agent` from subagent tool sets | Low complexity; depth counter exists but structural enforcement is stronger | `deep-dive-claw-code-architecture.md` Pattern 1 |
| CP-2 | Manifest-first subagent spawning — write manifest before spawning thread | ✅ CLOSED: manifest written before thread spawn | `deep-dive-claw-code-architecture.md` Pattern 2 |
| CP-3 | Stable content hash for instruction files — SHA-256 dedup when walking for `AGENT.md` | Low complexity; prevents duplicate instruction injection | `deep-dive-claw-code-architecture.md` Pattern 3 |
| CP-4 | Dynamic boundary sentinel in system prompt — `__DYNAMIC_BOUNDARY__` for prompt caching | ✅ CLOSED CP-12: `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` + Anthropic `cache_control` | `deep-dive-claw-code-architecture.md` Pattern 4 |
| CP-5 | `verification_nudge_needed` in TodoWrite — remind agent to verify when all todos complete | Trivial; prevents agent from stopping without running tests | `deep-dive-claw-code-architecture.md` Pattern 5 |
| CP-6 | Deterministic auto-compaction — token-count threshold, structured 7-section summary, no LLM call | Medium complexity; current `/compact` is LLM-based and manual | `deep-dive-claw-code-architecture.md` §3 `compact.rs` |
| CP-7 | Shell hooks with post-tool + deny semantics — stdin-JSON payload, exit `2` = deny, settings-file-configurable | Medium complexity; current hooks are Python-only, pre-only, no deny | `deep-dive-claw-code-architecture.md` §5 `hooks.rs` |
| CP-8 | Per-tool `PermissionMode` + `PermissionPolicy` object + config-file permission mode | Low complexity; replaces hardcoded bash allowlist with typed policy | `deep-dive-claw-code-architecture.md` §6 `permissions.rs` |
| CP-9 | Cache token tracking — `cache_creation_input_tokens` + `cache_read_input_tokens` in `SessionCostTracker` | Low complexity; requires API adapter changes to capture cache fields | `deep-dive-claw-code-architecture.md` §7 `usage.rs` |
| CP-10 | LSP diagnostics + go-to-definition + references injected into system prompt as named section | ✅ CLOSED vol17: `_diagnostics_cache` in `LSPClient`, `get_lsp_diagnostics_block()` in `lsp_context.py`, wired into `instruction_loader.build_runtime_context()` | `deep-dive-claw-code-architecture.md` §12 `lsp/` |
| CP-11 | Ancestor instruction file walk — `AGENTS.md` in all ancestor dirs with dedup + 4K/12K budget caps | Low complexity; currently only workspace root is checked | `deep-dive-claw-code-architecture.md` §4 `prompt.rs` |
| CP-12 | `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` sentinel wired to Anthropic `cache_control` breakpoint | ✅ CLOSED vol17: new `AnthropicAdapter` with `_preprocess_messages()` override; `cache_control: {"type": "ephemeral"}` on static block | `deep-dive-claw-code-architecture.md` §4 `prompt.rs` |
| CP-13 | Per-project settings file — `.agent/settings.json` with model + permission-mode overrides, deep merge | Low-Medium complexity; enables per-project agent configuration | `deep-dive-claw-code-architecture.md` §9 `config.rs` |
| CP-14 | Session `version` field in `MessageManager` persistence for future migration | Trivial; no functional change, insurance for schema evolution | `deep-dive-claw-code-architecture.md` §8 `session.rs` |
| CP-15 | `send_user_message` tool — agent sends mid-turn message to user with `normal`/`proactive` status | Low complexity; new tool registration + EventBus publish | `deep-dive-claw-code-architecture.md` §14 `tools/src/lib.rs` |
| GAP-TUI-2 | Per-tool permission `allow/deny/ask` policy with glob patterns persisted per project | Open — CodingAgent gates only `bash`; OpenCode gates all tools | `docs/gap-analysis-opencode-vs-codingagent-v2.md` Part 2 |
| GAP-TUI-7 | `/share` session export (gist / clipboard) | Open | `docs/gap-analysis-opencode-vs-codingagent-v2.md` Part 3 |
| LOOP-1 | Hard non-bypassable global token budget + step ceiling | Open | `docs/agent-loop-improvement-analysis.md` §1.1 |
| LOOP-2 | Broad doom-loop detection across full session (semantic fingerprint) | Open | `docs/agent-loop-improvement-analysis.md` §1.2 |

Full TUI/permission/UX gap analysis: `docs/gap-analysis-opencode-vs-codingagent-v2.md`
Agent loop analysis: `docs/agent-loop-improvement-analysis.md`

---

## Pipeline Overview

```
Fast-Path (simple 1-step task):
  perception → execution → verification → evaluation → (memory_sync | delegation | END)

Full Pipeline (complex multi-step task):
  perception → analysis → [analyst_delegation →] planning → plan_validator
            → execution → step_controller → verification → evaluation
            → (memory_sync | delegation | step_controller | END)

Plan validation failure (F10):
  plan_validator → planning  (direct re-planning, saves 2 LLM calls)
  Guard: plan_attempts ≥ 3 → force execution (prevents planning infinite loop)

Patch-too-large path:
  execution → replan → step_controller → execution (smaller steps)
  Guard: replan_attempts ≥ 5 → memory_sync (prevents replan infinite loop)

Verification failure / debug path:
  verification → evaluation → debug → (execution | END)
  Guard: total_debug_attempts ≥ max → memory_sync

Tool budget path:
  execution → memory_sync (when tool_call_count ≥ max_tool_calls)
```

---

## Phase 6: PRSW - Parallel Reads, Sequential Writes

### Core Principle

```
┌─────────────────────────────────────────────────────────────────────┐
│                    PARALLEL READS, SEQUENTIAL WRITES                 │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│   READ-ONLY AGENTS (can run in parallel)                           │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐                        │
│   │  Scout   │  │Researcher│  │ Reviewer │                        │
│   │  read()  │  │  read()  │  │  read()  │                        │
│   └────┬─────┘  └────┬─────┘  └────┬─────┘                        │
│        │             │             │                               │
│        └─────────────┼─────────────┘                               │
│                      │                                             │
│                      ▼                                             │
│           ┌──────────────────────┐                                 │
│           │   EventBus P2P     │                                 │
│           │ (files.discovered)  │                                 │
│           │ (docs.fetched)     │                                 │
│           │ (bugs.found)       │                                 │
│           └──────────┬───────────┘                                 │
│                      │                                             │
│                      ▼                                             │
│   WRITE-ONLY AGENT (sequential)                                    │
│   ┌──────────────────────────────────────────┐                     │
│   │              CODER AGENT                  │                     │
│   │                                           │                     │
│   │  1. Wait for Scout/Researcher results    │                     │
│   │  2. Apply changes ONE AT A TIME          │                     │
│   │  3. Wait for confirmation before next    │                     │
│   │  4. Notify Reviewer/Tester via P2P      │                     │
│   └──────────────────────────────────────────┘                     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Components

| Component | File | Description |
|-----------|------|-------------|
| **FileLockManager** | `file_lock_manager.py` | Async file locking with read/write separation. Multiple read locks allowed; single write lock exclusive. |
| **WaveCoordinator** | `wave_coordinator.py` | Manages wave execution: parallel read agents → sequential write agents. |
| **PRSWTopics** | `prsw_topics.py` | Event topics: `files.ready`, `context.gathered`, `write.complete`, `blocked.on.write`. |
| **should_use_prsw** | `graph/builder.py` | Routing function that detects when PRSW should be used (mixed read/write delegations). |

### State Fields Added

| Field | Type | Purpose |
|-------|------|---------|
| `execution_waves` | `Optional[List[List[str]]]` | Computed DAG wave execution order |
| `current_wave` | `Optional[int]` | Current wave index being executed |
| `plan_dag` | `Optional[Dict]` | DAG representation of the execution plan |
| `_file_lock_manager` | `Optional[FileLockManager]` | File lock manager instance for PRSW |
| `preview_mode` | `Optional[bool]` | Preview/dry-run mode flag |

### Integration Points

- **delegation_node**: Detects PRSW delegations, executes read agents in parallel, writes sequentially
- **execution_node**: Tracks wave progression, advances to next wave when current wave completes
- **planning_node**: Computes execution waves from DAG when generating plans
- **orchestrator**: Initializes FileLockManager, exposes via `get_file_lock_manager()`

---

## DAG-Based Execution

### Overview

Plans are now parsed into a DAG to compute optimal execution order:
- Dependency analysis identifies which steps can run in parallel
- Wave computation groups independent steps into execution waves
- Wave advancement ensures all steps in a wave complete before next wave starts

### DAG Parser (`dag_parser.py`)

| Function | Description |
|----------|-------------|
| `PlanDAG` | DAG representation with nodes and edges |
| `add_edge(from, to)` | Add dependency edge |
| `validate()` | Check for cycles |
| `topological_sort_waves()` | Compute execution waves (list of step ID lists) |
| `_convert_flat_to_dag(steps)` | Convert flat plan to DAG based on file dependencies |

### Wave Execution Flow

```
planning_node generates plan
         │
         ▼
   ┌─────────────┐
   │dag_parser   │
   │converts to  │
   │DAG + waves  │
   └─────────────┘
         │
         ▼
   State stores:
   - plan_dag: {steps: [...]}
   - execution_waves: [[step_0, step_1], [step_2], [step_3]]
   - current_wave: 0
         │
         ▼
   execution_node executes steps
         │
         ▼
   Step completes → check if all steps in wave done
         │
         ├── Yes → current_wave += 1
         │
         └── No → continue to next step
```

---

**Conditional Routing (Fast-Path / W3):**
`route_after_perception` checks `next_action` and task complexity:
- Tool call ready + **simple task** → skip to `execution` (fast-path)
- Tool call ready + **complex task** (refactor/rewrite/multi-step keyword, >3 relevant files, or 2+ step plan) → force through `analysis` (W3 fix)
- No tool call → `analysis` (full pipeline)

`should_after_analysis` checks task complexity:
- Complex task → `analyst_delegation` → `planning`
- Simple task → `planning` directly

**Node Role Mapping:**

| Node | Role | Temperature | LLM Calls | Notes |
|------|------|-------------|-----------|-------|
| `perception_node` | `operational` | (config) | ✅ Yes | Task parsing, tool call generation; F8 prompt injection guard rejects reflected YAML tool blocks from user-role history |
| `planning_node` | `strategic` | **0.3** | ✅ Yes | Structured JSON plan via LLM; `max_tokens=3000`; fallback plan on parse failure; injects `analyst_findings` when present |
| `debug_node` | `debugger` | (config) | ✅ Yes | Error analysis and fix generation; resets counter on error-type change |
| `replan_node` | `strategic` | (config) | ✅ Yes | Step splitting for oversized patches (>200 lines) |
| `analysis_node` | N/A | N/A | ❌ Tool-based | VectorStore + SymbolGraph + ContextController; fast-path bypassed for complex tasks (C3 fix); `_INDEXED_DIRS` keyed by `(path, mtime_ns)` |
| `analyst_delegation_node` | `analyst` | (config) | ✅ Yes | Spawned for complex tasks only; injects `<findings>` into `analyst_findings`; result feeds `planning_node` |
| `execution_node` | `operational` | **0.0** | ⚠️ Optional | Uses `planned_action` when set (F1); enforces read-before-edit via `files_read` O(1) dict + `verified_reads` fallback; tool cooldown via `tool_last_used` (COOLDOWN_GAP=3); LLM call only if no pre-set action |
| `verification_node` | N/A | N/A | ❌ Tool-based | pytest/ruff/tsc/jest — deterministic; rollback on failure |
| `evaluation_node` | N/A | N/A | ❌ State-based | Routes to `debug` on failure (bounded by `debug_attempts`); never routes directly to step_controller on failure |
| `step_controller_node` | N/A | N/A | ❌ State-based | Step gating; failed step retries via `execution` not `verification` |
| `plan_validator_node` | N/A | N/A | ❌ State-based | Plan structure validation; on failure routes to `planning` (F10 fix — saves 2 LLM calls); emergency round≥8 guard forces execution |
| `delegation_node` | N/A | N/A | ❌ Spawns subagents | C4 fix: results injected into conversation history (not write-only); `delegation_results` also kept in state for backward compat |
| `memory_update_node` | N/A | N/A | ❌ Tool-based | Distillation + parallel memory ops via `asyncio.gather()` |

---

## Phase 13: Native Tool Support (Frontier vs Local Models)

### Overview

Support both local models (YAML tool format) and frontier models (native JSON function calling) in the same codebase.

### Configuration

**providers.json** - `supports_native_tools` flag:
```json
[
  {"name": "lm_studio", "supports_native_tools": false},
  {"name": "openai", "supports_native_tools": true}
]
```

### ContextBuilder Format-Aware

The `ContextBuilder.build_prompt()` method accepts `provider_capabilities` parameter:
- `supports_native_tools: true` → Injects native function calling instructions
- `supports_native_tools: false` → Injects YAML tool format instructions

### Node Integration

All graph nodes pass provider capabilities to ContextBuilder:
- `perception_node` - Handles native tool_calls from API responses
- `execution_node` - Handles native tool_calls from API responses  
- `planning_node` - Uses format-aware prompts
- `debug_node` - Uses format-aware prompts
- `replan_node` - Uses format-aware prompts

### Tool Call Parsing

Nodes check for native tool_calls first, then fall back to YAML parsing:
```python
# 1. Check for Native JSON Tool Calls (Frontier Models)
if "tool_calls" in message_obj:
    tool_call = {"name": name, "arguments": args}

# 2. Fallback to YAML parsing (Local Models)
else:
    tool_call = parse_tool_block(content)
```

---

## Stage 22-25: Role Blurring Fix, Temperature Routing & Anti-Yap

### GAP 1: Role Blurring Fix (Prompt Contamination)

**The Problem:** Previous `ContextBuilder.build_prompt()` concatenated SOUL + operational + strategic prompts for every LLM call, causing local models to suffer from cognitive dissonance.

**The Fix:** Refactored `ContextBuilder.build_prompt()` to accept `role_name` parameter and inject only the selected role:

```python
def build_prompt(
    self,
    role_name: str,  # NEW: single role instead of identity+role
    active_skills: List[str],
    task_description: str,
    tools: List[Dict],
    conversation: List[Dict],
    ...
):
    role_content = self.roles.get(role_name, "")  # Load from agent-brain/roles/
    system_parts = [self.soul, role_content]  # Only SOUL + selected role
```

**Roles loaded from `agent-brain/roles/*.md`:**
- `operational` - Tool execution and implementation
- `strategic` - Task decomposition and planning
- `debugger` - Error analysis and fix generation
- `analyst` - Repository exploration
- `reviewer` - Quality assurance
- `tester`, `researcher`, `scout` - PRSW subagents

### GAP 2: Dynamic Temperature Routing

**The Problem:** Using global temperature (e.g., 0.7) caused:
- Execution node: random hallucinations of file paths
- Planning node: not creative enough for complex task decomposition

**The Fix:** Hardcoded temperatures at node level:
- `planning_node.py`: `temperature=0.3` (allows slight creativity)
- `execution_node.py`: `temperature=0.0` (strict determinism)

### GAP 3: Few-Shot Examples in Roles

**The Problem:** Local models learn from examples, not principles. Role prompts had good principles but no examples.

**The Fix:** Added execution format examples to `operational.md`:
```markdown
## Execution Format Example

USER: Read the auth.py file to check the login logic.
ASSISTANT:
```yaml
name: read_file
arguments:
  path: src/auth.py
```
RESULT: File read successfully
STATUS: partial
FILES_CHANGED: none
OBSERVE: The login function validates credentials...
```

### GAP 4: Anti-Yap Directive

**The Problem:** SOUL.md told the agent to "Be concise" but local models still output "Certainly! I will now list the directory..." wasting tokens.

**The Fix:** Added strict formatting constraints to `SOUL.md` and `strategic.md`:
```markdown
## Strict Formatting Constraints
- **NO CONVERSATIONAL FILLER:** Never say "Certainly!", "Here is the plan", "I will now execute..."
- **ACTION ONLY:** Your response must ONLY contain the required YAML tool block or JSON structure.
- **ZERO PREAMBLE:** Do not describe what you are about to do. Just execute.
```

### Node Integration

All graph nodes updated to use new `role_name` parameter:
- `planning_node.py` → `role_name="strategic"`, `temperature=0.3`
- `execution_node.py` → `role_name="operational"`, `temperature=0.0`
- `perception_node.py` → `role_name="operational"`
- `replan_node.py` → `role_name="strategic"`
- `debug_node.py` → `role_name="debugger"`

**Dynamic Skill Injection:** Skills now passed by name and loaded from `agent-brain/skills/*.md`:
- `perception_node`: injects `"context_hygiene"` for debug/search tasks
- `execution_node`: injects `"dry"` when `len(relevant_files) > 2`

---

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
   | `perception_node.py` | `perception_node` | Understands the user request, decomposes tasks, extracts tool calls from YAML or native tool_calls. Uses `operational` role. Injects `context_hygiene` skill for debug/search tasks. F8 prompt injection guard: rejects tool blocks whose `name:` appears verbatim in any user-role history message. Pre-retrieval (round 0): concurrent `asyncio.gather()` across `search_code`, `find_symbol`, `find_references`, and `find_tests_for_module` (P3-2). |
   | `analysis_node.py` | `analysis_node` | Explores the repository before planning via tool calls (no LLM). Three phases: (1) VectorStore semantic search, (2) SymbolGraph call graph enrichment (outputs `call_graph` + `test_map` as JSON dicts — P3-1), (3) ContextController token budget enforcement. Runs `repo_summary()` at start. Fast-path bypass suppressed for complex tasks (C3). `_INDEXED_DIRS` keyed by `(path, mtime_ns)` (F15). |
   | `analyst_delegation_node.py` | `analyst_delegation_node` | Spawned for complex tasks only (vol9 #56). Delegates deep repo analysis to `analyst` subagent. Stores `<findings>` in `state["analyst_findings"]`; `planning_node` injects findings into its LLM prompt. |
   | `planning_node.py` | `planning_node` | Converts perception/analysis outputs into a structured step-by-step plan via LLM. `max_tokens=3000` (P5). Injects `analyst_findings`, `call_graph`, and `test_map` JSON blocks when present (P3-1). Includes few-shot DAG examples in prompt (P3-6). Guaranteed fallback plan on parse failure (F7). Cross-session plan persistence via `last_plan.json`. Uses `strategic` role. Increments `plan_attempts` counter. |
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

### Tool Registry (`src/tools/_registry.py`)
Central registry of named tools. Uses the `@tool` decorator (`src/tools/_tool.py`) for auto-discovery. `build_registry()` discovers all 60 built-in tools across 16 modules in one call. Tool schemas are auto-generated from function signatures and injected into the system prompt as YAML or native JSON function definitions.

```python
from src.tools import build_registry
registry = build_registry(working_dir="/path/to/project")
# or with extra modules:
registry = build_registry(extra_modules=[my_custom_module])
```

**`@tool` decorator** marks a function for auto-discovery:
```python
@tool(side_effects=["write"], tags=["coding"])
def my_tool(path: str, content: str) -> Dict[str, Any]:
    """Description injected into the system prompt."""
    ...
```

`tags` control which toolset the tool appears in (`"coding"`, `"planning"`, `"debug"`, `"review"`). `side_effects` flags write tools for guardrail enforcement.

### File Operations (`src/tools/file_tools.py`)
- `list_files` / `fs.list` — directory listing
- `read_file` / `fs.read` — read file content; calls `mark_file_read()` on success
- `read_file_chunk` — read a byte range of a file
- `write_file` / `fs.write` — write file; 500-line hard guard fires **before** write; publishes `file.diff.preview` event before writing
- `edit_file` — apply a patch/diff to a file
- `edit_file_atomic` — replace an exact unique string in a file (like Claude Code's `Edit` tool)
- `edit_by_line_range` — replace specific line ranges; `start_line`/`end_line` coerced to `int`
- `delete_file` — delete a file
- `glob` — pattern matching; rejects `..` traversal; truncates at 200 results with `truncated: true`
- `batched_file_read` — read multiple files efficiently
- `multi_file_summary` — get file metadata without full reads
- `bash` — shell execution with allowlist; `DANGEROUS_PATTERNS` whitespace-normalised before matching; `sed -i` blocked in all flag forms (`-ni`, `-rni`, `--in-place=`)
- `bash_readonly` — read-only bash; blocks any command matching DANGEROUS_PATTERNS or write operators; safe for analysis tasks
- `tail_log_file` — tail the last N lines of a log file (default 50); safe alternative to `bash("tail ...")`
- `create_directory` — create a directory (and parents); respects safe_resolve path guard

**Diff Preview:** All write tools publish `file.diff.preview` before the write. TUI renders as side-by-side table with line numbers.

**Post-Write Lint (`src/tools/lint_dispatch.py`):** `quick_lint(path, workdir)` dispatches a fast syntax check after every write based on file extension (10 s timeout, never raises):
- `.py` → `py_compile`
- `.js/.mjs/.jsx` → `node --check`
- `.ts/.tsx` → `tsc --noEmit --module commonjs --target es2020` (falls back to `node --check`)
- `.go` → `go build -o /dev/null .` in the file's own directory
- `.rs` → `rustc --edition=2021 --emit=metadata`

Results returned as `lint_warnings` in the tool result dict.

**Security:** Tiered bash allowlist (Tier 1: safe read-only, Tier 2: test/compile, Tier 3: restricted). Shell operators blocked pre-parse. `safe_resolve()` shared utility for all path validation.

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
- `find_symbol(name)`, `find_references(name)` — symbol lookup via SymbolGraph; `find_references` uses word-boundary regex (`\b{name}\b`) to avoid false positives

### Repository Analysis (`src/tools/repo_analysis_tools.py`)
- `analyze_repository(workdir)` — scans Python, JS/TS, Go, and Rust files; extracts module summaries, import relationships, and per-language stats; writes `.agent-context/repo_memory.json`

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

### Memory Tools (`src/tools/memory_tools.py`)
- `memory_search(query, workdir)` — searches `TASK_STATE.md` and `execution_trace.json`; returns ranked matches (exact lines first, then trace entries by recency)

### Subagent Tools (`src/tools/subagent_tools.py`)
- `delegate_task(role, subtask_description, working_dir)` — spawns an isolated autonomous subagent via `GraphFactory` for a specific subtask, keeping the main agent's context window clean
- Supports PRSW roles: `scout`, `researcher`, `reviewer` (read-only, parallel), `coder`, `tester` (write, sequential)

**Subagent Roles (Phase 6 PRSW):**

| Role | Type | Description |
|------|------|-------------|
| `scout` | Read-only | Discovers files and project structure |
| `researcher` | Read-only | Analyzes code and gathers context |
| `reviewer` | Read-only | Reviews changes, finds bugs |
| `coder` | Write | Implements changes sequentially |
| `tester` | Write | Runs tests, validates changes |

### Web Tools (`src/tools/web_tools.py`)
- `web_search(query, max_results)` — DuckDuckGo search (duckduckgo-search package, HTML fallback); returns `{title, url, snippet}` list
- `read_web_page(url)` — fetches and extracts text (up to 10,000 chars); uses html2text when available

**SSRF protection:** `_is_url_blocked()` rejects non-HTTP schemes (`file://`, `ftp://`, etc.) and private/internal IP ranges (`127.x`, `10.x`, `192.168.x`, `172.16-31.x`, `169.254.x`, `localhost`). Fails closed on parse error.

### AST Tools (`src/tools/ast_tools.py`)
- `ast_rename(path, old_name, new_name)` — renames a symbol using AST line-number discovery + word-boundary regex on affected lines only. Preserves all comments, blank lines, and formatting. Calls `mark_file_read()` + `check_read_before_write()` before writing (guardrail-compliant). For non-Python files falls back to full-text word-boundary regex.
- `ast_list_symbols(path, symbol_type)` — lists all function/class/variable definitions with line numbers. Python: full AST walk. Non-Python: regex fallback for JS/TS patterns.

### Interaction Tools (`src/tools/interaction_tools.py`)
- `ask_user(question)` — pauses execution and presents a question to the user via EventBus; blocks up to 5 minutes for a response. Unsubscribes in `finally` so no callback leaks on exception.
- `submit_plan_for_review(plan_summary, plan_steps, risk_level)` — HITL plan approval gate; blocks until user approves, rejects, or requests changes. Unsubscribes in `finally`.

### Project Tools (`src/tools/project_tools.py`)
- `fingerprint_tech_stack(workdir)` — detects languages (Python, JavaScript, TypeScript, Go, Rust, Java, Ruby, C/C++) and frameworks (FastAPI, Flask, Django, React, Vue, Express, etc.) from file patterns and config files. Uses separate `glob("**/*.ts")` and `glob("**/*.tsx")` calls (pathlib does not support `{}` brace expansion).

### Read-Before-Write Guardrail (`src/tools/guardrails.py`)
Enforces that every existing file must be read before it can be written. Dual-tracking for correctness across threading models:
- **ContextVar** (`_read_files_var`) — propagates through async chains
- **Global Lock-protected set** (`_global_read_files`) — visible from any thread, including `run_in_executor` workers on Python 3.11

`mark_file_read(path)` — called by `read_file`, `read_file_chunk`, and `ast_rename` after successful reads.
`check_read_before_write(path)` — called by all write tools; returns `{}` if OK or `{error, requires_read_first: True}`.
`reset_guardrail_state()` — called by `Orchestrator.start_new_task()` to clear state between tasks.

New files (non-existent on disk) are always allowed through without a prior read.

### Git Tools (`src/tools/git_tools.py`)
- `git_status`, `git_log`, `git_diff`, `git_commit`, `git_stash`, `git_restore` — structured git operations
- `get_git_diff` (in `system_tools.py`) — **deprecated**; use `git_diff` from `git_tools` instead

### Toolset Loader (`src/config/toolsets/loader.py`)
Loads YAML toolset files from `src/config/toolsets/`. Caches loaded toolsets. Role → toolset mapping: `coding`, `planning`, `debug`, `review`.

**Toolsets** (`src/config/toolsets/`):
- `coding.yaml`, `debug.yaml`, `review.yaml`, `planning.yaml`

---

## Inference Layer (`src/core/inference/`)

| File | Description |
|------|-------------|
| `llm_manager.py` | `ProviderManager` — provider registry, model discovery, `call_model()`, routing helpers. Singleton via `get_provider_manager()`. |
| `llm_client.py` | Abstract `LLMClient` base class — defines `generate()` and `agenerate()` interface. |
| `adapter_wrappers.py` | `AdapterWrapper` — wraps existing adapters into a uniform `generate()` API; normalizes model lists. |
| `adapters/openai_compat_adapter.py` | **Base class** for any OpenAI-compatible REST endpoint — full chat/completions wire protocol, model discovery, Bearer auth, streaming, tool_calls extraction. Extended by `LmStudioAdapter` and `OpenRouterAdapter`. |
| `adapters/lm_studio_adapter.py` | LM Studio HTTP adapter — extends `OpenAICompatibleAdapter`; overrides config loading (providers.json, env vars) and short-name model resolution. |
| `adapters/openrouter_adapter.py` | OpenRouter adapter — extends `OpenAICompatibleAdapter`; hardcoded BASE_URL, API key from UserPrefs, required `HTTP-Referer`/`X-Title` headers, public `/models` endpoint. |
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
| `workspace_guard.py` | Protected path patterns (`.git/`, `.env`, `pyproject.toml`, etc.) — blocks writes to critical files. |
| `role_config.py` | Role-based access control: planner (read-only), coder (full), reviewer (read+verify), researcher (search). `normalize_role()`, `CANONICAL_ROLES`, `ROLE_ALIASES`. |
| `tool_contracts.py` | Pydantic result schemas for specific tools (e.g. `ListFilesResult`). Validated in `execute_tool`. Includes `requires_split` flag for patch size guard. |
| `tool_schema.py` | Base `ToolContract` pydantic model: `{tool, args, result, error}`. |
| `file_lock_manager.py` | **Phase 6**: Async file locking for PRSW (Parallel Reads, Sequential Writes). Multiple read locks per file, single write lock. |
| `wave_coordinator.py` | **Phase 6**: Wave-based execution coordinator. Manages parallel read agents and sequential write agents. |
| `dag_parser.py` | DAG-based plan parsing. Converts flat plans to dependency graph, computes topological wave execution order. |
| `token_budget.py` | Token budget tracking per phase. Monitors prompt/completion tokens and warns approaching limits. |
| `prsw_topics.py` | **Phase 6**: PRSW event topics for multi-agent coordination (`files.ready`, `write.complete`, etc.). |
| `session_lifecycle.py` | Session lifecycle management. Handles graceful shutdown, snapshot creation on exit. |
| `session_registry.py` | Registry of active sessions. Tracks session IDs, timestamps, and metadata. |
| `agent_session_manager.py` | P2P agent session management. Enables cross-agent context sharing via EventBus. |
| `preview_service.py` | Preview mode service. Handles dry-run mode for validation without execution. |
| `plan_mode.py` | Plan-only mode. Parses and validates plans without executing them. |
| `cross_session_bus.py` | Cross-session event bus. Enables events to span multiple sessions. |
| `session_watcher.py` | File system watcher for session-related files. Triggers reload on file changes. |
| `token_budget.py` | Token budget monitor per phase. Records prompt/completion usage; warns approaching context window limits. |
| `mcp_stdio_server.py` | MCP STDIO server — bridges EventBus to stdin/stdout JSON-RPC 2.0; supports IDE integration (GAP 3). |

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
| `file.diff.preview` | `file_tools.write_file` | `{path, diff, is_new_file}` |
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

### PRSW Events (Phase 6)

| Event | Publisher | Payload |
|-------|-----------|---------|
| `prsw.files.ready` | Scout/Researcher | `{files, agent_id}` |
| `prsw.context` | Researcher | `{summary, agent_id}` |
| `prsw.changes` | Coder | `{files, status}` |
| `prsw.new_files` | Coder | `{files, agent_id}` |
| `prsw.blocked` | FileLockManager | `{path, agent_id}` |
| `prsw.write_done` | Coder | `{files, status, agent_id}` |

### Agent Topics (P2P)

| Event | Publisher | Payload |
|-------|-----------|---------|
| `agent.scout.broadcast` | Scout agent | `{files, agent_id}` |
| `agent.researcher.broadcast` | Researcher agent | `{summary, agent_id}` |
| `agent.reviewer.broadcast` | Reviewer agent | `{bugs, agent_id}` |
| `agent.tester.broadcast` | Tester agent | `{results, agent_id}` |

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

- **`workflow_nodes.py`** has been deleted. New code imports directly from individual node files in `graph/nodes/`.
- **`sandbox.py`** has been deleted. AST pre-write validation uses `ast.parse(new_content)` inline in `execute_tool` (C2 fix). `ExecutionSandbox` and `SelfDebugLoop` are both gone.
- **Toolset loader** is at `src/config/toolsets/loader.py` (canonical). `src/tools/toolsets/` (legacy) has been deleted.
- **`providers.json`** must be an array `[{...}]` not a top-level object.
- **`plan_validator_node.py`** exposes a standalone `validate_plan()` function; called directly by `planning_node` or `orchestrator` before executing a plan.
- **`replan_node`** uses the `strategic` role (not `planner`). Increments `replan_attempts` counter.
- **`graph/nodes/__init__.py`** does not exist; all node imports are explicit in `graph/builder.py`.
- **`advanced_features.py`** (`src/core/memory/`) provides `TrajectoryLogger`, `DreamConsolidator`, `ReviewAgent`, `RefactoringAgent`, `SkillLearner` — all used in `memory_update_node`.
- **Correlation IDs**: `new_correlation_id()` minted per agent turn; `event_bus.publish()` auto-stamps dict payloads; `call_model()` logs `cid=` for end-to-end tracing.
- **`call_graph` / `test_map`** flow: `analysis_node` populates → `AgentState` carries → `planning_node` injects as JSON blocks into the planning prompt (P3-1).
- **Phase 1 audit fixes (2026-03)**: P1-1 async delegation_node wrapped in LangGraph async wrapper; P1-2 plan_attempts counter prevents infinite planning→validator loop; P1-3 replan_attempts caps inner replan cycle at 5; P1-4 asyncio.Event created lazily in preview_service; P1-5 duplicate code removed from todo_tools; P1-6 plan_enforce_warnings defaults False to avoid infinite loop; P1-7 atomic providers.json write via tmp-file + rename; P1-8 Textual shutdown in finally block.
- **Phase 2 audit fixes (2026-03)**: P2-1 retry logic in openai_compat_adapter; P2-2 token budget wired to distiller via should_after_execution_with_replan; P2-5 run_tests uses _safe_resolve_workdir; P2-6 edit_by_line_range coerces start_line to int; P2-9 plan_mode_approved reset to None in planning_node.
- **TokenBudgetMonitor**: Integrated into graph flow via builder.py `should_after_execution_with_replan` — checks budget and routes to memory_sync for compaction when usage exceeds threshold.
- **Session Hydration**: TUI publishes `session.request_state` on mount; AgentSessionManager responds with `session.hydrated` containing full state; orchestrator calls `_sync_session_state()` after tool execution.

---

## Textual TUI (`tui/`)

The TUI is a fully decoupled sub-project built with the [Textual](https://textual.textualize.io/) framework. It lives in `tui/` with its own `pyproject.toml` and communicates with `src/core` exclusively through `core_bridge.py`.

### Directory Layout

```
tui/
├── pyproject.toml
└── src/ui/
    ├── app.py                  # AgentApp — main Textual application (2116 lines)
    ├── core_bridge.py          # Bridges EventBus ↔ Textual message bus
    ├── bus.py                  # 40+ typed backend→UI message events
    ├── events.py               # UI→backend message events
    ├── logging.py              # InMemoryHandler ring buffer (RuntimeError guards)
    ├── settings.py             # SettingsStore — persists to ~/.agent_tui/settings.json
    ├── mock_engine.py          # Mock backend for development/demo
    ├── components/
    │   ├── stream_view.py      # Token-streaming text widget
    │   ├── agent_artifact.py   # Rendered file diff / code artifact card
    │   ├── thinking_process.py # Collapsible thinking-token display
    │   ├── console_panel.py    # Log/event console overlay
    │   ├── history_input.py    # Up/down history input widget
    │   ├── chat_textarea.py    # Multi-line chat input area
    │   ├── file_picker_overlay.py  # Fuzzy-search file picker
    │   └── side_by_side_diff.py    # Unified diff renderer
    ├── features/
    │   ├── palette/            # ctrl+o command palette modal
    │   └── settings/           # Settings screen + provider config screen
    └── screens/
        ├── session_list.py     # Session picker screen
        ├── timeline.py         # Event timeline screen
        └── probe_results.py    # Test/lint probe results screen
```

### Key Bindings

| Key | Action |
|-----|--------|
| `ctrl+o` | Open command palette |
| `ctrl+s` | Open settings |
| `ctrl+l` | Toggle console panel |
| `tab` | Cycle through roles (operational / analyst / reviewer) |
| `Esc Esc` | Interrupt running agent |
| `ctrl+q` | Quit |

### Slash Commands

`/help` `/clear` `/new` `/compact` `/continue` `/interrupt` `/status` `/fast` `/provider` `/model` `/settings` `/sessions` `/timeline` `/diff` `/fork` `/mcp` `/quit`

### Architecture Notes

- **`AgentApp`** (`app.py`): Single Textual `App` subclass. Manages session lifecycle, slash command dispatch, cost display, and reactive state. Cost rate constants: `_COST_INPUT_PER_1K = 0.001`, `_COST_OUTPUT_PER_1K = 0.003`. Token tracking uses `_session_input_tokens` / `_session_output_tokens` (LOW-12 fix: no tool token double-count).
- **`core_bridge.py`**: Subscribes to EventBus in a background asyncio task; posts typed Textual messages to the UI thread via `app.call_from_thread()`. Fixed `os.close(fd)` leak on cancelled tasks.
- **`bus.py`**: 40+ dataclass message types (e.g. `AgentThinking`, `ToolCallStart`, `ToolCallFinish`, `PlanUpdated`, `SessionHydrated`, `CostUpdated`, `ErrorOccurred`).
- **`settings.py`** / **`SettingsStore`**: Atomic JSON write; provider, model, and appearance preferences.
- **`mock_engine.py`**: Fully-functional mock backend for TUI development without a running model.

---

## New Inference Adapters (`src/core/inference/adapters/`)

### GitHub Copilot Adapter (`github_copilot_adapter.py`)

Extends `OpenAICompatibleAdapter`. Uses RFC 8628 device-flow OAuth to authenticate with GitHub and obtain a Copilot API token.

| Symbol | Description |
|--------|-------------|
| `GithubCopilotAdapter` | Adapter class; `AUTH_FLOW = "github_device"`; `COPILOT_BASE_URL` constant |
| `github_copilot_auth.py` | `start_device_flow()`, `poll_for_token()`, `save_token()`, `load_token()`, `is_authenticated()` |
| Token storage | `~/.agent/copilot_token.json` via `credentials.py` (`os.chmod(0o600)` on write) |
| Error handling | RFC 8628 error codes: `authorization_pending`, `slow_down`, `expired_token`, `access_denied` |

The settings controller (`src/core/settings/controller.py`) handles `SaveProviderCredentials` messages and triggers the device-flow when the provider is `github_copilot`.

### Anthropic Adapter (`anthropic_adapter.py`)

Extends `OpenAICompatibleAdapter`. Overrides `_preprocess_messages()` to split the message list at the `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` sentinel and attach `cache_control: {"type": "ephemeral"}` to the last message of the static block. This enables Anthropic prompt caching (CP-12 fix).

### Mock Adapter (`mock_adapter.py`)

Deterministic mock adapter for unit/integration tests. Returns configurable response fixtures. Used by `test_mock_adapter_integration.py` and delegation mock integration tests.

### Model Tiers (`model_tiers.py`)

```python
class ModelTier(Enum):
    NANO     = "nano"      # sub-1B, fastest, cheapest
    STANDARD = "standard"  # 7B–13B
    LARGE    = "large"     # 30B–70B
    FRONTIER = "frontier"  # 100B+, API-only
```

`get_model_tier(model_name: str) -> ModelTier` — heuristic mapping based on model name substrings.

### Tokenizer (`tokenizer.py`)

Token counting without a `tiktoken` dependency. Uses character-based estimation for non-OpenAI models and the `cl100k_base` approximation for OpenAI-compatible endpoints.

---

## New Orchestration Services (`src/core/orchestration/`)

### ApprovalGate (`approval_gate.py`)

Non-polling asyncio permission gate. Two gate instances:

| Gate | Purpose |
|------|---------|
| `bash_gate` | Blocks bash tool execution pending user approval |
| `tool_gate` | Blocks arbitrary tool execution pending user approval |

Both gates use a shared `_gate_lock: asyncio.Lock`. `AsyncGate.wait_async()` suspends the caller until `AsyncGate.allow()` or `AsyncGate.deny()` is called from the TUI thread.

### LoopGuards (`loop_guards.py`)

Centralised loop-prevention checks called from `tool_execution_service.pre_execute()`:

| Function | Description |
|----------|-------------|
| `check_read_before_write(tool_name, path, state)` | Enforces read-before-write for modifying tools |
| `check_cooldown(tool_name, path, state)` | Enforces `COOLDOWN_GAP=3` between identical read calls |
| `check_doom_loop(tool_name, args, state, policy)` | Queries `PermissionPolicy.check_doom_loop()`; blocks if pattern triggers PERM-02 |

### EventLog (`event_log.py`)

Append-only SQLite event log with WAL mode. Per-session sequence counter.

| Symbol | Description |
|--------|-------------|
| `EventKind` | Enum: `TOOL_CALL`, `LLM_CALL`, `PLAN_STEP`, `ERROR`, `SCOPE_VIOLATION`, `BUDGET_ALERT` |
| `EventLog.open(path)` | Opens (creates) the SQLite DB; enables WAL; creates `events` table |
| `EventLog.append(session_id, kind, payload)` | Atomically inserts event; increments per-session seq |
| `EventLog.get_events(session_id, after_seq)` | Returns events for a session after a given seq |
| `EventLog.get_diff(seq_a, seq_b)` | Returns events between two sequence numbers |
| `EventLog.count(session_id)` | Returns event count for a session |
| Connection guard | All query methods raise `RuntimeError("EventLog: database connection is not open")` if `_conn is None` (MED-8 fix) |

### ShellHooks (`shell_hooks.py`)

`ShellHookRunner` implements CP-7 shell hooks. Pre- and post-tool hooks are configured in `.agent/settings.json`. Hook scripts receive a JSON payload on stdin. Exit code semantics:

| Exit code | Meaning |
|-----------|---------|
| 0 | Allow / success |
| 2 | **Deny** — tool execution is blocked; reason from stdout is surfaced to the user |
| other | Warning logged; execution continues |

Hook commands are parsed with `shlex.split()` (never `shell=True`).

### SnapshotManager (`snapshot_manager.py`)

Git-based file snapshots using `--sparse` checkout. Sanitises `project_id` via `_PROJECT_ID_RE` regex before using it in git commands (P2-3 fix). `restore()` runs `git clean -fd` after `checkout-index` to remove untracked files left by partial writes (P2-4 fix). Git subprocess timeout: `_GIT_TIMEOUT = 60.0` with `asyncio.wait_for` + kill (P1-2 fix).

### ToolExecutionService (`tool_execution_service.py`)

Async pre/post execution pipeline:

```
pre_execute:
  1. reset_idempotency() called at turn start (HIGH-3 fix)
  2. check idempotency guard (_seen_calls set)
  3. check_read_before_write (loop_guards)
  4. check_cooldown (loop_guards)
  5. check_doom_loop (loop_guards + permission_policy)
  6. await approval_gate.wait_async() (async permission gate, P1-1 fix)
  7. run pre-tool shell hooks (shell_hooks)

post_execute:
  1. run post-tool shell hooks (shell_hooks)
  2. record_tool_call in cost_tracker
  3. append to event_log
```

### ToolHooks (`tool_hooks.py`)

`ToolHookRunner` — Python-level pre/post hooks (as opposed to shell hooks). Hook callables are registered per tool name. Used for internal audit instrumentation.

### PermissionPolicy (`permission_policy.py`)

```python
class Behavior(Enum):
    ALLOW = "allow"
    ASK   = "ask"
    DENY  = "deny"
```

`PermissionPolicy` holds a mapping of `tool_name → Behavior`. `check_doom_loop(tool_name, args)` is consulted by `loop_guards.check_doom_loop()` to determine whether a repeated tool call should be blocked (PERM-02 policy).

### SessionCostTracker (`session_cost_tracker.py`)

Thread-safe cost tracking with atomic JSON flush.

| Method | Description |
|--------|-------------|
| `record_tool_call(tool_name, input_tokens, output_tokens)` | Lock-guarded; appends to `_usage_buffer` |
| `flush(task_id)` | Atomic write via `mkstemp` + `os.replace`; clears buffer inside lock after snapshot (HIGH-2 fix) |
| `reset()` | Lock-guarded; clears buffer and resets counters |
| `budget_ceiling_usd` | From `ProjectSettings`; triggers `BUDGET_ALERT` event when exceeded |

### InstructionLoader (`instruction_loader.py`)

Builds the runtime context block injected into the system prompt each turn:
- Reads `AGENTS.md` / instruction files from workspace root and ancestor dirs
- Injects LSP diagnostics block via `lsp_context.get_lsp_diagnostics_block()` (CP-10 fix)
- Respects 4K/12K token budget caps

### ProjectSettings (`project_settings.py`)

Loads `.agent/settings.json` from the project root. Supports:
- `model` override
- `budget_ceiling_usd` (wired to `SessionCostTracker`)
- Permission mode overrides (deep merge with global config)

### DeferredInit (`deferred_init.py`)

Runs at startup after the event loop is running:
- World-writable directory check using both `os.lstat()` and `Path.stat()` (TS-1 fix)
- Plugin loader — refuses world-writable plugin dirs (P1-2 fix)

### PreviewCoordinator (`preview_coordinator.py`)

Preview gate for file diffs. `_on_confirmed` / `_on_rejected` validate non-empty `path` before calling `resolve_preview_gate` (P1-1 fix).

---

## Sandbox (`src/tools/sandbox.py`)

`run_sandboxed(cmd, level)` executes shell commands inside a `bwrap` (bubblewrap) sandbox.

| Level | Description |
|-------|-------------|
| `off` | No sandboxing (default for development) |
| `workspace` | Read-only bind for `/`, read-write bind for workspace only |
| `full` | Minimal filesystem; `--dev-bind /dev /dev` for device access |

Sandbox level is configurable via `ProjectSettings` or the `CODINGAGENT_SANDBOX` environment variable.

---

## Additional Tools (`src/tools/`)

| Module | Description |
|--------|-------------|
| `bash_security.py` | Bash security utilities extracted from `_security.py`; `SAFE_COMMANDS`, `GIT_SAFE_SUBCOMMANDS`, `SED_WRITE_FLAGS` |
| `batch_tools.py` | `batch_tool_calls(calls)` — parallel execution capped at `min(len(calls), MAX_PARALLEL)` |
| `lsp_tools.py` | `lsp_diagnostics`, `lsp_goto_definition`, `lsp_references` — LSP tool wrappers |
| `skill_tools.py` | `load_skill(name)`, `list_skills()` — skill management tools |
| `plan_mode_tools.py` | Tools available only in plan-only mode |
| `permission_context.py` | Per-call permission context dataclass |
| `_approval.py` | Tool approval helpers; integrates with `approval_gate` |
| `_result.py` | Structured tool result types (`ToolResult`, `ToolError`, `ToolDenied`) |
| `_truncate.py` | Output truncation utilities; respects max-chars limits |

---

## LSP Integration (`src/core/indexing/`)

| File | Description |
|------|-------------|
| `lsp_client.py` | `LSPClient` — JSON-RPC 2.0 LSP client; auto-restart on crash with exponential backoff (P4-2 fix); `_shutting_down` guard prevents restart loops; `_reader_loop` `finally` drains pending futures (P1-3 fix) |
| `lsp_context.py` | `get_lsp_diagnostics_block()` — formats LSP diagnostics as a named section for injection into the system prompt (CP-10 fix) |
| `lsp_manager.py` | `LSPManager` — manages per-language LSP server instances; reads `src/config/lsp_servers.yaml` |

---

## MCP Client (`src/core/mcp/`)

```
src/core/mcp/
├── __init__.py
├── mcp_client.py    # MCPClient — JSON-RPC 2.0 over stdio; connect/disconnect lifecycle
└── mcp_manager.py   # MCPManager — multiple server connections; tool routing
```

Key fixes: nested `asyncio.wait_for` removed from `_initialize()` (P2-1 fix); reader task + process terminated on connect failure (P2-5 fix).

---

## Config & Credentials

| File | Description |
|------|-------------|
| `src/core/config_loader.py` | `ConfigLoader` + `ConfigWatcher` — live config reload on file change (P4-1 fix); debounced `watchdog` observer |
| `src/core/credentials.py` | `save_credentials()` / `load_credentials()` — `os.chmod(_PREFS_PATH, 0o600)` after every write (P2-2 fix) |
| `src/core/errors.py` | Centralised error type hierarchy (`AgentError`, `ToolError`, `PermissionError`, `BudgetExceededError`, etc.) |

---

## System Prompt Builder (`src/core/prompts/`)

`system_prompt_builder.py` — `SystemPromptBuilder.build(role, runtime_context)`:
1. Loads identity files (`LAWS.md`, `SOUL.md`)
2. Loads role file (e.g. `operational.md`)
3. Inserts `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` sentinel between static and dynamic sections
4. Appends runtime context block from `InstructionLoader`
5. Appends LSP diagnostics block if available

The `AnthropicAdapter` uses the sentinel position to attach `cache_control` (CP-12 fix).
