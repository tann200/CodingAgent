# Phase 3 — Progress & Next-Task Analysis

**Scope:** Track Phase 3 of the audit roadmap (capability improvements), record completed items, and provide a concrete implementation analysis for the next task.
**Status:** 3.1 + 3.5 + 3.6 + 3.7 complete; remaining items **3.2–3.4, 3.8** pending.

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

## Remaining (next-task candidates)

| # | Item | Location | Complexity | Notes |
|---|------|----------|------------|-------|
| 3.2 | Build evaluation framework | New `src/evaluation/` | High | Systematic quality measurement |
| 3.3 | Add SWE-bench integration | New evaluation harness | High | Industry-standard benchmarking; depends on 3.2 |
| 3.4 | Implement graph-state checkpointing | `inference_loop.py` + LangGraph checkpointer | High | Automatic crash recovery |
| 3.8 | Reconcile documentation test baselines | All docs | Low | Single authoritative count |

Suggested next: **3.4 (graph-state checkpointing)** — High value (automatic crash recovery) and self-contained in `inference_loop.py`, unlike 3.2/3.3 which are High-complexity new subsystems and can reuse the checkpoint milestone for their harness. 3.8 (docs test baselines, Low) remains as a quick win at any point.