# Phase 5 — Progress & Next-Task Analysis

**Scope:** Address the remaining open findings from the comprehensive audit
(`audit/COMPREHENSIVE_AUDIT_REPORT.md`) that were NOT covered by Phases 1–4,
plus leftover doc/lint cleanups.  Prioritized by severity, security-critical
items first.
**Status:** HS-1 + MC-6 + TW-2 complete; open backlog below.

---

## Completed — HS-1: Autonomous mode must NOT silently suppress approval prompts

Previously `is_autonomous()` auto-allowed EVERY DANGER/PROMPT tool with no
operator override — any process with control over the
`CODINGAGENT_AUTONOMOUS` env var (or a `configure(autonomous_mode=True)`
call) silently disabled all interactive safety prompts.  This included
irreversible deletes (`delete_file` DANGER) and session-ending spawns
(`delegate_task` PROMPT).

**Fix: explicit operator override (fail-closed).**  Autonomous mode now only
skips the approval prompt for tools the operator explicitly allowlisted;
every other gated tool is DENIED loudly (observable error, never a silent
pass-through).

- **New allowlist in `src/tools/tools_config.py`:**
  - `set_autonomous_approve(tools)` / `get_autonomous_approve()` — runtime
    setter/getter for the canonical-tool set (or `{"*"}` for all gated tools).
  - `autonomous_approval_allowed(name)` — the single decision point.  Resolves
    aliases to canonical names first (`run`→`bash`), honors the
    `CODINGAGENT_AUTONOMOUS_APPROVE` env var (comma-separated canonical names
    or `*`), AND the configured set.  Default (empty) = deny everything.
  - `configure(autonomous_approve=...)` — startup opt-in.
  - `reset_to_defaults()` resets the allowlist.
- **All four suppression sites now consult the allowlist:**
  - `tool_execution_pipeline._run_permission_gate` (PRODUCTION path) — denies
    unlisted gated tools with an explicit error (was a silent `return None`).
  - `tool_execution_service._check_permission_gate` — mirrored (blocked
    verdict for unlisted tools).
  - `permission_gateway._gate5_user_approval` — mirrored (denied at gate 5 for
    unlisted tools).
  - `_bash_exec._check_tier3_approval` — bash command-level tier-3 gate is no
    longer suppressed by autonomous mode alone; requires the allowlist to
    include the canonical tool name `bash`.
- **CLI:** `src/main.py` gains `--autonomous-approve <TOOL>…` (or `*`); the
  `--autonomous` help text documents that unlisted gated tools are denied.
- **Existing tests updated:** `test_agent_loop_plaintext_tools.py` and
  `test_loop_prevention.py` now opt their `bash` mocks into the allowlist
  (they previously relied on blanket autonomous auto-approval).

**New contract tests:** `tests/unit/test_phase5_security_hardening.py` (20
tests): default fail-closed, canonical-name + alias resolution, `*` wildcard,
env-var override, roundtrip/reset, `configure` option, production-pipeline
deny/allow/no-gate/auto-approve paths, gateway gate-5 consistency, bash
tier-3 auto-approve-vs-prompt, and ToolExecutionService consistency.

**Gates:** ruff (default + CI-scoped) clean, mypy clean.  Full baseline:
**4,870 tests collected (4,850 + 20 new), all green**; integration tests
patching `_AUTONOMOUS_MODE` updated and passing.

---

## Completed — MC-6: Missing per-tool network policy in sandbox

`bash()` ran inside the sandbox without declaring a network policy — it relied
on `run_sandboxed`'s *default* `network=False` instead of passing it
explicitly (as `bash_readonly()` already did), so the deny-remote-network
intent was default-dependent and invisible.  Worse, `network=False` is only
meaningful when a backend is actually enforcing it: with sandbox level `"off"`
or the unsandboxed fallback (no bwrap / non-enforcing sandbox-exec),
`run_sandboxed` silently ignores the flag and a network-capable command (e.g.
`curl`, `git push`) would run with **full host network**.

**Fix: explicit, enforceable per-tool network policy.**

- **`src/tools/_approval.py`** — new `NETWORK_CAPABLE_COMMANDS` /
  `NETWORK_CAPABLE_SUBCOMMANDS` sets + `is_network_capable(command)` (same
  exact-token / prefix matching style as `is_tier3`).  Covers curl/wget/pip/
  apt/brew/ssh/rsync/scp + multi-token `git clone|fetch|pull|push|ls-remote`,
  `npm install|add|publish`, `cargo install`, `go mod download`, etc.  Local
  commands (`git status`, `npm test`, `ls`) are NOT network-capable.
- **`src/tools/_bash_exec.py`**:
  - `bash()` now declares its policy explicitly — `run_sandboxed(..., network=False)`.
  - New `_check_network_policy(cmd_parts, first_cmd, command)` guard, wired
    into `bash()` right after the tier-3 approval gate (so it covers the
    foreground AND `run_in_background` paths).  When a network-capable command
    would run with the deny unenforceable (level `"off"` or no enforcing
    backend): **refuse** if enforcement is required (autonomous mode or
    `SANDBOX_REQUIRE_ENFORCEMENT`) — fail-closed; otherwise **warn + proceed**
    with a `system.warning` event (mirrors the documented unsandboxed-fallback
    behaviour).
- One reachable-vector note: the analyzer currently rates `curl`/`wget`/`pip`
  as DANGEROUS (hard-blocked at gate 2) and remote `git` subcommands are
  excluded by `GIT_SAFE_SUBCOMMANDS`; the guard is the defense-in-depth that
  keeps network capable commands gated if an allowlist/analyzer changes or
  `bash_security` import fails.

**New contract tests:** 10 added to `tests/unit/test_phase5_security_hardening.py`
(`TestMC6NetworkPolicy`): network-capable classification True/False, guard
no-op under an enforcing sandbox, fail-closed refusal when unsandboxed +
enforcement required, warn-and-run when interactive, explicit `network=False`
in `bash()`'s `run_sandboxed` call, and the wired `bash()` refusal (mock
backend) that never reaches `run_sandboxed`.

**Gates:** ruff (default + CI-scoped) clean, mypy clean.  Full baseline:
**4,880 tests collected (4,870 + 10 new), all green** (4,879 passed,
1 skipped).

---

## Completed — TW-2: `run_in_background` bypasses sandboxing/output capping

`bash(run_in_background=True)` spawned the background process via a bare
`subprocess.Popen(stdout=DEVNULL)` — no filesystem confinement, no network
deny, no timeout, unsupervised.  In autonomous mode with a no-sandbox host a
background server/child would run with **full host privileges and network**.

**Fix: sandbox the background path, fail-closed when unenforceable.**

- **`src/tools/_bash_exec.py`:**
  - New `_build_background_sandbox(cmd_parts, workdir)` returns a sandboxed
    argv for the spawn: bwrap prefix (`_build_bwrap_args(...) + ["--"] +
    cmd`, which is `--unshare-net` + ro system binds + `--die-with-parent`)
    when bwrap is available, else the enforcing sandbox-exec profile
    (`sandbox-exec -f <profile> cmd`) when available+enforcing.  Returns
    `None` when the level is `"off"` or no enforcing backend exists.
  - The `run_in_background` branch now: wraps the Popen argv when a backend
    exists; otherwise **refuses** if `_enforcement_required()` (autonomous /
    `SANDBOX_REQUIRE_ENFORCEMENT` — same fail-closed condition as
    `run_sandboxed`), or **runs unsandboxed with a `system.warning`** event
    in interactive mode.
- The MC-6 network guard (runs just before this branch) already refuses
  network-capable background commands when the deny is unenforceable +
  enforcement required; TW-2 additionally covers non-network commands.
- Background output remains `stream=DEVNULL` (intentionally discarded — an
  inherent output cap), unchanged.

**New contract tests:** 7 added to `tests/unit/test_phase5_security_hardening.py`
(`TestTW2BackgroundSandbox`): bwrap prefix build, enforcing sandbox-exec
profile build, `None` without backend / when level `off`, fail-closed refusal
(spawn never fires), interactive warn-and-run, and sandboxed-vs-raw argv
passed to `Popen`.

**Gates:** ruff (default + CI-scoped) clean, mypy clean.  Full baseline:
**4,887 tests collected (4,880 + 7 new), all green** (4,886 passed,
1 skipped).

---

## Open backlog (remaining audit findings)

All audit items below have been **implemented and CI-verified** (see
`git log -8 --oneline` for `AUDIT PHASE-5` commits `755189a`..`1fa76ae`).
The only truly open rows are the ones still listed with a **▶** marker.

| # | Item | Location | Status |
|---|------|----------|--------|
| HS-6 | 1,868 silent `except Exception: pass` blocks | across `src/` | ✅ Bounded slice done — logged high-value sites (post-exec contract, post-tool hook, CP-3.4 recovery); full triage found remaining swallows intentional |
| TW-3 | Contract `model_validate` fail-open | `src/core/orchestration/tool_execution_pipeline.py` | ✅ fail-closed |
| MC-2 / 1.7 | `HOOK_SESSION_START` exported but no call-site | `src/core/plugin/hook_registry.py:79` | ✅ Already wired (`inference_loop.py`, Phase 3.4) + `test_hook_registry` item 1.7 — verified, no change needed |
| WR-1 | Routing default-to-perception loop risk | `session_routing.py:74` | ✅ Verified interlocking guards + contract tests |
| WR-2 | Two independent round-limiting mechanisms (20 vs 15) | `inference_loop.py`, `planning_routing.py` | ✅ Reconciled via `routing_constants.MAX_GRAPH_ROUNDS` |
| WR-3 | Inter-round compaction drops role alternation | `inference_loop_rounds.py:126-143` | ✅ `[COMPACTED]` system summary + recent + one user turn |
| WR-5 | Complexity-heuristic keyword fragility | `perception_routing.py` | ✅ Language-agnostic structural fallback |
| TW-4 | Inconsistent truncation limits (16 KB / 8 KB / 100 KB) | `_bash_exec.py`, pipeline, `_truncate` | ✅ `RESULT_MAX_CHARS` canonical in `_truncate.py` |
| TW-5 | `_BUILTIN_MODULES` hardcoded | `src/tools/_registry.py:43` | ✅ pkgutil auto-discovery |
| RA-3 | LSP `_semaphore` created but not enforced | `lsp_manager.py:121` | ✅ `limit_concurrency()` enforced in all 6 LSP tools |
| RA-4 | Symbol graph uses MD5 for change detection | `symbol_graph.py:164` | ✅ SHA-256 |
| 3.8 leftover | Root-level duplicate test-report cleanup | repo root | ✅ `.coverage` untracked; `results/`/`coverage.xml` already ignored |
| ▶ Live-provider checks (Phase-3/4 feature surface) | CI `live-provider-checks` job | — | Requires provider credentials; currently skipped |

Every commit above was gated by the full unit baseline (`pytest tests/unit`,
**~4,909 passed, 1 skipped**) plus ruff; GitHub Actions pushed runs
(`git push origin main`) reported `success` for each.