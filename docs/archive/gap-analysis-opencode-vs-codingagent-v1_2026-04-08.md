# Gap Analysis: opencode vs CodingAgent

**Date:** 2026-04-08
**Scope:** Prompts, Roles, Skills, Efficiency, Orchestration, Subagent Spawning, Model Tiering

---

## Executive Summary

opencode is a production TypeScript/Bun monorepo with a clean, composable architecture built on Effect + Vercel AI SDK. CodingAgent is a Python/LangGraph system with significantly more architectural complexity, a richer role taxonomy, and model-tier awareness — but it carries accumulated technical debt, a monolithic orchestrator (4132 lines), and gaps in several areas where opencode is ahead. The most critical gaps are: context compaction, per-provider prompt tuning, doom-loop detection, filesystem snapshots, and the absence of a true local-model tier in provider routing (the 9b model exists but is not wired to a degraded-capability path end-to-end).

---

## 1. System Architecture Overview

| Dimension | opencode | CodingAgent |
|---|---|---|
| Language / runtime | TypeScript / Bun | Python / LangGraph |
| Core framework | Effect (DI + FP) + Vercel AI SDK | LangGraph + custom graph nodes |
| Orchestrator size | ~1 900 lines (`session/prompt.ts`) | 4 132 lines (`orchestrator.py`) — monolith |
| Graph nodes | Flat stream processor | 14 typed LangGraph nodes |
| Provider abstraction | Vercel AI SDK adapters (20+ providers) | Custom adapters per provider (6 adapters) |
| TUI | React/Ink (JS) | Textual (Python) |

---

## 2. Prompts

### opencode

- **8 provider-specific system prompts** routed dynamically based on `model.api.id`:
  `anthropic.txt`, `beast.txt` (GPT-4/o-series), `gpt.txt`, `gemini.txt`, `codex.txt`, `trinity.txt`, `kimi.txt`, `default.txt`
- **4 agent-specific prompts** that override provider prompts entirely:
  `explore.txt`, `compaction.txt`, `title.txt`, `summary.txt`
- **1 plan-mode injection** (`plan.txt`) — enforces a strict 5-phase read-only workflow
- **2 runtime reminders**: `build-switch.txt` (plan → build handoff), `max-steps.txt`
- `copilot-gpt-5.txt` — a dedicated variant for GitHub Copilot GPT-5
- Dynamic injections: environment context, skills list, AGENTS.md content

### CodingAgent

- **3 provider-specific templates**: `anthropic.txt`, `openai.txt`, `default.txt`
- **2 local-model variants**: `local-small.md` (≤14B), `local-medium.md` (14–70B)
- **1 plan reminder**: `plan_reminder.txt`, `build_switch.txt`
- **1 reasoning template**: `reasoning.md`
- **Role-level prompt suffixes** defined in `role_config.py` (`system_prompt_suffix` per role)
- **Agent-brain identity**: `SOUL.md`, `LAWS.md` — injected into system prompt
- **Per-role markdown prompts** in `src/config/agent-brain/roles/` (8 roles)

### Gaps

| Gap | Severity | Notes |
|---|---|---|
| No `gemini.txt` / `kimi.txt` / `beast.txt` equivalents | Medium | CodingAgent lacks tailored prompts for Gemini and o-series reasoning models |
| No `compaction` agent prompt | High | CodingAgent has no auto-compaction agent — overflow is not handled |
| No `copilot-gpt-5.txt` variant | Low | GitHub Copilot GPT-5 gets a generic prompt; a tuned variant would improve reliability |
| plan.txt depth | Medium | opencode's plan prompt enforces a rigorous 5-phase workflow; CodingAgent's is a lighter reminder |
| Local-model prompts not wired to provider routing | High | `local-small.md` / `local-medium.md` exist but are not injected based on active tier at runtime — see §7 |

---

## 3. Roles

### opencode

6 built-in agents (roles are agent-level):

| Agent | Mode | Write? | Tools |
|---|---|---|---|
| `build` | primary | Yes | All |
| `plan` | primary | No (except plan files) | All read + plan_exit |
| `general` | subagent | Yes | All except todowrite |
| `explore` | subagent | No | grep, glob, read, bash, webfetch |
| `compaction` | primary (hidden) | No | None |
| `title` | primary (hidden) | No | None |

Custom agents can be defined in `opencode.json` or via markdown files in `agent/` directories.

### CodingAgent

8 roles defined in `role_config.py` + agent-brain markdown files:

| Role | Write? | Notes |
|---|---|---|
| `planner` / `strategic` | No | Task decomposition, planning |
| `coder` / `operational` | Yes | Code implementation |
| `reviewer` | No | Read-only QA and validation |
| `researcher` | No | Codebase exploration |
| `analyst` | No | Pre-planning reconnaissance (mirrors opencode `explore`) |
| `debugger` | Yes | Targeted fixes, no delegation |
| `scout` | No | File discovery specialist |
| `tester` | Partial | Test-only tools |

Each role has: `description`, `system_prompt_suffix`, `allowed_tools`, `denied_tools`, `max_rounds`.

### Gaps

| Gap | Severity | Notes |
|---|---|---|
| No `title` / `summary` auto-generation agents | Low | Session titles and PR summaries are not auto-generated |
| No `plan_exit` tool / plan-to-build handoff | Medium | CodingAgent has no formal mechanism to exit plan mode and hand off to a coder agent |
| Custom agent definition from config/files | Medium | opencode allows user-defined agents via `opencode.json` or markdown files; CodingAgent roles are hardcoded in `role_config.py` |
| No `mode` field (primary / subagent / all) | Low | CodingAgent has no formal mode distinction to control where roles can be used |
| `compaction` role missing | High | No role handles context overflow (see §5) |

---

## 4. Skills

### opencode

- Skills are markdown files with YAML frontmatter (`name`, `description`) + instruction body
- **6 discovery locations**: `~/.claude/skills/`, `~/.agents/skills/`, `.opencode/skill/`, config `skills.paths`, remote `skills.urls`
- **Remote skills**: fetches `index.json` from a URL, downloads individual files — enables team-shared skill libraries
- Skills are listed verbosely in the system prompt on startup
- `skill` tool loads full content into context on demand
- Skills are permission-controlled per-agent

### CodingAgent

- Skills are markdown files in `src/config/agent-brain/skills/` (3 skills: `context_hygiene`, `dry`, `stuck`)
- `load_skill` tool reads the file and returns content
- `list_skills` tool enumerates available skills
- No remote discovery, no external paths, no config-level `skills.paths`
- Skills are **not** listed in the system prompt — the agent must call `list_skills` first

### Gaps

| Gap | Severity | Notes |
|---|---|---|
| Only 3 skills vs opencode's extensible library | High | Need: `code_review`, `write_tests`, `refactor`, `debug_checklist`, `security_review`, `performance_audit` |
| No remote skill discovery | Medium | Can't share skills across teams or load from URL |
| Skills not auto-listed in system prompt | Medium | Agent must discover skills by calling `list_skills` — opencode proactively lists them |
| No config-level `skills.paths` | Low | Can only load skills from a hardcoded path |
| No YAML frontmatter convention | Low | Skills lack structured metadata (name, description, when-to-use tags) |

---

## 5. Orchestration

### opencode

- **`SessionPrompt`** (1 912 lines) — top-level: resolves tools, builds system prompt, manages runners, handles compaction, processes `@agent` subtask syntax
- **`SessionProcessor`** — drives the LLM stream event loop; returns `"compact" | "stop" | "continue"`
- **Doom-loop detection**: same tool + same inputs × 3 consecutive calls → escalates to `ask` permission
- **Auto-compaction**: on token overflow, spawns the `compaction` agent, summarizes the conversation, restarts the context
- **Plan mode**: 5-phase workflow enforced at prompt level + `plan_exit` tool for formal handoff
- **Filesystem snapshots**: every tool call records filesystem state; revert/undo per tool call
- **ACP server**: IDE integration protocol
- **Plugin hooks**: `tool.definition`, `tool.execute.before`, `tool.execute.after`

### CodingAgent

- **LangGraph graph** with 14 typed nodes:
  `perception`, `planning`, `step_controller`, `execution`, `analysis`, `evaluation`, `verification`, `memory_update`, `replan`, `delegation`, `analyst_delegation`, `debug`, `plan_validator`, `wait_for_user`
- Custom `Orchestrator` class (4 132 lines) coordinates graph execution
- Event bus (`event_bus.py`) for pub/sub between components
- Token budget monitor tracks usage per session
- Preview coordinator for file diff previews
- Permission gateway for tool approval
- Loop guards via `loop_guards.py`
- Session cost tracker

### Gaps

| Gap | Severity | Notes |
|---|---|---|
| No auto-compaction | Critical | When context overflows CodingAgent has no mechanism to summarize and continue — it will fail |
| No filesystem snapshots | High | opencode can revert any tool call; CodingAgent has no undo system |
| Monolithic `orchestrator.py` (4 132 lines) | High | Vol19 audit already flagged this; `SessionManager`, `ScopeGuard`, `OrchestratorCore` need extraction |
| Doom-loop detection is incomplete | Medium | `loop_guards.py` exists but `delegate_task` NoneType crash fires before the depth guard (open Vol19 bug) |
| No plugin/hook system | Medium | No `tool.execute.before/after` hooks; behavior can't be extended without modifying source |
| No ACP/IDE protocol | Low | No programmatic IDE integration layer |
| No `@agent-name` inline subtask syntax | Low | Users can't inline subtask routing in a message |

---

## 6. Subagent Spawning

### opencode

- **`task` tool**: creates a child `Session` with `parentID`; parameters: `subagent_type`, `description`, `prompt`, optional `task_id` (resume prior session)
- Subagents inherit parent model unless the agent has its own model override
- Tool descriptions dynamically enumerate available agents so the LLM always knows what it can spawn
- **Permission propagation**: subagents cannot use `task` or `todowrite` unless explicitly permitted (prevents unbounded recursion)
- Depth not hard-limited by a counter but controlled via permission rules
- **`@agent-name` inline syntax**: user messages can route parts to named agents

### CodingAgent

- **`delegate_task` tool**: spawns an isolated `SubagentOrchestrator` with role-based tool enforcement
- Parameters: `role`, `subtask_description`, `working_dir`, `allowed_tools`
- Depth guard: `_DELEGATION_DEPTH_VAR` (ContextVar) enforces `_MAX_DELEGATION_DEPTH = 3`
- `SubagentOrchestrator` is a minimal shim — real graph execution handled by `GraphFactory`
- 8 valid roles with defined allowed/denied tool lists
- **Known bug**: NoneType crash in `delegate_task` before the depth guard fires (Vol19 SEC-4)
- No `task_id` / session resumption for subagents
- No inline `@role` message syntax

### Gaps

| Gap | Severity | Notes |
|---|---|---|
| No subagent session resumption (`task_id`) | High | opencode allows resuming a prior subagent session; CodingAgent spawns fresh every time |
| NoneType crash before depth guard | High | Vol19 open bug in `subagent_tools.py:486` |
| Dynamic tool description not updated | Medium | `delegate_task` tool description doesn't list available roles dynamically; roles are hardcoded in docstring |
| No `@role` inline message routing | Low | Inline subtask routing requires explicit `delegate_task` call |
| Subagents can't use their own model override | Medium | No per-role model binding; all subagents use the active global model |

---

## 7. Model Tiering (Critical Gap)

This is the largest structural gap. CodingAgent has the concept of tiers in `model_tiers.py` but the end-to-end wiring is incomplete.

### Current State

```
model_tiers.py defines:
  NANO     ≤7B  / ≤4K  context — 8 tools,  simple_mode, YAML format
  SMALL    7-14B / 4-16K        — 20 tools, full pipeline, YAML format
  MEDIUM   14-70B / 16-128K     — 35 tools, full pipeline, YAML format
  LARGE    >70B  / >128K        — 50 tools, parallel, JSON format
  FRONTIER cloud models         — 60 tools, parallel, JSON format

providers.json has:
  lm_studio  (active) : qwen/qwen3.5-9b  → classifies as SMALL
  ollama     (inactive): no models
  github_copilot (inactive): gpt-4o, gpt-4o-mini, claude-3.5-sonnet,
                             claude-3.7-sonnet, claude-sonnet-4.5,
                             claude-sonnet-4.6, o1, o3-mini, gemini-2.0-flash-001

local-small.md / local-medium.md prompt templates exist but are NOT injected based on tier.
```

### Required Tiered System

The requirement is: **local 9b models as minimum, up to GitHub Copilot frontier models**.

| Tier | Models | Tool limit | Format | Prompt template |
|---|---|---|---|---|
| NANO | local ≤7B (phi-3-mini, gemma-2b) | 8 | YAML | local-small.md |
| SMALL | local 7–14B (qwen3.5-9b, mistral-7b) | 20 | YAML | local-small.md |
| MEDIUM | local 14–70B (qwen-14b, mixtral-8x7b) | 35 | YAML | local-medium.md |
| LARGE | local >70B (llama-3.1-70b, qwen-72b) | 50 | JSON | default.txt |
| FRONTIER-MINI | Copilot gpt-4o-mini, gemini-2.0-flash | 50 | JSON | openai.txt / gemini.txt |
| FRONTIER | Copilot claude-sonnet-4.6, gpt-4o, o1, o3-mini | 60 | JSON | anthropic.txt / openai.txt |

### Gaps — Tiering

| Gap | Severity | Recommendation |
|---|---|---|
| `local-small.md` / `local-medium.md` not injected at runtime | Critical | Wire `classify_model()` result into system prompt assembly; inject the right template based on active tier |
| No `gemini.txt` prompt template | High | Add a Gemini-specific prompt (structured output, function-calling format differs) |
| No `o1`/`o3` (reasoning model) prompt variant | High | Reasoning models need a `beast.txt`-style prompt: no CoT instruction, direct output, different tool format |
| GitHub Copilot adapter is inactive by default | High | Provide clear activation path; document OAuth flow; add first-run setup wizard |
| `qwen3.5-9b` classified as SMALL but tool limit is 20 — needs validation | Medium | 9B model with 7600 context is effectively NANO; adjust `classify_model()` to also weight context_window |
| `supports_native_tools()` returns False for NANO/SMALL — but no YAML fallback path is enforced at tool dispatch level | High | Execution node must check `is_simple_mode()` and enforce single-tool-per-response + YAML format |
| No automatic provider failover / tier escalation | Medium | If local model fails or returns garbage, no fallback to a higher-tier provider |
| Tool list is not filtered to `get_tool_limit(tier)` at dispatch time | High | Planning/execution nodes must use `get_tool_limit(classify_model(...))` to cap the tool list sent to the LLM |
| No `small_model` config concept (opencode has `config.small_model`) | Medium | Add a `small_model` field to config for background tasks (title, summary, compaction) |

---

## 8. Efficiency

### opencode

- **Parallel tool execution**: `batch` tool for concurrent tool calls; Vercel AI SDK handles streaming concurrency
- **Context compaction**: auto-triggered on overflow; avoids hard-stop failures
- **Per-SSE chunk timeout**: prevents hanging on slow providers
- **Model catalog caching**: cached to `~/.cache/opencode/models.json` with bundled snapshot fallback
- **`apply_patch` routing for GPT models**: model-specific tool routing based on observed reliability

### CodingAgent

- **Batch tools** (`batch_tools.py`): parallel execution wrapper exists
- **Token budget monitor**: tracks and warns on usage
- **LSP integration** (`lsp_tools.py`): symbol lookup, faster than grep for large codebases
- **Repo intelligence cache**: `_REPO_SUMMARY_CACHE` for analysis results
- **Semantic memory** (`memory_tools.py`): cross-session context retrieval

### Gaps

| Gap | Severity | Recommendation |
|---|---|---|
| No auto-compaction on overflow | Critical | Implement `compaction` agent + trigger on `isOverflow` — this is the most impactful single improvement |
| No per-SSE / per-LLM-call timeout | High | Vol19 Phase 2 item; add `max_llm_wait_seconds` to execution and planning nodes |
| Tool list not capped by tier | High | Sending 60+ tools to a 9B model wastes tokens and degrades output quality |
| No model-specific tool routing | Medium | GPT o-series and local YAML models need different tool dispatch paths |
| `apply_patch` not used for any model | Low | Consider routing GPT models through `apply_patch` as opencode does |

---

## 9. Provider & Model Coverage

### opencode

20+ providers via Vercel AI SDK: Anthropic, OpenAI, Google, Azure, Bedrock, OpenRouter, xAI, Mistral, Groq, DeepInfra, Cerebras, Cohere, TogetherAI, Perplexity, Vercel, GitLab, GitHub Copilot, LiteLLM proxy

### CodingAgent

6 adapters: `anthropic_adapter`, `github_copilot_adapter`, `lm_studio_adapter`, `mock_adapter`, `ollama_adapter`, `openai_compat_adapter`, `openrouter_adapter`

### Gaps

| Gap | Severity | Recommendation |
|---|---|---|
| No Groq adapter (fast inference for local-equivalent latency) | Medium | Add Groq for SMALL/MEDIUM tier — Llama-3.1-70b at local latency |
| No Azure / Bedrock | Low | Enterprise deployment path missing |
| No Google / Gemini adapter | High | Gemini 2.0 Flash is in providers.json under Copilot but has no adapter; add a native Gemini adapter |
| LiteLLM proxy not supported | Medium | LiteLLM unlocks 100+ models via one integration |
| ollama adapter exists but has no models configured | Medium | ollama is the primary local-model runner; needs default model list and auto-discovery |

---

## 10. Prioritized Improvement Roadmap

### Phase 1 — Critical (1–2 weeks)

1. **Wire tier → system prompt**: In `planning_node.py` / `execution_node.py`, call `classify_model(active_model, context_window)` and inject the matching prompt template (`local-small.md`, `local-medium.md`, `default.txt`, `anthropic.txt`, `openai.txt`).

2. **Cap tool list by tier**: Filter the tool list passed to the LLM using `get_tool_limit(tier)`. Priority-rank tools so the most useful survive the cap for small tiers.

3. **Enforce YAML single-tool mode for SMALL/NANO**: In `execution_node.py`, check `is_simple_mode(tier)` and inject a system-reminder that limits the response to one YAML tool block.

4. **Implement context compaction**: Add a `compaction` agent role with a system prompt that summarizes the conversation to essential facts. Wire into the orchestrator's token budget monitor — trigger when remaining budget < threshold.

5. **Fix Vol19 open bugs** (9 test regressions, NoneType crash in `delegate_task`): These are a prerequisite for any new feature work.

### Phase 2 — High (2–4 weeks)

6. **Add gemini.txt prompt template**: Tailored for Gemini's structured output and function-calling format differences.

7. **Add beast.txt / reasoning.txt prompt**: For o1/o3-mini and other reasoning models — no chain-of-thought instruction, direct output, no tool streaming quirks.

8. **Add `small_model` config field**: Use a lightweight model (e.g. `gpt-4o-mini`, `gemini-2.0-flash`) for background tasks: session title generation, summary, compaction.

9. **Per-subagent model binding**: Allow `delegate_task` to accept a `model` parameter, or bind model to role in `role_config.py`. Analyst/researcher roles should default to a small-tier model.

10. **Subagent session resumption (`task_id`)**: Persist subagent session state so a resumed `delegate_task` call continues from where it left off rather than starting fresh.

11. **Expand skills library**: Add at minimum: `code_review.md`, `write_tests.md`, `refactor.md`, `debug_checklist.md`, `security_review.md`. Add YAML frontmatter metadata. Auto-list skills in system prompt.

12. **Decompose `orchestrator.py`**: Extract `SessionManager`, `ScopeGuard`, `OrchestratorCore` as Vol19 Phase 2/3 planned.

### Phase 3 — Medium (1–2 months)

13. **Remote skill discovery**: Support `skills.urls` in config — fetch an `index.json` and download skill files, enabling shared team skill libraries.

14. **Add Groq adapter**: Fast inference path for SMALL/MEDIUM tier models without needing local GPU.

15. **Add native Gemini adapter**: Separate from Copilot; supports streaming, function-calling, and direct API access.

16. **LiteLLM proxy support**: One adapter, 100+ models; enables easy experimentation with new local and cloud models.

17. **Filesystem snapshots**: Record filesystem state before each tool call; add a `revert_last_tool` command.

18. **Plugin/hook system**: `tool.execute.before`, `tool.execute.after` hooks for extending behavior without modifying source.

19. **Dynamic role descriptions in `delegate_task`**: Build the tool description string at runtime from the current `ROLE_CONFIGS` so the LLM always has an accurate, up-to-date list.

20. **`plan_exit` tool + formal plan-to-build handoff**: Replace the current `plan_reminder.txt` soft reminder with a formal `plan_exit` tool that records the plan and switches to the coder role.

---

## 11. Summary Score

| Dimension | opencode | CodingAgent | Winner |
|---|---|---|---|
| Prompt coverage | 8 provider variants + 4 agent variants | 3 provider + 2 local + role suffixes | opencode |
| Role richness | 6 built-in + user-defined | 8 built-in (richer taxonomy) | CodingAgent |
| Skills system | Extensible, remote, auto-listed | 3 skills, local only, manual load | opencode |
| Orchestration cleanliness | ~1900 lines, composable | 4132 lines, monolith | opencode |
| Graph node model | Flat stream processor | 14 typed LangGraph nodes | CodingAgent |
| Context compaction | Yes (auto, dedicated agent) | No | opencode |
| Model tier awareness | Implicit (per-provider prompts) | Explicit (ModelTier enum, classify_model) | CodingAgent |
| Provider coverage | 20+ | 6 | opencode |
| Subagent spawning | task tool + @agent syntax + resume | delegate_task (depth-guarded, role-based) | Tie |
| Doom-loop protection | Built-in (3x same call → ask) | Partial (loop_guards + depth, but buggy) | opencode |
| Filesystem safety | Snapshots + revert per tool call | Preview diff only | opencode |
| Efficiency (local models) | No local model support | Full YAML/NANO path (partially wired) | CodingAgent |
| Test coverage | Not measured | 3022 tests | CodingAgent |
