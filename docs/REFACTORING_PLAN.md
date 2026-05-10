# Refactoring Plan

> Purpose: reduce code complexity, shorten oversized classes and modules, and continue the in-progress refactoring work already visible in the repository.
>
> Status: reconstructed plan based on current codebase state and prior phased orchestrator extractions.
>
> Last Updated: 2026-05-04

## Goals

- Make large modules easier to reason about and test.
- Reduce mixed-responsibility classes and functions.
- Continue existing phased refactors instead of restarting from scratch.
- Preserve behavior and public imports while internals are being split.
- Prefer module-level functions and small focused services over new deep class hierarchies.

## Constraints

These come directly from the project documentation and should guide every refactor:

- `docs/PRINCIPLES.md`: prefer functions over classes unless shared mutable state is required.
- `docs/PRINCIPLES.md`: avoid unnecessary abstraction layers.
- `docs/PRINCIPLES.md`: core logic stays in `src/`; UI remains presentation-only.
- Existing compatibility shims in `orchestrator.py` and tool modules should be preserved while callers migrate.
- Refactors should be test-backed and done in small slices.

## What Already Happened

The repo shows a real phased refactor already underway, mainly around `orchestrator.py`.
The plan file itself does not appear to be present anymore, but the extracted phases are still visible:

- Phase A: `src/core/orchestration/tool_constants.py`
- Phase B: `src/core/orchestration/tool_registry.py`, `registry_builder.py`
- Phase C: `src/core/orchestration/tool_execution_pipeline.py`
- Phase D: `src/core/orchestration/task_lifecycle.py`
- Phase E: `src/core/orchestration/inference_loop.py`
- Phase F: `src/core/orchestration/orchestrator_bootstrap.py`
- Phase G1-G6: `work_summary.py`, `execution_trace.py`, `provider_capabilities.py`, `tool_preflight.py`, `orchestrator_helpers.py`

This means the right next step is not a new architecture. The right next step is to finish the same style of extraction in the remaining hotspots.

## Current Complexity Hotspots

These are the highest-value refactor targets based on current code structure.

### Tier 1: highest priority

1. `src/core/context/context_builder.py`
2. `src/core/inference/llm_manager.py`
3. `src/core/memory/sqlite_session_store.py`
4. `src/core/orchestration/graph/nodes/execution_node.py`
5. `src/core/orchestration/graph/builder.py`

### Tier 2: next wave

1. `src/core/orchestration/graph/nodes/perception_node.py`
2. `src/core/orchestration/graph/nodes/planning_node.py`
3. `src/tools/subagent_tools.py`
4. `src/core/memory/jsonl_session_store.py`
5. `src/server/app.py`

## Why These Files

### `src/core/context/context_builder.py`

Problems:

- Very large `ContextBuilder` class with broad responsibilities.
- Mixes prompt assembly, caches, repo context loading, instruction loading, token budgeting, task-state injection, and provider/model behavior.
- Pushes against the project's stated preference for functions over stateful classes.

Refactor target:

- Keep `ContextBuilder` as a thin facade or remove it over time.
- Extract pure functions or small modules for:
  - instruction loading/rendering
  - system prompt cache management
  - dynamic environment block generation
  - task-state and TODO block injection
  - repo context and memory context rendering
  - token budget trimming

### `src/core/inference/llm_manager.py`

Problems:

- Oversized module mixing provider registry, config IO, adapter loading, model discovery, streaming, retries, caching, and event publishing.
- `ProviderManager` still reads as a broad coordinator with too many operational details.

Refactor target:

- Split into focused modules around responsibilities:
  - provider config loading/writing
  - provider discovery and activation
  - adapter factory/loading
  - streaming response consumption
  - call-model execution path
  - model cache helpers

### `src/core/memory/sqlite_session_store.py`

Problems:

- Large persistence class that handles connection lifecycle, schema creation, migration, snapshots, forking, rollback, and CRUD access.
- Many methods are operationally unrelated but share one class because of the backing database.

Refactor target:

- Keep one store entry point if needed, but move internals into focused helpers:
  - connection management
  - schema and migration functions
  - snapshot and revert operations
  - session fork/clone logic
  - query helpers for messages/tools/errors/plans

### `src/core/orchestration/graph/nodes/execution_node.py`

Problems:

- Giant implementation function with multiple decision layers.
- Hard to test specific behaviors in isolation.
- Likely central source of cognitive complexity in the graph runtime.

Refactor target:

- Split `_execution_node_impl()` into internal helpers for:
  - step selection and wave advancement
  - tool execution dispatch
  - tool result normalization and truncation
  - TODO state synchronization
  - retry / loop guard / failure path handling

### `src/core/orchestration/graph/builder.py`

Problems:

- Graph assembly, routing policy, heuristics, constants, and route helpers live together.
- Behavior is likely correct, but understanding change impact is expensive.

Refactor target:

- Keep one small graph assembly module.
- Move policy into dedicated routing modules, for example:
  - perception routing
  - planning routing
  - execution routing
  - evaluation/debug routing

## Phased Plan

## Phase 1: Finish the Orchestrator Cleanup Pattern

Objective: apply the same extraction style already used for `orchestrator.py` to the next largest coordination points.

Scope:

- Reduce remaining broad helper logic reachable from orchestration.
- Prefer extracting leaf modules with minimal imports.
- Preserve current imports through compatibility re-exports where needed.

Deliverables:

- Smaller orchestration leaf modules.
- No behavior change.
- Tests updated only where import paths change.

Exit criteria:

- `orchestrator.py` remains mostly wiring and backward-compatible exports.
- New helper modules each have a single clear responsibility.

## Phase 2: Break Up `ContextBuilder`

Objective: remove god-class behavior from prompt/context construction.

Status: largely complete.

Completed slices:

1. Cache operations extracted.
2. Static prompt-part rendering extracted.
3. Dynamic prompt block assembly extracted.
4. Conversation and task-message assembly extracted.

Current state:

- `context_builder.py` now acts primarily as a facade/coordinator.
- Compatibility-sensitive cache globals remain in `context_builder.py` because tests and callers reach into them directly.

Recommended extraction order:

1. Move cache operations to a dedicated module.
2. Move instruction discovery/loading/rendering to dedicated prompt helper modules.
3. Move dynamic context block assembly into pure functions.
4. Keep one thin `build_prompt()` coordinator until callers can depend on the smaller API.

Low-risk first slices:

- Cache read/write/invalidate helpers.
- Dynamic env block generation.
- TODO/task-state/preferences block rendering.

Exit criteria:

- `ContextBuilder` is primarily orchestration glue, not a storage and rendering container.
- Major methods are short and named by concern.

## Phase 3: Split `llm_manager.py` by Responsibility

Objective: turn the inference layer into a set of narrow modules instead of one large manager file.

Status: major slices complete.

Completed slices:

1. Config-path and providers.json helpers extracted.
2. Provider canonicalization and model cache helpers extracted.
3. Provider discovery and adapter loading/factory helpers extracted.
4. Provider probe/status helpers extracted.
5. Post-call processing extracted from `call_model()`.
6. Runtime adapter resolution and invocation extracted from `_call_model_internal()`.
7. Streaming consumption helpers extracted from `_consume_sse_stream()`.

Current extracted modules:

- `src/core/inference/provider_config.py`
- `src/core/inference/model_cache.py`
- `src/core/inference/provider_discovery.py`
- `src/core/inference/provider_loading.py`
- `src/core/inference/provider_probe.py`
- `src/core/inference/call_postprocess.py`
- `src/core/inference/runtime_call.py`
- `src/core/inference/streaming.py`

Current state:

- `llm_manager.py` still owns the stable public API and coordination glue.
- Most operational details have been moved into narrower helper modules.
- Remaining work in the inference area should now focus on opportunistic cleanup and any adjacent modules that are still oversized, not on further breaking apart already-stable helper seams.

Recommended extraction order:

1. Config-path and providers.json read/write helpers.
2. Provider canonicalization and model cache helpers.
3. Adapter loading/factory functions.
4. Streaming consumption helpers.
5. Request execution path used by `call_model()`.

Keep stable APIs:

- `get_provider_manager`
- `call_model`
- `resolve_config_path`
- existing adapter-facing helper functions

Exit criteria:

- `llm_manager.py` becomes a facade module, not the implementation home for every inference concern.

## Phase 4: Decompose Session Storage

Objective: reduce size and responsibility overlap across the session-store layer.

Status: major SQLite slices complete.

Completed slices:

1. Schema and migration helpers extracted.
2. Session fork/revert row operations extracted.
3. Sidecar and diagnostics helpers extracted.
4. Query/result-mapping helpers extracted.

Current extracted modules:

- `src/core/memory/sqlite_store_schema.py`
- `src/core/memory/sqlite_store_session_ops.py`
- `src/core/memory/sqlite_store_sidecar.py`
- `src/core/memory/sqlite_store_queries.py`

Current state:

- `sqlite_session_store.py` remains the stable store facade and connection owner.
- Most deterministic schema, sidecar, fork/revert, and query mapping logic has moved into focused helpers.
- Remaining persistence work should focus on opportunistic cleanup or adjacent backends rather than reopening the extracted seams.

Scope:

- `sqlite_session_store.py`
- `jsonl_session_store.py`
- `session_store.py`

Recommended direction:

- Move shared concepts into backend-agnostic helpers where genuinely shared.
- Do not add a new abstraction layer unless both backends clearly benefit.
- Prefer backend-specific helper modules over inheritance.

Suggested split points:

- schema/migrations
- connection management
- snapshots/revert
- session clone/fork
- read/write query groups

Exit criteria:

- Backends are easier to compare and test independently.
- `SessionStore` becomes a thin selection/compatibility layer.

## Phase 5: Reduce Node-Level Giant Functions

Objective: make graph node behavior understandable at a glance.

Status: `execution_node.py` major deterministic slices continue to land; `graph/builder.py` routing-policy split is largely complete; `perception_node.py` helper extraction is well underway; `planning_node.py` helper extraction is well underway with prompt assembly, resolved-plan payloads, and the main fast-path payload family extracted.

Completed slices:

1. Execution response parsing and no-action handling extracted.
2. Tool tracking, plan-step advancement, and retry/failure helpers extracted.
3. Replan trigger, plan progress, and final payload assembly extracted.
4. Perception-routing policy helpers extracted from `graph/builder.py` into a dedicated module.
5. Execution/debug/replan/evaluation routing helpers extracted from `graph/builder.py` into a dedicated module.
6. Planning, analysis, wait-for-user, and memory-sync routing helpers extracted from `graph/builder.py` into dedicated modules.
7. Perception-node parsing, message-assembly, no-tool, post-call, compaction, and retrieval helpers extracted into dedicated modules.
8. Perception-node runtime-prep helpers extracted into a dedicated module.
9. Perception-node final result assembly extracted into a dedicated module.
10. Planning-node prompt/task-description assembly extracted into a dedicated module.
11. Planning-node resolved-plan payload assembly extracted into a dedicated module.
12. Planning-node early fast-path payloads extracted into a dedicated module, including resumed-plan, existing-plan, simple next-action, and early-response returns.
13. Execution-node LLM step-generation for plan steps extracted into a dedicated helper.
14. Execution-node preflight, plan-mode, completion-signal, and role-gate early returns extracted into a dedicated helper.
15. Execution-node plan-progress event emission and TODO sync extracted into a dedicated helper.
16. Execution-node post-tool bookkeeping updates extracted into a dedicated helper.
17. Execution-node step.start / step.finish event emission extracted into dedicated helpers.
18. Execution-node preview-mode early return extracted into a dedicated helper.
19. Execution-node successful read-then-write branch extracted into a dedicated helper.
20. Execution-node async post-tool hook scheduling extracted into a dedicated helper.
21. Execution-node lock-manager / tool-dispatch selection extracted into a dedicated helper.
22. Server SSE/WebSocket backpressure queue handling extracted into shared helpers.
23. JSONL session-store snapshot/fork filename and payload helpers extracted.
24. Subagent role/tool-policy and persisted payload builders extracted into helpers.
25. Subagent initial-state assembly and result formatting extracted into pure helpers.
26. Server admin-auth/header parsing and SSE env-config parsing extracted into helpers.
27. JSONL sidecar atomic-write fallback path extracted into a shared helper.
28. Server default event subscription list and WebSocket `events` parsing extracted into helpers.
29. Server WebSocket control-message parsing and `_control` payload builders extracted into helpers.
30. Server scheduler endpoint auth/import wrappers extracted into shared helpers.
31. Subagent child-history extraction and child-session file-path assembly extracted into helpers.
32. Subagent dispatch-result content selection extracted into a dedicated helper.
33. Context-builder token truncation helpers extracted into a dedicated module.
34. Context-builder stale tool-output pruning extracted into a dedicated helper.
35. Context-builder retrieved-snippet descriptor conversion and budget filtering extracted into helpers.
36. Context-builder agent-brain role/skill directory loading and workspace skill override merging extracted into helpers.
37. SQLite session-store message search SQL/params construction extracted into query helpers.
38. SQLite session-store mistake-search SQL/params construction extracted into query helpers.
39. SQLite recent-decisions query/params construction extracted into query helpers.
40. Execution-node step-retry count normalization/increment extracted into execution helpers.
41. Shared tool-output truncation helper extracted for execution/frontier nodes with compatibility wrappers retained.
42. Execution-node orchestrator resolution and subagent/config error normalization extracted into execution helpers.
43. Execution-node cancellation-result and planned-action selection preamble extracted into execution helpers.
44. Execution-node step-transaction start and plan/wave logging branches extracted into execution helpers.
45. Execution-node plan-step execution logging and UI tool-result sync extracted into execution helpers.
46. Execution-node no-action outcome logging extracted into execution helpers.
47. Execution-node plan-mode approval and affected-file propagation extracted into execution helpers.
48. Planning-node orchestrator resolution and config-error normalization extracted into planning helpers.
49. Planning-node last-plan persistence path/load/save implementation extracted into planning helpers.
50. Planning-node resume-eligibility and repo-context hydration logic extracted into planning helpers.
51. Perception-node orchestrator resolution and cancellation preamble extracted into perception runtime helpers.
52. Perception-node turn-limit early return extracted into perception runtime helpers.
53. Perception-node call-model and adapter validation extracted into perception runtime helpers.
54. Perception-node near-turn-limit prompt tool filtering extracted into perception runtime helpers.
55. Perception-node dynamic skill injection for debugging/search tasks extracted into perception runtime helpers.
56. Perception-node compacted-history prompt bootstrap extracted into perception compaction helpers.
57. Perception-node role selection from agent mode extracted into perception runtime helpers.
58. Perception-node provider/model context resolution extracted into perception runtime helpers.
59. Builder frontier/lite nested routing helpers extracted into tier_graph_routing.
60. Builder tier-graph cache-key selection and compile dispatch extracted into tier_graph_routing.
61. Builder PRSW mixed-role delegation detection extracted into tier_graph_routing.
62. Builder lite-mode selection extracted into tier_graph_routing.
63. llm_manager model-name selection extracted into a dedicated inference helper.
64. llm_manager LM Studio full-id normalization extracted into a dedicated inference helper.
65. llm_manager canonical provider normalization extracted into a dedicated inference helper.
66. llm_manager provider model normalization extracted into a dedicated inference helper.
67. llm_manager providers.json config-path resolution extracted into a dedicated inference helper.
68. llm_manager provider active-flag update delegation extracted into a dedicated inference helper.
69. llm_manager provider-config loading extracted into a dedicated inference helper.
70. llm_manager provider-config persistence extracted into a dedicated inference helper.
71. llm_manager provider validation delegated into the shared inference probe helper.
72. llm_manager active-provider lookup delegated into the shared provider-config helper.
73. llm_manager active-model fallback logic delegated into the shared provider-discovery helper.
74. llm_manager provider model-source fallback chain delegated into the shared provider-discovery helper.
75. llm_manager requested-model resolution and missing-model event publication delegated into the shared model-selection helper.
76. ProviderManager initialize probe/caching/event phase delegated into the shared provider-probe helper.
77. ProviderManager initialize provider-loading/registration phase delegated into the shared provider-loading helper.
78. TUI system settings hydration consolidated so `AgentBridge` owns the real settings/config load path and translates `system.settings` into `SystemSettingsLoaded`.
79. TUI subagent lifecycle bookkeeping normalized so `_subagent_widgets` is the single source of truth for active subagent widgets.
80. TUI provider identity normalization started by introducing a shared normalized provider `id` in `SettingsStore` and switching settings/palette/app selection paths to consume it.
81. Context-controller budget status aligned with actual enforcement by reporting the latest enforced snippet usage against the effective available context budget instead of a stale configured maximum.
82. Packaging/config drift reduced by registering all default-discovered custom pytest markers in `pytest.ini` and replacing placeholder metadata in `tui/pyproject.toml` with real TUI package metadata.
83. Network permission semantics aligned by mapping the real network tool names into the permission-table network kinds so stored allow/deny rules apply consistently alongside the `PermissionLevel.DANGER` gate.
84. Toolset loader compatibility restored by forwarding the canonical model-aware `load_toolset_for_model()` helper through the legacy `src.tools.toolsets.loader` shim.
85. ContextBuilder repository-intelligence budgeting/rendering branch extracted into a dedicated helper so `build_prompt()` no longer inlines snippet budget enforcement and repository block assembly.
86. ContextBuilder dynamic per-turn system-prompt assembly extracted into a dedicated helper so `build_prompt()` no longer inlines prior-context, repository-context, and LSP-context block assembly.
87. ContextBuilder conversation-pruning and final task-message assembly extracted into a dedicated helper so `build_prompt()` no longer inlines post-system conversation filtering, truncation, and user-task append logic.
88. ContextBuilder sanitization logic extracted into `src/core/context/sanitization.py` so the builder keeps a thin delegator instead of owning the prompt-injection/comment-collapse implementation directly.
89. ContextBuilder final `HOOK_CONTEXT_BUILT` dispatch extracted into a dedicated helper so `build_prompt()` stays focused on prompt assembly rather than plugin-notification plumbing.

Current extracted modules:

- `src/core/orchestration/graph/nodes/execution_helpers.py`
- `src/core/orchestration/graph/perception_routing.py`
- `src/core/orchestration/graph/execution_routing.py`
- `src/core/orchestration/graph/analysis_routing.py`
- `src/core/orchestration/graph/planning_routing.py`
- `src/core/orchestration/graph/session_routing.py`
- `src/core/orchestration/graph/tier_graph_routing.py`
- `src/core/orchestration/graph/nodes/perception_parsing.py`
- `src/core/orchestration/graph/nodes/perception_no_tool.py`
- `src/core/orchestration/graph/nodes/perception_post_call.py`
- `src/core/orchestration/graph/nodes/perception_messages.py`
- `src/core/orchestration/graph/nodes/perception_compaction.py`
- `src/core/orchestration/graph/nodes/perception_retrieval.py`
- `src/core/orchestration/graph/nodes/perception_runtime.py`
- `src/core/orchestration/graph/nodes/perception_result.py`
- `src/core/orchestration/graph/nodes/planning_prompt.py`
- `src/core/orchestration/graph/nodes/planning_result.py`
- `src/core/orchestration/graph/nodes/planning_fast_paths.py`
- `src/core/orchestration/graph/nodes/planning_helpers.py`
- `src/core/orchestration/graph/nodes/execution_helpers.py` now also owns extracted execution step-generation, preflight/role-gate, preview-mode, step-event, successful read-then-write, async post-tool hook scheduling, lock-manager/tool-dispatch selection, plan-progress/TODO-sync, post-tool bookkeeping, step-retry normalization, orchestrator-resolution, cancellation, action-selection, step-transaction, plan/wave logging, plan-step logging, UI sync, no-action outcome logging, and execution-state propagation helpers.
- `src/core/orchestration/graph/nodes/tool_output_truncation.py` now owns the shared large-tool-result truncation implementation used by execution/frontier wrappers.

Current state:

- `execution_node.py` still owns orchestration-heavy side effects, but most pure decision logic now lives in `execution_helpers.py`.
- `execution_node.py` still owns the core execution flow and the most side-effect-heavy operations, but additional pre-execution LLM step-generation, gating branches, preview handling, step events, successful read-then-write handling, async post-tool hook scheduling, lock-manager/tool-dispatch selection, and post-tool completion/bookkeeping helpers now live in `execution_helpers.py`.
- `builder.py` now mostly owns graph assembly, compatibility re-exports, and tier-specific graph builders.
- Perception, execution, planning, analysis, and session routing helpers now live in dedicated modules and are re-exported from `builder.py` for compatibility.
- `perception_node.py` now retains the stable entrypoint and compatibility wrappers while most deterministic helper seams live in dedicated modules beneath it.
- The remaining work in `perception_node.py` should focus only on small orchestration or notification-heavy slices that still read better as named helpers; the main extraction gains are already in place.
- `planning_node.py` now retains the stable entrypoint while prompt construction and the main repeated return payloads live in dedicated helper modules.
- `planning_node.py` appears to be at a reasonable stopping point for low-risk extraction; any further work there should target only clear post-LLM or persistence-adjacent duplication.

Priority order:

1. `execution_node.py`
2. `planning_node.py`
3. `perception_node.py`

Method:

- Keep node entrypoints intact.
- Extract pure decision helpers first.
- Extract rendering/parsing helpers second.
- Extract side-effectful operations last.

Exit criteria:

- Main node functions read top-down as a short control flow.
- Branch-heavy logic is named and unit-tested separately.

## Phase 6: Secondary Cleanup Targets

Objective: address the next wave after the highest-risk hotspots are under control.

Targets:

- `src/tools/subagent_tools.py`
- `src/server/app.py`
- any large compatibility layers that remain after the main refactor phases

These should wait until the core orchestration, context, inference, and persistence seams are clearer.

## Working Rules For Each Refactor

- One extraction at a time.
- Keep behavior stable.
- Preserve public imports unless there is a deliberate migration.
- Add or update focused unit tests around extracted seams.
- Avoid simultaneous renames plus logic changes in the same patch.
- Prefer moving pure logic first, then simplifying callers.

## Suggested Milestone Order

1. Finalize the orchestrator-adjacent cleanup pattern.
2. Finalize `ContextBuilder` cleanup and compatibility shims.
3. Finalize `llm_manager.py` cleanup and shift to adjacent inference modules as needed.
4. Finalize the extracted SQLite/session-store internals and only then revisit adjacent persistence modules if still needed.
5. Continue refactoring giant graph node and routing functions, starting from the remaining `builder.py` route helpers.
6. Clean up secondary modules.

## How To Decide If A Refactor Slice Is Good

A slice is good if it satisfies most of these:

- one reason to change
- one dominant responsibility
- smaller import surface
- fewer circular-import guards
- easier unit testing without heavyweight setup
- fewer instance attributes needed just to call helper logic

## Success Metrics

- Fewer 1000+ line modules.
- Fewer classes acting as mixed-responsibility coordinators.
- Shorter top-level node and manager methods.
- Lower need for broad monkeypatch-heavy tests.
- Same external behavior and test results.

## First Concrete Refactors To Execute

If work starts immediately, this is the safest order:

1. Continue reducing `perception_node.py` by extracting any remaining low-risk helper seams while preserving compatibility wrappers.
2. Reassess `planning_node.py` as the next graph-node hotspot once `perception_node.py` reaches a stable stopping point.
3. Revisit any remaining SQLite/session-store cleanup only where helper seams are still mixed into facade code.
4. Revisit any remaining inference compatibility shims only if they still block testing or readability.
5. Clean up secondary modules after the graph/runtime seams stabilize.

## Notes

- This document is a reconstructed plan; the original phased plan is not present in the repository.
- The current repo state strongly suggests the team was already pursuing incremental extraction over rewrite.
- The best continuation is to keep that same approach and push it into the remaining hotspots.
