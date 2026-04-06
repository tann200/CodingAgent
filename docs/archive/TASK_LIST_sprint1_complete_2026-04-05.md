# Merged Task List: CodingAgent — claw/opencode Parity
*Merged from `docs/audit/dev-plan-claw-parity.md` and `docs/audit/mitigation-plan-opencode-claw-parity.md`*
*Last validated: 2026-04-05*

Legend: ✅ Done · ❌ Todo · 🔄 Partial

---

## Workstream A — TUI Integration

| ID | Task | Status |
|----|------|--------|
| TUI-01 | Swap mock → real Orchestrator + EventBus in `core_bridge.py` | ✅ |
| TUI-02 | Event name translation shim (`_EVENT_MAP`) in `core_bridge.py` | ✅ |
| TUI-03 | Bash tier-3 approval gate — backend pause/resume (`approval_gate.py`) | ✅ |
| TUI-04 | Tool permission gate — backend pause/resume (`approval_gate.py`) | ✅ |
| TUI-05 | Diff preview blocking flow — await accept/reject in `file_tools.py` | ✅ |
| TUI-06 | Session list schema alignment + bridge resume (`session_store.py`) | ✅ |
| TUI-07 | Git branch status publishing (`orchestrator._publish_git_status`) | ✅ |
| TUI-08 | MCP server status publishing (`mcp_stdio_server.py`) | ✅ |
| TUI-09 | Token budget + usage event publishing (`token.budget`) | ✅ |
| TUI-10 | Reasoning/thinking stream events (`is_reasoning` field routing) | ✅ |
| TUI-11 | System settings handshake on startup | ✅ |

---

## Workstream B — Autonomy + claw Parity

| ID | Task | Status |
|----|------|--------|
| AUTO-01 | `autonomous_mode` flag — bypass all approval gates | ✅ |
| AUTO-02 | Per-role autonomy config (max_turns, permission_mode) | ✅ |
| AUTO-03 | TUI role → CodingAgent system prompt mapping | ✅ |
| TASK-01 | Wire PermissionLevel enforcement in `execute_tool()` | ✅ |
| TASK-03 | Plugin–builtin conflict detection (`_origins` in `_registry.py`) | ✅ |
| TASK-04 | Secure keychain credential storage (`src/core/credentials.py`) | ✅ |
| TASK-05 | Session persistence — enrich snapshot + `session_store.py` | ✅ |
| TASK-07 | Token-based compaction trigger (replaces message-count) | ✅ |
| TASK-08 | Compact summary as System message + continuation signal | ✅ |
| TASK-09 | `--allowed-tools` / `--deny-tool` CLI flags | ✅ |
| TASK-10 | `init` subcommand | ✅ |
| TASK-11 | Deferred feature gating (`src/core/orchestration/deferred_init.py`) | ✅ |
| TASK-12 | `max_turns` guard at Orchestrator level (pre-graph check) | ✅ |
| TASK-13 | Hook scripts run async (asyncio.create_subprocess_exec) | ✅ |
| TASK-14 | Structured patch hunks in edit tools (`generate_unified_diff`) | ✅ |
| TASK-16 | Alias resolution at startup in `build_registry()` | ✅ |
| TASK-17 | Glob truncation reporting (`truncated` + `total_found` fields) | ✅ |
| TASK-18 | Model pricing table in `provider_context.py` | ✅ |
| TASK-20 | `--permission-mode` CLI flag | ✅ |
| TASK-21 | Outbound MCP client stdio (`src/core/mcp/mcp_client.py`) | ✅ |
| TASK-22 | `system-prompt` debug subcommand | ✅ |

---

## Workstream C — `src/ui/` Retirement

| ID | Task | Status |
|----|------|--------|
| LEGACY-01 | Freeze `src/ui/` — add deprecation headers | ✅ |
| LEGACY-02 | Migrate 16 test files (~4 800 lines) to `core_bridge` fixtures | ✅ |
| LEGACY-03 | Delete `src/ui/` after Sprint 1 gate | ✅ |
| LEGACY-04 | Absorb unique `src/ui/` logic (`_HistoryWrapper`, `_compute_cost`) | ✅ |

---

## Workstream D — Python Best Practices + Agentic Architecture

| ID | Task | Status |
|----|------|--------|
| D-01 | Structured error types — `src/tools/_result.py` (ToolResult + ErrorCode) | ✅ |
| D-02 | AgentState validation — `validate_state()` in `graph/state.py` | ✅ |
| D-03 | Retry decorator — `src/core/utils/retry.py` | ✅ |
| D-04 | Tool idempotency guards (write, git_commit, manage_todo) | ✅ |
| D-05 | Prompt templates — `src/core/prompts/` package + `templates/` | ✅ |
| D-06 | Token counting — `src/core/inference/tokenizer.py` (tiktoken, replaces len/4) | ✅ |
| D-07 | Correlation IDs propagated to executor threads (`run_with_correlation`) | ✅ |
| D-08 | Async correctness — LRU-cached role file reads in `context_builder.py` | ✅ |
| D-09 | Doom-loop + cooldown guards extracted to `loop_guards.py` | ✅ |
| D-10 | Orchestrator decomposition (ToolExecutionService, SessionCostTracker, PreviewCoordinator) | ✅ |

---

## Sprint S0 — Token Accuracy, Typed Errors, Mock LLM

| ID | Task | Status |
|----|------|--------|
| S0-A | Replace `len(s)//4` with tiktoken — `src/core/inference/tokenizer.py` | ✅ |
| S0-B | Typed error hierarchy — `src/core/errors.py` (AgentError + ErrorCode enum) | ✅ |
| S0-C | Mock LLM adapter for CI — `src/core/inference/adapters/mock_adapter.py` | ✅ |

*Note: D-06 and S0-A are the same deliverable (tokenizer.py).*

---

## Sprint S1 — Model-Tier Routing + Per-Model Prompt Templates

| ID | Task | Status |
|----|------|--------|
| S1-A | Model capability tiers — `src/core/inference/model_tiers.py` (ModelTier enum, classify_model) | ✅ |
| S1-B | Per-provider system prompt partials in `src/config/agent-brain/prompts/` | ✅ |
| S1-C | Dynamic tool list pruning in `ContextBuilder._select_tools()` | ✅ |

---

## Sprint S2 — LSP Tools

| ID | Task | Status |
|----|------|--------|
| S2-A | LSP client — `src/core/indexing/lsp_client.py` + `lsp_manager.py` | ✅ |
| S2-B | LSP tools — `src/tools/lsp_tools.py` (diagnostics, refs, definition, symbols, hover, rename) | ✅ |
| S2-C | Auto-formatter on write (`formatters.yaml` + hook in write_file/edit_file_atomic) | ✅ |

---

## Sprint S3 — MCP Client (stdio + HTTP/SSE)

| ID | Task | Status |
|----|------|--------|
| S3-A | MCP client — `src/core/mcp/client.py` + `manager.py` | ✅ |
| S3-B | Config schema extension (`.agent/config.json` `mcp` section) | ✅ |
| S3-C | In-TUI MCP commands (`/mcp list`, `/mcp add`, `/mcp status`) | ✅ |

*Note: TASK-21 covers S3-A stdio transport specifically.*

---

## Sprint S4 — Git Workspace Snapshots + Event Log

| ID | Task | Status |
|----|------|--------|
| S4-A | Git snapshot system — `src/core/orchestration/snapshot_manager.py` | ✅ |
| S4-B | Immutable event log — `src/core/orchestration/event_log.py` (SQLite append-only) | ✅ |

---

## Sprint S5 — Session Fork / Revert + Session Diff

| ID | Task | Status |
|----|------|--------|
| S5-A | Session fork — `fork_session()` in `session_store.py` | ✅ |
| S5-B | Session revert — `revert_session()` using GitSnapshotManager | ✅ |
| S5-C | Session diff — `/diff` TUI slash command + `EventLog.get_diff()` | ✅ |

---

## Sprint S6 — Cost Tracking + Per-Provider Prompts + Config Hot-Reload

| ID | Task | Status |
|----|------|--------|
| S6-A | Cost tracking — `estimate_turn_cost()` + `session_cost_usd` in AgentState | ✅ |
| S6-B | Per-provider prompts wired to ContextBuilder (requires S1-B wiring) | ✅ |
| S6-C | Config hot-reload (`watchfiles` watching `providers.json`, `.agent/config.json`) | ✅ |

---

## Sprint S7 — AST Bash Security + Non-Polling Permission Gate

| ID | Task | Status |
|----|------|--------|
| S7-A | AST-level bash security — `src/tools/bash_security.py` (BashRiskLevel, shlex analysis) | ✅ |
| S7-B | Non-polling permission gate (threading.Event → asyncio.Event) | ✅ |

---

## Sprint S8 — Dynamic Tool Pruning + Schema Stripping

| ID | Task | Status |
|----|------|--------|
| S8-A | Schema stripping for NANO/SMALL tiers (YAML minimal vs JSON schema) | ✅ |
| S8-B | `simple_mode` for NANO tier (8 core tools, one-tool-per-message) | ✅ |
| S8-C | `/fast` and `/model` slash commands in `chat_input.py` | ✅ |

---

## Sprint S9 — Cross-Session Memory Injection + `/compact`

| ID | Task | Status |
|----|------|--------|
| S9-A | Cross-session memory injection at session start (`inject_prior_session_memories`) | ✅ |
| S9-B | `/compact` user command — immediate context distillation | ✅ |

---

## Sprint S10 — Hook Verification + `stuck` Skill + Golden File Tests

| ID | Task | Status |
|----|------|--------|
| S10-A | Verify and fix tool hook dispatch (`pre_tool.sh`, `post_tool.sh`) | ✅ |
| S10-B | `stuck` auto-recovery skill (`src/config/agent-brain/skills/stuck.md`) | ✅ |
| S10-C | Golden file tests for system prompts (`tests/unit/test_system_prompts_golden.py`) | ✅ |
| S10-D | Mock LLM adapter in 5 key integration tests (requires S0-C) | ✅ |

---

## Execution Order (Next Steps)

**Immediate (unblocking CI and correctness):**
1. D-06 / S0-A — `tokenizer.py` (tiktoken, replaces all `len/4` heuristics)
2. S0-B — `src/core/errors.py` (AgentError typed hierarchy)
3. S0-C — `src/core/inference/adapters/mock_adapter.py`
4. D-07 — correlation ID propagation to executor threads

**Short-term (model scaling):**
5. S1-A — `model_tiers.py`
6. S1-B — wire prompt partials to ContextBuilder
7. S1-C — dynamic tool pruning in ContextBuilder
8. S8-A/B — schema stripping + simple_mode for NANO

**Medium-term (feature parity):**
9. TASK-04 — credentials/keyring
10. TASK-11 — deferred_init.py
11. TASK-21 / S3-A — MCP client
12. S7-A — AST bash security
13. S6-A — complete cost tracking (session_cost_usd)
14. S6-C — config hot-reload
15. S9-A/B — memory injection + /compact

**Longer-term (advanced):**
16. S2 — LSP tools
17. S4 — Git snapshots + event log
18. S5 — Session fork/revert
19. D-10 — Orchestrator decomposition
20. S10 — Hooks, stuck skill, golden tests
