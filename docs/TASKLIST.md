# CodingAgent Master Remediation & Gap-Closure Tasklist (`docs/TASKLIST.md`)

**Derived From:**
- [`docs/audit/2026-11-16-full-engineering-audit.md`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/audit/2026-11-16-full-engineering-audit.md)
- [`docs/SPEC.md`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- 2025–2026 Local-AI Coding Agent Gap Analysis (vs. `Aider`, `OpenCode`, `Cline`, `Goose`, `Nanocoder`, `Qwen Code`, `Mistral Vibe`, `Pi-Mono`)

**Execution Rule:** Every task MUST comply with [`docs/SPEC.md`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md) and pass:
```bash
.venv/bin/python -m pytest tests/unit -q -p no:logging
.venv/bin/python -m pytest tests/integration -q -p no:logging -m "not real_llm and not lmstudio and not ollama"
.venv/bin/python -m ruff check src tests tui --select=E,F,W --ignore=E501
.venv/bin/python -m mypy src/server src/core/inference/provider_utils.py src/core/orchestration/tool_contracts.py src/core/orchestration/tool_parser.py --ignore-missing-imports --follow-imports=silent --disable-error-code=import-untyped --no-error-summary
```

---

## Tier 0 — Critical Breakages (Live TUI, Graph Core, Tools, Inference & Trust Boundaries)

### `TASK-01`: Fix Textual 8.1.0 Mixin `@on(...)` Dispatch & CLI Flag Propagation in TUI
- **Addresses:** `ARCH-C1`, `ARCH-C10`, [`SPEC §2.1`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md), [`SPEC §6.1`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **Critical** (unblocks the entire live Textual TUI)
- **Files & Symbols:**
  - `tui/tui_src/ui/app.py` (`AgentApp.__init__`, mixin handler registration)
  - `tui/tui_src/ui/_app_message_handlers_mixin.py`
  - `tui/tui_src/ui/_app_tool_handlers_mixin.py`
  - `tui/tui_src/ui/_app_status_handlers_mixin.py`
  - `tui/tui_src/ui/_app_session_mixin.py`
  - `tui/tui_src/ui/_app_slash_commands_mixin.py` (`handle_slash_command`)
  - `src/main.py` (`main` TUI launch lines ~1134–1145)
- **Implementation Steps:**
  1. Ensure every `@on(...)` handler defined in `AppMessageHandlersMixin`, `AppToolHandlersMixin`, `AppStatusHandlersMixin`, and `AppSessionMixin` is merged into `AgentApp._decorated_handlers` (or converted to Textual `on_<msg_name>` methods without `_textual_on`), and register `@on(SlashCommand)` / `on_slash_command` on `AppSlashCommandsMixin`.
  2. Fix `on_chat_text_area_submitted` (ensure `_textual_on` doesn't suppress or break Textual dispatch).
  3. Add `working_dir`, `trusted`, and `plugin_dir` kwargs to `AgentApp.__init__` and pass them directly from `src/main.py` before constructing `AgentBridge` / `Orchestrator`.
- **Acceptance Criteria & Tests:**
  - `app._get_dispatch_methods(...)` returns non-empty handlers for `StreamChunkEvent`, `SlashCommand`, `ChatTextArea.Submitted`, `ToolCallStartEvent`, `Button.Pressed`, `SessionHydratedEvent`, `StatusUpdate`.
  - Add headless `app.run_test()` integration test in `tests/unit/test_tui_pilot_suite.py` that posts messages via `app.post_message(...)` and submits text via UI widgets (not direct `await app.handle_*` calls).

### `TASK-02`: Fix `AgentState.history` Exponential Duplication, Infinite `agent_loop`, Cancellation, Deletions & `_verified_file_hashes`
- **Addresses:** `ARCH-C2`, `ARCH-C3`, `ARCH-C7`, `ARCH-C8`, `ARCH-C9`, `Workflow Reliability §6.1–6.2`, [`SPEC §3.1–3.4`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium–High | **Impact:** **Critical** (restores deterministic graph execution & rollback integrity)
- **Files & Symbols:**
  - `src/core/orchestration/graph/state.py` (`_AgentStateSpec`, `merge_or_replace_list`, `replace_state_list`)
  - `src/core/orchestration/graph/nodes/frontier_loop_node.py`
  - `src/core/orchestration/graph/nodes/frontier_completion.py` (`build_exit_result`, `build_plan_mode_blocked_exit`, `finalize_frontier_exit`)
  - `src/core/orchestration/graph/canonical_routing.py` (`route_after_agent_loop`, `route_after_verify`)
  - `src/core/orchestration/graph/nodes/verification_node.py` (`_verification_node_impl`)
  - `src/core/orchestration/graph/nodes/finalize_node.py` (`_finalize_node_impl`)
  - `src/core/orchestration/rollback_manager.py` (`commit_step`)
- **Implementation Steps:**
  1. Declare `_verified_file_hashes: Optional[Dict[str, str]]` in `_AgentStateSpec`.
  2. Ensure `frontier_loop_node` and `verification_node` wrap `history` updates with `replace_state_list(...)` or return strict message deltas (`history[initial_len:]`).
  3. On `tool_call_count >= max_tool_calls` or non-overflow `_call_llm_for_turn` exceptions, set structured error/terminal status (`run["outcome"] = "failed"` on error, capped terminal exit on budget) and forbid `agent_loop -> agent_loop` re-entry when budget is exhausted; deduct `state["run"]["turns"]` from `effective_max_turns`.
  4. Always set `"replan_required": replan_required` (`None` when cleared) and `"action_failed": False` in `build_exit_result()`.
  5. Poll `cancel_event.is_set()` at the top of every `frontier_loop_node` turn and before each tool wave; set `run["status"] = "cancelled"` and `run["outcome"] = "cancelled"` on early-cancel exits in both `frontier_loop_node` and `verification_node`, routing cancelled runs straight to `finalize`.
  6. In `verification_node` deletion-pass early return, set `"verification_passed": True`, structured `"verification"`, and terminal `run` status so `finalize_node` commits deletions cleanly.
  7. Fix `RollbackManager.commit_step()` to resolve `snapshot_id = self._step_to_snapshot.pop(step_id, step_id)` and delete both `self.snapshots[snapshot_id]` and `.codingAgent/snapshots/{snapshot_id}.json`.
- **Acceptance Criteria & Tests:**
  - Multi-turn graph test verifies `len(history)` scales linearly across `agent_loop -> verify -> agent_loop` transitions (zero duplicate messages/IDs).
  - Tests verify budget exhaustion terminates without `GraphRecursionError`, cancelled runs roll back via `finalize`, `delete_file` commits instead of restoring, `_verified_file_hashes` persists across nodes, and `commit_step()` deletes snapshot files from disk.

### `TASK-03`: Repair Core Tools (`list_dir`, `GrepContract`, `GeneratePatchContract`) & Unify `example_registry()`
- **Addresses:** `ARCH-C4`, `ARCH-C5`, `ARCH-C6`, [`SPEC §4.1`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **Critical** (unblocks repository exploration and pipeline tool calls)
- **Files & Symbols:**
  - `src/tools/_file_io.py` (`list_dir`)
  - `src/tools/system_tools.py` (`grep`, lines ~97, 168)
  - `src/core/orchestration/tool_contracts.py` (`GrepContract`, `GeneratePatchContract`, `WriteFileContract`)
  - `src/core/orchestration/tool_execution_pipeline.py` (contract checks, preflight auto-corrected `name` propagation)
  - `src/core/orchestration/registry_builder.py` (`example_registry`)
  - `src/core/orchestration/tool_registry.py`
- **Implementation Steps:**
  1. Fix `list_dir` loop to append directory/file items to `items` (plus check `p.exists()`, `p.is_dir()`, `_is_secret_path(p)`).
  2. Fix `grep` Python fallback for single-file targets (`search_path.is_file()`) and pass `["-e", pattern, "--", ...]` to binary `grep` to stop option injection.
  3. Separate input vs output contract validation in `tool_contracts.py` / `tool_execution_pipeline.py`; fix `GrepContract` against `ToolResult` dict and `GeneratePatchContract` against `new_content`.
  4. Preserve `__tool_meta__`, `schema_visibility`, `tags`, `origin`, and `get_permission_kind()` in `example_registry()` / unified `ToolRegistry` (and fail closed in `ToolRegistry.call()` if orchestrator initialization fails).
  5. Propagate auto-corrected tool `name` returned by `preflight_check_impl` cleanly inside `tool_execution_pipeline.py`.
- **Acceptance Criteria & Tests:**
  - `Orchestrator.execute_tool("list_files", ...)` returns populated entries; `Orchestrator.execute_tool("grep", ...)` succeeds on files & directories; `Orchestrator.execute_tool("generate_patch", ...)` succeeds; `example_registry()` hides `alias_only` schemas and validates args via `__tool_meta__`.

### `TASK-04`: Close Trust-Boundary, Hook, Remote-MCP & Shell Sandbox Bypasses
- **Addresses:** `SEC-H1`, `SEC-H2`, `SEC-H3`, `SEC-H4`, `SEC-H5`, `SEC-H15`, [`SPEC §2.2`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md), [`SPEC §4.3`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **Critical** (eliminates untrusted workspace RCE, permission escalation & shell bypasses)
- **Files & Symbols:**
  - `src/main.py` (`_parse_args`, `load_project_settings` application lines ~1041–1075)
  - `src/core/config_loader.py` (`load_merged_config` trust check)
  - `src/core/orchestration/tool_execution_pipeline.py` (`_check_workdir_confinement`, lazy `ToolHookRunner`)
  - `src/core/orchestration/tool_hooks.py` (`ToolHookRunner`, `_build_env`)
  - `src/core/mcp/manager.py` (`McpServerManager.start_from_servers`, `stop`)
  - `src/tools/_bash_exec.py` (`_check_shell_flags`, `bash` secret check), `src/tools/_security.py` (`SAFE_COMMANDS`)
- **Implementation Steps:**
  1. Switch `_parse_args` to fail-closed `parser.parse_args(argv)` so security flag typos fail instead of silently degrading permissions.
  2. Require `is_trusted(orch)` before applying workspace `settings.json` `permissionMode` escalation, workspace `config.json` provider/tool overrides, `ToolHookRunner` repo hooks (and scrub env secrets + `redact_data` on hook JSON), and workspace MCP servers across **all** transports (`stdio`, `http`, `sse`, `ws`). Release `McpServerManager._lock` before any `await`.
  3. Remove the `bash`/`run_tests` bypass in `tool_execution_pipeline.py:_check_workdir_confinement()` (lines ~509–512).
  4. Remove `env` and `xargs` from `SAFE_COMMANDS` (or recursively validate inner commands & interpreter flags including paths like `/usr/bin/python3` and versioned `python3.11`), and run Gate 6 `is_secret_path` checks in `bash()` too.
- **Acceptance Criteria & Tests:**
  - Regression tests verify `--untrusted` ignores workspace `permissionMode: "allow"`, skips `.codingAgent/hooks/`, and blocks unapproved `http`/`sse`/`ws` workspace MCP servers; tests verify `env python3 -c` and `xargs python3 -Bc` are blocked and non-tier3 `bash` respects `PermissionPolicy`.

### `TASK-05`: Fix `CircuitBreaker` State Mutation, Streaming Error Masking & `auto_compactor` Pairing
- **Addresses:** `ARCH-C11`, `ARCH-C12`, `ARCH-C13`, [`SPEC §5.1`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md), [`SPEC §5.3`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **Critical** (fixes provider lockups, silent stream failures & HTTP 400s)
- **Files & Symbols:**
  - `src/core/inference/llm_manager.py` (`CircuitBreaker._current_state`, `is_open`, `allow_request`)
  - `src/core/inference/adapters/openai_compat_adapter.py` (`_execute_with_retry`, `_chat_internal`, `generate`)
  - `src/core/memory/auto_compactor.py` (`compact_messages`, `estimate_message_tokens`, `_merge_compact_summaries`)
- **Implementation Steps:**
  1. Make `CircuitBreaker._current_state()`, `state`, and `is_open()` purely read-only; claim `_probe_in_flight = True` exclusively inside `allow_request()`.
  2. When `stream=True`, validate `r.status_code` inside `_execute_with_retry` (retrying transient codes and calling `_parse_error_response` / `raise_for_status` on 4xx/5xx *before* returning the stream response), preserving Copilot 401 token refresh. Also guard `isinstance(_u, dict)` for `"usage": null` and forward `timeout`/`format_json`.
  3. Group messages into atomic `[assistant(tool_calls), role="tool"...]` chunks in `auto_compactor.compact_messages()` before slicing `keep_from`; include `tool_calls` arguments in `estimate_message_tokens()`; cap recursive `_merge_compact_summaries` timeline bloat.
- **Acceptance Criteria & Tests:**
  - Unit tests verify `cb.is_open()` after `recovery_timeout` allows 1 probe via `allow_request()`, streaming HTTP 401/429/500 triggers retry/error instead of `ok=True, text=""`, and `auto_compactor` never leaves orphan `role="tool"` messages.

---

## Tier 1 — High-Risk Safety, Sandbox, Memory, Server & Bridge Fixes

### `TASK-06`: Harden `sandbox.py` Process-Group Kills & Eliminate Unsandboxed Fallbacks
- **Addresses:** `SEC-H8`, `SEC-H9`, [`SPEC §4.3`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **High**
- **Files:** `src/tools/sandbox.py`, `src/tools/lint_dispatch.py`, `src/tools/verification_tools.py`, `src/core/orchestration/tool_execution_pipeline.py`
- **Steps:**
  1. Route `run_sandboxed()` through `_run_subprocess_proc_group()` at all execution branches (lines ~517, 531, 570, 643) so timeouts/cancellations kill process trees.
  2. Reject `sandbox_level="off"` when `_REQUIRE_ENFORCEMENT` or `_autonomous_mode()` is active.
  3. Fix `exec_kwargs.get("env") or os.environ` so explicit `env={}` stays empty; scrub secret env vars (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`) by default; fix `_build_bwrap_args` to bind resolved `d_str`; support `network=True` in macOS SBPL profile cleanly.
  4. Remove `except Exception` unsandboxed `subprocess.run()` fallbacks in `lint_dispatch.py:68` and `verification_tools.py:57`, and route `tool_execution_pipeline.py` JS/TS checks (`node --check`, `tsc`) through `run_sandboxed()`.

### `TASK-07`: Standardize Pre-Write Guardrails, Binary/Atomic Writes, Subagent Isolation & SSRF
- **Addresses:** `SEC-H10`, `SEC-H11`, `SEC-H12`, `SEC-H13`, [`SPEC §4.2–4.3`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium–High | **Impact:** **High**
- **Files:** `src/tools/_edit_tools.py`, `src/tools/ast_tools.py`, `src/tools/lsp_tools.py`, `src/tools/git_tools.py`, `src/tools/patch_tools.py`, `src/tools/_file_io.py`, `src/core/orchestration/workspace_guard.py`, `src/core/orchestration/tool_execution_pipeline.py`, `src/tools/subagent_tools.py`, `src/tools/subagent_payloads.py`, `src/tools/web_tools.py`, `src/core/orchestration/remote_skills.py`, `src/core/orchestration/rollback_manager.py`
- **Steps:**
  1. Extract a shared pre-write guard helper enforcing `safe_resolve(workdir)` -> anchored `WorkspaceGuard.is_protected` (no bare `.endswith()` matches like `my_setup.py`) -> `_check_project_deny_write` (fixing `PermissionError` handling in `ast_rename`/`lsp_rename` and relative `workdir`) -> `check_read_before_write` -> binary check -> `_atomic_write()`. Add binary check to `generate_patch` and `write_file`; fix `create_directory` `WorkspaceGuard(workspace_root=workdir)` and `git_restore(..., "--")`; make pipeline security gates fail closed on unexpected exceptions.
  2. Remove `parent_orchestrator.active_agent = _child_agent` mutation in `subagent_tools.py` (use `_ACTIVE_AGENT_CTX` `ContextVar` exclusively), default `working_dir` to `parent_orchestrator.working_dir`, and add `"delegate"` to `DELEGATION_DENYLIST`.
  3. Fail closed on DNS `socket.gaierror` in `web_tools.py`, block octal/hex IPv4 forms (`0177.0.0.1`, `0x7f.0.0.1`), validate `ssrf_allowlist` suffixes, and disable `allow_redirects=True` in `remote_skills.py` (revalidating each hop + sanitizing cached skill filenames).
  4. Preserve binary bytes (base64) in `RollbackManager` instead of corrupting binaries with `errors="replace"`.

### `TASK-08`: Harden Plan Mode, Approval Node, Safe JSON Checkpointing & Multi-Turn `MessageManager` Sync
- **Addresses:** `SEC-H6`, `Workflow Reliability §6.3–6.7`, [`SPEC §3.3–3.4`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium–High | **Impact:** **High**
- **Files:** `src/core/orchestration/plan_mode.py`, `approval_node.py`, `frontier_loop_node.py`, `graph/checkpoint_saver.py`, `inference_loop.py`, `inference_loop_responses.py`, `message_manager.py`, `orchestrator_config_reload.py`, `orchestrator.py`, `approval_gate.py`, `completion_contract.py`
- **Steps:**
  1. Align `PlanMode.BLOCKED_TOOLS` with mutating `PermissionKind`s (`edit_file_atomic`, `multiedit`, `rename_file`, `bash`, `git_commit`, etc.).
  2. Suspend multi-tool batches safely without orphan/mismatched `tool_call_id`s; fix `approval_node` digest requirement (fail closed if empty) and prevent `plan_mode_approved=True` from auto-approving diff previews.
  3. Replace `pickle` in `JsonlCheckpointSaver` with schema-validated JSON + `redact_data`; isolate/purge cancelled checkpoints so stale `task`/`run` fields don't overwrite new `run_agent_once` prompts.
  4. Fix `_sync_msg_mgr_with_final_history` (sync after compaction and preserve `tool_calls`/`tool_call_id`/`name`), guard `MessageManager` mutations with `_msg_mgr_lock`, and truncate by `TurnGroup`.
  5. Fix `orchestrator_config_reload._on_config_reloaded` to re-apply `_ACTIVE_CONTEXT.filter_registry`, `set_orchestrator`, and `tool_execution_service._registry`. Fix `approval_gate.wait_async` `assert` crash and `CompletionContract` `"result": None` crash.

### `TASK-09`: Fix Streaming Decoders, Adapters, Tokenizer Safety, Session Stores & `GUILogger` Stdout
- **Addresses:** `SEC-H7`, `Workflow Reliability §6.9`, `Memory §9`, `Usability §11.3`, [`SPEC §2.1, §5.1, §5.3`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium–High | **Impact:** **High**
- **Files:** `src/core/inference/tokenizer.py`, `streaming.py`, `adapters/anthropic_adapter.py`, `adapters/ollama_adapter.py`, `src/core/memory/sqlite_store_collaborators.py`, `sqlite_session_store.py`, `jsonl_session_store.py`, `memory_tools.py`, `src/core/logger.py`
- **Steps:**
  1. Remove `trust_remote_code=True` and blocking HF Hub downloads from `tokenizer.py` (`local_files_only=True`).
  2. Buffer full SSE lines before UTF-8 decoding and add rolling `<think>`/`</think>` tag buffering in `streaming.py`; extract Ollama `thinking` chunks and pass true provider name into `consume_generator_stream`. Fix `AnthropicAdapter` `base_url` + non-empty `tool_use` IDs and `OllamaAdapter` `"error"` key handling.
  3. Fix `SnapshotManager` SQL subquery (`SELECT snapshot_id` instead of `SELECT id`), apply `redact_secrets` across `add_error`/`add_plan`/`save_plan`/`memory_search`, and add `, id` deterministic tie-breaking.
  4. Fix `JsonlSessionStore`: validate `session_id` (`^[A-Za-z0-9._-]+$`), fix rotated-file `revert_session` under lock, align `get_snapshot`/`get_recent_sessions`/`save_plan`/`search_mistakes` parity with SQLite.
  5. Route `GUILogger` output to `sys.stderr` so `--output-format json|raw|stream` stdout stays clean JSON/text.

### `TASK-10`: Fix TUI Bridge Contracts (`ModelRouting`, Bash Tier-3, Subagent Double-Prompt, Console Thread Safety, Escaping & CSS)
- **Addresses:** `SEC-H16`, `Rich Markup §5`, `Thread Safety §6`, `Session §8`, `CSS §8.11`, [`SPEC §6.1`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium–High | **Impact:** **High**
- **Files:** `tui/tui_src/ui/core_bridge.py`, `_bridge_tools.py`, `_bridge_subscriptions.py`, `_app_message_handlers_mixin.py`, `_app_tool_handlers_mixin.py`, `_app_status_handlers_mixin.py`, `_app_session_mixin.py`, `_app_slash_commands_mixin.py`, `components/{console.py,chat_mixin.py,stream_view.py,todo_list.py,subagent_progress.py,thinking.py}`, `screens/{session_screen.py,session_list.py,subagent_detail.py}`, `logging.py`, `styles/app.tcss`, `src/core/messaging/event_types.py`, `src/core/orchestration/event_bus.py`
- **Steps:**
  1. Route `AgentBridge.publish_typed` through `EventBus.publish_typed` so `/provider`, `/model`, `/fast` reach `LLMManager`.
  2. Add `BashApprovalRequired` typed event + `_EVENT_IMPORT_PATHS` mapping so Bash Tier-3 approvals resolve the exact `_tool_id` without hanging 120s.
  3. Deduplicate `delegate_task` `SpawnPermissionRequired` / `ToolPermissionRequired` prompts; block `"delete_file"` and raw UUID fallbacks from `_allow_always_tools`; wire doom-loop Stop button to `force_interrupt()`.
  4. Lock `InMemoryHandler` and marshal `ConsolePanel._on_new_log` to the UI thread via `call_from_thread`.
  5. Apply `markup=False` or `rich.markup.escape(...)` across all unescaped `Static`/`Label`/`notify` sites (`chat_mixin`, `handle_session_hydrated`, `todo_list`, `subagent_progress`, `subagent_detail`, `_app_slash_commands_mixin`, `_app_message_handlers_mixin`, `_app_status_handlers_mixin`, `thinking`, `session_list`, `session_screen`); sanitize dynamic DOM IDs.
  6. Fix `#mcp_status_chip` / `#subagent_footer_chip` CSS in `app.tcss` and `_finalize_stream` `StreamView` orphan tracking + `_flush_lock` safety.

### `TASK-11`: Secure HTTP/WebSocket Server, Hydrate `POST /task`, Fix `atexit` Leak & Harden Scheduler
- **Addresses:** `SEC-H14`, `Missing Capabilities §5.2–5.3`, [`SPEC §6.2`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium–High | **Impact:** **High**
- **Files:** `src/server/{app.py,server_config.py,task_endpoints.py,websocket_handler.py,sse_adapter.py,metrics.py}`, `src/core/orchestration/orchestrator_bootstrap.py`, `orchestrator_scheduler.py`, `src/core/scheduler/worker.py`
- **Steps:**
  1. Enforce `validate_server_exposure()` inside FastAPI `_lifespan`, validate WebSocket `Origin` headers, gate `/session/all` to admin scope, enforce auth via FastAPI `Depends` before Pydantic validation, protect `/metrics` with `hmac.compare_digest` + Prometheus newline escaping, and shut down `_TASK_EXECUTOR` cleanly on lifespan exit.
  2. Populate `orch.msg_mgr.messages` from `session_store.get_messages(session_id)` in `task_endpoints.py` before calling `run_agent_once` (and reset `set_session_id` token in `finally:`, redact secrets *before* `task[:200]` slice, and pass `on_evict=_record_dropped` + priority control acks in WS).
  3. Change `orchestrator_bootstrap.py` `atexit` hook to `weakref.ref(orch)` (and unregister in `close()`) and avoid overwriting global scheduler jobs from per-request `/task` `Orchestrator`s.
  4. Add `_LIFECYCLE_LOCK`, interval bounds (`>=1`), bounded executor timeouts, persistent job state, and redacted structured logging to `src/core/scheduler/worker.py`.

---

## Tier 2 — Local-AI Competitive Gaps & Performance Architecture

### `TASK-12`: Forward `num_ctx` & `kv_cache_quant` to Local Engines, Recalibrate `KVCacheGovernor` & Add Native `MLX` / `llama-server` Support
- **Addresses:** Ecosystem Gaps (Apple Silicon MLX prefill speedup, Ollama `num_ctx` silent 2K/4K truncation, `KVCacheGovernor` false turn-1 overflow), [`SPEC §5.1`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **High** (dramatically improves local LLM speed & reliability)
- **Files:** `src/core/inference/adapters/ollama_adapter.py`, `lm_studio_adapter.py`, `openai_compat_adapter.py`, new `mlx_adapter.py` (or `llama_server` config), `runtime_profile.py`, `kv_cache_governor.py`, `hardware_capability_profile.py`, `frontier_loop_node.py`
- **Steps:**
  1. Pass `options.num_ctx` (and `kv_cache_quant` hints where supported) in `OllamaAdapter` so Ollama stops silently truncating prompts to 2K–4K defaults.
  2. Reserve OS/wired RAM headroom on macOS Metal unified memory (~70–75% usable instead of 100% `hw.memsize`), fix MoE/GQA KV weight math in `KVCacheGovernor`, and invoke `kv_gov.reset()` post-compaction.
  3. Add first-class `mlx_lm` / `llama-server` provider presets and auto-discovery (`http://localhost:8080/v1`, MLX prefill/context telemetry).

### `TASK-13`: Fix Prompt-Cache Boundary, Dynamic System Budgeting & `ContextController` Relevance Pruning
- **Addresses:** Performance §12.1–12.2, Context §2, [`SPEC §5.2`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **High** (unlocks Anthropic ephemeral caching & stops context-window overruns)
- **Files:** `src/core/context/{static_prompt_parts.py,context_builder.py,context_controller.py,retrieved_snippets.py,prompt_blocks.py,prompt_cache.py,sanitization.py,message_assembly.py}`
- **Steps:**
  1. Move `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` to the **end** of `build_static_system_parts()` (after `<available_tools>` and `<output_format>`).
  2. Compute `system_tokens = self.token_estimator(full_system)` after assembly and pass `min(conversation_tokens, conversation_quota)` to `ContextController.enforce_budget()`.
  3. Fix `ContextController` to rank by relevance score and measure real tokens instead of `line_count // 4`; preserve targeted retrieved snippets in `prompt_blocks.py` instead of overwriting them with generic 20-line stubs.
  4. Harden `sanitization.py` against XML closing-tag spoofing (`</task>`, `</system>`, `</available_tools>`) and invisible Unicode; bound `_STATIC_PROMPT_CACHE` and include parameter schemas in `tools_key`.

### `TASK-14`: Align Local Prompt Templates & Role Mappings with Live Flat Toolsets
- **Addresses:** Usability §11.1, Architecture §1, [`SPEC §5.2`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Low–Medium | **Impact:** **High** (eliminates hallucinated tool names on 9B–35B local models)
- **Files:** `src/core/prompts/templates/{local-small.md,local-medium.md,openai-frontier.md}`, `src/config/agent-brain/identity/{LAWS.md,SOUL.md}`, `src/config/agent-brain/roles/*.md`, `src/config/agent-brain/skills/*.md`, `src/config/toolsets/loader.py`
- **Steps:**
  1. Replace every non-existent tool reference (`grep_search`, `bash_sandboxed`, `fetch_web_page`, `finish_task`, `respond`, `file_search`, `list_directory`) and retired path (`.agent-context/`) with exact flat toolset primitives and `.codingAgent/`.
  2. Remove contradictory YAML-code-block mandates from `SOUL.md`, `LAWS.md`, and `operational-gemma4.md` where native JSON tool calling is active.
  3. Fix `get_toolset_for_role()` so `scout`, `explore`, `researcher`, and unknown roles fail closed to read-only `analysis` instead of `coding`.
  4. Add a CI assertion checking that every tool name referenced in prompt templates exists in `build_registry()`.

### `TASK-15`: Wire Live `LSPClient` `didChange` Sync, Fix `VectorStore` Embeddings & Upgrade `SymbolGraph` RepoMaps
- **Addresses:** Repository Awareness §8, Competitive Gap (vs. `Aider` RepoMap & `OpenCode` LSP), [`SPEC §5.3`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium–High | **Impact:** **High**
- **Files:** `src/core/indexing/{lsp_client.py,lsp_manager.py,lsp_context.py,repo_indexer.py,symbol_graph.py,vector_store.py}`
- **Steps:**
  1. Acquire `self._lock` in `LSPClient._send()`, clear `_open_documents` on restart, add `notify_file_changed()` (`textDocument/didChange`) triggered by write tools, avoid cross-loop `asyncio.Lock` bindings, and wire live diagnostics into `ContextBuilder`.
  2. Tag `VectorStore` embeddings with `embed_model`/`dim`, never dot-product 8-dim stubs with 384-dim vectors, persist code embeddings, and lock `_EMBEDDING_CACHE`.
  3. Lock `repo_indexer.refresh_file_in_index()`; replace `SymbolGraph.rebuild_index` $O(N^2)$ saves and unpruned `rglob("*")` with `walk_language_files` + single batch save, normalize paths, and feed PageRank/call-graph context into planning/retrieval.

### `TASK-16`: Repair or Retire Broken Track 1 `capability_tools.py` Dispatchers & Unblock Offloadable UI I/O
- **Addresses:** Tool System §7.1–7.5, UI Blocking §7, Config Drift §11.2, [`SPEC §2.2, §4.1, §6.1`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **Medium–High**
- **Files:** `src/tools/{capability_tools.py,_tool.py,_bash_exec.py}`, `src/cli/{doctor_cmd.py,status_cmd.py,mcp_cmd.py,diff_cmd.py,skill_cmd.py}`, `src/main.py`, `tui/tui_src/ui/{app.py,config_writer.py,components/chat_mixin.py,screens/session_screen.py,screens/session_list.py}`
- **Steps:**
  1. Fix broken kwargs/imports/permissions in `capability_tools.py` (or deprecate/remove dead Track 1 wrappers), fix `_tool.py` type annotation inspection, and track `Popen` objects instead of bare PIDs in `_bash_exec.py` (with `.sb` cleanup).
  2. Offload blocking startup provider probes (`AgentApp.__init__`), `start_device_flow`, initial `@` `os.walk` (with depth cap), and `SessionScreen._do_revert` `git` commands off the Textual UI thread.
  3. Unify `.codingAgent/config.json` vs `settings.json` resolution, fix `init` hook env docs (`TOOL_ARGS_JSON`), fix `doctor` `LiteLLM` classification + `[CONFIGURED]`/`[NO CREDENTIALS]` labels, fix `skill export` path traversal + YAML serialization, fix TUI `/new`/`/fork`/`/rename`/Revert `git_sha`, and unify `prefs.json` vs `providers.json`.

### `TASK-17`: Upgrade Evaluation Harnesses, Fix `OtelExporter`/`tracer.py` & Restore Sandbox CI Gate
- **Addresses:** Evaluation §10, Observability §13.1, CI & Testing Gaps, [`SPEC §7`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** Medium | **Impact:** **High**
- **Files:** `src/core/evaluation/{scenario_evaluator.py,swebench.py,regression.py,cli.py,compare.py}`, `src/core/observability/otel_exporter.py`, `src/core/telemetry/tracer.py`, `tests/conftest.py`, `.github/workflows/ci.yml`, `pytest.ini`
- **Steps:**
  1. Remove global `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1` from `tests/conftest.py` (scope only to specific local-shell unit tests without backend) and avoid mutating `os.environ["HOME"]` permanently.
  2. Add executable `test_command` verification across all 23 default scenarios in `ScenarioEvaluator`, grade uncaught agent crashes as `"error"` + call `agent.close()` in `finally:`, and fail `regression.py` on missing baseline scenarios.
  3. Exclude `.codingAgent/` from `swebench.py` patch extraction, parse JSON-string `FAIL_TO_PASS` lists reliably, use `pytest -v -rA`, and clean tempdirs in `run_pass_at_k`.
  4. Fix `OtelExporter` & `tracer.py` topic/field alignment (`tool.execute.*`, `redact_secrets`), eliminate double `TracerProvider` registration, and add `tests/acceptance` + expanded `mypy`/`pyright` checks to CI.

---

## Tier 3 — Architectural Simplification, Dead Code Removal & Pre-Release Gates

### `TASK-18`: Prune Unused / Redundant Subsystems & Stale Artifacts
- **Addresses:** Over-Engineered Components §13
- **Complexity:** Medium | **Impact:** **Medium** (cuts maintenance burden & eliminates conflicting dead paths)
- **Targets:**
  - Remove or consolidate dead orchestration modules/methods: `FileLockManager` (`file_lock_manager.py`), unused `ToolExecutionService.pre_execute`/`execute_with_retry`, `GitSnapshotManager` (`snapshot_manager.py`), `PlanDAG` (`dag_parser.py`), `SessionRegistry`, `CrossSessionBus`, `ConfigWatcher` (`config_loader.py:520-648`), `validate_state()` (`state.py:454`), `TrajectoryLogger` dead query methods (`advanced_features.py`), unused 12-node legacy `AgentState` fields & nodes (`memory_update_node.py`, `wait_for_user_node.py`, `planning_helpers.py`).
  - Remove unused config/prompt/tool artifacts: `src/config/schema.json`, unused templates (`plan_reminder.txt`, `build_switch.txt`, `max_steps.txt`, `reasoning.md`), duplicate `formatter.py` vs `verification_tools.py:format_file`, `LSP_TOOL_SCHEMAS` duplication, and `mcp_stdio_server.py` fixes (`parents[2]` roles path, `stdout` lock, listener cleanup).
  - Remove unused TUI files/stubs: `commands/registry.py`, `components/{inline_tool.py,cards.py,history_input.py}`, `screens/probe_results.py`, `tui_src/ui/main.py`, `tui_src/{config,core}/*`, dead `config_writer.py` helpers, and stale `tests/_ci_artifacts*` + orphaned `__pycache__` directories.

### `TASK-19`: Live-Provider & Medium-Model Evaluation Gate Verification
- **Addresses:** Rollout Gates (`2026-10-09-engineering-audit.md`), [`SPEC §7`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/SPEC.md)
- **Complexity:** High | **Impact:** **High (Pre-Release Validation)**
- **Steps:**
  1. Run live evaluation suites against representative local weights (`qwen3.5:9b`, `gemma-4-26b-a4b`, `qwen3.5:27b`) via Ollama/LM Studio/MLX after Tiers 0–2 complete.
  2. Measure pass@1, edit accuracy, malformed-tool-call rate, verification pass rate, and token/prefill latency across all 23 repaired scenarios and SWE-bench samples.
