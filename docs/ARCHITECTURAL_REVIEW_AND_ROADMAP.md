# CodingAgent Architectural Review and Stabilization Roadmap

**Review date:** 2026-08-19  
**Scope:** Current `main` branch, with emphasis on the local-first runtime, orchestration, persistence, server exposure, provider resilience, and test strategy.

## Executive assessment

CodingAgent has a strong product foundation: a local-first provider abstraction, hardware-aware model tiers, a rich tool surface with multiple guard layers, durable SQLite/JSONL session storage, and a substantial automated test suite. The main architectural risk is not missing capability; it is that behavior has evolved faster than its contracts and documentation.

The next optimization cycle should prioritize explicit safety and state semantics before broad decomposition. The highest-leverage sequence is:

1. Stabilize approval, remote exposure, persistence, and state ownership.
2. Classify event delivery guarantees and add real concurrency contracts.
3. Introduce bounded/streaming persistence reads and a shared provider resilience policy.
4. Only then replace free-form graph state mutation or perform broad module refactors.

## Current architecture

### Strengths to preserve

- **Local-first inference is a first-class design constraint.** Ollama, LM Studio, and OpenAI-compatible providers share the same orchestration layer rather than being UI-specific integrations.
- **Model-tier routing is a useful differentiator.** The NANO-to-FRONTIER profile can reduce prompt/tool pressure for constrained local models.
- **Tool execution has layered controls.** Workspace guards, shell analysis, permission checks, read-before-write rules, and rollback support provide defense in depth.
- **Persistence matches the product.** SQLite and JSONL keep sessions portable and offline while supporting snapshots, forks, and recovery.
- **The test surface is broad.** Hundreds of test modules cover routing, providers, persistence, tools, the TUI bridge, and server behavior.

### Main risks

#### 1. Runtime contracts lag implementation

The default fast-path graph had evolved from the documented six-node pipeline to nine compiled nodes while describing itself as ten. Approval-required routes mapped directly to execution because `wait_for_user` was not compiled. This made a stabilization shortcut a hidden product policy.

**Decision:** The active fast path is a ten-node graph and includes `wait_for_user`. Approval is a security boundary, not an optional optimization.

#### 2. State ownership is too loose

`AgentState` is a large mutable `TypedDict`; validation logs but does not reject invalid transitions. Separately, session hydration returned the manager's live mutable `SessionState`, allowing a consumer to mutate internal state after the lock was released.

**Decision:** Short term, hydration returns isolated snapshots and lock acquisition is non-nested. Medium term, graph nodes should own explicit input/result schemas and transitions should be validated at graph boundaries.

#### 3. Event delivery guarantees are implicit

The legacy `EventBus` dual-emits into an asynchronous `MessageBus`. The typed bus has a bounded queue and drops on pressure. That is acceptable for replaceable UI telemetry but unsafe if lifecycle or persistence consumers assume delivery.

**Decision:** Typed events are centrally classified as lossy telemetry, ordered lifecycle, or reliable. Only explicitly replaceable high-volume events are telemetry; unclassified events default to reliable. Each class has independent bounded admission so telemetry saturation cannot consume reliable capacity. Reliable admission waits for capacity and raises visibly on timeout rather than dropping.

#### 4. Persistence limits are not caller-visible

JSONL reads stop at 10,000 records and log truncation, but callers still receive a normal-looking partial list. Snapshot references also crossed a destructive file boundary and needed containment checks.

**Decision:** Destructive snapshot operations stay confined to the selected session's JSONL files. Bounded reads must expose pagination/truncation to callers rather than silently returning partial history.

#### 5. Remote server safety depends on launch choices

Task endpoints can invoke LLM-driven file and shell tooling. Authentication is optional for local use, which is appropriate only when the server is loopback-bound.

**Decision:** Loopback may run without a token. Any non-loopback bind requires `CODINGAGENT_ADMIN_TOKEN`.

#### 6. Tests over-represent deterministic mocks

The suite is large, but the `sync_threads` fixture turns thread and executor work into inline calls. It is useful for deterministic unit tests but cannot prove queue pressure, cancellation, FIFO behavior, shutdown, or lock safety.

**Decision:** Keep deterministic unit tests and add a small, separate concurrency/fault-injection contract suite.

## Baseline quality snapshot

As of the QUAL-01/TEST-02 pass (ruff + mypy gates fail closed):

- The full unit suite passes from a clean environment: **4659 passed, 1 skipped, 0 xfail/xpass** (includes the 14-test real-concurrency contract suite from TEST-01, the 23-test STATE-01 boundary suite, the 30-test PERF-02 resilience-policy suite, and the 9-test PERF-01 JSONL + 8-test PERF-01 Sqlite streaming-pagination suites).
- Ruff passes on both the default ruleset and the CI gate (`--select=E,F,W --ignore=E501`) with zero errors. CI no longer masks failures with `|| true`.
- The CI mypy gate (narrow file list) passes with zero errors; the `|| true` masking was removed.
- The `patch` executable dependency was eliminated by making file-edit and unit tests portable (`ad75951`); `patch` is no longer required.
- A separate concurrency/fault-injection contract suite remains to be added as TEST-01.

## Task roadmap

Tasks are ordered by dependency. Each task should land as its own commit or pull request with focused acceptance tests.

### Tranche A — Stabilization and security

#### STAB-01 — Make active graph policy explicit

**Status:** Completed in this review  
**Risk:** Low  
**Depends on:** None

- Compile `wait_for_user` in the default fast path.
- Route approval-required plan and execution outcomes through it.
- Align architecture and contributor documentation with the ten active nodes.

**Acceptance:** The compiled fast path contains `wait_for_user`; approval routes cannot reach execution without the wait node.

#### STAB-02 — Enforce safe server exposure

**Status:** Completed in this review  
**Risk:** Low  
**Depends on:** None

- Allow unauthenticated operation only for loopback binds.
- Reject non-loopback startup without an admin token.
- Follow up by applying the same token policy to WebSocket handshakes.

**Acceptance:** `127.0.0.1`, loopback IPv6, and `localhost` start without a token; wildcard, LAN, and hostname binds fail closed without one.

#### STAB-03 — Harden snapshot revert semantics

**Status:** Completed in this review  
**Risk:** Low  
**Depends on:** None

- Resolve and validate snapshot targets under the session directory.
- Restrict the target filename to the selected session.
- Perform existence/open handling inside the per-session lock.

**Acceptance:** A tampered sidecar cannot truncate files outside the session store or a different session; a target removed before open becomes a safe no-op.

#### STAB-04 — Isolate hydration snapshots

**Status:** Completed in this review  
**Risk:** Low  
**Depends on:** None

- Flush P2P messages before taking the session-state lock.
- Return a deep snapshot instead of the manager's live mutable object.

**Acceptance:** Mutating a returned hydration object does not change manager state; tests exercise concurrent buffer/flush behavior without nested lock acquisition.

#### STAB-05 — Complete remote transport authentication

**Status:** Completed in this review  
**Risk:** Low  
**Depends on:** STAB-02

- Authenticate WebSocket upgrades using the same admin token policy as HTTP/SSE.

**Acceptance:** Unauthorized WebSocket clients are closed with a policy violation; authenticated clients retain current behavior.

#### STAB-06 — Define production endpoint exposure

**Status:** Completed in this review  
**Risk:** Low  
**Depends on:** STAB-02, STAB-05

- **Single source of truth added** — `src/server/exposure_policy.py` now carries the exposure/auth registry (`ENDPOINT_POLICIES`) for every route: `/health` (always public), `/metrics` (optional HTTP Basic via `CODINGAGENT_METRICS_AUTH`), and all session/scheduler/task/SSE/WebSocket endpoints (admin token via header when `CODINGAGENT_ADMIN_TOKEN` is set).
- **Enforceable acceptance** — `tests/unit/test_exposure_policy.py` asserts key policy decisions and that **every route on the FastAPI app has a documented policy** (`unregistered_routes(app.routes)`), so a new endpoint cannot be added without documenting its exposure and auth.
- **Production deployment guide added** — `docs/PRODUCTION_DEPLOYMENT.md` documents the bind/exposure security model (loopback default + `validate_server_exposure` fail-closed guard), the per-endpoint policy table, TLS/reverse-proxy expectations (proxy terminates TLS, forwards WS/SSE and auth headers, blocks `/docs*`/`/redoc*`/`/openapi.json`), and a deployment checklist.

**Acceptance:** Every HTTP/SSE/WebSocket endpoint has a documented exposure and authentication policy (registry + contract test + deployment guide).

### Tranche B — Delivery and state contracts

#### EVENT-01 — Classify event durability

**Status:** Completed
**Risk:** Medium  
**Depends on:** STAB-01

- Event-delivery classification now distinguishes telemetry, ordered lifecycle, and reliable events.
- Telemetry remains lossy and bounded under pressure.
- Reliable and ordered events use separate bounded lanes with blocking admission and a structured failure if their deadline expires.
- Full-pipeline queue depth/high-water marks, admissions, failures, drops, delivery counts, and bounded admission-to-completion latency samples are exposed by class.

**Acceptance:** Queue saturation tests prove reliable events continue through telemetry pressure; reliable exhaustion raises rather than dropping; telemetry drops remain bounded and observable; ordered shutdown drains preserve FIFO delivery.

#### STATE-01 — Define node-owned state schemas

**Status:** In progress (boundary layer + scenario tests done)  
**Risk:** High  
**Depends on:** STAB-01, EVENT-01

- Inventory which node reads, writes, and clears each `AgentState` field → exhaustive union of every return key for perception (21), planning (16), execution (31), verification (2).
- **Focused result schemas done** — `src/core/orchestration/graph/state_schemas.py` declares per-node `NodeOutputSchema` (allow-list superset + required core keys).
- **Boundary enforcement done** — `wrap_node()` validated at graph boundaries across all four graph compilers (full, fast-path, frontier, lite). Fail-open (default, non-strict): logs + emits typed `NodeResultValidationFailed` event via the orchestrator's event bus. Fail-closed (`strict=True`): raises structured `NodeResultViolation`.
- Remaining: migrate per-node *field ownership* (which node may write which shared field) beyond the current output-key structural contract, node-family by node-family.

**Acceptance:** Invalid transitions fail with a structured error; scenario tests cover fast, full, approval, cancellation, and recovery paths. Status: **met** — `tests/unit/test_state_schemas.py` (23 tests) asserts correct per-path results pass and invalid transitions raise structurally (`unknown_key` / `missing_core_key`) or surface the typed event; full unit suite green (4606 passed).

### Tranche C — Performance and resilience

#### PERF-01 — Replace partial all-record reads with streaming pagination

**Status:** In progress (JSONL + Sqlite pagination API, lazy summary, silent-truncation removal done)  
**Risk:** Medium  
**Depends on:** STAB-03

- **Lazy iterator API added (both backends)** — `JsonlSessionStore.iter_records(session_id)` yields records one line at a time across all rotation files, and `SqliteSessionStore.iter_records(session_id)` yields messages row-by-row from the messages table — both lazy and memory-bounded.
- **Cursor/page API added (both backends)** — `read_page(session_id, page_size, offset=0)` on JSONL and Sqlite returns `(records, has_more)`, giving explicit, non-silent pagination metadata.
- **Silent truncation removed** — `_read_all_records` no longer applies the destructive `_MAX_RECORDS = 10_000` cap; it now materialises the full session from the lazy iterator. A >10k-record session is fully reachable.
- **Analytics summary is lazy** — `JsonlSessionStore.get_session_summary` counts records by type from `iter_records` instead of materialising the whole session, so summaries of arbitrarily large sessions stay memory-bounded (output identical to a full read).
- **Tests** — `tests/unit/test_streaming_pagination.py` (9 tests) proves a 12,000-record JSONL session is traversed without truncation, pages cover every record exactly once, memory stays bounded by page size, and blank/malformed lines are skipped. `tests/unit/test_streaming_pagination_sqlite.py` (8 tests) proves the Sqlite backend exposes the same `iter_records`/`read_page` semantics (ordered messages, bounded pages, exact cover, empty session, argument validation) and that the lazy summary counts match a full read.
- Remaining: adopt the page API in the remaining export/analytics callers (`write_decisions_json`, `get_recent_sessions`).

**Acceptance:** A session larger than 10,000 records is traversable without silent truncation; peak memory stays bounded by page size.

#### PERF-02 — Unify provider resilience policy

**Status:** In progress (centralized policy + classifier adoption + fault-injection tests done)  
**Risk:** Medium  
**Depends on:** None

- **Centralized policy done** — `src/core/utils/retry.py` now owns the shared resilience contract: `ResiliencePolicy` dataclass with the shared phase timeouts (connect 10s, model-load 120s, first-token 120s, stream-idle 60s, request 120s), a single `RETRYABLE_STATUS_CODES` set with `is_retryable_status_code()` / `is_retryable_exception()` classifiers, and `async_retry` now uses the capped, jittered `jittered_backoff` (fixing the previous dead, uncapped twin).
- **Classifier adoption done** — `openai_compat_adapter._execute_with_retry` and `ollama_adapter._request_with_retry` now use the shared `is_retryable_status_code()` instead of their private hardcoded variants; ollama's backoff is now capped+jittered and retries 429.
- **Fault-injection tests done** — `tests/unit/test_resilience_policy.py` (30 tests) covers connection refusal, model warm-up, timeout, 429, 5xx, stream interruption, retry exhaustion (final error surfaced for fallback), backoff capping, and adapter adoption of the shared classifier. Full unit suite green (4636 passed).
- Remaining: retire the remaining literal timeout numbers inside adapters to `ResiliencePolicy`, and extend native generation-path retry to the Ollama chat/generate bodies (currently guarded only at the model-listing helper).

#### PERF-03 — Measure before decomposing hot paths

**Status:** Planned  
**Risk:** Low  
**Depends on:** PERF-01, PERF-02

- Add end-to-end timing for prompt assembly, provider wait, tool execution, persistence, and TUI delivery.
- Establish local-model scenarios and baseline budgets.
- Use profiles to choose module splits or native acceleration; do not optimize based on file size alone.

**Acceptance:** Benchmarks identify p50/p95 stage latency and memory for at least one Ollama and one LM Studio workflow.

### Tranche D — Test and quality gates

#### TEST-01 — Add real concurrency contracts

**Status:** Completed  
**Risk:** Medium  
**Depends on:** EVENT-01, STAB-04

- Added `tests/unit/test_concurrency_contracts.py` (14 tests) that deliberately do **not** use the `sync_threads` fixture/marker, exercising the production `threading.Thread` and async paths under real concurrency.
- Verified FIFO ordering within sequenced categories under multi-threaded publication (per-pair relative ordering, no loss), queue-pressure isolation (telemetry saturation does not starve reliable events; telemetry drops under pressure without blocking), reliable admission (blocking wait for capacity, and visible `ReliableEventAdmissionError` on exhaustion), shutdown under load (FIFO drain, clean post-shutdown rejection, no deadlock), owner-scoped cancellation under real threads, session-state hydration snapshot isolation, and real file-lock mutual exclusion across agents.
- Every test uses bounded synchronization timeouts so a regression fails fast rather than hanging the suite.

**Acceptance:** Repeated stress runs complete without deadlock, event reordering within a category, or post-shutdown logging loops — confirmed across repeated runs plus the full unit suite (4583 passed, 1 skipped, 0 xfail).

#### QUAL-01 — Make quality checks actionable

**Status:** Completed  
**Risk:** Low  
**Depends on:** None

- Removed the `|| true` masking from the Ruff and mypy CI gates; CI now fails on newly introduced lint/type errors.
- Fixed all 127 Ruff `W293` (blank-line whitespace) errors across `src/` and `tests/`; the CI gate (`--select=E,F,W --ignore=E501`) passes at zero.
- Fixed the 18 pre-existing mypy errors surfaced by the CI gate (per-symbol `**dict` splats converted to explicit kwargs in `McpServerStatus`, `OrchestratorStartup`, `PerceptionCorrectivePrompt`; lambda/loop-variable typing in `mcp/manager.py`; `Optional` fd typing in `repo_read_tools.py`; `_loop` None-guard in `bus.py`).
- File-edit and unit tests were made portable (in-process edit implementation), removing the external `patch` dependency.

**Acceptance:** CI fails on newly introduced lint/type errors and a clean environment can run file-edit tests from documented prerequisites.

#### TEST-02 — Triage current baseline failures

**Status:** Completed  
**Risk:** Low  
**Depends on:** QUAL-01

- The `patch` executable requirement was removed; the unit suite passes from a clean environment (**4569 passed, 1 skipped, 0 xfail**).
- The WebSocket backpressure and tool-result normalization concerns are covered by the passing suite; timing-sensitive assertions no longer retry, and remaining instability is tracked under TEST-01's real-concurrency contract suite.

**Acceptance:** The unit suite passes from a clean environment without timing retries.

## Recommended delivery sequence

1. ~~Run **QUAL-01** and **TEST-02** so the baseline is trustworthy.~~ **Completed** — CI gates fail closed; unit suite green.
2. ~~Define the production surface in **STAB-06**.~~ **Completed** — `src/server/exposure_policy.py` registry + contract tests + `docs/PRODUCTION_DEPLOYMENT.md`.
3. ~~Implement **EVENT-01**, followed by **TEST-01**.~~ **Completed** — delivery classes live and the real-concurrency contract suite (`test_concurrency_contracts.py`) validates them.
4. ~~Implement **PERF-01** and **PERF-02** independently.~~ — **PERF-02's centralized resilience policy is landed** (shared timeouts/classifier/capped-jittered backoff + 30 fault-injection tests). **PERF-01's JSONL + Sqlite bounded-memory pagination API is landed** (`iter_records`/`read_page` on both backends + lazy `get_session_summary` + removal of the silent 10k cap + 17 tests); remaining PERF-01 work is export/analytics caller adoption. **PERF-03** (measurement) remains.
5. Use **PERF-03** evidence to scope optimization.
6. ~~Start **STATE-01** only after delivery and concurrency contracts protect behavior.~~ — Contracts are in place and **STATE-01's boundary layer is implemented** (node output schemas + enforcement wrapper + 23 scenario tests). Remaining STATE-01 work is the per-node field-ownership migration.

With the baseline, delivery, concurrency, STATE-01 boundary, PERF-02 resilience-policy, STAB-06 endpoint-exposure, and PERF-01 JSONL+Sqlite pagination work landed, the next highest-leverage tasks are **PERF-03** (measurement to scope optimization) or completing the remaining **PERF-01**/**PERF-02**/**STATE-01** migrations (export/analytics caller adoption for pagination; literal-timeout retirement and generation-path retry in the Ollama adapter; per-node field ownership).