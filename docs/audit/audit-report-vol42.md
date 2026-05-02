# Code Quality Audit — Volume 42

**Date:** 2026-05-02
**Files audited:** 15
**Findings:** 42

---

## Summary Table

| ID | Severity | Category | File |
|----|----------|----------|------|
| F-01 | High | InlineImport / MissingImport | context_builder.py |
| F-02 | High | InlineImport | context_builder.py |
| F-03 | High | InlineImport | context_builder.py |
| F-04 | High | InlineImport | event_bus.py |
| F-05 | High | InlineImport | event_bus.py |
| F-06 | High | InlineImport | todo_tools.py |
| F-07 | High | InlineImport | repo_indexer.py |
| F-08 | High | DuplicateCode | repo_indexer.py / symbol_graph.py / vector_store.py |
| F-09 | High | DuplicateCode | todo_tools.py |
| F-10 | Medium | InlineImport | context_builder.py |
| F-11 | Medium | InlineImport | context_builder.py |
| F-12 | Medium | DeadCode | agent_brain.py |
| F-13 | Medium | DeadCode / Stub | commands.py |
| F-14 | Medium | DeadCode | commands.py |
| F-15 | Medium | DeadCode | context_controller.py |
| F-16 | Medium | MagicLiteral | context_builder.py |
| F-17 | Medium | MagicLiteral | context_builder.py |
| F-18 | Medium | DuplicateLiteral | repo_indexer.py |
| F-19 | Medium | DuplicateLiteral | context_builder.py |
| F-20 | Medium | DuplicateLiteral | symbol_graph.py |
| F-21 | Medium | InlineImport | dag_parser.py |
| F-22 | Medium | InlineImport | dag_parser.py |
| F-23 | Medium | InlineImport | patch_tools.py |
| F-24 | Medium | InlineImport | batch_tools.py |
| F-25 | Medium | InconsistentPattern | agent_brain.py |
| F-26 | Medium | InconsistentPattern | commands.py |
| F-27 | Medium | InconsistentPattern | repo_indexer.py / symbol_graph.py |
| F-28 | Medium | UnnecessaryComplexity | todo_tools.py |
| F-29 | Medium | UnnecessaryComplexity | context_builder.py |
| F-30 | Medium | UnnecessaryComplexity | todo_tools.py |
| F-31 | Medium | DuplicateCode | todo_tools.py |
| F-32 | Low | DeadCode | context_builder.py |
| F-33 | Low | DeadCode | context_builder.py |
| F-34 | Low | DeadCode | agent_brain.py |
| F-35 | Low | DeadCode | context_controller.py |
| F-36 | Low | MagicLiteral | context_builder.py |
| F-37 | Low | MagicLiteral | context_builder.py |
| F-38 | Low | InconsistentPattern | commands.py |
| F-39 | Low | InconsistentPattern | symbol_graph.py |
| F-40 | Low | InconsistentPattern | todo_tools.py |
| F-41 | Low | MagicLiteral | todo_tools.py |
| F-42 | Info | StyleInconsistency | context_builder.py |

**Totals:** 9 High · 22 Medium · 10 Low · 1 Info

---

## Findings

### F-01 — Missing `import re` in `context_builder.py`

**Severity:** High
**Category:** InlineImport / MissingImport
**File:** `src/core/context/context_builder.py:529`

`re.search(pattern, active_model)` is called inside `_select_prompt_partial()` but `re` is never imported at module level. This causes a `NameError` at runtime on every non-trivial LLM call that triggers a model-ID partial lookup.

**Fix:** Add `import re` to the module-level imports block.

---

### F-02 — Pervasive inline imports in `context_builder.py`

**Severity:** High
**Category:** InlineImport
**File:** `src/core/context/context_builder.py` (lines 38–42, 128–132, 139, 198, 572–575, 613, 735–749, 767–770, 789–793, 825–829)

At least 10 separate `try: from src.core.inference.*` and `from src.tools.*` blocks scattered through instance/static methods. Several modules (e.g., `ModelTier`) are imported repeatedly across `_prune_tools`, `_render_tools_for_tier`, and `_build_static_system_prefix`.

**Fix:** Hoist stable imports to module level with a single guarded `try/except ImportError`. Reserve inline imports only for genuine circular-import situations.

---

### F-03 — `from datetime import date` inside `_today_iso()`

**Severity:** High
**Category:** InlineImport
**File:** `src/core/context/context_builder.py:77–79`

`from datetime import date` imported inside `_today_iso()`, which is called on every `build_prompt` invocation. `datetime` is stdlib with no circular-import risk.

**Fix:** Move `from datetime import date` to the module-level imports.

---

### F-04 — Inline stdlib imports inside `run_with_correlation()`

**Severity:** High
**Category:** InlineImport
**File:** `src/core/orchestration/event_bus.py:83–86`

`import contextvars`, `import functools`, `import inspect`, and `import asyncio` all imported inside `run_with_correlation()`. `asyncio` is already at module level (line 27).

**Fix:** Hoist `contextvars`, `functools`, `inspect` to module level; remove redundant inline `import asyncio`.

---

### F-05 — Inline imports in `_get_shared_executor()`

**Severity:** High
**Category:** InlineImport
**File:** `src/core/orchestration/event_bus.py:389–393`

`from concurrent.futures import ThreadPoolExecutor` and `import atexit` imported inside `_get_shared_executor()`, called on every `run_with_correlation()` invocation.

**Fix:** Hoist both imports to module level.

---

### F-06 — Multiple inline stdlib imports in `todo_tools.py`

**Severity:** High
**Category:** InlineImport
**File:** `src/tools/todo_tools.py` (lines 145–146, 279–280, 311, 329, 362)

`import sys`, `import subprocess`, `import re`, `import socket`, `import traceback`, `import errno` imported inside `_is_network_filesystem()` and `_FileLock.__enter__()`. `import re` appears in multiple separate nested `try` blocks.

**Fix:** Hoist all six to module level; remove duplicated inline occurrences.

---

### F-07 — Inline `import traceback` in `repo_indexer.py`

**Severity:** High
**Category:** InlineImport
**File:** `src/core/indexing/repo_indexer.py:233–244, 269–270`

`import traceback` imported twice inside nested `except` blocks in `_save_index_metadata()`.

**Fix:** Add `import traceback` to module-level imports and remove all inline occurrences.

---

### F-08 — Triplicated atomic-write fallback ladder across indexing modules

**Severity:** High
**Category:** DuplicateCode
**File:** `src/core/indexing/repo_indexer.py:251–292`; `src/core/indexing/symbol_graph.py:168–211`; `src/core/indexing/vector_store.py:148–199`

Identical three-tier atomic-write fallback (`atomic_write_json` → `mkstemp+os.replace` → `write_text`) copy-pasted verbatim across all three indexing files (~130 lines total). Bug fixes must be applied three times.

**Fix:** Replace the inner fallback ladder in each file with a direct call to `src.core.io_utils.atomic_write_json`; the outer `try/except` at each call site is sufficient.

---

### F-09 — Duplicate ContextBuilder invalidation in `todo_tools.py`

**Severity:** High
**Category:** DuplicateCode
**File:** `src/tools/todo_tools.py:731–745` (`_notify_rbw_after_write`) and `lines 779–792` (`notify_rbw`)

ContextBuilder invalidation logic (`ContextBuilder.invalidate_path` → fallback `clear_cache`) is duplicated verbatim in both functions.

**Fix:** Extract `_invalidate_context_builder(path: str) -> None` helper and call it from both sites.

---

### F-10 — Inline `VectorStore` import in `inject_prior_session_memories()`

**Severity:** Medium
**Category:** InlineImport
**File:** `src/core/context/context_builder.py:440–443`

`from src.core.indexing.vector_store import VectorStore` imported inside a method called on every `perception_node` turn (round 0).

**Fix:** Import at module level inside a `try/except ImportError` guard.

---

### F-11 — Inline instruction-file imports in `_build_static_system_prefix()`

**Severity:** Medium
**Category:** InlineImport
**File:** `src/core/context/context_builder.py:767–770, 789–793`

`from src.core.context.instruction_files import ...` and `from src.core.orchestration.instruction_loader import ...` imported inside a method called on every LLM turn.

**Fix:** Move both imports to module level with `try/except` guards.

---

### F-12 — Dead backward-compat functions in `agent_brain.py`

**Severity:** Medium
**Category:** DeadCode
**File:** `src/core/orchestration/agent_brain.py:275–326`

`_repo_root_old()`, `_agent_brain_dir_old()`, and `_compile_system_prompt()` are labelled "Backward compatibility". `_compile_system_prompt()` duplicates `AgentBrainManager.compile_system_prompt()` but with a known-broken `<operating_principles>` re-injection that was explicitly removed to avoid token doubling. The `_old` path functions reference a non-existent `agent-brain` directory.

**Fix:** Remove all three functions. Update any callers to use `AgentBrainManager.compile_system_prompt()`.

---

### F-13 — `SkillsCommand.execute()` is an unimplemented stub

**Severity:** Medium
**Category:** DeadCode / Stub
**File:** `src/core/orchestration/commands.py:52–57`

Always returns hardcoded `"- (skill listing not implemented)"`. `AgentBrainManager.list_skills_summary()` exists and is functional.

**Fix:** Implement using `get_agent_brain_manager().list_skills_summary()`.

---

### F-14 — Unreachable branch in `commands.py`

**Severity:** Medium
**Category:** DeadCode
**File:** `src/core/orchestration/commands.py:144–145`

`snapshot_id = arg if arg else None` — the `else None` branch is unreachable because the function returns early at line 129 when `arg` is empty.

**Fix:** Replace with `snapshot_id = arg`.

---

### F-15 — Redundant monkey-patch in `context_controller.py`

**Severity:** Medium
**Category:** DeadCode
**File:** `src/core/context/context_controller.py:232`

`ContextController.get_relevant_snippets = ContextController.extract_relevant_snippets` duplicates the proper method alias already defined at lines 215–219.

**Fix:** Remove the monkey-patch on line 232.

---

### F-16 — Magic literal `2100` (token overhead reserve) in `context_builder.py`

**Severity:** Medium
**Category:** MagicLiteral
**File:** `src/core/context/context_builder.py:942`

The inline integer `2100` represents a system overhead token reserve. If prompt structure changes, it silently becomes wrong.

**Fix:** Define `_SYSTEM_OVERHEAD_TOKENS: int = 2100` at module level.

---

### F-17 — `_CORE_NAMES` set rebuilt on every `_prune_tools()` call

**Severity:** Medium
**Category:** MagicLiteral
**File:** `src/core/context/context_builder.py:583–595`

10-element set `_CORE_NAMES` defined as a local inside a `@staticmethod` called on every LLM turn.

**Fix:** Hoist to `_CORE_TOOL_NAMES: frozenset[str] = frozenset({...})` at module level.

---

### F-18 — `".codingAgent"` hardcoded in 5 places in `repo_indexer.py`

**Severity:** Medium
**Category:** DuplicateLiteral
**File:** `src/core/indexing/repo_indexer.py` (lines 215, 227, 313, 465, 531)

Raw `".codingAgent"` path constructions used in 5 methods instead of `agent_context_path(base_path)` from `tools_config`. Silently breaks custom context-dir configurations.

**Fix:** Replace all raw `".codingAgent"` path constructions with `agent_context_path(base_path)`.

---

### F-19 — Duplicated `_get_ctx_name()` boilerplate in `context_builder.py`

**Severity:** Medium
**Category:** DuplicateLiteral
**File:** `src/core/context/context_builder.py:202–207, 404`

`try: get_context_dir_name() except: ".codingAgent"` written separately at two sites.

**Fix:** Extract `_get_ctx_name() -> str` module-level helper and call from both sites.

---

### F-20 — Duplicate `.tsx`/`.jsx` regex patterns in `symbol_graph.py`

**Severity:** Medium
**Category:** DuplicateLiteral
**File:** `src/core/indexing/symbol_graph.py:44–63`

`.tsx` and `.jsx` pattern sets are byte-for-byte copies of `.ts` and `.js` (noted by comments `# same as .ts`, `# same as .js`).

**Fix:** `_LANG_PATTERNS[".tsx"] = _LANG_PATTERNS[".ts"]`; `_LANG_PATTERNS[".jsx"] = _LANG_PATTERNS[".js"]`.

---

### F-21 — Inline `from pathlib import Path` in three `dag_parser.py` methods

**Severity:** Medium
**Category:** InlineImport
**File:** `src/core/orchestration/dag_parser.py` (lines 76–78, 114–116, 229–230, 256–259)

`from pathlib import Path` imported inside `from_todo_json`, `from_todo_markdown`, and `sync_to_files` (twice). Stdlib with no side effects.

**Fix:** Hoist to module-level imports.

---

### F-22 — Inline `todo_tools` imports in `dag_parser.py`

**Severity:** Medium
**Category:** InlineImport
**File:** `src/core/orchestration/dag_parser.py:84–89, 125–128`

`from src.tools.todo_tools import _load_todo_json` and `from src.tools.todo_tools import _lock_path, _FileLock` imported inside classmethods.

**Fix:** Hoist to module level with a single `try/except ImportError` guard.

---

### F-23 — Inline `file_tools` import in `patch_tools.py`

**Severity:** Medium
**Category:** InlineImport
**File:** `src/tools/patch_tools.py:83, 99`

`from src.tools import file_tools` imported inside two separate `@tool` functions with no circular-import risk.

**Fix:** Hoist to module level.

---

### F-24 — Inline stdlib imports in `batch_tools.py`

**Severity:** Medium
**Category:** InlineImport
**File:** `src/tools/batch_tools.py:113–118`

`import concurrent.futures` and `import contextvars as _cv` imported inside an async-context branch of `batch()`.

**Fix:** Hoist both to module-level imports.

---

### F-25 — Inline `import yaml` inside hot-path function in `agent_brain.py`

**Severity:** Medium
**Category:** InconsistentPattern
**File:** `src/core/orchestration/agent_brain.py:34–45`

`import yaml` is inside `_parse_front_matter()`, called once per skill/role at startup. If `yaml` is available, it is re-imported on every call due to no module-level caching.

**Fix:** `try: import yaml except ImportError: yaml = None` at module level; guard call with `if yaml is not None`.

---

### F-26 — Inline `git_diff` import in `DiffCommand.execute()`

**Severity:** Medium
**Category:** InconsistentPattern
**File:** `src/core/orchestration/commands.py:172`

`from src.tools.git_tools import git_diff` imported inline while all other commands use the orchestrator's tool registry.

**Fix:** Hoist to module level or use the tool registry for consistency.

---

### F-27 — Inconsistent file encoding handling across indexing modules

**Severity:** Medium
**Category:** InconsistentPattern
**File:** `src/core/indexing/repo_indexer.py:170–177, 218, 479, 491`; `src/core/indexing/symbol_graph.py:295`

`open()` calls at lines 218, 479, 491 in `repo_indexer.py` omit `encoding="utf-8"`, while the rest use it explicitly. Risks `UnicodeDecodeError` on non-ASCII sources on Windows.

**Fix:** Standardise on `path.read_text(encoding="utf-8")` / `open(..., encoding="utf-8")` throughout.

---

### F-28 — `_FileLock.__enter__()` is 246 lines with deep nesting

**Severity:** Medium
**Category:** UnnecessaryComplexity
**File:** `src/tools/todo_tools.py:241–486`

12+ nesting levels; stale-lock reclaim logic duplicated for "pid present" and "no pid" cases. Inline imports scattered throughout.

**Fix:** Extract `_reclaim_stale_lock()` and `_parse_lockfile()` private helpers; move inline imports to module level. Method should shrink to ~60 lines.

---

### F-29 — `_build_static_system_prefix()` is 262 lines with 6+ nesting levels

**Severity:** Medium
**Category:** UnnecessaryComplexity
**File:** `src/core/context/context_builder.py:658–920`

Handles 8+ distinct concerns in one method. Multiple nested `try/except` blocks each wrapping a single import line.

**Fix:** Extract `_build_model_constraints_block()`, `_build_output_format_block()`, `_build_instruction_files_block()` as private helpers.

---

### F-30 — `manage_todo()` is 473 lines; range-check duplicated 6 times

**Severity:** Medium
**Category:** UnnecessaryComplexity
**File:** `src/tools/todo_tools.py:804–1277`

`step_id = int(step_id)` coercion and bounds check `if step_id < 0 or step_id >= len(current)` appear 6 times across 9 `elif` branches.

**Fix:** Extract `_coerce_step_id(step_id, current) -> Tuple[int, Optional[Dict]]` returning coerced id + error dict; call from each branch.

---

### F-31 — `_notify_rbw_after_write` and `notify_rbw` are near-duplicates

**Severity:** Medium
**Category:** DuplicateCode
**File:** `src/tools/todo_tools.py:701–801`

Both functions perform identical path resolution, ContextBuilder invalidation, cache clear, and metric increment. `notify_rbw` falls through to `_notify_rbw_after_write` anyway (line 797).

**Fix:** Consolidate into single `notify_rbw(workdir, orchestrator=None)`; remove `_notify_rbw_after_write`.

---

### F-32 — Dead `_build_system_message()` in `context_builder.py`

**Severity:** Low
**Category:** DeadCode
**File:** `src/core/context/context_builder.py:1372–1417`

Never called anywhere in the file; `build_prompt()` appends directly to `built_messages` without using this helper.

**Fix:** Remove or add a comment explaining the intended future use.

---

### F-33 — `_last_token_count` never updated by `build_prompt()`

**Severity:** Low
**Category:** DeadCode
**File:** `src/core/context/context_builder.py:1419–1424`

`get_token_usage()` references `_last_token_count` which is never updated inside `build_prompt()`. External callers must remember to call `update_token_count()` — error-prone contract.

**Fix:** Update `_last_token_count` inside `build_prompt()` after assembly, or document the external caller contract explicitly.

---

### F-34 — `get_role_with_topics()` is likely dead code

**Severity:** Low
**Category:** DeadCode
**File:** `src/core/orchestration/agent_brain.py:202–215`

Returns P2P topic strings for four hardcoded role names; the P2P messaging system uses `event_bus.py`, not this method. The topic map is rebuilt on every call.

**Fix:** Verify callers; if none, remove. If kept, hoist topic map to a module-level constant.

---

### F-35 — `add_p2p_context` / `_add_truncated` appear unused

**Severity:** Low
**Category:** DeadCode
**File:** `src/core/context/context_controller.py:170–202`

`add_p2p_context()` and `_add_truncated()` manage a `_context_budget`/`_used_tokens` budget but appear to have no callers in the current orchestration flow.

**Fix:** Search for callers; if none, remove both methods and the associated `__init__` attributes.

---

### F-36 — Stale-after-turns prune note hardcodes `3`

**Severity:** Low
**Category:** MagicLiteral
**File:** `src/core/context/context_builder.py:1285`

`"Full output pruned (stale — >3 turns ago)."` hardcodes the threshold while the actual parameter `stale_after_turns` defaults to `3`. If the default changes, the note is silently wrong.

**Fix:** `f"Full output pruned (stale — >{stale_after_turns} turns ago). ..."`

---

### F-37 — Magic injection thresholds `60`, `20`, `10` in `context_builder.py`

**Severity:** Low
**Category:** MagicLiteral
**File:** `src/core/context/context_builder.py:1030, 1041, 1092`

`len(ts_content) > 60`, `len(todo_content) > 20`, `len(prefs_content) > 10` are undocumented magic integers.

**Fix:** Define `_MIN_TASK_STATE_CHARS = 60`, `_MIN_TODO_CHARS = 20`, `_MIN_PREFS_CHARS = 10` at module level.

---

### F-38 — `get_command_registry()` singleton is not thread-safe

**Severity:** Low
**Category:** InconsistentPattern
**File:** `src/core/orchestration/commands.py:338–349`

Race condition when two threads call `get_command_registry()` simultaneously with `_command_registry is None`. Inconsistent with `get_event_bus()` which uses a lock.

**Fix:** Add `_registry_lock = threading.Lock()` and double-checked locking pattern.

---

### F-39 — f-string logger calls in `symbol_graph.py`

**Severity:** Low
**Category:** InconsistentPattern
**File:** `src/core/indexing/symbol_graph.py` (lines 289, 350, 363, 365, 493, 503)

`logger.warning(f"Failed to parse {path}: {e}")` — f-strings evaluate eagerly even when the log level is suppressed.

**Fix:** Use `logger.warning("Failed to parse %s: %s", path, e)` style throughout.

---

### F-40 — f-string logger calls in `manage_todo()`

**Severity:** Low
**Category:** InconsistentPattern
**File:** `src/tools/todo_tools.py` (lines 949, 1001–1002, 1085, 1276)

Same issue as F-39; inconsistent with `%`-style formatting used in the module-level helpers in the same file.

**Fix:** Convert to `%`-style lazy formatting throughout `manage_todo`.

---

### F-41 — `_BACKUP_KEEP` defined as local variable inside `_save_todo()`

**Severity:** Low
**Category:** MagicLiteral
**File:** `src/tools/todo_tools.py:559`

`_BACKUP_KEEP = 5` defined as a local variable inside `_save_todo()` rather than as a module-level constant, making it invisible without reading the function body.

**Fix:** Move `_BACKUP_KEEP: int = 5` to module level near the other module-level constants.

---

### F-42 — Lone walrus operator use in `context_builder.py`

**Severity:** Info
**Category:** StyleInconsistency
**File:** `src/core/context/context_builder.py:807–815`

Walrus operator (`:=`) used in one list comprehension — the only usage across all 15 audited files. Fine technically, but may surprise contributors.

**Fix:** Add a comment `# walrus: bind skill content and filter empties in one pass`, or rewrite as a conventional filter+map for consistency.

---

## Priority Order for Fixes

1. **F-01** — Silent `NameError` bug in production (missing `import re`)
2. **F-08** — 130 lines of triplicated atomic-write ladder
3. **F-04, F-05** — stdlib inline imports in hot-path `event_bus.py`
4. **F-06** — Multiple inline imports in `todo_tools._FileLock`
5. **F-03, F-07, F-21, F-23, F-24** — trivial one-line hoist fixes
6. **F-20** — two-line fix for duplicate `.tsx`/`.jsx` regex patterns
7. **F-18** — hardcoded `".codingAgent"` in 5 places (silent config-dir bug)
8. **F-12** — dead backward-compat functions with known broken prompt injection
9. **F-15** — redundant monkey-patch (one-line removal)
10. **F-25** — `import yaml` re-imported on every front-matter parse call
11. **F-09, F-31** — DuplicateCode in `todo_tools` notify helpers
12. **F-28, F-29, F-30** — UnnecessaryComplexity refactors
13. Medium/Low findings in order as time permits
