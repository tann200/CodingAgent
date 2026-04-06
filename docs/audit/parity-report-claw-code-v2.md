# Parity Gap Report: CodingAgent vs. claw-code v2

**Date:** 2026-04-06
**Prepared by:** Deep-dive review of all live Rust source files in `/Users/tann200/PycharmProjects/claw-code-main/`
**Reference:** `docs/audit/deep-dive-claw-code-architecture.md` (v2)
**Basis for comparison:** CodingAgent at Stage 34 / vol14 baseline (2617 tests passing)

---

## Executive Summary

CodingAgent is broadly superior to claw-code in pipeline sophistication (LangGraph, multi-node planning, DAG execution, 60 tools vs 19), memory architecture (VectorStore, SymbolGraph, TrajectoryLogger), and protocol compliance (ACP/MCP GAP 1–3). However, claw-code has several **focused, implementable features** that CodingAgent lacks. All are low-to-medium complexity.

---

## Section 1: Areas Where CodingAgent Is Ahead

These are deliberate architectural choices or additional features not present in claw-code.

| Feature | CodingAgent | claw-code |
|---------|-------------|-----------|
| LangGraph multi-node pipeline | ✅ Planning → Execution → Verification → Replan | Single `run_turn()` loop |
| Planning and analysis nodes | ✅ `planning_node`, `analysis_node`, `analyst_delegation_node` | None |
| Vector memory | ✅ `VectorStore`, `SymbolGraph`, SHA-256 incremental indexing | None |
| Skill system | ✅ YAML skill definitions, `SkillLearner` | `Skill` tool only (loads JSON) |
| Tool count | ✅ 60 tools across 16 modules | 19 built-in tools |
| ACP payload schema | ✅ GAP 2 compliant (sessionUpdate, toolCallId, status, content) | No ACP compliance |
| MCP STDIO server (inbound) | ✅ `mcp_stdio_server.py` for IDE integration | No STDIO server mode |
| DAG-based wave execution | ✅ `WaveCoordinator`, `FileLockManager`, PRSW | None |
| Session state hydration | ✅ GAP 1: `session.request_state` / `session.hydrated` | None |
| Evaluation framework | ✅ `ScenarioEvaluator`, SWE-bench style | None |
| EventBus / TUI dashboard | ✅ Full Textual TUI + EventBus pub/sub | Terminal rendering only |
| Role-based prompt injection | ✅ 5 named roles (strategic, operational, analyst, debugger, reviewer) | Single system prompt |
| Temperature routing | ✅ Per-node temperature (0.3 planning, 0.0 execution) | Single temperature |
| Thinking-token stripping | ✅ `strip_thinking()`, DeepSeek-R1 / Qwen3 compatibility | None |
| Multi-file atomicity | ✅ `RollbackManager` step-level snapshots | None |
| Prompt injection guard | ✅ Tool name in user history → reject | None |
| Multi-provider support | ✅ OpenAI, Anthropic, Ollama, LM Studio, xAI adapters | Anthropic + OpenAI-compat only |
| Idempotency guard | ✅ `_seen_calls` per turn (vol14) | None |

---

## Section 2: Areas Where claw-code Is Ahead (Gap Items)

These are features claw-code has that CodingAgent does not, and that are worth implementing.

### CP-6: Deterministic Context Compaction

**Status:** Missing in CodingAgent

**claw-code implementation:**
- `compact.rs` — `CompactionConfig { preserve_recent_messages: 4, max_estimated_tokens: 10_000 }`
- `should_compact()` fires automatically after every turn, no LLM call required
- `compact_session()` produces structured summary with 7 named sections (scope stats, tool names, recent user requests, pending work, key files, current work, key timeline)
- `merge_compact_summaries()` stacks summaries on repeated compaction
- Continues with: "Resume directly — do not acknowledge the summary…"

**CodingAgent current state:**
- `/compact` slash command only (manual trigger)
- `distiller.py` `compact_messages_to_prose()` requires an LLM call
- Produces prose, not structured sections
- `TokenBudgetMonitor` routes to `memory_sync` but does not call a compaction method automatically

**Gap:** Automatic threshold-based compaction without LLM cost. Priority: **Medium**

---

### CP-7: Shell Hook System with Deny Semantics

**Status:** Missing in CodingAgent

**claw-code implementation (`hooks.rs`):**
- Hooks are shell command strings in `.claw/settings.json`
- `PreToolUse` and `PostToolUse` arrays
- Exit code `0` = allow, `2` = **deny** (blocks tool / marks result as error), other = warn (appends feedback)
- Stdin payload: `{ hook_event_name, tool_name, tool_input, tool_input_json, tool_output, tool_result_is_error }`
- Env vars: `HOOK_EVENT`, `HOOK_TOOL_NAME`, `HOOK_TOOL_INPUT`, `HOOK_TOOL_IS_ERROR`, `HOOK_TOOL_OUTPUT`
- Deny on pre-hook blocks the tool call entirely
- Deny on post-hook marks `is_error = true` on the result

**CodingAgent current state:**
- `deferred_init.py` has a Python plugin hook mechanism
- Pre-tool only (no post-tool hooks)
- No "deny" exit code convention
- Hooks are Python callables, not shell commands
- Not configurable from a per-project settings file

**Gap:** stdin-JSON shell hooks with deny semantics for both pre and post. Priority: **Medium**

---

### CP-8: Per-Tool Runtime-Configurable Permission Policy

**Status:** Partially missing in CodingAgent

**claw-code implementation (`permissions.rs`):**
- `PermissionMode` enum: `ReadOnly | WorkspaceWrite | DangerFullAccess | Prompt | Allow`
- Every tool has a `required_permission` level in `ToolSpec`
- `PermissionPolicy` per session enforces level escalation (workspace → danger triggers prompt)
- `Prompt` mode asks user for every tool
- `Allow` mode bypasses all checks
- Permission mode readable from `.claw/settings.json` (`permissionMode`, `permissions.defaultMode`)

**CodingAgent current state:**
- T1/T2/T3 tier classification in `_security.py` (bash commands only)
- No `PermissionMode` per tool concept
- No session-level permission policy object
- No config-file-loaded permission mode
- `WorkspaceGuard` exists but is path-based not permission-mode based

**Gap:** Unified `PermissionMode` per tool + policy object + config-file-loadable session mode. Priority: **Low**

---

### CP-9: Prompt Cache Token Tracking

**Status:** Missing in CodingAgent

**claw-code implementation (`usage.rs`):**
- `TokenUsage { input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens }`
- All four components tracked and serialized per assistant message
- `UsageTracker::from_session()` reconstructs cumulative usage from existing messages on session resume
- Cost estimate accounts for cache pricing separately

**CodingAgent current state:**
- `SessionCostTracker` tracks `input_tokens` + `output_tokens` (vol14 wired)
- No `cache_creation_input_tokens` or `cache_read_input_tokens` fields
- Does not reconstruct usage from session on resume

**Gap:** Add two cache token fields to `SessionCostTracker`; reconstruct on resume. Priority: **Low**

---

### CP-10: LSP Diagnostics Injected into System Prompt

**Status:** Missing in CodingAgent

**claw-code implementation (`lsp/`, `prompt.rs`):**
- `LspManager.context_enrichment(path, position)` returns `LspContextEnrichment { diagnostics, definitions, references }`
- `SystemPromptBuilder::with_lsp_context(enrichment)` appends this as a named section in the system prompt
- Agent sees live type errors, go-to-definition results, and reference locations inline in its context

**CodingAgent current state:**
- `lsp_client.py` implements `go_to_definition` and `find_references` ✓
- Collects diagnostics ✓
- **Does not inject any LSP data into the system prompt** ✗ — it is available but unused

**Gap:** Wire `lsp_client.py` output into `context_builder.py` as a named system prompt section. Priority: **Low**

---

### CP-11: Project Instruction File Discovery (`CLAW.md` / `AGENTS.md`)

**Status:** Partially implemented in CodingAgent

**claw-code implementation (`prompt.rs`):**
- Walks all ancestor directories looking for: `CLAW.md`, `CLAW.local.md`, `.claw/CLAW.md`, `.claw/instructions.md`
- Deduplicates via `stable_content_hash()` (fast non-cryptographic hash)
- Per-file budget: 4,000 chars; total budget: 12,000 chars
- Files included as named sections with path labels

**CodingAgent current state:**
- Reads `AGENTS.md` at workspace root (this session uses it) ✓
- Does not walk ancestor directories for additional instruction files
- No deduplication logic
- No per-file or total budget cap

**Gap:** Ancestor directory walk + dedup + budget caps for instruction files. Priority: **Low**

---

### CP-12: `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` for Prompt Caching

**Status:** Not implemented in CodingAgent (only a deferred note in IMPL_PLAN)

**claw-code implementation (`prompt.rs`):**
- `SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"` constant
- Inserted between static (cacheable) and dynamic (per-turn) system prompt sections
- API client reads this boundary to set Anthropic `cache_control: { type: "ephemeral" }` breakpoints
- Reduces prompt cache misses — only the dynamic tail re-fills the cache

**CodingAgent current state:**
- `context_builder.py` assembles the system prompt but has no cache boundary concept
- Anthropic adapter does not set `cache_control` on prompt segments

**Gap:** Insert boundary constant; wire Anthropic adapter to set `cache_control` at boundary. Priority: **Low** (reduces cost, not functionality)

---

### CP-13: Project-Level Settings File (`.agent/settings.json`)

**Status:** Not implemented in CodingAgent

**claw-code implementation (`config.rs`):**
- 5-layer config with deep merge: user-legacy → user-canonical → project-legacy → project-canonical → project-local
- Project settings in `{cwd}/.claw/settings.json` — per-project model, hooks, permission mode, MCP servers, sandbox
- Local overrides in `{cwd}/.claw/settings.local.json` (typically gitignored)
- Full deep merge for nested objects; last-write-wins for scalars

**CodingAgent current state:**
- Global `src/config/providers.json` for provider list
- Global `~/.config/codingagent/prefs.json` via `UserPrefs`
- No project-level settings file
- No config layering

**Gap:** Per-project `.agent/settings.json` with at least model and permission-mode overrides. Priority: **Low**

---

### CP-14: Session `version` Field for Migration

**Status:** Not implemented in CodingAgent

**claw-code implementation (`session.rs`):**
- `Session { version: u32, messages: Vec<ConversationMessage> }` — `version` starts at 1
- Allows `from_json()` to apply migration logic for older sessions

**CodingAgent current state:**
- `MessageManager` manages a plain `list[dict]`; no version field

**Gap:** Add `version` field to `MessageManager` / session persistence. Priority: **Low** (maintenance insurance)

---

### CP-15: Agent-to-User Messaging Tool (`SendUserMessage`)

**Status:** Not implemented in CodingAgent

**claw-code implementation (`tools/src/lib.rs`):**
- `SendUserMessage` tool (alias `Brief`) — `{ message: string, attachments: [string], status: "normal"|"proactive" }`
- Allows the agent to proactively send an interim message to the user without completing its turn
- `status: "proactive"` is for unsolicited updates

**CodingAgent current state:**
- Agent communicates only via final text at end of turn
- No mid-turn user messaging tool

**Gap:** `send_user_message` tool in CodingAgent's tool registry. Priority: **Low**

---

## Section 3: Parity Summary Table

| ID | Feature | CodingAgent | claw-code | Gap priority |
|----|---------|-------------|-----------|--------------|
| CP-6 | Deterministic auto-compaction | LLM-based, manual | Token-count, automatic | Medium |
| CP-7 | Shell hooks with deny + post-tool | Pre-only, Python | Pre+post, shell, deny | Medium |
| CP-8 | Per-tool permission policy + config | Bash allowlist only | Full PermissionMode system | Low |
| CP-9 | Cache token tracking (creation+read) | Not tracked | Tracked + reconstructed | Low |
| CP-10 | LSP → system prompt injection | LSP exists, not injected | Injected via with_lsp_context | Low |
| CP-11 | Ancestor instruction file discovery | Root AGENTS.md only | Walk + dedup + budget | Low |
| CP-12 | `__DYNAMIC_BOUNDARY__` for caching | Not implemented | Implemented + wired | Low |
| CP-13 | Per-project settings file | Not implemented | 5-layer deep merge | Low |
| CP-14 | Session version field | Not present | Present (migration path) | Low |
| CP-15 | Agent-to-user mid-turn message tool | Not present | `SendUserMessage` tool | Low |
| CP-1 | Structural recursion prevention | Not implemented | Tool set restriction per subagent | Low |
| CP-2 | Manifest-first subagent spawning | Not implemented | Write JSON before spawn | Low |
| CP-3 | Stable content hash for instruction files | Not implemented | Fast dedup hash | Low |
| CP-4 | Dynamic prompt boundary sentinel | Not implemented | `__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__` | Low |
| CP-5 | `verification_nudge_needed` in TodoWrite | Not implemented | Auto-remind on complete | Trivial |

---

## Section 4: Implementation Effort Estimates

| Effort | Items |
|--------|-------|
| **Trivial** (< 1 hour) | CP-5, CP-14 |
| **Low** (half day) | CP-3, CP-4, CP-9, CP-12, CP-15 |
| **Low-Medium** (1 day) | CP-1, CP-2, CP-8, CP-10, CP-11, CP-13 |
| **Medium** (2–3 days) | CP-6 (deterministic compaction engine), CP-7 (shell hooks with deny) |

All items can be implemented independently. There are no cross-item dependencies.

---

## Section 5: Recommended Implementation Order

Priority order (highest value / lowest risk first):

```
1. CP-9  (Low effort, direct cost reduction — add cache token fields)
2. CP-12 (Low effort, direct cost reduction — prompt cache boundary)
3. CP-10 (Low effort, immediate UX improvement — LSP in system prompt)
4. CP-6  (Medium effort, highest architectural value — auto compaction)
5. CP-7  (Medium effort, security + extensibility — shell hooks)
6. CP-11 (Low-Medium — ancestor instruction file walk)
7. CP-8  (Low-Medium — per-tool permission policy)
8. CP-13 (Low-Medium — per-project settings file)
9. CP-15 (Low — mid-turn messaging tool)
10. CP-5/CP-14/CP-3/CP-4/CP-1/CP-2 (Trivial/Low — maintenance items)
```
