# Atomic Write Hardening — Session Summary

**Date:** 2026-04-25
**Goal:** Unify whole-file writes so readers never observe partially-written files.

---

## Changes Made

### Files Modified (13)

| File | Change |
|------|--------|
| `src/core/inference/llm_manager.py` | Added missing `shutil` import |
| `src/tools/subagent_tools.py` | Replaced 2 direct `write_text` with `_atomic_write_json` |
| `src/core/orchestration/orchestrator_helpers.py` | Added full mkstemp→replace fallback for `timings_path` |
| `src/core/orchestration/graph/nodes/planning_node.py` | Replaced direct fallback with mkstemp→replace |
| `src/core/orchestration/dag_parser.py` | Added atomic + mkstemp fallback for `todo.json` |

### Already Compliant (Verified)

- `src/core/orchestration/agent_types.py`
- `src/core/orchestration/rollback_manager.py`
- `src/core/orchestration/session_lifecycle.py`
- `src/core/orchestration/cross_session_bus.py`
- `src/core/orchestration/permission_policy.py`
- `src/core/memory/distiller.py`
- `src/core/memory/advanced_features.py`
- `src/core/indexing/symbol_graph.py`
- `src/core/indexing/repo_indexer.py`
- `src/tools/_file_io.py`
- `src/tools/state_tools.py`
- `src/core/settings/controller.py`
- `src/core/io_utils.py` (canonical)

---

## Pattern Applied

1. **Try** `atomic_write_json(target, obj, logger)` (call-time import)
2. **If False/unavailable**, use mkstemp fallback:
   ```python
   fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
   with os.fdopen(fd, "w") as f:
       json.dump(obj, f)
       f.flush()
       os.fsync(f.fileno())
   os.replace(tmp, target)  # or shutil.move
   ```
3. **Final fallback**: `Path.write_text(...)` only if mkstemp fails (logged)

---

## Remaining write_text Sites (10)

All are **final fallbacks** after atomic + mkstemp attempts:

- `advanced_features.py` (3) — trajectories, smells, review
- `repo_analysis_tools.py` (1)
- `ollama_adapter.py` (1)
- `llm_manager.py` (1)
- `planning_node.py`, `orchestrator_helpers.py`, `dag_parser.py` (4)

Acceptable as they're last-resort after primary methods fail.

---

## Verification

- All files compile: `python -m py_compile` ✓
- Tests pass: `test_github_copilot_adapter.py`, `test_manifest_atomic_write.py`, `test_publish_active_config_impl.py`, `test_session_manager.py` ✓