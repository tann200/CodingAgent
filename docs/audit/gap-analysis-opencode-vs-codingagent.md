# Gap Analysis: CodingAgent vs opencode
## Workflow, System Prompts, and Model Compatibility

**Date:** 2026-04-10  
**Scope:** Architecture, pipeline design, system prompts, model-tier handling, and agent initialization — with improvement recommendations for 9B and frontier models.

---

## 1. Architecture Overview

| Dimension | CodingAgent | opencode |
|---|---|---|
| Runtime | Python / LangGraph | TypeScript / Effect.ts |
| Execution model | 14-node state machine (DAG) | Simple loop-until-finish |
| Entry point | `perception_node` → route decision | `session.run()` → tool calls → loop |
| Planning | Separate `strategic` role node, JSON DAG | Planning mode = same agent, same turn |
| Subagents | `delegate_task()` → child orchestrator via CrossSessionBus | `Task` tool → inline `explore` agent |
| Tool calling | YAML format (NANO/SMALL), native JSON (MEDIUM+) | Always native function calling |
| Compaction | `distill_context()` → VectorStore → checkpoint | Token-count threshold → compaction agent turn |
| Memory | VectorStore (session + cross-session), distiller | `.github/instructions/memory.instruction.md` |

### CodingAgent Pipeline (14 nodes)

```
perception → analysis → analyst_delegation → planning → plan_validator
   → execution → step_controller → verification → debug → replan
   → evaluation → memory_sync → delegation → END
```

Fast-path: simple tasks skip from `perception` directly to `execution` (bypasses 5+ nodes).

### opencode Loop

```
user_message → [tool calls in parallel] → [more tool calls] → assistant_message
```

No state machine. The model drives everything. Context is trimmed by a compaction agent
when token count exceeds threshold.

---

## 2. System Prompt Comparison

### 2.1 opencode — Model-specific selection

`src/session/system.ts` routes by model ID string to one of 8 prompt files:

| Model match | Prompt | Lines |
|---|---|---|
| `gpt-4`, `o1`, `o3` | `beast.txt` | 147 |
| `gpt` + `codex` | `codex.txt` | 79 |
| other `gpt` | `gpt.txt` | 107 |
| `gemini-` | `gemini.txt` | 155 |
| `claude` | `anthropic.txt` | 105 |
| `trinity` | `trinity.txt` | 97 |
| `kimi` | `kimi.txt` | 114 |
| fallback | `default.txt` | 105 |

Plus: `plan.txt` (26 lines), `plan-reminder-anthropic.txt` (67 lines) appended in planning mode.

**Key differentiators:**
- `beast.txt` (o1/o3/gpt-4): 10-phase exhaustive workflow, mandatory web research, recursive URL fetching, memory file, `continue` mode resume
- `anthropic.txt`: `TodoWrite` tracking, `Task` tool for parallel exploration, thinking-heavy
- `default.txt` / `gpt.txt`: ultra-concise, ≤4 lines output, no preamble, one-word answers
- `gemini.txt`: structured output, search-first approach
- `kimi.txt` / `codex.txt`: provider-specific quirks addressed

**Small model handling:** `ProviderTransform.smallOptions()` disables reasoning tokens per-provider (e.g., `thinkingBudget: 0` for Gemini, `reasoningEffort: "minimal"` for OpenAI, `disableThinking: true` for Venice). No tool count reduction — small models still get full tool list.

### 2.2 CodingAgent — Role-based prompts + tier partials

| File | Lines | Usage |
|---|---|---|
| `identity/SOUL.md` | 39 | Injected into every node via `ContextBuilder` |
| `roles/operational.md` | 163 | perception, execution, verification nodes |
| `roles/strategic.md` | 102 | planning, replan nodes |
| `roles/analyst.md` | 75 | analysis, analyst_delegation nodes |
| `roles/debugger.md` | 83 | debug node |
| `roles/reviewer.md` | 70 | evaluation node |
| `roles/researcher.md` | 19 | (stub) |

**Tier partials** (from `ContextBuilder._render_tools_for_tier`):
- NANO/SMALL: first sentence of each tool description only; NANO uses YAML-only output
- MEDIUM+: full descriptions, native JSON tool calling

**Current gaps vs opencode:**
- No per-model-ID routing — only provider-level partials (`anthropic.txt`, `openai.txt`)
- `operational.md` at 163 lines is too heavy for 9B models (high token cost, complex format requirements)
- Small models receive the same `PLAN-ACT-OBSERVE` format with `RESULT:` / `STATUS:` / `FILES_CHANGED:` / `OBSERVE:` — a demanding 4-field output format
- No equivalent to beast.txt for reasoning models (o1/o3/claude-opus)
- No "continue/resume" instruction in any prompt
- No mandatory reflection-before-tool-call for reasoning models

---

## 3. Gap Analysis: Small Models (≤14B / 9B)

### GAP-SMALL-1: Output format is over-specified for 9B models

**CodingAgent:** Every operational response must end with:
```
RESULT: <summary>
STATUS: complete | partial | failed
FILES_CHANGED: <paths>
OBSERVE: <observation>
```

**Impact:** 9B models frequently omit fields, misformat them, or emit extra prose between tool calls. The parser fails silently and the node treats the response as having no status signal.

**opencode:** No required output format. The model uses native tool calls; text output is free-form.

**Recommendation:** For SMALL/NANO tiers, collapse to a single optional line:
```
STATUS: complete|partial|failed
```
and infer `FILES_CHANGED` from the tool calls actually made. Drop `RESULT:` and `OBSERVE:` for these tiers — the tool call record itself provides the observation.

**Files to change:** `roles/operational.md` (add SMALL-tier variant section), `src/core/context/context_builder.py` (suppress format block for NANO/SMALL).

---

### GAP-SMALL-2: operational.md is 163 lines — too large for small context windows

At ~3,500 tokens (with examples), `operational.md` consumes 22–87% of a 4K–16K context budget before any user message is added. The tool table, format examples, and delegation section all contribute.

**Recommendation:** Add `roles/operational-small.md` (target ≤60 lines) that strips:
- The full tool table (replaced by: "Use the exact tool names. Type `?` to see all tools.")
- All format examples
- The delegation section (9B models should not initiate delegation)
- The "Replan Signal" section (not relevant at SMALL tier)

`ContextBuilder` selects between the two based on `model_tier`.

**Estimated savings:** ~2,200 tokens per turn for NANO/SMALL.

---

### GAP-SMALL-3: `is_simple_mode` only covers NANO; SMALL models also struggle with multi-tool turns

Currently, YAML tool format (one tool per turn) applies only to NANO. SMALL models (7–14B) receive full native JSON calling with no guardrails.

**opencode:** Uses `ProviderTransform.smallOptions()` to disable reasoning/thinking tokens for small models, which reduces token waste. No per-turn tool limit is enforced.

**Recommendation:** Extend `is_simple_mode` to SMALL tier for providers without proven function-calling reliability (Ollama local, LM Studio with non-instruct models). Add a `provider_supports_parallel_tools` flag to `providers.json`; default to `False` for local providers.

**Files to change:** `src/core/inference/model_tiers.py`, `src/config/providers.json`.

---

### GAP-SMALL-4: No clarify-before-execute step for ambiguous tasks

**opencode:** Models ask clarifying questions naturally (no structural enforcement needed for frontier models). For small models, the default prompt says nothing about ambiguous tasks.

**CodingAgent:** `perception_node` tries to extract `next_action` immediately. Ambiguous user messages result in bad plans.

**Recommendation:** Add a `needs_clarification` field to `AgentState`. In `perception_node`, when `model_tier ∈ {NANO, SMALL}` and no clear action is extractable, set `needs_clarification=True` and route directly to `END` with a clarifying question. This prevents the 14-node pipeline from being entered on a bad premise.

**Files to change:** `src/core/orchestration/graph/state.py`, `src/core/orchestration/graph/nodes/perception_node.py`, `src/core/orchestration/graph/builder.py`.

---

### GAP-SMALL-5: Doom-loop detection too aggressive for small models

The current loop guard increments `no_plan_fail_count` whenever execution returns `ok=False`. Small models fail more often on format issues (malformed YAML, wrong tool name) than on actual task failure. The guard cuts the session before the model has a chance to recover.

**Recommendation:**
- Distinguish format errors (`status="format_error"`) from task errors (`status="failed"`)
- Only increment `no_plan_fail_count` on task errors, not format errors
- For NANO/SMALL: allow up to 5 format-error retries per step before escalating

**Files to change:** `src/core/orchestration/graph/nodes/execution_node.py`, `src/core/orchestration/loop_guards.py`.

---

### GAP-SMALL-6: No `thinking`/reasoning token suppression for local providers

opencode's `ProviderTransform.smallOptions()` explicitly disables thinking tokens (e.g., `thinkingBudget: 0` for Gemini, `veniceParameters.disableThinking` for Venice). This prevents small models from spending their entire context budget on internal reasoning.

**CodingAgent:** `thinking_utils.py` has `budget_max_tokens()` for reasoning models, but no mechanism to _suppress_ thinking for local small models.

**Recommendation:** Add `disable_thinking: bool` to the provider config and pass it through the adapter layer. For LM Studio / Ollama providers, default to `True` for `model_tier ∈ {NANO, SMALL}`.

**Files to change:** `src/config/providers.json`, `src/core/inference/adapters/ollama_adapter.py`, `src/core/inference/adapters/lm_studio_adapter.py`, `src/core/inference/thinking_utils.py`.

---

## 4. Gap Analysis: Frontier Models (Claude Opus, o1/o3, GPT-4)

### GAP-FRONTIER-1: No model-ID-based prompt routing (equivalent to opencode's system.ts)

**opencode:** `SystemPrompt.provider(model)` routes by model ID string. `claude` → `anthropic.txt`, `o1`/`o3`/`gpt-4` → `beast.txt`, etc.

**CodingAgent:** Provider-level partials (`anthropic.txt`, `openai.txt`) exist in `ContextBuilder` but these are provider-level, not model-level. A `claude-haiku` and `claude-opus` both get `anthropic.txt`.

**Recommendation:** Add `SystemPrompt.select(model_id: str) -> str` in `context_builder.py`:

```python
_MODEL_PROMPT_MAP = [
    (r"claude-opus|claude-3-7|claude-sonnet-4", "anthropic-frontier"),
    (r"claude-haiku|claude-3-5-haiku",           "anthropic-small"),
    (r"o1|o3|o4",                                 "openai-reasoning"),
    (r"gpt-4",                                    "openai-frontier"),
    (r"gemini-2\.5|gemini-pro",                   "gemini-frontier"),
    (r"gemini-flash|gemini-nano",                 "gemini-small"),
]
```

Partials stored in `src/config/agent-brain/partials/` (already referenced in `ContextBuilder`).

---

### GAP-FRONTIER-2: No beast-mode equivalent for reasoning models

**opencode:** `beast.txt` (for o1/o3/gpt-4) mandates a 10-phase workflow: fetch URLs, understand deeply, investigate codebase, research online, plan step-by-step, implement incrementally, debug, test, reflect, validate. It also requires web search before using any third-party library.

**CodingAgent:** No equivalent. Frontier models go through the same 14-node pipeline as SMALL models, with identical per-node prompts. The `strategic` (planning) node has a ≤8 step limit that is often too small for complex frontier tasks.

**Recommendation:** Add `roles/operational-frontier.md` with:
- Mandatory exploration phase before planning (≥3 files read before any edit)
- Explicit reflection gate: "Before each tool call, state in one sentence what you expect it to return"
- No step limit for complex tasks when `model_tier == FRONTIER`
- Instruction to run lint + typecheck after every code change (not just at the end)
- Resume/continue instruction: "If the user says 'continue' or 'resume', check the todo list and proceed from the last unchecked item"

---

### GAP-FRONTIER-3: Parallel explore subagents not used before planning

**opencode:** In planning mode, the plan agent spawns up to 3 parallel `explore` subagents to gather codebase context before writing the plan.

**CodingAgent:** `analyst_delegation_node` can spawn one analyst, but it runs sequentially and only when the complexity classifier fires. Complex frontier tasks could benefit from 2–3 parallel analyst subagents (file structure, symbol graph, test coverage) before planning.

**Recommendation:** In `analyst_delegation_node`, when `model_tier == FRONTIER` and task complexity is `complex`, spawn up to 3 parallel analysts via `asyncio.gather()`:
- Analyst 1: file structure + entry points
- Analyst 2: symbol graph + dependencies
- Analyst 3: test coverage + existing patterns

Merge results into `analysis_result` before handing off to `planning_node`.

**Files to change:** `src/core/orchestration/graph/nodes/analyst_delegation_node.py`.

---

### GAP-FRONTIER-4: No memory file persistence across sessions

**opencode:** `default.txt` / `anthropic.txt` instruct the model to maintain `.github/instructions/memory.instruction.md` — a persistent instruction file the model updates with learned preferences and patterns. This survives across sessions.

**CodingAgent:** VectorStore persists episodic memory, but no writable instruction file that the model itself controls. `inject_prior_session_memories()` injects memories but the model cannot add to them.

**Recommendation:** Add a `memory://self` tool (or extend `manage_todo`) that allows the model to append entries to `~/.coding_agent/memory.md`. `perception_node` reads this file and injects it as a `<memory>` block in the system prompt. This gives frontier models the same self-curating memory loop as opencode.

---

### GAP-FRONTIER-5: No web research capability

**opencode:** `beast.txt` makes web research mandatory. `webfetch` tool used to search Google and read docs before using any library.

**CodingAgent:** No web fetch tool registered. Frontier models cannot verify library versions, read updated API docs, or research error messages.

**Recommendation:** Add `web_search(query: str)` and `web_fetch(url: str)` tools (can wrap existing Python libraries: `httpx` + `beautifulsoup4`). Register in `coding.yaml` toolset for MEDIUM+ tiers only. Add to `operational-frontier.md`: "Before using any third-party library or API, fetch its latest documentation."

---

### GAP-FRONTIER-6: Planning step limit (≤8) too restrictive for complex tasks

**CodingAgent:** `strategic.md` enforces a hard ≤8 step limit. For large refactors or multi-file features, this is insufficient and leads to incomplete plans that then fail during execution.

**opencode:** No step limit. Plans are as long as needed.

**Recommendation:** Make the step limit tier-dependent:
- NANO: ≤4 steps
- SMALL: ≤6 steps  
- MEDIUM: ≤10 steps
- LARGE/FRONTIER: ≤20 steps (or unlimited with delegation)

Update `roles/strategic.md` to inject the limit dynamically via `ContextBuilder`. Already possible since `model_tier` is in `AgentState` and available to `planning_node`.

---

### GAP-FRONTIER-7: Reflection requirement not enforced for reasoning models

**opencode:** `anthropic.txt` says "Your thinking should be thorough and so it's fine if it's very long." Extended thinking is explicitly encouraged.

**CodingAgent:** `thinking_utils.py` passes `budget_tokens` through to the adapter, but no prompt instruction encourages or structures thinking. Models with extended thinking enabled may not use it effectively without a prompt cue.

**Recommendation:** For `model_tier == FRONTIER` and providers that support extended thinking (Anthropic, OpenAI o-series), inject a `<thinking_mode>` block:
```
Before every tool call, think through:
1. What am I trying to accomplish with this call?
2. What could go wrong?
3. What will I do if it fails?
```
This adds ~50 tokens but significantly improves tool call accuracy on complex tasks.

---

## 5. Implementation Priority

### Immediate (small model reliability — 1–2 days)

| ID | Change | Effort |
|---|---|---|
| GAP-SMALL-1 | Collapse output format for NANO/SMALL | Low |
| GAP-SMALL-2 | Add `operational-small.md` (≤60 lines) | Medium |
| GAP-SMALL-5 | Distinguish format vs task errors in loop guard | Low |
| GAP-SMALL-6 | Suppress thinking tokens for local providers | Low |

### Short-term (frontier model quality — 3–5 days)

| ID | Change | Effort |
|---|---|---|
| GAP-FRONTIER-1 | Model-ID prompt routing in `ContextBuilder` | Medium |
| GAP-FRONTIER-2 | Add `operational-frontier.md` | Medium |
| GAP-FRONTIER-6 | Tier-dependent planning step limit | Low |
| GAP-FRONTIER-7 | Inject `<thinking_mode>` block for reasoning models | Low |

### Medium-term (capability gaps — 1–2 weeks)

| ID | Change | Effort |
|---|---|---|
| GAP-SMALL-3 | Extend `is_simple_mode` to SMALL with `provider_supports_parallel_tools` | Medium |
| GAP-SMALL-4 | Add `needs_clarification` guard in `perception_node` | Medium |
| GAP-FRONTIER-3 | Parallel analyst subagents for FRONTIER planning | High |
| GAP-FRONTIER-4 | Self-curating `memory.md` + `memory://self` tool | High |
| GAP-FRONTIER-5 | Add `web_search` / `web_fetch` tools | High |

---

## 6. Prompt File Changes Summary

### New files needed

```
src/config/agent-brain/roles/operational-small.md    # ≤60 lines, stripped format
src/config/agent-brain/roles/operational-frontier.md # exhaustive workflow, reflection
src/config/agent-brain/partials/anthropic-frontier.md
src/config/agent-brain/partials/anthropic-small.md
src/config/agent-brain/partials/openai-reasoning.md  # o1/o3/o4 equivalent of beast.txt
src/config/agent-brain/partials/openai-frontier.md
src/config/agent-brain/partials/gemini-frontier.md
src/config/agent-brain/partials/gemini-small.md
```

### Modified files

```
src/core/context/context_builder.py  — add select_role_prompt(tier, model_id), inject_thinking_mode()
src/core/inference/model_tiers.py    — extend is_simple_mode, add tier_step_limit()
src/core/orchestration/graph/nodes/perception_node.py — add needs_clarification guard
src/core/orchestration/graph/nodes/analyst_delegation_node.py — parallel analysts for FRONTIER
src/core/orchestration/loop_guards.py — format_error vs task_error distinction
src/config/providers.json             — add disable_thinking, provider_supports_parallel_tools
```

---

## 7. Key Takeaways

1. **opencode's biggest structural advantage** is its simplicity: no state machine, no routing logic, no node overhead. The model does all the routing via tool calls. CodingAgent's 14-node pipeline adds latency and cognitive load — for 9B models in particular, the overhead of traversing perception → analysis → planning → execution is costly in both tokens and turns.

2. **Prompt specificity per model ID** is opencode's biggest prompt advantage. A `claude-opus` behaves very differently from a `gpt-4o-mini`; serving them the same system prompt is a significant opportunity cost.

3. **The output format mandate** (`RESULT: / STATUS: / FILES_CHANGED: / OBSERVE:`) is CodingAgent's biggest reliability problem with small models. It should be treated as an optional structured response that the parser accepts when present, not a required schema.

4. **Frontier models need more latitude**, not less. The ≤8 step limit, single analyst subagent, and lack of web research capability artificially constrain what frontier models can accomplish. opencode's beast mode shows the ceiling; CodingAgent's pipeline currently falls well below it.

5. **Local small model adoption** (Ollama, LM Studio) requires aggressive prompt compression, thinking suppression, and one-tool-per-turn semantics. These are table stakes for 9B model reliability and should be the first priority.
