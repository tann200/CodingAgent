# Capability Audit Report

This report outlines the results of a programmatic audit of the coding agent's capabilities, based on the criteria in `docs/tool_optimization.md`.

## Phase 1: Tool Capability Audit

### Capability Matrix

| Capability | Tool Name | Status | Weakness / Notes |
|---|---|---|---|
| Workspace Exploration | `list_files` | **PRESENT** | The `list_dir` function exists in `src/tools/file_tools.py` and is registered as `list_files` in the orchestrator example registry. |
| File Reading | `read_file` | **PRESENT** | Two aliases exist (`read_file`, `fs.read`), which is redundant and should be rationalized but both are functional. |
| Incremental File Reading | `read_file_chunk`| **PRESENT** | Implemented in `src/tools/file_tools.py` as `read_file_chunk` (supports `offset` and `limit`). Recommended to prefer this for large files. |
| File Creation / Writing | `write_file` | **PRESENT** | Two aliases exist (`write_file`, `fs.write`), which is redundant. |
| Targeted Edits | `edit_file` | **PRESENT** | Implemented in `src/tools/file_tools.py` and registered as `edit_file` (surgical old->new string replacement). |
| Pattern Search | `grep` | **PRESENT** | Implemented in `src/tools/system_tools.py` as a thin wrapper around system `grep`. Note: relies on system `grep` binary being available. |
| Repository Summary | `summarize_structure` | **MISSING** | No high-level workspace summary tool exists yet. |
| Change Tracking | `get_git_diff` | **PRESENT** | Implemented in `src/tools/system_tools.py` as `get_git_diff` calling `git diff`. |

### Recommended Actions

1.  **Rationalize aliases**: De-duplicate `read_file`/`fs.read` and `write_file`/`fs.write` to a single canonical name in the registry (low effort).
2.  **Prefer `read_file_chunk` for large files**: Update callers and documentation to use `read_file_chunk` when file size or line count exceeds a safe threshold (e.g., 1000 lines or N tokens).
3.  **Add `summarize_structure` tool**: Implement a small workspace summarizer that returns file counts/size and top-level structure to help model planning (medium effort).
4.  **Document `grep` dependency**: Note that `grep` is a system dependency; consider a pure-Python fallback for environments without `grep` (low effort).
5.  **Add comprehensive tests**: Add unit tests that exercise `read_file_chunk`, `edit_file`, and `get_git_diff` in a temporary workdir to prevent accidental repository changes.

---

I will now proceed with updating the main audit report to reflect implemented items and then run the test suite.
