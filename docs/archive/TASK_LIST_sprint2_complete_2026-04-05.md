# CodingAgent — Unified Development Task List
*Merged from: tui-gap-analysis.md · orchestration-gap-analysis.md · gap-analysis-claw-code-v2.md · gap-analysis-vs-opencode-claw.md*
*Last validated: 2026-04-05*

Legend: ✅ Done · ❌ Todo · 🔄 Partial

---

## Workstream T — TUI

| ID | Task | Priority | Effort | Status |
|----|------|----------|--------|--------|
| TUI-T1 | Toast / notification system — `StatusUpdate` uses `notify()` toast instead of chat widget | P1 | S | ✅ |
| TUI-T2 | Multi-line prompt input — `ChatTextArea` with Shift+Enter | P1 | M | ✅ |
| TUI-T3 | Slash command palette overlay — `CommandPalette` ModalScreen | P1 | M | ✅ |
| TUI-T4 | Searchable model selection dialog — `CommandPalette` model submenu with filter | P1 | M | ✅ |
| TUI-T5 | Input history frecency | P1 | S | ✅ |
| TUI-T6 | Per-message token + cost display — `UsageTurnSummaryEvent` + dim footer appended after each assistant turn | P2 | S | ✅ |
| TUI-T7 | MCP server status footer chip | P2 | S | ✅ |
| TUI-T8 | Permission prompt dialog — TUI modal for `tool.permission_required` events | P2 | M | ✅ |
| TUI-T9 | Session management dialog — `SessionListScreen` | P2 | M | ✅ |
| TUI-T10 | `@file` attachment autocomplete | P2 | M | ✅ |
| TUI-T11 | Theme switcher — theme picker in Settings | P2 | M | ✅ |
| TUI-T12 | Ctrl+M keyboard shortcut — open model picker directly (`action_open_model_picker`) | P2 | XS | ✅ |

---

## Workstream P — Permission System

| ID | Task | Priority | Effort | Status |
|----|------|----------|--------|--------|
| PERM-W1 | `PROMPT` permission level wired to interactive approval gate | P0 | S | ✅ |
| PERM-W2 | Active permission mode checked vs required level in `execute_tool()` | P0 | S | ✅ |
| PERM-W3 | Doom-loop TUI gate — `DoomLoopEvent` + confirmation buttons in chat; grant/deny via `tool.doom_loop_continue` / `agent.interrupt` | P1 | S | ✅ |
| PERM-W4 | Per-agent permission override — `AgentDefinition.permission_rules` extends global policy during agent execution | P2 | S | ✅ |
| PERM-W5 | Permission audit log — append `allow`/`deny`/`ask` decisions to `.agent/permission_audit.jsonl` | P2 | S | ✅ |

---

## Workstream O — Orchestration Hardening

| ID | Task | Priority | Effort | Status |
|----|------|----------|--------|--------|
| ORCH-W1 | Mid-run max steps tool disabling — write tools pruned from `tools_list` + `max_steps.txt` injected into system msg when `turn_count >= max_turns - 2` | P2 | S | ✅ |
| ORCH-W2 | Compact continuation signal | P1 | S | ✅ |
| ORCH-W3 | `AgentDefinition.prompt_override` wired into `SystemPromptBuilder.build()` | P2 | S | ✅ |
| ORCH-W4 | `plan_enter` / `plan_exit` as real tool calls — tools that transition `AgentState` agent mode and rebuild system prompt | P2 | M | ✅ |
| ORCH-W5 | Internal utility agent calls — one-shot LLM calls (no tool loop) for session title generation and compaction summary | P3 | M | ✅ |

---

## Workstream S — Subagent Spawning

| ID | Task | Priority | Effort | Status |
|----|------|----------|--------|--------|
| SPAWN-W1 | Recursive loop re-entry — `delegate_task` re-enters the LangGraph pipeline in a child context with `parent_session_id` tracked in `AgentState` | P1 | XL | ✅ |
| SPAWN-W2 | `allowed_tools` enforcement — reject tools outside `AgentDefinition.allowed_tools` when executing in a delegated context | P1 | S | ✅ |
| SPAWN-W3 | Child session persistence — store child session in `SessionStore`; return `child_session_id` from `delegate_task` | P1 | M | ✅ |
| SPAWN-W4 | Session hierarchy in `SessionStore` + TUI | P2 | M | ✅ |
| SPAWN-W5 | Spawn permission gate — `spawn.permission_required` event before `delegate_task` executes | P2 | S | ✅ |

---

## Workstream SS — Session & State

| ID | Task | Priority | Effort | Status |
|----|------|----------|--------|--------|
| SES-W1 | Session file version field — `schema_meta` table + `get_schema_version()` in `SessionStore`; versioned envelope `{"version":1,"history":[...]}` in TUI history JSON with legacy migration | P1 | S | ✅ |
| SES-W2 | Full conversation pair storage — persist assistant response messages alongside user prompts in session transcript | P2 | S | ✅ |
| SES-W3 | Per-model-per-role config — `"planning_model"`, `"execution_model"` keys in `providers.json`; route orchestrator calls accordingly | P2 | M | ✅ |

---

## Status: ALL ITEMS COMPLETE ✅

All 5 workstreams fully implemented as of 2026-04-05.
Test suite: **2524 passed, 2 skipped, 1 pre-existing failure** (test_llm_manager_fallback).
