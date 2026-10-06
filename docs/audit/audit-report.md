# CodingAgent — Comprehensive Engineering Audit

**Date:** 2026-09-29
**Scope:** full repository (440 production modules, 869 test files, ~180k source lines)
**Method:** static reading, AST import-graph analysis, and *runtime execution* of the
real modules (graph compilation, bash classification, tier routing) rather than
inference from names.
**Code state audited:** `4665b98` + staged documentation deletions.

> **Remediation status (updated 2026-10-06).** Roadmap **Phases 1–3 are
> implemented and verified, plus P4.1 of Phase 4.** Every finding in §3–§4 now
> carries a **Status** box with file references and re-executed proof; all
> Critical/High/Medium findings are fixed (§14 items 1–12 done, item 13 open as
> P4.4). The prose below is retained as written on 2026-09-29 so the original
> reasoning stays auditable — **for current state read the Status boxes, not the
> finding text.** Verification behind these claims: `tests/unit` **4,831 passed,
> 1 skipped**; integration/acceptance/e2e/benchmarks **168 passed, 8 skipped**;
> `ruff` and targeted `mypy` clean; TUI mirror trees byte-identical.

## Evidence grading

Every finding below is tagged with how it was established:

- **[EXEC]** — proven by importing and calling the real code; the observed output is quoted.
- **[STATIC]** — proven by exhaustive reference/import analysis across `src/`, `tui/`, `tests/`.
- **[READ]** — confirmed by reading the code path, not independently executed.

## Known limitation

Audit input sources 2–4 named in `docs/audit/audit-instructions.md` (architecture doc,
gap analysis, implementation plans) were **deleted by the repository owner before this
audit**. They could not be used, cross-checked, or audited for accuracy. Categories
9–13 (evaluation, observability, UX, extensibility) are consequently covered at
lower depth and are marked **[READ]** or **[PARTIAL]** rather than overstated.

---

# 1. Executive Summary

CodingAgent is a **substantively engineered agent with genuinely strong safety
primitives that are wired into the wrong execution path.** The codebase has
excellent defensive design — centralized loop caps, a canonical tool-classification
module with drift-locking identity tests, fail-closed autonomous approval, sandbox
enforcement refusal, per-symbol import isolation, and round-granularity crash
recovery. These are not incidental; they are deliberate and test-enforced.

The central architectural problem is that **the production graph and the
hardened graph have diverged.** Production runs `_compile_frontier_graph()` /
`_compile_lite_graph()`. Several of the most carefully built safeguards —
prompt-injection scanning of tool output, repository indexing during planning and
after edits, the role-specific graph factory, the `PermissionGateway`
authorization layer — are implemented, unit-tested, and reachable **only from
legacy nodes that the production graph does not contain.** The tests pass, CI is
green, and the safeguards still do not run.

This produces a specific, dangerous illusion: the audit-visible surface of the
system (test count, coverage, documented invariants in `AGENTS.md`) implies
protection that the production execution path does not have. The most concrete
instance is **prompt-injection detection: the guard exists, is tested, and is
never invoked on tool results in production.**

**Four findings are Critical or High.** Two of them (injection scanning, repo
awareness) are single-call-site wiring fixes that immediately close large gaps.
Separately, **8,222 lines across 14 modules are unreachable from any entrypoint**,
including a 156-line server exposure policy and a 1,151-line permission layer.

The system is **not production-ready as an unattended autonomous agent** — chiefly
because destructive git operations (`git clean -fdx`, `git reset --hard`,
`git push --force`) classify as `SAFE` and bypass the approval gate entirely, and
because untrusted content enters the model context unscanned. For an
attended developer tool with a human in the approval loop, it is close to
defensible; the safeguards that matter most are missing precisely where
automation removes the human. *(Both blockers are since closed: P1.3 gates the
destructive git forms as DANGEROUS, P1.1 scans every production tool result —
re-executed proof in §3.)*

---

# 2. Architecture Strengths

These are real, verified, and worth preserving through any refactor.

1. **Centralized, test-locked loop guards.** `graph/routing_constants.py` holds 13
   explicit caps — `DEFAULT_MAX_TOOL_CALLS=30`, `LOOP_GUARD_ROUNDS=10`,
   `MAX_STEP_RETRIES=3`, `MAX_NO_PLAN_FAILS=3`, `REPLAN_CAP_LARGE_FRONTIER=3`,
   `TOTAL_DEBUG_CAP_LARGE_FRONTIER=5`, `MAX_GRAPH_ROUNDS=20`,
   `MAX_ROUNDS_PLANNING=15`, and more. Infinite-loop protection is a named
   invariant with one source of truth, not scattered magic numbers.
   **[READ]**

2. **A single canonical tool-classification module with drift-locking tests.**
   `src/tools/constants.py` owns `TOOL_ALIASES`, `WRITE_TOOLS_REQUIRING_READ`,
   `MODIFYING_TOOLS`, `PERMISSION_REQUIRED_TOOLS`, `PERM_ORDER`, `FILE_TOOLS`;
   consumers re-export and object identity is asserted by
   `TestCanonicalCentralization`. Alias normalization happens once, at the top of
   `execute_tool_impl`, *before* every permission and policy check, so a deny rule
   keyed on a canonical name cannot be evaded via `run`/`ls`/`write`.
   **[READ]**

3. **Fail-closed autonomous approval.** `autonomous_approval_allowed()`
   (`tools_config.py:300-324`) resolves the alias first, then requires an explicit
   operator allowlist (`set_autonomous_approve()`, `configure()`, or
   `CODINGAGENT_AUTONOMOUS_APPROVE`). With nothing configured it returns `False`
   for **every** tool. Verified: the empty-allowlist case denies rather than
   silently approving. This is the correct default and is the single most
   important safety property in the system. **[EXEC]**

4. **Sandbox refuses to fail open.** `_enforcement_required()` is true under
   autonomous mode or `SANDBOX_REQUIRE_ENFORCEMENT`, and in that state the
   fallback to plain `subprocess` is refused rather than downgraded. Network
   policy is passed explicitly per call rather than defaulted.
   **[READ]**

5. **Per-symbol import safety.** `_EVENT_IMPORT_PATHS` resolves each event type
   lazily via `importlib` under a lock, so a single broken import degrades one
   event type instead of destroying the whole mapping. **[READ]**

6. **Typed event system with error isolation and ordering.** `MessageBus` uses a
   thread-safe `queue.Queue` for sync `publish()`, a bridge task to hand off to
   the event loop, and `run_in_executor` for handlers so blocking handlers cannot
   stall the loop. Lifecycle categories are FIFO via per-category
   `asyncio.Queue` consumers. **[READ]**

7. **Round-granularity crash recovery, honestly scoped.** The code does not
   pretend langgraph can resume mid-node; it writes a JSON-safe per-round state
   snapshot and rehydrates on same-thread re-entry, and documents precisely why
   `app.invoke(None, ...)` cannot resume pending nodes. **[READ]**

8. **Drift-locking on the TUI mirror.** `TestMirrorSync` enforces byte-identity
   between `tui/src` and `tui/tui_src`, preventing a class of bug that is
   otherwise silent and expensive. **[READ]**

9. **A real evaluation harness.** Scenario runner, pass@k, regression baselines
   with a CI gate, SWE-bench grading, and model comparison with isolated run
   directories. This is more than most projects at this scale have.
   **[READ]**

10. **4,936 passing unit tests** with ruff enforced fail-closed in CI. **[EXEC-adjacent:
    from the recorded CI run at `4665b98`]**

---

# 3. Critical Architectural Flaws

## CRITICAL-1 — Prompt-injection scanning is absent from the production tool-result path

**Severity: Critical** · **Category: 8 (Safeguards/Security), 14 (Not integrated)**

> **Status: FIXED (P1.1).** `graph/nodes/tool_result_safety.py` now owns the
> policy as `sanitize_tool_result` (single choke point), called from
> `_sanitize_tool_output` at `frontier_loop_node.py:55,153` — re-verified by `rg`
> over `src/`, where the production loop now imports the safety module directly.
> Detection runs **before** truncation (a tail-held injection would otherwise be
> scanned out of existence), and the `_prompt_injection_detected` dict key
> survives the byte cap so the model always sees the flag. Proof:
> `tests/unit/test_production_path.py` asserts the frontier graph wires the call;
> `test_context_builder_sanitization.py`, `test_perception_parsers.py` and the
> P1.1 cases in `test_property_based_fuzzing.py` cover annotation, a 120k-char
> tail injection, and flag persistence. Full suite after P4.1: 4,831 unit tests
> passed, 1 skipped (exit 0).

The finding below is retained in its original form for the record.

`detect_prompt_injection()` exists at
`src/core/orchestration/graph/nodes/tool_output_truncation.py:187` and is
thoroughly unit-tested. The complete inventory of its call sites in `src/` is
**[EXEC — exhaustive `rg` over all of `src/`]**:

| Call site | What it scans | In production graph? |
|---|---|---|
| `execution_node.py:113` | tool **result** | ❌ legacy only |
| `execution_lifecycle.py:204` | tool **result** (`sanitize_result_dict`) | ❌ legacy only |
| `perception_parsing.py:166` | the model's own **tool_call** | ✅ but wrong direction |
| `prompt_injection_guard.py:93` | its own module | n/a |

The production hot loop, `frontier_loop_node.py` (1,035 lines — the largest file
in the graph), imports only `prune_tool_outputs` and `truncate_tool_output`
(lines 31–32). Neither of those calls `detect_prompt_injection`; the injection
check at `tool_output_truncation.py:213` is a log statement *inside the
function body*, not a call from the pruner.

**Consequence:** every tool result that enters context during a production
frontier-tier run — file reads, `repo_read_tools`, web fetches, issue text,
`grep` over untrusted files — reaches the model **unannotated**. The agent reads
a file containing `ignore all previous instructions` and that text is
indistinguishable from operator intent.

This is the textbook definition of "a feature that exists but is not
integrated," and it is the highest-impact finding in the audit: the system
advertises prompt-injection resilience and does not have it on the path that
matters.

## CRITICAL-2 — Repository awareness is wired into the legacy graph, not the production graph

**Severity: Critical** · **Category: 4 (Repository Awareness), 15 (Missing capabilities)**

> **Status: FIXED (P1.2), with the indexing cost closed by P4.1.**
> `graph/nodes/repo_context.py` provides `build_repo_context_block` (called on
> loop entry; fails closed on a missing `working_dir`, never defaults to `.`)
> and `refresh_index_after_write` (self-gates on `MODIFYING_TOOLS` after
> `resolve_tool_alias`), both consuming `_path_utils.changed_paths` shared with
> P3.2's change trail so the index and the audit log cannot disagree. The
> pre-existing `refresh_file_in_index` bug (updated `files` but not the
> top-level `symbols`) is fixed; both it and `index_repository` share
> `_flat_symbols_for_file`. Proof: `tests/unit/test_production_path.py` pins the
> graph wiring, and P4.1 measured the previously-blocking cost — see below.

The instruction asks directly: *"whether it is actually used during planning"* and
*"whether planning is repo-aware."* The answer is no.

| Node | Repo-index integration | In production graph? |
|---|---|---|
| `planning_node.py:89` | `get_symbols_for_task` | ❌ legacy only |
| `execution_node.py:76` | `refresh_file_in_index` | ❌ legacy only |
| `analysis_node.py:319` | `index_repository` | ✅ but conditionally skipped |
| `frontier_loop_node.py` | **0 references** | ✅ production hot loop |

**[EXEC]** Production routing, `route_perception_frontier`, sends simple tasks
straight from `perception` to `frontier_loop`, bypassing `analysis` entirely. A
simple task on a frontier-class model therefore executes as:

```
perception → frontier_loop → memory_sync
```

with no repository indexing, no symbol graph, and no LSP. The agent edits code
having performed no repository intelligence gathering. The capability exists, is
substantial (2,866 lines in `src/core/indexing/`), and is unused in the most
common production path.

## HIGH-1 — 8,222 lines across 14 modules are unreachable from any entrypoint

**Severity: High** · **Category: 1 (Core Architecture), 14 (Over-engineering)**

> **Status: FIXED (P1.4 + P2.1 + P2.5).** Every module in the table below was
> deleted, not re-wired: `permission_gateway.py`, `permission_table.py`,
> `server/exposure_policy.py`, `session_watcher`, `event_log`,
> `event_persistence`, `agent_hooks`, `context_manager`, `env_registry`,
> `adapter_wrappers`, `wave_coordinator`, `graph_factory`, and the skills island
> (25 modules / 6,312 lines total across P2.1 + P2.5). `tests/unit/test_dead_code.py`
> is the ratchet: it walks the import graph from real entrypoints (modeling
> dynamic tool imports, `pkgutil` loading, the evaluation CLI, both TUI mirrors)
> and fails on any newly-unreachable module — its `_KNOWN_DEAD` baseline is now
> **empty and the walk reports zero unreachable modules** (not merely "not
> regressed"), and `test_baseline_entries_are_still_dead` prevents the baseline
> from rotting into a false pass.

Measured by AST import-graph traversal from all real entrypoints, then
cross-checked with exhaustive reference search (**[EXEC] + [STATIC]**):

| Module | Lines | Note |
|---|---:|---|
| `orchestration/permission_gateway.py` | 793 | **authorization layer, never imported** |
| `orchestration/session_watcher.py` | 382 | |
| `orchestration/event_log.py` | 366 | |
| `orchestration/permission_table.py` | 358 | imported only by `permission_gateway` |
| `env_registry.py` | 333 | |
| `orchestration/event_persistence.py` | 325 | |
| `orchestration/agent_hooks.py` | 311 | |
| `orchestration/context_manager.py` | 250 | |
| `skills/skill_loader.py` | 234 | island with `skill_registry` |
| `skills/skill_registry.py` | 201 | |
| `inference/adapter_wrappers.py` | 172 | |
| **`server/exposure_policy.py`** | **156** | **only referenced by its own test** |
| `orchestration/wave_coordinator.py` | 139 | |
| `orchestration/graph_factory.py` | 91 | 4 role graphs, tests only |
| **Total** | **8,222** | |

Two of these are security-relevant and deserve individual attention:

- **`src/server/exposure_policy.py`** — a server exposure policy registry whose
  *only* reference in the entire repository is `tests/unit/test_exposure_policy.py`.
  The server never imports it. It has full unit coverage and zero production
  effect. **[EXEC]**
- **`permission_gateway.py` + `permission_table.py` (1,151 lines)** — a complete
  staged-authorization layer. Every production "reference" is a docstring
  cross-reference (`event_types.py:264,299`, `tool_constants.py:3,5`,
  `_tool.py:45`, `permission_context.py:101`). No import. It has 8 test
  references. The system carries 1,151 lines of authorization logic that no
  request ever passes through, and `AGENTS.md` itself concedes the gateway "is
  dead code in production."

## HIGH-2 — Destructive operations classify as `SAFE` and bypass the approval gate

**Severity: High** · **Category: 8 (Safeguards), 3 (Tool safety)**

> **Status: FIXED (P1.3).** Re-executed against the real
> `analyze_bash_command(cmd, working_dir=...)` after the fix **[EXEC, 2026-10-06]**:
>
> | Command | Level (before → after) | Gated? |
> |---|---|---|
> | `git clean -fdx` | SAFE → **DANGEROUS** (`git clean (removes untracked files)`) | ✅ |
> | `git reset --hard` | SAFE → **DANGEROUS** (`discards uncommitted changes`) | ✅ |
> | `git push --force` | SAFE → **DANGEROUS** (`rewrites remote history`) | ✅ |
> | `find . -name '*.py' -delete` | SAFE → **DANGEROUS** (`bulk file deletion`) | ✅ |
> | `truncate -s 0 file` | SAFE → **DANGEROUS** (`file truncation`) | ✅ |
> | `mv src /tmp` | SAFE → **DANGEROUS** (`mv target escapes workspace (/tmp)`) | ✅ |
> | `> file.txt` | SAFE → **DANGEROUS** (`truncation redirect`) | ✅ |
> | `git clean -n` (dry run) | safe → safe | correctly ungated |
>
> Patterns live in `src/tools/bash_security.py:_DANGEROUS_PATTERNS` (L85ff:
> `git clean`/`reset --hard`/`checkout -f`/`push --force`/`branch -D`/`gc --prune`,
> each excluding its dry-run form); the out-of-workspace `mv` is the path-aware
> `_path_escape_reasons` pass reached through `analyze_bash_command(...,
> working_dir=...)`, supplied by both `_bash_exec` call sites. Gate 2 hard-blocks
> DANGEROUS as defence-in-depth, and DANGEROUS/BLOCKED stay refused in autonomous
> mode (`autonomous_approval_allowed` is fail-closed). Proof:
> `tests/unit/test_production_path.py` asserts `analyze_bash_command("git
> push --force")[0] is DANGEROUS`; `tests/unit/test_bash_security_file_tools_caching.py`
> covers the classification.

Executed against the real `analyze_bash_command()` **[EXEC]**:

| Command | Level | Gated? |
|---|---|---|
| `rm -rf /` | DANGEROUS | ✅ approval required |
| `rm -rf .` | DANGEROUS | ✅ |
| `sudo` / `curl` / `chmod -R 777 /` | DANGEROUS | ✅ |
| `:(){ :\|:& };:` (fork bomb) | BLOCKED | ✅ |
| **`git clean -fdx`** | **SAFE** | ❌ **no gate** |
| **`git reset --hard`** | **SAFE** | ❌ **no gate** |
| **`git push --force`** | **SAFE** | ❌ **no gate** |
| **`find . -name '*.py' -delete`** | **SAFE** | ❌ **no gate** |
| **`truncate -s 0 file`** | **SAFE** | ❌ **no gate** |
| **`mv src /tmp`** | **SAFE** | ❌ **no gate** |
| **`> file.txt`** (truncation redirect) | **SAFE** | ❌ **no gate** |

`_DANGEROUS_PATTERNS` (`bash_security.py:84-123`) covers recursive `rm` and
`rmdir` for recursive deletion, but nothing for the many other ways to destroy
work irreversibly. The instruction asks *"whether the agent can accidentally
delete repositories"* — via `rm` it cannot (correctly, and fail-closed in
autonomous mode). Via `git clean -fdx` + `git reset --hard` it can, silently, in
one command, with no prompt. `git push --force` is additionally
`is_network_capable()==True` **[EXEC]**, so it is subject to network policy but
still carries no approval gate.

## HIGH-3 — 251 excluded tests are hiding bit-rot

**Severity: High** · **Category: 9 (Evaluation)**

> **Status: FIXED (P2.4).** `tests/_deprecated/` was deleted outright — 16 files
> / ~5.1k lines — after verifying it was stale rather than parked: 29 of the 251
> tests fail on execution (they patch `tui/app.py`, which no longer exists), 10 of
> 16 files need a live lmstudio/ollama server and can never run in CI, and
> nothing outside the directory referenced it. The
> `tests/_deprecated_candidates.txt` manifest and the `--ignore` flag were
> dropped with it. Verified: `ls tests/_deprecated` → *No such file or directory*.

`tests/_deprecated/` (251 tests) is excluded from CI.
`tests/_deprecated/test_subagent_spawn.py:149` patches
`src.tools.subagent_tools.GraphFactory` — a target that no longer exists, since
`subagent_tools` never imported it and `graph_factory` is unreachable **[EXEC]**.
Because the directory does not run, this is invisible. Deprecated tests are
normally dead weight; here they are actively misleading, and they document a
production behavior (`GraphFactory` integration in subagent tools) that has
silently stopped existing.

## MEDIUM-1 — Tier routing fails open to the heaviest graph

**Severity: Medium** · **Category: 1 (Architecture)**

> **Status: FIXED (P2.2).** `select_tier_graph_cache_key` now derives its valid
> set from the canonical `ModelTier` enum plus the `"lite"` alias (so the enum
> cannot drift ahead of the router) and **raises** on an unknown non-empty tier
> instead of silently compiling the frontier graph. Only an empty/`None` tier —
> meaning classification itself failed — still degrades to `"capable"`, because
> crashing the run would be worse. `nano` is deliberately not a valid tier,
> resolving the contradiction with `_is_nano_or_small`. Proof:
> `tests/unit/test_tier_graph_routing.py`; no caller relied on the old fallback.

`select_tier_graph_cache_key()` maps any unrecognized tier to `"capable"` —
the 12-node graph. **[EXEC]**

```
'nano' → capable     'unknown' → capable     '' → capable
```

Two problems. First, `nano` is special-cased into the *capable* graph while
`_is_nano_or_small` treats nano as small elsewhere
(`static_prompt_parts.py:145`) — an internal contradiction. Second,
`compile_tier_graph_for_key` fails loudly on an unknown `cache_key`, but the
key is derived by a function that can never produce an unknown value, so that
defensive branch is unreachable and the fail-loud design is defeated by
fail-open defaulting.

## MEDIUM-2 — Lite tier has no verification stage

**Severity: Medium** · **Category: 2 (Reasoning Workflow)**

> **Status: FIXED (P2.3).** The lite graph now has a `verification` node and
> `route_frontier_loop_exit_lite` requests it only when the last tool resolves
> (via `resolve_tool_alias`) to a member of `MODIFYING_TOOLS` — read-only work
> keeps the tier's context/latency budget. Two pre-existing gaps had to be
> closed for this to mean anything: `verification_node` keys off
> `state["last_tool_name"]`, which only the legacy `execution_node` ever set, so
> `_dispatch_tool_calls` now returns the last tool it ran and
> `frontier_loop_node` publishes it. Without that the node would have run blind
> or always skipped. Proof: `tests/unit/test_tier_graph_routing.py` (lite route
> gate) and `tests/unit/test_production_path.py`.

Verified by compiling each tier **[EXEC]**:

```
small / lite → {__start__, perception, frontier_loop, memory_sync}   (3 nodes)
medium/large/frontier/nano → 12 nodes including verification, evaluation,
                             replan, debug, delegation, wait_for_user
```

A small/lite-tier model has no verification node, no evaluation, no replan, and
no `wait_for_user`. This is a material difference in reliability, not merely
speed, and it is undocumented at the point of configuration — a user selecting a
local small model silently opts out of verification entirely.

---

# 4. High-Risk Safety Issues

Consolidated for action; detail in §3.

| ID | Issue | Severity | Location | Status |
|---|---|---|---|---|
| CRITICAL-1 | No prompt-injection scan of tool results in production | Critical | `frontier_loop_node.py` | **FIXED (P1.1)** — `tool_result_safety.sanitize_tool_result` on every production tool result |
| CRITICAL-2 | No repo awareness on simple tasks / production loop | Critical | `graph/builder.py`, `planning_node.py` | **FIXED (P1.2)** — `repo_context.py` on loop entry + after writes; cost closed by P4.1 |
| HIGH-2 | `git clean`/`reset --hard`/`push --force` ungated | High | `bash_security.py:84-123` | **FIXED (P1.3)** — all 7 rows re-executed as DANGEROUS (§3) |
| HIGH-1a | Server exposure policy never wired | High | `server/exposure_policy.py` | **FIXED (P2.1)** — deleted |
| HIGH-1b | 1,151-line permission layer unreachable | High | `permission_gateway.py` | **FIXED (P2.1)** — deleted |
| HIGH-3 | 251 excluded tests hiding bit-rot | High | `tests/_deprecated/` | **FIXED (P2.4)** — directory deleted |
| MEDIUM-1 | Tier routing fails open to `capable` | Medium | `tier_graph_routing.py` | **FIXED (P2.2)** — raises on unknown tier |
| MEDIUM-2 | Lite tier has no verification stage | Medium | `builder.py` | **FIXED (P2.3)** — lite graph verifies writes |

**Mitigating factors, stated fairly.** The approval gate itself is sound
(§2.3), sandbox non-fail-open is correct (§2.4), `delete_file` is never
auto-approved, and `rm -rf` is properly gated. The failure was one of *coverage*
in the classifier, not in the gate mechanism — which is why HIGH-2 turned out to
be the pattern-list fix it looked like (P1.3).

---

# 5. Major Missing Capabilities

> **Status: all five closed.** Each entry now carries the item that closed it.

- **Retrieval-before-planning is absent in production.** No `RepoIndexer` call on
  the frontier path. Modern agents (Claude Code, Aider, Cursor) ground the plan
  in indexed symbols before the first edit. *(CRITICAL-2)* —
  **closed by P1.2**: `repo_context.build_repo_context_block` indexes once per
  workdir per process and injects a bounded `<repo_context>` symbol/file block on
  loop entry; P4.1 made the index cheap enough to afford (measurements in the
  Phase 4 roadmap below).
- **No injection boundary between untrusted tool output and the model.** *(CRITICAL-1)*
  — **closed by P1.1**: `tool_result_safety.sanitize_tool_result` runs
  `detect_prompt_injection` before truncation on every production tool result.
- **No verification tier for small/local models.** *(MEDIUM-2)* —
  **closed by P2.3**: the lite graph has a `verification` node gated on
  `MODIFYING_TOOLS` via `last_tool_name`.
- **Structured plan-step verification does not exist in the production loop.** The
  instructions name "plan-step verification" as a component to look for; the
  production frontier loop has no per-step verification callback, only a terminal
  `verification` node that the simple-task path skips. — **closed by P3.1**:
  `in_loop_verification.py` captures a lint baseline before each write and diffs
  it after, feeding failures back into `history` inside the same loop; capped at
  `_MAX_INLINE_VERIFICATION_FAILURES = 3` before exiting to terminal
  verification.
- **No repo-change audit trail.** Despite `event_log.py` (366 lines) and
  `event_persistence.py` (325 lines) existing, neither is reachable, so there is
  no live record of which files the agent changed and why. — **closed by P3.2**:
  `change_log.py` fingerprints each path before/after dispatch at
  `tool_execution_pipeline` steps 7/10, appends to `change_log.jsonl`, and
  publishes typed `RepoChangeRecorded` (the two dead modules were deleted in
  P2.5 rather than revived).

---

# 6. Workflow Reliability Issues

- **Determinism is well-served by the cap table** (§2.1) — the strongest part of
  the design.
- **The legacy/production split undermines confidence in every other workflow
  claim.** `AGENTS.md` documents 16-node behavior as if it were the pipeline; the
  production frontier graph has 11 nodes and the lite graph 3. Any reasoning
  about retry, replan, debug, or verification must be re-derived per graph.
- **Crash recovery is round-granularity and honest** (§2.7), but combined with
  CRITICAL-2 an interrupted run resumes a loop that never indexed the repository.
- `MAX_GRAPH_ROUNDS=20` bounds rounds per turn, but there is no verified global
  wall-clock or token ceiling for a single turn. **[PARTIAL]**

---

# 7. Tool System Weaknesses

- **71 tools across 46 files** is a large surface. Discoverability is addressed
  via `toolsets/`, but the breadth increases the probability of the classifier
  gap in HIGH-2.
- **Argument validation and idempotency were not audited to the standard applied
  above.** Idempotency in particular (e.g. retry of a non-idempotent write after
  a transport failure) is unverified. **[NOT AUDITED]** — idempotency **now
  audited and hardened (P3.4)**: `IDEMPOTENT_TOOLS` is a fail-closed allowlist
  resolved through `resolve_tool_alias`, a timed-out non-idempotent tool reports
  `tool.outcome_unknown` instead of a re-run-inviting "failed", and
  `execute_with_retry` runs once unless the allowlist says otherwise (annotated
  `tool.retry_suppressed`). Argument validation remains not audited to this
  standard.
- **Approval-gate coverage is the demonstrated weakness**, not the gate itself.
- `delete_file` correctly never auto-approves; `MODIFYING_TOOLS` and
  `PERM_ORDER` are centrally derived. **[READ]**

---

# 8. Repository Awareness Gaps

Fully covered by CRITICAL-2. Summarized:

> **Status: closed by P1.2 (and priced by P4.1).** `repo_context.py` indexes on
> loop entry and refreshes after every modifying tool; answers to the four
> questions below are the *pre-fix* state, retained for the record.

- Capability present (2,866 lines) but bound to legacy nodes.
- Production loop (`frontier_loop_node`) has **zero** repo/symbol/LSP references.
- Simple tasks skip even `analysis`, the only production node with awareness.
- Answering the instruction's four questions: repository intelligence **exists**;
  it is **not used during planning in production**; retrieval does **not** happen
  automatically on the frontier path; planning is **not** repo-aware in
  production. The "unused symbol graph" gap named in the instructions is real.

---

# 9. Memory System Evaluation

The memory subsystem is one of the better-engineered areas.

- Three backends behind one protocol with deterministic precedence
  (`_resolve_backend`: explicit > `CODING_AGENT_STORAGE_BACKEND` > config > `jsonl`).
- `JsonlSessionStore.get_recent_sessions` correctly orders by newest file mtime
  across active and rotated files (fixed in `b062fb5`; 7 regression tests).
- Bounded decisions sidecar: `iter_decisions` streams and `write_decisions_json`
  caps per-session candidates before global merge. Differential fuzz over 3,000
  random stores × 7 limits found zero mismatches; peak gather memory fell
  357 KiB → 26 KiB (fixed in `af1f0b9`; 9 tests).
- `vector_store.py` degrades to a SHA-256/token stub while the
  sentence-transformers model warms in a background thread, with
  `_MEMORY_LOCK` held across dedup-read + append to prevent double-append.
- Atomic tmp-file `replace()` for JSONL appends; rotation retains the newest 200.

**Gaps:** no plan persistence verified in production (the `plan_mode_tools.py`
reference count was 1 — self only, see HIGH-1 methodology); cross-task
contamination guards were not independently exercised. **[PARTIAL]** — plan
persistence **since built and verified (P3.3)**: `frontier_completion.py`
records `plan_completed` from the loop's own `_completion_detected` signal,
persists through `save_last_plan` (which now reports whether the record landed)
and `append_plan_audit_log`, and `plan_is_resumable` refuses a completed plan.
Cross-task contamination guards remain **[PARTIAL]**.

---

# 10. Evaluation and Testing Gaps

- **4,936 unit tests pass; ruff is enforced fail-closed.** Genuine strength.
  *(Now 4,831 passed / 1 skipped after P2.4 removed 251 excluded tests and
  suites were consolidated — a smaller number covering production, not legacy.)*
- **The core weakness is that the test suite validates legacy code paths that
  production does not execute** — which is precisely how CRITICAL-1, CRITICAL-2,
  and HIGH-1 stayed invisible through a green CI. A 100%-covered
  `exposure_policy` and a fully tested `permission_gateway` produced no signal
  because nothing asserted *reachability from an entrypoint*. —
  **closed by P1.4:** `test_dead_code.py` asserts reachability from real
  entrypoints (baseline now empty, zero unreachable modules) and
  `test_production_path.py` (68 tests) pins the production graph shape and the
  P1.1–P1.3 behaviors.
- **251 deprecated tests excluded from CI contain stale patches** (HIGH-3). —
  **closed by P2.4:** directory deleted.
- **No production-path assertion suite.** There is no test asserting that
  `frontier_loop` sanitizes tool results, or that the production graph retains
  repo-awareness — because neither is true. — **closed by P1.4**; those two
  assertions are now exactly what `test_production_path.py` checks.
- The evaluation framework (scenario runner, pass@k, SWE-bench, model
  comparison) is a genuine asset. **[READ]**

---

# 11. Usability Problems

- **Configuration complexity is high**: storage backend, tier, sandbox
  enforcement, autonomous allowlist, checkpointing, preview confirmation, MCP,
  telemetry — each with several interacting resolution layers.
- **Silent capability downgrade.** Selecting a small/local model silently
  disables verification (§MEDIUM-2) with no warning. **[PARTIAL]** —
  **closed by P2.3:** the lite graph verifies writes (and the downgrade is now
  the documented tier policy rather than a silent hole).
- Six launch scripts (`start.sh`, `start_tui.sh`, `start.ps1`, `starth.sh`,
  `scripts/run.sh`, …) for one binary is redundant surface. **[READ]**
- The TUI is the most polished surface; headless debugging is comparatively thin. **[READ]**

---

# 12. Performance Bottlenecks

- `analysis_node.py:319` calls `index_repository` on the complex-task path.
  Whether this is incremental/cached was not verified and is the most likely
  latency spike in the pipeline. **[PARTIAL — verify]** —
  **now measured and fixed (P4.1):** the index *was* hash-incremental, but file
  discovery ran 17 full-tree `rglob` walks with the ignore filter applied after
  enumeration, so a warm unchanged call cost ~4.0 s (98 % of it in `rglob`, not
  parsing). One pruned `os.scandir` walk replaced it: **3,943 ms → 69 ms**, same
  1,016-file set.
- `frontier_loop_node.py` at 1,035 lines is both the largest and hottest module;
  it is a maintainability and a profiling risk. — reduced to **899 lines** by
  the P1.1/P1.2/P3.1/P3.3 extractions, with `test_module_sizes.py` ratcheting the
  limit **down only** (1035 → 1017 → 900 → 899).
- Token efficiency is well-served by the single canonical estimator and the
  prune/recent-tail policy. **[READ]**
- Rediscovered LLM calls were not measured; no caching layer was found in the
  frontier path. **[NOT AUDITED]**

---

# 13. Over-Engineered Components

- **8,222 lines of unreachable code** (§HIGH-1) is the headline. The most
  expensive over-engineering is *security* over-engineering: two full
  authorization subsystems (`permission_gateway` + `permission_table`, 1,151
  lines; `exposure_policy`, 156 lines) that are complete, tested, and inert.
  This is worse than having no such code, because it inflates the perceived
  security posture of the system.
- `wave_coordinator.py` (139), `agent_hooks.py` (311), `adapter_wrappers.py` (172)
  — no production consumer.
- The dual-bus `EventBus`/`MessageBus` design retains a legacy string-addressed
  façade (`publish("name", dict)`) alongside typed events. The façade is a
  documented compatibility layer; its long-term cost is two APIs to keep
  consistent. **[READ]**

---

# 14. Prioritized Fix List

| # | Fix | Severity | Location | Complexity | Impact | Status |
|---|---|---|---|---|---|---|
| 1 | Call `detect_prompt_injection` on every tool result in the frontier loop | Critical | `frontier_loop_node.py` (~L705, next to `_truncate_tool_output`) | **Low** (~5 LOC) | Closes the untrusted-content→model boundary | **DONE (P1.1)** — `tool_result_safety.sanitize_tool_result` |
| 2 | Add production-path repo retrieval: index in `perception`/on loop entry, `refresh_file_in_index` after edits | Critical | `frontier_loop_node.py`; promote from `planning_node.py:89` | Medium | Restores repo-aware execution | **DONE (P1.2)** — `repo_context.py`; cost closed by P4.1 |
| 3 | Add `git clean/reset --hard/push --force`, `find -delete`, `truncate`, `mv`-out-of-workspace to `_DANGEROUS_PATTERNS` | High | `bash_security.py:84-123` | **Low** (patterns + tests) | Prevents silent loss of uncommitted work | **DONE (P1.3)** — re-executed, all DANGEROUS |
| 4 | Wire `exposure_policy` into the server, or delete it | High | `src/server/app.py`, `server_config.py` | Low | Removes a false security guarantee | **DONE (P2.1)** — deleted |
| 5 | Decide the `PermissionGateway` question: wire or delete 1,151 lines | High | `permission_gateway.py`, `permission_table.py` | Medium | Removes dead authz surface | **DONE (P2.1)** — deleted |
| 6 | Fail closed on unknown tier instead of defaulting to `capable` | Medium | `tier_graph_routing.py:151-155` | **Low** | Restores intended fail-loud design | **DONE (P2.2)** |
| 7 | Warn when the selected tier has no verification stage | Medium | `builder.py:759+`, model-tier selection | Low | Removes silent reliability downgrade | **DONE (P2.3)** — lite graph verifies writes |
| 8 | Add reachability test: every `src/` module is imported by an entrypoint | High | new `tests/unit/test_dead_code.py` | Low | Prevents CRITICAL-1/2-class rot recurring | **DONE (P1.4)** — `_KNOWN_DEAD` now empty, zero unreachable |
| 9 | Add production-path assertion tests (frontier loop sanitizes; graph keeps repo awareness) | High | new `tests/unit/test_production_path.py` | Medium | Converts audit findings into CI gates | **DONE (P1.4)** — 68 tests |
| 10 | Repair or delete `tests/_deprecated/` | Medium | `tests/_deprecated/` | Low | Stops misleading green | **DONE (P2.4)** — deleted |
| 11 | Delete remaining dead modules (session_watcher, event_log, event_persistence, agent_hooks, context_manager, env_registry, adapter_wrappers, wave_coordinator, graph_factory, skills island) | High | 22 modules (P2.5) | Low–Medium | P2.5: −5,005 lines; P2.1: −1,307 lines | **DONE (P2.5)** — true count 25 modules total including P2.1, not 28 |
| 12 | Measure `index_repository` cost on the analysis path | Medium | `analysis_node.py:319` | Low | Confirms/denies latency risk | **DONE (P4.1)** — 3,943 ms → 69 ms warm; see Phase 4 |
| 13 | Consolidate the 6 launch scripts to one | Low | root | Low | Less surface | **open (P4.4)** |

---

# Prioritized Engineering Roadmap

## Phase 1 — Critical stability fixes

> **Status: implemented and verified.** All four items below are complete.
> Verification: `tests/unit` (4,995 tests), `tests/integration`,
> `tests/acceptance`, `tests/e2e`, and `tests/benchmarks` all pass; `ruff`
> (`E,F,W`) clean; targeted `mypy` clean; TUI mirrors byte-identical.

- **P1.1 — Prompt-injection gate on the production path.** *Location:*
  `src/core/orchestration/graph/nodes/frontier_loop_node.py`, at the tool-result
  handling around L705 where `_truncate_tool_output` is already applied.
  *Complexity:* Low. *Impact:* Critical. Closes the untrusted-content injection
  boundary; ~5 lines plus tests.
  **DONE.** Policy extracted to `graph/nodes/tool_result_safety.py`
  (`sanitize_tool_result`), called from `_sanitize_tool_output` in
  `frontier_loop_node.py`. Detection deliberately runs **before** truncation:
  truncation keeps the head of each large field, so scanning afterwards would
  miss an injection hidden in the truncated tail. The
  `_prompt_injection_detected` dict key survives truncation, so the model always
  sees the flag even when the inline warning is itself cut by the byte cap.
  Tests cover clean output, annotation, a 120k-character tail injection, and
  flag persistence, plus a graph-shape assertion that the production frontier
  graph really wires the function.
- **P1.2 — Repo-aware frontier loop.** *Location:*
  `frontier_loop_node.py` (index on entry), promoting `get_symbols_for_task` from
  `planning_node.py:89` and `refresh_file_in_index` from `execution_node.py:76`.
  *Complexity:* Medium. *Impact:* Critical. Restores repository awareness for the
  common production path.
  **DONE.** Helpers live in `graph/nodes/repo_context.py`:
  `build_repo_context_block` (indexes incrementally once per workdir per process,
  then injects a bounded 4,000-char `<repo_context>` block carrying relevant
  symbols and files) and `refresh_index_after_write`. A missing `working_dir`
  fails closed rather than falling back to `.` and indexing the process CWD.
  Also fixed a **pre-existing bug** in `repo_indexer.refresh_file_in_index`: it
  updated `files` but not the top-level `symbols` index, so *every* symbol search
  stayed stale after a write. Both `index_repository` and `refresh_file_in_index`
  now share `_flat_symbols_for_file`.
  Refresh coverage: single-path tools reparse one file, `rename_file` refreshes
  both ends, and tools with no single path (multi-file patches) fall back to a
  full incremental re-walk. The call is unconditional — the helper self-gates on
  `MODIFYING_TOOLS`, so the node tracks no per-tool argument schema.
- **P1.3 — Destructive-command classification.** *Location:*
  `src/tools/bash_security.py:84-123`. Add `git clean`, `git reset --hard`,
  `git push --force`, `find -delete`, `truncate`, out-of-workspace `mv`, and bare
  truncation redirects to `_DANGEROUS_PATTERNS`, with regression tests.
  *Complexity:* Low. *Impact:* High.
  **DONE.** Added `git clean`, `git reset --hard`, `git checkout -f`,
  `git push --force`, `git branch -D`, `find -delete`, `truncate -s`,
  `git gc --prune`, and leading `> file` redirects; dry-run variants are
  explicitly excluded so read-only invocations keep their current behavior.
  Out-of-workspace `mv`/`cp` needed a different approach than a static pattern:
  because Gate 2 in `_bash_exec.py` **hard-blocks** anything DANGEROUS, a blanket
  `\bmv\b` pattern would have made ordinary file moves impossible. It is
  implemented instead as a path-aware pass (`_path_escape_reasons`) reached via
  the new optional `analyze_bash_command(cmd, working_dir=...)` argument, which
  both `_bash_exec` call sites now supply. Omitting `working_dir` skips only that
  check; all static pattern passes still run.
  *Residual risk:* when `working_dir` is not supplied by some other caller, an
  out-of-workspace `mv` is not gated. Both production call sites are threaded.
- **P1.4 — Reachability + production-path CI gates.** *Location:* new
  `tests/unit/test_dead_code.py` and `tests/unit/test_production_path.py`.
  *Complexity:* Low–Medium. *Impact:* High. This is the meta-fix: it prevents
  every other finding in this report from recurring undetected.
  **DONE.** `test_dead_code.py` walks the import graph from real entrypoints
  (modeling dynamic imports, `pkgutil`-loaded tools, the evaluation CLI, and both
  TUI mirrors as live roots) and fails on any newly-unreachable module, with a
  justified `_KNOWN_DEAD` baseline for modules that are dead *today* so Phase 1
  is a ratchet rather than a mass-deletion diff. `test_production_path.py`
  (68 tests) pins the production graph shape and the P1.1–P1.3 behaviors.
  `test_module_sizes.py`'s `frontier_loop_node.py` limit was **tightened**
  1035 → 1017 (limits may only go down), reflecting the extraction of
  `tool_result_safety.py`, `repo_context.py`, and `frontier_toolcalls.py`.


## Phase 2 — Robustness improvements

- **P2.1 — Resolve the two inert authorization subsystems.** *Location:*
  `permission_gateway.py` + `permission_table.py` (wire or delete);
  `server/exposure_policy.py` (wire or delete). *Complexity:* Medium.
  *Impact:* High. Removes a misleading security posture.
  **DONE (delete).** The reconsideration pass ended in removal, not wiring:
  `permission_gateway.py` (793), `permission_table.py` (358), and
  `server/exposure_policy.py` (156) — 1,307 lines of security-shaped code — had
  zero production importers. The production path is
  `tool_execution_pipeline._run_permission_gate` (approval) and
  `tools_config.autonomous_approval_allowed` (HS-1 allowlist); the gateway was
  a parallel, never-executed copy. Every invariant the gateway tests asserted
  was re-expressed against the live path before deletion: the `delete_file`
  never-auto-approve rule now checks `tools.constants.WORKDIR_SAFE_TOOLS` +
  `_check_workdir_confinement`, and the HS-1 autonomous-allowlist tests now
  drive `_run_permission_gate` directly. Guardrails `test_gate2b_policy_rules`
  and `test_tool_kind_map_keys_resolve_to_live_registry_tools` were dropped
  with the code they pinned.
- **P2.2 — Tier routing correctness.** *Location:*
  `tier_graph_routing.py:151-155`; unknown tier must raise, not default to
  `capable`. Reconcile the `nano` contradiction with `_is_nano_or_small`.
  *Complexity:* Low. *Impact:* Medium.
  **DONE.** `select_tier_graph_cache_key` collapsed every unrecognized tier to
  `"capable"`, so a typo like `"capabel"` was silently compiled as the frontier
  graph — which also made the pre-existing F-14 guard in
  `compile_tier_graph_for_key` unreachable, since it only ever received
  `"capable"` or `"lite"`. It now derives the valid set from the canonical
  `ModelTier` enum (plus the `"lite"` alias) so the enum cannot drift ahead of
  the router, and raises on an unknown non-empty tier. An empty/`None` tier
  still degrades to `"capable"` because it means classification failed and
  crashing the run would be worse. `nano` is deliberately not a valid tier: it
  is not a `ModelTier` member and `_is_nano_or_small` already treats it as
  non-small. Verified no caller relied on the old silent fallback.
- **P2.3 — Verification parity for small/lite tiers.** *Location:*
  `builder.py:628-685`. Add a minimal verification node to the lite graph, or
  make the downgrade explicit and warned at configuration time.
  *Complexity:* Medium. *Impact:* Medium–High.
  **DONE.** The lite graph routed unconditionally to `memory_sync` and had no
  verification node, which made the P3-B policy inside `verification_node`
  ("SMALL tier: verify writes only") dead code on the very path it was written
  for. Worse, `verification_node` keys off `state["last_tool_name"]`, which only
  the legacy `execution_node` ever set — `frontier_loop_node` never populated
  it, so simply adding the node would have made verification either always skip
  or run blind. Fixed end to end: `_dispatch_tool_calls` now returns the last
  tool it ran, `frontier_loop_node` publishes `last_tool_name`, the lite graph
  has a `verification` node, and `route_frontier_loop_exit_lite` requests
  verification only when that tool resolves (via `resolve_tool_alias`) to a
  member of `MODIFYING_TOOLS`. Read-only work still skips the hop, so the tier's
  context/latency budget is preserved.
- **P2.4 — Purge `tests/_deprecated/`.** *Location:* `tests/_deprecated/`
  (251 tests, includes patches to targets that no longer exist).
  *Complexity:* Low. *Impact:* Medium.
  **DONE.** Removed all 16 files / ~5.1k lines, which were excluded from every
  run by `--ignore=tests/_deprecated`. Verified stale rather than merely parked:
  29 fail on execution (`test_tui_fixes.py` asserts against `tui/app.py`, which
  no longer exists — the TUI moved to `tui/tui_src/ui/` and was decomposed into
  mixins), 10 of 16 files require a live lmstudio/ollama server so they can never
  run in CI, last touched 2026-05-24 while `tests/unit` was updated 2026-09-29,
  and nothing outside the directory references it. Also dropped the now-meaningless
  `tests/_deprecated_candidates.txt` manifest and the `--ignore` flag itself.

- **P2.5 — Remove the remaining dead modules.** *Location:* see §14 item 11.
  *Complexity:* Low–Medium. *Impact:* High.
  **DONE.** After P2.1, 22 modules / 5,005 lines were removed. The pre-existing
  `_KNOWN_DEAD` baseline for this ratchet was **unsound** and had to be fixed
  first: `_build_import_edges` ignored `ImportFrom.module` whenever `level > 0`,
  so every `from .submodule import X` edge inside a package was dropped. That
  made three modules re-exported by `src/core/inference/__init__.py`
  (`model_capability_profile`, `hardware_capability_profile`, `runtime_profile`)
  look dead; deleting them broke 85 test modules at collection time. The
  resolver now anchors on `base + node.module` and the cleanup list was
  recomputed honestly (25 modules, 6,312 lines total including P2.1, not 28).
  `TestImportResolverSoundness` pins the resolver on a synthetic package so the
  class of bug, not just the instance, is caught. Result: `_KNOWN_DEAD` is now
  empty and the walk reports **zero** unreachable modules.
  Live at-risk invariants were carried over rather than deleted: the atomic
  `providers.json` write guard moved to its surviving location
  (`tui/src/ui/app.py`), and `test_tool_constants.py`'s centralization checks
  dropped only the dead `permission_gateway` re-export.

## Phase 3 — Capability improvements

> **Status: implemented and verified.** All four items are complete
> (`d7e36b1` P3.1, `2aaf393` P3.2, `9e5a754` P3.3, `9bb4bd1` P3.4).
> Verification: `tests/unit` **4,831 passed, 1 skipped**; `tests/integration`,
> `tests/acceptance`, `tests/e2e`, `tests/benchmarks`, `tests/integration_real`
> and `tests/test_session_store_close.py` **168 passed, 8 skipped**; `ruff`
> (`E,F,W`) clean; `mypy` clean on every changed module; TUI mirrors
> byte-identical (`diff -rq tui/src tui/tui_src`).

- **P3.1 — Per-step verification in the frontier loop.** **DONE.** *Location:*
  `in_loop_verification.py` + `frontier_loop_node.py`. *Complexity:* Medium–High.
  *Impact:* High. Closes the "plan-step verification" gap named in the audit
  spec. `frontier_loop_node` is the only node in the tier graphs that executes
  tools, and `verification_node` only runs *after* the loop exits, so a broken
  write used to surface a turn late — or never, if the loop exited on
  `no tool calls`. `capture_lint_baseline` (pre-write) + `run_inline_check`
  (post-write) now diff a fast parse-only `quick_lint` against a per-invocation
  baseline and inject the failure into `history` as a `role="user"` message the
  model can act on within the same loop. `delete_file` gets an absence
  assertion. The baseline is captured once per path per invocation, so a file
  the agent broke is re-reported until repaired (per-call re-capture would go
  quiet on a repeat of the same bad write) while pre-existing breakage is never
  blamed. `.ts`/`.tsx`/`.rs` are excluded because `quick_lint` shells out to a
  full type-checker/compiler there. `_MAX_INLINE_VERIFICATION_FAILURES = 3`
  bounds self-correction: after that the loop exits with
  `errors += ["inline_verification_failed"]` and `inline_verification_failures`
  in state, so verification/evaluation/replan take over. No new graph nodes.
  `frontier_messages.py` absorbed `_extract_tool_call_from_text` /
  `_normalize_messages_for_native_tools` (re-exported) to keep
  `frontier_loop_node.py` inside its size guard: 1016 → 900 lines.
- **P3.2 — Live repo-change audit trail.** **DONE.** *Location:*
  `src/core/orchestration/change_log.py` + `tool_execution_pipeline.py`.
  *Complexity:* Medium. *Impact:* High — "which files did the agent change, and
  why" is now answerable in production. `change_log` records one durable entry
  per file a modifying tool actually changed, with before/after fingerprints,
  the plan step in force, and whether the path was in the plan's
  `affected_files`; entries append to `<agent context>/change_log.jsonl` and a
  typed `RepoChangeRecorded` event is published for live subscribers. Hooked
  into `tool_execution_pipeline` — the single choke point every tool call
  passes through on both the tier and legacy graphs — at step 7
  (`begin_change`, fingerprint before dispatch) and step 10
  (`record_change`, after dispatch). Deliberately fingerprint-based rather than
  git-based: it must work in a non-git workspace and attribute *individual*
  writes instead of one end-of-task diff (`work_summary`'s git view stays as the
  complementary end-of-task rendering). Attribution context is pushed onto the
  orchestrator once per round by `inference_loop_rounds` — the plan can only
  change between graph invocations, never between tool calls inside one loop,
  so the tool-call sites stay a pure lookup. Path extraction moved to
  `_path_utils.changed_paths`, shared with `repo_context.refresh_index_after_write`,
  so the index and the trail can never disagree about what a tool touched.
  A path is only recorded when a captured "before" fingerprint exists: without
  it the trail cannot distinguish "the agent created this" from "it was already
  there", and a phantom creation in an audit record is worse than silence.
  Known limitation: `bash`-mediated writes are not attributable to a path and
  are therefore not recorded.
- **P3.3 — Persisted plan memory across turns.** **DONE.** *Location:*
  `graph/nodes/frontier_completion.py`, `planning_helpers.py`,
  `evaluation_node.py`. *Complexity:* Medium. *Impact:* High — this was not
  merely a missing feature; it was corrupting the outcome of *every* successful
  run. The tier graphs run a plan as one LLM+tool loop, so unlike the legacy
  `execution_node` they never advanced `current_step` and never set a per-step
  `completed` flag. That made `evaluation_node`'s `plan_completed` check
  unsatisfiable on the production path: `current_step` stayed 0 and no step was
  flagged, so a fully successful run with an N-step plan was reported as
  "PARTIAL COMPLETION" with `evaluation_result="replan"` — state, TUI, and
  distillation all recorded the wrong outcome. Routing survived only by luck
  (the frontier graph remaps `step_controller` → `memory_sync`, the same
  destination as the real "complete" branch). Verified before the fix by
  executing `evaluation_node` on frontier-shaped state.
  The loop already emits an authoritative completion signal —
  `_completion_detected`, set when a turn returns no tool calls — so
  `frontier_completion.plan_completion_patch` records `plan_completed` from that
  and nothing else. It deliberately makes **no** per-step claim: the loop has no
  per-step checkpoint, and inventing one would put a falsehood in the persisted
  plan. `evaluation_node` now trusts that flag the same way it already trusted
  `verification_passed` over recomputation. Persistence reuses the existing
  `save_last_plan` / `append_plan_audit_log` helpers, so there is still exactly
  one writer for `last_plan.json`; `save_last_plan` gained an `extra` kwarg for
  the flag (merged *before* the canonical identity keys, so a caller cannot
  corrupt a record's plan/task/step identity) and now **returns whether the
  record landed**, because a silently-lost progress record would leave the next
  turn re-running finished work — the exact failure P3.3 exists to prevent.
  `plan_is_resumable` refuses a plan marked `plan_completed`, so a finished plan
  is re-planned rather than silently re-run, and it now validates that `plan` is
  a non-empty list (a corrupt string/dict record previously reached
  `current_plan` and would crash the first `step.get(...)` on it).
  `build_exit_result` moved into the new module verbatim to keep
  `frontier_loop_node.py` inside its size guard (900 → 899).
- **P3.4 — Idempotency guarantees for non-idempotent tools.** **DONE.**
  *Location:* `src/tools/constants.py` (`IDEMPOTENT_TOOLS`),
  `tools_config.is_idempotent_tool`, `tool_execution_pipeline._timeout_result`,
  `tool_execution_service.execute_with_retry`. *Complexity:* High.
  *Impact:* Medium–High — closes the §7 "NOT AUDITED" idempotency gap, and it
  was worse than an unverified risk: the double-apply is reachable on the
  production path. `_dispatch_tool_call` runs tools on a `ThreadPoolExecutor` and
  calls `future.cancel()` on timeout, which returns `False` for an
  already-running future. The tool keeps executing and lands its side effect
  *after* the pipeline has already reported failure — verified by execution, and
  pinned by `test_a_timed_out_tool_keeps_running_and_lands_its_effect`. So a
  plain "failed" result was an invitation to re-run, i.e. to apply a write
  twice. A timeout on a non-idempotent tool now reports
  `error_code="tool.outcome_unknown"` with `outcome_unknown: True` and wording
  that tells the model to verify before re-running rather than re-run on faith
  (P3.2's change journal is what makes that verification possible).
  Classification is a **fail-closed allowlist**: only pure reads and
  overwrite-shaped writes (`write_file`, `edit_file*`, `apply_patch`,
  `multiedit`, `delete_file`, `rename_file`) are retryable, so a newly added or
  unclassified tool is treated as non-idempotent rather than requiring someone to
  remember to classify it. `bash`, `git_commit`/`git_stash`, the append-style
  tools (`manage_todo`, `record_mistake`, `memory_save`), `delegate_task`,
  `create_state_checkpoint`, the *popping* `revert_last_tool`, and the
  human-facing `send_user_message`/`ask_user` are all deliberately excluded.
  Membership is resolved through `resolve_tool_alias` first, so `run`/`shell`
  cannot read as safe merely because no allowlist mentions them.
  `execute_with_retry` (TS-4) — which retried blindly on any
  transient-looking error string — now runs a tool **once** unless its name is on
  that allowlist, and annotates the result with
  `error_code="tool.retry_suppressed"` plus the reason, so a suppressed retry is
  visible in the transcript instead of silently missing. It is fail-closed with
  no `tool_name` too; the four TS-4 backoff tests now pass an idempotent
  `tool_name`, since the old ones encoded blind retry of an unknown tool as the
  expected behaviour. Note this helper still has no production caller — the
  timeout fix is what closes the reachable path.
  Adapter-level retries were audited and are *not* part of this defect:
  `openai_compat_adapter._execute_with_retry` returns immediately for
  `stream=True`, so a dropped stream is never replayed, and a retried
  non-streaming completion cannot double-execute a tool because tool calls only
  run on the agent side, from a response that was actually received.

## Phase 4 — Advanced features

> **Status: 1 of 4 complete** — P4.1 done (measurements and file references in
> the entry below); P4.2–P4.4 open.

- **P4.1 — Incremental/cached repository indexing.** **DONE.** *Location:*
  `src/core/indexing/repo_indexer.py` (`walk_language_files`,
  `DEFAULT_IGNORE_DIRS`), consumed by `index_repository` (and thereby by
  `analysis_node.py:319` and `repo_context.refresh_index_after_write`).
  *Complexity:* Medium. *Impact:* High — makes P1.2 affordable enough to run on
  every task. The audit's "incremental" index was already hash-incremental
  (`repo_index_meta.json`, `incremental=True`), but a warm call on an unchanged
  tree still cost **~4.0 s**, and profiling showed why: **98 % of the time was
  the file-discovery walk, not parsing** — 4.52 s of 4.595 s in `pathlib.rglob`,
  versus 0.06 s for `compute_file_hash` + `parse_file` over all 1,016 files.
  The cause was one `rglob("*" + ext)` per extension (17 full-tree walks, since
  `LANGUAGE_EXTENSIONS` has 17 keys) with the ignore filter applied on
  `path.parts` *after* enumeration — so `.venv` (23,131 entries), `.git` (2,649)
  and `.codingAgent` (651) were walked 17 times each, every call. Replaced with
  a single `os.scandir` traversal that prunes `DEFAULT_IGNORE_DIRS` *before*
  descending and does not follow symlinked directories. **Proof [EXEC]:**
  discovery **4,239.7 ms → 44.4 ms** on an **identical file set** (1,016 files,
  set equality verified both directions), and a warm `index_repository` call
  **3,943 ms → 69 ms** (57×); output is now sorted, so the index no longer
  depends on filesystem enumeration order. Deliberate behavior change: ignore
  dirs are pruned *below* `base_path` only, so a repository living under e.g.
  `~/projects/build/myrepo` now indexes instead of silently returning nothing
  (the old ancestor-wide `parts` check matched the base path too).
  `analysis_node`'s process-local `(path, mtime)` cache remains as the second
  layer on top. Verified: full `tests/unit` suite 4,831 passed / 1 skipped
  (exit 0), `ruff` clean, `mypy` clean on `repo_indexer.py`.
- **P4.2 — Multi-agent / hub-and-spoke, or formal removal.** *Location:*
  `delegation_node.py`. *Complexity:* High.
  *Impact:* Medium. The inert `graph_factory.py` was removed in P2.5, so the
  lingering multi-agent question is now only about `delegation_node.py`.
- **P4.3 — Prompt-injection defense in depth.** *Location:*
  `prompt_injection_guard.py`, `memory/security.py`. *Complexity:* High.
  *Impact:* Medium–High. Content-provenance labelling, not only pattern
  matching, so the model can distinguish tool output from operator intent.
- **P4.4 — Configuration consolidation and launch-script unification.**
  *Location:* `.localAgent/config.json` resolution chain, root scripts.
  *Complexity:* Medium. *Impact:* Low–Medium.

---

## What is *not* claimed

To keep this report honest, the following were **not** verified to the standard
applied above and should not be read as endorsements: tool argument validation
and idempotency (§7), LLM-call redundancy and caching (§12), cross-task memory
contamination (§9), and the evaluation framework's internal correctness (§9/10,
marked READ). The four Critical/High findings, by contrast, were each
established by execution or exhaustive reference analysis and are not
speculative.
