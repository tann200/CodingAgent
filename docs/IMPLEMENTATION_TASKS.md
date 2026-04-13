# Implementation Tasks

> **Source:** Extracted from docs/implementation-plan-claw-parity.md and docs/CODEBASE_FINDINGS.md  
> **Last updated:** 2026-04-13

---

## Part A: TASK-1 to TASK-12 (from implementation-plan-claw-parity.md)

| # | Phase | Task | Effort | Status |
|---|-------|------|--------|--------|
| 1 | 1 | Add CLAUDE.md to `_CANDIDATE_NAMES` in instruction_files.py | XS | **DONE** |
| 2 | 5.1 | Define `src/core/interfaces.py` (Protocol classes) | S | **DONE** |
| 3 | 2 | Add `permission_kind` to `@tool` decorator | M | **DONE** |
| 4 | 2 | Wire `PermissionPolicy` into `PermissionGateway` Gate 3 | S | **DONE** |
| 5 | 6 | Implement `frontier_loop_node.py` | L | **DONE** |
| 6 | 6 | Tier-based graph cache + routing in builder.py | M | **DONE** |
| 7 | 4 | Implement `jsonl_session_store.py` | L | **DONE** |
| 8 | 4 | Storage backend toggle (orchestrator_bootstrap.py) | S | **DONE** |
| 9 | 4 | Migration script (scripts/migrate_sessions.py) | S | **DONE** |
| 10 | 3 | Extend ShellHookRunner with per-tool matcher | S | **DONE** |
| 11 | 5.2 | MCP SSE transport | M | **DONE** |
| 12 | 5.2 | MCP WebSocket transport | M | **DONE** |

### TASK Details

#### TASK-1: Add CLAUDE.md to instruction file candidates
- **File:** `src/core/context/instruction_files.py`
- **Change:** Extend `_CANDIDATE_NAMES` to include CLAUDE.md, CLAUDE.local.md, .claude/CLAUDE.md, .claw/CLAUDE.md, .claw/instructions.md

#### TASK-2: Define interfaces.py (Protocol classes)
- **File:** `src/core/interfaces.py` (new)
- **Defines:** `ApiClientProtocol`, `ToolExecutorProtocol`, `SessionStoreProtocol`

#### TASK-3: Add permission_kind to @tool decorator
- **File:** `src/tools/_tool.py`
- **Add:** `PermissionKind` enum + `permission_kind` parameter to `@tool`

#### TASK-4: Wire PermissionPolicy into PermissionGateway
- **File:** `src/core/orchestration/permission_gateway.py`
- **Add:** `_gate2b_policy_rules()` method between Gate 2 and Gate 3

#### TASK-5: Implement frontier_loop_node.py
- **File:** `src/core/orchestration/graph/nodes/frontier_loop_node.py` (new)
- **Purpose:** Single mega-node for LARGE/FRONTIER models

#### TASK-6: Tier-based graph cache in builder.py
- **File:** `src/core/orchestration/graph/builder.py`
- **Add:** `_GRAPH_CACHE` dict + `get_compiled_graph()` + `invalidate_graph_cache()`

#### TASK-7: Implement jsonl_session_store.py
- **File:** `src/core/memory/jsonl_session_store.py` (new)
- **Interface:** Must match existing `SessionStore` for drop-in replacement

#### TASK-8: Storage backend toggle
- **File:** `src/core/orchestration/orchestrator_bootstrap.py`
- **Add:** `_resolve_storage_backend()` + env var / config file support

#### TASK-8b: ShellHookRunner matcher field
- **File:** `src/core/orchestration/shell_hooks.py`
- **Add:** Optional `matcher` field to hook entries (backward compatible)

#### TASK-9/10: MCP transports
- **File:** `src/core/mcp/mcp_client.py`
- **Add:** `McpSseClient` and `McpWsClient` classes

---

## Part B: CODEBASE_FINDINGS — High Priority Fixes

### Critical / High Severity (from docs/CODEBASE_FINDINGS.md)

| ID | File | Issue | Severity |
|----|------|-------|----------|
| CRIT-1 | `src/tools/sandbox.py:63` | Invalid bwrap flags — sandbox never activates | Critical |
| HIGH-1 | `src/tools/file_tools.py:73-74` | `_pending_previews` / `_preview_rejected` globals without lock | High |
| HIGH-2 | `src/tools/web_tools.py:18-22` | SSRF blocklist bypassed by IPv6 and decimal IPs | High |
| HIGH-3 | `src/tools/subagent_tools.py:172` | Delegation depth from forgeable `os.environ` | High |
| HIGH-4 | `src/core/orchestration/approval_gate.py:55-59` | Shared dicts/sets without lock | High |
| HIGH-5 | `src/core/orchestration/workspace_guard.py:62` | Directory pattern check bypassed for absolute paths | High |
| HIGH-6 | `src/core/orchestration/agent_session_manager.py:186-192` | Potential AB-BA deadlock | High |
| HIGH-7 | `src/core/orchestration/mcp_stdio_server.py:358` | asyncio.run() inside running event loop | High |
| HIGH-8 | `src/core/orchestration/tool_hooks.py:177-184` | shell=True with project-controlled hook command | High |
| HIGH-9 | `src/core/orchestration/graph/nodes/delegation_node.py:69` | Mutates os.environ for depth tracking | High |
| HIGH-10 | `src/core/orchestration/graph/nodes/execution_node.py:876-910` | Read-before-write path overwrites state["task"] | High |
| HIGH-11 | `tui/src/ui/core_bridge.py:857` | asyncio.run on background thread conflicts with Textual | High |
| HIGH-12 | `src/tools/system_tools.py:184` | Path.cwd() default evaluated at import time | High |

### Medium Severity (selected)

| ID | File | Issue |
|----|------|-------|
| MED-1 | `src/tools/file_tools.py:363` | Diff + line count inflated by +++ header |
| MED-2 | `src/tools/batch_tools.py:112` | Unbounded thread pool |
| MED-3 | `src/tools/tools_config.py` | Module globals written without lock |
| MED-4 | `src/core/orchestration/orchestrator.py:2083-2121` | Temp file leak if assignment not reached |
| MED-5 | `src/core/orchestration/orchestrator.py:3524` | ThreadPoolExecutor recreated every run_agent_once |
| MED-6 | `src/core/orchestration/orchestrator.py:2793` | Loop prevention threshold off-by-one |
| MED-7 | `src/core/orchestration/cross_session_bus.py:434-466` | Subscriber callbacks called while holding lock |
| MED-8 | `src/core/orchestration/event_log.py:198` | assert stripped in optimized mode |
| MED-9 | `src/core/orchestration/file_lock_manager.py:62-68` | Sync can_write/can_read race with async mutators |
| MED-10 | `src/core/orchestration/mcp_stdio_server.py:282` | rglob with no depth/count limit |

---

## Part C: TODO/FIXME/XXX Occurrences in Code

### Files with TODO/FIXME/XXX Comments (src/ only)

| File | Count |
|------|-------|
| src/tools/todo_tools.py | 30+ |
| src/core/context/context_builder.py | 4 |
| src/core/memory/distiller.py | 3 |
| src/core/orchestration/task_lifecycle.py | 3 |
| src/core/orchestration/registry_builder.py | 3 |
| src/core/orchestration/loop_guards.py | 1 |
| src/core/orchestration/graph/nodes/planning_node.py | 3 |
| src/core/orchestration/graph/nodes/execution_node.py | 2 |
| src/core/memory/advanced_features.py | 2 |
| src/tools/memory_tools.py | 3 |
| src/tools/verification_tools.py | 2 |
| src/core/orchestration/graph/builder.py | 1 |

### Notable Clusters

- **TODO.md handling:** Many files reference TODO.md read/write/clear operations. Edge case flagged: `manage_todo` bypasses read-before-write (RBW) guard (docs/agent-loop-improvement-analysis.md).
- **TODO injection in perception:** docs/tiered-model-redesign-plan.md mentions injecting TODO state into perception_node so LLM sees progress.

---

## Execution Order (Recommended)

```
Sprint 1 (low-risk, high-value):
  TASK-1  → TASK-2  → TASK-3  → TASK-4
  TASK-8b (ShellHookRunner matcher — isolated, no deps)

Sprint 2 (medium complexity):
  TASK-7  → TASK-8 + TASK-8b (JSONL store + toggle + migration)
  TASK-9  → TASK-10 (MCP transports — independent)

Sprint 3 (high complexity — frontier path):
  TASK-5  → TASK-6 (frontier_loop_node → tier-based routing)
```

---

## Open Bugs (from TEST-1 in CODEBASE_FINDINGS.md)

These are documented in `tests/unit/test_bash_planning_threading_bug_documentation.py`:

| Bug ID | Description | Test Behavior |
|--------|-------------|----------------|
| NEW-7 | bash double-space bypass | Weak assertion |
| NEW-8 | should_after_step_controller off-by-one | Ambiguous assertion |
| NEW-9 | Fragile config.get() re-fetch in planning_node | pytest.skip() |
| NEW-10 | ContextBuilder uses cwd not working_dir | Inverted assertion |
| NEW-12 | execution_node create_task + polling pattern | pytest.skip() |
| NEW-16 | delegate_task_async unbounded ThreadPoolExecutor | pytest.skip() |
| NEW-21 | TrajectoryLogger.log_run not thread-safe | pytest.skip() |
| NEW-22 | VectorStore.search returns raw vector column | pytest.skip() |
