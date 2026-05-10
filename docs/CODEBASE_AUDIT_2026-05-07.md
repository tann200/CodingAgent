# Codebase Audit 2026-05-07

This document records a whole-codebase audit covering `src/`, `tui/`, and the main test/config surfaces.

Audit inputs:

- `python -m compileall src tui`
- `.venv/bin/ruff check src tui`
- targeted file review of the largest hotspot modules
- subsystem review across orchestration, tools, inference/memory/context, and server/TUI

## Summary

The repository has made meaningful progress on incremental extraction and simplification, especially in inference and graph-node helpers. However, there are still several critical regressions caused by partial extractions and alias drift. The most serious problems are active runtime breakages in orchestration and tools, plus a few security/config inconsistencies in server and permission handling.

The main pattern behind the highest-severity failures is:

- extracting helpers without moving all required constants/symbols
- importing helpers under underscored aliases and still calling the old names
- keeping large coordinator modules that are difficult to change safely

## Critical Findings

1. `src/core/orchestration/tool_execution_pipeline.py:989`

- Real syntax error: misindented `except` after `return {"ok": True, "result": res}`.
- `python -m compileall src tui` fails on this file.
- Any runtime path importing this module through orchestrator tool execution is unsafe.

2. `src/tools/_bash_exec.py`

- Runtime-breaking undefined symbols remain after extraction:
  - `_logging`
  - `RESTRICTED_COMMANDS`
  - `RESTRICTED_ALLOWED_SUBCOMMANDS`
  - `TAR_EXTRACT_FLAGS`
  - `TAR_CREATE_FLAGS`
  - `CODE_EXEC_INTERPRETERS`
  - `CODE_EXEC_FLAGS`
  - `GIT_SAFE_SUBCOMMANDS`
  - `SAFE_COMMANDS`
  - `TEST_COMPILE_COMMANDS`
  - `run_sandboxed`
  - `_truncate_bash_output`
  - `is_tier3`
  - `is_autonomous`
  - `get_event_bus`
- `bash()` / `bash_readonly()` are not trustworthy until this is fixed.

3. `src/tools/_file_io.py`

- Partial extraction left many undefined symbols and alias mismatches:
  - `get_project_deny_write_patterns`
  - `check_read_before_write`
  - `_WRITE_HARD_LINE_LIMIT`
  - `register_preview_gate`
  - `_publish_diff_preview`
  - `_WRITE_WARN_LINE_LIMIT`
  - `mark_file_read`
  - `_READ_FILE_MAX_LINE`
  - `_READ_FILE_MAX_CHARS`
- This affects core read/write behavior, deny-write policy, preview gating, and read-before-write guardrails.

4. `src/tools/_tool.py`

- Tool schema generation is structurally broken.
- `ToolDefinition.to_openai_schema()` appears incomplete, and later schema assembly references `required` out of scope.
- Since registry code depends on this path, tool/function-calling behavior is fragile.

5. `src/core/memory/distiller.py`

- `_atomic_write_text` and `_atomic_write_json` are called but not defined.
- Distillation persistence can fail at runtime during compaction checkpoint, `TASK_STATE.md`, and repo-memory writes.

## High Severity Findings

1. `src/server/app.py`

- Admin-token protection is inconsistent.
- Scheduler/WebSocket admin paths are guarded, but task/session/SSE surfaces are not consistently protected.

2. `src/server/app.py`

- SSE handler cleanup is keyed only by `session_id`.
- Multiple clients on the same session can unsubscribe each other.

3. Tier-graph routing drift

- Tier-aware graph selection exists but major callers still use the old compiled-graph path.
- This likely leaves frontier/lite behavior effectively dead in real runs.

4. `src/core/orchestration/graph/nodes/frontier_loop_node.py`

- Uses `conversation_history` instead of the canonical `history` state key.
- If enabled, it will drift from the rest of the memory/update pipeline.

5. Step retry contract drift

- Some code stores retry keys as `int`, other paths use `str`.
- Retry caps can be bypassed.

6. `src/core/inference/provider_probe.py` / `src/core/inference/provider_context.py`

- Active context-window state is process-global and overwritten by provider probing.
- Prompt budgets can reflect the wrong provider.

7. `src/core/context/prompt_cache.py`

- Static prompt cache key is too coarse.
- Different models in the same provider family can reuse stale cached prompt guidance.

8. `src/core/memory/jsonl_session_store.py`

- Snapshot creation can report success even if persistence failed.
- Existing fallback helper is not used.

9. `src/core/inference/llm_manager.py`

- `ProviderManager.initialize()` is still race-prone under concurrent callers.

10. Network permission inconsistency

- Network tools are classified as `PermissionKind.NETWORK` but also treated as `PermissionLevel.READ_ONLY`.
- Approval behavior is therefore inconsistent with real side effects.

Status:

- Fixed the remaining runtime mismatch in `src/core/orchestration/permission_gateway.py` by mapping the real network tool names (`read_web_page`, `web_search`) into the stored permission-table network kinds (`webfetch`, `websearch`).
- `src/tools/tools_config.py` already classified both network tools as `PermissionLevel.DANGER`; the missing network mapping meant stored allow/deny rules did not consistently apply to the actual tool names.

11. `src/tools/toolsets/loader.py`

- Compatibility shim delegates to `get_tools_for_role()`, but the canonical loader does not provide that function.

Status:

- Fixed the remaining shim drift in `src/tools/toolsets/loader.py` by forwarding the canonical `load_toolset_for_model()` helper as well.
- The canonical loader now already provides `get_tools_for_role()`; the live compatibility gap was that legacy-call-site imports preferred the shim for model-aware loading, but the shim did not expose the model-aware helper and therefore silently fell back to non-model-aware loading.

12. `src/tools/interaction_tools.py`

- Same aliasing bug pattern as `_bash_exec.py` and `_file_io.py`.
- Event-bus access uses the wrong symbol name.

Status:

- Already fixed in `src/tools/interaction_tools.py` by importing the event-bus module and calling `_event_bus_module.get_event_bus()` consistently.
- Existing regressions in `tests/unit/test_gap_impl_fixes.py` and `tests/unit/test_send_user_message.py` cover the corrected event-bus access and unsubscribe behavior.

## Medium Severity Findings

1. `src/core/memory/sqlite_session_store.py`

- Fallback path references `agent_context_path` without ensuring it is bound in the import-failure branch.

2. Frozen memory injection

- `ContextBuilder` reads frozen memory for prompts, but the load path appears unwired.

3. `src/server/app.py`

- `register_event_bus()` is not idempotent; repeated calls can duplicate subscriptions.

4. TUI settings/config hydration

- `AgentBridge` and `AgentApp` both hydrate provider/system settings, duplicating config parsing.

Status:

- Fixed in `tui/src/ui/core_bridge.py` and `tui/src/ui/app.py` by routing startup/request hydration through the bridge-owned `system.settings` path and adding the missing bus-to-`SystemSettingsLoaded` translation.

5. TUI subagent widget bookkeeping

- Some progress widgets are created but not consistently stored in `_subagent_widgets`.

Status:

- Fixed in `tui/src/ui/app.py` by registering widgets on subagent start, reusing existing widgets for duplicate start events, and cleaning them up consistently on finish.

6. `tests/conftest.py`

- `sync_threads` shim does not accept all realistic `threading.Thread(...)` kwargs.

Status:

- Largely addressed in `tests/conftest.py`: the synchronous thread shim now accepts realistic constructor kwargs including `name=` and `daemon=` and is still sufficient for the current marked/fixture-backed test usage.
- Focused verification of the current sync-threaded paths passed (`tests/unit/test_scheduler_compaction_integration.py`, `tests/unit/test_manifest_atomic_write.py`), so this item does not appear to require further code changes at present.

7. Packaging/config drift

- Root dev dependencies do not fully match default pytest discovery.
- `tui/pyproject.toml` had missing tool config and still has placeholder package metadata.

Status:

- Fixed marker/config drift in `pytest.ini` by registering the additional custom markers used by default-discovered test trees (`e2e`, `slow`, `scenario`, `ollama`), eliminating unknown-mark warnings during collection.
- Fixed `tui/pyproject.toml` placeholder metadata by replacing the placeholder project name/description with TUI-specific metadata and declaring the small runtime dependency set the TUI actually uses.

8. TUI provider identity drift

- Different TUI modules match providers using different identity rules (`name` only vs `name` or `type`).

Status:

- In progress: normalizing TUI provider identity around a shared provider `id` derived from `type`/`name`, starting with `tui/src/ui/settings.py`, settings-screen provider selects, palette provider actions, and slash provider/model selection.

9. `src/core/context/context_controller.py`

- Budget/accounting API does not clearly reflect actual enforcement behavior.

Status:

- Fixed in `src/core/context/context_controller.py` by recording the actual token usage from the latest `enforce_budget()` call and reporting status against the effective available snippet budget after prompt/history/overhead reservations.

## Large Module Hotspots

The following files remain major complexity hotspots and should be considered primary simplification targets after blocker bugs are fixed:

- `src/core/inference/llm_manager.py`
- `src/core/context/context_builder.py`
- `src/core/orchestration/inference_loop.py`
- `src/core/orchestration/orchestrator_bootstrap.py`
- `src/core/orchestration/tool_execution_pipeline.py`
- `src/server/app.py`
- `src/tools/todo_tools.py`
- `src/core/memory/sqlite_session_store.py`
- `tui/src/ui/app.py`
- `tui/src/ui/core_bridge.py`

## Cross-Cutting Simplification Opportunities

1. Centralize extracted helper ownership

- Create one source of truth for:
  - bash policy constants
  - file preview/read-before-write limits
  - atomic text/json persistence
  - tool schema generation
- Avoid partial extraction patterns where callers use old symbol names.

2. Collapse duplicate atomic persistence code

- JSON/text atomic write behavior is duplicated across tools, memory, inference, and TUI.
- This duplication is already producing real divergence and bugs.

3. Normalize orchestration contracts

- One graph entrypoint
- One history field
- One retry-key type
- One canonical provider-context update path

4. Finish the “thin facade” cleanup for coordinators

- Highest-value remaining targets:
  - `context_builder.py`
  - `inference_loop.py`
  - `orchestrator_bootstrap.py`
  - `server/app.py`
  - `todo_tools.py`

5. Unify permission semantics

- Pick one canonical decision source between `PermissionKind`, `PermissionLevel`, and tool config.
- The current split is already causing security-policy contradictions.

6. Centralize TUI/server provider and settings hydration

- There should be one canonical provider identity and one canonical settings-loading path shared by bridge, settings store, and app-level UI flows.

## Recommended Remediation Order

1. Fix compile/runtime blockers
   - `tool_execution_pipeline.py`
   - `_bash_exec.py`
   - `_file_io.py`
   - `_tool.py`
   - `distiller.py`

2. Re-run broad validation once blockers are removed
   - `python -m compileall src tui`
   - `.venv/bin/ruff check src tui`
   - broader targeted pytest subsets around tools/orchestration/server

3. Fix security/config inconsistencies
   - server admin-token coverage
   - network permission classification
   - toolset loader shim mismatch

4. Simplify remaining hotspot coordinators in small slices

## Notes

- This audit is based on static scanning and focused subsystem review, not full end-to-end runtime coverage of every user workflow.
- The highest-confidence findings are the compile errors, undefined symbol references, state contract drift, and auth/permission inconsistencies listed above.
