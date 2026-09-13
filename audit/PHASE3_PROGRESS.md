# Phase 3 — Progress & Next-Task Analysis

**Scope:** Track Phase 3 of the audit roadmap (capability improvements), record completed items, and provide a concrete implementation analysis for the next task.
**Status:** 3.1 + 3.2 + 3.3 + 3.4 + 3.5 + 3.6 + 3.7 + 3.8 complete — **all Phase-3 items DONE**.

---

## Scope Correction for 3.1 (important)

The audit item 3.1 ("Enable full graph `_USE_FULL_GRAPH = True`", `builder.py:94`) is **stale** relative to the current architecture:

- Production does **not** route through `compile_agent_graph()` / `_USE_FULL_GRAPH`. The production path is `get_compiled_graph_for_orchestrator` → tier-based graphs: `_compile_frontier_graph` ("capable" tier) or `_compile_lite_graph` ("lite"/"small"). The `_USE_FULL_GRAPH` flag and the fast-path 10-node graph are now effectively test-only (used by `tests/`, the legacy `_get_compiled_graph()` singleton, and the `GraphFactory` facade).
- The production frontier graph **already** has `analyst_delegation`, `debug`, and `delegation` nodes active. The only genuinely-missing capability was **replan**: the frontier graph had no `replan` node, so a `requires_split` tool result (oversized patch) could not be split into smaller steps — the frontier loop just kept the error in history.

**Decision (user-approved):** "Add replan to frontier graph" — restore the one genuinely-missing capability in the production tier graph, rather than flipping the dormant `_USE_FULL_GRAPH` flag.

## Completed

| # | Item | Notes |
|---|------|-------|
| 3.1 | Add replan to the frontier graph | Frontier loop now triggers replan on `requires_split`; new `replan` node split oversized steps and re-enters the loop. See below. |
| 3.5 | Consolidate duplicate skill directories | Removed legacy `src/config/skills/`; `explore_codebase` migrated into `agent-brain/skills/`; skill tools + prompt templates repointed. See below. |
| 3.6 | Remove/update stub roles | Deleted defunct `researcher.md` (alias→analyst, never compiled); rewrote `scout.md`/`tester.md` as functional roles; aligned agent overrides. See below. |
| 3.7 | Add CLI feature parity with TUI | New `src/cli/` package + `src/main.py` subparsers: `session`, `status`, `mcp`, `diff` subcommands plus `--provider`/`--model`/`--continue` flags. See below. |

### Item 3.1 details — Add replan to the frontier graph

**Files changed:**
- `src/core/orchestration/graph/nodes/frontier_loop_node.py` — after a tool batch, call `compute_replan_trigger(result=batch_last_result)` (the same patch-size guard semantics used by `execution_node`). When triggered, set `replan_required` + `action_failed=True` + `next_action=None` on exit and break the tight loop so the graph can route to the replan node. Imported `compute_replan_trigger` from `execution_plan.py` (no circular import).
- `src/core/orchestration/graph/tier_graph_routing.py` — `route_frontier_loop_exit` now returns `"replan"` when `replan_required` is set (checked after approval/overflow, before the verification fall-through). Added `route_replan_frontier(state, should_after_replan_fn=...)` mirroring `route_debug_frontier`: maps the full graph's `step_controller`/`perception` continuation targets to `frontier_loop`, and keeps `memory_sync` as the recovery-cap bail-out.
- `src/core/orchestration/graph/builder.py` — `_compile_frontier_graph` gains the `replan` node (wrapped with `_validated("replan", ...)` so its schema is enforced at the boundary) plus two edges: `frontier_loop → replan` (via `route_frontier_loop_exit`) and `replan → {frontier_loop, memory_sync}` (via `route_replan_frontier`).

**Routing caps:** respected because `should_after_replan` (delegated to by `route_replan_frontier`) checks `total_recovery_attempts` against the tier recovery cap, and `replan_node` itself enforces `max_replan_attempts` / `max_total_recovery_attempts`. Replan is therefore bounded — it cannot loop forever.

**Tests added/updated:**
- `tests/unit/test_tier_graph_routing.py` — `test_route_frontier_loop_exit_routes_to_replan_on_patch_size_trigger`, `test_route_replan_frontier_maps_continuation_to_frontier_loop`, `test_route_replan_frontier_bails_to_memory_sync_on_recovery_cap`.
- `tests/unit/test_frontier_analyst_coverage_gaps.py` — `TestFrontierGraphWiring.test_frontier_graph_includes_replan_node` (structural compile check) + `test_frontier_replan_routes_back_to_loop_and_bails_on_cap`.
- `tests/unit/test_frontier_tool_output_truncation.py` — `test_frontier_loop_breaks_to_replan_on_requires_split` (drives the node end-to-end with a `requires_split` tool result; asserts `replan_required`, `action_failed`, `next_action`).

**Gates:** ruff + mypy clean on changed files (the one mypy error in `frontier_loop_node.py:458` is pre-existing on `main`). Affected unit suites + fast-path integration green.

---

## Scope Correction for 3.6 (important)

The audit item 3.6 ("Remove/update stub roles `researcher.md`, `scout.md`, `tester.md`", rooted in finding **MC-5**) is partially stale in its premise but the direction is correct:

- **`researcher.md` was genuinely defunct.** The `researcher` role name canonicalizes to `analyst` everywhere (`canonicalize_subagent_role` in `subagent_payloads.py`, `ROLE_ALIASES` in `role_config.py`). Subagent prompts are compiled via `compile_system_prompt(canonical_role)`, so `researcher.md` was **never read** — only `analyst.md` is. The file was dead content → **deleted**.
- **`scout.md` and `tester.md` are real canonical roles** (`CANONICAL_ROLES`, `canonicalize_subagent_role` passes them through, `SCOUT_AGENT`/`TESTER_AGENT` definitions exist) and ARE used at runtime via `compile_system_prompt("scout"/"tester")`. But they were thin 19-line stubs whose final step told the agent to "Publish to `agent.<role>.broadcast`" — a topic that does not exist in the current typed event architecture (the real topics are `agent.scout.files_discovered`, `agent.researcher.doc_summary`, `agent.<role>.result`, etc.). → These were **rewritten** into functional roles that report via their **returned result** (which is what `delegate_task_async`/`delegation_node` actually consumes), not a publish topic.

### Item 3.6 details

**Files changed:**
- `src/config/agent-brain/roles/researcher.md` — **deleted** (defunct alias; `researcher` → `analyst`).
- `src/config/agent-brain/roles/scout.md` — rewritten: read-only exploration role with concrete search strategy + `<findings>` output format; front-matter preserved; broadcast step removed.
- `src/config/agent-brain/roles/tester.md` — rewritten: test creation/execution role with strategy + `<test_report>` output format; front-matter preserved; broadcast step removed.
- `src/core/orchestration/agent_types.py` — `SCOUT_AGENT`/`TESTER_AGENT` `prompt_override` strings updated to drop the defunct "Publish to agent.<role>.broadcast." sentence (now "report as the final returned text").
- `tests/unit/test_role_brain.py` — new contract tests: (1) no role brain file references `agent.<role>.broadcast`; (2) `researcher` is not a standalone brain role + `normalize_role("researcher") == "analyst"`; (3) `scout`/`tester` are canonical roles with substantive (non-stub) content.

**Gates:** ruff + mypy clean on changed files. Agent-brain/role/prsw/subagent test suites green.

---

## Item 3.5 details — Consolidate duplicate skill directories (MC-4)

Two parallel skill sets existed: prompt assembly (`ContextBuilder` at `context_builder.py:309`, `AgentBrainManager` at `agent_brain.py:126`) read the newer front-matter-based set in `src/config/agent-brain/skills/`, while the LLM-facing `load_skill`/`list_skills` tools (`skill_tools.py`) read the older set in `src/config/skills/`.

**Files changed:**
- `src/config/skills/` — **removed**. The 4 overlapping files (`code_review`, `debug_checklist`, `refactor`, `write_tests`) were older drafts of strictly-more-polished `agent-brain/skills/` versions → dropped. Legacy-only `explore_codebase.md` → migrated.
- `src/config/agent-brain/skills/explore_codebase.md` — **new**, converted to the canonical format (front-matter + When to Use / Strategy / Execution Steps), preserving the original 5-phase content.
- `src/tools/skill_tools.py` — `_SKILLS_DIR` now `config/agent-brain/skills/`; docstrings updated.
- `src/core/prompts/templates/{default,anthropic}.txt` — skill hints updated for the authoritative set (`explore_codebase`, `security_review` added).
- `tests/fixtures/golden_prompts/*.golden` — 8 fixtures regenerated (`UPDATE_GOLDEN=1`) for the template line changes only (verified diff).
- `tests/unit/test_skill_consolidation.py` — contract tests: legacy dir absent; tools resolve against `agent-brain/skills/`; `explore_codebase` present; unique names.

**Gates:** ruff + mypy clean. Full unit suite (4717 passed) + fast-path integration green.

---

## Item 3.7 details — Add CLI feature parity with TUI

Headless `codingagent` reuse was a hard script: short "task" mode and no session/status/mcp/diff visibility. This item reuses the existing in-process plumbing (session store, provider config, MCP config, git) with zero new runtime dependencies.

**New `src/cli/` package:**
- `_helpers.py` — working-directory/config-path resolution helpers + MCP config JSON read/write.
- `session_cmd.py` — `session list` (tabs, recent sessions first), `session show <id|n>` (metadata + flattened transcript), `session export <id>` (markdown with `created_at`), all via `session_store` (`list_sessions`/`load_session`; monkeypatchable `_SESSIONS_DIR`). Export writes atomically with `mkstemp`+`fsync`+`os.replace` to a temp dir when outside the repo.
- `status_cmd.py` — prints active provider/model by resolving the providers config path through the real config API (`_pc.resolve_providers_config_path(None, _pc.__file__)`), plus session count.
- `mcp_cmd.py` — `mcp list` / `mcp status` (reads `.agent/config.json`, no live handshake headlessly) / `mcp add <name> <cmd...>`. Add validates name (`[a-zA-Z0-9_-]+`) and **rejects shell metacharacters** (`[;&$`|<>]`) in the command; writes atomically and warns to restart the session. `cmd` uses `nargs=argparse.REMAINDER` so flags like `-y` survive; a misplaced `--workdir` after the command is swallowed by REMAINDER, so `--workdir` lives on the parent `mcp` parser (`mcp --workdir DIR add name cmd...`).
- `diff_cmd.py` — `diff --path <glob>` runs `git diff --stat --patch HEAD` through the real repo (GitSnapshotManager's shadow repo has no HEAD → dropped that route). Shows a lock-hint line when files are locked.

**`src/main.py` wiring:**
- Subparsers for `session|status|mcp|diff` dispatched to the new package with graceful fallback to the legacy task-mode parser if `argparse` rejects the invocation.
- Global flags `--provider`/`--model` (moved off the task parser) and `--continue`. `--provider`/`--model` are applied headlessly by publishing `ModelRouting(provider=…, selected=…, available_models=[])` **after** `Orchestrator` construction (ProviderManager is wired to the bus → `llm_manager._on_model_routing` performs the live switch). `available_models` is a required positional on `ModelRouting` (`event_types.py:826`) — a missing kwarg raised `TypeError`, silently swallowed by the try/except guard (did not break routing, but the override was a no-op).
- `--continue` resumes the last session: loads `.codingAgent/last_plan.json` for the `task` (via `get_last_plan_path(workdir)`), falling back to scanning the newest 10 sessions for the final user/human message; on resume, the target session id is passed through to `_run_headless` and the task prompt is skipped.
- Headless streaming (`--output stream`) now emits tokens incrementally via a `response.stream_chunk` subscription (pre-existing hook moved before `run_agent_once`).

**Tests added:** `tests/unit/test_cli_subcommands.py` — 25 tests across `TestParser` (arg parsing incl. `--provider/--model/--continue` placement), `TestMcpSubcommand` (unsafe-cmd rejection, config write/placement), `TestSessionSubcommand` (list/show/export against a temp session dir + atomic export into the repo), `TestHeadlessRouting` (a `model.routing` event is published on `_run_headless(provider=…)`); patched `Orchestrator` via `orch_mod.Orchestrator` to a FakeOrch/FakeBus.

**Smoke-tested manually:** `status`, `mcp list/add/status`, `session list/show/export`, `diff --path`.

**Gates:** ruff + mypy clean (8 source files: `src/cli/`, `src/main.py`, tests). Full unit suite + fast-path integration green (4744 tests passing, exit 0; two earlier full-suite runs hit transient environment hangs — collection and runs are otherwise fast and consistent).

---

## Completed — 3.4 Graph-State Checkpointing (automatic crash recovery)

**Files:**
- `src/core/orchestration/graph/checkpoint_saver.py` (new) — durable checkpointing for the tier graphs.
- `src/core/orchestration/graph/builder.py` — `_compile_frontier_graph`/`_compile_lite_graph` now `workflow.compile(checkpointer=_graph_checkpointer())` (production tiers only; legacy full/fast-path graphs untouched).
- `src/core/orchestration/inference_loop_rounds.py` — `_run_graph_round_sync` config now carries `"thread_id": thread_key(orch)`.
- `src/core/orchestration/inference_loop.py` — crash-recovery rehydration of `initial_state` on re-entry; per-round durable snapshots; purge on clean completion.
- `src/main.py` — headless seeding of `orch._current_task_id` (resume = original session id; fresh = minted uuid) so `--continue` resumes the same thread.
- `tests/unit/test_graph_checkpointing.py` (new, 20 tests).

**Design:**
- **`JsonlCheckpointSaver(BaseCheckpointSaver)`** — per-thread JSONL under `agent_context_path(workdir)/checkpoints` (`checkpoint_{thread}.jsonl` + `.writes.jsonl`); pickle+base64 payloads with `LIVE_CHANNELS` (`cancel_event`, `_file_lock_manager`, `_agent_session_manager`, `_context_controller`, `_write_queue`, `_pending_injections_source`) and unpicklable values dropped (graceful degradation); rotation keeps newest `_MAX_RECORDS_PER_THREAD` (500). Validation confirmed langgraph 1.1.10 only persists `channel_values/channel_versions/id/ts/updated_channels/v/versions_seen` — **no stored `next`/`versions`** — and that a plain on-disk checkpoint cannot re-run pending nodes (`app.invoke(None, …)` no-ops; node resume needs in-memory task bookkeeping the loop does not serialize).
- **Round-boundary state snapshots** (the actual recovery mechanism) — after each completed graph round the inference loop writes a JSON-safe copy of the graph state to `checkpoint_{thread}.state.jsonl` (atomic tmp-file replace; unserializable keys skipped). On thread re-entry (`--continue` reusing the task id), `load_thread_state` + `rehydrate_initial_state` merge the surviving snapshot over the fresh `build_initial_state` so completed rounds are not redone. Clean completion purges all three per-thread files; cancellation/loop-limit paths intentionally keep them.
- **Toggle:** `CODINGAGENT_GRAPH_CHECKPOINTING` env 0/1 override → config `graph_checkpointing` → default OFF under pytest. `default_checkpointer()` is a thread-safe process singleton shared by both tier graphs.

**Gates:** ruff + mypy clean (source + tests). Full unit suite + fast-path integration green (4764 tests passing = 4744 + 20 new; exit 0).

---

## Completed — 3.8 Documentation Test Baselines (single authoritative count)

Reconciled every test-count claim in the documentation tree to a single authoritative number.

- **Single source of truth:** README "Test Baseline" now defines the baseline as whatever `pytest tests/unit tests/integration/test_fast_path_graph_e2e.py` collects — currently **4,764 tests** (4,761 unit across 352 files + 3 fast-path integration).
- **Current-state claims updated to 4,764/4,761:** `README.md` (was 4,659), `docs/developer-guide.md` (was 3,537, header + tree), `docs/DEVELOPMENT.md` (was 3,844), `AGENTS.md` (was ~4,660), `COMPREHENSIVE_AUDIT_REPORT.md` + `audit/COMPREHENSIVE_AUDIT_REPORT.md` (was ~4,660, both count claims), `docs/codingagent-architecture.md` structure block (was 240/206/25/3) and §13 tree opener (was ~330).
- **Historical reports annotated, not rewritten:** `docs/TEST_QUALITY_ANALYSIS.md` (4,399) and `docs/TEST_CLEANUP_SUMMARY.md` (4,399→4,388) keep their point-in-time numbers but now carry a note pointing at the authoritative baseline.
- **Verified by collection:** `tests/unit` = 4,761, fast-path integration = 3; remaining dirs (integration/e2e/benchmarks/acceptance) collect 175 more but are not part of the gate suite.
- **Known leftover (Phase 2):** root-level `COMPREHENSIVE_AUDIT_REPORT.md` is a stale duplicate of `audit/COMPREHENSIVE_AUDIT_REPORT.md`; counts in both were aligned but deleting the duplicate remains open cleanup.

---

## Completed — 3.2 Evaluation Framework

Surfaced the scenario-evaluation core (which already ships 23 standardized scenarios + pass@k) as a first-class framework with a CLI runner and a golden-regression gate.

**Files:**
- `src/core/evaluation/__init__.py` (new) — public API: `Scenario`, `ScenarioResult`, `ScenarioEvaluator`, `get_default_scenarios`, `pass_at_k`, `run_pass_at_k`, `run_benchmark`.
- `src/core/evaluation/regression.py` (new) — baseline `save_baseline`/`load_baseline`, `compare_baseline` (per-scenario regression/improvement/new/missing + `got_regressions`), `is_regression`; atomic tmp-file write; corrupt/missing baseline → `None`.
- `src/core/evaluation/cli.py` + `__main__.py` (new) — `python -m src.core.evaluation` with subcommands:
  - `list` — scenario inventory (filter by `--category`/`--difficulty`).
  - `run` — execute scenarios against a `module:callable` agent factory (`--agent`), `--samples n` for pass@k, `--output report.json` (JSON report), `--baseline <path>` compares against a golden baseline and **exits 1 on regression** (CI gate).
  - `baseline-save` — run and persist a golden baseline with `--metadata k=v`.
- `tests/unit/test_evaluation_framework.py` (new, 16 tests) — package exports, baseline round-trip + corrupt/missing handling, regression detection (pass→fail, pass→error, fail→pass, new/missing), CLI list/run/output/baseline-exit-code, pass@k sampling, bad-factory resolution. Fake agents injected via synthetic `sys.modules` modules (no live-LLM dep).

**Design notes:**
- Best-of-`n` scenario status (pass if any attempt passed) is the unit compared against a baseline, so run-to-run variance on easy scenarios does not false-positive the CI gate; individual attempt outcomes are still fully recorded in `--output` reports.
- Agent surface dispatch reused from `scenario_evaluator._run_agent_for_scenario`: supports `run(...)`, `run_agent_once(...)`, or `__call__` agents — the Orchestrator is the intended live target via `default_agent_factory()`.
- Exit codes: 0 success; 1 regression detected; 2 usage/configuration error.

**Gates:** ruff + mypy clean (`src/core/evaluation/` + `tests/unit/test_evaluation_framework.py`). Full gate suite green — **4,780 tests passing** (4,764 + 16 new; exit 0).

---

## Completed — 3.3 SWE-bench Integration

Added a SWE-bench style grading harness that plugs into the 3.2 evaluation framework.

**Files:**
- `src/core/evaluation/swebench.py` (new) — `SWEBenchInstance` data model (+`from_dict`/`to_dict` with `FAIL_TO_PASS`/`PASS_TO_PASS` aliases and required-field validation); `load_instances(source)` for JSON (list/dict-keyed/single) / JSONL / directory; `checkout_repo` (clones a local path, `file://`, or `org/repo` GitHub spec at `base_commit`); `grade_runnable` (extract agent patch via `git add -N .` + `git diff --binary HEAD` so new files are captured, apply `test_patch`, run FAIL_TO_PASS/PASS_TO_PASS with pytest `-x` — or an explicit `test_command` — and classify pass/fail/error); `SWEBenchRunner` (isolated per-instance run dirs, `_ensure_working_dir` alignment, agent surface dispatch via `run`/`run_agent_once`/`__call__`, `summarize`).
- `src/core/evaluation/cli.py` — new `swebench` subcommand: `--source`, `--agent`, `--limit`, `--workdir`, `--output <report.json>`, `--baseline <json>` (exit 1 on regression).
- `src/core/evaluation/__init__.py` — exports `SWEBenchInstance`/`SWEBenchRunner`/`load_instances`.
- `tests/unit/test_swebench_integration.py` (new, 24 tests) — data model + loader variants + validation errors, grading (pass/fail/error, new-file capture, test_command override, test_patch apply failure), runner end-to-end pass/fail/checkout-error, CLI run/report/regression-exit-code. Offline fixtures are git repos built at test time via `git init`/`commit`; no network.

**Design notes:**
- Grading is round-trip safe: the agent works in a clean checkout at `base_commit`; no commit is made by the agent; the grader diffs the working tree so a regression baseline can compare patches if needed.
- Real test runs use the local `python -m pytest` runner; actual SWE-bench datasets (which need conda/docker environments per instance) are a documented extension — the harness expects pre-installed dependencies in the invocation environment.
- Regression baselines from other subcommands are compatible (compare uses `scenario_name`/`status`).

**Gates:** ruff + mypy clean (`src/core/evaluation/` + both new test files). Full gate suite green — **4,804 tests passing** (4,780 + 24 new; exit 0).

---

## Remaining (next-task candidates)

| # | Item | Location | Complexity | Notes |
|---|------|----------|------------|-------|
| — | Phase 3 COMPLETE | — | — | All eight Phase-3 items (3.1–3.8) are done and committed. Suggested next: Phase 4 advanced features (multi-agent orchestration, adaptive tool selection, semantic search improvements, distributed training). |