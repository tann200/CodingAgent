# CodingAgent Engineering Audit

**Date:** 2026-10-09  
**Scope:** Current source code and current tests only. Historical Git documentation, plans, reports, and README claims were not used as evidence.  
**Method:** Every live Python class was reviewed by implementation partition. The runtime TUI tree is `tui/tui_src`; its source mirror `tui/src` is byte-identical when generated caches are excluded.

## Review Coverage

The review covers all **497 canonical class declarations**: 372 in `src/` and 125 in the runtime TUI tree. Including the identical non-runtime TUI mirror gives 622 declarations in the checkout. Coverage was partitioned as follows:

| Partition                                                         | Classes | Review focus                                                              |
| ----------------------------------------------------------------- | ------: | ------------------------------------------------------------------------- |
| `src/core/context` and `src/core/orchestration`                   |     127 | Graphs, state, permissions, sessions, planning, locking, hooks, rollback  |
| `src/core/inference` and `src/core/messaging`                     |     146 | Providers, adapters, fallback, model profiles, events, delivery           |
| Other `src/core` packages                                         |      73 | Memory, indexing, LSP, MCP, evaluation, config reload, telemetry, plugins |
| `src/tools`, `src/server`, `src/config`, `src/cli`, `src/main.py` |      26 | Tool safety, server API, configuration, entry points                      |
| `tui/tui_src`                                                     |     125 | Bridge, app, messages, screens, components, nested message classes        |

The audit found no unreviewed live class. Findings below are implementation defects or incomplete integrations; each includes a reproducible trigger and a regression-test target.

## 1. Executive Summary

CodingAgent has useful defensive infrastructure: a centralized tool pipeline, atomic patch paths, typed messaging, checkpoints, session stores, provider abstractions, and broad focused unit tests. It is not production-ready as an autonomous coding agent or a network-exposed service.

The highest-risk design problem is a mismatch between available and selected control paths. The full graph contains planning, step control, debugging, and verification, but typical non-lite runs select an LLM-directed frontier loop. That loop can declare success from text alone. The corresponding safety boundary is weakened further by fail-open sandbox behavior, unrestricted sensitive reads, raw tool registry access, and an optional task server that accepts arbitrary host directories.

**Deployment conclusion:** use only for supervised local work in a trusted workspace. Do not expose the HTTP server, grant broad autonomous approval, or rely on sandboxing as a host-security boundary before the Phase 1 fixes are complete.

**Remediation note:** this report was authored against an earlier revision. Section 15 reconciles every Phase 1 and section-4 High finding with the current tree (status, current source evidence, and the commits that changed it). Treat §15 as the current status of these findings.

## 2. Architecture Strengths

- `src/main.py` separates CLI dispatch from the Textual UI; the runtime UI communicates with core services through bridge/event abstractions.
- Model-originated tool calls normally pass through `tool_execution_pipeline.py`, which composes normalization, permission checks, workspace scope, read-before-write, approval, contracts, snapshots, hooks, timeouts, events, and traces.
- Patch/edit tooling has atomic write and stale-context protections, while loop, round, token, cancellation, and checkpoint controls provide a base reliability envelope.
- JSONL/SQLite session persistence, compaction, graph checkpoints, provider fallback, LSP/index abstractions, telemetry, and scenario/evaluation utilities are present.
- Current tests cover many individual safeguards, including shell-pattern bypasses, tool contracts, routing, rollback, SSRF pre-checks, sessions, and UI bridging.

## 3. Critical Architectural Flaws

No confirmed Critical finding was identified. The following High findings prevent deterministic, trustworthy completion.

| Severity | Finding                                                                 | Evidence                                                                                    | Trigger and impact                                                                                                                                    | Fix and regression test                                                                                                                                            |
| -------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| High     | Live non-lite runs bypass persisted planning and step control.          | `src/core/orchestration/graph/builder.py:451-616`; `tier_graph_routing.py:151-167`          | Medium, large, and frontier models use the frontier graph, omitting plan validation and step control. Writes are not tied to persisted, scoped steps. | Route mutations through the full graph or add equivalent mandatory plan state. Test frontier writes for persisted plan, scoped action, and post-step verification. |
| High     | Text-only frontier output completes a task.                             | `frontier_loop_node.py:938-952`; `verification_node.py:87-151`; `evaluation_node.py:70-123` | Any no-tool output, including empty output, sets completion; empty plans evaluate as complete.                                                        | Require task-type-specific evidence. Test a requested file edit with text-only provider output and require failure.                                                |
| High     | Lite/small paths skip verification and evaluation.                      | `graph/builder.py:628-685`; `tier_graph_routing.py:145-148`                                 | A mutation or failed command can reach memory sync and end without outcome validation.                                                                | Retain minimum mutation verification on every tier. Test failed lite write and shell actions.                                                                      |
| High     | State contract enforcement is fail-open and excludes the frontier loop. | `graph/builder.py:97-107,485-486`; `state_schemas.py:324-476`                               | Invalid output is logged; no frontier output schema is wrapped. Partial state can influence routing/completion.                                       | Enable strict production validation and cover all nodes. Test invalid frontier fields abort the run.                                                               |
| Medium   | Workflow configuration is not the runtime graph authority.              | `src/core/inference/workflow_selector.py:39-130`                                            | `WorkflowConfig`, `LoopControl`, and several declared limits have no observed graph-construction consumer.                                            | Use one configuration source to compile runtime graph behavior. Test each profile changes actual graph limits/capabilities.                                        |
| Medium   | Deferred plugin startup is not integrated.                              | `src/core/orchestration/deferred_init.py:68-209`                                            | Trusted plugin/MCP discovery has no live orchestration caller.                                                                                        | Wire startup with trust controls or remove unsupported configuration. Test trusted/untrusted startup end-to-end.                                                   |

## 4. High-Risk Safety Issues

| Severity | Finding                                                                  | Evidence                                                                                                                       | Trigger and impact                                                                                                                         | Fix and regression test                                                                                                                           |
| -------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| High     | Sandbox falls back to unrestricted host execution.                       | `src/tools/sandbox.py:397-480,515-524`; `src/tools/_bash_exec.py:560-568`                                                      | Missing/non-enforcing backend, common on macOS, permits model shell commands as the user.                                                  | Fail closed unless an explicit unsafe opt-out is set. Test no backend rejects shell execution.                                                    |
| High     | Linux sandbox shares writable host `/tmp`.                               | `src/tools/sandbox.py:311-317`                                                                                                 | Sandboxed commands can access unrelated temporary data.                                                                                    | Use a private tmpfs/task directory. Test a host temp sentinel is inaccessible.                                                                    |
| High     | Redirects bypass SSRF validation.                                        | `src/tools/web_tools.py:293-309`                                                                                               | Valid public URL redirects to loopback, metadata, or private network.                                                                      | Disable automatic redirects; validate every hop and peer address. Test redirect-to-loopback denial.                                               |
| High     | Server tasks may select arbitrary host directories.                      | `src/server/task_endpoints.py:89-114,246-253`                                                                                  | Authenticated caller, or local caller with no token, supplies any existing directory. Canonicalization is not authorization.               | Enforce configured workspace roots and caller authorization. Test `/etc`, home, and sibling repo denial.                                          |
| High     | Raw `ToolRegistry.call()` bypasses central enforcement.                  | `src/tools/_registry.py:305-316`; `tool_execution_pipeline.py:905-1071`                                                        | Plugin or new integration calls a raw mutator without approval, contracts, snapshots, hooks, timeout tracing, or audit.                    | Restrict raw calls or route all calls through guarded execution. Test direct mutation receives identical controls.                                |
| High     | Secret-bearing content can enter prompts, sessions, and logs.            | `src/tools/_file_io.py:354-442,589-620,757-796`; `tool_execution_pipeline.py:885-894`; `frontier_loop_node.py:456-457,559-599` | `.env`, keys, and arbitrary bytes are readable; negative chunk/byte limits can remove intended bounds; outputs are recorded and previewed. | Add protected-path policy and redaction before prompt, persistence, trace, export, event, or response. Test secrets never appear in any sink.     |
| High     | Prior-session memory is inserted without the available safety scanner.   | `context_builder.py:529-569`; `memory/distiller.py:618-633`; `memory/security.py:63-90`                                        | A poisoned summary controls later task context.                                                                                            | Scan at write and retrieval; label retained content as untrusted data. Test instruction-bearing memory is omitted.                                |
| High     | `rename_file` can overwrite a destination without equivalent safeguards. | `src/tools/_file_io.py:527-586,742-754`                                                                                        | POSIX rename replaces an existing destination; direct rename/create calls lack the write/delete guard path and rollback coverage.          | Reject existing destination unless explicitly reviewed; guard all mutators and snapshot both paths. Test rejected overwrite preserves both files. |
| High     | Fallback file locking can evict a live holder.                           | `src/tools/file_lock.py:244-248`                                                                                               | On fallback locking, a live same-host PID is reclaimed after successful `os.kill(pid, 0)`.                                                 | Reclaim only conclusively dead owners. Test live owner cannot be unlinked by another process.                                                     |
| High     | Parallel delegation re-enters one `Context` concurrently.                | `src/tools/subagent_tools.py:1034,1039-1046,1071-1073`                                                                         | Two overlapping delegated tasks use one `contextvars.Context`, causing `RuntimeError`.                                                     | Copy one context per submitted task. Test barrier-synchronized parallel tasks both succeed.                                                       |

## 5. Major Missing Capabilities

- Evidence-based completion contracts for informational, read-only, and mutating tasks.
- A portable default-deny execution isolation model and private per-task temporary storage.
- Enforced repository retrieval, plan persistence, scoped execution, and postcondition verification for every mutation tier.
- Protected-path and content-level secret classification with universal redaction.
- Server tenancy controls: workspace allowlists, bounded admission, caller/session isolation, and cancellation-before-start.
- Repository intelligence that indexes code chunks, references, dependencies, and test relationships rather than only symbols/paths.

## 6. Workflow Reliability Issues

| Severity | Finding                                                      | Evidence                                                                                  | Trigger and impact                                                          | Fix and regression test                                                                                            |
| -------- | ------------------------------------------------------------ | ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| High     | Provider fallback cannot serve an open circuit breaker.      | `src/core/inference/llm_manager.py:1395-1403`; `provider_fallback.py:141-145`             | Primary breaker returns before fallback chain despite healthy secondary.    | Route breaker-open results into fallback. Test secondary succeeds.                                                 |
| High     | Anthropic adapter uses an OpenAI request shape.              | `src/core/inference/adapters/anthropic_adapter.py:54`; `openai_compat_adapter.py:330-339` | Inherited `/chat/completions` payload is sent to Anthropic native API.      | Implement Messages API request/response mapping. Test endpoint, system block, `max_tokens`, and content parsing.   |
| Medium   | Fallback preserves unsupported provider-specific model IDs.  | `provider_fallback.py:189-198`                                                            | OpenAI model is sent unchanged to Ollama/other fallback.                    | Resolve candidate model/default per provider. Test `gpt-4o` falls back to configured local model.                  |
| Medium   | Timeout reports failure without terminating underlying work. | `tool_execution_pipeline.py:629-653`                                                      | Thread continues a delayed write after future cancellation.                 | Use killable processes/groups or cooperative cancellation. Test delayed writer never creates a file after timeout. |
| Medium   | File-lock ownership guard can be cancelled by a stale owner. | `file_lock_manager.py:53,183-185,226-228`                                                 | Expired owner A releases after B acquires and cancels B's shared guard.     | Keep guards per owner/path and verify ownership before cancellation. Test B auto-releases.                         |
| Medium   | Shutdown timeout is not enforced.                            | `session_lifecycle.py:141-146,164-173`                                                    | Blocking callback ignores declared timeout and stalls cleanup.              | Run hooks under bounded execution and report timeouts. Test bounded return.                                        |
| Medium   | Forced compaction may call an LLM under context pressure.    | `memory_update_node.py:121-131,178-185`; `compaction_service.py:164-230`                  | Overflow path is LLM-first despite non-stalling intent.                     | Use deterministic compaction first. Test no model call on forced compaction.                                       |
| Medium   | Cancelled queued server task still begins execution.         | `src/server/task_endpoints.py:147-183,282-301`                                            | Cancellation before worker start still creates orchestrator and runs tools. | Check cancellation before state transition/construction. Test orchestrator is never called.                        |

## 7. Tool System Weaknesses

- Mutation safety is inconsistent: rename and directory creation do not share `write_file`/`delete_file` direct controls, while rollback tracks only one path and excludes several operations: `tool_execution_pipeline.py:549-561,1025-1029`; `src/tools/rollback_tools.py:31-41`.
- Policy failures can permit continuation: deny-write configuration, permission-table lookup, and hook failures are ignored in paths at `src/tools/_file_io.py:79-106`, `permission_gateway.py:597-634`, and `tool_execution_pipeline.py:1039-1053`.
- Tool schemas do not reject undeclared fields: `src/tools/_tool.py:114-233`. Set `additionalProperties: false` semantics and test extra arguments.
- `symbol_reader.read_file_chunked()` reads external absolute paths in its full-file branch: `src/tools/symbol_reader.py:229-253`. Route every branch through workspace resolution and test escape denial.
- Large/negative `tail_log_file` input can read whole files and reports byte size as line count: `src/tools/_file_io.py:679-736`. Bound lines and bytes and return correct metadata.

## 8. Repository Awareness Gaps

| Severity | Finding                                                                               | Evidence                                                            | Trigger and impact                                                                           | Fix and regression test                                                                             |
| -------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| High     | LSP prompt context is always empty for actual `SymbolGraph` data.                     | `src/core/indexing/lsp_context.py:56-76`; `symbol_graph.py:244-294` | Code iterates class/function lists as symbol dictionaries; exception is swallowed.           | Iterate class/function lists correctly. Test indexed class and function appear in context.          |
| High     | Refreshing an edited file corrupts index entry schema and leaves symbol search stale. | `src/core/indexing/repo_indexer.py:568-583,520-536`                 | Refresh replaces normal file entry with parser output and does not update top-level symbols. | Rebuild normal entry and replace symbols for path. Test old symbol disappears/new symbol retrieves. |
| Medium   | Retrieval is symbol/path oriented, not code/dependency/test aware.                    | `vector_store.py:266-396`; `repo_indexer.py:92-162`                 | Planning may not receive implementation bodies, references, or related tests.                | Add chunk/reference/test index and retrieval evaluation.                                            |
| Medium   | Index freshness relies on root directory mtime.                                       | `analysis_node.py:151-175`                                          | Editing existing file may not update root directory mtime, suppressing reindex.              | Track per-file digest/revision. Test source edit reindexes.                                         |
| Medium   | Simple frontier tasks skip analysis/retrieval.                                        | `tier_graph_routing.py:35-54`; `frontier_loop_node.py:475-489`      | Most simple edits have no automatic repository context.                                      | Require retrieval before planning/editing. Test retrieved context on simple write request.          |

## 9. Memory System Evaluation

The persistence surface is substantial: JSONL and SQLite stores, snapshots, compaction, distillation, frozen memory, decisions, and checkpoints. It has functional correctness defects as well as contamination risks.

| Severity | Finding                                                                           | Evidence                                                 | Trigger and impact                                                                                              | Fix and regression test                                                                                  |
| -------- | --------------------------------------------------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| High     | SQLite session deletion executes invalid schema deletes.                          | `src/core/memory/sqlite_session_store.py:753-773`        | `session_children` lacks `session_id`; snapshot rows require `snapshot_id`. Transaction fails and data remains. | Delete children by parent ID and rows by snapshot ID. Test parent, child, message, and snapshot removal. |
| Medium   | Session/task identity from HTTP requests is not propagated to orchestrator state. | `src/server/task_endpoints.py:136-183`                   | Client session ID is stored for events but not assigned to orchestrator/checkpoint lifecycle.                   | Pass explicit task/session context. Test distinct submissions persist separately.                        |
| Medium   | Memory/decision retrieval has no common redaction or trust metadata.              | `context_builder.py:529-569`; `memory/security.py:63-90` | Persisted content is indistinguishable from trusted instructions.                                               | Store provenance/trust classification; sanitize before prompt construction.                              |

## 10. Evaluation And Testing Gaps

The project has extensive unit, mock integration, scenario, E2E, benchmark, and evaluation classes. This is not yet a reliable measure of agent quality because key runtime invariants lack end-to-end regression coverage.

- Required CI lacks scenarios for false completion, no-backend sandbox denial, redirect-chain SSRF, secret redaction, direct registry parity, cross-workspace server access, timeout termination, and all mutation rollback paths.
- Tests cover static policy registry (`EndpointPolicy`), but it has no observed runtime enforcement integration: `src/server/exposure_policy.py:33`.
- Live provider checks are scheduled/manual in `.github/workflows/ci.yml:200-216`, so provider compatibility regressions such as the Anthropic request mismatch can merge without required coverage.
- Full local collection currently requires the `server` extras; absent `fastapi`/`uvicorn` caused collection failures during the earlier documentation verification.

## 11. Usability Problems

- `codingagent mcp add` writes `.agent/config.json`, while active configuration and `init` use `.codingAgent`: `src/cli/mcp_cmd.py:96-129`; `src/core/config_loader.py:76-136`; `src/main.py:335-447`.
- `ConfigReloader` recursively invokes config loading during first initialization, its normal watcher exits after baseline initialization, it watches a file outside active JSON configuration hierarchy, and no live path executes pending reloads: `config_loader.py:158-171`; `config_hot_reload/__init__.py:57-106,169-187,241-251`.
- TUI role selection updates display state but not bridge execution role: `tui/tui_src/ui/app.py:696-701`; `_app_status_handlers_mixin.py:167-172`; `_bridge_agent.py:85,99`.
- Session-list resume/delete use UI selection index instead of data index when the synthetic “Start new session” row is visible: `tui/tui_src/ui/screens/session_list.py:245-280`.

## 12. Performance Bottlenecks

- Frontier INFO logs include prompts, assistant content, and tool-derived history, multiplying sensitive data and per-turn allocations: `frontier_loop_node.py:456-457,559-599,945-950`. Use redacted diagnostic logs only.
- WebSocket clients control unbounded queue/keepalive values: `src/server/websocket_handler.py:94-156`. Clamp values server-side.
- HTTP task executor has an unbounded work queue: `src/server/task_endpoints.py:32-35,217-254`. Apply bounded admission and return overload status.
- SSE adapter writes `asyncio.Queue` from worker threads without `loop.call_soon_threadsafe`: `src/server/sse_adapter.py:45-68`; tasks publish from executor workers at `task_endpoints.py:136-211`. Test threaded delivery and fix loop ownership.
- Hardware detection invents 16 GB GPU VRAM for CPU-only Linux: `hardware_capability_profile.py:321-374`. Preserve zero VRAM and choose CPU/unified profile explicitly.
- `KVCacheGovernor` stores custom thresholds but state checks hard-code 70/85 percent: `kv_cache_governor.py:45-61,83-101`. Test custom 50/60 thresholds.

## 13. Over-Engineered Or Dormant Components

The following classes have no meaningful observed production integration, or duplicate active capability. They should be wired with tests or removed to reduce misleading surface area:

- Full graph nodes for planning/debug/replan/delegation while live non-lite routing uses the frontier loop.
- `GitWorktreeManager`, `ContextManager`, `EventLog`, and `WaveCoordinator` in orchestration.
- `FrozenMemoryStore`, `SkillLoader`, `SkillRegistry`, `SystemPromptBuilder`, `HookRegistry`, `TelemetryConsumer`, `TelemetryMetrics`, `RollingHistogram`, `MetricsStore`, `Timer`, `IncrementalIndexer`, and `ResiliencePolicy`.
- `AdapterWrapper`, deprecated `AgentMode`/`AgentModeSettings`, `WorkflowType`/`WorkflowConfig`/`LoopControl`, plus test-focused `MockAdapter`.
- `ToolsetManager`, `Truncate`, and the unregistered symbol-reader wrapper path.
- TUI `WorkflowController`, `Coordinator`, legacy `widgets.DiffViewer`, and `HistoryInput`.

The source mirror is currently identical, but maintaining both `tui/src` and `tui/tui_src` doubles future drift risk.

## 14. Prioritized Fix List

### Phase 1 - Critical Stability Fixes

Status is reconciled against the current tree in §15 (with source evidence).
`OPEN` = present as described; `PARTIAL` = primary risk mitigated, contract not
fully met; `MIXED` = sub-items differ.

| Task                                                                                   | Location                                              | Complexity | Expected impact                                            | Status |
| -------------------------------------------------------------------------------------- | ----------------------------------------------------- | ---------: | ---------------------------------------------------------- | ------ |
| Add evidence-based completion contracts and minimum verification to every tier.        | graph builder, frontier, verification, evaluation     |       High | Stops false success and unverified edits.                  | OPEN |
| Make unavailable sandbox enforcement deny by default; isolate temporary storage.       | `sandbox.py`, `_bash_exec.py`                         |     Medium | Prevents host-privilege shell execution.                   | PARTIAL |
| Add protected paths, secret detection, and universal redaction.                        | file tools, pipeline, context, memory, events, server |       High | Prevents credential disclosure.                            | OPEN |
| Enforce task-server workspace roots, bounded admission, and cancellation-before-start. | `server/task_endpoints.py`                            |     Medium | Prevents arbitrary-host access and queue abuse.            | FIXED |
| Validate redirect hops and peer addresses.                                             | `web_tools.py`                                        |     Medium | Closes SSRF redirect bypass.                               | FIXED |
| Repair SQLite session deletion, LSP context, and incremental index refresh.            | memory/indexing modules                               |     Medium | Restores persistence and repository awareness correctness. | FIXED |

### Phase 2 - Robustness Improvements

| Task                                                                                                     | Location                                | Complexity | Expected impact                                           |
| -------------------------------------------------------------------------------------------------------- | --------------------------------------- | ---------: | --------------------------------------------------------- |
| Route every tool invocation through guarded execution; make all mutations transactional.                 | registry, pipeline, file/rollback tools |       High | Consistent permission, rollback, and audit behavior.      |
| Implement Anthropic native API mapping and provider-compatible fallback model resolution.                | inference adapters/fallback/manager     |     Medium | Makes configured providers and failover functional.       |
| Enforce strict output schemas, per-owner locks, killable timeout execution, and bounded lifecycle hooks. | graph, locks, pipeline, lifecycle       |       High | Prevents hidden corruption and post-timeout side effects. |
| Sanitize memory at write/retrieval and persist provenance.                                               | context/memory                          |     Medium | Mitigates cross-session injection.                        |
| Repair config reload lifecycle and reconcile `.agent`/`.codingAgent`.                                    | config modules, MCP CLI                 |     Medium | Makes runtime configuration predictable.                  |

### Phase 3 - Capability Improvements

| Task                                                                                              | Location                 | Complexity | Expected impact                              |
| ------------------------------------------------------------------------------------------------- | ------------------------ | ---------: | -------------------------------------------- |
| Require repository retrieval, plan persistence, scoped execution, and verification for mutations. | graph, analysis, context |       High | Deterministic repository-aware changes.      |
| Index code chunks, references, dependencies, and tests with per-file freshness.                   | indexing                 |       High | Higher retrieval and test-selection quality. |
| Fix thread-safe SSE delivery and bound WebSocket controls; wire real endpoint policies.           | server modules           |     Medium | Reliable observable local service.           |
| Synchronize TUI role state and implement spawn-approval UI.                                       | runtime TUI bridge/app   |     Medium | Makes displayed controls affect execution.   |

### Phase 4 - Advanced Features

| Task                                                                                                                           | Location          | Complexity | Expected impact                                        |
| ------------------------------------------------------------------------------------------------------------------------------ | ----------------- | ---------: | ------------------------------------------------------ |
| Make completion, sandbox, SSRF, secrets, rollback, server isolation, providers, and timeout termination required CI scenarios. | tests and CI      |     Medium | Prevents high-risk regressions.                        |
| Create repository-scale evaluation for retrieval, plans, edits, recovery, and tool safety.                                     | evaluation and CI |       High | Measures actual agent quality.                         |
| Wire or remove dormant subsystems and consolidate the TUI mirror.                                                              | core and `tui/`   |     Medium | Reduces maintenance and misleading capability surface. |

## 15. Remediation Status Against the Current Tree

The findings above were authored against an earlier revision. The current working
tree additionally contains a remediation branch that the audit authors could not
see. This section reconciles the two. **Every status below was re-verified by
reading the current source**; the paths and line numbers are current, not the
audit's original citations.

Status legend: **FIXED** — the cited defect is no longer present; **PARTIAL** —
the primary risk is mitigated but the audit's stated contract is not fully met;
**OPEN** — the defect is present as described.

### Phase 1 — Critical Stability Fixes

| # | Item | Status | Current evidence | Commits |
| - | ---- | ------ | ---------------- | ------- |
| 1 | Evidence-based completion + minimum verification on every tier | **OPEN** (partial) | `frontier_loop_node.py:783-796` still sets `ok=True, completed_without_tool=True, _completion_detected=True` on any no-tool turn (including empty text). `verification_node.py:76,130,138` still gates on `last_tool_name` and `_WRITE_TOOLS_ALWAYS_VERIFY`, not on `modified_files`/side effects. | Lite-tier verification parity `9bbf470`; in-loop verification checkpoint `d7e36b1` mitigate but do not close. |
| 2 | Unavailable sandbox enforcement must deny by default; private temp | **PARTIAL** | `sandbox.py:452-480` falls back to plain `subprocess.run`; it refuses only when `_enforcement_required()` (`sandbox.py:515-524`) is true (env `SANDBOX_REQUIRE_ENFORCEMENT` **or** autonomous mode). Interactive default still fails open. bwrap still bind-mounts host `/tmp` (`sandbox.py:311-317`). | — |
| 3 | Protected paths + secret detection + universal redaction | **OPEN** | No redaction exists anywhere in `src/` (`rg redact src` → 0 hits). `tool_execution_pipeline.py` has no secret scan. `WorkspaceGuard` protects paths only for `write_file`/`delete_file` (`_file_io.py:176-177,472-473`), not reads, prompts, traces, or events. | — |
| 4 | Server workspace roots + bounded admission + cancellation-before-start | **FIXED** | `task_endpoints.py` now enforces workspace roots via `validate_working_dir_within_roots` against `get_allowed_workspace_roots()` (defaulting to cwd if unspecified), rejecting unauthorized directories (e.g. `/etc`, home, sibling dirs) with HTTP 403; bounds accepted queue with `_TASK_MAX_QUEUE` (HTTP 503 on overflow); checks `cancel_event` before worker start to prevent orchestrator creation/execution; and propagates `session_id`/`task_id` to orchestrator state and session manager. Covered by `tests/unit/test_task_server_safeguards.py` and `tests/unit/test_server_app.py`. | this change |
| 5 | Redirect-hop / peer-address SSRF validation | **FIXED** | `web_tools.py` now fetches through `_fetch_with_validated_redirects`: `allow_redirects=False`, and every hop is validated with `_is_ssrf_blocked` (scheme + resolved peer addresses) before being followed, with a bounded redirect count. Tests: `tests/unit/test_web_tools_ssrf.py` (`..._blocks_redirect_to_loopback`, `..._follows_validated_redirect`, `..._too_many_redirects`, `..._blocks_redirect_scheme_change`). | this change |
| 6a | SQLite session deletion correctness | **FIXED** | `delete_session` in `sqlite_session_store.py` now cascades correctly: snapshot rows deleted by snapshot ID before snapshots, `session_children` deleted by parent/child ID, and session tables (`messages`, `tool_calls`, `errors`, `plans`, `decisions`, `mistakes`, `session_plans`, `messages_fts`) cleaned up. Covered by `tests/unit/test_sqlite_session_delete.py`. | this change |
| 6b | LSP prompt context for real `SymbolGraph` data | **FIXED** | `lsp_context.py:_get_symbols` now iterates `symbols["classes"]` and `symbols["functions"]` as lists (handling None/non-list shapes) and extracts `{name, kind, file, line}`. New `tests/unit/test_lsp_context.py` covers AST-based lists, sorting, and bad shapes. | this change |
| 6c | Incremental index refresh | **FIXED** | `repo_indexer.refresh_file_in_index` rebuilds top-level `symbols` (`:656-665`) and updates `files_list` with normalized `{"path", "language", "imports"}` dicts (`:643-655`) and metadata hashes. Covered by `tests/unit/test_repo_indexer_schema.py` and `tests/unit/test_repo_indexer*.py`. | this change |

### Section 4 — High-Risk Safety (High rows)

| Finding | Status | Current evidence |
| ------- | ------ | ---------------- |
| Sandbox falls back to unrestricted host execution | **PARTIAL** | Same as Phase 1 item 2. |
| Linux sandbox shares writable host `/tmp` | **OPEN** | `sandbox.py:311-317` binds `/tmp` into the namespace. |
| Redirects bypass SSRF validation | **FIXED** | Same as Phase 1 item 5. |
| Server tasks may select arbitrary host directories | **FIXED** | Same as Phase 1 item 4. |
| Raw `ToolRegistry.call()` bypasses central enforcement | **OPEN** | `_registry.py:305-316` unchanged. The guarded entry point remains `Orchestrator.execute_tool()` / `tool_execution_pipeline.py`; no runtime restriction prevents direct calls. |
| Secret-bearing content can enter prompts, sessions, and logs | **OPEN** | Same as Phase 1 item 3. |
| Prior-session memory inserted without the safety scanner | **FIXED** | `context_builder.py:553-568` scans retrieved prior-session memory and frozen memory snapshots with `scan_memory_content` from `memory/security.py`, omitting threat/injection patterns, and labels `<prior_context>` as untrusted reference data. Covered by `tests/unit/test_prior_session_memory_scan.py`. |
| `rename_file` can overwrite a destination | **FIXED** | `_file_io.rename_file` now rejects the move when the destination already exists (`destination exists: ...` error), enforces `WorkspaceGuard` on both paths, applies project `deny_write`, and `tool_execution_pipeline` snapshots both paths. Covered by `tests/unit/test_rename_file_safeguards.py`. |
| Fallback file locking can evict a live holder | **FIXED** | `file_lock.py:244-258` never reclaims when `os.kill(pid, 0)` confirms owner is alive; `FileLock` provides explicit `acquire()` / `release()`. Covered by `tests/unit/test_todo_lock_live_holder.py`. |
| Parallel delegation re-enters one `Context` concurrently | **FIXED** | `subagent_tools.py:1036-1046` copies `contextvars.Context` per worker subtask execution in `ThreadPoolExecutor`. Covered by `tests/unit/test_parallel_delegation_context.py`. |

### Additional findings already resolved in the current tree

| Audit reference | Status | Evidence / commit |
| --------------- | ------ | ----------------- |
| §11 `codingagent mcp add` writes `.agent/config.json` | **FIXED** | `cli/mcp_cmd.py` now writes through `agent_context_path(workdir)` (`.codingAgent`); round-trip covered by `tests/unit/test_cli_subcommands.py::test_mcp_add_roundtrips_through_loader`. Commit `0c67999`. |
| §8 incremental index symbol staleness | **FIXED** | `repo_indexer.refresh_file_in_index` rebuilds top-level `symbols` (`:651-665`). Commit `976813a`. |
| §13 dormant skills island (`SkillLoader`/`SkillRegistry`), `symbol_reader`, `role_tools` | **REMOVED** | Deleted with ~1,000 lines of unreachable production code and their tests. Commits `51660eb`, `6dcd1a9`, `62e9480`. |
| §7 `symbol_reader.read_file_chunked` external-path read | **N/A** | Module deleted (see above). |
| §3 lite/small paths skip verification | **FIXED** (partial) | Lite tier now shares verification (commit `9bbf470`). |
| §11 config reload / `.agent` vs `.codingAgent` reconciliation | **PARTIAL** | Workspace config chain consolidated on `.codingAgent` (commit `0c67999`); `ConfigReloader` lifecycle issues in §11 are not addressed. |
| §6 provider fallback circuit breaker + candidate model | **FIXED** | `llm_manager.py:1410-1440` routes open circuit breaker into cross-provider fallback; `provider_fallback.py:120-230` resolves candidate-supported model ID. Covered by `tests/unit/test_circuit_breaker_fallback.py`. |
| §7 tool schema strictness (`additionalProperties: false`) | **FIXED** | `_tool.py:114-233` generates `additionalProperties: False` in function calling schema unless `**kwargs` is declared, and `validate_args` flags unexpected parameters. Covered by `tests/unit/test_tool_schema_strictness.py`. |

### Method note

Statuses were established by direct source inspection of the files and line
ranges listed above on the current tree, supplemented by `git log` of the
remediation branch. No assertion above relies on the deleted historical
documentation. Where a finding was not re-verified (e.g. schema
`additionalProperties`), it is marked "not reassessed" rather than presumed
fixed.

## Appendix: TUI Defects

- Spawn permission is bridged but never rendered/resolved: `tui/tui_src/ui/_bridge_tools.py:222-232`. Add Allow/Deny handler and test event publication.
- Diff preview drops `requires_confirmation`: `_bridge_tools.py:150-159`; `_bridge_context.py:167-176`. Preserve it and render actions only for gated previews.
- `/share` mounts a feedback widget twice without clipboard support: `_app_slash_commands_mixin.py:754-774`. Mount exactly once and test it.

## Appendix: Tool And Graph Simplification Plan

### Design Decision

Reduce the model-facing tool surface to **27 capability-oriented tools**. Preserve legacy names only as compatibility adapters for saved sessions, plugins, scripts, and MCP callers; do not expose those aliases or adapters in model schemas.

Replace the `lite` and `capable` graph split with **one functional graph topology** for 15-30B and frontier models. Tier determines immutable budgets and allowed capabilities, not topology. This eliminates the present path where medium and frontier models share a frontier loop that bypasses plan state, while the reduced path has no verification.

This follows current harness practice: use a small number of distinct, namespaced tools; enforce strict schemas and permission contracts; return bounded high-signal results; maintain explicit run context outside the model prompt; keep a human approval boundary for sensitive actions; and use evidence-based evaluations to decide further tool splits. MCP requires validation, access control, output sanitization, timeouts, auditability, and human confirmation for sensitive actions.

### Current Defects Driving This Change

- The current registry intends roughly 72 canonical tools and the coding toolset lists 69 entries, including overlapping file reads, edits, patches, verification commands, Git helpers, and aliases: `src/config/toolsets/coding.yaml:3-73`.
- Registry discovery logs and omits tool-module import failures, producing an incomplete live capability set rather than failing startup: `src/tools/_registry.py:246-263`.
- LSP tools appear in YAML but their custom registration function is not called by the default registry: `src/tools/lsp_tools.py:476-491`.
- `frontier_loop_node` executes tools but returns only the final raw result and count. It does not provide `last_tool_name`, affected-file, or plan-step updates used by verification and recovery: `src/core/orchestration/graph/nodes/frontier_loop_node.py:656-763,1018-1035`.
- `verification_node` decides whether to validate mainly from `last_tool_name` and result shape, so a successful frontier-loop write can be treated as no-op/read-only work: `src/core/orchestration/graph/nodes/verification_node.py:127-151`.
- A frontier response without a tool call sets `ok=True` and completion flags even for a failed, incomplete, or accidental text response: `frontier_loop_node.py:938-952`.
- Oversize-patch recovery routes to `replan_node`, but that node requires `current_plan`; the capable graph deliberately omits planning: `src/core/orchestration/graph/builder.py:538-573`; `replan_node.py:30-52`.
- Tier selection occurs before graph invocation, then perception resolves tier again inside graph state. Graph topology and runtime tier may disagree: `src/core/orchestration/graph/builder.py:688-756`; `src/core/orchestration/graph/nodes/perception_runtime.py:279-308`.

### Target Model-Facing Tools

Each target tool has one distinct user intent, a strict `operation` enum where it consolidates related behavior, bounded output controls, a declared output schema, and operation-level permission metadata. Broad tools do not weaken controls: deletion, shell, delegated work, network access, and irreversible actions remain separate approved operations.

| Tool            | Replaces or consolidates                                           | Required controls                                                                                       |
| --------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `files_read`    | `read_file`, `read_file_chunk`, `read_file_bytes`, `tail_log_file` | Workspace/protected-path checks; positive range/byte limits; secret redaction.                          |
| `files_list`    | `list_files`, `glob`, `find_files`                                 | Workspace scope; pagination; ignored-path summary.                                                      |
| `files_search`  | `grep`, `search_code`                                              | Pattern limits; pagination; contextual matching lines.                                                  |
| `code_search`   | repository text/symbol cross-search helpers                        | Query, path, language, and result-limit filters.                                                        |
| `symbols`       | `find_symbol`, `find_references`, `ast_list_symbols`               | `operation` enum; index freshness indicator; bounded references.                                        |
| `repo_inspect`  | `repo_overview`, summaries, analysis, tech fingerprint             | Summary/detail modes; no raw unbounded source dump.                                                     |
| `files_write`   | `write_file`, directory creation                                   | Atomic write; explicit create/overwrite mode; prior-read proof for overwrite.                           |
| `files_edit`    | edit variants, line-range edit, multiedit                          | Atomic multi-edit transaction; expected-content/context hash; affected-file result.                     |
| `files_move`    | `rename_file`                                                      | Destination-must-not-exist default; snapshot source and destination; approval for overwrite.            |
| `files_delete`  | `delete_file`                                                      | Explicit approval; snapshot; protected-path and workspace checks.                                       |
| `patch`         | `generate_patch`, `apply_patch`, `edit_code_block`                 | Separate preview/apply operations; unified diff validation; atomic apply.                               |
| `shell`         | `bash`, `bash_readonly`                                            | Strict command schema; sandbox required; readonly/write operation; timeout; process-group cancellation. |
| `tasks_wait`    | `check_background_task`                                            | Only agent-owned task IDs; bounded polling.                                                             |
| `verify`        | test/lint/syntax/JS/TS/ESLint tools                                | `operation` enum; target paths; bounded output; normalized pass/fail evidence.                          |
| `format`        | formatter/lint auto-fix paths                                      | Explicit files; diff preview; write approval.                                                           |
| `git`           | status/log/diff/commit/stash/restore helpers                       | `operation` enum; mutation approval; never expose credentials.                                          |
| `lsp`           | diagnostics/definition/references/symbols/hover/rename             | Register through normal registry; rename routes through edit transaction.                               |
| `web_search`    | existing search                                                    | Query limit and source metadata.                                                                        |
| `web_read`      | `read_web_page`                                                    | Redirect-hop SSRF validation; output truncation; content treated untrusted.                             |
| `memory`        | search/save/mistake recording                                      | Provenance; redaction; injection scanning.                                                              |
| `state`         | checkpoints, snapshots, diff, revert                               | Operation-level confirmation; explicit reversibility result.                                            |
| `todo`          | `manage_todo`                                                      | Project-local bounded state and conflict-safe writes.                                                   |
| `skills`        | list/load skill                                                    | Trust/provenance indicator and bounded content.                                                         |
| `user_interact` | ask/send-user-message                                              | One consistent question/notification contract.                                                          |
| `delegate`      | single/parallel delegation and roles                               | Explicit role, scope, budgets, independent context per worker, approval.                                |
| `plan`          | enter/exit/submit plan review                                      | Structured plan with scoped files, success criteria, and approval status.                               |
| `batch`         | existing batch                                                     | Only independent read-only actions in parallel; mutations remain serialized.                            |

### Tier Capability Policy

The topology is identical for both tiers. Policies are resolved once before graph compilation and stored in state; they cannot be reclassified mid-run.

| Policy                                    |                               15-30B medium |                                                                   Frontier |
| ----------------------------------------- | ------------------------------------------: | -------------------------------------------------------------------------: |
| Model-facing tools                        |                               18 core tools |                                                               All 27 tools |
| Internal model turns per agent-loop visit |                                           6 |                                                                         12 |
| Total tool calls                          |                                          20 |                                                                         40 |
| Recovery attempts                         |                                           2 |                                                                          3 |
| Compaction attempts                       |                                           1 |                                                                          2 |
| Read-only parallelism                     | Disabled initially; enable after evaluation |                                                     Independent reads only |
| Mutating tool execution                   |                                  Serialized |                                                                 Serialized |
| Verification after modifications          |             Targeted tests plus syntax/lint |               Targeted tests plus configured full validation at completion |
| Preflight/repository inspection           |               Required before modifications | Required before modifications; optional bounded research for complex tasks |

The medium core should include `files_read`, `files_list`, `files_search`, `code_search`, `symbols`, `repo_inspect`, `files_write`, `files_edit`, `patch`, `shell`, `verify`, `format`, `git`, `state`, `todo`, `plan`, `user_interact`, and `tasks_wait`. `files_delete`, `files_move`, `lsp`, web, memory, skills, delegation, and batch are frontier-only until evaluation shows reliable medium behavior. This is a safety and choice-reduction policy, not a claim that 15-30B models cannot execute those operations.

### Target Graph

```text
START -> initialize -> agent_loop

agent_loop -> agent_loop     outcome=continue
agent_loop -> approval       outcome=approval_required
agent_loop -> verify         outcome=verify
agent_loop -> recover        outcome=recover
agent_loop -> compact        outcome=compact
agent_loop -> finalize       outcome=complete|failed|cancelled

approval -> agent_loop       approved
approval -> finalize         rejected|cancelled

verify -> finalize           passed
verify -> recover            failed and recovery budget remains
verify -> finalize           failed and budget exhausted

recover -> agent_loop        replacement action or mini-plan available
recover -> finalize          unrecoverable

compact -> agent_loop        compacted
compact -> finalize          repeat overflow or compact failure

finalize -> END
```

**`initialize`** resolves provider, model, tier, capability set, sandbox/permission context, and immutable budgets. It performs no LLM call.

**`agent_loop`** is the only LLM-and-tool node. It may execute a bounded batch, but records every action using the same normalized result envelope. It must classify a no-tool model response as `answer`, `clarification`, `refusal`, or `incomplete`; only a valid informational answer may become `complete` without verification.

**`approval`** renders one pending action or structured mini-plan. It cannot approve an empty plan, stale action, or action that has changed after display.

**`verify`** runs when modifications or declared side effects exist, using explicit `modified_files` and side-effect metadata rather than inferring from the last tool name. It records commands, exit codes, target files, and pass/fail evidence.

**`recover`** replaces the legacy replan/debug split. It receives the failed action, error classification, verification evidence, and a normalized action signature. It creates one replacement action or bounded mini-plan, and rejects duplicate failed signatures.

**`compact`** performs deterministic-first history reduction. LLM distillation is optional and cannot be the only escape from context overflow.

**`finalize`** writes session/memory output, emits terminal events, and returns the user result. It never re-enters perception or agent loop.

### Target State Contract

```text
runtime: {
  tier: "medium" | "frontier",
  provider: str,
  model: str,
  capabilities: set[str],
  policy: { turn_limit, tool_limit, recovery_limit, compaction_limit,
            verification_mode }
}
run: {
  status: "running" | "awaiting_approval" | "verifying" | "recovering" |
          "completed" | "failed" | "cancelled",
  outcome: "continue" | "approval_required" | "verify" | "recover" |
           "compact" | "complete" | "failed" | "cancelled",
  turns: int,
  tool_calls: int,
  recoveries: int,
  compactions: int
}
pending_action: { name, arguments, summary, digest } | null
last_action: { name, arguments, result, side_effects, modified_files, digest } | null
modified_files: list[str]
verification: { required, passed, commands, evidence } | null
```

Retire graph-control use of `current_plan`, `current_step`, `next_action`, `last_tool_name`, `action_failed`, `replan_required`, and duplicate recovery counters. Keep adapters only while sessions/checkpoints using those fields are migrated.

### Required Invariants

1. Tier and policy are resolved exactly once for an invocation.
2. Every tool action is validated, authorized, executed through the central pipeline, and stored as `last_action`.
3. Every mutating action records canonical workspace-relative `modified_files` and side effects.
4. A mutating task cannot finish as successful until required verification passes.
5. No-tool output is not equivalent to successful completion.
6. Counters are monotonic, bounded, persisted in checkpoints, and cannot be reset by routing.
7. Approval binds to an action digest; changed arguments require a new approval.
8. Mutations and external side effects are serialized; only proven-independent reads may execute in parallel.
9. Every node output validates against a strict schema before its route is evaluated.
10. All terminal outcomes are explicit: completed, failed, cancelled, or awaiting clarification.

### Migration Steps

1. **Stabilize the current registry.** Make built-in import failure fail startup, repair the `kw_only` dependency incompatibility, register or remove LSP declarations, and make toolset resolution fail closed when any configured name is absent.
2. **Create capability metadata.** Define a single capability registry with strict input/output schemas, operation enum, permissions, workspace rules, sandbox requirement, rollback strategy, output budget, and audit classification. Remove policy duplication between constants, tool configuration, and execution-service code.
3. **Build compatibility adapters.** Map legacy names and aliases to the new capability/operation contract before policy checks. Existing MCP callers and persisted sessions continue to function, but schemas sent to models list only the 27 targets.
4. **Migrate tool implementations.** Move duplicate reads, edits, verification helpers, and Git helpers behind target capability dispatchers. Ensure `files_move`, `files_delete`, `shell`, `state`, and `delegate` preserve their stricter approval semantics.
5. **Replace YAML role lists.** Remove broad role-specific lists and define explicit `medium-core` and `frontier-full` capability manifests. Selection must never silently fall back to the full registry.
6. **Introduce the new graph state and policy.** Resolve tier before invocation in `inference_loop.py`, place immutable policy in initial state, and compile one canonical graph. Eliminate post-start tier reclassification as a graph-control input.
7. **Refactor frontier loop.** Reuse prompt construction and tool parsing from `frontier_loop_node.py`, but rename/refactor it to `agent_loop` and emit the target action contract after each batch. Remove internal completion inference based only on absent tool calls.
8. **Replace replan/debug behavior.** Implement one recovery node driven by failed action evidence. Remove frontier mappings that redirect planning, step-controller, or replan targets to unrelated nodes.
9. **Make verification evidence-based.** Replace dependence on `last_tool_name` in `verification_node.py` with `last_action.side_effects` and `modified_files`; require verification for every mutation regardless of tier.
10. **Delete old topology after checkpoint migration.** Provide one release of read-compatible legacy state adapters, migrate persisted checkpoints, then delete lite/capable graph builders, legacy graph routing, stale YAML names, and unused duplicate policy services.

### Acceptance Test Matrix

| Area            | Required test                                                                                                                                    |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Registry        | Exact 27 model-facing tool names; startup fails on built-in import failure; every target has strict input/output schemas.                        |
| Compatibility   | Each legacy name maps to one capability operation and retains equal-or-stricter authorization.                                                   |
| Medium policy   | 15B, 26B, and 30B models receive exactly medium-core tools and bounded budgets.                                                                  |
| Frontier policy | Frontier model receives all 27 tools and frontier budgets, but identical graph topology.                                                         |
| Tool contract   | Every action updates `last_action`, counters, side effects, and canonical modified files.                                                        |
| Completion      | Text-only answer, refusal, clarification, and incomplete response route to distinct outcomes.                                                    |
| Approval        | Proposed write is non-empty, digest-bound, executes once after approval, and never after rejection.                                              |
| Verification    | Write, patch, move, delete, shell-write, and formatter-write all trigger targeted verification.                                                  |
| Recovery        | Oversized patch and failing verification produce bounded replacement actions without requiring legacy plans.                                     |
| Safety          | All destructive suboperations preserve workspace guard, read-before-write, rollback, sandbox, and audit coverage.                                |
| Checkpoint      | Resume from approval, verification, recovery, and compaction preserves immutable policy and counters.                                            |
| Evaluation      | Held-out coding tasks track task success, verification pass rate, unsafe-action denial, tool error rate, calls/task, tokens/task, and wall time. |

### Rollout Gates

- Do not retire any legacy tool name until compatibility tests and persisted-session replay pass.
- Do not enable medium-model mutations until medium evaluation confirms successful verification on held-out edit tasks.
- Do not enable frontier read parallelism until action ordering, state merging, and output budgeting are tested under concurrent calls.
- Do not expose the HTTP server beyond a trusted local boundary until the existing audit's server workspace, queue, cancellation, and SSE findings are fixed.
