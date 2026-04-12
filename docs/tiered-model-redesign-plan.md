# CodingAgent — Tiered Model Redesign Plan

**Date:** 2026-04-11 (audit v2 — deepened 2026-04-11)
**Status:** Approved for implementation
**Goal:** Make CodingAgent work reliably from 9B local models (LM Studio / Ollama) up to frontier models (GPT-4o, Claude 4, Gemini), using a single codebase with tier-adaptive behaviour.

---

## Executive Summary

CodingAgent already has the right architectural foundations: a 5-tier ModelTier system, tier-specific role prompts, tool pruning, and a fast-path bypass for simple tasks. What it has in excess is **pipeline overhead calibrated for 7–9B failure modes** that actively hurts capable models.

**Evidence:** Gemma 4 31B works well on OpenCode — a system with no planning nodes, no validator, no analyst delegation. It just does: system prompt + tool call loop + context compaction. CodingAgent's 14-node pipeline adds latency without improving outcomes for the same model.

A deep audit found **12 real bugs**, **8 anti-patterns**, and **7 architectural gaps**:

- `_prune_tools()` is defined but **never called** — NANO models silently receive 35 tools instead of 8 *(FIXED)*
- `debug_attempts` and `replan_attempts` are **independent counters** — alternating errors produce unbounded recovery loops *(FIXED)*
- Thinking-block detection fires **before** tool parsing — adds a wasted round-trip on every thinking response *(FIXED)*
- `analyst_delegation`, `plan_validator`, and `step_controller` heuristics add **3–5 extra node hops** for LARGE/FRONTIER tasks that rarely need them
- `execute_tool` is a **blocking synchronous call** inside an async function — long bash/git operations stall the event loop
- Compaction **re-fires on every turn**, and `_prune_tool_outputs` prunes at **15K tokens** (OpenCode protects 40K)
- `AgentState` has **95 fields** (not the estimated 55), with at least 6 dead fields and 3 memory leaks

This plan is **evolutionary, not a rewrite**. Two parallel tracks:
1. **Small model stabilization** (Phases 1–2): fix the bugs that break 9B models
2. **Capable model simplification** (Phase 3b): remove the nodes that slow down 31B+ models

The goal is the same throughput as OpenCode for capable models, with better recovery and memory for all models.

---

## Current State Assessment

### What Works (Preserve)

| Component | Location | Value |
|---|---|---|
| 5-tier enum (NANO/SMALL/MEDIUM/LARGE/FRONTIER) | `model_tiers.py` | Accurate tier detection by model name + context window |
| Tier-specific role prompts | `roles/operational-small.md`, `operational-frontier.md` | 36-line minimal vs 85-line comprehensive |
| Tool description shortening | `context_builder.py:_render_tools_for_tier()` | NANO/SMALL get first-sentence-only descriptions — saves 30–50% tokens |
| Tool parse fallback chain | `tool_parser.py` | Native JSON → YAML safe_load → custom line-by-line parser |
| Fast-path for simple tasks | `builder.py:route_after_perception()` | Skips analysis when `task_complexity=="simple"` |
| Two-tier prompt cache | `context_builder.py` | Static (role+tools+hash) + dynamic (env block) — eliminates redundant SOUL.md renders |
| History token-budget pruning | `perception_node.py:_prune_tool_outputs()` | Keeps recent 15K tokens; replaces old tool results with placeholder |
| Provider capability config | `providers.json` | `supports_native_tools`, `context_length`, `small_model` |
| Dynamic context window probing | `lm_studio_adapter.py:get_loaded_context_length()` | Reads actual loaded context from LM Studio `/api/v0/models` |

### Root Causes of Small-Model Failures (Original 5 + 7 New)

**1. Wrong context limit in config** *(FIXED — Vol29)*
`providers.json` `context_length: 7600` override removed; `get_context_budget()` max_tokens raised to 131072; `llm_manager.py` now calls `set_active_context_length()` directly so headless mode works.

**2. Thinking blocks trigger corrective loop** *(PARTIALLY FIXED — Vol28)*
`perception_node` REACT-OVF-EARLY-EXIT catches overflow. But the root cause remains: thinking-block empty-response detection fires at line ~1183 **before** tool parsing at line ~1240. If the model emits `<think>...</think>\n```yaml...````, the thinking check marks it as empty and injects a corrective prompt. The tool call is only found on the NEXT round.

**3. No graduated corrective prompts** *(UNFIXED)*
All 3 retry attempts receive the same 2-line `system_reminder` message. No additional guidance at attempt 2. No tier-aware variant.

**4. Pipeline doesn't adapt to tier at routing level** *(UNFIXED)*
`route_after_perception` and `route_execution` never inspect `model_tier`. NANO models traverse analyst_delegation (extra LLM call). `route_execution`'s read-then-modify heuristic (line ~516) applies to all tiers, causing SMALL model misrouting.

**5. Over-mixed node responsibilities** *(UNFIXED)*
`perception_node.py` (1,550 lines): 8 distinct concerns. `execution_node.py` (1,204 lines): 7 distinct concerns.

**6. `_prune_tools()` defined but never called** *(NEW — HIGH SEVERITY)*
`context_builder.py` line ~860 calls `_prune_tools` only inside an `if len(tools) > 35` guard that is never reached by `build_prompt`. `_render_tools_for_tier()` is called instead — but it only truncates descriptions; it does NOT limit tool count. Result: NANO models silently receive all 35 tools in their context instead of 6–8. For a 4K context window, tool descriptions alone can exhaust 50% of the budget.

**7. Memory leaks in AgentState** *(NEW — MEDIUM SEVERITY)*
Three fields grow unbounded across turns with no pruning:
- `tool_last_used` (dict): grows per unique tool+path combination, never reset
- `recent_tool_calls` (list): dedup fingerprints, never pruned
- `snapshots` (list): git tree-hash per turn, never pruned

On 500+ turn sessions, these add measurable overhead to state serialisation on every graph round.

**8. Independent recovery counters allow unbounded loops** *(NEW — MEDIUM SEVERITY)*
`debug_attempts` and `replan_attempts` are separate counters. A task can exhaust debug (3 attempts), enter replan (5 attempts), then if a new error occurs it gets another 3 debug attempts. No `total_recovery_attempts` cap exists. The `total_debug_attempts` field exists but is not enforced as a hard stop.

**9. Blocking `execute_tool` inside async function** *(NEW — MEDIUM SEVERITY)*
`execution_node.py` line ~100: `result = orchestrator.execute_tool(...)` is a synchronous blocking call inside an `async def` function. Long-running tools (bash with `sleep`, git operations on large repos) block the asyncio event loop thread, preventing cancellation and delaying other coroutines.

**10. Compaction re-fires on every turn** *(NEW — LOW-MEDIUM SEVERITY)*
`perception_node.py` lines ~571–576: `_should_compact()` fires before every LLM call. After a compaction event, new messages are immediately appended. The next turn's compaction check sees the compacted base + new messages and re-evaluates. Since there is no `last_compact_turn` deduplication, the compaction logic runs and potentially re-compacts on every subsequent turn, wasting CPU.

**11. History pruning uses `len/4` token estimate** *(NEW — LOW SEVERITY)*
`_prune_tool_outputs()` (line ~136) estimates token count as `len(s) // 4`. Actual tiktoken counts can be 2–4× higher for code-heavy content. This causes over-pruning — useful tool results are dropped before the budget is actually exhausted.

**12. Dead router code in builder.py** *(NEW — CODE QUALITY)*
Three "should_" router functions (`should_after_planning`, `should_after_execution`, `should_after_verification`) were superseded by `route_` variants but not removed. They remain in the file (170+ lines), causing confusion about which router is active for each edge. The main graph uses `route_execution` (line ~968) but `should_after_execution` (line ~394, 163 lines) still exists.

---

## Architecture Target

### Tier Behaviour Matrix

| Capability | NANO (≤7B, ≤8K) | SMALL (7–14B, 8–32K) | MEDIUM (14–70B, 32–128K) | LARGE (70B+, 128K+) | FRONTIER (Cloud/31B+) |
|---|---|---|---|---|---|
| **Graph path** | perception → execution → sync | perception → plan → execution → sync | Full pipeline (all nodes) | Simplified: perception → plan → execution → verify → sync | Simplified: perception → (plan) → execution → sync |
| **Nodes skipped** | analysis, plan_validator, delegation, verify, debug | analysis, plan_validator | none | analyst_delegation, plan_validator, step_controller heuristics | analyst_delegation, plan_validator, step_controller heuristics, verify (simple tasks) |
| **Tool format** | YAML only, simple_mode | YAML only | JSON native preferred | JSON native | JSON native + parallel |
| **Tool count** | 6 (core only) | 15 | 35 | 50 | 60 |
| **Max steps** | 4 | 6 | 10 | 16 | 20 |
| **Max turns** | 12 | 20 | 40 | 60 | 80 |
| **Planning** | Skipped | 3-step flat list | Full DAG | Full DAG (no separate validator) | Full DAG for complex; skipped for simple |
| **Analyst delegation** | Disabled | Disabled | Enabled | **Disabled** (model self-analyses) | **Disabled** (model self-analyses) |
| **Plan validation** | N/A | Inline only | Full validator node | **Inlined into planning_node** | **Inlined into planning_node** |
| **Verification** | Skip all | Write-file only | Full verification | Full + eval | Skipped for simple tasks; full for complex |
| **Recovery budget** | 2 total | 4 total | 8 total | **6 total** | **5 total** |
| **System prompt** | 100 tokens (constraints only) | 300 (operational-small) | 600 (operational) | 800 (operational-frontier) | 1000 (operational-frontier + skills) |
| **Per-provider prompt** | — | — | — | operational-gemma4 if Gemma 4 | operational-gemma4 / provider variant |
| **Thinking blocks** | Extracted, not re-sent | Extracted | Optional | Budget-controlled | Budget-controlled |
| **execute_tool** | Sync OK | Sync OK | asyncio.to_thread | asyncio.to_thread | asyncio.to_thread |

> **Key insight:** LARGE/FRONTIER models fail less often and self-correct better. The full pipeline overhead (analyst_delegation, plan_validator, step_controller heuristics) was designed for 7–9B failure modes. On a 31B+ model it adds latency without improving outcomes. OpenCode's simpler loop — system prompt + tool calls + compaction — works well on Gemma 4 31B. CodingAgent must match or beat that baseline for capable models.

### Node Map (Target: 6 nodes from current 14)

```
Current (14 nodes):                    Target (6 nodes):
perception_node           ─────────►  act_node  (LLM call + tool parse)
execution_node            ─────────►  (merged into act_node)
step_controller_node      ─────────►  (inlined into act_node)
planning_node             ─────────►  plan_node  (planning + validation + analyst delegation)
plan_validator_node       ─────────►  (inlined into plan_node)
analyst_delegation_node   ─────────►  (inlined into plan_node)
analysis_node             ─────────►  (inlined into plan_node for complex tasks)
verification_node         ─────────►  verify_node  (tier-gated; includes evaluation)
evaluation_node           ─────────►  (inlined into verify_node)
debug_node                ─────────►  recover_node  (shared LLM-call helper)
replan_node               ─────────►  (merged into recover_node)
memory_update_node        ─────────►  sync_node
delegation_node           ─────────►  (merged into sync_node)
wait_for_user_node        ─────────►  wait_node  (unchanged)
```

### Router Map (Target: 5–6 routers from current 17)

```
route_after_act(state) → plan | verify | recover | sync | wait
route_after_plan(state) → act | wait | sync
route_after_verify(state) → recover | sync | end
route_after_recover(state) → act | sync | end
route_after_sync(state) → act | end
```

Remove: `should_after_planning`, `should_after_execution`, `should_after_verification` (superseded dead code).
Consolidate: `should_after_execution_with_replan`, `should_after_execution_with_compaction` into `route_after_act`.

---

## Implementation Plan

---

### Phase 1 — Small Model Stabilization
**Goal:** 9B models (Gemma 4 E4B, Qwen 2.5 7B, Llama 3.1 8B) work without overflow loops, corrective spam, or silent tool count violations.
**Test gate:** Live LM Studio tests pass; no LM Studio request spam in developer logs; NANO models receive ≤8 tools.

---

#### P1-A: Dynamic context window *(DONE — Vol29)*
`context_length: 7600` removed from `providers.json`. `get_context_budget()` max raised to 131072. `llm_manager.py` calls `set_active_context_length()` directly for headless mode. Per-task context probe added to `inference_loop.py`.

---

#### P1-B: Fix `_prune_tools()` not being called *(30 min, HIGH IMPACT)*
**File:** `src/core/context/context_builder.py`

`_prune_tools()` (which enforces the per-tier tool count limit via `get_tool_limit()`) is never called from `build_prompt()`. Only `_render_tools_for_tier()` (which shortens descriptions) is called. Fix: call `_prune_tools(tools, model_tier)` BEFORE `_render_tools_for_tier()` inside `build_prompt()`.

```python
# In build_prompt(), BEFORE the description-shortening step:
tools = _prune_tools(tools, model_tier)          # <-- ADD THIS CALL
tool_section = _render_tools_for_tier(tools, model_tier, native_tools)
```

Also verify `get_tool_limit()` returns the right values:
- NANO: 6–8 tools
- SMALL: 15–20 tools
- MEDIUM: 35 tools
- LARGE/FRONTIER: 50–60 tools

**Tests:**
- `test_nano_tier_tool_count_enforced` — NANO model gets ≤8 tools from `build_prompt()`
- `test_small_tier_tool_count_enforced` — SMALL gets ≤20 tools
- `test_prune_tools_called_before_render` — inspect source to confirm call order

---

#### P1-C: Fix thinking-block detection before tool parsing *(2 hours)*
**File:** `src/core/orchestration/graph/nodes/perception_node.py` (~line 1183)

Current code (line ~1183):
```python
is_empty_response = (
    not content_stripped
    or content_stripped.replace("<think>", "").replace("</think>", "").strip() == ""
)
```
This fires BEFORE tool parsing at line ~1240. A model that outputs `<think>...</think>\n```yaml...``` gets marked as empty-response, triggering a corrective prompt, and the YAML is only found on the NEXT round.

Fix — restructure the order:
```python
# 1. Attempt tool parse on raw content first
tool_call = _attempt_tool_parse(content_stripped)

# 2. ONLY if no tool call found, check for thinking-only content
if not tool_call:
    _after_thinking = re.sub(r"<think>.*?</think>", "", content_stripped, flags=re.DOTALL).strip()
    is_empty_response = not _after_thinking
    if not is_empty_response:
        # Try again on content with thinking stripped
        tool_call = _attempt_tool_parse(_after_thinking)
```

For SMALL/MEDIUM: if thinking content is present but no tool call is found after stripping, try parsing the stripped content before incrementing `empty_response_count`.

**Tests:**
- `test_thinking_block_with_yaml_not_counted_as_empty` — `<think>...</think>\n```yaml...``` parses on first attempt
- `test_thinking_only_still_increments_empty_count` — pure `<think>...</think>` still triggers corrective

---

#### P1-D: Graduated corrective prompts *(2 hours)*
**File:** `src/core/orchestration/graph/nodes/perception_node.py` (~line 1214, ~line 1428)

Current code uses a single string for all attempts:
```python
corrective_prompt = (
    "\n\n<system_reminder>\n"
    "You must output a valid YAML tool call...\n"
    "</system_reminder>\n"
)
```

Replace with tier-aware graduated prompts:

```python
_CORRECTIVE_PROMPTS = [
    # Attempt 1: gentle hint
    "\n\n<system_reminder>\nPlease output a YAML tool call.\n"
    "```yaml\nname: tool_name\narguments:\n  key: value\n```\n</system_reminder>\n",

    # Attempt 2: includes available tools + stronger tone
    "\n\n<system_reminder>\nREQUIRED: Output exactly one YAML tool call block.\n"
    "Available tools: {tools_hint}\n"
    "```yaml\nname: tool_name\narguments:\n  key: value\n```\n</system_reminder>\n",

    # Attempt 3 (MEDIUM+): diagnostic — restate the task
    "\n\n<system_reminder>\nThird attempt. Task: {task_summary}\n"
    "Output ONE YAML tool call. If unsure, use 'respond' to ask for clarification.\n</system_reminder>\n",
]

# For NANO: only attempt 1 then fail (no benefit from retries)
# For SMALL: attempts 1 and 2
# For MEDIUM+: all three
max_attempts = {"nano": 1, "small": 2}.get(model_tier, 3)
```

For truncated YAML specifically (context-full case), the attempt 2 prompt should say: *"Your previous response was cut off. Output a SHORT tool call with minimal arguments."*

**Tests:**
- `test_corrective_prompt_attempt_1_is_gentle`
- `test_corrective_prompt_attempt_2_includes_tools`
- `test_nano_tier_fails_after_attempt_1`

---

#### P1-E: Explicit model constraints block *(3 hours)*
**File:** `src/core/context/context_builder.py`

Inject a `<model_constraints>` block at the top of the system prompt for NANO/SMALL:

```python
def _build_constraints_block(model_tier: str, tool_count: int, ctx_budget: int, step_limit: int) -> str:
    if model_tier not in ("nano", "small"):
        return ""
    return (
        f"<model_constraints>\n"
        f"Tier: {model_tier.upper()} | Context: {ctx_budget} tokens | Tools: {tool_count} available\n"
        f"Max steps: {step_limit} | Format: YAML tool calls only (no JSON, no prose before tool call)\n"
        f"Not available: parallel tool calls, subagent delegation, extended reasoning\n"
        f"</model_constraints>\n\n"
    )
```

This tells the model what it can and cannot do upfront, reducing hallucinated tool names and over-ambitious plans that collapse on YAML parsing.

**Tests:**
- `test_constraints_block_injected_for_nano`
- `test_constraints_block_injected_for_small`
- `test_constraints_block_absent_for_medium`

---

#### P1-F: NANO minimal tool subset *(2 hours)*
**File:** `src/tools/toolsets/nano.yaml` (new), `src/core/orchestration/orchestrator.py`

After fixing P1-B (so tool counts are enforced), also create an explicit NANO toolset:

```yaml
# nano.yaml — 6 tools for ≤7B models
name: nano
tools:
  - read_file
  - write_file
  - bash
  - grep
  - glob
  - respond
```

Wire `get_tools_for_role()` to return nano toolset when `model_tier == "nano"` regardless of role. This is a belt-and-suspenders guarantee on top of `_prune_tools()`.

---

#### P1-G: Fix memory leaks in AgentState *(1 hour)*
**File:** `src/core/orchestration/graph/nodes/memory_update_node.py` (or `orchestrator.py:start_new_task()`)

Add pruning in `memory_update_node` (which runs at the end of each task) and in `start_new_task()`:

```python
# In memory_update_node, add to result dict:
"snapshots": state.get("snapshots", [])[-10:],          # keep last 10
"recent_tool_calls": state.get("recent_tool_calls", [])[-50:],  # keep last 50

# In orchestrator.start_new_task():
initial_state["tool_last_used"] = {}      # reset per task (already large by turn 500)
initial_state["recent_tool_calls"] = []
initial_state["snapshots"] = []
```

**Tests:**
- `test_snapshots_pruned_to_10_in_memory_update`
- `test_tool_last_used_reset_on_new_task`

---

**Phase 1 Completion Criteria:**
- NANO tier: ≤8 tools in prompt, `<model_constraints>` block present, corrective loop fires once then fails gracefully
- SMALL tier: thinking-block content not double-counted, graduated prompts at attempt 2
- No LM Studio request spam; context budget matches loaded model's window
- All 3,615+ existing tests pass

---

### Phase 2 — Correctness & Stability
**Goal:** Fix the critical correctness bugs (blocking async, recovery counter interactions, compaction re-firing) and remove dead code.
**Test gate:** New regression tests for each fix pass; existing tests unaffected.

---

#### P2-A: Add global recovery cap *(2 hours)*
**File:** `src/core/orchestration/graph/state.py`, `src/core/orchestration/graph/builder.py`

Add `total_recovery_attempts: int` to AgentState (initialised to 0 in `initial_state`). Increment in BOTH `debug_node` and `replan_node`. Add a hard check in both routing functions:

```python
# In should_after_debug / should_after_replan:
total_attempts = state.get("total_recovery_attempts", 0)
if total_attempts >= 10:
    logger.warning("Global recovery cap (10) reached — routing to memory_sync")
    return "memory_sync"
```

Cap table:
- NANO: 2 total
- SMALL: 4 total
- MEDIUM: 8 total
- LARGE/FRONTIER: 12 total

**Tests:**
- `test_global_recovery_cap_stops_alternating_debug_replan_loop`
- `test_recovery_cap_is_tier_aware`

---

#### P2-B: Wrap `execute_tool` in `asyncio.to_thread` *(2 hours)*
**File:** `src/core/orchestration/graph/nodes/execution_node.py` (~line 100)

```python
# Before:
result = orchestrator.execute_tool(action["name"], action.get("arguments", {}), ...)

# After (MEDIUM/LARGE/FRONTIER only — NANO/SMALL don't need it):
if model_tier in ("medium", "large", "frontier"):
    result = await asyncio.to_thread(
        orchestrator.execute_tool, action["name"], action.get("arguments", {}), ...
    )
else:
    result = orchestrator.execute_tool(action["name"], action.get("arguments", {}), ...)
```

This unblocks the event loop for cancellation signals and other coroutines when bash/git operations take >2 seconds.

**Tests:**
- `test_execute_tool_uses_asyncio_to_thread_for_medium_tier`
- `test_execute_tool_cancellable_while_running`

---

#### P2-C: Fix compaction re-firing every turn *(1 hour)*
**File:** `src/core/orchestration/graph/nodes/perception_node.py` (~line 571)

Add minimum-turn gap before re-compaction:

```python
_MIN_TURNS_BETWEEN_COMPACTIONS = 3

last_compact = state.get("last_compact_turn", 0)
current_turn = state.get("turn_count", 0)

if (current_turn - last_compact) >= _MIN_TURNS_BETWEEN_COMPACTIONS:
    compact_result = _should_compact(_history_for_prompt, _ac_config)
    if compact_result and compact_result.removed_message_count > 0:
        # Do compaction
        ...
        return_delta["last_compact_turn"] = current_turn
```

**Tests:**
- `test_compaction_skipped_within_min_gap`
- `test_compaction_fires_after_min_gap`

---

#### P2-D: Improve history pruning token estimation *(1 hour)*
**File:** `src/core/orchestration/graph/nodes/perception_node.py` (~line 136)

Replace `len(s) // 4` with the real tiktoken estimator (already available via `context_builder.token_estimator`):

```python
# In _prune_tool_outputs():
# Before:
rough_tokens = len(content) // 4

# After:
try:
    rough_tokens = count_tokens(content)  # imported from src.core.inference.tokenizer
except Exception:
    rough_tokens = len(content) // 4  # fallback
```

This prevents over-pruning of tool results that have dense code content.

---

#### P2-E: Remove dead router code *(1 hour)*
**File:** `src/core/orchestration/graph/builder.py`

Remove or clearly mark the three superseded router functions:
- `should_after_planning` (line ~42, 22 lines) — superseded by `route_after_act` in target
- `should_after_execution` (line ~394, 163 lines) — superseded by `route_execution`
- `should_after_verification` (line ~595, 52 lines) — superseded by `should_after_evaluation`

If they are still used by `GraphFactory` subgraphs, add a comment: `# DEPRECATED: used only by GraphFactory subgraphs, see route_execution for main graph`. Do not delete until GraphFactory is confirmed not to reference them.

Also fix the overlapping guard in `should_after_plan_validator` (line ~96–108) — use `elif` to prevent dead branch confusion.

---

#### P2-F: Inline plan_validator into planning_node *(4 hours)*
**File:** `src/core/orchestration/graph/nodes/planning_node.py`, delete `plan_validator_node.py`

`plan_validator_node` is 273 lines of boilerplate around 50 lines of validation logic. Move the validation (registered tool checks, step references) to the end of `planning_node`. Remove the separate node and its edges from `builder.py`.

This eliminates one graph round-trip per task (plan → validate → execute becomes plan → execute).

**Router impact:** Remove `should_after_plan_validator` (90 lines) from builder.py; merge into `should_after_planning`.

---

#### P2-G: Merge debug_node + replan_node into recovery_node *(1 day)*
**Files:** New `src/core/orchestration/graph/nodes/recovery_node.py`

Both nodes share ~100 lines of identical boilerplate: ContextBuilder call, LLM call, timeout handling, cancellation check, attempt counter. Extract into a `_recovery_llm_call(role, task, context, timeout)` helper. Then build `recovery_node` around it:

```python
async def recovery_node(state: AgentState) -> Dict:
    replan_needed = state.get("replan_required") or _diverged_from_plan(state)
    if replan_needed:
        return await _replan_path(state)   # strategic role
    else:
        return await _debug_path(state)    # debugger role
```

Tier gates for recovery budget:
- NANO: max 1 debug attempt, no replan
- SMALL: max 2 debug, 1 replan
- MEDIUM+: max 3 debug, 5 replan (governed by global cap P2-A)

Add error type classification to the replan path (already done in debug path).

---

#### P2-H: Fix YAML scalar silent failure *(1 hour)*
**File:** `src/core/orchestration/tool_parser.py` (~line 107)

After `safe_load`, if result is not a dict, fail fast rather than falling through to the custom parser with a non-dict value:

```python
parsed = _yaml.safe_load(yaml_content)
if not isinstance(parsed, dict):
    return None  # Not a YAML mapping; let the custom parser try
```

Also: if `parsed` is a dict with a `name` key but NO `arguments` key AND the tool requires arguments, log a warning rather than silently passing empty `{}`.

---

**Phase 2 Tests:**
- All 3,615+ existing tests must pass unchanged
- Add: `test_global_recovery_cap_*`, `test_execute_tool_async_*`, `test_compaction_gap_*`
- Add: `test_recovery_node_debug_path`, `test_recovery_node_replan_path`, `test_recovery_node_tier_budget`
- Add: `test_planning_includes_validation`, `test_plan_validator_node_removed_from_graph`

---

### Phase 3 — Tier-Aware Routing
**Goal:** NANO/SMALL models take a short pipeline path; MEDIUM+ takes full pipeline. Routes check `model_tier`.
**Test gate:** MockAdapter tests demonstrate different node traversal counts by tier.

---

#### P3-A: Add tier check to all routing functions *(1 day)*
**File:** `src/core/orchestration/graph/builder.py`

Add `model_tier` guard to:

1. **`route_after_perception`** — before analyst_delegation:
   ```python
   if state.get("model_tier") in ("nano", "small"):
       return "planning"  # skip analyst_delegation entirely
   ```

2. **`route_execution`** — disable read-then-modify heuristic for small models:
   ```python
   if state.get("model_tier") in ("nano", "small"):
       _skip_read_then_modify_heuristic = True
   ```

3. **`should_after_analysis`** — NANO should never reach analysis; guard at entry:
   ```python
   if state.get("model_tier") == "nano":
       return "planning"
   ```

---

#### P3-B: Tier-gated verification *(2 hours)*
**File:** `src/core/orchestration/graph/nodes/verification_node.py`

```python
tier = state.get("model_tier", "medium")
if tier == "nano":
    return {"verification_passed": True, "verification_result": {"status": "skipped_nano"}}
if tier == "small" and state.get("last_tool_name") not in _WRITE_TOOLS:
    return {"verification_passed": True, "verification_result": {"status": "skipped_read_only"}}
```

NANO: always skip (step limit already caps damage; verification adds >30% overhead).
SMALL: skip for read-only tool results; verify writes.
MEDIUM+: full verification + evaluation.

---

#### P3-C: Tier-gated planning depth *(2 hours)*
**File:** `src/core/orchestration/graph/nodes/planning_node.py`

```python
_TIER_STEP_LIMITS = {"nano": 0, "small": 3, "medium": 8, "large": 15, "frontier": 0}  # 0 = unlimited

step_limit = _TIER_STEP_LIMITS.get(model_tier, 8)
if step_limit == 0 and model_tier == "nano":
    return {"task_decomposed": False, "current_plan": None}  # no planning for NANO
```

For SMALL: use a **flat ordered list** planning prompt (not DAG) to avoid JSON parsing failures on multi-level nested structures.

---

#### P3-D: Fuzzy tool name matching in preflight *(2 hours)*
**File:** `src/core/orchestration/orchestrator.py:preflight_check()`

Small models frequently typo tool names (`read_fail` for `read_file`, `writ_file` for `write_file`). Add fuzzy correction before rejecting:

```python
from difflib import get_close_matches
registered = list(self._tool_registry.keys())
close = get_close_matches(tool_name, registered, n=1, cutoff=0.8)
if close:
    logger.warning(f"preflight: fuzzy-corrected '{tool_name}' → '{close[0]}'")
    tool_name = close[0]
    action["name"] = tool_name
```

Gate behind SMALL and above (NANO models should fail fast on bad tool names to preserve context).

---

#### P3-E: Preserve-flagged messages in compaction *(2 hours)*
**File:** `src/core/memory/distiller.py`, `src/core/orchestration/graph/nodes/perception_node.py`

Add a `"preserve": True` metadata key to critical messages that must survive compaction:
- User format instructions ("use YAML format")
- Error messages that define the current task context
- The original task description (already in `state["task"]`, but reinforcing in history)

In compaction logic:
```python
def _should_compact_message(msg: dict) -> bool:
    metadata = msg.get("metadata", {})
    if metadata.get("preserve"):
        return False  # never compact this message
    ...
```

---

#### P3-F: Per-tier context fraction *(2 hours)*
**File:** `src/core/inference/provider_context.py`

```python
_TIER_CONTEXT_FRACTION = {
    "nano":     0.50,  # 50% for context (output needs half the window)
    "small":    0.60,
    "medium":   0.70,
    "large":    0.75,
    "frontier": 0.80,
}

def get_context_budget(model_tier: str = "medium", ...) -> int:
    fraction = _TIER_CONTEXT_FRACTION.get(model_tier, 0.65)
    ...
```

Pass `model_tier` through from `ContextBuilder.build_prompt()` which already has it.

---

**Phase 3 Tests:**
- `test_nano_tier_skips_analysis_and_delegation`
- `test_nano_tier_skips_verification`
- `test_small_tier_skips_verification_for_reads`
- `test_fuzzy_tool_name_correction_small_tier`
- `test_preserve_flagged_messages_survive_compaction`
- MockAdapter pipeline tests: NANO traverses ≤3 nodes; MEDIUM traverses ≥7 nodes

---

### Phase 3b — LARGE/FRONTIER Pipeline Simplification
**Goal:** Remove overhead nodes for capable models (Gemma 4 31B, cloud frontier). The 14-node pipeline was calibrated for 7–9B failure modes. On a 31B model, most of that overhead is pure latency — the same tasks complete faster and more reliably on OpenCode's direct loop. Meet the model where it is.

**Evidence:** Gemma 4 31B works well on OpenCode which has: single system prompt, direct tool call loop, no planning nodes, no validator, no analyst delegation. CodingAgent must not be slower or less reliable than OpenCode for the same model.

**Test gate:** LARGE/FRONTIER MockAdapter task completes in ≤4 node hops for a simple task (vs current 7+). No regression on recovery paths.

---

#### P3b-A: Skip analyst_delegation for LARGE/FRONTIER

**Problem:** `analyst_delegation_node` fires for "complex" tasks before any work starts — it costs 1–2 extra LLM round-trips to decide whether to delegate, then usually decides not to. On a 31B model, the model itself can handle the analysis inline. The delegation overhead is net negative.

**File:** `src/core/orchestration/graph/builder.py` — `route_after_perception()`

```python
# LARGE/FRONTIER: skip analyst delegation entirely — model can self-analyze
if state.get("model_tier") in ("large", "frontier"):
    if state.get("next_action"):
        return "execution"
    return "planning"  # skip analysis AND analyst_delegation
```

This means LARGE/FRONTIER goes: `perception → planning → execution`, same as OpenCode's effective flow.

---

#### P3b-B: Skip plan_validator for LARGE/FRONTIER

**Problem:** `plan_validator_node` checks that the planning node produced a valid plan with registered tools. A 31B model's plans are well-formed ~95% of the time. The validator adds a deterministic round-trip that rarely catches anything and always adds latency.

**File:** `src/core/orchestration/graph/builder.py` — `should_after_plan_validator()`

```python
# LARGE/FRONTIER: trust the plan, go directly to execution
if state.get("model_tier") in ("large", "frontier"):
    if state.get("current_plan"):
        return "execution"
```

For LARGE/FRONTIER, fold the validation logic (tool name check) directly into `planning_node`'s return path — fail fast there rather than via a separate node.

---

#### P3b-C: Lightweight step_controller for LARGE/FRONTIER

**Problem:** `step_controller_node` applies heuristics designed for unreliable small models: it checks if the tool call was a "read" and forces a second tool call, detects prematurely-declared completion, and gates multi-step plans. On a 31B model these heuristics misfire more often than they help.

**File:** `src/core/orchestration/graph/nodes/step_controller_node.py`

Add a fast-path for capable models:
```python
tier = state.get("model_tier", "medium")
if tier in ("large", "frontier"):
    # Trust the model's own completion signal — skip read-then-modify heuristic
    # and premature-completion detection. Only enforce hard step limit.
    if current_step >= step_limit:
        return {"task_complete": True, ...}
    return {"current_step": current_step + 1, ...}
```

The read-then-modify heuristic (which forces an extra read before writes) was added to fix small-model behaviour. On a 31B model it creates unnecessary round-trips.

---

#### P3b-D: Reduce default recovery budget for LARGE/FRONTIER

**Current caps:** LARGE: 12, FRONTIER: 12 total recovery attempts.

**Problem:** A capable model that has already failed 12 times is genuinely stuck — either the task is impossible, there's a missing dependency, or the model needs human input. 12 attempts is too many; it burns context and time before admitting defeat. OpenCode has no recovery loop at all — it just fails and lets the user retry.

**Revised caps:**

```python
_RECOVERY_CAPS = {
    "nano":     2,   # fail fast — NANO can't self-correct reliably
    "small":    4,
    "medium":   8,
    "large":    6,   # reduced from 12 — capable models fail for real reasons
    "frontier": 5,   # reduced from 12 — trust the model; fewer thrash loops
}
```

For LARGE/FRONTIER, if the model has failed 5 times on the same error pattern (`last_debug_error_type` unchanged for 3 consecutive attempts), route immediately to `memory_sync` with an `errors` entry asking the user to clarify.

---

#### P3b-E: Direct execution mode for simple tasks (all tiers)

**OpenCode's approach:** There is no planning phase. Every task goes: system prompt + task → tool calls → done. Planning is implicit in the model's own reasoning.

**CodingAgent current approach:** Even `task_complexity=="simple"` tasks hit `perception → fast-path execution`. But `fast-path execution` still routes through `step_controller → verification → evaluation → memory_sync`. That's 5 nodes for "add a comment to line 42".

**Target for LARGE/FRONTIER simple tasks:**

```
perception → execution → memory_sync
```

Gate: `task_complexity == "simple"` AND `model_tier in ("large", "frontier")` AND `next_action` is set after perception.

```python
# In route_after_perception():
if (
    state.get("task_complexity") == "simple"
    and state.get("model_tier") in ("large", "frontier")
    and state.get("next_action")
):
    return "execution"  # skip planning, go directly to execution
    # execution routes to memory_sync on success (skip verification for simple tasks)
```

This matches OpenCode's effective flow for the common case: single-tool tasks complete in 2 node hops instead of 5–7.

---

**Phase 3b Tests:**
- `test_frontier_skips_analyst_delegation` — perception → planning (not analyst_delegation) for frontier tier
- `test_frontier_skips_plan_validator` — planning → execution directly for frontier tier
- `test_frontier_step_controller_no_read_heuristic` — step_controller doesn't inject extra read for frontier
- `test_frontier_recovery_cap_is_5` — should_after_debug returns memory_sync at 5 for frontier
- `test_simple_task_frontier_2_hops` — simple task with next_action → execution → memory_sync (no planning, no verify)
- `test_medium_tier_still_uses_full_pipeline` — these changes don't affect MEDIUM tier

---

### Phase 4 — Frontier Optimization
**Goal:** Cloud frontier models (GPT-4o, Claude 4, Gemini) get parallel tool calling, reasoning budget control, and per-role model routing.
**Test gate:** Integration tests with GitHub Copilot and Groq pass; OpenRouter end-to-end works.

---

#### P4-A: Parallel tool execution for LARGE/FRONTIER *(3 days)*
**File:** `src/core/orchestration/tool_execution_service.py`

Add `batch_execute(tool_calls: list[dict]) -> list[dict]` using `asyncio.gather` for independent parallel steps. Gate behind `provider_supports_parallel_tools: true` in providers.json. The planner already generates DAG plans with dependency edges — execution can respect them.

---

#### P4-B: Reasoning budget control *(1 day)*
**Files:** `src/core/inference/thinking_utils.py`, `src/core/context/context_builder.py`

For LARGE/FRONTIER models with thinking support:
- `thinking_budget = min(8000, context_window * 0.10)`
- Inject budget into system prompt for Claude 3.7+, O1/O3
- Track actual `reasoning_tokens` from usage metadata

---

#### P4-C: Long-context planning — remove step cap for FRONTIER *(2 hours)*
**File:** `src/core/orchestration/graph/nodes/plan_node.py`

Remove the 20-step cap for FRONTIER tier. Add plan resumption tracking so 50+ step plans can survive across sessions. The `plan_dag` field already exists in AgentState.

---

#### P4-D: Per-role model assignment *(2 days)*
**File:** `src/config/providers.json`, `src/core/config_loader.py`

```json
{
  "name": "GitHub Copilot",
  "models": {
    "execution": "claude-sonnet-4.6",
    "planning": "o3-mini",
    "analysis": "gpt-4o-mini",
    "background": "gpt-4o-mini"
  }
}
```

Wire `get_model_for_role(role)` through all node LLM calls.

---

**Phase 4 Tests:**
- `test_parallel_tool_execution`
- `test_reasoning_budget_injected_for_frontier`
- `test_per_role_model_assignment_routing`

---

## AgentState Cleanup (Cross-Phase)

Current: **95 fields** (audited). Target: **50 fields**.

### Fields to Remove (Dead)

| Field | Reason |
|---|---|
| `task_history` | Set by comment; no node writes to it. Dead. |
| `step_lint_warnings` | Set by step_controller_node; read nowhere. Dead. |
| `call_graph` | "Phase 3: call graph from analysis phase" — no consumer found. Dead. |
| `test_map` | Same as call_graph. Dead. |
| `awaiting_plan_approval` | Only used as UI flag; never drives routing decisions. Move to Orchestrator config. |
| `plan_resumed` | Read only in should_after_plan_validator (to be removed). |

### Fields to Consolidate

| Current | Target | Notes |
|---|---|---|
| `plan_mode_enabled`, `plan_mode_approved`, `plan_mode_blocked_tool`, `awaiting_plan_approval` | `plan_gate: dict` | Single gate state object |
| `preview_mode_enabled`, `pending_preview_id`, `preview_confirmed` | `preview_gate: dict` | Single gate state object |
| `execution_waves`, `current_wave` | Subsume into `plan_dag` | Plan DAG already tracks step status |
| `last_debug_error_type`, `debug_attempts`, `total_debug_attempts`, `replan_attempts` | `recovery: dict` | Unified recovery tracking dict |
| `_agent_session_manager`, `_agent_messages`, `_context_controller`, `_write_queue`, `_file_lock_manager`, `_pending_injections_source` | Remove from state entirely | These are infrastructure references, not graph state |

### Fields to Add (New)

| Field | Type | Purpose |
|---|---|---|
| `total_recovery_attempts` | `int` | Global cap across debug + replan (P2-A) |
| `last_compact_turn` | `int` | Prevent compaction re-firing (P2-C) |
| `tier_step_limit` | `int` | Computed at start; avoids repeated tier lookup |

---

## What NOT to Change

- **LangGraph as the orchestration framework** — explicit state machine is correct for recovery paths; OpenCode's loop works for the happy path, but recovery (debug, replan, rollback) benefits from explicit graph edges
- **The 5-tier ModelTier enum** — well-designed; needs to be applied at more routing points, not replaced
- **The YAML tool parse fallback chain** — handles malformed small-model output well; OpenCode's models.dev schema validation is different infrastructure
- **The two-tier prompt cache** — well-implemented; eliminates 70% of redundant SOUL.md renders
- **The `errors` field clearing** — correct and needed
- **Vol28 overflow early-exit patterns** — correct; OVF-1 through OVF-6 all working
- **Git snapshot + rollback** — genuine advantage over OpenCode; keep and maintain
- **VectorStore memory** — persistent cross-session memory is a genuine advantage over OpenCode's stateless sessions
- **3,615+ passing tests** — must all pass after each phase

## What TO Simplify for Capable Models (Gemma 4 31B+)

These nodes/heuristics exist because small models fail at them. On a capable model they add latency without adding value:

| Node/Heuristic | Why it existed | Why to skip for LARGE/FRONTIER |
|---|---|---|
| `analyst_delegation_node` | Small models miss relevant files | 31B model reasons about file relevance inline |
| `plan_validator_node` | Small model plans reference unregistered tools | 31B plans are well-formed; validate inline in planning_node |
| `step_controller` read-then-modify heuristic | Small models write before reading | 31B follows read-first instructions reliably |
| `step_controller` premature-completion detection | Small models say "done" after one tool | 31B knows when a task is actually complete |
| Recovery cap of 12 attempts | Many retries needed for unreliable models | 31B failing 5+ times = genuinely stuck; ask user |
| Verification for simple single-tool tasks | Small models make subtle mistakes | 31B self-corrects; skip verify for clearly bounded tasks |

---

## Prioritised Work List (Implement in Order)

Items marked **DONE** are implemented and have regression tests. Remaining items are ordered by impact-to-effort ratio.

| Priority | Item | File(s) | Effort | Impact | Status |
|---|---|---|---|---|---|
| 1 | `_prune_tools()` never called | `context_builder.py` | 30 min | HIGH — NANO gets 35 tools | **DONE** |
| 2 | Thinking block detection before tool parse | `perception_node.py` | 2h | HIGH — extra round per thinking response | **DONE** |
| 3 | Global recovery counter missing | `state.py`, `builder.py` | 2h | HIGH — unbounded debug/replan loops | **DONE** |
| 4 | Skip analyst_delegation + plan_validator for LARGE/FRONTIER (P3b-A/B) | `builder.py` | 3h | HIGH — removes 2–3 extra node hops for capable models | **DONE** |
| 5 | Direct execution mode for simple tasks on LARGE/FRONTIER (P3b-E) | `builder.py` | 2h | HIGH — matches OpenCode throughput for 1-tool tasks | **DONE** |
| 6 | Project config `.agent-context/config.json` (OP-5) | `config_loader.py`, `context_builder.py`, `file_tools.py` | 4h | HIGH — per-project instructions/tools/permissions | — |
| 7 | Tool output truncation 50KB (OP-9) | `execution_node.py` | 2h | HIGH — prevents context blowout on large bash output | **DONE** |
| 8 | Graduated corrective prompts (P1-D) | `perception_node.py` | 2h | HIGH — corrective spam on small models | **DONE** |
| 9 | Lightweight step_controller for LARGE/FRONTIER (P3b-C) | `step_controller_node.py` | 2h | MEDIUM — removes read-then-modify false positives | **DONE** |
| 10 | Reduce LARGE/FRONTIER recovery caps to 5–6 (P3b-D) | `builder.py` | 30 min | MEDIUM — stops thrash loops faster | **DONE** |
| 11 | Tier not checked in routing for NANO/SMALL (P3-A + P3-B) | `builder.py`, `verification_node.py` | 1 day | MEDIUM — NANO traverses expensive nodes | **DONE** |
| 12 | `execute_tool` blocks event loop (P2-B) | `execution_node.py` | 2h | MEDIUM — long tools stall agent | **DONE** |
| 13 | Compaction re-fires every turn (P2-C) | `perception_node.py`, `state.py`, `task_lifecycle.py` | 1h | MEDIUM — CPU waste, stale context | **DONE** |
| 14 | PRUNE_PROTECT = 40K (OP-3) | `perception_node.py` | 1h | MEDIUM — prevents over-pruning recent context | **DONE** |
| 15 | Overflow detection uses real limits (OP-4) | `perception_node.py`, `provider_context.py` | 1h | MEDIUM — correct overflow math | **DONE** |
| 16 | Memory leaks in AgentState (P1-G) | `execution_node.py`, `perception_node.py` | 1h | MEDIUM — slow long sessions | **DONE** |
| 17 | Protected tool outputs in compaction (OP-10) | `perception_node.py` | 1h | MEDIUM — TODO/skill outputs survive pruning | **DONE** |
| 18 | Structured compaction format (OP-2) | `distiller.py` | 2h | MEDIUM — more parseable summaries | **DONE** |
| 19 | History pruning uses `len/4` (P2-D) | `perception_node.py` | 1h | LOW — over-prunes code-heavy content | **DONE** |
| 20 | YAML scalar falls through to custom parser (P2-H) | `tool_parser.py` | 1h | LOW — inefficient fallback | **DONE** |
| 21 | Dead router functions (P2-E) | `builder.py` | 1h | LOW — code confusion | **DONE** |
| 22 | Compacted message markers (OP-8) | `distiller.py` | 1h | LOW — TUI diagnostic quality | **DONE** |

---

## Success Criteria

| Tier | Metric |
|---|---|
| NANO (7B) | ≤8 tools in prompt; completes single-file tasks without overflow; zero corrective loops after fix |
| SMALL (9–14B) | 3-step tasks work end-to-end; thinking-block responses parsed on first attempt; graduated prompts fire correctly |
| MEDIUM (30–70B) | Full pipeline; planning, execution, verification, debug all work; global recovery cap prevents runaway |
| LARGE/FRONTIER | Parallel tools; reasoning models; per-role model routing; long plans survive across sessions |

---

## Estimated Effort

| Phase | Duration | Risk |
|---|---|---|
| Phase 1 — Small model stabilization | 1 week | Low — targeted fixes, no structural change |
| Phase 2 — Correctness & stability | 1–2 weeks | Medium — async changes need careful testing |
| Phase 3 — Tier-aware routing | 1–2 weeks | Medium — new routing logic, extensive mocking |
| Phase 4 — Frontier optimization | 2–3 weeks | Low-medium — additive features |

**Total: 5–8 weeks**

Phase 1 items P1-B (`_prune_tools`) and P1-C (thinking block ordering) are the highest ROI fixes — each takes under 2 hours and directly unblocks small model usability.

---

## Gemma 4 Optimization

### Target Hardware Context

**Primary:** LM Studio on 16 GB VRAM. This constrains which Gemma 4 model can run and how to configure it:

| Model | Parameters | Active | VRAM (Q4_K_M) | Context (native) | LiveCodeBench | Tier |
|---|---|---|---|---|---|---|
| **Gemma 4 E4B** | 4.5B total (MoE) | 4B active | ~4 GB | 128K | 52% | SMALL |
| **Gemma 4 26B A4B** | 26B total (MoE) | 4B active | ~6–8 GB | 256K | 77% | MEDIUM |
| **Gemma 4 31B** | 31B dense | 31B | ~20 GB | 256K | 80% | LARGE (not on 16 GB) |

The **26B A4B** is the primary performance target on 16 GB hardware. It runs at 4B inference speed (similar to E4B) but delivers 25 percentage points more coding capability. **E4B is the fallback** for slower machines or when the 26B model doesn't load cleanly.

### Gemma 4 Architecture Properties

These properties directly change how CodingAgent should interact with it:

- **Native function calling** — Gemma 4 has structured output support for tool calls. `supports_native_tools: false` in providers.json is **wrong**; it forces YAML fallback mode unnecessarily and wastes tokens on format guidance.
- **Configurable thinking** — Gemma 4 supports budget-controlled thinking (`thinking_budget` param). `disable_thinking: true` in providers.json is **wrong** for planning/debugging roles where reasoning improves output quality. Thinking should be **enabled with budget control** (`budget_tokens: 2048` for SMALL E4B, `budget_tokens: 4096` for MEDIUM 26B), not disabled globally.
- **128K / 256K context** — The context budget ceiling in `get_context_budget()` was raised to 131072 (Vol29) and is now dynamic. For 26B A4B (256K), the ceiling should reach 196608 (75% of 256K, leaving room for output).
- **Temperature sensitivity** — Gemma 4 thinking models perform better with `temperature: 0.0` for structured output tasks (tool calls, plan generation) and `temperature: 0.3–0.7` for creative synthesis tasks. The provider config has no per-role temperature; this should be added.

### Required providers.json Corrections

**File:** `src/config/providers.json` (lm_studio entry)

```json
{
  "name": "lm_studio",
  "models": ["gemma-4-26b-a4b-it", "gemma-4-e4b-it"],
  "supports_native_tools": true,
  "disable_thinking": false,
  "thinking_budget_tokens": 4096,
  "temperature_structured": 0.0,
  "temperature_creative": 0.4,
  "context_length": null
}
```

Key changes from current config:
- `supports_native_tools: true` — enables JSON native tool call format
- `disable_thinking: false` — enables thinking with budget; use `thinking_utils.budget_max_tokens()` to set per role
- `thinking_budget_tokens: 4096` — cap for planning/debug roles (omit or set 0 for execution role where speed matters more)
- `temperature_structured: 0.0` — for tool calls, YAML parsing, plan generation
- Remove `context_length: null` — already fixed in Vol29; keep omitted so dynamic probe applies

### Gemma 4 Tier Classification Fix

**File:** `src/core/inference/model_tiers.py`

The `_GEMMA4_EDGE_PATTERNS` list correctly maps `E4B` → SMALL. The 26B A4B model must be MEDIUM (not SMALL, despite 4B active params) because its capabilities match MEDIUM:

```python
_GEMMA4_26B_PATTERNS = [
    "gemma-4-26b", "gemma4-26b", "gemma_4_26b",
    "gemma-4-a4b", "gemma4-a4b",   # common LM Studio naming
]
# These should classify as MEDIUM (or LARGE if 256K context confirmed)
```

The current classification uses name-matching only. Add a **context-window probe** as a secondary signal:
- If a model name is ambiguous (e.g., `gemma-4` with no size indicator) and `get_loaded_context_length()` returns >64K, classify as MEDIUM.
- If context window >128K, never classify below MEDIUM regardless of name.

### Gemma 4 Optimized System Prompt

**File:** `src/config/agent-brain/roles/operational-gemma4.md` (new)

Modelled on OpenCode's `gemini.txt` — a provider-specific variant of the core operational prompt tuned for Gemma 4's behaviour:

```markdown
<!-- operational-gemma4.md — used when model_name matches gemma-4 patterns -->
You are an expert software engineering assistant running locally.

## Core Workflow
For every task:
1. **Understand** — read relevant files before writing anything
2. **Plan** — decide the minimum set of changes needed
3. **Implement** — make the changes using tool calls
4. **Verify** — check the result is correct

## Tool Output Format
Always output tool calls in YAML:
```yaml
name: tool_name
arguments:
  key: value
```
One tool call per response. Wait for the result before calling the next tool.

## Constraints
- Absolute file paths only (no relative paths)
- No explanations before the tool call — output the YAML directly
- If you need to reason, use <think> tags; your reasoning will not be shown
- Do not use placeholder values — read the actual file to get real values
- Prefer editing existing files over creating new ones

## When Stuck
If a tool returns an error, read the error carefully and fix it in your next call.
If you have tried 2 times and failed, call `respond` to ask for help.
```

This prompt is:
- **20% shorter** than `operational.md` (token savings matter on a 128K window)
- **YAML-only** format instruction (Gemma 4 understands both; YAML is more reliable for small models)
- **No parallel tool call** instruction (E4B doesn't support them; 26B A4B MAY but the system doesn't yet)
- **Explicit `<think>` guidance** so the model knows thinking is available and expected

### Capability-First Model Classification

Currently `classify_model()` relies on name substring matching. For ambiguous names (custom GGUF filenames, LM Studio aliases), this fails silently and defaults to MEDIUM.

Replace with a **capability probe chain** in `model_tiers.py`:

```python
def classify_model(
    model_name: str,
    context_window: int | None = None,
    supports_function_calling: bool | None = None,
) -> ModelTier:
    """Classify by capabilities first, then name fallback."""

    # 1. Context window signals: >128K → at least MEDIUM
    if context_window and context_window > 128_000:
        if context_window >= 200_000:
            return ModelTier.LARGE  # 200K+ = assume large or frontier
        # else continue name matching but floor at MEDIUM

    # 2. Name matching (existing logic) ...
    name_tier = _classify_by_name(model_name)

    # 3. Apply context window floor
    if context_window and context_window > 128_000:
        if name_tier.value < ModelTier.MEDIUM.value:
            return ModelTier.MEDIUM

    return name_tier
```

Wire `context_window` through from `get_loaded_context_length()` → `perception_node.py` → `classify_model()`.

### Context Budget Ceilings (256K Models)

**File:** `src/core/inference/provider_context.py`

Update the tier-specific ceilings to handle 256K models:

```python
_TIER_MAX_TOKENS = {
    "nano":     32_768,    # 32K ceiling (model window typically 8K)
    "small":    65_536,    # 64K ceiling
    "medium":  131_072,    # 128K ceiling (covers Gemma 4 E4B, 26B A4B at 75%)
    "large":   196_608,    # 192K ceiling (for 256K models; 75%)
    "frontier": 196_608,   # same
}
```

The existing ceiling of 131072 is correct for E4B but caps the 26B A4B (256K model) unnecessarily. LARGE/FRONTIER should be allowed to use up to 75% of 256K = 196608.

---

## OpenCode Best Practices Adoption

Analysis of `/Users/tann200/WebstormProjects/opencode` found 8 structural practices that should be adopted in CodingAgent. These are not about copying code — they are design decisions that make the system more reliable.

### OP-1: Per-Provider System Prompts

**OpenCode approach:** `session/prompt/` contains `default.txt`, `gemini.txt`, `anthropic.txt`, `gpt.txt`, `beast.txt`, `kimi.txt`, `codex.txt`, `copilot-gpt-5.txt`. Each file is tuned to the specific model family's strengths and quirks.

**CodingAgent current approach:** Single `operational.md` with `operational-small.md` and `operational-frontier.md` variants — only 3 files for 5 tiers and unlimited provider permutations.

**Adoption:** Create per-provider prompt overlays that are merged with the tier prompt:
- `operational-gemma4.md` — YAML-first, think-block aware, no parallel tools (already specified above)
- `operational-ollama.md` — same as gemma4 but with reduced context assumptions
- `operational-openrouter.md` — JSON native tools, thinking optional
- Keep existing `operational.md` as default fallback

**Wire-up in ContextBuilder:**
```python
def _select_role_prompt(self, role_name: str, model_name: str, model_tier: str) -> str:
    """Select the most specific matching prompt file."""
    # Try: role-provider match → role-tier match → role default
    for variant in [
        f"{role_name}-{_get_provider_variant(model_name)}",  # operational-gemma4
        f"{role_name}-{model_tier}",                         # operational-small
        role_name,                                           # operational
    ]:
        path = self._roles_dir / f"{variant}.md"
        if path.exists():
            return path.read_text()
    raise FileNotFoundError(f"No prompt found for role {role_name}")
```

---

### OP-2: Structured Compaction Summary Format

**OpenCode approach (`session/compaction.ts`):** Compaction produces a structured summary with fixed sections:
```
Goal: <one-line task statement>
Instructions: <bullet list of learned constraints>
Discoveries: <important findings so far>
Accomplished: <completed steps>
Relevant files: <files modified/read with brief purpose>
```

**CodingAgent current approach:** Free-form summary via `distiller.py` — the LLM is prompted to summarize the session but the output format is uncontrolled. The prompt asks for JSON with `current_task`, `current_state`, `next_step` — minimal structure compared to OpenCode.

**Adoption:** Extend the distiller prompt to produce OpenCode-style structured output. The 6-section format is consistently parseable across models and survives re-injection into a new context window without confusion.

**File:** `src/core/memory/distiller.py`

```python
_COMPACTION_PROMPT_TEMPLATE = """
You are compacting a coding session. Produce a structured summary that preserves the most important context.

Output this exact JSON structure:
{
  "goal": "<one-line description of the overall task>",
  "instructions": ["<key constraint or learned rule>", ...],
  "discoveries": ["<important finding about the codebase>", ...],
  "accomplished": ["<completed step>", ...],
  "relevant_files": [{"path": "<abs path>", "role": "<purpose>"}],
  "next_step": "<what to do next>",
  "current_state": "<where the agent is in the task>"
}

History to compact:
{history}
"""
```

Add validation for all 7 keys (vs current 3-key validation).

---

### OP-3: Protected Context Window (PRUNE_PROTECT)

**OpenCode approach:** `PRUNE_PROTECT = 40_000` tokens — the most recent 40K tokens of history are **never pruned**, regardless of total size. `PRUNE_MINIMUM = 20_000` — only bother pruning if more than 20K tokens are reclaimable.

**CodingAgent current approach:** `_prune_tool_outputs()` keeps a rolling 15K token window and replaces old tool results with `[output truncated]`. The window is small (15K vs 40K), causing over-pruning of recent tool results.

**Adoption:**
```python
_PRUNE_PROTECT_TOKENS = 40_000   # match OpenCode; never prune most-recent 40K
_PRUNE_MINIMUM_GAIN   = 20_000   # only run pruning if ≥20K tokens recoverable

def _prune_tool_outputs(messages: list, budget: int) -> list:
    total = sum(count_tokens(m.get("content","")) for m in messages)
    if total - budget < _PRUNE_MINIMUM_GAIN:
        return messages  # not worth pruning
    # Walk backwards; skip messages in the protect zone
    ...
```

This prevents the case where a large tool output from 2 turns ago is kept but the tool output from last turn is pruned — which breaks sequential context.

---

### OP-4: Overflow Detection Using Actual Model Limits

**OpenCode approach (`session/overflow.ts`):**
```typescript
const isOverflow = (count: number, model: ModelConfig) =>
    count >= model.contextLength - model.maxOutputTokens - COMPACTION_BUFFER;
```
Uses the actual `contextLength` and `maxOutputTokens` from the loaded model config. `COMPACTION_BUFFER = 20_000` tokens reserved for the compaction output itself.

**CodingAgent current approach:** Overflow detection uses `_OVF_THRESHOLD_FRACTION = 0.85` of the context budget — a fraction of a fraction. The actual model limits are not used directly.

**Adoption:** Wire the actual context window from `get_loaded_context_length()` into overflow detection:

```python
_COMPACTION_BUFFER = 20_000  # tokens reserved for compaction output

def _is_overflow(token_count: int, context_window: int, max_output_tokens: int = 4096) -> bool:
    return token_count >= context_window - max_output_tokens - _COMPACTION_BUFFER
```

Pass `context_window` through from the adapter probe (already stored in `provider_context.py` via `set_active_context_length()`).

---

### OP-5: Project-Level Config (`.agent-context/config.json`)

**Problem:** CodingAgent's configuration is currently split across four separate files with no project-level override layer:
- `src/config/providers.json` — provider + model config (source-level, not user-editable)
- `~/.config/codeagent/prefs.json` — global user preferences
- `.agent-context/TASK_STATE.md` — per-session runtime state
- No file for project-specific settings (instructions, tool restrictions, model preference)

Users who want to say "this project always uses pnpm, never npm" must either edit the system prompt source or repeat the instruction in every chat message.

**OpenCode approach:** A single `opencode.jsonc` per project provides:
- `instructions` array — per-project system prompt additions
- `tools` map — enable/disable specific tools
- `permissions` — deny-write patterns for sensitive paths
- `model` — per-project model override

**Scope decision:** Implement a **project-scoped `config.json` only** (not full multi-layer merge). Full 4-layer merging (system → global → project → local) adds complexity without clear near-term value. A single project-level file covers the dominant use case.

---

#### Schema

**File:** `.agent-context/config.json` in the project working directory

```json
{
  "model": "gemma-4-26b-a4b-it",
  "instructions": [
    "This project uses pnpm, never npm or yarn.",
    "Tests live in tests/unit/, run with .venv/bin/pytest -p no:logging.",
    "Never modify migration files in db/migrations/."
  ],
  "tools": {
    "web_search": false,
    "bash": true
  },
  "permissions": {
    "deny_write": [
      "migrations/*",
      ".env*",
      "*.pem",
      "*.key"
    ]
  },
  "compaction": {
    "prune": true,
    "protect_tokens": 40000
  }
}
```

All fields are optional. An absent field means "use system default".

---

#### Implementation

**File:** `src/core/config_loader.py` — add `load_project_config()`:

```python
import json
from pathlib import Path
from typing import Any

_PROJECT_CONFIG_FILENAME = "config.json"

def load_project_config(working_dir: str) -> dict[str, Any]:
    """Load .agent-context/config.json if present. Returns {} on missing/invalid."""
    path = Path(working_dir) / ".agent-context" / _PROJECT_CONFIG_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"load_project_config: failed to parse {path}: {e}"
        )
        return {}

def get_project_instructions(working_dir: str) -> list[str]:
    """Return the instructions array from the project config."""
    return load_project_config(working_dir).get("instructions", [])

def get_project_tool_overrides(working_dir: str) -> dict[str, bool]:
    """Return tool enable/disable overrides. {tool_name: True/False}"""
    return load_project_config(working_dir).get("tools", {})

def get_project_deny_write_patterns(working_dir: str) -> list[str]:
    """Return path patterns that must never be written."""
    return load_project_config(working_dir).get("permissions", {}).get("deny_write", [])

def get_project_model_override(working_dir: str) -> str | None:
    """Return the per-project model name override, or None."""
    return load_project_config(working_dir).get("model")
```

---

#### Wire-up: Instructions → System Prompt

**File:** `src/core/context/context_builder.py` — in `_build_static_system_prefix()`, after the skills block:

```python
# OP-5: Project-specific instructions from .agent-context/config.json
try:
    from src.core.config_loader import get_project_instructions
    _project_instructions = get_project_instructions(str(self._agent_context_dir.parent))
    if _project_instructions:
        parts.append(
            "<project_instructions>\n"
            + "\n".join(f"- {i}" for i in _project_instructions)
            + "\n</project_instructions>"
        )
except Exception:
    pass
```

These instructions appear after role content but before the tools block — the LLM sees them as constraints that override the default role behaviour.

---

#### Wire-up: Tools → Orchestrator Registration

**File:** `src/core/orchestration/orchestrator.py` — in `__init__()` or `start_new_task()`, after tool registration:

```python
# OP-5: Apply per-project tool enable/disable overrides
from src.core.config_loader import get_project_tool_overrides
_tool_overrides = get_project_tool_overrides(self.working_dir or "")
for tool_name, enabled in _tool_overrides.items():
    if not enabled and tool_name in self.tool_registry.tools:
        self.tool_registry.deregister(tool_name)
        logger.info(f"Project config: disabled tool '{tool_name}'")
```

This runs once per task, not per LLM call. If a tool is disabled in the project config it is removed from the registry — it won't appear in the tools list sent to the model.

---

#### Wire-up: Permissions → Write Guard

**File:** `src/tools/file_tools.py` — in `write_file()` and `edit_file()`, before the write:

```python
from src.core.config_loader import get_project_deny_write_patterns
import fnmatch

def _check_write_permission(abs_path: str, working_dir: str) -> None:
    """Raise PermissionError if path matches a deny_write pattern."""
    deny_patterns = get_project_deny_write_patterns(working_dir)
    rel_path = str(Path(abs_path).relative_to(working_dir))
    for pattern in deny_patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            raise PermissionError(
                f"write blocked by project config: '{rel_path}' matches deny pattern '{pattern}'"
            )
```

The error propagates back to the LLM as a tool result with `ok: False` — the model sees the denial and can choose a different path or ask the user.

---

#### Wire-up: Model Override → Adapter Selection

**File:** `src/core/orchestration/inference_loop.py` — before model selection:

```python
from src.core.config_loader import get_project_model_override
_model_override = get_project_model_override(working_dir or "")
if _model_override:
    model_name = _model_override  # project config wins over provider default
    logger.info(f"inference_loop: using project model override '{model_name}'")
```

This lets teams check in a `config.json` that pins a specific model (e.g., `gemma-4-26b-a4b-it`) so the agent doesn't silently switch if the user changes their global default.

---

#### Caching

`load_project_config()` reads from disk on every call. For hot paths (every LLM call), add a per-working-dir LRU cache with mtime invalidation — same pattern as `_read_text_cached()` in `context_builder.py`:

```python
_PROJECT_CONFIG_CACHE: dict[str, tuple[float, dict]] = {}  # path → (mtime, config)

def load_project_config(working_dir: str) -> dict[str, Any]:
    path = Path(working_dir) / ".agent-context" / _PROJECT_CONFIG_FILENAME
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    cached_mtime, cached_config = _PROJECT_CONFIG_CACHE.get(str(path), (0, {}))
    if mtime == cached_mtime:
        return cached_config
    try:
        config = json.loads(path.read_text())
        _PROJECT_CONFIG_CACHE[str(path)] = (mtime, config)
        return config
    except Exception as e:
        logger.warning(f"load_project_config: {path}: {e}")
        return {}
```

---

#### Tests

- `test_project_config_instructions_injected` — `.agent-context/config.json` with instructions → system prompt contains `<project_instructions>` block
- `test_project_config_tool_disabled` — `tools: {bash: false}` → bash not in tools list after `start_new_task()`
- `test_project_config_deny_write_blocks` — `permissions.deny_write: [".env*"]` → write to `.env` raises PermissionError
- `test_project_config_model_override` — `model: gemma-4-26b-a4b-it` → `inference_loop` uses that model regardless of global default
- `test_project_config_missing_returns_empty` — no file → all accessors return empty defaults without errors
- `test_project_config_invalid_json_returns_empty` — malformed JSON → warning logged, empty dict returned
- `test_project_config_mtime_cache` — file read twice with same mtime → disk read happens once

---

**Effort:** 4 hours (schema + 4 wire-ups + caching + tests)
**Impact:** HIGH — directly enables per-project customization without touching source; unblocks teams using the agent across multiple repos with different conventions

---

### OP-6: `multiedit` — Atomic Multi-File Edits

**OpenCode's `multiedit` tool:** Applies multiple file edits atomically — all succeed or all fail. Reduces the number of tool call round-trips for refactors that touch multiple files.

**CodingAgent current approach:** One `edit_file` call per file. A refactor touching 5 files requires 5 round-trips, each with an LLM call to generate the next edit. For small models, this compounds context pressure.

**Adoption:** Add `multi_edit_files(edits: list[dict]) → dict` to `file_tools.py`:

```python
async def multi_edit_files(edits: list[dict]) -> dict:
    """Apply multiple edits atomically. Each edit: {path, old_string, new_string}."""
    results = []
    errors = []
    for edit in edits:
        try:
            _apply_edit(edit["path"], edit["old_string"], edit["new_string"])
            results.append({"path": edit["path"], "status": "ok"})
        except Exception as e:
            errors.append({"path": edit["path"], "error": str(e)})
    if errors:
        # Rollback successful edits (atomic guarantee)
        ...
        return {"ok": False, "errors": errors}
    return {"ok": True, "edited": len(results)}
```

Register as `multi_edit_files` tool. Planning prompt for MEDIUM+ can use this to generate multi-file edits in a single step.

---

### OP-7: Structured TODO List Tool

**OpenCode's `todo` tool:** Each TODO item has a `status` field (in-progress, completed, cancelled). The tool call format: `{action: "update", id: "...", status: "completed"}`. The assistant always updates TODO status before completing a task, making progress visible.

**CodingAgent current approach:** `manage_todo` tool exists with similar schema. The main gap is that `perception_node` does not inject current TODO state into the system prompt — the LLM doesn't "see" what it has accomplished unless it explicitly reads the todo file.

**Adoption:** In `perception_node.py`, after loading history, inject the current TODO state if it exists:

```python
# Inject current TODO state into the prompt (prevents replanning accomplished work)
todo_state = _load_todo_state(working_dir)
if todo_state:
    env_block_parts.append(
        f"<current_todos>\n{todo_state}\n</current_todos>"
    )
```

---

### OP-8: Compacted Tool Output Markers

**OpenCode approach:** When compaction occurs, old tool results are not deleted — they are marked `time.compacted = Date.now()`. A compacted message shows its original call but its output is replaced with `[compacted]`. This preserves message threading while freeing tokens.

**CodingAgent current approach:** `_prune_tool_outputs()` replaces old tool outputs with a `[output truncated — N chars]` placeholder in the message content. This is similar but not structured — the message itself is mutated, which can cause issues if the message is later re-injected.

**Adoption:** Add a `_compacted` metadata flag to pruned messages:

```python
def _prune_tool_outputs(messages, budget):
    ...
    for msg in messages:
        if should_prune(msg):
            msg = dict(msg)  # copy
            msg["content"] = "[compacted]"
            msg["metadata"] = {**msg.get("metadata", {}), "_compacted": True, "_original_len": len(original)}
        result.append(msg)
    return result
```

This allows the TUI to display `[compacted]` differently and lets future diagnostic code identify which messages were pruned.

---

## OpenCode Best Practices — Adoption Priority

| Item | Effort | Impact | Phase | Status |
|---|---|---|---|---|
| OP-1 Per-provider prompts | 2h | HIGH — Gemma 4 tuning | Phase 1 | DONE |
| OP-5 Project config (instructions, tools, permissions, model override) | 4h | HIGH — per-project customisation | Phase 1 | — |
| OP-9 Tool output truncation (50KB) | 2h | HIGH — prevents context blowout on large bash output | Phase 2 | — |
| OP-3 PRUNE_PROTECT 40K | 1h | MEDIUM — prevents over-pruning recent context | Phase 2 | — |
| OP-4 Actual overflow detection | 1h | MEDIUM — correct overflow math using real limits | Phase 2 | — |
| OP-2 Structured compaction format | 2h | MEDIUM — more parseable summaries | Phase 2 | — |
| OP-10 Protected tool outputs in compaction | 1h | MEDIUM — TODO/skill outputs survive pruning | Phase 2 | — |
| OP-8 Compacted markers | 1h | LOW — diagnostic quality, TUI display | Phase 2 | — |
| OP-7 TODO injection in perception | 1h | MEDIUM — prevents replanning completed work | Phase 3 | — |
| OP-11 Reflection loop in FRONTIER prompt | 1h | MEDIUM — reduces confident-but-wrong edits | Phase 4 | — |
| OP-6 multiedit tool | 3h | MEDIUM — fewer round-trips on MEDIUM+ | Phase 3 | — |

---

## Performance Target vs OpenCode

To perform **on par or better** than OpenCode on local models:

**Where CodingAgent has advantages:**
- Persistent session memory across tasks (VectorStore + distiller)
- Plan DAG with dependency tracking (parallel execution for FRONTIER)
- Git snapshot manager (rollback on debug exhaustion)
- LSP integration (go-to-definition, diagnostics in context)
- MCP stdio server (embeds as a tool provider for other agents)

**Where OpenCode has advantages (gaps to close):**
- Per-provider system prompts (OP-1) — **highest priority**
- Context compaction quality (OP-2, OP-3, OP-4) — prevents context cliff
- Project-level instructions (OP-5) — user experience
- Tool reliability: OpenCode's `apply_patch` handles partial/fuzzy diffs better than CodingAgent's `edit_file` for large files

**Parity checklist:**
- [x] Gemma 4 providers.json corrected (`supports_native_tools: true`, `disable_thinking: false`) — **DONE**
- [x] `operational-gemma4.md` created and auto-selected by ContextBuilder — **DONE**
- [x] Tier-classified 26B A4B as MEDIUM (not FRONTIER) — **DONE**
- [x] NANO gets ≤8 tools (P1-B) — **DONE**
- [x] Thinking blocks parsed on first attempt (P1-C) — **DONE**
- [x] Global recovery cap (P2-A) — **DONE**
- [ ] `.agent-context/config.json` with instructions, tools, permissions, model override (OP-5)
- [ ] Tool output auto-truncation at 50KB (OP-9)
- [ ] PRUNE_PROTECT = 40K tokens (OP-3)
- [ ] Overflow detection uses actual model context window (OP-4)
- [ ] Protected tool outputs never pruned in compaction (OP-10)
- [ ] FRONTIER prompt with reflection loop (OP-11)

---

## Additional Findings from Deep Analysis

A deeper OpenCode analysis surfaced 3 more practices not in the initial audit:

### OP-9: Tool Output Auto-Truncation (50KB limit)

**OpenCode approach (`tool.ts`):** All tool outputs are automatically truncated to ~50KB. When truncated, metadata carries `truncated: true` and an output file path so the LLM can access the full content if needed.

**CodingAgent gap:** Tool outputs have no hard size limit. A `bash` command that returns 200KB of output is injected directly into conversation history, consuming a large fraction of the context window.

**Adoption (Phase 2):**
```python
_TOOL_OUTPUT_MAX_CHARS = 51_200  # 50KB hard cap per tool result

def _truncate_tool_output(output: str, tool_name: str) -> tuple[str, bool]:
    if len(output) <= _TOOL_OUTPUT_MAX_CHARS:
        return output, False
    truncated = output[:_TOOL_OUTPUT_MAX_CHARS]
    return truncated + f"\n[... {len(output) - _TOOL_OUTPUT_MAX_CHARS} chars truncated]", True
```

Apply in `tool_execution_service.py` after every tool execution. Log truncation events to telemetry.

---

### OP-10: Protected Tool Outputs in Compaction

**OpenCode approach (`compaction.ts`):**
```typescript
const PRUNE_PROTECTED_TOOLS = ["skill"]
// Skill tool outputs are never erased during compaction
```

**CodingAgent gap:** `_prune_tool_outputs()` prunes based on age and size only — it doesn't protect the outputs of specific tools (e.g., `read_file` outputs for files still being edited, `manage_todo` outputs that define the current task).

**Adoption (Phase 2):** Add a `_PRUNE_PROTECTED_TOOLS` set and skip those messages when walking backwards during compaction:

```python
_PRUNE_PROTECTED_TOOLS = {
    "manage_todo",   # Current TODO state should survive compaction
    "memory_save",   # Explicit memory saves should not be erased
    "read_file",     # File content for actively-edited files (last 2 reads only)
}
```

Pair with OP-8 (compacted markers) — protected outputs get `_compacted: False` even after the context budget is exceeded.

---

### OP-11: Reflection Loop in FRONTIER Prompt

**OpenCode `beast.txt` approach:** For reasoning-capable models (GPT-4/o1/o3, and by analogy Claude Opus/Sonnet), OpenCode uses a "beast mode" prompt that requires:
1. Extensive research (recursive URL fetching, multiple grep passes)
2. **Reflection before each tool call** — explicitly think about edge cases
3. Full implementation — no partial solutions, no stubs
4. Testing edge cases before marking complete

**CodingAgent gap:** `operational-frontier.md` is 85 lines of general instructions with no explicit reflection requirement. For frontier models that can afford it (LARGE/FRONTIER tier), adding a reflection step before high-stakes tool calls (write_file, bash) reduces incorrect edits.

**Adoption:** Extend `operational-frontier.md` with a reflection block:

```markdown
## Before Making Changes

Before calling write_file or edit_file:
1. Re-read the relevant section of the file you're about to change
2. Verify your change does not break existing imports, function signatures, or tests
3. Check: does this change handle the edge case mentioned in the task?

Before calling bash with a destructive command:
1. Re-state what the command will do
2. Confirm the working directory
3. Proceed only if confident
```

This adds ~15 tokens to every frontier prompt but prevents a class of confident-but-wrong edits that are common on complex multi-file tasks.
