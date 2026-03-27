# CodingAgent — Full System Audit Report Vol. 9
**Date:** 2026-03-25
**Auditors:** 6 parallel deep-analysis agents
**Scope:** Full-spectrum audit across 15 categories
**Test baseline:** 1,180 passed, 0 failed, 5 skipped (pre-audit)

---

## 1. Executive Summary

The CodingAgent system has matured considerably through 8+ prior audit cycles. The orchestration pipeline, loop-prevention machinery, and tool-safety layer are all production-quality. However, this audit identified **37 new findings** across three major risk areas:

1. **Dead / unintegrated code** — ~1,420 LOC of recently-added multi-agent and session features are defined but never connected to the main execution path. Several of these modules contain correctness bugs (async/sync mismatches, race conditions) that would manifest the moment they are wired in.
2. **Planning intelligence gaps** — The SymbolGraph and test-file mapping infrastructure are implemented and correctly populated during analysis, but results are converted to prose before reaching the planning LLM. The planner therefore cannot produce dependency-aware or test-aware plans reliably.
3. **Evaluation & CI blind spots** — Integration tests are never run in default CI, the E2E scenario suite is entirely stubs, and no code-coverage gate exists. The 1,180 passing tests give a false sense of completeness.

**Severity distribution:**
| Severity | Count |
|----------|-------|
| Critical | 4 |
| High | 12 |
| Medium | 14 |
| Low | 7 |

---

## 2. Architecture Strengths

- **LangGraph pipeline** is logically sound with correct topological ordering and no unreachable terminal nodes.
- **Loop prevention** is multi-layered: `rounds` guard, per-step retry counter, `total_debug_attempts` cap, and tool-call budget all fire independently.
- **Tool safety** is defense-in-depth: DANGEROUS_PATTERNS normalisation → SAFE_COMMANDS allowlist → workspace_guard protected-path check → safe_resolve path-traversal prevention.
- **State management** is explicit: 58 typed AgentState fields, all initialized in `initial_state()`.
- **Concurrent pre-retrieval** in perception_node uses `asyncio.gather()` for search_code, find_symbol, and find_references simultaneously (PB-3 fix).
- **Thread-safe SQLite** with WAL mode, thread-local connections, and RLock.
- **Role-prompt system** cleanly separates identity (SOUL.md) from role (operational/strategic) from session context.
- **Reasoning-model support** (`thinking_utils.py`) handles DeepSeek-R1-Distill and Qwen3 correctly (budget doubling, strip_thinking).
- **Adapter architecture** is cleanly abstracted: `OpenAICompatibleAdapter` base, LM Studio and OpenRouter extend it.

---

## 3. Critical Architectural Flaws

### CF-1 — Async delegation_node in sync LangGraph context  [CRITICAL]
**Files:** `src/core/orchestration/graph/nodes/delegation_node.py`
LangGraph nodes must be synchronous. `delegation_node` is declared `async def` and contains `await` calls for PRSW file-locking and wave coordination. The `await` expressions will never execute in practice; the PRSW execution path is silently broken. Any scenario requiring parallel subagent writes produces incorrect sequential behavior with no error.

**Fix:** Either wrap async calls with `asyncio.run()` inside a sync node wrapper, or use a thread-pool bridge pattern consistent with the rest of the codebase.

---

### CF-2 — planning→validator→planning loop has no dedicated counter  [CRITICAL]
**Files:** `src/core/orchestration/graph/builder.py`
The rounds guard (fires at rounds ≥ 8) only increments inside `perception_node`. If planning consistently produces an invalid plan, the graph cycles `planning → plan_validator → planning` indefinitely without ever reaching perception. This loop has no explicit iteration limit of its own.

**Fix:** Add a `plan_attempts` counter in AgentState, increment in planning_node, and add a guard in `should_after_plan_validator()` to route to END/error after 3 failed attempts.

---

### CF-3 — evaluation→replan cycles bypass the rounds guard  [CRITICAL]
**Files:** `src/core/orchestration/graph/nodes/evaluation_node.py`, `builder.py`
`evaluation_node → replan_node → step_controller → execution → verification → evaluation` is a complete cycle that never passes through `perception_node`. The `rounds` counter therefore never advances. A consistently-failing subtask can loop in this inner cycle indefinitely — prevented only by `total_debug_attempts` (cap 9) if debug is triggered, but NOT otherwise.

**Fix:** Either increment `rounds` in `replan_node`, or add an independent `replan_attempts` counter with a hard cap.

---

### CF-4 — asyncio.Event misuse in preview_service  [CRITICAL]
**File:** `src/core/orchestration/preview_service.py:29`
`DiffPreview` uses `asyncio.Event` as a dataclass field with `default_factory=asyncio.Event`. `asyncio.Event()` requires a running event loop at construction time. When instantiated inside a LangGraph node (which runs in a thread pool), there is no running event loop, causing `RuntimeError`. The module is currently unintegrated (0% wired), masking this bug — but the moment it is connected it will crash.

**Fix:** Replace with `threading.Event`, consistent with the rest of the codebase.

---

## 4. High-Risk Safety Issues

### HR-1 — SymbolGraph call-graph results NOT passed to planner as structured data  [HIGH]
**Files:** `src/core/orchestration/graph/nodes/analysis_node.py:254–272`
`sg.find_calls(sym)` correctly retrieves callers of each key symbol, and `sg.find_tests_for_module()` correctly retrieves test file mappings. Both results are then **converted to prose** (string concatenation) and appended to `analysis_summary`. The planning LLM receives text like `'authenticate' called by: api.py, middleware.py` instead of a structured JSON object. The planner cannot reliably extract call sites from prose and frequently omits dependent files from its plans — leading to verification failures downstream.

**Fix:** Add `call_graph: dict` and `test_map: dict` fields to AgentState; populate them in analysis_node; inject as JSON block in planning_node prompt.

---

### HR-2 — Plan validator warnings disabled by default  [HIGH]
**File:** `src/core/orchestration/graph/nodes/plan_validator_node.py:211–216`
`enforce_warnings = state.get("plan_enforce_warnings", False)` and `strict_mode = state.get("plan_strict_mode", False)` default to False. This means a plan that omits verification steps or edits files without a prior read generates only warnings that are silently discarded. Plans with these flaws proceed to execution unchanged.

**Fix:** Set defaults to `True` for both flags, or set `enforce_warnings=True` at minimum.

---

### HR-3 — Integration tests never run in default CI  [HIGH]
**File:** `.github/workflows/ci.yml:63`
Integration tests require `RUN_INTEGRATION=1` env var. This is never set in the CI workflow. All 12 integration test files are permanently skipped in every PR and push. LLM adapter bugs, provider configuration errors, and orchestrator integration failures can only be discovered by users.

**Fix:** Add a scheduled CI job (e.g., nightly) with `RUN_INTEGRATION=1` and a reachable Ollama instance, or add a mock-backend integration job that always runs.

---

### HR-4 — E2E scenario suite is entirely stubs  [HIGH]
**File:** `tests/e2e/__init__.py:46–88`
All 6 E2E tests call `pytest.skip("Test implementation pending")`. There are zero real end-to-end scenario tests. Multi-step planning regressions, loop-prevention failures, and agent reasoning breakdowns are entirely invisible to the test suite.

**Fix:** Implement at minimum: (1) single-step file edit, (2) multi-step refactor with tests, (3) debug-loop recovery scenario, using the mock-LLM pattern from integration tests.

---

### HR-5 — todo_tools.py manage_todo has unreachable duplicate code  [HIGH]
**File:** `src/tools/todo_tools.py:113–132`
The `create` action branch contains two code blocks. Lines 114–122 handle creation with `depends_on` support and return. Lines 123–132 are a copy-paste duplicate that is unreachable dead code. If the first block is accidentally removed or reorganized, dependency information is silently lost.

**Fix:** Remove lines 123–132 (the duplicate unreachable block).

---

### HR-6 — Token budget monitoring disconnected from distiller  [HIGH]
**Files:** `src/core/orchestration/token_budget.py`, `src/core/orchestration/graph/builder.py`
`token_budget.py` defines `check_budget()` which returns `"compact"` when usage exceeds threshold. `memory_update_node.py` reads `state["_should_distill"]`. Neither node calls `check_budget()`, and no node ever sets `_should_distill = True` based on token usage. The token budget monitor is completely disconnected from the distillation pipeline. On long tasks, context grows without bound with no automatic compaction triggered.

**Fix:** Call `check_budget(state)` in execution_node or memory_update_node; set `_should_distill=True` if result is `"compact"`.

---

### HR-7 — No retry logic in any LLM adapter  [HIGH]
**Files:** `src/core/inference/adapters/openai_compat_adapter.py`, `lm_studio_adapter.py`, `ollama_adapter.py`
All adapters call `_safe_post()` exactly once with no retry on transient failure. A single DNS timeout, VRAM eviction, or slow model load causes immediate task failure. Local LLM backends (Ollama, LM Studio) are especially prone to slow first-response times after model loading.

**Fix:** Add exponential backoff with 3 retries (backoff: 0.5s, 1s, 2s) for `requests.exceptions.RequestException` and `TimeoutError`.

---

### HR-8 — providers.json write not atomic  [HIGH]
**File:** `src/ui/views/settings_panel.py:116`
`cfg_path.write_text(json.dumps(raw, indent=2))` writes directly to the config file without a tmp-file + rename pattern. A process crash, power loss, or signal during the write will leave a partially-written, corrupt JSON file that prevents all subsequent startups.

**Fix:** Write to `cfg_path.with_suffix('.tmp')`, then `tmp.rename(cfg_path)`.

---

## 5. Major Missing Capabilities

### MC-1 — Test-aware planning
The agent never automatically includes test updates in generated plans. The SymbolGraph already provides `find_tests_for_module()` (analysis_node:264) but the result is not surfaced to the planner. Plans that modify `user_service.py` routinely omit `test_user_service.py`, causing verification failures.

### MC-2 — Dependency-aware planning
The planner receives a flat file list. It has no structured call-graph to detect that modifying `authenticate()` also requires updating 12 call sites in `api.py` and 3 in `middleware.py`. The SymbolGraph has this data; it is simply not forwarded.

### MC-3 — Conversation compaction during active tasks
`distiller.compact_messages_to_prose()` is implemented but never called. Context grows unbounded during execution. On tasks with >50 turns, silent LLM truncation degrades reasoning quality with no warning.

### MC-4 — Decision memory
`SessionStore` has a `decisions` table (schema defined, lines 79–85). Neither the distiller nor any node ever calls `add_decision()`. Historical decision rationales are never persisted or retrieved.

### MC-5 — Plan mode enforcement
`plan_mode.py` defines `enable()`, `disable()`, `is_blocked()`. No tool executor checks `is_blocked()`. The feature is entirely disconnected.

### MC-6 — MCP/IDE integration
`mcp_stdio_server.py` (395 LOC) implements JSON-RPC 2.0 with MCP methods. All tool/resource/prompt handlers return stub empty results. The server is never instantiated. IDE integration does not exist.

---

## 6. Workflow Reliability Issues

### WR-1 — Wave execution does not recover from partial wave failure
**File:** `src/core/orchestration/graph/nodes/execution_node.py:685–694`
Wave advancement (`current_wave += 1`) only triggers when ALL steps in the current wave complete successfully. If step_1 of wave_0 fails after step_0 succeeds, `current_wave` remains 0 and the system retries step_1 up to `MAX_STEP_RETRIES`. After retries exhausted, a replan is triggered — but replan_node does not reset or adjust `current_wave`, so the new plan's wave state is incoherent.

### WR-2 — Silent analysis failure masks missing context
**File:** `src/core/orchestration/graph/nodes/analysis_node.py:235`
```python
except Exception as e:
    logger.error(f"analysis_node: analysis failed: {e}")
    analysis_summary = f"Analysis failed: {e}"
```
Execution continues with `relevant_files = []` and `key_symbols = []`. Downstream planning receives an empty context without any state flag indicating the analysis was incomplete.

### WR-3 — plan_resumed field set but never checked
**File:** `src/core/orchestration/graph/nodes/planning_node.py:115`
`plan_resumed = True` is set when a prior plan is loaded from disk, but no router or node branches on this flag. Resumed plans are treated identically to fresh plans.

### WR-4 — plan_mode_approved flag never reset
**File:** `src/core/orchestration/graph/builder.py:949`
`plan_mode_approved` is set by `wait_for_user_node` but never cleared. If `wait_for_user` is triggered a second time in the same session, the stale `True` value will skip the approval gate.

### WR-5 — execution_node has too many responsibilities
**File:** `src/core/orchestration/graph/nodes/execution_node.py` (~900 LOC)
Single node handles: tool-call generation, read-before-write enforcement (security check), tool cooldown enforcement, wave advancement logic, step completion tracking. A bug in any of these concerns can produce subtle cross-cutting side effects that are difficult to isolate.

---

## 7. Tool System Weaknesses

### TS-1 — edit_by_line_range missing integer type coercion  [Medium]
`start_line` and `end_line` are accepted as parameters but not validated/coerced to `int`. If the LLM passes them as strings, behavior is undefined.

### TS-2 — run_tests() workdir not safe_resolve'd  [Medium]
`workdir` is passed directly to `subprocess.run(cwd=workdir)` without path validation. All other file tools use `_safe_resolve()`.

### TS-3 — syntax_check() has no timeout on directory walk  [Medium]
`os.walk()` has no timeout. On monorepos with hundreds of thousands of Python files, this call can block for minutes.

### TS-4 — npm allowlist fragile  [Low]
`"run "` (with trailing space) is an item in the npm subcommand check, which would reject the valid command `npm run-script`. `npm ci` (safe lock-file install) is absent from the allowlist.

### TS-5 — run_js_tests() exits on first runner with config error  [Low]
If `jest` exists but its config file is broken, `run_js_tests()` returns an error instead of falling through to `vitest` or `mocha`.

### TS-6 — No rename/move tool  [Low]
Users must read + write + delete (3 operations) to rename a file. Alternatively must use `bash("mv ...")` which requires a workdir argument not in the SAFE_COMMANDS list.

---

## 8. Repository Awareness Gaps

### RA-1 — SymbolGraph test-file mapping discarded before planning  [HIGH — duplicate of HR-1]
See HR-1. `sg.find_tests_for_module()` is called; result converted to prose; structured mapping lost.

### RA-2 — LLM plan output is rarely valid DAG JSON  [Medium]
`_parse_plan_content()` in planning_node has 4 fallback strategies. The first (JSON DAG) is the only format that supports wave-based parallel execution. In practice the LLM produces markdown lists that fall through to strategy 3 (regex bullet parsing), generating flat sequential plans with no dependency information.

**Root cause:** The planning prompt does not include enough few-shot DAG examples, and the strategic role prompt does not explicitly require JSON output.

### RA-3 — Perception does not retrieve test files for task symbols  [Medium]
`search_code`, `find_symbol`, and `find_references` run at perception time, but `find_tests_for_module` is not called. The perception context never includes test files, so the LLM does not know which tests cover the code being modified.

### RA-4 — Complexity heuristic uses substring matching  [Medium]
`_task_is_complex()` in builder.py checks `"add " in task` and `"edit " in task` as plain substrings. This produces false positives: "address the issue" → complex; "credential edit" → complex.

---

## 9. Memory System Evaluation

### ME-1 — ContextController is dead code  [HIGH]
**File:** `src/core/context/context_controller.py` (227 LOC)
`enforce_budget()` and `extract_relevant_snippets()` are defined and exported but never imported or instantiated by any other module. This class appears to predate the `context_builder.py` quota system and was never removed.

**Fix:** Delete or integrate.

### ME-2 — Vector store not wired into session retrieval  [Medium]
`VectorStore.add_memory()` and `search_memories()` exist and work. Neither is called by the distiller, nor by any node. Semantic retrieval of prior task decisions is unavailable.

### ME-3 — Distiller only compresses last 20 messages  [Medium]
**File:** `src/core/memory/distiller.py:161`
`for m in messages[-20:]` — early messages (turns 1–80) are completely excluded from the distilled summary. For long tasks, the problem statement and initial analysis context are lost.

### ME-4 — Distiller LLM output not schema-validated  [Medium]
The distiller calls the LLM requesting JSON with required keys (`current_task`, `files_modified`, `next_step`, etc.) then catches `json.JSONDecodeError` but does NOT validate that returned keys are present. Incomplete JSON causes silent empty distilled state.

### ME-5 — Provider context length loaded on every build  [Low]
`provider_context.get_context_budget()` reads `providers.json` from disk on every call with no TTL cache. This causes repeated filesystem I/O on every prompt construction.

### ME-6 — Plans and decisions tables never populated  [Low]
`SessionStore` creates `plans` and `decisions` tables. No code path calls `add_plan()` or `add_decision()`. The schema exists but is permanently empty.

---

## 10. Evaluation and Testing Gaps

### ET-1 — Zero real E2E tests  [CRITICAL → see HR-4]

### ET-2 — Integration tests permanently skipped in CI  [HIGH → see HR-3]

### ET-3 — No code coverage measurement  [High]
`pytest` is run without `--cov`. Coverage regressions on PRs are invisible.

### ET-4 — Single-platform CI (macOS only)  [Medium]
`.github/workflows/ci.yml` targets `macos-latest` exclusively. Windows path-handling bugs, Linux permission issues, and Python 3.12 incompatibilities are undetected.

### ET-5 — Async cancellation tested via source inspection, not execution  [Medium]
`test_audit_vol8.py:724–776` verifies cancellation behavior by calling `inspect.getsource()` and asserting string patterns are present. This does not actually execute the async cancellation path.

### ET-6 — Vector store tests skipped  [Low]
`tests/unit/test_session_lifecycle.py:170–172` skips the vector-store semantic search test. Semantic retrieval is never validated.

### ET-7 — No benchmark harness  [Low]
No test measures response latency, token usage, or success rate on canonical coding tasks.

---

## 11. Usability Problems

### UP-1 — TUI conversation history lost on restart  [Medium]
`TextualAppBase.history` is an in-memory list. Restarting the TUI loses all prior conversation context. No persistence to `.agent-context/tui_history.jsonl`.

### UP-2 — Provider health check blocks for 5s on startup  [Medium]
**File:** `src/ui/app.py:115–130`
`run_provider_health_check_sync(timeout=5.0)` executes synchronously in the app constructor. If all providers are down, startup is delayed 5 seconds with no progress indication.

### UP-3 — Textual startup exception leaks resources  [Medium]
**File:** `src/ui/app.py:197`
If Textual raises during `app.run()`, the exception is caught and logged, but `self.shutdown()` is **not called**. Background threads (session watcher, health monitor, cross-session bus) remain running.

### UP-4 — Settings panel API key accepts empty string  [Low]
`save_api_key()` persists and injects whatever string is provided, including empty strings, which will cause silent auth failures on first request.

### UP-5 — No debugging guide in docs  [Low]
`docs/DEVELOPMENT.md` lacks a troubleshooting section, instructions for inspecting AgentState mid-run, or guidance on reading LangGraph trace output.

---

## 12. Performance Bottlenecks

### PB-1 — Conversation compaction never triggered  [High]
Context grows without bound during task execution. `compact_messages_to_prose()` is implemented but never called. On 50+ turn tasks, context window truncation degrades reasoning silently.

### PB-2 — Provider context length read from disk on every prompt build  [Low]
See ME-5. ~1 disk read per node invocation (typically 8–12 nodes per task = 8–12 unnecessary reads per task).

### PB-3 — syntax_check() directory walk has no timeout  [Low]
See TS-3.

### PB-4 — No background pre-indexing  [Low]
`index_repository()` runs lazily on first analysis request. On large repos (>5K files), first-task analysis blocks for several seconds.

---

## 13. Over-Engineered / Unintegrated Components

The following modules were added recently but have near-zero integration with the main agent loop. They represent ~1,420 LOC of maintenance burden.

| Module | LOC | Integration | Verdict |
|--------|-----|-------------|---------|
| `mcp_stdio_server.py` | 395 | 0% (stub handlers, never instantiated) | Remove or complete |
| `preview_service.py` | 145 | 0% (never imported) | Remove or fix async bug first |
| `plan_mode.py` | 54 | 0% (never enabled) | Remove or wire in |
| `agent_session_manager.py` | 280 | ~1% (optional in delegation_node) | Fix race condition or remove |
| `cross_session_bus.py` | 546 | ~5% (only cleanup path) | Wire fully or remove |
| `token_budget.py` | 200 | 0% connected to distiller | Wire to distiller or remove |
| `context_controller.py` | 227 | 0% (dead code, replaced by context_builder) | Delete |

Total: **1,847 LOC** of unintegrated code.

---

## 14. Prioritized Fix List

### Phase 1 — Critical Stability Fixes

| ID | Issue | Location | Complexity | Impact |
|----|-------|----------|-----------|--------|
| P1-1 | Fix async delegation_node (sync/async mismatch) | `graph/nodes/delegation_node.py` | Medium | CRITICAL — PRSW path silently broken |
| P1-2 | Add planning-loop iteration counter | `graph/builder.py`, `graph/state.py` | Low | CRITICAL — prevents infinite planning loop |
| P1-3 | Add replan_attempts counter to inner replan cycle | `evaluation_node.py`, `builder.py` | Low | CRITICAL — closes rounds-bypass loop |
| P1-4 | Fix asyncio.Event in preview_service | `preview_service.py:29` | Trivial | CRITICAL — runtime crash when wired |
| P1-5 | Remove duplicate dead code in manage_todo | `src/tools/todo_tools.py:123–132` | Trivial | HIGH — silent data loss |
| P1-6 | Enable plan validator warnings by default | `graph/nodes/plan_validator_node.py:212` | Trivial | HIGH — invalid plans execute |
| P1-7 | Make providers.json write atomic | `src/ui/views/settings_panel.py:116` | Low | HIGH — prevents config corruption |
| P1-8 | Fix Textual exception missing shutdown() | `src/ui/app.py:197` | Trivial | HIGH — resource leak on startup failure |

---

### Phase 2 — Robustness Improvements

| ID | Issue | Location | Complexity | Impact |
|----|-------|----------|-----------|--------|
| P2-1 | Add retry logic to all LLM adapters | `adapters/openai_compat_adapter.py`, `ollama_adapter.py`, `lm_studio_adapter.py` | Medium | HIGH — resilience to transient failures |
| P2-2 | Wire token budget → distiller compaction | `token_budget.py`, `memory_update_node.py` | Medium | HIGH — prevents context overflow |
| P2-3 | Add conversation compaction checkpoint | `distiller.py` | Medium | HIGH — prevents silent truncation at 50+ turns |
| P2-4 | Validate distiller JSON output schema | `distiller.py:207` | Low | Medium — prevents silent empty distillation |
| P2-5 | Add workdir safe_resolve to run_tests() | `verification_tools.py` | Trivial | Medium — consistency with other tools |
| P2-6 | Coerce start_line/end_line to int in edit_by_line_range | `file_tools.py` | Trivial | Medium — prevents LLM string-passing bugs |
| P2-7 | Add plan_attempts counter to AgentState | `graph/state.py`, `planning_node.py` | Low | Medium — detect repeated invalid plans |
| P2-8 | Fix wave execution partial-failure recovery | `execution_node.py:685–694` | Medium | Medium — prevents stuck wave state |
| P2-9 | Clear plan_mode_approved after use | `builder.py` | Trivial | Medium — prevents stale gate bypass |
| P2-10 | Add syntax_check() timeout on directory walk | `verification_tools.py` | Low | Medium — prevents blocking on large repos |

---

### Phase 3 — Capability Improvements

| ID | Issue | Location | Complexity | Impact |
|----|-------|----------|-----------|--------|
| P3-1 | Pass call_graph + test_map as JSON to planner | `analysis_node.py`, `planning_node.py`, `graph/state.py` | Medium | HIGH — enables dependency-aware planning |
| P3-2 | Add test-file retrieval to perception (round 0) | `perception_node.py` | Low | HIGH — test files available from start |
| P3-3 | Implement 5+ real E2E scenario tests | `tests/e2e/` | High | HIGH — closes the largest test gap |
| P3-4 | Enable integration tests in CI (scheduled job) | `.github/workflows/ci.yml` | Low | HIGH — catches adapter regressions |
| P3-5 | Add --cov to CI unit test command | `.github/workflows/ci.yml` | Trivial | Medium — coverage visibility |
| P3-6 | Add few-shot DAG examples to planning prompt | `strategic.md`, `planning_node.py` | Low | Medium — increases valid DAG output rate |
| P3-7 | Wire vector store to distiller for decision memory | `distiller.py`, `vector_store.py` | Medium | Medium — enables cross-task learning |
| P3-8 | Delete ContextController (dead code) | `src/core/context/context_controller.py` | Trivial | Low — removes 227 LOC maintenance burden |
| P3-9 | Expand CI matrix to ubuntu-latest + Python 3.12 | `.github/workflows/ci.yml` | Low | Medium — cross-platform coverage |
| P3-10 | Persist TUI conversation history | `textual_app_impl.py` | Low | Medium — user experience improvement |

---

### Phase 4 — Advanced Features

| ID | Issue | Location | Complexity | Impact |
|----|-------|----------|-----------|--------|
| P4-1 | Fully integrate cross_session_bus into delegation path | `delegation_node.py`, `cross_session_bus.py` | High | Medium — enables real P2P multi-agent |
| P4-2 | Complete MCP stdio server handlers | `mcp_stdio_server.py` | High | Medium — enables IDE integration |
| P4-3 | Implement benchmark harness (latency, token cost, success rate) | `tests/benchmarks/` | Medium | Medium — performance regression detection |
| P4-4 | Enable plan_mode gate in tool executor | `plan_mode.py`, execution path | Medium | Medium — user-controlled execution approval |
| P4-5 | Auto-suggest test steps in planning based on modification patterns | `planning_node.py` | Medium | High — closes test-omission failure mode |
| P4-6 | Add linting/type-checking gates to CI | `.github/workflows/ci.yml` | Low | Medium — code quality enforcement |

---

## Summary

The CodingAgent is well-designed at its core and has benefited enormously from the prior audit cycles. The immediate priority is **cleanup**: 1,847 LOC of unintegrated or dead code should be removed or completed, four critical correctness bugs (async mismatch, planning loop, rounds-bypass, asyncio.Event) must be fixed, and the CI pipeline needs real integration and E2E coverage.

The next major capability leap is **structured planning intelligence**: forwarding SymbolGraph call-graph and test-map data as JSON to the planning LLM. This single change would dramatically improve multi-file task success rates by enabling dependency-aware and test-aware plan generation.

**Recommended immediate action (Phase 1):** 8 fixes, all Low–Trivial complexity, addressable in a single session.
