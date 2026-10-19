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

**Remediation & resolution note (updated 2026-10-19):** following the 2026-10-17 audit that identified 20 comprehensive remediation specifications (`SPEC-01` through `SPEC-20`), all 20 specifications have been systematically implemented, regression-tested, and verified against the live codebase. Every critical safety defect, sandbox probe flaw, workspace escape, completion contract loophole, data-loss rollback issue, server tenancy leak, and TUI/provider bug has been resolved with 100% green test suite gates (`pytest`, `ruff`, `mypy`). Sections **§14** and **§15** below reflect the verified resolution status and test coverage for each specification.

## 2. Architecture Strengths

- `src/main.py` separates CLI dispatch from the Textual UI; the runtime UI communicates with core services through bridge/event abstractions.
- Model-originated tool calls normally pass through `tool_execution_pipeline.py`, which composes normalization, permission checks, workspace scope, read-before-write, approval, contracts, snapshots, hooks, timeouts, events, and traces.
- Patch/edit tooling has atomic write and stale-context protections, while loop, round, token, cancellation, and checkpoint controls provide a base reliability envelope.
- JSONL/SQLite session persistence, compaction, graph checkpoints, provider fallback, LSP/index abstractions, telemetry, and scenario/evaluation utilities are present.
- Current tests cover many individual safeguards, including shell-pattern bypasses, tool contracts, routing, rollback, SSRF pre-checks, sessions, and UI bridging.

## 3. Critical Architectural Flaws

| Severity | Finding | Evidence | Trigger and impact | Fix and regression test |
| :--- | :--- | :--- | :--- | :--- |
| High | Live non-lite runs bypass persisted planning and step control. | `src/core/orchestration/graph/builder.py:451-616`; `tier_graph_routing.py:151-167` | Medium, large, and frontier models use the frontier loop, omitting plan validation and step control. Writes are not tied to persisted, scoped steps. | Route mutations through the full graph or add equivalent mandatory plan state. Test frontier writes for persisted plan, scoped action, and post-step verification. |
| High | Completion contract passes mutating tasks after any read-only tool (`last_ok is True`), uses stale `state["last_result"]` in `frontier_loop_node`, and poisons `state["errors"]` on retry. | `completion_contract.py:169-177`; `frontier_loop_node.py:599,608-620,782-791`; `frontier_completion.py:249-258`; `evaluation_node.py:148-167` | Calling `read_file` once satisfies `TaskType.MUTATING`; failed tools in turn 2 are overwritten by turn 3 no-tool completion; `<analyst_findings>`/`repo_context` pollutes `classify_task_type(task)`; contract rejection strings remain in `state["errors"]` and block subsequent evaluation. | Require `mutating_tool_calls_count > 0` or `has_modified_files`; pass live loop `last_result`/`errors` and unpolluted user `task`; clear transient contract errors on retry ([SPEC-04](#spec-04-high--fix-evidence-based-completion-contracts-frontier-loop-stale-last_result-lite-verification-bypass-and-error-poisoning)). |
| High | `lite` tier skips verification when a read tool follows a write, and completes even when verification fails. | `tier_graph_routing.py:169-190`; `builder.py:671-678`; `verification_node.py:365-367` | `frontier_loop_node` never sets `modified_files` in state or starts `_step_snapshot_id`; `lite` routes `verification -> memory_sync -> END` unconditionally. | Return `modified_files` from `frontier_loop_node`, start step transactions before writes, and route failed `lite` verification to recovery/failure ([SPEC-04](#spec-04-high--fix-evidence-based-completion-contracts-frontier-loop-stale-last_result-lite-verification-bypass-and-error-poisoning), [SPEC-08](#spec-08-high--fix-mutating-tool-guardrails-read-before-write-cwd-bug-permissiondry-run-tables-and-directorystep-rollback)). |
| High | Strict state schema validation defaults to `False` and crashes on `frontier_loop_node` (`unknown output key 'retrieved_snippets'`). | `graph/builder.py:103-105`; `frontier_loop_node.py:802-810,895-896`; `state_schemas.py:246-266`; `state.py:260-480` | Enabling `CODINGAGENT_STATE_SCHEMAS_STRICT=1` raises `NodeResultViolation` when auto-retrieval runs; in default mode LangGraph silently drops `retrieved_snippets` and `_frontier_loop_turns`. | Add `retrieved_snippets`, `_frontier_loop_turns`, and `modified_files` to `AgentState` and `_NODE_OUTPUT_SCHEMAS["frontier_loop"]`; enable strict mode by default ([SPEC-05](#spec-05-high--fix-strict-state-schema-crash-on-retrieved_snippets-and-declare-missing-agentstate-fields)). |
| Medium | Oversize-patch recovery in `_compile_frontier_graph` routes to `replan_node` with an empty `current_plan`. | `graph/builder.py:534-554`; `replan_node.py:44-52` | Frontier graph omits `planning_node`, so `replan_node` resets `replan_attempts=0` and appends `"current_plan is empty"` to `state["errors"]`. | Handle oversize-patch recovery inside `frontier_loop_node` or a plan-independent recovery node ([SPEC-04](#spec-04-high--fix-evidence-based-completion-contracts-frontier-loop-stale-last_result-lite-verification-bypass-and-error-poisoning)). |
| Medium | Workflow configuration is not the runtime graph authority. | `src/core/inference/workflow_selector.py:39-130` | `WorkflowConfig`, `LoopControl`, and several declared limits have no observed graph-construction consumer. | Use one configuration source to compile runtime graph behavior. Test each profile changes actual graph limits/capabilities. |
| Medium | Deferred plugin startup is not integrated. | `src/core/orchestration/deferred_init.py:68-209` | Trusted plugin/MCP discovery has no live orchestration caller. | Wire startup with trust controls or remove unsupported configuration. Test trusted/untrusted startup end-to-end. |

## 4. High-Risk Safety Issues

| Severity | Finding | Evidence | Trigger and impact | Fix and regression test |
| :--- | :--- | :--- | :--- | :--- |
| **Critical** | `workdir` and `working_dir` tool argument injection bypasses workspace confinement across almost all tools. | `src/tools/_tool.py:237`; `tool_execution_pipeline.py:388-399,690-698`; `tool_preflight.py:120-149`; `lsp_tools.py:126-419`; `subagent_tools.py:863,987`; `memory_tools.py:12-45` | `_tool.py:237` exempts `"workdir"` from undeclared-field rejection and `tool_execution_pipeline.py:690` preserves caller-supplied `"workdir"`, allowing `read_file(path="passwd", workdir="/etc")` or `write_file(path="x", content="y", workdir="/tmp")`. | Reject `"workdir"`/`"working_dir"` in model schemas, unconditionally bind or confine them to `orch.working_dir` for all tools ([SPEC-01](#spec-01-critical--lock-down-workdir-and-working_dir-argument-injection-across-all-tools)). |
| **Critical** | macOS `sandbox-exec` enforcement probe always returns `True` (false positive), runtime SBPL profile is invalid + `(allow default)`, and verification/lint/git tools bypass `run_sandboxed`. | `src/tools/sandbox.py:116-141,184-225,438-459`; `src/tools/_bash_exec.py:765-789,960-962`; `verification_tools.py:88-948` | `(not ...)` in `_probe_sandbox_exec_enforced` causes exit code `65` before `sh` runs, so canary file is never created and probe returns `True`; runtime profile also exits `65` and lacks `(deny file-write*)`; `run_tests`/`run_js_tests`/`run_linter` run raw `subprocess.run`. | Use `(require-not ...)` and verify positive write in probe; add `(deny file-write*)` + private temp dir in SBPL; route all tool subprocesses through `run_sandboxed` ([SPEC-02](#spec-02-critical--repair-macos-sandbox-exec-sbpl-grammar-false-positive-probe-and-unsandboxed-subprocess-tools)). |
| **Critical** | Untrusted workspace `.codingAgent/config.json` RCE via MCP `stdio` server, unconsented `npx -y @upstash/context7-mcp` execution, and full `os.environ` secret leakage to MCP subprocesses. | `src/core/config_loader.py:253-295`; `src/core/mcp/manager.py:177-265`; `src/core/mcp/mcp_client.py:143`; `src/tools/tools_config.py:46` | Empty config auto-runs `npx -y @upstash/context7-mcp`; workspace `.codingAgent/config.json` spawns arbitrary `stdio` commands with `os.environ.copy()`; MCP tools default to auto-approved `WORKSPACE_WRITE`. | Remove `context7` fallback, require explicit trust for workspace `stdio` MCP servers, scrub subprocess `env`, and default MCP tools to `PROMPT` ([SPEC-03](#spec-03-critical--eliminate-untrusted-workspace-mcp-stdio-rce-unconsented-context7-npx-execution-and-env-leakage)). |
| High | Protected-path and secret-redaction bypasses in `batched_file_read`, `generate_patch`, native `grep`, `read_file_bytes` (base64), `bash_readonly("xxd")`, `SessionStore` wrapper, and `distiller.py`. | `state_tools.py:260-301`; `patch_tools.py:45-70`; `system_tools.py:69-98`; `_file_io.py:915-966`; `session_store.py:345-424,564-582`; `distiller.py:487-635` | `batched_file_read([".env"])`, `generate_patch(".env", "")`, and `grep(".", ".")` read secret files; base64/hex outputs evade regex redaction; `SessionStore` overrides bypass SQLite/JSONL decision sanitization. | Enforce `is_secret_path` and `redact_secrets` across all read/diff/grep/memory paths; block raw byte/hex reads on secret content; delegate `SessionStore` decision methods ([SPEC-06](#spec-06-high--close-protected-path-and-secret-redaction-bypasses-in-read-diff-grep-encoding-sessionstore-and-distiller)). |
| High | `WorkspaceGuard` dot-segment (`sub/../.git`) & symlink bypasses, and `delete_file(path=".")` deletes the entire workspace root. | `workspace_guard.py:59-84`; `src/tools/_file_io.py:509-578`; `src/tools/_edit_tools.py:265-681` | `is_protected("sub/../.git")` returns `False` due to missing `..` normalization; mutating tools check `WorkspaceGuard` before symlink resolution; `delete_file(".")` runs `shutil.rmtree(workdir)`. | Normalize `..` and check both lexical and resolved paths in `WorkspaceGuard`; reject `p == workdir` and enforce `_check_project_deny_write` in `delete_file` ([SPEC-07](#spec-07-high--fix-workspaceguard-dot-segment-and-symlink-bypasses-and-block-delete_filepath-workspace-wipe)). |
| High | SSRF DNS rebinding (TOCTOU), unwrapped IPv6 transition addresses (`::ffff:169.254.169.254`), HTTPS-to-HTTP redirect downgrade, and unbounded body download. | `src/tools/web_tools.py:42-81,139,291-304,343` | `_is_ssrf_blocked` resolves DNS once, then `requests.get` resolves DNS a second time without checking the connected socket peer IP; `IPv6Address.ipv4_mapped` is not unwrapped. | Unwrap `ipv4_mapped`/`sixtofour`/`teredo`, pin resolved IP and verify connected socket `getpeername()`, block HTTPS->HTTP downgrade, and stream bounded bytes ([SPEC-10](#spec-10-high--eliminate-ssrf-dns-rebinding-toctou-unwrap-ipv6-transition-addresses-and-bound-response-streams)). |
| High | Server `POST /task` with `working_dir=None` bypasses `CODINGAGENT_ALLOWED_WORKSPACE_ROOTS`, pre-auth validator leaks path existence, and WebSocket leaks events across sessions. | `src/server/task_endpoints.py:91-94,178-194,206-295`; `websocket_handler.py:50-56`; `sse_adapter.py:61-67` | Omitting `working_dir` executes in server CWD; unauthenticated `POST /task` returns `422` vs `401` oracle; WS broadcasts all `tool.*`/`model.*` events lacking `"session_id"` to every client. | Validate default `working_dir` after auth, attach `session_id` via `ContextVar` in `EventBus.publish`, and enforce strict `session_id` filtering ([SPEC-15](#spec-15-high--fix-server-working_dirnone-bypass-pre-auth-filesystem-oracle-session-history-hydration-eventbus-wiring-and-executor-leaks), [SPEC-16](#spec-16-high--enforce-websocket-and-sse-session-isolation-align-event-names-and-bound-subscriptionsmetrics)). |
| High | Shell Gate 3 bypassed via combined short flags (`python3 -Bc`, `node --eval=`) and `git` in `SAFE_COMMANDS` allows `git -c core.pager=...` and destructive git ops. | `src/tools/_bash_exec.py:426-433,648-680`; `src/tools/_security.py:155,242-276` | Exact token check `part in CODE_EXEC_FLAGS` misses `-Bc`, `-Ic`, `-e...`, `--eval=...`; `git` in `SAFE_COMMANDS` auto-approves `git branch -D` and `-c` config injection. | Inspect combined short flags and `--flag=` prefixes; remove `git` from `SAFE_COMMANDS` and validate git flags/subcommands in both `bash` and `bash_readonly` ([SPEC-09](#spec-09-high--close-shell-gate-3-combined-flag-bypasses-git-allowlist-abuse-and-arbitrary-pid-probing)). |
| High | `batch()` shares one `Context` across 25 worker threads and loses thread-local orchestrator; `delegate_tasks_parallel` races on `parent_orchestrator.active_agent`. | `src/tools/batch_tools.py:36-45,82,118-129`; `src/tools/subagent_tools.py:615,792` | `batch()` fails with `RuntimeError: cannot enter context` or `"requires an active orchestrator context"`; concurrent subagents overwrite `active_agent.allowed_tools`. | Use `ContextVar` for batch orchestrator and `active_agent`, and copy `Context` per submitted batch call ([SPEC-11](#spec-11-high--fix-batch-shared-context-and-thread-local-orchestrator-loss-and-subagent-active_agent-data-race)). |

## 5. Major Missing Capabilities

- Evidence-based completion contracts for informational, read-only, and mutating tasks that cannot be satisfied by read-only tool calls alone.
- A portable default-deny execution isolation model covering all tool subprocesses (`run_tests`, linters, formatters, git) and valid macOS SBPL enforcement.
- Enforced repository retrieval, plan persistence, scoped execution, and postcondition verification with step rollback for every mutation tier.
- Protected-path and content-level secret classification with universal redaction across batch reads, diffs, recursive grep, raw byte reads, and memory sidecars.
- Server tenancy controls: default workspace root enforcement, post-auth validation, multi-turn session hydration, and strict WS/SSE session event isolation.
- Repository intelligence that indexes code chunks, references, dependencies, and test relationships without failing on `SymbolGraph` caller structures.

## 6. Workflow Reliability Issues

| Severity | Finding | Evidence | Trigger and impact | Fix and regression test |
| :--- | :--- | :--- | :--- | :--- |
| High | Secondary provider fallback success calls `record_success()` on the failed primary provider's circuit breaker, and `HALF_OPEN` single-probe guard is unreachable. | `src/core/inference/llm_manager.py:1250-1261,1445-1468` | When secondary fallback succeeds, `_update_circuit_breaker_for_result` resets primary `_cb_key` failure count to `0`; first check in `HALF_OPEN` mutates `_state`, letting all concurrent callers through. | Record primary failure before cross-provider fallback; add atomic `allow_request()` for `HALF_OPEN` single-probe admission ([SPEC-14](#spec-14-high--fix-provider-circuit-breaker-primary-reset-bug-half_open-probe-guard-and-anthropicolllama-streaming)). |
| High | `AnthropicAdapter` and `OllamaAdapter` streaming (`stream=True`) are broken, and `generate()` places `tool_calls` outside `message`. | `anthropic_adapter.py:400-416,503-523`; `ollama_adapter.py:539-575`; `openai_compat_adapter.py:686-695`; `runtime_call.py:180` | Anthropic payload omits `"stream": True` and uses OpenAI SSE parser; Ollama returns a dict generator without `iter_lines`, yielding `choices: []`. | Send `"stream": True`, parse Anthropic SSE events, consume Ollama generator in `runtime_call.py`, and place `tool_calls` in `choices[0].message.tool_calls` ([SPEC-14](#spec-14-high--fix-provider-circuit-breaker-primary-reset-bug-half_open-probe-guard-and-anthropicolllama-streaming)). |
| Medium | Subprocess tools ignore cooperative cancellation on timeout, and `FileLock` blocks indefinitely on `fcntl.flock(LOCK_EX)`. | `tool_execution_pipeline.py:700-775`; `src/tools/file_lock.py:208,251-254` | 4 timed-out subprocess tool calls permanently starve `_tool_executor`; `FileLock` ignores `timeout` on POSIX and evicts live cross-host fallback locks. | Kill subprocess groups on `cancel_ev`, poll `LOCK_EX | LOCK_NB` until `timeout`, and skip `os.kill` when `same_host=False` ([SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli)). |
| Medium | Shutdown hooks run on a 1-worker executor (`max_workers=1`), and P2P cleanup runs outside the timeout budget. | `session_lifecycle.py:150-198` | If hook 1 hangs, all subsequent hooks queue behind the stuck thread and time out without running. | Run each hook and P2P cleanup in an independent daemon thread joined with the remaining timeout ([SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli)). |
| Medium | Forced compaction still calls an LLM first under context pressure (`prefer_deterministic=False`). | `memory_update_node.py:121-130`; `compaction_service.py:164-230` | Context-overflow path invokes LLM summarization before deterministic pruning. | Pass `prefer_deterministic=True` when `force_compact=True` ([SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli)). |

## 7. Tool System Weaknesses

- **`check_read_before_write` resolves relative paths against `Path.cwd()` instead of `workdir`**: `src/tools/guardrails.py:72-82`; `_edit_tools.py:374,675`; `_file_io.py:524,650`. When `workdir != Path.cwd()`, `multiedit` (which is also missing from `WRITE_TOOLS_REQUIRING_READ`) treats existing files in `workdir` as new files and overwrites them without prior read ([SPEC-08](#spec-08-high--fix-mutating-tool-guardrails-read-before-write-cwd-bug-permissiondry-run-tables-and-directorystep-rollback)).
- **`edit_code_block`, `ast_rename`, and `lsp_rename` bypass read-before-write, dry-run, and rollback**: `patch_tools.py:115` and `ast_tools.py:69` call `mark_file_read` immediately before `check_read_before_write`; `lsp_rename` (`lsp_tools.py:338`) writes files directly without `WorkspaceGuard`, `deny_write`, or rollback snapshots ([SPEC-08](#spec-08-high--fix-mutating-tool-guardrails-read-before-write-cwd-bug-permissiondry-run-tables-and-directorystep-rollback)).
- **Directory deletion rollback loses all files inside the directory**: `RollbackManager.snapshot_files` (`rollback_manager.py:109-119`) records only `content="__DIRECTORY__"`, and `rollback()` (`:309-312`) runs `mkdir()` without restoring child files ([SPEC-08](#spec-08-high--fix-mutating-tool-guardrails-read-before-write-cwd-bug-permissiondry-run-tables-and-directorystep-rollback)).
- **Permission table & dry-run gaps**: `tools_config.py:45-91` omits `delegate_tasks_parallel` and `run_js_tests` (auto-allowing them at `WORKSPACE_WRITE`), while omitting 15 read-only tools (`read_file_chunk`, `batched_file_read`, `repo_overview`, `find_files`, `lsp_*`, etc.) so they fail in `READ_ONLY` mode; `DRY_RUN_BLOCKED_TOOLS` (`constants.py:55-57`) omits `multiedit`, `edit_code_block`, `ast_rename`, `lsp_rename`, `format_file`, `create_directory`, `git_stash`, `git_restore`, `run_tests`, `run_js_tests`, and delegation tools ([SPEC-08](#spec-08-high--fix-mutating-tool-guardrails-read-before-write-cwd-bug-permissiondry-run-tables-and-directorystep-rollback)).

## 8. Repository Awareness Gaps

| Severity | Finding | Evidence | Trigger and impact | Fix and regression test |
| :--- | :--- | :--- | :--- | :--- |
| High | `analysis_node` symbol-graph enrichment always crashes with `TypeError` when any caller exists, wiping `call_graph` and `test_map`. | `analysis_node.py:479-513`; `symbol_graph.py:343-377` | `sg.find_calls(sym)` returns `List[Dict]`, and `', '.join(callers)` at line 507 raises `TypeError`, caught at line 510 which resets `call_graph_data = {}` and `test_map_data = {}`. | Format caller dicts (`f"{c['file']}:{c['line']}"`) before joining ([SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects)). |
| High | `analysis_node` never calls `VectorStore.index_code()`, so `vs.search()` always returns `[]`. | `analysis_node.py:354-361`; `vector_store.py:328-334` | `.codingAgent/vectorstore/symbols.json` is never written during indexing, making semantic symbol search a no-op. | Call `vs.index_code(idx.get("symbols", []))` after `index_repository(working_dir)` ([SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects)). |
| High | `refresh_file_in_index` checks `"hashes"` instead of `"file_hashes"`, drops `start_line`/`docstring`, ignores `async def`, and keeps deleted files. | `repo_indexer.py:203,563-603,624-680` | Incremental refresh leaves `.repo_index_meta.json` stale, strips symbol line numbers/docstrings, misses async functions, and retains deleted file symbols. | Use `"file_hashes"`, preserve `start_line`/`end_line`/`docstring`, include `ast.AsyncFunctionDef`, and purge deleted files ([SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects)). |
| High | `McpHttpClient` fails to unwrap JSON-RPC `result` envelopes, and `LSPClient` leaks timed-out processes and omits `textDocument/didOpen`. | `mcp_http_client.py:126-252`; `lsp_client.py:225-266,315-385` | HTTP MCP servers always return `[]` for `list_tools()`; LSP servers deadlock on undrained `stderr` or return empty diagnostics/references without `didOpen`. | Unwrap `resp["result"]` in `McpHttpClient`; drain `stderr`, kill timed-out `LSPClient` processes, and send `didOpen` ([SPEC-19](#spec-19-high--fix-mcp-http-json-rpc-unwrapping-sse-connect-race-stdio-stderrbuffer-limits-and-lspclient-lifecycle)). |
| Medium | `_ensure_repo_index` caches `_INDEXED_WORKDIRS` forever without checking workspace revision. | `repo_context.py:41-61`; `analysis_node.py:172-188` | Frontier loop's index check never re-indexes after first run in process, and `_is_already_indexed` does not verify `repo_index.json` exists on disk. | Track revision digest in `_ensure_repo_index` and check `repo_index.json` existence ([SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects)). |

## 9. Memory System Evaluation

| Severity | Finding | Evidence | Trigger and impact | Fix and regression test |
| :--- | :--- | :--- | :--- | :--- |
| High | SQLite FTS5 `rowid` desynchronizes from `messages.id` after `delete_session` or `revert_session`, breaking `search()`. | `sqlite_store_schema.py:9,77,113-135`; `sqlite_session_store.py:800-830`; `sqlite_store_queries.py:14,26` | `messages.id` uses `AUTOINCREMENT` while `messages_fts` trigger inserts without `rowid = new.id`. After deleting a session, `JOIN messages m ON m.rowid = fts.rowid` returns `0` rows or wrong messages from other sessions; `mistakes_fts` and `decisions.json` are also not cleaned on delete. | Insert explicit `rowid = new.id` in FTS triggers, add `AFTER DELETE` triggers, clean `mistakes_fts`, and refresh `decisions.json` ([SPEC-12](#spec-12-high--fix-sqlite-fts5-rowid-desynchronization-on-session-deleterevert-and-jsonlsessionstore-parity)). |
| High | `SessionStore` wrapper overrides `write_decisions_json` and `read_recent_decisions`, bypassing secret redaction, threat scanning, and provenance. | `src/core/memory/session_store.py:345-424,564-582` | Default `get_session_store()` returns `SessionStore`, whose wrapper methods query SQLite / read `decisions.json` directly without calling `SqliteSessionStore`/`JsonlSessionStore` sanitization. | Delegate `SessionStore.write_decisions_json` and `read_recent_decisions` to `self._store` ([SPEC-06](#spec-06-high--close-protected-path-and-secret-redaction-bypasses-in-read-diff-grep-encoding-sessionstore-and-distiller)). |
| High | Server `_run_task_thread` does not hydrate prior `session_id` messages, overwrites `orch.event_bus` post-bootstrap, and leaks executors. | `src/server/task_endpoints.py:206-295`; `orchestrator_bootstrap.py:52-64` | Every `POST /task` starts with a fresh `[{"role": "user", "content": task}]` list and leaks 3 executor threads because `orch.close()` is never called. | Load session history before `run_agent_once`, pass `event_bus` to `Orchestrator.__init__`, and call `orch.close()` in `finally` ([SPEC-15](#spec-15-high--fix-server-working_dirnone-bypass-pre-auth-filesystem-oracle-session-history-hydration-eventbus-wiring-and-executor-leaks)). |
| Medium | `JsonlSessionStore` lacks `search_mistakes`, leaves sidecars on `delete_session`, and truncates dotted `session_id`s. | `jsonl_session_store.py:461-476,581-591,1105`; `jsonl_store_helpers.py:62-67` | `rsplit(".", 1)[0]` on `path.stem` corrupts any `session_id` containing a `.`; snapshot/state sidecars remain after deletion. | Only strip numeric `.N` rotation suffixes, implement `search_mistakes`, and delete all sidecars ([SPEC-12](#spec-12-high--fix-sqlite-fts5-rowid-desynchronization-on-session-deleterevert-and-jsonlsessionstore-parity)). |

## 10. Evaluation And Testing Gaps

- `tests/conftest.py:15` sets `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1` globally across the test suite, masking the broken macOS `sandbox-exec` SBPL profile (`SPEC-02`).
- `SWEBenchRunner._invoke_swebench_agent` (`src/core/evaluation/swebench.py:415-425`) calls `agent.run_agent_once(system_prompt_name="swebench", messages=..., tools=[])` with an empty tool list (so live SWE-bench runs cannot execute any tools), `run_instance` (`:326-328`) runs `shutil.rmtree(self.workdir / instance.instance_id)` without path-traversal validation, and `grade_runnable` (`:297-298`) misgrades skipped tests under `pytest -x` as passed (`SPEC-20`).
- Three built-in evaluation scenarios (`add_type_hints`, `trace_call_chain`, `ambiguous_format_request` in `src/core/evaluation/scenario_evaluator.py:475,664,794`) check for substrings already present in `setup_files`, passing when the agent is a no-op (`SPEC-20`).
- Live provider checks remain scheduled/manual in `.github/workflows/ci.yml:200-216`.

## 11. Usability Problems

- **TUI `/mcp add` still writes to `.agent/config.json`**: While CLI `codingagent mcp add` was updated to `.codingAgent/config.json`, `tui/tui_src/ui/_app_slash_commands_mixin.py:469-480` still writes to `.agent/config.json`, and `get_mcp_servers()` (`config_loader.py:284-295`) injects a hardcoded `context7` server (`SPEC-03`, `SPEC-18`).
- **Config reload provider re-init is a no-op and `load_merged_config` prematurely clears `_pending_reload`**: `orchestrator_config_reload.py:82-100` calls `_ensure_provider_manager_initialized_sync()` (which returns immediately when `_initialized=True`), and `config_loader.py:165` calls `reloader.clear_changed()` without updating `_cached_config` (`SPEC-17`).
- **TUI role selection (`active_role`) is ignored by tool selection and graph nodes**: `_bridge_agent.py:115` hardcodes `get_tools_for_role("operational")`, `run_agent_once_impl` ignores its `tools` parameter, and `frontier_loop_node.py:263` / `perception_node.py:216` ignore `state["system_prompt"]` and rebuild `"operational"` (`SPEC-18`).
- **`SessionScreen` (`Ctrl+R`, `/fork`, `/revert`) off-by-one index, search input key hijacking, and plaintext API keys in tracked `src/config/providers.json`**: `session_screen.py:145,180` indexes `self._filtered[self._selected]` without subtracting `_offset = 1`, `on_key` intercepts `f`/`r`/`d`/`n`/`j`/`k` while typing in `#sessions_search`, and `config_writer.py:43,161` writes plaintext API keys into git-tracked `src/config/providers.json` (`SPEC-18`).

## 12. Performance Bottlenecks

- **WebSocket dynamic subscriptions and dropped-event metric keys are unbounded**: `websocket_handler.py:190-202` allows unlimited `subscribe` frames for arbitrary event names, and `metrics.py:94-99` stores arbitrary `(session_id, event_name)` keys in `_CLIENT_DROPPED_EVENT_COUNTERS` (`SPEC-16`).
- **Server `_run_task_thread` leaks 3 `ThreadPoolExecutor` threads per task**: `task_endpoints.py:264` creates a new `Orchestrator` per task and never calls `orch.close()` (`SPEC-15`).
- **`KVCacheGovernor` produces negative `max_tokens` when VRAM is `0.0` or over-committed**: `kv_cache_governor.py:99-100,38-45` computes negative `max_tokens` and returns `usage_ratio = 0.0` (`should_compact = False`) while `will_overflow(0)` returns `True` (`SPEC-20`).

## 13. Over-Engineered Or Dormant Components (Audited & Cleaned Up 2026-10-19)

The subsystems identified below have been audited, with confirmed dead code purged and actively integrated components documented to prevent regression:

- **Purged Dead/Dormant Modules & Classes**:
  - `IncrementalIndexer` (`src/core/indexing/symbol_graph.py`): Purged. Live incremental indexing is handled by `repo_indexer.py:refresh_file_in_index` and `repo_context.py:refresh_index_after_write`.
  - `ToolsetManager` (`src/config/toolsets/loader.py`): Purged along with test harness. Role-based tool filtering is handled directly by `get_toolset_for_role` and `load_toolset_for_model`.
  - `Truncate` (`src/tools/_truncate.py`): Purged along with `test_truncate.py`. Canonical truncation is handled by `tool_output_truncation.py` with `RESULT_MAX_CHARS: int = 8_000` in `tool_execution_pipeline.py`.
  - Deprecated `AgentModeSettings`, `_MODE_SETTINGS`, `select_agent_mode`, and `get_agent_mode_settings` (`src/core/inference/model_capability_profile.py`): Purged. Superseded by `TierConfig` and `classify_model` in `model_tiers.py` (`AgentMode` enum retained for event typing).
  - `LoopControl` dataclass & `get_loop_control` (`src/core/inference/workflow_selector.py`): Purged. Graph turns are managed by `frontier_loop_node` and `tier_config`.
  - TUI Dead Modules: `WorkflowController` (`ui/controller.py`), `Coordinator` (`ui/coordinator.py`), and `DiffViewer` (`ui/widgets.py`): Purged across both `tui/src` and `tui/tui_src`, maintaining strict byte-identity.
  - Previously Purged: `ContextManager`, `EventLog`, `WaveCoordinator`, `TelemetryConsumer`, `TelemetryMetrics`, `AdapterWrapper`, `SkillLoader`, `SkillRegistry`, `SystemPromptBuilder`.
- **Verified Active Components (Retained)**:
  - `GitWorktreeManager` (`src/core/orchestration/git_worktree_manager.py`): Retained; actively called by TUI `/worktree` slash command handler (`_app_slash_commands_mixin.py`).
  - `FrozenMemoryStore` (`src/core/memory/frozen_snapshot.py`): Retained; actively called by `src/core/context/context_builder.py:get_memory_for_prompt`.
  - `HookRegistry` (`src/core/plugin/hook_registry.py`): Retained; actively called by context builder, LLM manager, execution, perception, and inference loop nodes.
  - `MetricsStore` & `RollingHistogram` (`src/core/observability/metrics.py`): Retained; actively referenced in `state_schemas.py` and `todo_tools.py`.
  - `ResiliencePolicy` (`src/core/utils/retry.py`): Retained; actively imported and used by `src/core/inference/adapters/ollama_adapter.py`.
  - `HistoryInput` (`tui/tui_src/ui/components/history_input.py`): Retained; actively used by `ChatTextArea` for `HistoryInput.InterruptSignal` (double-Esc interrupt handling).
- **Single Source Tree Consolidation**:
  - The redundant `tui/src` tree was permanently removed, eliminating drift risk. `tui/tui_src` is the single canonical source of truth for all TUI implementations. `tui/__init__.py` provides a transparent dynamic alias loader so legacy imports of `tui.src.*` resolve to `tui.tui_src.*`.

## 14. Prioritized Fix List (Resolved & Reassessed 2026-10-19)

Status is reconciled against the current tree in §15 (with source evidence) and paired with full implementation specifications in §16 (`SPEC-01` through `SPEC-20`).
Status legend: `VERIFIED FIXED` = contract fully met and regression tests passing; `PARTIAL` = primary path mitigated, but high/medium bypasses or edge cases remain; `BROKEN` = runtime failure or complete bypass of the intended control; `REGRESSED` = fix introduced a new defect; `OPEN` = unaddressed.

### Phase 1 — Critical Security & Stability Fixes

| Task | Location | Complexity | Expected impact | Reassessed Status | Spec |
| :--- | :--- | :---: | :--- | :--- | :--- |
| Lock down `workdir` and `working_dir` tool argument injection across all tools and schemas. | `src/tools/_tool.py`, `tool_execution_pipeline.py`, `tool_preflight.py`, `lsp_tools.py`, `subagent_tools.py`, `memory_tools.py` | Low | Prevents arbitrary host file read/write/delete outside workspace. | **VERIFIED FIXED** (`test_workdir_confinement_all_tools.py`) | [SPEC-01](#spec-01-critical--lock-down-workdir-and-working_dir-argument-injection-across-all-tools) |
| Repair macOS `sandbox-exec` SBPL grammar, fix false-positive probe, and route all subprocess tools through `run_sandboxed`. | `sandbox.py`, `_bash_exec.py`, `verification_tools.py`, `formatter.py`, `lint_dispatch.py`, `git_tools.py`, `system_tools.py` | Medium | Restores real sandbox enforcement on macOS and closes raw `subprocess.run` host escapes. | **VERIFIED FIXED** (`test_sandbox_macos_sbpl.py`, `test_tools_sandboxed_subprocesses.py`) | [SPEC-02](#spec-02-critical--repair-macos-sandbox-exec-sbpl-grammar-false-positive-probe-and-unsandboxed-subprocess-tools) |
| Require trust/approval for workspace `.codingAgent/config.json` MCP `stdio` servers, remove hardcoded `context7` auto-execution, and scrub subprocess environment. | `config_loader.py`, `mcp/manager.py`, `mcp/mcp_client.py`, `tools_config.py` | Low | Eliminates untrusted repository RCE, unconsented `npx` execution, and API key leakage to MCP subprocesses. | **VERIFIED FIXED** (`test_mcp_security.py`) | [SPEC-03](#spec-03-critical--eliminate-untrusted-workspace-mcp-stdio-rce-unconsented-context7-npx-execution-and-env-leakage) |
| Fix evidence-based completion contract loopholes, stale `last_result` in `frontier_loop_node`, `_MUTATION_REGEX` context pollution, `lite` tier verification skip, and `state["errors"]` poisoning. | `completion_contract.py`, `frontier_completion.py`, `frontier_loop_node.py`, `tier_graph_routing.py`, `builder.py`, `evaluation_node.py` | Medium | Stops read-only tools from satisfying mutating tasks, detects multi-turn failures, and enforces verification/rollback across all tiers. | **VERIFIED FIXED** (`test_completion_contract.py`, `test_lite_verification_routing.py`) | [SPEC-04](#spec-04-high--fix-evidence-based-completion-contracts-frontier-loop-stale-last_result-lite-verification-bypass-and-error-poisoning) |
| Close protected-path, `..`/symlink, base64/hex encoding, `SessionStore` wrapper, and distiller secret-redaction bypasses; prevent `delete_file(path=".")` workspace root wipe. | `workspace_guard.py`, `state_tools.py`, `patch_tools.py`, `system_tools.py`, `_file_io.py`, `repo_read_tools.py`, `ast_tools.py`, `git_tools.py`, `session_store.py`, `distiller.py` | Medium | Prevents `.git`/workspace deletion and credential exfiltration across all read, diff, grep, and memory sinks. | **VERIFIED FIXED** (`test_secret_redaction_bypasses.py`, `test_workspace_guard_bypasses.py`) | [SPEC-06](#spec-06-high--close-protected-path-and-secret-redaction-bypasses-in-read-diff-grep-encoding-sessionstore-and-distiller), [SPEC-07](#spec-07-high--fix-workspaceguard-dot-segment-and-symlink-bypasses-and-block-delete_filepath-workspace-wipe) |
| Enforce task-server workspace roots when `working_dir=None`, move path validation after auth, hydrate session history, fix split-brain `EventBus`, and close leaked executors. | `server/task_endpoints.py`, `orchestrator_bootstrap.py`, `server/app.py`, `scheduler/worker.py` | Medium | Prevents unauthorized default-workdir execution, pre-auth filesystem oracle, broken multi-turn sessions, and thread leaks. | **VERIFIED FIXED** (`test_task_server_safeguards.py`) | [SPEC-15](#spec-15-high--fix-server-working_dirnone-bypass-pre-auth-filesystem-oracle-session-history-hydration-eventbus-wiring-and-executor-leaks) |
| Eliminate SSRF DNS rebinding (TOCTOU), unwrap IPv6 transition addresses, bound response body reads, and block HTTPS-to-HTTP redirect downgrades. | `web_tools.py` | Medium | Closes DNS-rebinding and IPv6-mapped SSRF bypasses and prevents OOM DoS. | **VERIFIED FIXED** (`test_ssrf_dns_rebinding.py`) | [SPEC-10](#spec-10-high--eliminate-ssrf-dns-rebinding-toctou-unwrap-ipv6-transition-addresses-and-bound-response-streams) |
| Repair SQLite FTS5 `rowid` desynchronization on delete/revert, clean `mistakes_fts` and `decisions.json`, fix `analysis_node` `SymbolGraph` `TypeError`, and index `VectorStore`. | `sqlite_store_schema.py`, `sqlite_session_store.py`, `analysis_node.py`, `repo_indexer.py`, `symbol_graph.py`, `repo_context.py` | Medium | Restores full-text search integrity and makes call-graph, test-map, and vector symbol retrieval functional. | **VERIFIED FIXED** (`test_sqlite_fts_session_delete_revert.py`, `test_analysis_node_symbol_graph_vectorstore.py`) | [SPEC-12](#spec-12-high--fix-sqlite-fts5-rowid-desynchronization-on-session-deleterevert-and-jsonlsessionstore-parity), [SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects) |

### Phase 2 — Robustness & Transactional Integrity Improvements

| Task | Location | Complexity | Expected impact | Reassessed Status | Spec |
| :--- | :--- | :---: | :--- | :--- | :--- |
| Fix `check_read_before_write` `Path.cwd()` resolution bug, complete `WRITE_TOOL_NAMES` / `TOOL_PERMISSIONS` / `DRY_RUN_BLOCKED_TOOLS`, and make directory deletion & step rollback lossless. | `guardrails.py`, `constants.py`, `tools_config.py`, `rollback_manager.py`, `rollback_tools.py`, `tool_execution_pipeline.py`, `patch_tools.py`, `ast_tools.py`, `lsp_tools.py` | High | Enforces read-before-write when `workdir != cwd`, blocks mutations in dry-run, prompts on dangerous tools, and restores deleted directory contents on rollback. | **VERIFIED FIXED** (`test_mutating_tool_guardrails.py`) | [SPEC-08](#spec-08-high--fix-mutating-tool-guardrails-read-before-write-cwd-bug-permissiondry-run-tables-and-directorystep-rollback) |
| Fix `frontier_loop_node` `retrieved_snippets` schema violation, declare missing `AgentState` keys, and enable strict state validation by default. | `state.py`, `state_schemas.py`, `builder.py`, `frontier_loop_node.py` | Low | Prevents strict-mode crash on retrieval and stops LangGraph from silently dropping `retrieved_snippets` and turn counters. | **VERIFIED FIXED** (`test_state_schemas.py`) | [SPEC-05](#spec-05-high--fix-strict-state-schema-crash-on-retrieved_snippets-and-declare-missing-agentstate-fields) |
| Fix circuit breaker primary-reset bug on fallback success, fix `HALF_OPEN` single-probe guard, and implement `AnthropicAdapter` and `OllamaAdapter` streaming. | `llm_manager.py`, `anthropic_adapter.py`, `ollama_adapter.py`, `openai_compat_adapter.py`, `runtime_call.py`, `streaming.py` | Medium | Prevents dead primary providers from resetting failure counts and makes Anthropic/Ollama streaming functional. | **VERIFIED FIXED** (`test_circuit_breaker_fallback.py`, `test_anthropic_adapter_messages.py`) | [SPEC-14](#spec-14-high--fix-provider-circuit-breaker-primary-reset-bug-half_open-probe-guard-and-anthropicolllama-streaming) |
| Fix `batch()` shared `Context` & thread-local orchestrator loss, and eliminate `delegate_tasks_parallel` data race on `parent_orchestrator.active_agent`. | `batch_tools.py`, `subagent_tools.py`, `tool_execution_pipeline.py` | Medium | Makes `batch()` functional and prevents concurrent subagent privilege escalation. | **VERIFIED FIXED** (`test_batch_context_isolation.py`) | [SPEC-11](#spec-11-high--fix-batch-shared-context-and-thread-local-orchestrator-loss-and-subagent-active_agent-data-race) |
| Close shell Gate 3 combined-flag bypasses (`python3 -Bc`, `node --eval=`), remove destructive `git` ops from `SAFE_COMMANDS`, and restrict `check_background_task` to agent-owned PIDs. | `_bash_exec.py`, `_security.py` | Medium | Prevents unapproved inline code execution and destructive git operations via `bash` / `bash_readonly`. | **VERIFIED FIXED** (`test_bash_gate_bypasses.py`) | [SPEC-09](#spec-09-high--close-shell-gate-3-combined-flag-bypasses-git-allowlist-abuse-and-arbitrary-pid-probing) |
| Repair config reload provider re-initialization, fix premature `clear_changed()` in `load_merged_config()`, preserve MCP tools on reload, and unify instruction loaders. | `orchestrator_config_reload.py`, `llm_manager.py`, `config_loader.py`, `config_hot_reload/__init__.py`, `instruction_loader.py`, `instruction_files.py` | Medium | Makes runtime `providers.json` and project config hot-reloading actually take effect. | **VERIFIED FIXED** (`test_phase2_item5_config_reload.py`) | [SPEC-17](#spec-17-high--fix-config-hot-reload-no-op-provider-re-init-premature-clear_changed-and-instruction-loader-drift) |
| Enforce non-blocking `FileLock` timeout with `LOCK_NB`, fix cross-host fallback lock eviction, parallelize shutdown hooks, and terminate timed-out tool subprocesses. | `file_lock.py`, `session_lifecycle.py`, `tool_execution_pipeline.py` | Medium | Honors lock timeouts, prevents single hung hook from starving shutdown, and prevents `_tool_executor` exhaustion. | **VERIFIED FIXED** (`test_spec20_reliability_safety.py`) | [SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli) |

### Phase 3 — Capability, Server Isolation, and TUI Correctness

| Task | Location | Complexity | Expected impact | Reassessed Status | Spec |
| :--- | :--- | :---: | :--- | :--- | :--- |
| Isolate WebSocket and SSE events by `session_id`, align `DEFAULT_SERVER_EVENT_TYPES` with actual published event names, and bound dynamic WS subscriptions & metric keys. | `websocket_handler.py`, `sse_adapter.py`, `event_subscriptions.py`, `metrics.py`, `event_types.py` | Medium | Stops cross-session event leakage in WS, stops silent event drops in SSE, and prevents metrics OOM. | **VERIFIED FIXED** (`test_ws_sse_session_isolation.py`) | [SPEC-16](#spec-16-high--enforce-websocket-and-sse-session-isolation-align-event-names-and-bound-subscriptionsmetrics) |
| Wire TUI `active_role` through `_bridge_agent.py`, `inference_loop.py`, `frontier_loop_node.py`, and `perception_node.py`; fix TUI `/mcp add` path, `SessionScreen` offset/focus, and plaintext key storage. | `_bridge_agent.py`, `inference_loop.py`, `frontier_loop_node.py`, `perception_node.py`, `_app_slash_commands_mixin.py`, `session_screen.py`, `session_list.py`, `config_writer.py` | Medium | Makes TUI role switching, `/mcp add`, and `SessionScreen` fork/revert work as displayed without writing secrets to tracked files. | **VERIFIED FIXED** (`test_phase3_usability_performance.py`) | [SPEC-18](#spec-18-high--wire-tui-active_role-end-to-end-fix-tui-mcp-add-path-fix-sessionscreen-offsetfocus-and-protect-api-keys) |
| Fix `McpHttpClient` JSON-RPC `result` envelope unwrapping, `McpSseClient` connection ordering, `McpStdioClient` `stderr` drain & buffer limits, and `LSPClient` lifecycle (`didOpen`, timeout cleanup). | `mcp_http_client.py`, `mcp_sse_client.py`, `mcp_ws_client.py`, `mcp_client.py`, `lsp_client.py`, `lsp_manager.py` | Medium | Makes HTTP/SSE MCP transports and LSP servers reliable in production. | **VERIFIED FIXED** (`test_mcp_http_client.py`, `test_lsp_client_lifecycle.py`) | [SPEC-19](#spec-19-high--fix-mcp-http-json-rpc-unwrapping-sse-connect-race-stdio-stderrbuffer-limits-and-lspclient-lifecycle) |

### Phase 4 — Evaluation, Observability, and Architecture Consolidation

| Task | Location | Complexity | Expected impact | Reassessed Status | Spec |
| :--- | :--- | :---: | :--- | :--- | :--- |
| Pass registered tools to `SWEBenchRunner`, sanitize `instance_id` path before `shutil.rmtree`, fix `pytest -x` grading, and replace tautological evaluation assertions. | `swebench.py`, `scenario_evaluator.py`, `cli.py` | Low | Makes SWE-bench and scenario evaluations measure real agent capability safely. | **VERIFIED FIXED** (`test_spec20_reliability_safety.py`) | [SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli) |
| Redact secrets in telemetry error logs and OTel spans, restore `_session_modified_files` in `_build_success_response`, wire `--thinking`, and fix `session export` `fsync`. | `telemetry.py`, `tracer.py`, `inference_loop_responses.py`, `work_summary.py`, `main.py`, `session_cmd.py` | Low | Closes telemetry secret leaks, restores work-summary git diffs, and fixes CLI defects. | **VERIFIED FIXED** (`test_spec20_reliability_safety.py`) | [SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli) |
| Execute the Tool and Graph Simplification Plan (27 capability tools and single unified graph topology). | `src/tools/`, `src/core/orchestration/graph/`, `tui/` | High | Eliminates legacy/frontier graph divergence and reduces maintenance surface. | **OPEN** | Appendix |

---

## 15. Remediation Status Against the Current Tree (Reassessed 2026-10-17)

The findings in §§3–13 were authored against an earlier revision, followed by a remediation branch that claimed 28 items as `FIXED`. On **2026-10-17**, every claimed fix was re-verified line-by-line against the current source tree and tested for bypasses, edge cases, and regressions.

Status legend:
- **VERIFIED FIXED** — the cited defect and its contract are fully satisfied in the current source tree.
- **PARTIAL** — the primary path was patched, but concrete bypasses, missing callers, or edge-case defects prevent the contract from holding.
- **BROKEN** — the fix is non-functional at runtime or trivially bypassed on normal execution paths.
- **REGRESSED** — the remediation introduced a new high/critical defect.

### Phase 1 — Critical Stability Fixes (Resolved & Reassessed 2026-10-19)

| # | Item | Claimed Status | Final Status (2026-10-19) | Resolution Details & Verified Test Coverage | Spec |
| :- | :--- | :--- | :--- | :--- | :--- |
| 1 | Evidence-based completion + minimum verification on every tier | FIXED | **VERIFIED FIXED** | Required actual mutation evidence for `TaskType.MUTATING`; passed live loop `last_result`, `mutating_tool_calls_count`, and errors to `handle_no_tool_completion`; passed unpolluted `raw_user_task` to task classifier; cleared transient turn errors on retry; routed failed `lite` verification to recovery/failure. Covered by `tests/unit/test_completion_contract.py` and `tests/integration/test_lite_verification_routing.py`. | [SPEC-04](#spec-04-high--fix-evidence-based-completion-contracts-frontier-loop-stale-last_result-lite-verification-bypass-and-error-poisoning) |
| 2 | Unavailable sandbox enforcement must deny by default; private temp | FIXED | **VERIFIED FIXED** | Repaired macOS SBPL profile with valid `(require-not (subpath ...))` grammar and `(deny file-write*)`; implemented two-phase probe requiring positive write confirmation; created private per-invocation `/tmp` directories; routed `run_tests`, `run_js_tests`, `run_linter`, `syntax_check`, `format_file`, `git_*`, and `grep` through `sandbox.run_sandboxed`. Covered by `tests/unit/test_sandbox_macos_sbpl.py` and `tests/unit/test_tools_sandboxed_subprocesses.py`. | [SPEC-02](#spec-02-critical--repair-macos-sandbox-exec-sbpl-grammar-false-positive-probe-and-unsandboxed-subprocess-tools) |
| 3 | Protected paths + secret detection + universal redaction | FIXED | **VERIFIED FIXED** | Enforced `is_secret_path` and `redact_secrets` across `batched_file_read`, `generate_patch`, `find_references`, `ast_list_symbols`, `git_diff`, and native `grep`; blocked base64 byte reads containing credentials; normalized `..` and checked resolved symlinks in `WorkspaceGuard`; blocked workspace root deletion in `delete_file`. Covered by `tests/unit/test_secret_redaction_bypasses.py` and `tests/unit/test_workspace_guard_bypasses.py`. | [SPEC-06](#spec-06-high--close-protected-path-and-secret-redaction-bypasses-in-read-diff-grep-encoding-sessionstore-and-distiller), [SPEC-07](#spec-07-high--fix-workspaceguard-dot-segment-and-symlink-bypasses-and-block-delete_filepath-workspace-wipe) |
| 4 | Server workspace roots + bounded admission + cancellation-before-start | FIXED | **VERIFIED FIXED** | Defaulted `working_dir=None` to approved workspace roots; deferred filesystem `resolve()` / `is_dir()` checks until after authentication; hydrated multi-turn session history from `session_store`; bound `event_bus` at `Orchestrator` construction; closed executors in `finally`; capped task registry history. Covered by `tests/unit/test_task_server_safeguards.py`. | [SPEC-15](#spec-15-high--fix-server-working_dirnone-bypass-pre-auth-filesystem-oracle-session-history-hydration-eventbus-wiring-and-executor-leaks) |
| 5 | Redirect-hop / peer-address SSRF validation | FIXED | **VERIFIED FIXED** | Eliminated DNS rebinding TOCTOU via connected socket `getpeername()` validation; unwrapped `ipv4_mapped`, `sixtofour`, and `teredo` IPv6 transition addresses; streamed response bodies with hard byte caps; blocked HTTPS-to-HTTP redirect downgrades. Covered by `tests/unit/test_ssrf_dns_rebinding.py`. | [SPEC-10](#spec-10-high--eliminate-ssrf-dns-rebinding-toctou-unwrap-ipv6-transition-addresses-and-bound-response-streams) |
| 6a | SQLite session deletion correctness | FIXED | **VERIFIED FIXED** | Synchronized `messages_fts` with `messages.id` by inserting explicit `rowid = new.id` in triggers; added `AFTER DELETE` triggers for FTS cleanup; cleaned `mistakes_fts` and `decisions.json` on session deletion; cleaned orphaned snapshot rows on revert. Covered by `tests/unit/test_sqlite_fts_session_delete_revert.py`. | [SPEC-12](#spec-12-high--fix-sqlite-fts5-rowid-desynchronization-on-session-deleterevert-and-jsonlsessionstore-parity) |
| 6b | LSP prompt context for real `SymbolGraph` data | FIXED | **VERIFIED FIXED** | `lsp_context.py:_get_symbols` iterates `symbols["classes"]` and `symbols["functions"]` lists and extracts structured `{name, kind, file, line}` symbols. | [SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects) |
| 6c | Incremental index refresh | FIXED | **VERIFIED FIXED** | Checked `"file_hashes"` in metadata; preserved `start_line`, `end_line`, and `docstring`; included `ast.AsyncFunctionDef`; purged deleted file symbols from `repo_index.json`. Covered by `tests/unit/test_analysis_node_symbol_graph_vectorstore.py`. | [SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects) |

### Section 4 — High-Risk Safety (Resolved & Reassessed 2026-10-19)

| Finding | Claimed Status | Final Status (2026-10-19) | Resolution Details & Verified Test Coverage | Spec |
| :--- | :--- | :--- | :--- | :--- |
| Sandbox falls back to unrestricted host execution | FIXED | **VERIFIED FIXED** | See Phase 1 #2 above; macOS SBPL grammar repaired, probe validated, `/tmp` isolated, and subprocess tools sandboxed. | [SPEC-02](#spec-02-critical--repair-macos-sandbox-exec-sbpl-grammar-false-positive-probe-and-unsandboxed-subprocess-tools) |
| Linux sandbox shares writable host `/tmp` | FIXED | **VERIFIED FIXED** | Private `--tmpfs /tmp` mounted in `bwrap`; subprocess tools routed through `run_sandboxed`. | [SPEC-02](#spec-02-critical--repair-macos-sandbox-exec-sbpl-grammar-false-positive-probe-and-unsandboxed-subprocess-tools) |
| Redirects bypass SSRF validation | FIXED | **VERIFIED FIXED** | See Phase 1 #5 above; socket peer IP verified, IPv6 transition addresses unwrapped, streams bounded. | [SPEC-10](#spec-10-high--eliminate-ssrf-dns-rebinding-toctou-unwrap-ipv6-transition-addresses-and-bound-response-streams) |
| Server tasks may select arbitrary host directories | FIXED | **VERIFIED FIXED** | See Phase 1 #4 above; `working_dir=None` validated inside approved roots, path checks deferred post-auth. | [SPEC-15](#spec-15-high--fix-server-working_dirnone-bypass-pre-auth-filesystem-oracle-session-history-hydration-eventbus-wiring-and-executor-leaks) |
| Raw `ToolRegistry.call()` bypasses central enforcement | FIXED | **VERIFIED FIXED** | Removed premature `mark_file_read` in `edit_code_block` and `ast_rename`; routed `lsp_rename` through pipeline; added `multiedit` to `WRITE_TOOLS_REQUIRING_READ`; fixed RBW `Path.cwd()` bug by resolving against `workdir`; made directory rollback recursive; started step transactions in `frontier_loop_node`. Covered by `tests/unit/test_mutating_tool_guardrails.py`. | [SPEC-08](#spec-08-high--fix-mutating-tool-guardrails-read-before-write-cwd-bug-permissiondry-run-tables-and-directorystep-rollback) |
| Secret-bearing content can enter prompts, sessions, and logs | FIXED | **VERIFIED FIXED** | See Phase 1 #3 above; universal `is_secret_path` and `redact_secrets` across all read/diff/grep/memory sinks. | [SPEC-06](#spec-06-high--close-protected-path-and-secret-redaction-bypasses-in-read-diff-grep-encoding-sessionstore-and-distiller) |
| Prior-session memory inserted without the safety scanner | FIXED | **VERIFIED FIXED** | Delegated `SessionStore` decision methods to underlying sanitized stores; compiled threat regexes with `re.DOTALL`; sanitized distiller/vector outputs; failed closed on scanner errors. Covered by `tests/unit/test_secret_redaction_bypasses.py`. | [SPEC-06](#spec-06-high--close-protected-path-and-secret-redaction-bypasses-in-read-diff-grep-encoding-sessionstore-and-distiller) |
| `rename_file` can overwrite a destination | FIXED | **VERIFIED FIXED** | Rejects existing destination; checks RBW on resolved path; records both `src_path` and `dst_path` in modified/read files. Covered by `tests/unit/test_mutating_tool_guardrails.py`. | [SPEC-08](#spec-08-high--fix-mutating-tool-guardrails-read-before-write-cwd-bug-permissiondry-run-tables-and-directorystep-rollback) |
| Fallback file locking can evict a live holder | FIXED | **VERIFIED FIXED** | Implemented non-blocking `LOCK_EX | LOCK_NB` polling loop with timeout; gated `os.kill(pid, 0)` behind `same_host is True`. Covered by `tests/unit/test_spec20_reliability_safety.py`. | [SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli) |
| Parallel delegation re-enters one `Context` concurrently | FIXED | **VERIFIED FIXED** | Copied `Context` per submitted batch call; isolated `active_agent` via `ContextVar` to eliminate data race. Covered by `tests/unit/test_batch_context_isolation.py`. | [SPEC-11](#spec-11-high--fix-batch-shared-context-and-thread-local-orchestrator-loss-and-subagent-active_agent-data-race) |

### Additional Findings Previously Marked Resolved (Resolved & Reassessed 2026-10-19)

| Audit reference | Claimed Status | Final Status (2026-10-19) | Resolution Details & Verified Test Coverage | Spec |
| :--- | :--- | :--- | :--- | :--- |
| §11 `codingagent mcp add` writes `.agent/config.json` | FIXED | **VERIFIED FIXED** | Updated TUI `/mcp add` to write to `.codingAgent/config.json` with standardized `"command"` and `"args"`. Covered by `tests/unit/test_phase3_usability_performance.py`. | [SPEC-18](#spec-18-high--wire-tui-active_role-end-to-end-fix-tui-mcp-add-path-fix-sessionscreen-offsetfocus-and-protect-api-keys) |
| §8 incremental index symbol staleness | FIXED | **VERIFIED FIXED** | Fixed `analysis_node.py` symbol-graph caller formatting `TypeError`; indexed `VectorStore` on repository index; fixed `"file_hashes"` key in incremental refresh. Covered by `tests/unit/test_analysis_node_symbol_graph_vectorstore.py`. | [SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects) |
| §13 dormant skills island (`SkillLoader`/`SkillRegistry`), `symbol_reader`, `role_tools` | REMOVED | **VERIFIED FIXED** | Modules deleted in commits `51660eb`, `6dcd1a9`, `62e9480`. | N/A |
| §3 lite/small paths skip verification | FIXED (partial) | **VERIFIED FIXED** | See Phase 1 #1 above; `route_frontier_loop_exit_lite` and `_compile_lite_graph` enforce verification and failure routing. | [SPEC-04](#spec-04-high--fix-evidence-based-completion-contracts-frontier-loop-stale-last_result-lite-verification-bypass-and-error-poisoning) |
| §11 config reload / `.agent` vs `.codingAgent` reconciliation | FIXED | **VERIFIED FIXED** | Added `ProviderManager.reload()` called from `_on_config_reloaded`; updated `_cached_config` before `reloader.clear_changed()`; preserved MCP tools on reload; reconciled instruction file loaders with secret redaction. Covered by `tests/unit/test_phase2_item5_config_reload.py`. | [SPEC-17](#spec-17-high--fix-config-hot-reload-no-op-provider-re-init-premature-clear_changed-and-instruction-loader-drift) |
| §6 provider fallback circuit breaker + candidate model | FIXED | **VERIFIED FIXED** | Recorded primary provider failure before secondary fallback so failed provider breakers open; implemented atomic `allow_request()` for `HALF_OPEN` single-probe admission. Covered by `tests/unit/test_circuit_breaker_fallback.py`. | [SPEC-14](#spec-14-high--fix-provider-circuit-breaker-primary-reset-bug-half_open-probe-guard-and-anthropicolllama-streaming) |
| §6 Anthropic native API Messages wire format | FIXED | **VERIFIED FIXED** | Added `"stream": True` to payload when `stream=True`; parsed Anthropic SSE event types in streaming consumer; placed `tool_calls` in `choices[0].message.tool_calls`. Covered by `tests/unit/test_anthropic_adapter_messages.py`. | [SPEC-14](#spec-14-high--fix-provider-circuit-breaker-primary-reset-bug-half_open-probe-guard-and-anthropicolllama-streaming) |
| §7 tool schema strictness (`additionalProperties: false`) | FIXED | **VERIFIED FIXED** | Removed `"workdir"` exemption from undeclared-field rejection; stripped `workdir`/`working_dir` from model schemas; unconditionally bound and confined working directories in pipeline. Covered by `tests/unit/test_workdir_confinement_all_tools.py`. | [SPEC-01](#spec-01-critical--lock-down-workdir-and-working_dir-argument-injection-across-all-tools) |
| §6 file-lock per-owner release guard | FIXED | **VERIFIED FIXED** | `file_lock_manager.py` keys release guards by `(path, agent_id)`. | N/A |
| §6 bounded shutdown hooks and P2P cleanup | FIXED | **VERIFIED FIXED** | Executed each shutdown hook and P2P cleanup in an independent daemon thread joined with remaining timeout budget. Covered by `tests/unit/test_spec20_reliability_safety.py`. | [SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli) |
| §6 killable timeout & cooperative cancellation | FIXED | **VERIFIED FIXED** | Registered child subprocess process groups and invoked `os.killpg(pgid, signal.SIGKILL)` on pipeline timeout. Covered by `tests/unit/test_spec20_reliability_safety.py`. | [SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli) |
| §3 strict state contract enforcement & frontier loop | FIXED | **VERIFIED FIXED** | Enabled strict state validation by default; added `retrieved_snippets`, `_frontier_loop_turns`, and `modified_files` to `AgentState` and node output schemas. Covered by `tests/unit/test_state_schemas.py`. | [SPEC-05](#spec-05-high--fix-strict-state-schema-crash-on-retrieved_snippets-and-declare-missing-agentstate-fields) |
| §9 memory/decision provenance and retrieval sanitization | FIXED | **VERIFIED FIXED** | Delegated `SessionStore` decision methods to underlying stores; redacted secrets in distiller and vector memory. Covered by `tests/unit/test_secret_redaction_bypasses.py`. | [SPEC-06](#spec-06-high--close-protected-path-and-secret-redaction-bypasses-in-read-diff-grep-encoding-sessionstore-and-distiller) |
| §11 session-list resume/delete index mismatch | FIXED | **VERIFIED FIXED** | Subtracted `_offset` in `SessionScreen`; prevented key hijacking while typing in search input; allowed keyboard activation on empty lists; validated git SHA. Covered by `tests/unit/test_phase3_usability_performance.py`. | [SPEC-18](#spec-18-high--wire-tui-active_role-end-to-end-fix-tui-mcp-add-path-fix-sessionscreen-offsetfocus-and-protect-api-keys) |
| §11 TUI role selection synchronization | FIXED | **VERIFIED FIXED** | Wired `active_role` end-to-end through `_bridge_agent.py`, `inference_loop.py`, `frontier_loop_node.py`, and `perception_node.py`. Covered by `tests/unit/test_phase3_usability_performance.py`. | [SPEC-18](#spec-18-high--wire-tui-active_role-end-to-end-fix-tui-mcp-add-path-fix-sessionscreen-offsetfocus-and-protect-api-keys) |
| §12 WebSocket client unbounded queue/keepalive controls | FIXED | **VERIFIED FIXED** | Enforced strict `session_id` isolation via `ContextVar`; allowlisted subscribe events; capped dropped-event metric keys. Covered by `tests/unit/test_ws_sse_session_isolation.py`. | [SPEC-16](#spec-16-high--enforce-websocket-and-sse-session-isolation-align-event-names-and-bound-subscriptionsmetrics) |
| §12 SSE adapter thread-safe queue dispatch & parameter clamping | FIXED | **VERIFIED FIXED** | Attached `session_id` via `ContextVar` in `EventBus.publish`; aligned `DEFAULT_SERVER_EVENT_TYPES` with backend published names. Covered by `tests/unit/test_ws_sse_session_isolation.py`. | [SPEC-16](#spec-16-high--enforce-websocket-and-sse-session-isolation-align-event-names-and-bound-subscriptionsmetrics) |
| §12 Frontier INFO logs history preview reduction & GUILogger isEnabledFor | FIXED | **VERIFIED FIXED** | `frontier_loop_node.py` guards debug logging with `isEnabledFor(logging.DEBUG)` and `GUILogger` implements `isEnabledFor`. | N/A |
| §12 hardware detection zero VRAM preservation | FIXED | **VERIFIED FIXED** | Preserved `0.0` VRAM; guarded macOS Metal call to run only on Darwin. Covered by `tests/unit/test_spec20_reliability_safety.py`. | [SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli) |
| §12 KVCacheGovernor custom threshold support | FIXED | **VERIFIED FIXED** | Clamped thresholds to `[0.0, 1.0]`; handled zero and overcommitted VRAM with CPU token budget without negative token counts. Covered by `tests/unit/test_spec20_reliability_safety.py`. | [SPEC-20](#spec-20-medium--fix-filelock-timeout-shutdown-hook-starvation-subprocess-timeout-kill-kvcachegovernor-work-summary-swebench-telemetry-and-cli) |
| §8 index freshness relies on root directory mtime | FIXED | **VERIFIED FIXED** | Tracked revision digest in `_ensure_repo_index` and verified `repo_index.json` exists on disk. Covered by `tests/unit/test_analysis_node_symbol_graph_vectorstore.py`. | [SPEC-13](#spec-13-high--fix-analysis_node-symbol-graph-typeerror-unindexed-vectorstore-and-incremental-index-refresh-defects) |
| §8 simple frontier tasks skip analysis/retrieval | FIXED | **VERIFIED FIXED** | Included `retrieved_snippets` in `_AgentStateSpec` and node output schema. Covered by `tests/unit/test_state_schemas.py`. | [SPEC-05](#spec-05-high--fix-strict-state-schema-crash-on-retrieved_snippets-and-declare-missing-agentstate-fields) |
| Appendix: TUI spawn permission, diff preview `requires_confirmation`, `/share` single-mount | FIXED | **VERIFIED FIXED** | Verified in `tui/tui_src/` and mirror `tui/src/`. | N/A |

---

## 16. Detailed Remediation & Implementation Specifications (2026-10-17 Audit)

Each specification below defines the exact root cause, affected source locations, step-by-step implementation contract, and required regression tests.

---

### SPEC-01 [Critical] — Lock Down `workdir` and `working_dir` Argument Injection Across All Tools

- **Addresses:** §7 Tool Schema Strictness Regression; §4 Workspace Confinement Bypass.
- **Locations:**
  - `src/tools/_tool.py:216-238` (`ToolDefinition.validate_args`)
  - `src/core/orchestration/tool_execution_pipeline.py:388-399` (`_check_workdir_confinement`) and `:690-698` (`_dispatch_tool_call`)
  - `src/core/orchestration/tool_preflight.py:120-149` (`preflight_check`)
  - `src/tools/lsp_tools.py:126,179,224,270,360,419`
  - `src/tools/subagent_tools.py:863,987`
  - `src/core/memory/memory_tools.py:12-45`
- **Root Cause & Trigger:**
  1. `_tool.py:237` exempts `"workdir"` when rejecting undeclared parameters (`if field_name not in properties and field_name not in ("self", "cls", "workdir"):`), even though `"workdir"` is intentionally hidden from the JSON schema (`:216`).
  2. `tool_execution_pipeline.py:690` only injects `orch.working_dir` if `"workdir" not in args`.
  3. `_check_workdir_confinement` (`tool_execution_pipeline.py:388`) only checks `args.get("workdir")` for `("bash", "run_tests", "run_bash")`, and `tool_preflight.py:126` only checks `args["path"]` against `workspace_root` (ignoring `args["workdir"]`).
  4. As a result, a model call like `read_file(path="passwd", workdir="/etc")`, `write_file(path="pwned.txt", content="x", workdir="/tmp")`, or `grep(pattern=".*", path=".", workdir="/Users")` passes schema validation, preserves `args["workdir"]`, and `_safe_resolve(path, workdir)` resolves the relative `path` inside the attacker-chosen `workdir`.
  5. Additionally, `lsp_*` tools (`lsp_tools.py`), `delegate_task` / `delegate_tasks_parallel` (`subagent_tools.py`), and `memory_search` (`memory_tools.py`) expose `working_dir` (or `workdir`) as a normal schema parameter that is neither overwritten nor confined to `orch.working_dir`.
- **Implementation Specification:**
  1. **Reject model-supplied `workdir` in schema validation (`src/tools/_tool.py:234-239`):**
     - Remove `"workdir"` from the exemption tuple at line 237 so it reads `if field_name not in properties and field_name not in ("self", "cls"):`.
     - In `ToolDefinition.to_openai_schema()` (`_tool.py:140-185`), strip both `"workdir"` and `"working_dir"` from model-facing `properties` and `required` lists so `working_dir` is treated identically to `workdir` as an orchestrator-injected context parameter.
  2. **Unconditionally bind `workdir` and `working_dir` in `_dispatch_tool_call` (`src/core/orchestration/tool_execution_pipeline.py:689-699`):**
     - When `hasattr(orch, "working_dir") and orch.working_dir`: inspect `sig.parameters` of `tool["fn"]`.
     - If `"workdir"` is in `sig.parameters`: if `"workdir"` was provided in `args`, validate via `_inside(args["workdir"])` that `Path(args["workdir"]).resolve()` is equal to or inside `Path(orch.working_dir).resolve()` (returning a confinement error dict if outside); otherwise set `args["workdir"] = Path(orch.working_dir).resolve()`.
     - Apply the exact same rule for `"working_dir"` in `sig.parameters` (setting `args["working_dir"] = str(Path(orch.working_dir).resolve())` or validating that any sub-directory stays strictly inside `Path(orch.working_dir).resolve()`).
  3. **Extend `_check_workdir_confinement` and `preflight_check` (`tool_execution_pipeline.py:388-399`, `tool_preflight.py:120-149`):**
     - In `_check_workdir_confinement`, check `args.get("workdir")` and `args.get("working_dir")` for **all** tools whenever either key is present in `args`, not only `("bash", "run_tests", "run_bash")`.
     - In `preflight_check`, if `"workdir"` or `"working_dir"` is present in `args`, resolve it first and verify it is inside `workspace_root`; then resolve path arguments (`path`, `file_path`, `src_path`, `dst_path`, `paths`) relative to that validated directory and verify they remain inside `workspace_root`.
  4. **Harden `memory_search` (`src/core/memory/memory_tools.py:12-45`):**
     - Change `workdir: str = "."` to `workdir: object = _WORKDIR_DEFAULT` and resolve `.codingAgent` paths strictly via `_safe_resolve`.
- **Regression Test Specification (`tests/unit/test_workdir_confinement_all_tools.py`):**
  - Test that `ToolDefinition.validate_args` rejects `{"path": "a.py", "workdir": "/etc"}` and `{"file_path": "a.py", "working_dir": "/etc"}` with `"unexpected undeclared field"`.
  - Test through `Orchestrator.execute_tool` that `read_file`, `write_file`, `edit_file_atomic`, `delete_file`, `rename_file`, `grep`, `glob`, `list_files`, `batched_file_read`, `lsp_diagnostics`, `delegate_task`, and `memory_search` all reject `workdir="/etc"` and `working_dir="/etc"` and never read or write outside `orch.working_dir`.

---

### SPEC-02 [Critical] — Repair macOS `sandbox-exec` SBPL Grammar, False-Positive Probe, and Unsandboxed Subprocess Tools

- **Addresses:** Phase 1 #2; §4 Sandbox Host Execution & `/tmp` Isolation.
- **Locations:**
  - `src/tools/sandbox.py:91-150` (`_probe_sandbox_exec_enforced`), `:184-244` (`_build_sandbox_exc_profile`, `_write_sandbox_exc_profile`), `:308-320` (`_build_bwrap_args`), `:404-487` (`run_sandboxed`), `:530-542` (`_enforcement_required`)
  - `src/tools/_bash_exec.py:511-561` (background execution), `:737-790` (`bash_readonly`), `:894-968` (`_check_network_policy`, `_build_background_sandbox`)
  - `src/tools/verification_tools.py:88,113,138,401,418,434,443,479,610,635,663,746,799,855,948`
  - `src/tools/formatter.py:111`, `src/tools/lint_dispatch.py:59,80,116,134`, `src/tools/git_tools.py:30`, `src/tools/system_tools.py:73`
- **Root Cause & Trigger:**
  1. `_probe_sandbox_exec_enforced` (`sandbox.py:116-120`) uses `(deny file-write* (not (subpath ...)))`. In SBPL, `not` is not a valid filter combinator (`require-not` is required). `sandbox-exec` exits with code `65` before running `sh`; because lines 131–141 ignore `returncode` and only check `if target_outside.exists(): return False`, the probe always returns `True` on macOS.
  2. `_build_sandbox_exc_profile` (`sandbox.py:184-225`) emits `(allow default)` with invalid `(require (file-issue-extension* ...) (not ...))` and **no `(deny file-write*)` rule**. At runtime (`sandbox.py:438`), `sandbox-exec` exits `65` and falls through to Step 4 (raising `RuntimeError` or running unsandboxed if `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1`).
  3. `bash_readonly` (`_bash_exec.py:765-789`) does not catch `RuntimeError`.
  4. `_enforcement_required()` (`sandbox.py:530-542`) returns `True` even when `CODINGAGENT_SANDBOX_LEVEL=off`, breaking `bash(run_in_background=True)` (`_bash_exec.py:516`) and `_check_network_policy` (`:905`) when sandboxing is explicitly set to `"off"`.
  5. `verification_tools.py`, `formatter.py`, `lint_dispatch.py`, `git_tools.py`, and `system_tools.py` call raw `subprocess.run`, bypassing `run_sandboxed`.
- **Implementation Specification:**
  1. **Fix `_probe_sandbox_exec_enforced` (`src/tools/sandbox.py:91-150`):**
     - Write a valid SBPL profile using `(require-not (subpath ...))`:
       ```scheme
       (version 1)
       (allow default)
       (deny file-write* (require-not (subpath "{probe_dir_escaped}")))
       ```
     - Execute two probes or a combined probe:
       - Probe A (allowed write inside `probe_dir`): must exit with `returncode == 0` and create `probe_dir / "inside_ok"`. If `returncode != 0` or `inside_ok` does not exist, return `False` (catches syntax/deprecation failures).
       - Probe B (denied write to `target_outside`): must fail (`returncode != 0`) and `target_outside.exists()` must be `False`.
  2. **Fix `_build_sandbox_exc_profile` and isolate `/tmp` on macOS (`src/tools/sandbox.py:184-244`):**
     - Escape `\` and `"` in all path strings (`def _sbpl_escape(p: str) -> str: return p.replace("\\", "\\\\").replace('"', '\\"')`).
     - Create a per-invocation private temporary directory (e.g. `tempfile.mkdtemp(prefix="codingagent-tmp-")`) passed as `TMPDIR` in `env` and cleaned up in `finally: shutil.rmtree(private_tmp, ignore_errors=True)`.
     - Emit valid SBPL:
       ```scheme
       (version 1)
       (allow default)
       (deny file-write*)
       (allow file-write*
           (subpath "{cwd_escaped}")
           (subpath "{private_tmp_escaped}")
           (literal "/dev/null")
           (literal "/dev/zero")
           (literal "/dev/tty")
           (literal "/dev/dtracehelper")
       )
       (deny file-read*
           (subpath "/tmp")
           (subpath "/private/tmp")
       )
       (allow file-read*
           (subpath "{cwd_escaped}")
           (subpath "{private_tmp_escaped}")
       )
       ```
       Plus `(deny network*)` when `network=False` and `(deny process-fork)` when `level == "full"`.
     - In `run_sandboxed` (`sandbox.py:438-445`), if `result.returncode == 65 and "sandbox-exec:" in str(_stderr)`: do **not** silently fall back when `_enforcement_required()` is `True`; raise `RuntimeError(f"sandbox-exec profile error: {_stderr}")`.
  3. **Fix Linux `_build_bwrap_args` `/etc` mounts (`src/tools/sandbox.py:308-320`):**
     - Add read-only binds for safe `/etc` paths needed by toolchains and dynamic linkers (`/etc/ld.so.cache`, `/etc/ld.so.conf`, `/etc/ld.so.conf.d`, `/etc/alternatives`, `/etc/ssl`, `/etc/ca-certificates`, `/etc/resolv.conf`, `/etc/hosts`, `/etc/nsswitch.conf`, `/etc/localtime`).
  4. **Fix `bash_readonly`, `_enforcement_required`, and background `.sb` cleanup (`src/tools/_bash_exec.py`, `src/tools/sandbox.py`):**
     - Add `except RuntimeError as _rte: return {"status": "error", "error": f"Sandbox execution denied: {_rte}"}` to `bash_readonly` (`_bash_exec.py:788`).
     - In `_enforcement_required()` (`sandbox.py:530-542`), if `get_sandbox_level() == "off"` and not `(_REQUIRE_ENFORCEMENT or _autonomous_mode())`, return `False`.
     - For `run_in_background=True` on macOS, wrap the background command so the temporary `.sb` file is unlinked on process exit or managed in a session cleanup list registered with `atexit`.
  5. **Route verification, lint, formatter, git, and grep subprocesses through `sandbox.run_sandboxed`:**
     - Replace raw `subprocess.run(...)` calls in `verification_tools.py`, `formatter.py`, `lint_dispatch.py`, `git_tools.py:_run_git`, and `system_tools.py:grep` with `_sandbox.run_sandboxed(..., cwd=Path(workdir), network=False)`.
- **Regression Test Specification (`tests/unit/test_sandbox_macos_sbpl.py`, `tests/unit/test_tools_sandboxed_subprocesses.py`):**
  - On macOS (when `/usr/bin/sandbox-exec` exists), run `_probe_sandbox_exec_enforced()` and `run_sandboxed(["sh", "-c", "echo ok > inside.txt"], cwd=tmp_path)` with `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=0`: verify `returncode == 0`, `inside.txt` is created, writing to `tmp_path.parent / "outside.txt"` fails with `Operation not permitted`, and reading a sentinel file in `/tmp` fails.
  - Test `bash_readonly` when `sandbox_available() == False` and `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=0`: verify it returns `{"status": "error", "error": "Sandbox execution denied: ..."}` instead of raising an uncaught `RuntimeError`.
  - Spy on `sandbox.run_sandboxed` and verify `run_tests`, `run_js_tests`, `run_linter`, `syntax_check`, `format_file`, `quick_lint`, and `git_status` all invoke `run_sandboxed`.

---

### SPEC-03 [Critical] — Eliminate Untrusted Workspace MCP `stdio` RCE, Unconsented `context7` `npx` Execution, and Env Leakage

- **Addresses:** New Critical Finding (Safeguards & Security / MCP).
- **Locations:**
  - `src/core/config_loader.py:253-295` (`get_mcp_servers`)
  - `src/core/mcp/manager.py:177-265,314-322` (`McpServerManager`)
  - `src/core/mcp/mcp_client.py:132-153` (`McpStdioClient.connect`)
  - `src/tools/tools_config.py:46,100-103` (`get_tool_permission`)
- **Root Cause & Trigger:**
  1. `get_mcp_servers()` (`config_loader.py:284-293`) injects a hardcoded default server (`npx -y @upstash/context7-mcp`) whenever `servers` is empty, causing `McpServerManager.start()` to download and run an external npm package without user consent and causing `codingagent mcp list` / TUI `/mcp list` to always show an unconfigured `context7` server.
  2. `get_mcp_config(working_dir)` loads `<working_dir>/.codingAgent/config.json` from the repository. A repository containing a malicious `stdio` server config achieves immediate RCE when `McpServerManager` starts.
  3. `McpStdioClient.connect()` (`mcp_client.py:143`) copies `os.environ.copy()` for every subprocess, leaking all host secrets (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CONTEXT7_API_KEY`, `GITHUB_TOKEN`, etc.) to every stdio MCP server.
  4. Dynamically registered MCP tools (`{server}/{tool}`) are not in `TOOL_PERMISSIONS` and fall back to `DEFAULT_PERMISSION_LEVEL = PermissionLevel.WORKSPACE_WRITE` (`tools_config.py:46`), executing without user confirmation.
- **Implementation Specification:**
  1. **Remove hardcoded `context7` injection (`src/core/config_loader.py:284-295`):**
     - Return `[]` when no MCP servers are configured. Normalize server entries so both `"cmd": ["bin", "arg1"]` and `"command": "bin", "args": ["arg1"]` are supported consistently across `config_loader.py`, `mcp_cmd.py`, and TUI `/mcp`.
  2. **Gate workspace-defined `stdio` MCP servers on explicit trust (`src/core/mcp/manager.py`, `src/core/config_loader.py`):**
     - Distinguish user-global MCP servers (`~/.config/codingagent/config.json`) from workspace-local servers (`<workdir>/.codingAgent/config.json`).
     - Workspace-local `stdio` servers must not auto-start unless the workspace path + command digest is explicitly trusted by the user (or approved via an interactive permission gate / `CODINGAGENT_TRUST_WORKSPACE_MCP=1`).
  3. **Scrub subprocess environment in `McpStdioClient.connect` (`src/core/mcp/mcp_client.py:141-145`):**
     - Replace `env = os.environ.copy()` with a minimal safe environment allowlist (`PATH`, `HOME`, `USER`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, `SYSTEMROOT`) merged only with explicitly configured `self._env_vars` for that specific server.
  4. **Default MCP tools to `PermissionLevel.PROMPT` (`src/tools/tools_config.py:100-103`):**
     - In `get_tool_permission(name)`, if `name` is an MCP tool (e.g. `"/" in name` or `name.startswith("mcp_")`) and not explicitly overridden in user config, return `PermissionLevel.PROMPT` instead of `PermissionLevel.WORKSPACE_WRITE`.
- **Regression Test Specification (`tests/unit/test_mcp_security.py`):**
  - Verify `get_mcp_servers()` returns `[]` on an empty workspace config (no `context7` auto-injection).
  - Verify `McpStdioClient.connect` does not pass `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` from `os.environ` into `create_subprocess_exec(..., env=...)`.
  - Verify workspace `.codingAgent/config.json` `stdio` server is refused without trust approval, and registered MCP tools default to `PermissionLevel.PROMPT`.

---

### SPEC-04 [High] — Fix Evidence-Based Completion Contracts, Frontier Loop Stale `last_result`, `lite` Verification Bypass, and Error Poisoning

- **Addresses:** Phase 1 #1; §3 Text-Only Completion & Lite Tier Verification.
- **Locations:**
  - `src/core/orchestration/graph/completion_contract.py:28-39,98-189`
  - `src/core/orchestration/graph/nodes/frontier_completion.py:114-139,240-265`
  - `src/core/orchestration/graph/nodes/frontier_loop_node.py:599-623,781-795,826,847,890-905`
  - `src/core/orchestration/graph/tier_graph_routing.py:169-190`
  - `src/core/orchestration/graph/builder.py:671-678`
  - `src/core/orchestration/graph/nodes/evaluation_node.py:125-167`
- **Root Cause & Trigger:**
  1. `completion_contract.py:170-177`: When `task_type == TaskType.MUTATING`, `has_modified_files == False`, and `tool_calls_count > 0`, it checks `if last_ok is True or last_status == "ok": return CompletionContractVerdict(is_complete=True, ...)`. Because every read-only tool returns `ok=True`, calling `read_file` once and stopping satisfies `MUTATING` completion without modifying anything.
  2. `frontier_loop_node.py:782`: Calls `handle_no_tool_completion(..., state=state)` without passing the loop's local `last_result`, `last_tool_name`, or `errors`. `frontier_completion.py:256` reads `state.get("last_result")` (the pre-node state value, usually `None`), missing failed tool calls from earlier turns in the same loop and overwriting `last_result` with `{"ok": True, "_completion_detected": True}`.
  3. `frontier_loop_node.py:608-620`: Appends `<analyst_findings>` and `repo_context` to `task` in-place, then passes `task` to `handle_no_tool_completion`, polluting `classify_task_type(task)` with repository symbol/file names. Meanwhile, `_MUTATION_REGEX` (`completion_contract.py:28-39`) misses common mutation verbs (`change`, `make`, `convert`, `revert`, `format`, `upgrade`, `rename`, `move`, `install`, `configure`) when not followed by a hardcoded noun/extension.
  4. `tier_graph_routing.py:182` (`route_frontier_loop_exit_lite`): Reads `state.get("modified_files") or state.get("_session_modified_files")`, neither of which is returned by `frontier_loop_node`. If `write_file` is followed by `read_file`, `last_tool_name` is `"read_file"` and `lite` skips `verification`. Furthermore, `_compile_lite_graph` (`builder.py:677`) routes `verification -> memory_sync -> END` unconditionally even when verification fails.
  5. `frontier_loop_node.py:599,790,847` & `evaluation_node.py:148-167`: Contract rejection codes (`"unverified_text_only_completion"`, `"inline_verification_failed"`) are appended to `state["errors"]` and never cleared when a subsequent turn succeeds, causing `evaluation_node` to treat them as permanent `critical_errors` and loop until `max_debug_attempts`.
- **Implementation Specification:**
  1. **Require actual mutation evidence for `TaskType.MUTATING` (`src/core/orchestration/graph/completion_contract.py:135-190`):**
     - Add `last_tool_name: Optional[str] = None` and `mutating_tool_calls_count: int = 0` to `evaluate_completion_contract(...)`.
     - When `task_type == TaskType.MUTATING` and `not has_modified_files`: only permit completion if `mutating_tool_calls_count > 0` (or `last_tool_name in MODIFYING_TOOLS | {"bash"}`) **and** `last_ok is True` (e.g., a successful mutating `bash` or `git` command). A read-only tool (`read_file`, `grep`, `list_files`, etc.) with `has_modified_files == False` must return `is_complete=False, error_code="unverified_mutation_missing"`.
  2. **Pass live loop state into `handle_no_tool_completion` (`frontier_loop_node.py:781-795`, `frontier_completion.py:240-265`):**
     - Pass the loop's current `last_result`, `last_tool_name`, `mutating_tool_calls_count`, and current-turn `errors` into `handle_no_tool_completion`.
     - Preserve the original unpolluted `raw_user_task = state.get("task") or ""` before appending `<analyst_findings>` / `repo_context` at line 608, and pass `raw_user_task` to `handle_no_tool_completion`.
     - Broaden `_MUTATION_REGEX` in `completion_contract.py` to cover action verbs (`make`, `change`, `set`, `convert`, `revert`, `restore`, `format`, `upgrade`, `downgrade`, `install`, `move`, ` wrap`, `extract`, `inline`, `implement`, `fix`, `update`, `edit`, `write`, `create`, `delete`, `remove`, `rename`, `patch`, `refactor`) without requiring a strict filename suffix when target code/config intent is present, while respecting `_READ_ONLY_OVERRIDE_REGEX`.
  3. **Return `modified_files` from `frontier_loop_node` and enforce `lite` verification failure routing (`frontier_loop_node.py:880-905`, `tier_graph_routing.py:169-190`, `builder.py:671-678`):**
     - In `frontier_loop_node.py`, include `"modified_files": sorted(orch._session_modified_files)` in the returned state dict (and declare `modified_files` in `AgentState` and `state_schemas.py`).
     - Expand `MODIFYING_TOOLS` in `src/tools/constants.py:38-40` to include `"edit_code_block"`, `"create_directory"`, `"format_file"`, `"ast_rename"`, `"lsp_rename"`, `"git_commit"`, `"git_stash"`, `"git_restore"`.
     - In `_compile_lite_graph` (`builder.py:671-678`), replace the unconditional `verification -> memory_sync` edge with a conditional edge that routes failed verification back to `frontier_loop` (up to `max_debug_attempts`, default 1–2 for lite) or marks the final result failed (`action_failed=True`) before `memory_sync`.
  4. **Clear transient turn errors on recovery (`frontier_loop_node.py:599,785-795`, `evaluation_node.py:148-167`):**
     - Track transient retryable contract/verification errors separately from fatal execution errors, or clear prior turn contract-rejection codes (`"unverified_text_only_completion"`, `"unverified_mutation_missing"`, `"inline_verification_failed"`, `"empty_response"`) when a subsequent turn executes tools and passes verification.
- **Regression Test Specification (`tests/unit/test_completion_contract.py`, `tests/integration/test_lite_verification_routing.py`):**
  - Test that a `MUTATING` task (`"Fix the bug in app.py"`) where the model calls `read_file("app.py")` (`ok=True`) and then stops with text-only output is **rejected** (`is_complete=False`, `error_code="unverified_mutation_missing"`).
  - Test `frontier_loop_node` where Turn 1 edits `a.py`, Turn 2 runs a tool that returns `{"ok": False, "error": "boom"}`, and Turn 3 emits no tool calls: verify `handle_no_tool_completion` sees Turn 2's failure and rejects completion (`error_code="mutation_failed"`).
  - Test `frontier_loop_node` with an informational task (`"How does login work?"`) when `repo_context` contains `"def update_login in auth.py"`: verify `classify_task_type` classifies only the user task (`INFORMATIONAL`), not `repo_context`.
  - Test `lite` graph where Turn 1 calls `write_file("a.py")`, Turn 2 calls `read_file("a.py")`, and Turn 3 stops: verify `route_frontier_loop_exit_lite` routes to `verification` (via `state["modified_files"]`) and that a failing verification does not report task success.

---

### SPEC-05 [High] — Fix Strict State Schema Crash on `retrieved_snippets` and Declare Missing `AgentState` Fields

- **Addresses:** §3 Strict State Contract Enforcement; §8 Auto-Retrieval on Simple Frontier Tasks.
- **Locations:**
  - `src/core/orchestration/graph/state.py:260-480` (`_AgentStateSpec`)
  - `src/core/orchestration/graph/state_schemas.py:246-266` (`_NODE_OUTPUT_SCHEMAS["frontier_loop"]`)
  - `src/core/orchestration/graph/nodes/frontier_loop_node.py:802-810,890-905`
  - `src/core/orchestration/graph/builder.py:103-105` (`_STATE_SCHEMAS_STRICT`)
- **Root Cause & Trigger:**
  1. `frontier_loop_node.py:895-896` sets `result["retrieved_snippets"] = retrieved_snippets`, which is not in `_NODE_OUTPUT_SCHEMAS["frontier_loop"].allowed_keys` (`state_schemas.py:246-266`) and not in `_AgentStateSpec` (`state.py`).
  2. When `CODINGAGENT_STATE_SCHEMAS_STRICT=1` is set, `_validated` raises `NodeResultViolation(node='frontier_loop', reason='unknown_key', details=["unknown output key 'retrieved_snippets'"])`. When strict mode is off (the default), LangGraph silently drops `retrieved_snippets` and `_frontier_loop_turns` because they are absent from `AgentState`.
  3. The plan-mode early return at `frontier_loop_node.py:802-810` omits `last_tool_name`.
- **Implementation Specification:**
  1. Add `retrieved_snippets: Optional[List[Dict[str, Any]]]`, `_frontier_loop_turns: int`, and `modified_files: List[str]` to `_AgentStateSpec` in `src/core/orchestration/graph/state.py` and `initial_state` in `inference_loop_state.py`.
  2. Add `"retrieved_snippets"` and `"modified_files"` to `_NODE_OUTPUT_SCHEMAS["frontier_loop"].allowed_keys` in `src/core/orchestration/graph/state_schemas.py:246-266`.
  3. Include `"last_tool_name": last_tool_name` in the plan-mode return dict at `frontier_loop_node.py:802-810`.
  4. Change `_STATE_SCHEMAS_STRICT` in `src/core/orchestration/graph/builder.py:103-105` to default to `True` (`os.environ.get("CODINGAGENT_STATE_SCHEMAS_STRICT", "1").strip().lower() not in ("0", "false", "no")`).
- **Regression Test Specification (`tests/unit/test_state_schemas.py`):**
  - Run `frontier_loop_node` with auto-retrieval returning non-empty `retrieved_snippets` and `CODINGAGENT_STATE_SCHEMAS_STRICT=1`: verify no `NodeResultViolation` is raised and `retrieved_snippets` persists in the compiled LangGraph output state.

---

### SPEC-06 [High] — Close Protected-Path and Secret Redaction Bypasses in Read, Diff, Grep, Encoding, `SessionStore`, and Distiller

- **Addresses:** Phase 1 #3; §4 Secret Content & Prior-Session Memory; §9 Memory Sanitization.
- **Locations:**
  - `src/tools/state_tools.py:260-301` (`batched_file_read`)
  - `src/tools/patch_tools.py:45-70` (`generate_patch`)
  - `src/tools/system_tools.py:69-98` (`grep`)
  - `src/tools/_file_io.py:915-966` (`read_file_bytes`)
  - `src/tools/repo_read_tools.py:597-640` (`find_references`), `src/tools/ast_tools.py:151-267` (`ast_list_symbols`), `src/tools/git_tools.py:53-116` (`git_diff`, `git_log`)
  - `src/tools/_security.py:184` (`SAFE_COMMANDS`)
  - `src/core/memory/session_store.py:345-424,564-582` (`SessionStore.write_decisions_json`, `read_recent_decisions`)
  - `src/core/memory/distiller.py:319-322,487-635` (`distill_context`)
  - `src/core/indexing/vector_store.py:505-512` (`VectorStore.add_memory`)
  - `src/core/memory/advanced_features.py:58-67,161-220` (`TrajectoryLogger`)
  - `src/core/memory/security.py:18-59` (`scan_memory_content`), `src/core/context/context_builder.py:485,583,601,608-612`
- **Root Cause & Trigger:**
  1. `batched_file_read`, `generate_patch`, `find_references`, `ast_list_symbols`, and `git_diff` read files without checking `is_secret_path(p)` or `WorkspaceGuard`.
  2. Native `grep` (`system_tools.py:69-98`) only checks `is_secret_path(search_path)` on the root directory (`"."` is `False`) and runs `grep -r -n` across `.env`, `.ssh/*`, `*.pem`, `.netrc`, and `.pgpass`.
  3. `read_file_bytes` returns base64-encoded bytes and `bash_readonly("xxd .env")` returns hex dumps; neither matches `redact_secrets()` regexes.
  4. `SessionStore` (`session_store.py:345-424,564-582`) overrides `write_decisions_json` and `read_recent_decisions`, reading/writing `.codingAgent/decisions.json` directly from SQLite/disk without `redact_secrets`, `scan_memory_content`, or provenance metadata.
  5. `distill_context` (`distiller.py:487-635`) writes unredacted file contents to `.codingAgent/file_summaries.json`, `TASK_STATE.md`, `compaction_checkpoint.md`, and `VectorStore.add_memory(..., metadata=distilled_state)`.
  6. `memory/security.py:18-45` compiles threat regexes without `re.DOTALL`, and `context_builder.py:485,583,601` fails open if `scan_memory_content` raises an exception.
- **Implementation Specification:**
  1. **Enforce `is_secret_path` and `redact_secrets` in all read/diff/search tools:**
     - In `batched_file_read` (`state_tools.py:260-301`), `generate_patch` (`patch_tools.py:45-70`), `find_references` (`repo_read_tools.py:597-640`), `ast_list_symbols` (`ast_tools.py:151-267`), and `git_diff` (`git_tools.py:53-84`): check `is_secret_path(resolved_path)` (and `WorkspaceGuard.guard_operation("read", ...)`) before reading, and pass all returned content/diff strings through `redact_secrets()`.
     - In `grep` (`system_tools.py:69-98`): filter every matched line's file path through `is_secret_path(Path(matched_file))` before including it in `matches` (and pass `--exclude=.env* --exclude=*.pem --exclude=*.key --exclude=.netrc --exclude=.pgpass --exclude-dir=.ssh --exclude-dir=.aws --exclude-dir=.kube --exclude-dir=.git` to native `grep`).
     - In `read_file_bytes` (`_file_io.py:915-966`): decode `data.decode("utf-8", errors="ignore")` before base64 encoding and check `contains_secrets(decoded_text)`; if secrets are detected in the byte range, deny the raw byte read (`"Cannot read raw bytes of file containing credentials/secrets; use read_file for redacted text"`).
     - In `_security.py:119-196` and `_bash_exec.py`: remove `xxd`, `hexdump`, `od`, `base64`, `strings` from `SAFE_COMMANDS`, and in `_bash_exec.py` check file arguments of `SAFE_COMMANDS` against `is_secret_path`.
  2. **Delegate `SessionStore` decision methods & redact distiller/vector/trajectory outputs:**
     - In `SessionStore` (`session_store.py:345-424,564-582`), delegate `write_decisions_json` and `read_recent_decisions` directly to `self._store.write_decisions_json` and `self._store.read_recent_decisions`.
     - In `distiller.py`: skip files where `is_secret_path(p)` is `True` when building `file_summaries.json`, and run `redact_data` / `redact_secrets` on `file_summaries.json`, `TASK_STATE.md`, and `compaction_checkpoint.md`.
     - In `VectorStore.add_memory` (`vector_store.py:505-512`) and `TrajectoryLogger` (`advanced_features.py`): apply `redact_data(metadata)` before writing JSONL/JSON to disk.
  3. **Harden `memory/security.py` and `context_builder.py`:**
     - Compile threat regexes in `memory/security.py:18-45` with `re.IGNORECASE | re.DOTALL` and strip Unicode tag characters (`\U000e0000-\U000e007f`), soft hyphens (`\u00ad`), and directional isolates (`\u2066-\u2069`).
     - In `context_builder.py:485,583,601`, fail **closed** (omit the memory block) if `scan_memory_content` raises an exception, and escape `</prior_context>` / `</frozen_memory>` closing tags inside untrusted content.
- **Regression Test Specification (`tests/unit/test_secret_redaction_bypasses.py`):**
  - Verify `batched_file_read([".env"])`, `generate_patch(".env", "")`, `grep("SECRET", ".")`, and `git_diff(path=".env")` all block `.env` access.
  - Verify `read_file_bytes("config_with_sk_key.py")` refuses to return base64-encoded secrets.
  - Verify `SessionStore(workdir).write_decisions_json()` and `read_recent_decisions()` redact secrets, strip prompt-injection payloads (including multi-line `"disregard all\nprevious instructions"`), and attach provenance.

---

### SPEC-07 [High] — Fix `WorkspaceGuard` Dot-Segment and Symlink Bypasses and Block `delete_file(path=".")` Workspace Wipe

- **Addresses:** Phase 1 #3; §4 & §5 Code Modification Safety.
- **Locations:**
  - `src/core/orchestration/workspace_guard.py:59-84` (`is_protected`), `:86-120` (`is_secret`, `guard_operation`)
  - `src/tools/_file_io.py:181-186,509-578,624-634` (`write_file`, `delete_file`, `rename_file`)
  - `src/tools/_edit_tools.py:265-270,365-381,486-491,667-681`
- **Root Cause & Trigger:**
  1. `WorkspaceGuard.is_protected` converts `path` to `str(Path(path))` without normalizing `..` segments or resolving symlinks. Passing `path="sub/../.git"` produces `path_str="sub/../.git"`, which fails to match `path_str == ".git"`, `startswith(".git/")`, or `"/.git/" in path_str` — returning `False`! `_safe_resolve` then resolves it to `<workdir>/.git` and `delete_file` deletes the `.git` directory.
  2. Mutating tools call `guard.guard_operation(..., path, ...)` on the raw `path` string *before* `_safe_resolve(path, workdir)`, so a workspace symlink `link -> .env` or `link -> pyproject.toml` bypasses `WorkspaceGuard`.
  3. In `delete_file` (`_file_io.py:509-578`), `_safe_resolve(".", workdir)` returns `real_workdir`, `WorkspaceGuard.is_protected(".")` returns `False`, `_check_project_deny_write` is never called, and `shutil.rmtree(p)` (`:570`) deletes the entire workspace root directory.
- **Implementation Specification:**
  1. **Normalize and resolve paths in `WorkspaceGuard` (`src/core/orchestration/workspace_guard.py:59-120`):**
     - In `is_protected` and `is_secret`, normalize `path` with `os.path.normpath` and check **both** the lexical relative path (so symlink names like `.env` are caught) **and** the resolved path `(self.workspace_root / path).resolve()` relative to `self.workspace_root.resolve()` (so `sub/../.git` and symlinks `link -> .git` or `link -> .env` are caught).
  2. **Block workspace root deletion and enforce `_check_project_deny_write` in `delete_file` (`src/tools/_file_io.py:509-578`):**
     - After `p = _safe_resolve(path, workdir)`, reject deletion if `p == Path(workdir).resolve()` (`return {"status": "error", "error": "Cannot delete the workspace root directory."}`).
     - Call `_check_project_deny_write(p, workdir)` in `delete_file` (matching `write_file` and `rename_file`).
     - Run `WorkspaceGuard.guard_operation` on `p` (the resolved path) as well as the input `path` across `write_file`, `delete_file`, `rename_file`, and all `_edit_tools.py` functions.
- **Regression Test Specification (`tests/unit/test_workspace_guard_bypasses.py`):**
  - Test `WorkspaceGuard.is_protected("sub/../.git")`, `"sub/../.git/config"`, and a symlink `link_to_git -> .git`: all must return `True`.
  - Test `delete_file(path=".")` and `delete_file(path="sub/..")`: must return an error without deleting any files.

---

### SPEC-08 [High] — Fix Mutating Tool Guardrails, Read-Before-Write `Path.cwd()` Bug, Permission/Dry-Run Tables, and Directory/Step Rollback

- **Addresses:** Phase 2 #1; §4 Raw Registry & Mutator Controls; §5 Code Modification Safety; §7 Tool System Weaknesses.
- **Locations:**
  - `src/tools/guardrails.py:72-82` (`check_read_before_write`, `mark_file_read`)
  - `src/tools/constants.py:20-57,168-186` (`WRITE_TOOLS_REQUIRING_READ`, `MODIFYING_TOOLS`, `DRY_RUN_BLOCKED_TOOLS`, `IDEMPOTENT_TOOLS`)
  - `src/tools/tools_config.py:45-91` (`TOOL_PERMISSIONS`)
  - `src/tools/rollback_tools.py:32-45` (`WRITE_TOOL_NAMES`)
  - `src/core/orchestration/rollback_manager.py:109-119,309-312` (`RollbackManager`)
  - `src/core/orchestration/tool_execution_pipeline.py:585-611,944-967,1094-1110`
  - `src/tools/patch_tools.py:89-138` (`edit_code_block`), `src/tools/ast_tools.py:27-148` (`ast_rename`), `src/tools/lsp_tools.py:268-355` (`lsp_rename`), `src/tools/_edit_tools.py:374,675`, `src/tools/_file_io.py:524,650`
- **Root Cause & Trigger:**
  1. **`Path.cwd()` bug in `guardrails.check_read_before_write`**: `multiedit` (`_edit_tools.py:675`), `edit_by_line_range` (`:374`), `delete_file` (`_file_io.py:524`), and `rename_file` (`:650`) pass the unresolved `path` string to `check_read_before_write(path)` *before* `_safe_resolve(path, workdir)`. `guardrails.py:72` calls `Path(path).resolve()` against process `Path.cwd()`. When `workdir != Path.cwd()`, `Path(p).exists()` is `False`, so `multiedit` (which is also missing from `WRITE_TOOLS_REQUIRING_READ`) overwrites existing files in `workdir` without ever reading them! Conversely, if a same-named file exists in `Path.cwd()`, `check_read_before_write` checks whether the `cwd` file was read and falsely blocks edits to files in `workdir` that *were* read.
  2. **Self-marking RBW bypass**: `edit_code_block` (`patch_tools.py:115`) and `ast_rename` (`ast_tools.py:69`) call `mark_file_read(...)` immediately before `check_read_before_write(...)`.
  3. **Incomplete tool sets**:
     - `WRITE_TOOLS_REQUIRING_READ` (`constants.py:20-33`) omits `multiedit`, `edit_code_block`, `ast_rename`, `lsp_rename`, `format_file`.
     - `DRY_RUN_BLOCKED_TOOLS` (`constants.py:55-57`) omits `multiedit`, `edit_code_block`, `ast_rename`, `lsp_rename`, `format_file`, `create_directory`, `git_stash`, `git_restore`, `run_tests`, `run_js_tests`, `delegate_task`, `delegate_tasks_parallel`.
     - `WRITE_TOOL_NAMES` (`rollback_tools.py:32-45`) omits `edit_code_block`, `ast_rename`, `lsp_rename`, `format_file` (and lists non-existent `patch_directory`).
     - `TOOL_PERMISSIONS` (`tools_config.py:45-91`) omits `delegate_tasks_parallel` (should be `PROMPT`), `run_js_tests` / `run_tests_legacy` (should be `DANGER`), and 15 read-only tools (`read_file_chunk`, `read_file_bytes`, `batched_file_read`, `tail_log_file`, `repo_overview`, `find_files`, `ast_list_symbols`, `lsp_diagnostics`, `lsp_references`, `lsp_definition`, `lsp_symbols`, `lsp_hover`, `diff_state`, `list_snapshots`, `list_checkpoints`), which default to `WORKSPACE_WRITE` and fail in `READ_ONLY` mode.
     - `IDEMPOTENT_TOOLS` (`constants.py:168-186`) wrongly includes `edit_file`, `edit_file_atomic`, `edit_code_block`, `apply_patch`, `multiedit`, `rename_file`, `run_tests`, `run_tests_legacy`, `run_js_tests`.
  4. **Directory rollback data loss (`rollback_manager.py:109-119,309-312`)**: When `delete_file` deletes a directory, `snapshot_files` only records `content="__DIRECTORY__"` for the directory path; `rollback()` recreates an empty directory via `mkdir()`, losing all deleted files inside it.
  5. **Missing step transactions in `frontier_loop_node` & missing `dst_path` tracking on `rename_file`**: `frontier_loop_node` never calls `begin_step_transaction()`, so `verification_node.py:366` step rollback is a no-op; `tool_execution_pipeline.py:950` only records `src_path` (not `dst_path`) in `_session_modified_files` and `_session_read_files`.
- **Implementation Specification:**
  1. **Always pass resolved `str(p)` to `check_read_before_write` (`_edit_tools.py`, `_file_io.py`, `ast_tools.py`, `patch_tools.py`):**
     - Resolve `p = _safe_resolve(path, workdir)` first, then pass `str(p)` to `check_read_before_write(str(p))`.
     - Remove the premature `mark_file_read(...)` calls in `edit_code_block` (`patch_tools.py:115`) and `ast_rename` (`ast_tools.py:69`).
     - Replace process-global `_global_read_files` in `guardrails.py` with a `ContextVar`-backed per-session set (reset at the start of `run_agent_once_impl`).
  2. **Update `constants.py`, `tools_config.py`, and `rollback_tools.py`:**
     - Add `multiedit`, `edit_code_block`, `ast_rename`, `lsp_rename` to `WRITE_TOOLS_REQUIRING_READ` and `WRITE_TOOL_NAMES`.
     - Add all mutating, test-execution, and delegation tools to `DRY_RUN_BLOCKED_TOOLS`.
     - Remove non-idempotent mutators and test runners from `IDEMPOTENT_TOOLS`.
     - Add explicit `PermissionLevel.READ_ONLY` entries for all 15 read-only tools in `TOOL_PERMISSIONS`, `PermissionLevel.PROMPT` for `delegate_tasks_parallel` and `lsp_rename` / `ast_rename`, and `PermissionLevel.DANGER` for `run_js_tests` / `run_tests_legacy`.
  3. **Snapshot directory contents recursively in `RollbackManager.snapshot_files` (`rollback_manager.py:109-119`):**
     - When `p.is_dir()` and `p.exists()`, walk `p.rglob("*")` and snapshot every regular file inside `p` (in addition to directory markers) so `rollback()` restores all files inside a deleted directory.
     - In `frontier_loop_node.py`, call `orch.begin_step_transaction()` before executing the first mutating tool in a loop visit so `verification_node` can roll back broken edits when verification fails.
     - In `tool_execution_pipeline.py:944-967`, when `name == "rename_file"`, record both `src_path` and `dst_path` in `orch._session_modified_files`, `orch._affected_files`, and `orch._session_read_files`.
- **Regression Test Specification (`tests/unit/test_mutating_tool_guardrails.py`):**
  - With `os.chdir(other_dir)` and `orch = Orchestrator(working_dir=workspace_dir)`, test that `multiedit` and `edit_code_block` reject modifying an unread file in `workspace_dir`, and succeed after `read_file` is called on that file.
  - Test `delete_file` on a directory containing nested files, followed by `revert_last_tool()`: verify all nested files and their contents are restored.
  - Test `READ_ONLY` permission mode allows `read_file_chunk`, `batched_file_read`, `repo_overview`, `find_files`, and `lsp_diagnostics`, while `--dry-run` blocks `multiedit`, `edit_code_block`, `ast_rename`, and `lsp_rename`.

---

### SPEC-09 [High] — Close Shell Gate 3 Combined-Flag Bypasses, `git` Allowlist Abuse, and Arbitrary PID Probing

- **Addresses:** §3 & §8 Tool Execution Safety / Shell Command Injection.
- **Locations:**
  - `src/tools/_bash_exec.py:210-256` (`_check_shell_flags`), `:374-456` (`bash`), `:648-732` (`bash_readonly`), `:793-828` (`check_background_task`)
  - `src/tools/_security.py:119-280` (`SAFE_COMMANDS`, `GIT_SAFE_SUBCOMMANDS`, `CODE_EXEC_INTERPRETERS`, `CODE_EXEC_FLAGS`)
- **Root Cause & Trigger:**
  1. In `_bash_exec.py:426-433` and `:720-726`, Gate 3/4 checks inline code execution flags (`-c`, `-e`, `-r`, `--eval`, `--execute`) using exact string membership (`if part in CODE_EXEC_FLAGS:`). Combined short flags (`python3 -Bc "import os..."`, `python3 -Ic "..."`, `node -e"..."`) or `=` flags (`node --eval="..."`) bypass Gate 3 and auto-execute via `TEST_COMPILE_COMMANDS`.
  2. `git` is in `SAFE_COMMANDS` (`_security.py:155`). In `bash()`, any `git` subcommand (`git branch -D main`, `git checkout -- .`, `git clean -fd`) is auto-allowed without approval; in both `bash()` and `bash_readonly()`, `git` options before the subcommand (`git -c core.pager="sh -c ..." log`) bypass `GIT_SAFE_SUBCOMMANDS` / flag checks.
  3. `bash_readonly()` (`_bash_exec.py:648-660`) omits the quote-stripping normalization (`replace("'", "").replace('"', "")`) and `_check_command_denylist()` used in `bash()`.
  4. `check_background_task` (`_bash_exec.py:793-828`) calls `os.kill(pid, 0)` on any caller-supplied integer PID.
- **Implementation Specification:**
  1. **Parse combined short flags and `--flag=value` in Gate 3/4 (`_bash_exec.py:426-433,720-726`):**
     - For any interpreter in `CODE_EXEC_INTERPRETERS` (including versioned names like `python3.11`, `python3.12`, `os.path.basename(first_cmd)`):
       - Block if any argument `part == "-"` (stdin execution), `part.startswith(("--eval=", "--execute="))`, `part in CODE_EXEC_FLAGS`, or if `part.startswith("-") and not part.startswith("--")` and any character in `part[1:]` is in `{"c", "e", "r"}` (e.g. `-Bc`, `-Ic`, `-uc`, `-e...`).
  2. **Harden `git` subcommand and flag validation in `bash` and `bash_readonly` (`_bash_exec.py`, `_security.py`):**
     - Remove `"git"` from `SAFE_COMMANDS` in `_security.py:155` and handle `"git"` explicitly in both `bash()` and `bash_readonly()`:
       - Scan `cmd_parts[1:]` to reject dangerous top-level git flags (`-c`, `--config-env`, `--exec-path`, `--git-dir`, `--work-tree`, `--upload-pack`, `--receive-pack`) before or after the subcommand.
       - Identify the first non-option token as the git subcommand: auto-allow only `GIT_SAFE_SUBCOMMANDS` (`status`, `log`, `diff`, `show`, `rev-parse`); require tier-3 user approval for mutating/destructive git subcommands in `bash()` and block them in `bash_readonly()`.
     - Apply quote-stripping normalization and `_check_command_denylist` to `bash_readonly()` identically to `bash()`.
  3. **Track agent-spawned background PIDs (`_bash_exec.py:543-556,793-828`):**
     - Record spawned `proc.pid` values in a module-level `_SPAWNED_BG_PIDS: Set[int]` when `bash(run_in_background=True)` succeeds, and reject `check_background_task(task_id)` if `pid not in _SPAWNED_BG_PIDS`.
- **Regression Test Specification (`tests/unit/test_bash_gate_bypasses.py`):**
  - Verify `bash("python3 -Bc 'print(1)'")`, `bash("node --eval='1+1'")`, `bash("node -e'1+1'")`, and `bash_readonly("git -c core.pager=id log")` are all blocked or require approval.
  - Verify `check_background_task("1")` returns an error because PID `1` was not spawned by the agent.

---

### SPEC-10 [High] — Eliminate SSRF DNS Rebinding (TOCTOU), Unwrap IPv6 Transition Addresses, and Bound Response Streams

- **Addresses:** Phase 1 #5; §4 SSRF Redirect & Peer Validation.
- **Locations:** `src/tools/web_tools.py:40-81` (`_ALLOWED_SCHEMES`, `_is_ip_blocked`), `:110-165` (`_is_ssrf_blocked`), `:280-365` (`_fetch_with_validated_redirects`, `read_web_page`)
- **Root Cause & Trigger:**
  1. `_is_ssrf_blocked` calls `socket.getaddrinfo(host, None)` (`:139`), and then `_fetch_with_validated_redirects` calls `requests.get(current, ...)` (`:292`), which performs a second DNS lookup. An attacker-controlled `TTL=0` DNS record can resolve to a public IP on the first lookup and `127.0.0.1` / `169.254.169.254` on the second.
  2. `_is_ip_blocked` (`:42-81`) checks `addr in _METADATA_NET` without unwrapping `addr.ipv4_mapped`, `addr.sixtofour`, or `addr.teredo`, so `::ffff:169.254.169.254` and `2002:a9fe:a9fe::1` are not recognized as metadata/loopback IPs.
  3. `requests.get` without `stream=True` downloads arbitrarily large response bodies into memory before truncating at line 361, and allows `https://` -> `http://` scheme downgrades on 301/302 redirects.
- **Implementation Specification:**
  1. **Unwrap IPv6 transition addresses in `_is_ip_blocked` (`src/tools/web_tools.py:42-81`):**
     - If `isinstance(addr, ipaddress.IPv6Address)`: check `addr.ipv4_mapped`, `addr.sixtofour`, and `addr.teredo`; if any is non-`None`, recursively call `if _is_ip_blocked(str(unwrapped)): return True`.
  2. **Pin resolved IP or verify connected socket peer + stream bounded bytes (`src/tools/web_tools.py:280-355`):**
     - Use `stream=True` in `requests.get(..., allow_redirects=False, stream=True)`.
     - Immediately after receiving headers (before reading body chunks or following redirects), inspect the underlying socket peer address (`sock = getattr( getattr(resp.raw, "_connection", None), "sock", None)` -> `peer_ip = sock.getpeername()[0]`) and verify `not _is_ip_blocked(peer_ip)`; if blocked, call `resp.close()` and raise/return an SSRF error. (Additionally, resolve the IP once in `_is_ssrf_blocked` and connect via a custom `HTTPAdapter` / `urllib3` resolver hook that pins the validated IP for that request hop).
     - Reject `https://` -> `http://` scheme downgrades across redirect hops (`if parsed_current.scheme == "https" and parsed_next.scheme == "http": return SSRF/downgrade error`).
     - Read the response body in chunks (`resp.iter_content(chunk_size=8192, decode_unicode=True)`) up to a hard byte/char cap (`_MAX_WEB_PAGE_BYTES = 512 * 1024`) and close `resp` in a `finally` block.
- **Regression Test Specification (`tests/unit/test_web_tools_ssrf.py`):**
  - Test `_is_ip_blocked("::ffff:169.254.169.254")`, `_is_ip_blocked("::ffff:127.0.0.1")`, and `_is_ip_blocked("2002:a9fe:a9fe::1")` all return `True`.
  - Simulate a DNS rebinding scenario where `getaddrinfo` returns `93.184.216.34` during pre-check but the connected socket `getpeername()` returns `("127.0.0.1", 8080)`: verify `read_web_page` aborts before reading the response body.
  - Test that an `https://` URL redirecting to `http://` is rejected, and a streaming response exceeding 512 KB stops reading at the cap without buffering the entire stream.

---

### SPEC-11 [High] — Fix `batch()` Shared `Context` and Thread-Local Orchestrator Loss, and Subagent `active_agent` Data Race

- **Addresses:** §4 Parallel Delegation Context & Concurrency Safety.
- **Locations:**
  - `src/tools/batch_tools.py:36-45,82-87,118-129`
  - `src/core/orchestration/tool_execution_pipeline.py:725,1193`
  - `src/tools/subagent_tools.py:615,792,1034-1046`
- **Root Cause & Trigger:**
  1. `tool_execution_pipeline.py:1193` sets `set_batch_orchestrator(orch)` on the calling thread (`_batch_context = threading.local()`), then submits `batch()` to `orch._tool_executor` (`:1201`), where `_batch_context.orchestrator` is `None`.
  2. `batch_tools.py:118-129` copies `_ctx = _cv.copy_context()` **once** outside the loop and calls `pool.submit(_ctx.run, _run_one, call)` for all calls in the batch, raising `RuntimeError: cannot enter context: <Context ...> is already entered` whenever `len(tool_calls) > 1`.
  3. In `subagent_tools.py:615,792`, `delegate_tasks_parallel` runs up to 6 subagents concurrently against `parent_orchestrator`, mutating `parent_orchestrator.active_agent = agent_def` and resetting it to `None` in `finally`, causing concurrent subagents to overwrite each other's `allowed_tools` enforcement in `tool_execution_pipeline.py:725`.
- **Implementation Specification:**
  1. **Use `ContextVar` instead of `threading.local` in `batch_tools.py` and copy `Context` per task (`src/tools/batch_tools.py:36-45,118-129`):**
     - Replace `_batch_context = threading.local()` with `_BATCH_ORCHESTRATOR: ContextVar[Optional[Any]] = ContextVar("_BATCH_ORCHESTRATOR", default=None)` so `_ctx.run` in `tool_execution_pipeline.py:735` automatically propagates `orch` into the worker thread.
     - In `batch_tools.py:120-129`, call `_cv.copy_context().run` **inside** the submission loop (`futures = [pool.submit(_cv.copy_context().run, _run_one, call) for call in tool_calls]`).
  2. **Move `active_agent` to a `ContextVar` (`src/tools/subagent_tools.py:615,792`, `src/core/orchestration/tool_execution_pipeline.py:725`):**
     - Store `active_agent` in a `ContextVar` (`_ACTIVE_AGENT_CTX`) alongside the attribute fallback on `orch` so concurrent subagents in `delegate_tasks_parallel` each have isolated `active_agent` tool restrictions.
- **Regression Test Specification (`tests/unit/test_parallel_delegation_context.py`):**
  - Execute `batch` through `Orchestrator.execute_tool` with 4 parallel `read_file` calls: verify all 4 succeed without `"batch requires an active orchestrator context"` or `RuntimeError: cannot enter context`.
  - Run `delegate_tasks_parallel` with a `reviewer` (read-only) subagent and an `operational` subagent synchronized on a barrier: verify the `reviewer` subagent is still blocked from calling `write_file`.

---

### SPEC-12 [High] — Fix SQLite FTS5 `rowid` Desynchronization on Session Delete/Revert and `JsonlSessionStore` Parity

- **Addresses:** Phase 1 #6a; §9 Memory System Evaluation.
- **Locations:**
  - `src/core/memory/sqlite_store_schema.py:9,77,100-140`
  - `src/core/memory/sqlite_session_store.py:800-830` (`delete_session`)
  - `src/core/memory/sqlite_store_collaborators.py:554-575,639-655` (`revert_session`)
  - `src/core/memory/sqlite_store_queries.py:14,26` (`search`, `search_mistakes`)
  - `src/core/memory/jsonl_session_store.py:461-476,581-591,1105,1138-1163`, `src/core/memory/jsonl_store_helpers.py:62-67`
- **Root Cause & Trigger:**
  1. `messages` and `mistakes` use `id INTEGER PRIMARY KEY AUTOINCREMENT`, while `messages_ai` / `mistakes_ai` triggers (`sqlite_store_schema.py:113-135`) insert into `messages_fts` / `mistakes_fts` without specifying `rowid = new.id`. After `delete_session` or `revert_session` deletes rows, `messages.id` never reuses deleted IDs whereas `messages_fts.rowid` resets to `MAX(rowid) + 1`, so `JOIN messages m ON m.rowid = fts.rowid` (`sqlite_store_queries.py:14,26`) joins the wrong rows or returns empty results.
  2. `delete_session` (`sqlite_session_store.py:818-827`) never deletes from `mistakes_fts` and never refreshes `.codingAgent/decisions.json`.
  3. `revert_session` (`sqlite_store_collaborators.py:572,650`) deletes from `session_snapshots` without deleting matching rows in `session_snapshot_rows` or `messages_fts`.
  4. `JsonlSessionStore` lacks `search_mistakes`, leaves snapshot/state/decision sidecars behind on `delete_session`, and `parse_session_id_from_jsonl_filename` (`jsonl_store_helpers.py:62-67`) truncates session IDs containing dots (`rsplit(".", 1)[0]`).
- **Implementation Specification:**
  1. **Synchronize FTS5 `rowid` with base table `id` (`src/core/memory/sqlite_store_schema.py:113-140`):**
     - Update `messages_ai` and `mistakes_ai` triggers to explicitly insert `rowid`:
       `INSERT INTO messages_fts (rowid, session_id, role, content) VALUES (new.id, new.new_session_id, ...)`
     - Add `AFTER DELETE` triggers (`messages_ad`, `mistakes_ad`) that delete `WHERE rowid = old.id` from `messages_fts` and `mistakes_fts` whenever rows are deleted from `messages` or `mistakes`.
  2. **Complete `delete_session` and `revert_session` cleanup (`sqlite_session_store.py:800-830`, `sqlite_store_collaborators.py:554-655`):**
     - In `delete_session`, also execute `DELETE FROM mistakes_fts WHERE session_id = ?` and call `self.write_decisions_json()` after commit.
     - In `_revert_to_snapshot` and `_revert_session_keep_messages`, delete matching `session_snapshot_rows` before deleting `session_snapshots`, and delete corresponding `messages_fts` rows.
  3. **Fix `JsonlSessionStore` parity (`jsonl_session_store.py`, `jsonl_store_helpers.py`):**
     - Implement `search_mistakes(query, limit)` in `JsonlSessionStore`.
     - In `JsonlSessionStore.delete_session`, unlink `snapshots/{session_id}.*.json`, `state/{session_id}.json`, and rewrite `decisions.json`.
     - Fix `parse_session_id_from_jsonl_filename` to strip `.jsonl` first and only strip `.N` rotation suffixes when the suffix after the last dot is all digits (`stem.rsplit(".", 1)[1].isdigit()`).
- **Regression Test Specification (`tests/unit/test_sqlite_session_delete.py`):**
  - Create Session A (insert message `"alpha"`), delete Session A, create Session B (insert message `"beta"`), and call `store.search("beta")`: verify `search("beta")` returns Session B's `"beta"` message (Previously returned `[]` because `messages.id=2` while `messages_fts.rowid=1`!).
  - Verify `delete_session` removes rows from `mistakes_fts` and updates `.codingAgent/decisions.json`.
  - Verify `JsonlSessionStore` preserves session IDs containing dots (e.g. `"release-1.2.3"`).

---

### SPEC-13 [High] — Fix `analysis_node` Symbol-Graph `TypeError`, Unindexed `VectorStore`, and Incremental Index Refresh Defects

- **Addresses:** Phase 1 #6b, #6c; §8 Repository Awareness Gaps.
- **Locations:**
  - `src/core/orchestration/graph/nodes/analysis_node.py:172-188` (`_is_already_indexed`), `:354-361` (vector search), `:479-513` (`call_graph` / `test_map`)
  - `src/core/indexing/repo_indexer.py:203` (`parse_python_file`), `:563-603` (`_flat_symbols_for_file`), `:606-683` (`refresh_file_in_index`)
  - `src/core/indexing/symbol_graph.py:307,321,335-339` (`update_file`, `remove_file`)
  - `src/core/orchestration/graph/nodes/repo_context.py:41-61,125-156` (`_ensure_repo_index`, `refresh_index_after_write`)
- **Root Cause & Trigger:**
  1. In `analysis_node.py:507`, `callers = sg.find_calls(sym)` returns `List[Dict[str, Any]]`, and `', '.join(callers)` raises `TypeError: sequence item 0: expected str instance, dict found`. The `except Exception:` block (`:510-513`) catches the `TypeError` and resets `call_graph_data = {}` and `test_map_data = {}`!
  2. `analysis_node.py:354-361` calls `index_repository(working_dir)` and `VectorStore(working_dir).search(task, limit=10)`, but nothing ever calls `vs.index_code(...)`, so `.codingAgent/vectorstore/symbols.json` is never created and `vs.search()` always returns `[]`.
  3. `refresh_file_in_index` (`repo_indexer.py:674`) checks `if meta and "hashes" in meta:` instead of `"file_hashes"`, `_flat_symbols_for_file` (`:563-603`) omits `start_line`, `end_line`, and `docstring`, `parse_python_file` (`:203`) ignores `ast.AsyncFunctionDef`, and deleting a file (`not p.exists()` at `:624`) returns `False` instead of removing the deleted file's symbols and file entry from `repo_index.json`.
  4. `SymbolGraph.update_file` (`symbol_graph.py:321`) keys `self.file_hashes` by `str(p)` (absolute), whereas `remove_file` (`:335-339`) pops `rel_path` (relative).
  5. `_ensure_repo_index` (`repo_context.py:41-61`) caches `resolved in _INDEXED_WORKDIRS` forever without checking `_compute_workspace_revision`, and `_is_already_indexed` (`analysis_node.py:172-188`) does not verify that `repo_index.json` exists on disk.
- **Implementation Specification:**
  1. **Fix `analysis_node.py:505-513` and index `VectorStore` (`analysis_node.py:354-365`):**
     - Format caller dicts as strings: `caller_strs = [f"{c.get('file', '?')}:{c.get('line', '?')}" if isinstance(c, dict) else str(c) for c in callers[:5]]` before `", ".join(caller_strs)`.
     - After `idx = index_repository(working_dir)` at line 354, call `vs.index_code(idx.get("symbols", []))` so `vs.search(task, limit=10)` searches the newly indexed symbols.
     - In `_is_already_indexed` (`:172-188`), require `(Path(working_dir) / ".codingAgent" / "repo_index.json").exists()`.
  2. **Fix `repo_indexer.py` incremental refresh and async functions:**
     - In `parse_python_file` (`:203`), check `isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))`.
     - In `_flat_symbols_for_file` (`:563-603`), include `"start_line": item.get("start_line") or item.get("line")`, `"end_line": item.get("end_line")`, and `"docstring": item.get("docstring")` in every symbol dict.
     - In `refresh_file_in_index` (`:624-680`), when `not p.exists()`, remove `rel_path` from `files`, `symbols`, and `meta["file_hashes"]` and write the updated index; when `p.exists()`, update `meta.setdefault("file_hashes", {})[rel_path] = _file_hash(p)`.
  3. **Fix `SymbolGraph.remove_file` key and `_ensure_repo_index` freshness (`symbol_graph.py:335-339`, `repo_context.py:41-61,125-156`):**
     - In `SymbolGraph.remove_file`, pop both `str(Path(file_path).resolve())` and `rel_path` from `self.file_hashes`.
     - In `_ensure_repo_index`, compare `_compute_workspace_revision(workdir)` against a stored revision dict instead of a boolean set.
- **Regression Test Specification (`tests/unit/test_analysis_node_symbol_graph.py`, `tests/unit/test_repo_indexer_schema.py`):**
  - Run `analysis_node` on a workspace where function `foo()` calls `bar()`: verify `call_graph` and `test_map` are populated in output state (not wiped to `{}` by `TypeError`) and `vs.search()` returns matching symbols.
  - Test `refresh_file_in_index` on a modified file with `async def` and a docstring, and on a deleted file: verify `start_line`/`docstring` are preserved, `meta["file_hashes"]` is updated, and deleted file symbols are removed from `repo_index.json`.

---

### SPEC-14 [High] — Fix Provider Circuit Breaker Primary-Reset Bug, `HALF_OPEN` Probe Guard, and Anthropic/Ollama Streaming

- **Addresses:** §6 Provider Fallback, Circuit Breaker, and Anthropic/Ollama Adapters.
- **Locations:**
  - `src/core/inference/llm_manager.py:1245-1275` (`CircuitBreaker._current_state`), `:1405-1470` (`call_model` fallback & breaker update)
  - `src/core/inference/adapters/anthropic_adapter.py:82,112` (`base_url`), `:400-416` (`_build_payload`), `:503-523` (`_chat_internal`)
  - `src/core/inference/adapters/ollama_adapter.py:539-585,713,825-835`
  - `src/core/inference/adapters/openai_compat_adapter.py:686-695`
  - `src/core/inference/runtime_call.py:168-188`
- **Root Cause & Trigger:**
  1. **Primary circuit breaker reset by secondary fallback (`llm_manager.py:1445-1468`)**: When the primary provider (`_cb_key`) fails and `_get_fallback_chain().call(...)` succeeds on a secondary provider, `res` is overwritten with the secondary provider's `{"ok": True, ...}` response. Line 1463 then calls `_update_circuit_breaker_for_result(provider_key=_cb_key, result=res, ...)`, which invokes `record_success()` on the **failed primary provider** (`_cb_key`), resetting its failure counter to `0` so the primary breaker never opens!
  2. **Unreachable `HALF_OPEN` single-probe guard (`llm_manager.py:1250-1261`)**: The first call to `_current_state()` after `recovery_timeout` mutates `self._state = self.HALF_OPEN` and sets `self._probe_in_flight = True`. Subsequent concurrent calls see `self._state == self.HALF_OPEN` (skipping `if self._state == self.OPEN:`) and return `HALF_OPEN`, allowing unlimited concurrent requests through a half-open breaker.
  3. **Broken `AnthropicAdapter` streaming (`anthropic_adapter.py:400-416,503-523`)**: `_build_payload` never adds `"stream": True` when `stream=True`, and `_chat_internal` returns the raw `Response` to `_consume_sse_stream` (`llm_manager.py:1327`), which only parses OpenAI `choices[0].delta.content` chunks rather than Anthropic `content_block_delta` / `message_delta` events.
  4. **Broken `OllamaAdapter` streaming (`ollama_adapter.py:539-550`, `runtime_call.py:180`)**: `OllamaAdapter.generate(stream=True)` returns a dict `{"ok": True, "choices": [], "raw": <generator>}`, which lacks `.iter_lines()`, so `runtime_call.py:180` returns empty `choices: []` without consuming the stream.
  5. **Misplaced `tool_calls` in `generate()` (`openai_compat_adapter.py:686-695`, `ollama_adapter.py:567-575`)**: Places `tool_calls` at `choices[0]["tool_calls"]` instead of `choices[0]["message"]["tool_calls"]`.
- **Implementation Specification:**
  1. **Record primary failure before running cross-provider fallback (`src/core/inference/llm_manager.py:1418-1468`):**
     - When the primary provider fails (`not res.get("ok")`) and model fallback did not handle it, call `_update_circuit_breaker_for_result(provider_key=_cb_key, result=res, ...)` **before** invoking `_get_fallback_chain().call(...)`, and set `_cb_handled = True` so the secondary provider's result never updates `_cb_key`'s breaker (the fallback chain already updates the secondary provider's own breaker).
     - Also resolve `_cb_key = (provider or active_provider_name or "").lower().strip()` so `provider=None` still uses the active provider's circuit breaker.
  2. **Fix `CircuitBreaker` `HALF_OPEN` probe admission (`src/core/inference/llm_manager.py:1245-1280`):**
     - Add an atomic `allow_request() -> bool` method on `CircuitBreaker`: under `self._lock`, if `_state == CLOSED` return `True`; if `_state == OPEN` and `time.time() - _opened_at >= recovery_timeout` and not `_probe_in_flight`, transition `_state = HALF_OPEN`, set `_probe_in_flight = True`, and return `True`; if `_state == HALF_OPEN` and `_probe_in_flight` is `True`, return `False` (treat as open for concurrent callers until the single probe calls `record_success()` or `record_failure()`).
  3. **Support streaming in `AnthropicAdapter` and `OllamaAdapter`:**
     - In `AnthropicAdapter._build_payload` (`:400-416`), accept `stream: bool = False` and set `payload["stream"] = True` when `stream=True`. In `_consume_sse_stream` (`llm_manager.py:1327-1375` and `streaming.py`), parse Anthropic SSE event types (`message_start`, `content_block_start`, `content_block_delta` with `text_delta` / `input_json_delta`, `message_delta`, `message_stop`) alongside OpenAI SSE chunks. Also honor caller-supplied `base_url` in `AnthropicAdapter.__init__`.
     - In `runtime_call.py:180`, handle `OllamaAdapter`'s streaming generator (`isinstance(result, dict) and result.get("raw") is not None`) by iterating the generator chunks, publishing `model.token` events, and assembling the final `choices` and token counts.
     - In `openai_compat_adapter.py:686-695` and `ollama_adapter.py:567-575`, include `"tool_calls"` inside `choice_obj["message"]["tool_calls"]` as well as `choice_obj["tool_calls"]` for backward compatibility.
- **Regression Test Specification (`tests/unit/test_circuit_breaker_fallback.py`, `tests/unit/test_anthropic_adapter_messages.py`):**
  - Configure primary provider to fail 3 times (`failure_threshold=3`) while secondary provider succeeds: verify primary provider's `failure_count` reaches `3` and its circuit breaker transitions to `OPEN` despite secondary fallback succeeding on every call.
  - Verify that when a `CircuitBreaker` transitions to `HALF_OPEN`, the first caller is admitted (`allow_request() == True`) and a second concurrent caller before probe completion is rejected (`allow_request() == False`).
  - Test `AnthropicAdapter` and `OllamaAdapter` with `stream=True`: verify streamed text chunks and tool calls are assembled into the final response and emitted as token events.

---

### SPEC-15 [High] — Fix Server `working_dir=None` Bypass, Pre-Auth Filesystem Oracle, Session History Hydration, `EventBus` Wiring, and Executor Leaks

- **Addresses:** Phase 1 #4; §4 Server Workspace Confinement; §6 Server Cancellation; §9 Session Identity Propagation; §12 Server Queue.
- **Locations:**
  - `src/server/task_endpoints.py:85-116` (`validate_working_dir_within_roots`), `:168-195` (`TaskCreateRequest`), `:206-315` (`_get_or_create_orchestrator`, `_run_task_thread`), `:343-375` (`submit_task`), `:453-463` (`list_tasks`)
  - `src/core/orchestration/orchestrator_bootstrap.py:52-64` (`_shutdown_executors`)
  - `src/server/app.py:310-336` (`PATCH /scheduler/jobs/{job_id}`)
  - `src/core/scheduler/worker.py:155-169`
- **Root Cause & Trigger:**
  1. `validate_working_dir_within_roots(None)` (`task_endpoints.py:93-94`) returns `None`. When `POST /task` omits `"working_dir"`, `_get_or_create_orchestrator(None, model)` defaults to the server process's CWD even when `CODINGAGENT_ALLOWED_WORKSPACE_ROOTS` is configured to a restricted directory.
  2. `TaskCreateRequest._validate_working_dir` (`:178-194`) runs during FastAPI request body parsing *before* `_deps.require_admin_auth(request)` (`:348`), allowing unauthenticated callers to probe whether arbitrary paths and symlinks exist on the host (`422` vs `401`).
  3. `_run_task_thread` (`:263-295`) creates a new `Orchestrator` on every task without loading prior messages for `session_id` from `SessionStore`, sets `orch.event_bus = bus` *after* `bootstrap_orchestrator` bound internal services to a private `EventBus`, and never calls `orch.close()` in a `finally` block (leaking `_tool_executor` and `_graph_executor` threads on every task). Additionally, `atexit.register(_shutdown_executors)` in `orchestrator_bootstrap.py:64` is indented inside `_shutdown_executors()`.
  4. `list_tasks` (`:457-460`) sorts by `r.get("started_at") or 0.0`, sorting queued `ACCEPTED` tasks (`started_at=None`) after all completed tasks, and `_TASK_REGISTRY` has no max-entries cap within the 1-hour TTL.
  5. `PATCH /scheduler/jobs/{job_id}` (`src/server/app.py:316-325`) calls `await request.json()` before `require_scheduler_admin_auth` and accepts `interval <= 0`, causing a tight-loop DoS in `scheduler/worker.py:163`.
- **Implementation Specification:**
  1. **Validate default `working_dir` against allowed roots & move filesystem checks after auth (`src/server/task_endpoints.py:85-116,168-195,343-360`):**
     - Remove filesystem `resolve()` / `is_dir()` checks from `TaskCreateRequest._validate_working_dir` so Pydantic only validates type/length before authentication.
     - After `_deps.require_admin_auth(request)` succeeds in `submit_task`: if `body.working_dir is None`, default it to `str(get_allowed_workspace_roots()[0])` and validate it through `validate_working_dir_within_roots(effective_working_dir)` so tasks always execute inside an approved workspace root.
  2. **Pass `event_bus` at `Orchestrator` construction, hydrate `session_id` history, and close `orch` in `finally` (`src/server/task_endpoints.py:206-315`, `src/core/orchestration/orchestrator_bootstrap.py:52-64`):**
     - Pass `event_bus=bus` into `Orchestrator(working_dir=working_dir, event_bus=bus)` (and update `Orchestrator.__init__` to use the supplied `event_bus` before `bootstrap_orchestrator` runs).
     - Before calling `orch.run_agent_once`, load prior conversation messages for `session_id` from `orch.session_store` / `orch.msg_mgr` and prepend them to `messages` so multi-turn tasks sharing a `session_id` preserve context.
     - Wrap `_run_task_thread` execution in `try: ... finally: orch.close()`.
     - Unindent `atexit.register(_shutdown_executors)` in `src/core/orchestration/orchestrator_bootstrap.py:64` so it is registered during `bootstrap_orchestrator`.
  3. **Fix `list_tasks` sorting, bound `_TASK_REGISTRY`, and harden scheduler `PATCH` (`task_endpoints.py`, `app.py`):**
     - Record `"created_at": time.time()` on task creation and sort `list_tasks` by `r.get("started_at") or r.get("created_at") or 0.0`. Cap `_TASK_REGISTRY` terminal records (e.g. `_TASK_MAX_HISTORY = 1000`, evicting oldest terminal records first).
     - In `PATCH /scheduler/jobs/{job_id}` (`src/server/app.py:310-336`), call `require_scheduler_admin_auth` **before** `await request.json()`, and reject `interval < 1` with `HTTPException(status_code=400, detail="interval must be >= 1")`.
- **Regression Test Specification (`tests/unit/test_task_server_safeguards.py`):**
  - Set `CODINGAGENT_ALLOWED_WORKSPACE_ROOTS=/tmp/allowed_root` while CWD is `/Users/...`: verify `POST /task` with `{"task": "hi"}` (omitting `working_dir`) either resolves `working_dir` to `/tmp/allowed_root` or rejects if CWD is outside allowed roots.
  - Verify unauthenticated `POST /task` with `{"task": "hi", "working_dir": "/etc/passwd"}` returns `401 Unauthorized` (not `422`).
  - Submit two sequential tasks with the same `session_id`: verify the second task receives the first task's message history and `orch.close()` is called after each task.

---

### SPEC-16 [High] — Enforce WebSocket and SSE Session Isolation, Align Event Names, and Bound Subscriptions/Metrics

- **Addresses:** §12 Server SSE & WebSocket Reliability and Tenancy.
- **Locations:**
  - `src/server/websocket_handler.py:50-56,190-245`
  - `src/server/sse_adapter.py:61-67`
  - `src/server/event_subscriptions.py:6-20` (`DEFAULT_SERVER_EVENT_TYPES`)
  - `src/server/metrics.py:15,94-99,147-155`
  - `src/core/orchestration/event_bus.py`
- **Root Cause & Trigger:**
  1. `websocket_handler.py:52-55` checks `if isinstance(payload, dict) and "session_id" in payload: if session_id != "all" and payload.get("session_id") != session_id: return`. Because `tool.execute.*`, `model.*`, and `log.new` payloads do not include `"session_id"`, `"session_id" in payload` is `False` and **every WebSocket client receives tool/model/log events from all other sessions**.
  2. Conversely, `sse_adapter.py:63-66` sets `payload["session_id"] = "unknown"` when missing, so SSE clients subscribed to `/session/<real_id>/events` **never receive** `tool.*` or `model.*` events.
  3. `DEFAULT_SERVER_EVENT_TYPES` (`event_subscriptions.py:6-20`) subscribes to `"tool.start"`, `"tool.end"`, `"llm.response"`, `"llm.token"`, `"log"`, whereas the backend publishes `"tool.execute.start"`, `"tool.execute.finish"`, `"model.response"`, `"model.token"`, `"log.new"`.
  4. `websocket_handler.py:190-202` allows a client to send unlimited `{"type": "subscribe", "event": "<arbitrary>"}` frames, and `metrics.py:94-99` stores `(session_id, event_name)` keys in `_CLIENT_DROPPED_EVENT_COUNTERS` without bound.
- **Implementation Specification:**
  1. **Attach `session_id` via `ContextVar` in `EventBus.publish` (`src/core/orchestration/event_bus.py`, `src/server/task_endpoints.py`):**
     - Define `_CURRENT_SESSION_ID: ContextVar[Optional[str]] = ContextVar("_CURRENT_SESSION_ID", default=None)` and set it in `_run_task_thread` (and `Orchestrator.run_agent_once`).
     - In `EventBus.publish(event_name, payload)`, if `isinstance(payload, dict)` and `"session_id"` not in `payload` and `_CURRENT_SESSION_ID.get()` is set, attach `"session_id": _CURRENT_SESSION_ID.get()`.
  2. **Require strict `session_id` match in both `websocket_handler.py` and `sse_adapter.py`:**
     - In `websocket_handler.py:50-56` and `sse_adapter.py:61-67`: only deliver an event when `session_id == "all"` (which should only be permitted when admin auth is verified) **or** `payload.get("session_id") == session_id`. Never broadcast events lacking `"session_id"` to specific session streams.
  3. **Align `DEFAULT_SERVER_EVENT_TYPES` and bound dynamic WS subscriptions & metric cardinality (`event_subscriptions.py`, `websocket_handler.py`, `metrics.py`):**
     - Include `"tool.execute.start"`, `"tool.execute.finish"`, `"tool.execute.error"`, `"model.response"`, `"model.token"`, `"log.new"` in `DEFAULT_SERVER_EVENT_TYPES`.
     - Restrict WebSocket `subscribe` control frames to an allowlist of known event names (or cap at 32 subscriptions per connection) and cap `_CLIENT_DROPPED_EVENT_COUNTERS` at 1,000 keys.
- **Regression Test Specification (`tests/unit/test_ws_sse_session_isolation.py`):**
  - Connect two WebSocket clients (`session_a` and `session_b`) and publish a `tool.execute.start` event inside `session_a`'s context: verify `session_a` receives the event on both WS and SSE, and `session_b` receives nothing.

---

### SPEC-17 [High] — Fix Config Hot-Reload No-Op Provider Re-Init, Premature `clear_changed()`, and Instruction Loader Drift

- **Addresses:** §11 Config Reload Lifecycle & `.agent`/`.codingAgent` Reconciliation.
- **Locations:**
  - `src/core/orchestration/orchestrator_config_reload.py:65-105` (`_on_config_reloaded`)
  - `src/core/inference/llm_manager.py:927,1027` (`ProviderManager.initialize`)
  - `src/core/config_loader.py:155-215` (`load_merged_config`, `get_global_config`, `reload_if_pending`)
  - `src/core/config_hot_reload/__init__.py:30-38,150-240` (`ConfigReloader`)
  - `src/core/context/instruction_files.py:44-56`, `src/core/orchestration/instruction_loader.py:27-33`
- **Root Cause & Trigger:**
  1. `_on_config_reloaded` (`orchestrator_config_reload.py:82-100`) calls `_ensure_provider_manager_initialized_sync()`, which immediately returns because `_provider_manager._initialized` is already `True` (`llm_manager.py:927,1027`). Editing `providers.json` at runtime never updates registered providers.
  2. `load_merged_config()` (`config_loader.py:165`) calls `reloader.clear_changed()` without updating `_cached_config`. Any caller that invokes `load_merged_config(working_dir=...)` directly clears `_pending_reload = False`, so `get_global_config()` and `reload_if_pending()` never see the change and serve stale `_cached_config` for 300s.
  3. `_on_config_reloaded` (`orchestrator_config_reload.py:70`) replaces `orch.tool_registry = example_registry()`, wiping out dynamically registered MCP tools and project tool overrides.
  4. `instruction_loader.py:27-33` checks `AGENT.md` (singular) and omits `AGENTS.md` (plural), whereas `instruction_files.py:44-56` checks `AGENTS.md` (plural) and omits `AGENT.md`, and neither redacts secrets from instruction files or git diffs.
- **Implementation Specification:**
  1. **Add `ProviderManager.reload()` (`src/core/inference/llm_manager.py:925-960`, `orchestrator_config_reload.py:82-100`):**
     - Implement `ProviderManager.reload()` (or `initialize(force=True)`) that resets `self._initialized = False` and reloads `providers.json`, and call it from `_on_config_reloaded`.
  2. **Only call `reloader.clear_changed()` when `_cached_config` is updated (`src/core/config_loader.py:155-215`):**
     - Remove `reloader.clear_changed()` from `load_merged_config()`; call `reloader.clear_changed()` only inside `get_global_config()` / `reload_if_pending()` when `_cached_config` is actually refreshed (and update `_cached_config` inside `ConfigReloader.load()`).
  3. **Preserve MCP tools & project overrides on tool registry reload (`orchestrator_config_reload.py:65-80`) and unify instruction discovery:**
     - Re-apply `get_project_tool_overrides` and re-register active `mcp_manager` tools after rebuilding `orch.tool_registry`.
     - Consolidate `instruction_loader.py` and `instruction_files.py` to support both `AGENTS.md` and `AGENT.md` in `.codingAgent/` and workspace root (stopping at workspace/git root rather than `/`), and pass loaded instruction/diff content through `redact_secrets()`.
- **Regression Test Specification (`tests/unit/test_phase2_item5_config_reload.py`):**
  - Modify `providers.json` while an `Orchestrator` is live, trigger `reload_if_pending()`, and verify `_provider_manager` reflects the updated provider/model list and `get_global_config()` returns the new config even if `get_mcp_config(workdir)` was called first.

---

### SPEC-18 [High] — Wire TUI `active_role` End-to-End, Fix TUI `/mcp add` Path, Fix `SessionScreen` Offset/Focus, and Protect API Keys

- **Addresses:** §11 TUI Role Selection, `/mcp add`, and Session List Index Offset.
- **Locations:**
  - `tui/tui_src/ui/_bridge_agent.py:86-121` (and mirror `tui/src/ui/...`)
  - `tui/tui_src/ui/app.py:129,147-148,696-707`
  - `src/core/orchestration/inference_loop.py:89-160`
  - `src/core/orchestration/graph/nodes/frontier_loop_node.py:263-265`, `src/core/orchestration/graph/nodes/perception_node.py:216,247`
  - `tui/tui_src/ui/_app_slash_commands_mixin.py:431-495` (`_slash_mcp`)
  - `tui/tui_src/ui/screens/session_screen.py:123-127,143-148,174-194,219-225,241-250`, `tui/tui_src/ui/screens/session_list.py:187-212`
  - `tui/tui_src/ui/config_writer.py:43,130-167`
- **Root Cause & Trigger:**
  1. **TUI role ignored at execution**: `_bridge_agent.py:115` hardcodes `tools = self._orchestrator.get_tools_for_role("operational")` instead of `prompt_name`; `run_agent_once_impl` ignores its `tools` parameter; and `frontier_loop_node.py:263` and `perception_node.py:216` ignore `state["system_prompt"]` and rebuild the prompt with `"operational"`. Also, `AgentApp.active_role` initializes to `"system"` while `_role_idx = 0` (`"lead_architect"`), skipping `"lead_architect"` on the first `Tab` press.
  2. **TUI `/mcp add` writes to `.agent/config.json`**: `_app_slash_commands_mixin.py:469-480` hardcodes `.agent/config.json` instead of `agent_context_path(workdir) / "config.json"` (`.codingAgent/config.json`).
  3. **`SessionScreen` off-by-one, search key hijacking, and empty-list freeze**: `SessionScreen.action_fork_session` (`session_screen.py:145`) and `action_revert_session` (`:180`) index `self._filtered[self._selected]` without subtracting `_offset = 1 if self._show_new_session else 0`. `on_key` in `session_screen.py:241` and `session_list.py:187` intercepts `f`, `r`, `d`, `n`, `j`, `k` even when `#sessions_search` `Input` has focus, and `session_list.py:188` returns early when `not self._filtered`, preventing keyboard activation of `"➕ Start new session"` when the list is empty.
  4. **Plaintext API keys written to tracked `src/config/providers.json`**: `config_writer.py:43,161-166` writes `entry["api_key"] = api_key` directly into git-tracked `src/config/providers.json` instead of user-private `prefs.json` (`~/.config/codingagent/prefs.json`), and `_bridge_session.py` / `_app_session_mixin.py` write TUI session files without `redact_secrets()`.
- **Implementation Specification:**
  1. **Wire role selection end-to-end (`_bridge_agent.py`, `inference_loop.py`, `frontier_loop_node.py`, `perception_node.py`, `app.py`):**
     - In `_bridge_agent.py:115`, call `self._orchestrator.get_tools_for_role(prompt_name)` and pass `system_prompt_name=prompt_name`.
     - Store `role_name = canonical_role_name(system_prompt_name or "operational")` in `initial_state["active_role"]` in `inference_loop_state.py`.
     - In `frontier_loop_node.py:263` and `perception_node.py:216`, use `state.get("active_role") or "operational"` (and filter `tools_list` via `orch.get_tools_for_role(role_name)` when specified) instead of hardcoding `"operational"`.
     - Initialize `AgentApp.active_role = ROLE_CYCLE[0]` (`"lead_architect"`) in `app.py:129,147`.
  2. **Fix TUI `/mcp add` path (`_app_slash_commands_mixin.py:469-480`):**
     - Use `from src.tools.tools_config import agent_context_path; agent_dir = agent_context_path(_Path(getattr(self._bridge, "working_dir", None) or _os.getcwd()))` so `/mcp add` writes to `.codingAgent/config.json`.
  3. **Fix `SessionScreen` & `SessionListScreen` (`session_screen.py`, `session_list.py`):**
     - Subtract `_offset = 1 if self._show_new_session else 0` in `action_fork_session` and `action_revert_session` (rejecting row 0 with `"Cannot fork/revert the 'Start new session' item"`).
     - In `on_key` (`session_list.py:187` and `session_screen.py:241`), if `isinstance(self.focused, Input)` and `event.key not in ("escape", "down", "up", "enter")`, return without intercepting letter keys (`f`, `r`, `d`, `n`, `j`, `k`).
     - In `session_list.py:188`, allow `enter` and `n` when `not self._filtered` and `self._show_new_session` is `True`.
     - Validate `git_sha` against `^[0-9a-fA-F]{7,40}$` before passing to `git checkout` in `session_screen.py:219`.
  4. **Store API keys in user prefs and redact TUI history (`config_writer.py`, `_bridge_session.py`, `_app_session_mixin.py`):**
     - In `config_writer.py:save_provider_credentials`, store `api_key` in user-level `prefs.json` (`0o600` permissions) rather than modifying tracked `src/config/providers.json`. Apply `redact_secrets()` before writing TUI conversation/prompt history files. Keep `tui/src` and `tui/tui_src` identical.
- **Regression Test Specification (`tests/unit/test_phase3_usability_performance.py`):**
  - Switch TUI role to `"reviewer"` (`qa_lead`) and run a turn through `AgentBridge._run_agent`: verify `frontier_loop_node` builds the prompt with role `"reviewer"` and restricts tools to the reviewer toolset.
  - Run TUI `_slash_mcp("add myserver npx -y server")` and verify `.codingAgent/config.json` is written and `get_mcp_servers()` returns `myserver`.
  - Test `SessionScreen.action_fork_session` with `self._selected = 1` (`_show_new_session = True`): verify it forks `self._filtered[0]`, not `self._filtered[1]`.

---

### SPEC-19 [High] — Fix `McpHttpClient` JSON-RPC Unwrapping, `McpSseClient` Connect Race, `McpStdioClient` `stderr`/Buffer Limits, and `LSPClient` Lifecycle

- **Addresses:** §8 & §13 MCP and LSP Integration Reliability.
- **Locations:**
  - `src/core/mcp/mcp_http_client.py:96-107,126-159,185-252`
  - `src/core/mcp/mcp_sse_client.py:108-113,336-341`
  - `src/core/mcp/mcp_ws_client.py:374-379`
  - `src/core/mcp/mcp_client.py:146-152,382`
  - `src/core/indexing/lsp_client.py:225-266,315-385`, `src/core/indexing/lsp_manager.py:152-158`
- **Root Cause & Trigger:**
  1. `McpHttpClient._request()` (`mcp_http_client.py:220`) returns the full JSON-RPC envelope `{"jsonrpc": "2.0", "id": 1, "result": {"tools": [...]}}`, while `list_tools()` (`:127`) reads `resp.get("tools", [])` and `call_tool()` (`:150`) reads `resp.get("content", resp)` from the top level instead of `resp.get("result", {})`.
  2. `McpHttpClient.register_tools`, `McpSseClient.register_tools`, and `McpWsClient.register_tools` omit `schema=tool_def.input_schema` when calling `registry.register(...)`.
  3. `McpStdioClient.connect` (`mcp_client.py:146-152`) sets `stderr=PIPE` without draining it (deadlocking verbose servers) and uses default 64 KiB `StreamReader` limit (`:382`, crashing on tool outputs >64 KiB).
  4. `LSPClient.start` (`lsp_client.py:225-266`) leaks `self._proc` if `initialize` times out, never drains `stderr=PIPE`, never sends `textDocument/didOpen` before document queries, and ignores `documentChanges` in `WorkspaceEdit` (`:383`).
- **Implementation Specification:**
  1. **Unwrap JSON-RPC `result` and check `error` in `McpHttpClient._request` (`src/core/mcp/mcp_http_client.py:198-252`):**
     - If `"error"` in `data`, raise `McpError(data["error"])`; return `data.get("result", data)`. Send `notifications/initialized` in `connect()`.
     - Pass `schema=tool_def.input_schema` in `register_tools()` across `McpHttpClient`, `McpSseClient`, and `McpWsClient`.
  2. **Drain `stderr` and increase `StreamReader` limit in `McpStdioClient` and `LSPClient` (`mcp_client.py:146`, `lsp_client.py:225-266`):**
     - Pass `limit=16 * 1024 * 1024` (16 MiB) to `asyncio.create_subprocess_exec`, spawn a background task draining `proc.stderr` to debug logs, and in `LSPClient.start()` terminate `self._proc` in `except Exception:` if `initialize` fails or times out.
     - In `LSPClient`, send `textDocument/didOpen` (tracking opened files per client) before `diagnostics`/`hover`/`definition`/`references`/`rename`, and parse both `changes` and `documentChanges` in `LSPClient.rename`.
- **Regression Test Specification (`tests/unit/test_mcp_http_client.py`, `tests/unit/test_lsp_client_lifecycle.py`):**
  - Mock a standard MCP HTTP JSON-RPC response `{"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}}`: verify `McpHttpClient.list_tools()` returns `[McpToolDefinition(name="echo", ...)]` and `register_tools` passes the schema to `ToolRegistry`.
  - Simulate an `LSPClient` `initialize` timeout and verify `proc.terminate()` / `proc.kill()` is called and no subprocess is leaked.

---

### SPEC-20 [Medium] — Fix `FileLock` Timeout, Shutdown Hook Starvation, Subprocess Timeout Kill, `KVCacheGovernor`, Work Summary, SWE-bench, Telemetry, and CLI

- **Addresses:** §§6, 9, 10, 11, 12 Medium/Low Findings.
- **Locations & Specifications:**
  1. **Non-blocking `fcntl.flock` timeout & cross-host fallback check (`src/tools/file_lock.py:204-275`):**
     - In `_acquire_flock`, poll `self._fcntl.flock(fd, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)` in a loop with `time.sleep(self.poll_interval)` up to `self.timeout`, raising `FileLockTimeout` when the deadline expires.
     - In `_is_fallback_lock_stale` (`:251-254`), only run `os.kill(pid, 0)` when `same_host` is `True`; when `same_host` is `False`, rely strictly on `too_old` (`age > _STALE_LOCK_AGE_SECS`).
  2. **Shutdown hook starvation & P2P cleanup (`src/core/orchestration/session_lifecycle.py:150-198`):**
     - Run each shutdown hook (and `bus.unsubscribe_session(session_id)`) in its own daemon `threading.Thread` with `thread.join(timeout=remaining)` so a single hung hook cannot block subsequent hooks on a shared 1-worker queue.
  3. **Subprocess termination on tool pipeline timeout (`src/core/orchestration/tool_execution_pipeline.py:700-775`, `src/tools/sandbox.py`):**
     - Register active child `subprocess.Popen` processes (started with `start_new_session=True`) with the current thread's `cancel_ev` so that when `fut.result(timeout=...)` times out in `tool_execution_pipeline.py:755` and sets `cancel_ev.set()`, a cancellation callback immediately sends `SIGTERM`/`SIGKILL` to the process group (`os.killpg`), freeing the `_tool_executor` worker thread.
  4. **`KVCacheGovernor` over-committed / zero VRAM handling (`src/core/inference/kv_cache_governor.py:95-105,38-55`) & Linux `sysctl` (`hardware_capability_profile.py:337-340`):**
     - Clamp `self.max_kv_cache_gb = max(0.0, ...)` and `self.max_tokens = max(0, ...)`. When `max_vram_gb > 0` and `self.max_tokens == 0`, report `usage_ratio = 1.0` (`should_warn = True`, `should_compact = True`); when `max_vram_gb == 0.0` (CPU-only), use a configurable token-count budget rather than negative VRAM math. Only call `_detect_vram_metal()` when `sys.platform == "darwin"`.
  5. **Restore `_session_modified_files` in `_build_success_response` & fix `work_summary.py` (`src/core/orchestration/inference_loop_responses.py:139-200`, `src/core/orchestration/work_summary.py:65-75`):**
     - Attach `final_state["_session_modified_files"] = list(getattr(orch, "_session_modified_files", []))` and call `orch._publish_session_changes()` inside `_build_success_response()`. Update `_generate_work_summary` to parse tool executions from `tool_call_history` / `tool_execution_result` messages.
  6. **Fix `SWEBenchRunner` tools/path-traversal & tautological evaluation scenarios (`src/core/evaluation/swebench.py:242,297,326,415-425`, `src/core/evaluation/scenario_evaluator.py:475,664,794`):**
     - In `_invoke_swebench_agent`, pass `tools=agent.get_tools_for_role("operational")` and `system_prompt_name="operational"`. Validate `run_dir.resolve().is_relative_to(self.workdir.resolve())` before `shutil.rmtree(run_dir)`. Remove `-x` from `pytest` in `_run_tests()` and require explicit `PASSED` lines for `fail_to_pass` tests. Update `add_type_hints`, `trace_call_chain`, and `ambiguous_format_request` in `scenario_evaluator.py` so `expected_files` checks assertions that are *not* already present in `setup_files`.
  7. **Telemetry redaction, `--thinking` CLI flag, and `session export` `fsync` (`src/core/inference/telemetry.py:93`, `src/core/telemetry/tracer.py:188,254`, `src/main.py:289-299`, `src/cli/session_cmd.py:124-129`):**
     - Wrap error strings and span attributes in `redact_secrets()` before logging or exporting in `telemetry.py` and `tracer.py`. Wire `args.thinking` in `src/main.py` to model thinking configuration. Move `f.flush(); os.fsync(f.fileno())` *after* `f.write(md_text)` in `src/cli/session_cmd.py:124-129`.

---

## Appendix: TUI Defects (Remediated)

- Spawn permission is bridged but never rendered/resolved: `tui/tui_src/ui/_bridge_tools.py:222-232`. **VERIFIED FIXED** — interactive Allow/Deny buttons render in chat log, publish `spawn.permission_granted`/`spawn.permission_denied` to event bus, and post `SpawnPermissionApproved`/`SpawnPermissionDenied`.
- Diff preview drops `requires_confirmation`: `_bridge_tools.py:150-159`; `_bridge_context.py:167-176`. **VERIFIED FIXED** — forwarded through bridge and components; Accept/Reject action buttons only render when confirmation is required.
- `/share` mounts a feedback widget twice without clipboard support: `_app_slash_commands_mixin.py:754-774`. **VERIFIED FIXED** — inner mount calls removed; widget mounted exactly once.
- Remaining TUI defects (`active_role` execution wiring, `/mcp add` `.agent` path, `SessionScreen` offset & search input key interception, and plaintext API key writing to tracked `providers.json`) are specified in [SPEC-18](#spec-18-high--wire-tui-active_role-end-to-end-fix-tui-mcp-add-path-fix-sessionscreen-offsetfocus-and-protect-api-keys).

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
