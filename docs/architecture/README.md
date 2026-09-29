# CodingAgent — Architecture

**Generated:** 2026-09-29 by direct code inspection.
Every claim below is traceable to a `file:line` reference. Where this document
and the code disagree, **the code is correct and this document is stale** —
file an issue against this doc rather than the code.

Method: topology and line counts were read from the live tree and, where
behavioral claims are made (e.g. which graph a tier compiles to), verified by
importing the real modules and calling them.

---

## 1. Project shape

| Area | Files | Lines | Role |
|---|---:|---:|---|
| `src/core/orchestration/` | 118 | 38,741 | Graph pipeline, inference loop, tool execution, session state |
| `src/tools/` | 46 | 15,020 | 71 `@tool`-decorated tools + registry/permission/sandbox plumbing |
| `src/core/inference/` | 37 | 10,473 | Provider adapters, model tiers, retry/resilience, tokenizer, telemetry |
| `src/core/memory/` | 20 | 6,618 | Session stores (jsonl/sqlite), vector memory, compaction, distiller |
| `src/core/indexing/` | 6 | 2,866 | Repo indexer, symbol graph, LSP |
| `src/core/context/` | 12 | 2,474 | ContextBuilder, prompt assembly, token truncation |
| `src/core/messaging/` | 5 | 2,913 | Typed MessageBus + event dataclasses |
| `src/core/evaluation/` | 7 | 2,182 | Scenario runner, pass@k, SWE-bench, model comparison |
| `src/core/mcp/` | 6 | 1,875 | MCP client (stdio/http/sse/ws) + stdio server |
| `src/server/` | 12 | 1,655 | HTTP/SSE/WebSocket server |
| `src/cli/` | 6 | 501 | CLI subcommands |
| `tui/` | 252 | 45,514 | Textual TUI (mirrored twice — see §7) |
| `tests/` | 869 | 89,046 | unit (4,911 collected) + integration/e2e/acceptance/benchmarks |

`output/` (14,426 files) is **untracked** agent run output and is not part of
the source tree.

---

## 2. Entry points

| Surface | Module | Notes |
|---|---|---|
| `codingagent` console script | `src/main.py:main` | `pyproject.toml:61` |
| Root shim | `main.py` | delegates to `src.main` |
| TUI | `tui/tui_src/ui/main.py` | runtime imports `tui_src` |
| Server | `src/server/` | HTTP + SSE + WS |
| Launch scripts | `start.sh`, `start_tui.sh`, `start.ps1`, `starth.sh`, `scripts/run.sh` | 5, not 1 |

---

## 3. The production graph (this is the part most docs get wrong)

There are **four** compile paths in `graph/builder.py`. Only two run in
production.

| Compiler | Nodes | Production? |
|---|---:|---|
| `_compile_frontier_graph()` | 11 | ✅ **yes** — tiers medium/large/frontier/nano |
| `_compile_lite_graph()` | 3 | ✅ **yes** — tiers small/lite |
| `_compile_full_graph()` | 14 | ❌ legacy, gated by `_USE_FULL_GRAPH = False` (`builder.py:94`) |
| `_compile_fast_path_graph()` | 10 | ❌ test/legacy only |

Selection is `get_compiled_graph_for_orchestrator()` → `_resolve_graph_tier()` →
`build_tier_graph()` (`builder.py:748-787`). The tier is resolved by
`classify_model(model, context_window)` and collapsed to two cache keys by
`select_tier_graph_cache_key()`:

```
tier in {small, lite}  -> "lite"      -> _compile_lite_graph()
everything else        -> "capable"   -> _compile_frontier_graph()
```

**Verified by importing and calling the real code** (not by reading):

```
small    -> 4 nodes  [__start__, frontier_loop, memory_sync, perception]
lite     -> 4 nodes  (same)
nano     -> 12 nodes [__start__, analysis, analyst_delegation, debug, delegation,
medium             evaluation, frontier_loop, memory_sync, perception,
large              replan, verification, wait_for_user]
frontier -> 12 nodes (same)
```

Both production graphs compile with `checkpointer=_graph_checkpointer()`
(`builder.py:616,685`) and are cached per tier under `_GRAPH_CACHE` with
`_GRAPH_CACHE_LOCK`.

### 3.1 Frontier graph (12 nodes incl. `__start__`)

```
perception ──┬─(needs_clarification | context_overflow)─> memory_sync
             ├─(next_action already set)───────────────> frontier_loop
             └─(task_is_complex)──> analysis ──> analyst_delegation ──> frontier_loop
                                                          (simple)──────────> frontier_loop

frontier_loop ─┬─(awaiting_plan_approval | needs_user_input)─> wait_for_user
               ├─(requires_split)────────────────────────────> replan ──┐
               ├─(verification needed)────────────────────────> verification
               └─(done)────────────────────────────────────────> memory_sync

verification ──> evaluation ──> memory_sync
replan ──(success)──> frontier_loop        replan ──(recovery cap)──> memory_sync
debug, delegation, memory_sync: conditional exits
```

### 3.2 Lite graph (4 nodes)

```
perception ──> frontier_loop ──> memory_sync
```

Minimal: no analysis, no delegation, no verification, no replan, no
wait_for_user. **A small/lite-tier model therefore has no verification stage at
all** — a real capability difference by tier, not just a speed difference.

### 3.3 Node implementations

`graph/nodes/` is 37 files / 10,600 lines. Largest:

| File | Lines |
|---|---:|
| `frontier_loop_node.py` | 1,035 |
| `planning_node.py` | 960 |
| `execution_node.py` | 608 |
| `analysis_node.py` | 507 |
| `perception_node.py` | 504 |
| `debug_node.py` | 438 |
| `perception_runtime.py` | 435 |
| `delegation_node.py` | 418 |
| `replan_node.py` | 407 |

`perception_node.py` is a thin orchestrator over 9 `perception_*.py`
collaborators. `frontier_loop_node.py` (1,035) is the largest file in the
graph and the production hot loop for capable models.

### 3.4 Round execution & recovery

`_execute_graph_round()` (`inference_loop_rounds.py:29`) probes
`asyncio.get_running_loop()`; when a loop is already running it dispatches to a
thread executor with `contextvars.copy_context()` (`:37-47`), otherwise it calls
`asyncio.run()` directly (`:18`). A fresh event loop is allocated per round on
the non-async path.

Crash recovery is at **round granularity**: `inference_loop` writes a
JSON-sanitized state snapshot per round and rehydrates on same-thread re-entry
(`--continue`). `app.invoke(None, ...)` against an on-disk checkpoint does **not**
resume pending nodes — langgraph 1.1.10 does not persist `next`/`versions`.

---

## 4. Event system

Two cooperating buses:

1. **MessageBus** (`src/core/messaging/bus.py`) — typed delivery, subscribers
   register on event classes. Internals: thread-safe `queue.Queue` for sync
   `publish()` → `_bridge` task transfers to the event loop → `_dispatch` runs
   handlers via `run_in_executor` (so blocking handlers do not stall the loop).
   Critical categories (tool/step/delegation) are FIFO via per-category
   `asyncio.Queue` consumers.
2. **EventBus** (`src/core/orchestration/event_bus.py`) — legacy
   string-addressed facade. `publish_typed(Event(...))` is the current API;
   `publish("name", dict)` is retained. It holds a MessageBus reference and does
   dual emission.

Per-symbol import safety: `_EVENT_IMPORT_PATHS` maps event names to dotted
module paths, resolved lazily with `importlib` under a lock, so one failed
import degrades a single event type rather than the whole mapping.

---

## 5. Tools

71 `@tool` decorators across 46 files in `src/tools/`. Canonical
classification sets live in `src/tools/constants.py` (`TOOL_ALIASES`,
`WRITE_TOOLS_REQUIRING_READ`, `MODIFYING_TOOLS`, `PERMISSION_REQUIRED_TOOLS`,
`PERM_ORDER`, `FILE_TOOLS`, …) and are re-exported by consumers; object identity
is asserted by `TestCanonicalCentralization`.

Execution path: `execute_tool_impl` in `tool_execution_pipeline.py` normalizes
the alias **first** (so a deny rule keyed on the canonical name cannot be
evaded via `run`/`ls`/`write`), then dry-run, then permission/policy gates, then
the tool body. `delete_file` is never auto-approved.

Sandbox: `bubblewrap` (Linux) / `sandbox-exec` (macOS) with network policy
enforced explicitly per call. In autonomous mode the sandbox **must not** fail
open — `_enforcement_required()` is True when `SANDBOX_REQUIRE_ENFORCEMENT` is
set or `is_autonomous()`.

---

## 6. Memory

Three session store backends behind one protocol, selected by
`_resolve_backend(explicit) > CODING_AGENT_STORAGE_BACKEND env > config >
"jsonl"` (`session_store.py:44`):

- `JsonlSessionStore` — append-only rotated JSONL + `locked_file`; per-instance
  lock registry cleared by `delete_session`.
- `SqliteSessionStore` — WAL, pushdown for `decisions.json` (limit in SQL).
- Sidecar `decisions.json` written on every `add_decision`, bounded by `limit`.

Plus `vector_store.py` (episodic memory, JSONL-backed, SHA-256/token fallback
while the sentence-transformers model warms in a background thread),
`auto_compactor.py` (deterministic compaction), `compaction_service.py`,
`distiller.py` (cross-session recency retrieval).

---

## 7. TUI mirror trees — a structural hazard

`tui/src/` and `tui/tui_src/` are **byte-identical mirrors**, both
load-bearing:

- Runtime imports `tui.tui_src`.
- `scripts/run_tui.py`, `python -m tui.src.ui.main`, and some unit tests import
  `tui.src`.

Files inside `tui/src` self-import `tui.tui_src`, so `tui.src` is not a
standalone package. Parity is enforced by
`TestMirrorSync::test_src_and_tui_src_trees_byte_identical`
(`tests/unit/test_core_bridge_decomposition.py`) and should be checked with
`diff -rq tui/src tui/tui_src --exclude=__pycache__ --exclude='*.pyc'` after any
TUI edit. **Every TUI change must be applied twice.**

`app.py` is 753 lines; the bridge is decomposed into `core_bridge.py` (342) plus
7 `_bridge_*.py` mixins (2,240 total).

---

## 8. Canonical-single-source rules enforced in-tree

These are invariants with tests; breaking them causes drift bugs.

| Rule | Location |
|---|---|
| One token estimator | `context/token_truncation.estimate_text_tokens` |
| One tool-output pruner | `nodes/tool_output_truncation.prune_tool_outputs` |
| One tool-classification source | `tools/constants.py` |
| Routing caps | `graph/routing_constants.py` |
| Graph-state checkpointing | `JsonlCheckpointSaver` singleton + per-round state snapshot |
| Langgraph node `config` must be annotated `Optional[RunnableConfig]` (PEP 604 `X \| None` is not recognized) | `graph/state_schemas.py` |
| Tool aliases normalized before any permission check | `tool_execution_pipeline.execute_tool_impl` |
| Autonomous approval requires an operator allowlist | `tools_config.autonomous_approval_allowed` |
| TUI mirror parity | `TestMirrorSync` |

---

## 9. Test layout

| Suite | Collected |
|---|---:|
| `tests/unit` | 4,911 |
| `tests/integration` | 145 |
| `tests/e2e` | 26 |
| `tests/integration_real` | 8 |
| `tests/acceptance` | 4 |
| `tests/benchmarks` | 18 |
| `tests/_deprecated` | 251 (excluded from CI) |

Current status: **4,936 passed, 1 skipped, 0 failures** on `tests/unit`.
CI gates: ruff (`--select=E,F,W --ignore=E501`, fails closed) and a narrow mypy
file list.
