REQUIREMENTS: Build-Your-Own-OpenClaw → CodingAgent mapping

Purpose
- Map each Build-Your-Own-OpenClaw step (00–17) to files and tests in this repository.
- Record current status: Implemented / Partial / Missing.
- Provide concise evidence and a prioritized backlog of follow-up tasks (P0..P3) with rough estimates.

Notes on formatting
- Each step is a short checklist: Description, Files, Status, Evidence, Follow-ups.
- Follow-ups are written as single-line items (no nested bullets) to keep the document easy to scan.

Step 00 - Chat loop / Inference turn processing
Description: The agent chat loop that constructs prompts, calls the LLM, streams tokens, and handles turn lifecycle.
Files: src/core/inference/inference_loop.py; src/core/inference/llm_manager.py; src/core/orchestration/orchestrator.py; src/main.py
Status: Implemented
Evidence: tests/integration/test_e2e_pipeline_smoke.py; tests/unit/test_orchestrator_system_prompt_auto.py; inference_loop contains turn orchestration and MessageManager integration.
Follow-up 1: Add end-to-end tests that exercise TUI + server flows (Priority: P1, Est: 1-2 days)
Follow-up 2: Verify and document the model-token budgeting heuristics used by ContextBuilder/MessageManager (Priority: P2, Est: 4-8 hours)

Step 01 - Tools and Tool Registry
Description: Tools as callable actions exposed to the model; registry and toolset loading (including format-aware selection).
Files: src/tools/_registry.py; src/tools/_tool.py; src/core/orchestration/registry_builder.py; src/tools/tools_config.py; src/config/toolsets/loader.py
Status: Implemented (with recent model-aware toolset loader improvements)
Evidence: tests/unit/test_delegate_task_schema_enum.py; tests/unit/test_toolset_coverage.py; tests/unit/test_toolset_format_selection.py; ToolDefinition.to_openai_schema injects runtime enums.
Follow-up 1: Decide and implement a canonical policy for model-aware toolset loading across both toolsets loader modules (Priority: P0, Est: 1-2 days).
Follow-up 2: Add integration tests to assert cache consistency across load_toolset and load_toolset_for_model under _DIR monkeypatch (Priority: P0, Est: 4-8 hours).

Step 02 - Skills (skill loader & exposure)
Description: Skill discovery and loading (skills are higher-level tool bundles / templates).
Files: src/tools/skill_tools.py; src/tools/_registry.py; src/core/orchestration/registry_builder.py
Status: Partial
Evidence: tests/unit/test_opencode_improvements_pt4.py; tests/unit/test_opencode_improvements_pt5.py; skill_tools implements list_skills/load_skill with a pluggable _SKILLS_DIR.
Follow-up 1: Harmonize skill model / system-prompt semantics (document current approach and add tests) (Priority: P1, Est: 1 day).
Follow-up 2: Add dynamic enum injection for any tool parameter referencing skill names (Priority: P2, Est: 4-8 hours).

Step 03 - Persistence (session store, long-term storage)
Description: Persist conversation snapshots, tool calls, decisions, and session metadata.
Files: src/core/memory/session_store.py; src/core/memory/jsonl_session_store.py; src/core/orchestration/orchestrator_bootstrap.py
Status: Implemented
Evidence: tests/unit/test_session_manager.py; tests/unit/test_claw_parity.py; orchestrator_bootstrap wires a SessionStore/JsonlSessionStore based on config.
Follow-up 1: Add integration test covering concurrent access to the chosen backend (SQLite vs JSONL) (Priority: P2, Est: 1 day).

Step 04 - CLI / TUI and slash commands
Description: Local TUI front-end, AgentBridge, and support for local slash commands and UI interactions.
Files: tui/src/ui/core_bridge.py; tui/src/ui/app.py; src/main.py (AgentBridge wiring)
Status: Implemented
Evidence: tests/unit/test_tui_fixes.py; tests/unit/test_tui_threading.py; AgentBridge provides EventBus bridging and history management.
Follow-up 1: Add smoke test for AgentBridge wired to the real orchestrator (Priority: P2, Est: 4-8 hours).

Step 05 - Compaction (context/message compaction)
Description: Automatic compaction of history to fit token budgets and preserve salient context.
Files: src/core/orchestration/message_manager.py; src/core/memory/auto_compactor.py; src/core/orchestration/orchestrator_helpers.py
Status: Implemented
Evidence: tests/unit/test_message_manager.py; tests/unit/test_auto_compactor.py; perception_node calls the compactor helper.
Follow-up 1: Add coverage for compactor failure modes and retry/backoff behaviors (Priority: P2, Est: 4-8 hours).

Step 06 - Web tools & HTTP server
Description: HTTP APIs, SSE adapter for pushing events to UI clients, and server wiring.
Files: src/server/app.py (ServerEventBusAdapter and run_server integration); src/core/orchestration/orchestrator_bootstrap.py
Status: Implemented
Evidence: tests/unit/test_server_app.py; orchestrator_bootstrap registers ServerEventBusAdapter when the server package is available.
Follow-up 1: Expand API docs and add integration tests that exercise SSE event streaming under load (Priority: P2, Est: 1-2 days).

Step 07 - Event-driven architecture (EventBus)
Description: In-process EventBus for telemetry, UI, and internal signaling; optional persistent EventBus for crash recovery.
Files: src/core/orchestration/event_bus.py; src/core/orchestration/event_persistence.py; orchestrator_bootstrap.py (wires topics)
Status: Implemented
Evidence: tests/unit/test_eventbus_thread_pool_graph_singleton.py; tests/unit/test_subagent_dispatch_event.py; many modules publish/subscribe to EventBus topics.
Follow-up 1: Document key EventBus topics and payload schemas for external clients (Priority: P2, Est: 6-8 hours).

Step 08 - Config hot reload
Description: Hot reloading of config/toolsets/providers and hooks to reconfigure running orchestrator.
Files: src/core/orchestration/orchestrator_bootstrap.py (register_config_reload_handlers); src/core/config_loader.py
Status: Implemented
Evidence: tests/unit/test_orchestrator_config_reload_hooks.py
Follow-up 1: Add tests that exercise complex reload scenarios (provider removal, toolset changes) (Priority: P2, Est: 1-2 days).

Step 09 - Channels & SSE (subscribe/publish channels)
Description: Named channels and client subscriptions (SSE) that allow clients to subscribe to subsets of EventBus topics.
Files: src/server/app.py (ServerEventBusAdapter); orchestrator_bootstrap registers SSE adapter
Status: Partial
Evidence: tests/unit/test_sse_adapter_stream.py; ServerEventBusAdapter adapts EventBus to SSE but channel subscription features need clarification.
Follow-up 1: Add channel subscription tests, and document the channel naming and filtering semantics (Priority: P1, Est: 8-12 hours).

Step 10 - WebSocket transport and sessions
Description: WebSocket session management for streaming assistant responses and bidirectional control channels.
Files: src/server/app.py (server wiring); codebase uses FastAPI in the environment but explicit websocket endpoints are limited.
Status: Partial / Missing
Evidence: project contains FastAPI dependencies and docs mention websocket transports, but there is no fully-featured websocket endpoint implementation wired to orchestrator in src/server/app.py.
Follow-up 1: Implement a WebSocket session endpoint that authenticates, binds to an orchestrator instance, and streams tokens via EventBus (Priority: P1, Est: 2-4 days).

Step 11 - Multi-agent routing & subagents (delegate_task)
Description: Ability to spawn isolated subagents with controlled permissions and depth-limits (delegate_task primitive).
Files: src/tools/subagent_tools.py (delegate_task, delegate_task_async); src/core/orchestration/graph/nodes/delegation_node.py; src/core/orchestration/task_lifecycle.py (get_tools_for_role_impl)
Status: Implemented
Evidence: tests/unit/test_subagent_tools.py; tests/unit/test_subagent_dispatch_event.py; many graph nodes call delegate_task_async; ToolDefinition enum injection covers roles.
Follow-up 1: Audit and add tests enforcing CP-1 (prevent unbounded subagent recursion) and spawn-depth semantics (Priority: P0, Est: 1 day).

Step 12 - Scheduled tasks / Cron & heartbeat
Description: Periodic cron-like tasks and heartbeat checks to trigger maintenance jobs (compaction, distillation) or scheduled skills.
Files: (none obvious: grep uncovered only crontab references in tools/bash_security.py)
Status: Missing
Evidence: No scheduler/cron worker module found; only crontab appears in a security matcher.
Follow-up 1: Design and implement a scheduler/heartbeat worker that posts events to EventBus (Priority: P1, Est: 2-3 days).

Step 13 - Multi-layer prompts & SystemPromptBuilder
Description: Two-part system prompt assembly (static prefix + dynamic contextual prefix) and model-adaptive prompt composition.
Files: src/core/prompts/system_prompt_builder.py; src/core/context/context_builder.py
Status: Implemented
Evidence: tests/unit/test_system_prompt_builder.py; ContextBuilder uses model-aware partial selection and caches.
Follow-up 1: Add tests for provider-specific prompt partial selection and ensure the model-tier heuristics interact correctly (Priority: P2, Est: 1-2 days).

Step 14 - Post-message return contracts (tool output handling)
Description: Clear contract for how tool outputs are returned to the conversation (roles, content types, attachments) and how they are persisted/published.
Files: src/core/orchestration/tool_execution_pipeline.py; src/core/orchestration/message_manager.py; src/core/orchestration/preview_coordinator.py
Status: Partial
Evidence: MessageManager and tool_execution_pipeline persist tool calls and results; execution_node returns tool outputs as messages but ContextBuilder filters non-user roles (see execution_node comments).
Follow-up 1: Standardize tool output role (user vs assistant) and add tests that validate ContextBuilder sees tool outputs as intended (Priority: P1, Est: 1 day).

Step 15 - Agent dispatch policies & permission gating
Description: Permission policies for tool usage, gating of dangerous actions, and interactive approval flows.
Files: src/core/orchestration/permission_policy.py; src/core/orchestration/permission_gateway.py; src/core/orchestration/approval_gate.py
Status: Implemented
Evidence: tests/unit/test_task_list_sprint2.py and others check permission levels and publish permission-required events.
Follow-up 1: Add integration tests for real approval flows via EventBus + UI (Priority: P2, Est: 1-2 days).

Step 16 - Concurrency control & worker pools
Description: Executor pools, bounded thread executors for delegate_task_async, and loop guards to avoid doom-loops.
Files: src/tools/subagent_tools.py; src/core/orchestration/orchestrator_bootstrap.py; src/core/orchestration/loop_guards.py
Status: Implemented
Evidence: tests/unit/test_bash_planning_threading_bug_documentation.py verifies bounded executors and loop guards exist.
Follow-up 1: Add stress tests and ensure executor sizing is configurable (Priority: P2, Est: 1-2 days).

Step 17 - Memory management, distillation & retrieval
Description: Long-term memory distillation, retrieval, and integration with compaction to build summaries.
Files: src/core/memory/distiller.py; src/core/memory/auto_compactor.py; src/core/memory/session_store.py
Status: Partial
Evidence: tests/unit/test_distiller.py; auto_compactor exists; distiller not yet fully wired to periodic jobs or compaction callbacks everywhere.
Follow-up 1: Wire memory distiller into MessageManager compaction and scheduler/cron jobs; add tests for summary quality and eviction heuristics (Priority: P1, Est: 2-3 days).

Top-priority Backlog (summary)
1. Canonical toolset model-aware loading policy: pick A/B/C and implement. Ensure both loaders expose load_toolset_for_model or make orchestrator import src.config.toolsets.loader explicitly (Priority: P0, Est: 1-2 days).
2. Add integration tests proving cache consistency across load_toolset/load_toolset_for_model under monkeypatched _DIRs (Priority: P0, Est: 4-8 hours).
3. Add CP-1 enforcement tests for delegate_task recursion depth and ensure delegation cannot spawn unlimited nested subagents (Priority: P0, Est: 1 day).
4. Implement scheduler/heartbeat worker (Priority: P1, Est: 2-3 days).
5. Implement a WebSocket session endpoint that streams tokens and binds to orchestrator via EventBus (Priority: P1, Est: 2-4 days).
6. Channel subscription tests + documentation for SSE adapter (Priority: P1, Est: 8-12 hours).
7. Provider capability-based model heuristics as an improvement over string-token heuristics (Priority: P2, Est: 1-2 days).

How this document was created
- I scanned repository files and tests for relevant symbols (delegate_task, ContextBuilder, SystemPromptBuilder, MessageManager, EventBus, session_store, auto_compactor, ServerEventBusAdapter, toolset loader, skill_tools, etc.).
- Evidence lines reference unit and integration tests in tests/unit and tests/integration that exercise the features above.

Next recommended action
1. Review this mapping for correctness and update if you want different prioritization.
2. I can (pick one): 1) implement the canonical toolset loading policy + tests (P0 work), or 2) implement the scheduler/heartbeat worker, or 3) implement the WebSocket endpoint. Tell me which to start and I will proceed.

Expanded per-step details

For each step below: Purpose; Key functions / classes (file: symbol); Representative tests; Current gaps; PR-sized follow-ups (single-line with files and rough estimate).

Step 00 — Chat loop / Inference turn processing
- Purpose: Build prompt, call LLM, stream tokens, manage turn lifecycle.
- Key functions/classes: src/core/inference/inference_loop.py: run_agent_once / _run_turn; src/core/inference/llm_manager.py: LLMManager/adapter logic; src/core/orchestration/orchestrator.py: run loop wiring; src/core/orchestration/message_manager.py: MessageManager/compaction integration.
- Representative tests: tests/integration/test_e2e_pipeline_smoke.py; tests/unit/test_orchestrator_system_prompt_auto.py
- Gaps: Missing multi-transport (TUI + server/ws) end-to-end smoke tests exercising token streaming.
- PR follow-ups: Add tests/integration/test_tui_orch_smoke.py (Est: 8h).

Step 01 — Tools and Tool Registry
- Purpose: Tools exposed to models, toolsets loader and format-aware selection.
- Key functions/classes: src/tools/_tool.py: ToolDefinition.to_openai_schema (dynamic enums); src/tools/_registry.py: build_registry(); src/config/toolsets/loader.py: load_toolset_for_model(), _is_small_model(), _format_cache; src/tools/toolsets/loader.py (legacy shim)
- Representative tests: tests/unit/test_delegate_task_schema_enum.py; tests/unit/test_toolset_format_selection.py; tests/unit/test_orchestrator_model_aware_toolset.py
- Gaps: Legacy loader lacked model-aware function and could cause inconsistent behavior depending on import path.
- PR follow-ups: Implement wrapper in src/tools/toolsets/loader.py delegating to src.config.toolsets.loader and add tests/unit/test_legacy_loader_forwarding.py + tests/unit/test_toolset_cache_consistency.py (Est: 6-12h).

Step 02 — Skills
- Purpose: Discover and load skill bundles; expose skill names safely to model schemas.
- Key functions/classes: src/tools/skill_tools.py: list_skills(), load_skill(); src/tools/_tool.py: enum injection for skill parameters.
- Representative tests: tests/unit/test_opencode_improvements_pt4.py; tests/unit/test_opencode_improvements_pt5.py
- Gaps: Missing uniform enum injection for all skill-name parameters; mismatch between skills-as-system-prompts and skills-as-tools patterns.
- PR follow-ups: docs + ToolDefinition enhancements + tests (Est: 6h).

Step 03 — Persistence (session_store)
- Purpose: Persist messages, tool calls, decisions, and session metadata.
- Key functions/classes: src/core/memory/session_store.py; src/core/memory/jsonl_session_store.py; src/core/orchestration/session_manager.py
- Representative tests: tests/unit/test_session_manager.py; tests/unit/test_claw_parity.py
- Gaps: Concurrency stress tests for JSONL vs SQLite backends missing.
- PR follow-ups: tests/unit/test_session_store_concurrency.py (Est: 1 day).

Step 04 — CLI / TUI and AgentBridge
- Purpose: Local UI + bridging to EventBus.
- Key functions/classes: tui/src/ui/core_bridge.py: AgentBridge; tui/src/ui/app.py; src/main.py (AgentBridge wiring)
- Representative tests: tests/unit/test_tui_fixes.py; tests/unit/test_tui_threading.py
- Gaps: End-to-end AgentBridge → real orchestrator smoke tests.
- PR follow-ups: tests/integration/test_tui_orch_smoke.py (Est: 1 day).

Step 05 — Compaction / auto-compactor
- Purpose: Condense conversation history to fit token budgets preserving salient context.
- Key functions/classes: src/core/orchestration/message_manager.py; src/core/memory/auto_compactor.py; src/core/orchestration/orchestrator_helpers.py
- Representative tests: tests/unit/test_message_manager.py; tests/unit/test_auto_compactor.py
- Gaps: Failure and retry semantics for compaction and durable hooks for distillation.
- PR follow-ups: Add robust error-handling and tests (Est: 6-12h).

Step 06 — Web tools & HTTP server / SSE adapter
- Purpose: SSE adapter for browsers/clients and server wiring.
- Key functions/classes: src/server/app.py: ServerEventBusAdapter; src/core/orchestration/orchestrator_bootstrap.py
- Representative tests: tests/unit/test_server_app.py; tests/unit/test_sse_adapter_stream.py
- Gaps: Channel filtering semantics and docs.
- PR follow-ups: docs + channel tests (Est: 6-8h).

Step 07 — EventBus
- Purpose: In-process pub/sub for telemetry, UI, and internal signaling.
- Key functions/classes: src/core/orchestration/event_bus.py; src/core/orchestration/event_persistence.py
- Representative tests: tests/unit/test_eventbus_thread_pool_graph_singleton.py
- Gaps: Public topic/payload schemas not documented.
- PR follow-ups: docs + tests (Est: 1-2 days).

Step 08 — Config hot reload
- Purpose: Hot reload for configs/toolsets/providers and runtime handlers.
- Key functions/classes: src/core/orchestration/orchestrator_bootstrap.py (register_config_reload_handlers); src/core/config_loader.py
- Representative tests: tests/unit/test_orchestrator_config_reload_hooks.py
- Gaps: Complex reload scenario tests missing.
- PR follow-ups: Add tests for provider removal and toolset changes (Est: 1-2 days).

Step 09 — Channels & SSE subscription tests
- Purpose: Channel-level subscribe/unsubscribe; SSE streams filtering.
- Key functions/classes: src/server/app.py: ServerEventBusAdapter
- Representative tests: tests/unit/test_sse_adapter_stream.py
- Gaps: Channel-level tests and docs.
- PR follow-ups: tests/unit/test_sse_channels.py (Est: 8h).

Step 10 — WebSocket sessions
- Purpose: WebSocket transport binding to orchestrator for token streaming and control.
- Key functions/classes: src/server/app.py; planned src/server/websocket.py
- Representative tests: none
- Gaps: No websocket endpoint that binds sessions to orchestrator and streams model.token events.
- PR follow-ups: Implement websocket session endpoint + tests (Est: 2-4 days).

Step 11 — Subagents & delegate_task protection (CP-1)
- Purpose: Spawn isolated subagents with limited depth and restricted tool permissions.
- Key functions/classes: src/tools/subagent_tools.py: delegate_task / delegate_task_async; src/core/orchestration/graph/nodes/delegation_node.py
- Representative tests: tests/unit/test_subagent_tools.py; tests/unit/test_subagent_dispatch_event.py
- Gaps: Explicit unit tests asserting depth-limit enforcement and structural removal of delegate tools in child agents.
- PR follow-ups: tests/unit/test_delegate_task_recursion_limit.py (Est: 6-12h).

Step 12 — Scheduler / Cron / Heartbeat
- Purpose: Periodic tasks (distill, compaction, health checks) driven by scheduler and posted to EventBus.
- Key functions/classes: None yet; implement src/core/scheduler/worker.py
- Representative tests: none
- Gaps: Missing scheduler and wiring to distiller/compactor.
- PR follow-ups: Add scheduler worker + tests (Est: 2-3 days).

Step 13 — SystemPrompt + ContextBuilder
- Purpose: Two-part system prompt assembly + model-adaptive prompt partials.
- Key functions/classes: src/core/prompts/system_prompt_builder.py; src/core/context/context_builder.py
- Representative tests: tests/unit/test_system_prompt_builder.py; tests/unit/test_context_builder.py
- Gaps: Provider-specific prompt-partial tests.
- PR follow-ups: tests for provider capability based partial selection (Est: 6-8h).

Step 14 — Tool output contracts
- Purpose: Standardize how tool outputs are inserted as messages and persisted.
- Key functions/classes: src/core/orchestration/tool_execution_pipeline.py; src/core/orchestration/message_manager.py
- Representative tests: tests/unit/test_tool_safety_node_caching_plan_contracts.py
- Gaps: Clarify and test the role used for tool outputs vs ContextBuilder filtering.
- PR follow-ups: Add standardisation and tests (Est: 1 day).

Step 15 — Permission gating & approval flows
- Purpose: Permission policy enforcements and interactive approval gates.
- Key functions/classes: src/core/orchestration/permission_policy.py; src/core/orchestration/permission_gateway.py; src/core/orchestration/approval_gate.py
- Representative tests: tests/unit/test_task_list_sprint2.py
- Gaps: UI + EventBus approval flow integration tests missing.
- PR follow-ups: tests for approval flow via AgentBridge (Est: 1-2 days).

Step 16 — Concurrency controls & loop guards
- Purpose: Executor pools (bounded), loop guards to avoid doom-loops.
- Key functions/classes: src/tools/subagent_tools.py; src/core/orchestration/loop_guards.py
- Representative tests: tests/unit/test_bash_planning_threading_bug_documentation.py
- Gaps: Stress tests for executor sizing.
- PR follow-ups: Add stress tests (Est: 1-2 days).

Step 17 — Memory distillation & retrieval
- Purpose: Distillation, long-term memory storage & summary retrieval.
- Key functions/classes: src/core/memory/distiller.py; src/core/memory/auto_compactor.py; src/core/memory/session_store.py
- Representative tests: tests/unit/test_distiller.py
- Gaps: Scheduler-invoked distillation hooks.
- PR follow-ups: Wire distiller into scheduler & tests (Est: 2-3 days).

Execution roadmap (first-phase)
1. Implement legacy loader delegation to src.config.toolsets.loader (Option A) and tests.
2. Add delegate_task recursion-depth test to enforce CP-1.
3. Add toolset cache-consistency tests that monkeypatch _DIR and verify deterministic selection.
4. Proceed with scheduler, websocket, SSE-channel tests in sequence.
