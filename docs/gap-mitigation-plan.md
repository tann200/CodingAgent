# Gap Mitigation Plan: CodingAgent vs OpenCode
**Date:** April 2026
**Status:** Actionable — ordered by impact/effort ratio

**Hardware target:** 16GB VRAM (single consumer GPU — RTX 4080/4090, RX 7900 XTX, etc.)
**Primary local model:** Gemma 4 E4B (`gemma-4-e4b-it`) via LM Studio
**Upgrade path:** Gemma 4 31B q4 (~15.5GB) or Gemma 4 26B MoE q4 (~13GB) on the same 16GB GPU

---

## 0. The LangGraph Question

Before addressing specific gaps, the architecture question must be settled: **is LangGraph the right foundation in 2026?**

### What the evidence says

**Against continued investment in LangGraph:**

- **Anthropic's official guidance** ("Building Effective Agents," Dec 2024): *"Consistently, the most successful implementations weren't using complex frameworks or specialized libraries... We suggest that developers start by using LLM APIs directly... Frameworks create extra layers of abstraction that can obscure the underlying prompts and responses, making them harder to debug."* LangGraph is not mentioned anywhere in the document. Source: https://www.anthropic.com/engineering/building-effective-agents

- **mini-SWE-agent** (Princeton/Stanford, Jul 2025): A ~100-line pure ReAct loop using bash only achieved **>74% on SWE-bench Verified** with a frontier cloud model — near-SOTA — with zero graph abstractions, zero tools beyond bash. The team's explicit conclusion: complex scaffolding may actually *overfit* agents to specific eval setups rather than improving general capability. Source: https://github.com/SWE-agent/mini-swe-agent

- **LlamaIndex (official docs)**: *"DAGs did not feel natural to developers trying to develop complex, looping, branching AI applications. Logic like loops and branches needed to be encoded into the edges of graphs, which made them hard to read and understand."* Source: https://developers.llamaindex.ai/python/llamaagents/workflows/

- **OpenAI** deprecated the Assistants API (graph/thread model) in mid-2026 after developer feedback that it was "too complex." Replacement (Responses API + Agents SDK) uses a simple loop-based agent definition with handoffs, no graph topology. Source: https://openai.com/index/new-tools-for-building-agents/

**For keeping LangGraph (or a graph-like structure):**

- CodingAgent's value proposition *is* the explicit, auditable control flow. Planning → validation → execution → verification → debug is a differentiated loop that a raw ReAct agent does not have.
- LangGraph is used in production at Klarna, Uber, J.P. Morgan for workflows with complex branching, durable state, and human-in-the-loop requirements — which matches CodingAgent's profile.
- A full rewrite would take months with high regression risk and zero user-visible benefit during the transition.

### Verdict

**Do not rewrite. Do simplify.**

The 14-node graph has significant accidental complexity — some nodes (delegation, analyst_delegation, wave_coordinator) exist to paper over weaknesses in the main loop rather than solving problems that require a graph node. The correct approach is:

1. **Reduce node count** by collapsing nodes that do not have distinct routing semantics.
2. **Adopt Pydantic AI's type-safe tool/agent decorator pattern** as a layer *over* the existing graph, removing the need for raw dict state and `Any` typed fields.
3. **Keep the graph for what it's genuinely good at**: durable human-in-the-loop (wait_for_user), multi-step plan enforcement, and explicit debug/replan cycles.
4. **Treat the graph as an implementation detail**, not the public API surface. If a simpler async loop (Pydantic AI, raw asyncio) ever becomes clearly superior, the nodes can be migrated incrementally.

The current 14-node graph with ~60 state fields and 15+ router functions is objectively over-complex. Reducing it to 7–8 nodes (see Gap 6 below) would remove most of the maintenance burden without giving up the explicit-control-flow advantage.

---

## 0a. Hardware Target and Model Family

### Gemma 4 (April 2026)

Gemma 4 is a distinct new model family from Google DeepMind (released April 2, 2026), not a continuation of Gemma 3. Source: https://blog.google/innovation-and-ai/technology/developers-tools/gemma-4/

**Sizes and 16GB VRAM fit:**

| Model | Type | Params (active) | Context | VRAM (q4) | 16GB fit? | Tier |
|---|---|---|---|---|---|---|
| gemma-4-e2b-it | Edge (mobile) | 2B eff. | 128K | ~1GB | Yes | SMALL |
| gemma-4-e4b-it | Edge (mobile) | 4B eff. | 128K | ~2.5GB | Yes | SMALL |
| gemma-4-26b-a4b-it | MoE (3.8B active) | 26B total | 256K | ~13GB | Yes | FRONTIER |
| gemma-4-31b-it | Dense | 31B | 256K | ~15.5GB | Tight | FRONTIER |

**Arena.ai rankings (April 2026):**
- gemma-4-31b: #3 open model in the world
- gemma-4-26b-moe: #6 open model in the world
- Both outcompete models 20× their parameter count

**Capabilities relevant to CodingAgent:**
- Native function-calling and structured JSON output (all sizes including E4B)
- 128K context window on E4B (the LM Studio slot limit of 7168 is a *configuration* issue, not a model limit — increase `n_ctx` in LM Studio settings)
- Agentic workflow support and multi-step planning explicitly listed as design goals
- Code generation explicitly optimized for offline local-first use
- Apache 2.0 license — commercially permissive

**Key implication:** The primary constraint when running `gemma-4-e4b-it` is the LM Studio context window configuration, not model capability. Setting `n_ctx` to at least 32K (or 128K if VRAM allows) immediately unlocks dramatically better agentic performance. The current 7168-token session context is severely hampering the agent.

### The Upgrade Path on 16GB VRAM

```
Now:    gemma-4-e4b-it (7168 ctx configured)  → SMALL tier, 20 tools
Better: gemma-4-e4b-it (32K ctx configured)   → SMALL tier, 20 tools, full pipeline
Best:   gemma-4-26b-a4b-it (q4, ~13GB)        → FRONTIER tier, 60 tools, full pipeline
Also:   gemma-4-31b-it (q4, ~15.5GB)          → FRONTIER tier, 60 tools (tight VRAM fit)
```

**Immediate action (zero code changes):** Open LM Studio → model settings → set `n_ctx` to 32768 for `gemma-4-e4b-it`. This is the single highest-leverage change available today.

---

## 0b. Dual-Mode Architecture

CodingAgent must support two distinct operating modes without a code fork:

### Mode 1: Local Small Model (Gemma 4 E4B / any SMALL-tier model)
- **Tier:** SMALL
- **Context window:** 32K–128K (depends on LM Studio config)
- **Tool limit:** 20 tools (enforced by `_prune_tools` in `context_builder.py`)
- **Output format:** YAML tool blocks (not JSON native tools — Gemma 4 E4B supports function calling, but YAML is more reliable for local models with limited context)
- **Graph behavior:** Full pipeline runs. SMALL tier does NOT use simple_mode (single tool per message). The `is_simple_mode()` check only triggers for NANO tier.
- **Compaction threshold:** 85% of context window (already implemented in `perception_node.py`)
- **System prompt:** `operational-small` role variant (stripped, ≤60 lines)

### Mode 2: Frontier Large Model (gemma-4-31b, gemma-4-26b-moe, Claude, GPT-4o etc.)
- **Tier:** FRONTIER
- **Context window:** 128K–256K
- **Tool limit:** 60 tools
- **Output format:** Native JSON function calling where provider supports it
- **Graph behavior:** Full 14-node pipeline, parallel tool calls, extended planning steps (20 vs 4)
- **System prompt:** `operational-frontier` role variant (exhaustive, reflection gate)
- **Additional:** Thinking gate (`<thinking_mode>` block injected for LARGE/FRONTIER tiers)

### Mode Detection

Mode is detected automatically — no user configuration needed. The `classify_model()` function in `model_tiers.py` classifies the active model on each `perception_node` call and writes `model_tier` into `AgentState`. All subsequent nodes and `ContextBuilder` read from `state["model_tier"]`.

**Classification of Gemma 4 models (as of this plan):**

| Model name | `classify_model()` result | Justification |
|---|---|---|
| `gemma-4-e4b-it` | SMALL | 4B edge model; capable but limited context when misconfigured |
| `gemma-4-e2b-it` | SMALL | 2B edge model; same reasoning |
| `gemma-4-26b-a4b-it` | FRONTIER | #6 open model; 256K context; full agentic support |
| `gemma-4-31b-it` | FRONTIER | #3 open model; fits on 16GB at q4 |

### What Changes Between Modes (Implementation Status)

| Feature | SMALL mode | FRONTIER mode | Status |
|---|---|---|---|
| Tool list | 20 tools (core + top supplementary) | 60 tools | Implemented (`_prune_tools`) |
| Tool description verbosity | First sentence only | Full description | Implemented (`_render_tools_for_tier`) |
| Output format | YAML tool blocks | Native JSON (if provider supports) | Implemented |
| Role prompt | `operational-small` | `operational-frontier` | Implemented (`_select_role_for_tier`) |
| Prompt partial | `local-small.md` | `gemini-frontier.md` (for Gemma 4) | Implemented (this plan adds Gemma 4 patterns) |
| Thinking gate | Off | On | Implemented |
| Plan step limit | 6 steps | 20 steps | Implemented (`get_plan_step_limit`) |
| Clarification guard | On (ambiguous tasks) | Off | Implemented (`GAP-SMALL-4`) |
| Max turns | 50 (default) | 50 (default) | **Not yet tier-aware — see Gap 9** |
| Compaction threshold | 85% context window | 85% context window | Implemented |

---



Gaps are ordered by **impact × effort⁻¹** (highest bang-for-buck first).

---

### Gap 1 — MCP: stdio only, no HTTP/SSE, no OAuth
**Severity:** High. MCP is becoming the standard tool-integration layer. Limited MCP means CodingAgent cannot consume tools from the growing ecosystem of MCP servers.

**Root cause:** `src/core/mcp/mcp_client.py` is a hand-rolled stdio JSON-RPC 2.0 client (356 lines). No transport abstraction exists.

**Target state:** Match OpenCode's MCP posture — all three transports, OAuth, live tool-list refresh.

**Mitigation steps:**

1. **Replace the hand-rolled client with `mcp` Python SDK** (`pip install mcp`). The official Python MCP SDK (`modelcontextprotocol/python-sdk`) supports stdio, SSE, and Streamable HTTP with a single `ClientSession` abstraction. This is a direct swap: the existing `register_tools()` method stays, only the transport layer changes.

2. **Add OAuth support** via the SDK's built-in `oauth` utilities. The TUI already has a GitHub Copilot OAuth device-flow screen (`tui/src/ui/oauth/screen.py`) — reuse this pattern for generic MCP OAuth.

3. **Add live tool-list refresh**: subscribe to the SDK's `tools/list_changed` notification and call `register_tools()` again. Publish to the `EventBus` so the TUI footer chip updates.

4. **Expose MCP server status via EventBus** with a `McpStatusEvent` so the TUI sidebar can show per-server health (connected / needs_auth / failed).

**Effort:** 1–2 weeks. The Python MCP SDK is mature and the existing `mcp_client.py` has a clean interface to swap behind.

**Files:** `src/core/mcp/mcp_client.py`, `tui/src/ui/app.py` (MCP slash commands), `tui/src/ui/core_bridge.py`

---

### Gap 2 — No HTTP server / multi-client architecture
**Severity:** High long-term, Medium short-term. Without an HTTP server, CodingAgent cannot be driven by anything other than the TUI. No SDK clients, no web UI, no CI/CD automation, no remote access.

**Root cause:** Entire design assumes a single in-process TUI via `EventBus`. There is no abstraction between "client" and "agent backend."

**Target state:** An embedded HTTP server (FastAPI, aiohttp, or Starlette) that exposes session, message, and event-stream routes — identical to what the TUI already consumes from `EventBus`, just over HTTP/SSE.

**Mitigation steps:**

1. **Define a thin adapter interface.** The `EventBus` already decouples producers from consumers. Add a `ServerEventBusAdapter` that subscribes to all bus topics and forwards them as SSE to connected HTTP clients.

2. **Add a FastAPI app** (`src/server/app.py`) with routes:
   - `POST /session` — create/fork session
   - `GET /session/{id}/events` — SSE stream of `AgentRunningEvent`, `AgentFinalResponse`, `WorkerError`, `ToolCallEvent`
   - `POST /session/{id}/prompt` — equivalent to `bridge.send_prompt()`
   - `POST /session/{id}/interrupt` — equivalent to `bridge.force_interrupt()`
   - `GET /session/{id}/history` — conversation history
   - `GET /providers` — list configured providers
   - `GET /status` — health check

3. **Make the TUI a first-class HTTP client** (optional, longer term): the TUI can continue to use `EventBus` directly (in-process) while the HTTP server also exists — both consume the same bus. No TUI rewrite needed.

4. **Ship a Python SDK** (`pip install codingagent-client`) wrapping the HTTP API, enabling CI/CD use and scripted automation.

**Effort:** 3–4 weeks for the server + SSE adapter. TUI migration is optional and indefinitely deferrable.

**Files:** New `src/server/` directory. `tui/src/ui/core_bridge.py` gains a `ServerMode` flag.

---

### Gap 3 — No plugin system
**Severity:** Medium. Without hooks, users cannot customize prompt construction, compaction behavior, tool filtering, or model parameters without forking the codebase.

**Root cause:** Everything is wired together via direct imports and function calls. There is no hook registry.

**Target state:** A minimal hook system modeled on OpenCode's `experimental.*` pattern — not a full plugin runtime, but enough to make the top-5 most-requested customizations possible without code changes.

**The 5 hooks that provide 80% of the value (in order):**

| Hook name | Called from | Signature |
|---|---|---|
| `agent.system.transform` | `context_builder.py` | `(system_prompt: str, state: dict) -> str` |
| `agent.session.compacting` | `auto_compactor.py` / `distiller.py` | `(messages: list, state: dict) -> list \| None` |
| `agent.tool.filter` | `execution_node.py` | `(tools: list[Tool], state: dict) -> list[Tool]` |
| `agent.llm.params` | `llm_manager.py` | `(params: dict, state: dict) -> dict` |
| `agent.message.transform` | `perception_node.py` | `(messages: list, state: dict) -> list` |

**Mitigation steps:**

1. **Create `src/core/plugin/hook_registry.py`**: a simple `dict[str, list[Callable]]` with `register(hook_name, fn)` and `call(hook_name, *args, **kwargs) -> Any`.

2. **Add `hooks:` section to workspace config** (`.agent/config.json`): a list of Python module paths to import on startup. Each module registers its hooks in its module-level code.

3. **Insert `hook_registry.call(...)` at the 5 locations above.** Default behavior is unchanged when no hook is registered — the call is a no-op pass-through.

4. **Write one example hook** as documentation: a `hooks/no_search_tools.py` that filters out web search tools on every call.

**Effort:** 1 week. The hook registry itself is trivial (~50 lines). The value is in inserting the call-sites.

**Files:** New `src/core/plugin/hook_registry.py`. Touch: `context_builder.py`, `auto_compactor.py`, `distiller.py`, `execution_node.py`, `llm_manager.py`, `perception_node.py`.

---

### Gap 4 — Provider breadth / adapter maintenance burden
**Severity:** Medium. Each new LLM provider requires a new hand-rolled adapter. OpenCode gets providers for free via Vercel AI SDK.

**Root cause:** Custom ABC + 9 concrete adapters, each requiring individual maintenance for API changes, authentication flows, and model naming.

**Target state:** Reduce the adapter maintenance surface without abandoning low-level control over prompt-cache splitting and thinking-token stripping.

**Mitigation steps (choose one path):**

**Path A — LiteLLM as universal adapter (recommended)**
Replace all adapters except Anthropic and GitHub Copilot with a single `LiteLLMAdapter` that calls `litellm.completion()`. LiteLLM already exists in the codebase (`litellm_adapter.py`) and supports 100+ providers with a unified interface. The existing specific adapters (Groq, OpenRouter, Ollama, LM Studio) are all OpenAI-compatible and could be handled by LiteLLM with a routing prefix. Keep `AnthropicAdapter` for prompt-cache splitting and `GithubCopilotAdapter` for OAuth. This eliminates 6 of 9 adapters.

**Path B — Add `litellm` proxy config** to the `providers.json` schema, letting users point any OpenAI-compatible provider at LiteLLM's local proxy. Zero code changes needed beyond a config schema addition.

**Path A is recommended.** It reduces the adapter count from 9 to 3 with immediate effect and is already partially done (the LiteLLMAdapter exists).

**Effort:** 1 week. The LiteLLMAdapter already exists; it needs the `model_tier` classification and `thinking_utils` integration that the specialised adapters have.

**Files:** `src/core/inference/adapters/litellm_adapter.py`, `src/core/config/providers.json`, `src/core/inference/llm_manager.py`

---

### Gap 5 — Agent configurability (roles vs typed agent definitions)
**Severity:** Low-Medium. CodingAgent's "roles" are TUI-only personas. There is no user-facing API for defining new agent types, adjusting per-agent model or temperature, or specifying per-agent tool subsets.

**Root cause:** Role definitions live in YAML files under `src/core/prompts/roles/`. They control persona/system-prompt only, not model selection, tool subsets, or permission rules.

**Target state:** A typed `AgentDefinition` config object (in `.agent/config.json`) that encapsulates: name, base_role, model_override, temperature, max_turns, tools_include/exclude, permission_rules, max_cost_usd.

**Mitigation steps:**

1. **Define `AgentDefinition` schema** in `src/core/config_loader.py` (Pydantic model). Fields: `name`, `role`, `model`, `temperature`, `max_turns`, `tool_filter`, `permission_rules`, `description`.

2. **Add `agents:` section to `.agent/config.json`** as a list of `AgentDefinition` objects. Merge with the 4 built-in definitions (operational, planning, review, strategic).

3. **Wire `AgentDefinition` into `inference_loop.py`**: read the definition for the active role and override `initial_state` fields (`max_turns`, model selection in `llm_manager`, tool list passed to `execution_node`).

4. **Surface in TUI**: the role-switcher (Ctrl+R) lists user-defined agents alongside built-ins. The sidebar shows the active definition's name and description.

**Effort:** 1–2 weeks.

**Files:** `src/core/config_loader.py`, `src/core/orchestration/inference_loop.py`, `tui/src/ui/app.py`

---

### Gap 6 — LangGraph node complexity (14 nodes, ~60 state fields, 15+ routers)
**Severity:** Medium (maintenance). The graph is the largest source of technical debt and the most common location for regressions.

**Root cause:** Each phase of the original roadmap added new nodes without consolidating old ones. delegation, analyst_delegation, and wave_coordinator exist to handle multi-agent use cases that could be handled by a simpler subagent call pattern.

**Target state:** A 7-node graph with a single unified delegation mechanism.

**Proposed consolidation:**

| Current nodes | Consolidated to |
|---|---|
| `perception` | `perception` (unchanged) |
| `analysis` | merged into `perception` (analysis runs in same LLM call when task_complexity == "complex") |
| `planning` + `plan_validator` | `planning` (validator becomes an inline check, not a node) |
| `execution` + `step_controller` | `execution` (step control is a loop within the node, not a node hop) |
| `verification` + `evaluation` | `evaluation` (verification is an inline check before LLM eval) |
| `debug` + `replan` | `recovery` (one node that decides: fix-in-place or replan) |
| `memory_sync` | `memory_sync` (unchanged) |
| `delegation` + `analyst_delegation` + `wave_coordinator` | `dispatch` (one generic subagent dispatch node) |
| `wait_for_user` | `wait_for_user` (unchanged — required for plan mode) |

Result: **7 nodes** (perception, planning, execution, evaluation, recovery, memory_sync, wait_for_user + dispatch as optional).

**Migration strategy:**
- Consolidations are not rewrites — they move code from a node function into an inline call within another node
- Each consolidation is a separate PR with its own regression test pass
- Start with `analysis → perception` (lowest risk, analysis already runs in perception on round 0 for many tasks)
- End with `delegation` consolidation (highest risk, most complex)

**State field reduction:**
- Audit the ~60 state fields. ~15 are used by only one node. Move those into local variables within that node and remove them from `AgentState`.
- This reduces the `initial_state` dict in `inference_loop.py` from ~60 entries to ~40.

**Effort:** 6–8 weeks (spread across multiple PRs, one consolidation at a time).

**Files:** `src/core/orchestration/graph/builder.py`, all node files, `src/core/orchestration/graph/state.py`, `inference_loop.py`

---

### Gap 7 — MCP OAuth / permission completeness
Already covered as part of Gap 1. Tracked separately here because the OAuth flow specifically needs to be a reusable component (not just for MCP but also for any future provider requiring device-flow auth).

**Mitigation:** Extract `tui/src/ui/oauth/screen.py` into a generic `src/core/auth/device_flow.py` that any component (MCP, LLM provider, GitHub API) can call. This is a 1-day refactor.

---

### Gap 8 — Observability (no OTel, no structured traces)
**Severity:** Medium. The current observability story is `logger.info()` calls and the `execution_trace` list. There is no OpenTelemetry integration, no structured span tracing, and no way to feed traces to external tools (Grafana, Honeycomb, LangSmith).

**What OpenCode does:** Every session, message, and tool call is an SQLite-backed event with metadata. SSE streams all events to clients in real time.

**What the research recommends:** Every serious agent framework (Pydantic AI → Logfire/OTel, LangGraph → LangSmith, AutoGen → built-in OTel) now treats structured observability as first-class. Anthropic: *"Maintain a 'scratchpad' that tracks agent steps, and provide human operators with a clear UI for checking the agent's work."*

**Mitigation steps:**

1. **Add `opentelemetry-sdk` and `opentelemetry-exporter-otlp`** to dependencies.

2. **Create `src/core/telemetry/tracer.py`**: a singleton `tracer = trace.get_tracer("codingagent")` wrapper with convenience `span_node(node_name)` context manager.

3. **Wrap each graph node** with a span: `with span_node("perception_node"): ...`. Include span attributes: `task_id`, `round`, `model`, `tool_name`.

4. **Wire to any OTLP endpoint** via env var `OTEL_EXPORTER_OTLP_ENDPOINT`. Default: disabled (no-op tracer). Supports Jaeger, Grafana Tempo, Honeycomb, Logfire out of the box.

5. **In the TUI**, add a `/trace` slash command that opens the last session's trace as a simple text timeline (no external dependency needed for basic use).

**Effort:** 1 week.

**Files:** New `src/core/telemetry/tracer.py`. Touch all node files (one-liner each).

---

### Gap 9 — Tier-aware max_turns and context budget configuration
**Severity:** Medium. The current `max_turns=50` default is applied uniformly regardless of model tier. For SMALL models with tight context windows, 50 turns will exhaust the context long before completing a non-trivial task, causing context overflow errors. For FRONTIER models on 256K context, 50 turns is unnecessarily conservative.

**Root cause:** `max_turns` is set once in `inference_loop.py` from `state.get("max_turns") or project_max_turns or 50`. There is no tier-driven default.

**Current tier-aware limits in `model_tiers.py`:**
- `get_plan_step_limit()` returns tier-appropriate plan step counts (4/6/10/16/20).
- `get_tool_limit()` returns tier-appropriate tool counts (8/20/35/50/60).
- But `max_turns` in `inference_loop.py` ignores the tier entirely.

**Target state:** Add `get_max_turns(tier: ModelTier) -> int` to `model_tiers.py` and use it as the default in `inference_loop.py`.

**Proposed defaults:**

| Tier | max_turns | Rationale |
|---|---|---|
| NANO | 15 | 7K–16K context; model exhausts quickly |
| SMALL | 25 | 32K–128K context; moderate task length |
| MEDIUM | 40 | 128K context; longer tasks feasible |
| LARGE | 60 | >128K context; complex multi-file tasks |
| FRONTIER | 80 | 256K context; long autonomous runs |

**Note:** These are *defaults*. Project-level `maxTurns` in `.agent/settings.json` and the `--max-turns` CLI flag always take precedence.

**Mitigation steps:**

1. Add `_MAX_TURNS: dict[ModelTier, int]` and `get_max_turns(tier) -> int` to `model_tiers.py`.

2. In `inference_loop.py`, after classifying the model tier (already done in `perception_node`), read the tier from the provider and use `get_max_turns(tier)` as the fallback default.

3. Surface the effective `max_turns` in the TUI status bar so users know the limit before hitting it.

**Effort:** 2 hours. Entirely additive — no behavior change for users who set `max_turns` explicitly.

**Files:** `src/core/inference/model_tiers.py`, `src/core/orchestration/inference_loop.py`, `tui/src/ui/app.py` (optional status display)

---

### Gap 10 — LM Studio context window configuration guide
**Severity:** High (for the primary user). The `gemma-4-e4b-it` model supports 128K context, but LM Studio defaults to 7168 tokens for this model (or whatever was last configured). This is the single largest performance bottleneck for the E4B model — the system prompt + tool list + conversation history overflow within 3–5 turns.

**Root cause:** LM Studio's context window setting (`n_ctx`) is a per-model user configuration. The model itself supports 128K but LM Studio ships with conservative defaults.

**Impact of current 7168-token limit:**
- System prompt + role + tool list: ~2000–3000 tokens
- Conversation quota: only ~4000–5000 tokens remaining
- After 3–4 tool calls with non-trivial outputs: context overflow, compaction fires, history truncated
- Agent cannot hold multi-file context across more than 2–3 turns

**Recommended LM Studio settings for `gemma-4-e4b-it`:**

| Setting | Value | Notes |
|---|---|---|
| Context Length (`n_ctx`) | 32768 | Safe minimum; leaves headroom for VRAM |
| Context Length (`n_ctx`) | 65536 | Recommended if VRAM allows |
| GPU Layers | max | All layers on GPU for E4B |
| Flash Attention | On | Reduces VRAM usage for long contexts |

**Mitigation steps:**

1. **Document** the recommended settings in `docs/lm-studio-setup.md` (new file, 1 hour).

2. **Detect misconfiguration at runtime**: in `perception_node.py`, if `model_tier == "small"` and `adapter.context_window < 16384`, publish a `task.warning` event via `orchestrator.event_bus` with message `"Context window is very small ({n} tokens) for {model}. Increase n_ctx in LM Studio to at least 32768."` The TUI displays this as a warning banner.

3. **Add a startup check**: in `core_bridge.py`'s `_start_agent()`, after the adapter is initialized, read `adapter.context_window` and log a warning if < 16384 for SMALL/FRONTIER models.

**Effort:** 2 hours (detection code) + 1 hour (documentation).

**Files:** `src/core/orchestration/graph/nodes/perception_node.py`, `tui/src/ui/core_bridge.py`, new `docs/lm-studio-setup.md`

---

## Implementation Roadmap

Sequenced by dependencies and risk:

### Phase 0 — Immediate wins (< 1 day, zero code risk)
| # | Action | Effort | Risk |
|---|---|---|---|
| 10 | Increase LM Studio n_ctx to 32768 for gemma-4-e4b-it | 5 min (config) | Zero |
| 10 | Add runtime context-window warning in perception_node | 1 hour | Low |
| 9 | Add tier-aware max_turns defaults to model_tiers.py | 2 hours | Low |

Phase 0 is pre-code. The LM Studio config change alone is the highest-leverage action in this entire plan.

### Phase 1 — Quick wins (2–4 weeks)
| # | Gap | Effort | Risk |
|---|---|---|---|
| 3 | Plugin hook system | 1 week | Low |
| 4 | LiteLLM universal adapter | 1 week | Low |
| 7 | Generic OAuth device-flow module | 1 day | Low |
| 8 | OTel observability | 1 week | Low |

These are entirely additive. No existing behavior changes. Ship as a single release.

### Phase 2 — MCP and server (4–6 weeks)
| # | Gap | Effort | Risk |
|---|---|---|---|
| 1 | MCP Python SDK + HTTP/SSE + OAuth | 2 weeks | Medium |
| 2 | HTTP server + SSE adapter | 3 weeks | Medium |

MCP before HTTP server because the HTTP server's tool-discovery routes depend on MCP working correctly.

### Phase 3 — Agent system and configurability (3–4 weeks)
| # | Gap | Effort | Risk |
|---|---|---|---|
| 5 | Typed AgentDefinition config | 2 weeks | Low–Medium |
| 6a | Graph consolidation: analysis→perception | 1 week | Medium |
| 6b | Graph consolidation: verification→evaluation | 1 week | Medium |

### Phase 4 — Graph simplification (8–12 weeks, incremental)
| # | Gap | Effort | Risk |
|---|---|---|---|
| 6c | planning + plan_validator → planning | 2 weeks | Medium |
| 6d | execution + step_controller → execution | 2 weeks | High |
| 6e | debug + replan → recovery | 2 weeks | High |
| 6f | delegation consolidation → dispatch | 3 weeks | High |
| 6g | State field reduction (60 → 40) | 1 week | Medium |

Phase 4 is the highest-risk work and should be preceded by a comprehensive integration test suite covering all routing paths. Do not start Phase 4 until the OTel traces from Phase 1 provide visibility into which routing paths are actually exercised in production.

---

## The LangGraph Question — Final Answer

**Keep LangGraph. Simplify the graph.**

The evidence (Anthropic, mini-SWE-agent, LlamaIndex) argues against *complex* graph architectures — not against explicit control flow. The insight is:

> The capability ceiling is set by the model, not the scaffold. But the floor is set by how debuggable, maintainable, and observable the scaffold is.

CodingAgent's 14-node graph has a low floor: hard to debug, hard to extend, high chance of routing regressions. Reducing it to 7 nodes raises the floor without losing the explicit planning/verification loop that is the genuine differentiator.

A raw asyncio loop (mini-SWE-agent style) would score better on a fresh SWE-bench eval — but it would lose the plan-approval UI, the mid-run injection, the snapshot/rollback, the semantic search, and the structured compaction that make this a *tool for human developers* rather than an autonomous evaluation agent.

Those features require state, require routing, and require a graph. They just don't require *this many* nodes.

---

## What to ignore from the gap analysis

- **Switching to Vercel AI SDK / TypeScript**: No. The Python ecosystem for ML (vector stores / Lance-like libraries, SentenceTransformer, AST parsing) has no TS equivalent. The semantic search advantage would be lost.
- **Rewriting the TUI to be an HTTP client**: Deferrable indefinitely. The in-process `EventBus` is not a limitation until there is a use case that requires a second client.
- **Matching OpenCode's provider count via Vercel AI SDK**: Unnecessary. LiteLLM (Path A in Gap 4) provides equivalent breadth with Python.
