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

**Decision needed:** Every event class must be designated as lossy telemetry or durable lifecycle. Critical events should use synchronous acknowledgement or persistence before publication.

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

Before review changes:

- The focused architectural/security tests passed after the fixes in this review.
- The full unit run exposed five pre-existing failures.
- Three failures require the external `patch` executable, which is not declared as a development prerequisite.
- One WebSocket backpressure assertion is timing-sensitive.
- One orchestrator tool-normalization failure needs independent triage.
- Ruff reports 134 existing errors, while CI currently runs Ruff and mypy with non-blocking `|| true`.

These are baseline issues, not regressions introduced by this review.

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

**Status:** Planned  
**Risk:** Low  
**Depends on:** STAB-02, STAB-05

- Decide whether `/health` and `/metrics` remain public on authenticated non-loopback deployments.
- Document TLS termination and reverse-proxy expectations.
- Add a production deployment checklist.

**Acceptance:** Every HTTP/SSE/WebSocket endpoint has a documented exposure and authentication policy.

### Tranche B — Delivery and state contracts

#### EVENT-01 — Classify event durability

**Status:** Planned  
**Risk:** Medium  
**Depends on:** STAB-01

- Add an event-delivery classification: telemetry, ordered lifecycle, or durable.
- Keep telemetry lossy under pressure.
- Require acknowledged delivery or write-ahead persistence for durable events.
- Expose queue depth, drops, and delivery latency by class.

**Acceptance:** Queue saturation tests prove lifecycle/persistence events are not silently lost; telemetry drop behavior remains bounded and observable.

#### STATE-01 — Define node-owned state schemas

**Status:** Planned  
**Risk:** High  
**Depends on:** STAB-01, EVENT-01

- Inventory which node reads, writes, and clears each `AgentState` field.
- Introduce focused result schemas for perception, planning, execution, and verification.
- Enforce valid transitions at graph boundaries instead of logging only.
- Migrate one node family at a time with adapters for compatibility.

**Acceptance:** Invalid transitions fail with a structured error; scenario tests cover fast, full, approval, cancellation, and recovery paths.

### Tranche C — Performance and resilience

#### PERF-01 — Replace partial all-record reads with streaming pagination

**Status:** Planned  
**Risk:** Medium  
**Depends on:** STAB-03

- Add a lazy record iterator and cursor/page API.
- Update history, export, message-since, and analytics callers.
- Return explicit `has_more`/truncation metadata.
- Benchmark large rotated sessions for peak memory and latency.

**Acceptance:** A session larger than 10,000 records is traversable without silent truncation; peak memory stays bounded by page size.

#### PERF-02 — Unify provider resilience policy

**Status:** Planned  
**Risk:** Medium  
**Depends on:** None

- Define shared connect, model-load, first-token, and stream-idle timeouts.
- Centralize retryable status/error classification, capped exponential backoff, and jitter.
- Apply it to Ollama model info/generation and other adapters consistently.
- Preserve longer local-model warm-up windows without masking permanent errors.

**Acceptance:** Fault-injection tests cover connection refusal, model warm-up, timeout, 429, 5xx, stream interruption, and fallback selection.

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

**Status:** Planned  
**Risk:** Medium  
**Depends on:** EVENT-01, STAB-04

- Run selected tests without `sync_threads`.
- Stress event FIFO, queue pressure, owner-scoped cancellation, shutdown, session-state snapshots, and file locks.
- Give each test a bounded timeout and deterministic synchronization primitives.

**Acceptance:** Repeated stress runs complete without deadlock, event reordering within a category, or post-shutdown logging loops.

#### QUAL-01 — Make quality checks actionable

**Status:** Planned  
**Risk:** Low  
**Depends on:** None

- Record the current Ruff/mypy baseline.
- Fix errors by subsystem rather than one repository-wide formatting commit.
- Remove `|| true` once each gate reaches zero.
- Declare the `patch` executable or replace that dependency with an in-process implementation.

**Acceptance:** CI fails on newly introduced lint/type errors and a clean environment can run file-edit tests from documented prerequisites.

#### TEST-02 — Triage current baseline failures

**Status:** Planned  
**Risk:** Low  
**Depends on:** QUAL-01

- Fix or declare the `patch` executable requirement.
- Make WebSocket backpressure tests synchronize on queue state instead of elapsed time.
- Root-cause the tool-result normalization failure.
- Eliminate the atexit logger write-to-closed-stream error.

**Acceptance:** The unit suite passes from a clean environment without timing retries.

## Recommended delivery sequence

1. Run **QUAL-01** and **TEST-02** so the baseline is trustworthy.
2. Define the production surface in **STAB-06**.
3. Implement **EVENT-01**, followed by **TEST-01**.
4. Implement **PERF-01** and **PERF-02** independently.
5. Use **PERF-03** evidence to scope optimization.
6. Start **STATE-01** only after delivery and concurrency contracts protect behavior.

Broad rewrites of the event system, state object, or orchestration modules before these contracts would increase risk without proving user-visible improvement.