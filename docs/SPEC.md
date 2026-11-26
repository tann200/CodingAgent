# CodingAgent System Engineering Specification (`docs/SPEC.md`)

**Status:** Normative System Specification  
**Applies To:** All changes in `src/`, `tui/`, `tests/`, `.github/`, and configuration manifests (`src/config/`).  
**Companion Documents:**
- [`AGENTS.md`](file:///Users/tann200/PycharmProjects/CodingAgent/AGENTS.md) (Contributor Rules & Development Commands)
- [`ARCHITECTURE.md`](file:///Users/tann200/PycharmProjects/CodingAgent/ARCHITECTURE.md) (Runtime Overview)
- [`docs/audit/2026-11-16-full-engineering-audit.md`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/audit/2026-11-16-full-engineering-audit.md) (2026-11-16 Full-Repository Audit)
- [`docs/TASKLIST.md`](file:///Users/tann200/PycharmProjects/CodingAgent/docs/TASKLIST.md) (Detailed Remediation & Gap-Closure Tasklist)

---

## 1. Normative Precedence & Change Enforcement Rules

1. **Mandatory Compliance:** Every pull request, refactor, bug fix, or new feature MUST satisfy all **MUST** / **MUST NOT** invariants in this specification alongside [`AGENTS.md`](file:///Users/tann200/PycharmProjects/CodingAgent/AGENTS.md).
2. **Fail-Closed Default:** Whenever a permission check, workspace boundary, trust verification, sandbox backend, contract validation, or config resolution fails unexpectedly, execution **MUST fail closed** (deny/abort with structured error). Catching broad exceptions to return `None` / `True` / raw host fallback is prohibited in security gates.
3. **Hermetic & Behavioral Verification:**
   - Every behavioral change MUST include focused regression tests exercising the **live dispatch path** (not bypassing `Orchestrator.execute_tool()`, LangGraph node boundaries, or Textual message posting).
   - Unit and mock-integration tests MUST NOT require live LLM providers or bypass sandbox enforcement globally in shared fixtures.
   - Any change affecting system boundaries, runtime paths, CLI commands, or security contracts MUST update [`ARCHITECTURE.md`](file:///Users/tann200/PycharmProjects/CodingAgent/ARCHITECTURE.md) and [`README.md`](file:///Users/tann200/PycharmProjects/CodingAgent/README.md) in the same change set.

---

## 2. Entrypoints, CLI & Workspace Trust Specification

### 2.1 Strict Entry Boundary (`src/main.py`)
- `src/main.py` MUST remain the strict CLI/dispatch boundary. Core packages under `src/core/` and `src/tools/` MUST NOT import modules from `tui/`.
- CLI argument parsing (`_parse_args`) MUST reject unrecognized flags cleanly (`parse_args()`) rather than silently discarding typos on security flags (`--permission-mode`, `--untrusted`, `--sandbox-level`, `--deny-tool`).
- When launching the TUI, `src/main.py` MUST pass `--workdir`, `--trusted` / `--untrusted`, and `--plugin-dir` directly into `AgentApp.__init__(...)` before `AgentBridge` or `Orchestrator` is constructed.
- Headless structured outputs (`--output-format json | raw | stream | stream-json`, `--output text | json | stream-json`) MUST keep `stdout` strictly machine-readable: diagnostic/info logs (`GUILogger` and adapter warnings) MUST write to `sys.stderr` (or structured log sinks), never `stdout`. `--output stream-json` MUST omit raw secrets and raw tool bodies from NDJSON lifecycle events, terminate with an authoritative `{type: "result", status, result, usage}` envelope, and exit `0` strictly when `status == "complete"`.
- `--private` (`CODINGAGENT_PRIVATE=1`) MUST disable SQLite session writes, snapshot/memory persistence, `execution_trace.json`, and `timings.json` disk artifacts while permitting normal workspace file edits.
- `--parallel` MUST isolate work inside a timestamped temporary git worktree, locally squash-merge on exit without ever auto-pushing, and prompt `[a]bort / [l]eave / [h]elp` on merge conflict.
- Cross-model switches (`/model`, `/provider`, or CLI override on `--resume`) MUST normalize conversation history via `history_normalizer.py` (flattening native tool calls/results into labeled text blocks and stripping provider-specific reasoning signatures/IDs). Opt-in `ask_advisor` MUST stay dormant by default and enforce `max_uses=3` per session; `/btw` MUST remain strictly read-only without mutating main conversation history.
- `codingagent` invoked with `--output-format json` without `--task` and without piped stdin MUST fail fast with a structured JSON error rather than blocking indefinitely on `stdin.read()`.

### 2.2 Workspace Configuration & Trust Boundary
- Workspace-local configuration under `<workdir>/.codingAgent/` (`config.json`, `config.local.json`, `settings.json`, `settings.local.json`, `hooks.json`, `hooks/`, `.mcp.json`, `checkpoints/`) is **untrusted input**.
- **Trust Gate Invariant:** Unless `is_trusted(orchestrator)` is explicitly `True`:
  1. Workspace config/settings MUST NOT escalate `PermissionLevel` (e.g., `permissionMode: "allow"`), inject/override active providers or endpoints, or enable `hooks_enabled` / `.codingAgent/hooks/*` execution (`ToolHookRunner`).
  2. Workspace MCP servers MUST require explicit user trust confirmation across **all transports** (`stdio`, `http`, `sse`, `ws`), never only `stdio`.
- Project configuration schemas (`config.json` vs `settings.json`) MUST resolve from one documented precedence chain; `codingagent init` MUST NOT scaffold dead/unread keys (`autonomous`, `permission_mode`, `sandbox_level`) unless wired into startup resolution, and `--validate-config` MUST validate the active config files.
- `CODINGAGENT_CONTEXT_DIR` MUST be validated as a single relative directory name (rejecting `/`, `\`, `..`, and empty strings).
- Credential persistence (`UserPrefs`, OAuth caches) MUST enforce `0o600` file permissions on temporary files *before* atomic move/replace and MUST surface write failures instead of swallowing exceptions.

---

## 3. Canonical Graph & Orchestration State Specification

### 3.1 Canonical Pipeline Topology & Reducer Safety (`src/core/orchestration/runner.py`, `graph/`)
- All model-tier executions MUST run on the canonical 6-stage native async `AgentRunner` pipeline (`initialize -> agent_loop -> [approval | verify | compact | finalize] -> END`) constructed by [`builder.py`](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/builder.py) and executed by [`runner.py`](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/runner.py).
- **Explicit State Spec Declaration:** Every state key read or written between stages (including `_verified_file_hashes`) MUST be declared in `_AgentStateSpec` ([`state.py`](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/state.py)) and validated under `NodeOutputSchema` ([`state_schemas.py`](file:///Users/tann200/PycharmProjects/CodingAgent/src/core/orchestration/graph/state_schemas.py)) with strict mode enabled (`CODINGAGENT_STATE_SCHEMAS_STRICT=1`).
- **`merge_or_replace_list` Reducer Rule:** Any stage returning a `history` update MUST return either:
  - strictly the new **delta** slice (`history[initial_len:]`), **or**
  - the full list explicitly wrapped via `replace_state_list(...)`.
  Returning an unwrapped full `list` of prior + new history is forbidden (causes $O(2^k)$ exponential history duplication).

### 3.2 Loop Termination, Cancellation & Routing Invariants
- **Bounded Execution:** Cumulative `run["turns"]` MUST count monotonically across re-entries (`agent_loop` MUST subtract `state["run"]["turns"]` from `effective_max_turns`), and `max_tool_calls` MUST honor `runtime_policy["tool_limit"]`.
- **No Infinite Self-Loops:** When `tool_call_count >= max_tool_calls` or when `_call_llm_for_turn` raises a non-recoverable LLM/connection error, `frontier_loop_node` MUST record explicit failure metadata (`errors`, `run["outcome"] = "failed"` / `"complete"`), and `route_after_agent_loop` MUST NOT route back to `agent_loop`.
- **Flag Reset Hygiene:** Transient exit flags (`replan_required`, `action_failed`) MUST be explicitly reset (`None` / `False`) on clean recovery turns in `build_exit_result()` so stale flags cannot hijack later verified/completed routing.
- **Cooperative Cancellation:** `frontier_loop_node` MUST check `cancel_event.is_set()` before every LLM call and between tool execution waves. Any cancellation detected in `agent_loop` or `verification_node` MUST set `run["status"] = "cancelled"` and `run["outcome"] = "cancelled"`, routing exclusively to `finalize` (rolling back open snapshots) and NEVER re-entering `agent_loop`.

### 3.3 Approval, Plan Mode & Checkpointing Invariants
- **Plan Mode Completeness:** `PlanMode.BLOCKED_TOOLS` MUST block **all** mutating/exec tools (`PermissionKind.WRITE_FILE`, `EXECUTE_BASH`, `GIT_WRITE`, `LSP_WRITE`, including `edit_file_atomic`, `multiedit`, `rename_file`, `create_directory`, `ast_rename`, `bash`, `git_commit`).
- **Batch Integrity on Suspension:** Blocking for approval during a multi-tool assistant turn MUST NOT discard sibling tool calls or mismatch `tool_call_id`s upon resumption.
- **Digest & Scope Isolation:** `approval_node` MUST require a non-empty `digest` match against the exact action being resumed and MUST NOT allow an earlier `plan_mode_approved=True` flag to auto-approve subsequent file diff previews (`preview_confirmed`).
- **Safe Checkpointing:** `JsonlCheckpointSaver` MUST serialize/deserialize using JSON + `redact_data` schema validation; `pickle` deserialization on workspace files is prohibited. Cancelled or terminal runs MUST be purged/quarantined so stale `task` / `run` state never overwrites a new prompt.

### 3.4 Verification & Transaction Settlement Invariants
- **Deletion Verification:** Confirming `delete_file` removal in `verification_node` MUST set `"verification_passed": True`, structured `"verification"` evidence, and terminal `run` status so `finalize_node` commits instead of rolling back valid deletions.
- **Transaction Snapshot Lifecycle:** `begin_step_transaction` -> `append_to_snapshot` (`exists=False` for new paths) -> `commit_step` / `rollback_step` MUST share consistent snapshot keys so `commit_step` removes both in-memory entries and `.codingAgent/snapshots/*.json` files.
- **Verification Security Precedence:** `.codingagent/verify.yaml` tampering and allowlist validation MUST execute *before* any non-code asset bypass heuristic, and relative modified paths MUST resolve against `working_dir` (never process `cwd`).
- **Lifecycle Resource Cleanup:** `Orchestrator` shutdown hooks (`atexit`) MUST hold `weakref.ref(orch)` (and unregister on `close()`), never strong references; `MessageManager` mutations MUST hold `_msg_mgr_lock` and sync native `tool_calls`/`tool_call_id`/`name` cleanly after compaction.

---

## 4. Tool System, Execution Pipeline & Sandbox Specification

### 4.1 Single Tool Registry & Schema Contract
- `example_registry()` and `ToolRegistry` MUST expose one unified contract preserving `ToolDefinition` metadata (`__tool_meta__`), `schema_visibility`, `PermissionKind`, and wrapper unwrapping (`inspect.unwrap`).
- `ToolRegistry.call()` MUST route through `Orchestrator.execute_tool()` and fail closed (raising `RuntimeError`) if orchestrator/pipeline setup fails.
- Every model-facing tool in `compact-flat` / `standard-flat` / `frontier-flat` MUST declare explicit typed parameters without `**kwargs` (preserving `"additionalProperties": False`), and `to_openai_schema()` MUST inspect `typing.get_origin` / `typing.get_args` rather than string matching.
- Pre-execution input contracts and post-execution `ToolResult` output contracts MUST align with tool signatures and normalized outputs (`grep`, `generate_patch`, etc.); auto-corrected tool names from `preflight_check_impl` MUST propagate to all downstream guards and dispatch.

### 4.2 File Safety & Pre-Write Guardrail Parity
All mutating file/AST/LSP/Git tools (`write_file`, `edit_file`, `edit_file_atomic`, `edit_by_line_range`, `multiedit`, `edit_code_block`, `apply_patch`, `delete_file`, `rename_file`, `create_directory`, `ast_rename`, `lsp_rename`, `git_restore`) MUST enforce in order:
1. `check_file_safety()` lexical + `os.path.realpath()` check blocking `.git` internals (`git_internals_protected`), secret files (while keeping `.env.example`/`.sample`/`.template` and `*.pub` readable), dangling symlink writes (`dangling_symlink_protected`), and multi-hardlink regular files (`st_nlink > 1`, `multi_link_file_protected`).
2. `safe_resolve(path, workdir)` confinement (`Path.cwd()` MUST NOT substitute for `workdir`).
3. `WorkspaceGuard` check (exact basename / path-boundary suffix matching, never unanchored `.endswith()`).
4. `_check_project_deny_write` check (normalized relative to resolved `workdir`).
5. `check_read_before_write` (resolving against `workdir`; a Context-Aware Read Guard preview MUST NOT satisfy full-file overwrite).
6. Binary & UTF-8 text validation before textual edits (rejecting `\x00` or invalid UTF-8 cleanly; storing binary snapshots losslessly without `errors="replace"` corruption on rollback).
7. Atomic writes via `mkstemp` + `os.replace` (`_atomic_write`).

### 4.3 Shell Security, Sandboxing & Network Isolation
- `_check_workdir_confinement()` MUST NOT exempt `bash` or `run_tests` from `PermissionPolicy` / `PermissionLevel` gating.
- `PreToolUse` hooks MUST run strictly after `PermissionPolicy` / permission gating (never escalating a `deny`/`ask` verdict), and project `.codingAgent/hooks*` files require SHA-256 trust confirmation in live runs.
- Read-only / untrusted-ingestion roles (`scout`, `analyst`, `explore`, `researcher`, `planner`, `strategic`) MUST fail closed via `assert_lethal_trifecta_isolated()` if any mutating tool is admitted.
- Shell analysis in both `bash()` and `bash_readonly()` MUST enforce recursive interpreter/flag checks across `env`, `xargs`, `find -exec`, versioned binaries (`python3.11`), and combined short flags (`-Bc`), plus `is_secret_path` argument blocking.
- `run_sandboxed()` (`src/tools/sandbox.py`) MUST:
  1. Execute via `_run_subprocess_proc_group()` so timeouts and cancellations kill the entire process tree.
  2. Reject `sandbox_level="off"` whenever mandatory enforcement (`_REQUIRE_ENFORCEMENT` or autonomous mode) is active.
  3. Distinguish `env={}` from `env=None` and scrub sensitive environment variables (`*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD`) by default.
  4. Mask host credential directories (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.kube`, `~/.docker`, `$XDG_CONFIG_HOME/{gh,gcloud,op,sops/age}`) using SBPL last-match-wins `(deny file-read*)` or `bwrap --tmpfs`, honoring explicit `--sandbox-expose` overrides placed after the deny/tmpfs mask.
  5. Never allow callers (`lint_dispatch.py`, `verification_tools.py`, `tool_execution_pipeline.py`) to catch sandbox `RuntimeError` and fall back to unsandboxed `subprocess.run()`.
- Network tools (`web_tools.py`, `remote_skills.py`) MUST reject octal/hex/private/link-local IPs, fail closed on DNS failures, disable automatic redirects and revalidate every redirect hop, pin validated socket peer IPs, and sanitize remote cache filenames.
- Parallel subagents (`delegate_tasks_parallel`) MUST isolate permissions via thread-local `_ACTIVE_AGENT_CTX` (`ContextVar`), MUST NOT mutate shared `parent_orchestrator.active_agent`, and MUST keep `"delegate"` and `"batch"` inside `DELEGATION_DENYLIST` where appropriate.

---

## 5. Local-First Inference, Context, Prompting & Memory Specification

### 5.1 Local Inference & Runtime Parity
- **Circuit Breaker:** `CircuitBreaker._current_state()`, `.state`, and `.is_open()` MUST remain strictly side-effect free; only `allow_request()` may transition/claim `_probe_in_flight`.
- **Streaming Error Handling:** Streaming calls (`stream=True`) MUST check HTTP status codes, run retries, and trigger auth refresh handlers (e.g., Copilot 401) *before* entering SSE parsing; HTTP 4xx/5xx errors MUST NEVER succeed as `{"ok": True, "text": ""}`.
- **Stream Decoding:** SSE line parsers MUST buffer complete lines before UTF-8 decoding (preventing split multi-byte character drops), track `<think>` / `</think>` across chunk boundaries, preserve Ollama native `thinking` deltas, and tag chunk events with the true active provider.
- **Local Runtime Context & KV Control:**
  - Local inference adapters (`ollama`, `lm_studio`, `llama_server`, and any future `mlx` runtime) MUST honor configured `num_ctx` / `context_window` and `kv_cache_quant` overrides cleanly, overriding conservative 2K–4K runtime defaults.
  - `KVCacheGovernor` MUST reserve realistic OS/wired-memory headroom on Apple Silicon unified memory, calibrate sparse/MoE KV memory estimates accurately, and call `reset()` after compaction.
- **Adapter Contract:** `AnthropicAdapter` MUST honor `self.base_url` and synthesize deterministic non-empty `tool_use` IDs; `OpenAICompatibleAdapter` MUST forward `timeout`/`format_json` and guard `usage is None`.

### 5.2 Prompt Hygiene, Context Budgeting & Caching
- **Verification Authority (`validation_authority.py`):** Piped POSIX validation commands executed via `bash` MUST wrap with `set -o pipefail;` when `/bin/bash` is used. Commands containing unquoted failure-masking operators (`;`, `||`, `&`) MUST be demoted to `authority="unconfirmed"` and MUST NOT clear required verification or red→green debt. Any recognized test/build command that fails before the first workspace mutation creates `red_debt` that MUST pass after the last mutation before `CompletionContract` accepts completion.
- **Deterministic Loop Guards (`loop_guards.py` & `workspace_bootstrap.py`):** Turn-1 bootstrap summaries MUST be stat-only (zero subprocesses) and `<= 5` body lines. `ReadDedupCache` MUST invalidate completely on any mutating tool or `bash` invocation. Unknown tool errors MUST include closest-match hints via `difflib.get_close_matches`.
- **Single-Truth Prompts & Roles:** Every tool name, parameter signature, and state directory (`.codingAgent/`) cited in `src/core/prompts/templates/*` and `src/config/agent-brain/*` MUST match the live registered flat toolset for that tier and native tool-calling contract. Contradictory YAML-vs-JSON instructions are prohibited. CodingAgent operates as a single-agent + progressive-disclosure skills loop over the 3 canonical flat toolsets (`compact-flat`, `standard-flat`, `frontier-flat`); legacy role YAML toolsets (`analysis`/`coding`/`debug`/`planning`/`review`) are retired and `get_toolset_for_role` maps exploratory roles fail-closed to `compact-flat`. Opt-in delegation (`delegate_task`) supports strictly `mode="scout"` (read-only, lethal-trifecta checked, excluded from `compact-flat`) and `mode="worktree"` (isolated git worktree).
- **Prompt Cache Boundary:** `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` MUST sit *after* all static blocks (identity, role, rules, `<active_skills>`, `<available_tools>`, `<output_format>`) so static tool schemas remain cached across turns.
- **Dynamic Token Budgeting:** `ContextBuilder.build_prompt()` MUST measure actual `full_system` tokens via `token_estimator` before setting `conversation_quota`, budget `ContextController` against the *post-truncation* conversation budget ordered by relevance, and preserve targeted retrieved snippets over generic head/tail summaries.
- **Sanitization:** Untrusted repository, tool, memory, and web inputs MUST be scanned for invisible Unicode obfuscation and escaped/delimited against XML structural boundary injection (`</task>`, `</system>`, `</available_tools>`).

### 5.3 Memory, Indexing & LSP
- **Atomic Compaction:** `auto_compactor.compact_messages()` and `MessageManager._truncate_to_window()` MUST slice strictly on `TurnGroup` boundaries (preserving `[assistant(tool_calls), role="tool"...]`), include `tool_calls` payloads in token estimates, and bound summary growth across repeated compactions.
- **Session Store Parity & Security:** `SqliteSessionStore` and `JsonlSessionStore` MUST validate `session_id` (`^[A-Za-z0-9._-]+$`), implement consistent method signatures and return schemas (`get_snapshot`, `fork_session`, `save_plan`, `load_plan`, `get_recent_sessions`, `search_mistakes`), redact secrets across all error/plan/state/search surfaces, and order SQLite queries deterministically (`ORDER BY created_at, id`).
- **Indexing & LSP Freshness:**
  - `VectorStore` MUST store `embed_model`/`dim` metadata and never compute cosine similarity across mismatched dimensions (e.g., 8-dim stub vs 384-dim ST).
  - `repo_indexer.refresh_file_in_index()` MUST hold a file/thread lock during read-modify-write and retain prior valid metadata during transient syntax errors.
  - `SymbolGraph` MUST walk via pruned `walk_language_files`, persist once per batch rebuild (not $O(N^2)$), and feed repository awareness cleanly into planning/context.
  - `LSPClient` MUST synchronize all `stdin` writes with `self._lock`, clear `_open_documents` on restart, send `textDocument/didChange` on file edits, and avoid binding `asyncio.Lock` across separate event loops.

---

## 6. Textual UI, Bridge, MCP & Server Specification

### 6.1 Textual UI & Bridge (`tui/tui_src/`)
- **Live Message Dispatch Invariant:** Every `@on(...)` / `on_<msg>` handler on `AgentApp` and its mixins (including `SlashCommand`) MUST be verified as registered in Textual's dispatch table (`app._get_dispatch_methods(...)`) via live `app.run_test()` `post_message` tests.
- **Markup & CSS Safety:**
  - Plain-text widgets (`Static`, `Label`, `notify`) rendering dynamic user prompts, errors, file paths, diffs, branch names, subagent metadata, or tool output MUST set `markup=False`, use Rich `Text`, or wrap dynamic values with `rich.markup.escape(...)`.
  - Dynamic DOM widget IDs MUST be sanitized (`[a-zA-Z0-9_-]`).
  - Layout chips (`#mcp_status_chip`, `#subagent_footer_chip`, `InlineDiff`) MUST define valid Textual CSS (no browser-only properties like `cursor: pointer;`) and maintain non-overlapping bounds.
- **UI-Thread Non-Blocking Invariant:** `AgentApp.__init__`, `on_mount`, `handle_start_github_device_flow`, `@` file walking (including initial cold cache), `SessionScreen` session scanning, and `git` revert MUST NOT block the UI event loop. Background log handlers (`ConsolePanel` / `InMemoryHandler`) MUST synchronize buffers with `threading.Lock` and marshal widget updates via `call_from_thread`.
- **Single-Source Bridge Contracts:**
  - `AgentBridge.publish_typed` MUST forward typed routing events (`ModelRouting`) so `LLMManager` switches adapters/models live.
  - Bash Tier-3 approval (`BashApprovalRequired`), `delegate_task` approval (single prompt per `tool_call_id`), doom-loop abort (`force_interrupt()`), and `delete_file` prompt enforcement (no TUI "Allow Always" bypass) MUST share consistent IDs and event names across Core and TUI.
  - Session lifecycle (`/new`, `/fork`, `/rename`, `/sessions`, Revert `git_sha`), provider credential stores (`prefs.json` vs `providers.json`), `working_dir` propagation, and `StreamView` cleanup MUST stay synchronized without orphaned widgets or colliding IDs.
  - Bridge tool results MUST strip ANSI escape sequences and cap output size (`<= 100 KB`) before UI rendering.

### 6.2 HTTP/WebSocket Server & Scheduler (`src/server/`, `src/core/scheduler/`)
- **Defense-in-Depth Server Boundary:**
  - `_lifespan` MUST enforce `validate_server_exposure()` regardless of whether launched via `run_server()` or `uvicorn src.server.app:app`.
  - WebSocket handlers MUST validate the `Origin` header against local/allowlisted origins; `/session/all/events` and `/ws/session/all` MUST be restricted to admin scope.
  - Authentication MUST execute as a FastAPI `Depends` dependency *before* Pydantic body validation; `/metrics` MUST protect session metadata when `CODINGAGENT_ADMIN_TOKEN` is set, compare Basic Auth via `hmac.compare_digest`, and escape `\`, `\n`, and `"` in Prometheus labels.
  - `POST /task` MUST hydrate prior `session_id` turns into `orch.msg_mgr.messages`, propagate `ContextVar` session IDs cleanly with `reset()` in `finally:`, shut down `_TASK_EXECUTOR` on lifespan teardown, and avoid strong `atexit` references or global scheduler overwrites.
- **Scheduler Safety:** `worker.py` MUST guard start/stop lifecycle with a lock, run jobs in a bounded executor with timeouts and cancellation tokens (never stalling the loop on `heartbeat_interval`), and persist enablement/interval overrides cleanly.

---

## 7. Evaluation, Observability & Testing Specification

- **Observability Alignment:** `OtelExporter` and `tracer.py` MUST subscribe to canonical topic names (`tool.execute.*`), read `ToolExecute*` payload fields (`tool`, `title`, `content`, `raw_output`, `error`), initialize at most one `TracerProvider`, and redact span attributes before export.
- **Evaluation Ground Truth:**
  - `ScenarioEvaluator` MUST validate behaviors via executable `test_command` / AST / test assertions rather than trivial keyword substrings, record uncaught agent exceptions as `"error"` (never `"pass"`), always call `agent.close()` in `finally:`, and fail `regression.py` when baseline scenarios are missing.
  - `swebench.py` MUST exclude `.codingAgent/` artifacts from generated patches, parse JSON-encoded `FAIL_TO_PASS`/`PASS_TO_PASS` lists reliably, use `pytest -v -rA` (or JUnit XML) for test grading, and guard `git` commands with `--` and timeouts.
- **Test Harness Hygiene:**
  - `tests/conftest.py` MUST NOT globally disable `CODINGAGENT_SANDBOX_ALLOW_UNENFORCED=1` or permanently mutate `os.environ["HOME"]`.
  - CI MUST cover `tests/acceptance/`, full-package `mypy` (or `pyright`), TUI pilot `post_message` flows, and real `POST /task` integration without masking contract failures behind over-mocked stubs.
