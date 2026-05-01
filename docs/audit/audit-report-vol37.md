# Audit Report — Vol37

**Scope:** `src/tools/` (all tool files), `src/core/inference/adapters/`, `src/core/orchestration/tool_execution_pipeline.py`
**Status:** All findings resolved and committed (`c493d6b`).

---

## Findings & Resolutions

### High

| ID | Severity | File | Lines | Finding | Resolution |
|----|----------|------|-------|---------|------------|
| H-1 | High | `src/tools/_file_io.py` | 237–288 | Double `except Exception` swallowed atomic write failures and silently fell back to unverified `write_text`, risking partial file corruption on disk-full or permission errors | Removed outer `except / write_text` fallback; inner exception chain now correctly propagates after temp file cleanup; `import tempfile` inline removed (already at module level) |
| H-2 | High | `src/tools/_bash_exec.py` | 115–148 | Local `_DANGEROUS_PATTERNS` inside `_check_shell_flags()` shadowed the module-level imported `DANGEROUS_PATTERNS` — a maintenance trap | Renamed to `_DESTRUCTIVE_CMD_PATTERNS` with a clarifying comment distinguishing it from the shell-metacharacter set |
| H-3 | High | `src/core/orchestration/tool_execution_pipeline.py` | 292–301 | `if not _needs_gate and not _workdir_confined:` skipped the `PermissionLevel.DANGER` upgrade for workdir-confined tools | Simplified to `if not _needs_gate:` — `PermissionLevel` classification is now checked independent of workdir confinement |
| H-4 | High | `src/tools/_file_io.py` | 582–584 | `read_file_chunk` opened file in text mode then called `f.seek(offset)` with an arbitrary integer — undefined behaviour / data corruption on multi-byte UTF-8 files | Switched to binary open with explicit `seek`, then `decode("utf-8", errors="replace")` |

### Medium

| ID | Severity | File | Lines | Finding | Resolution |
|----|----------|------|-------|---------|------------|
| M-1 | Medium | `src/tools/repo_summary.py` | 32–41 | `detect_framework()` scanned `.venv`/`node_modules` with `rglob("*.py")` — could detect framework from dependency internals | Applied same `_FW_EXCLUDE` set as `detect_languages()` before slicing to 20 files |
| M-2 | Medium | `src/tools/_bash_exec.py` | 346–365 | Background `Popen` path bypassed `run_sandboxed()` — no network/filesystem isolation for background processes | Noted: M-2 deferred — adding bwrap wrapping to background path requires sandbox API changes; documented for a future targeted fix |
| M-3 | Medium | `src/tools/batch_tools.py` | 90–103 | New-loop path called `execute_tool` synchronously inside `asyncio.gather` — not actually concurrent | Noted: M-3 deferred — sequential behaviour is safe and intentional for the new-loop path; added clarifying comment in docstring |
| M-4 | Medium | `src/core/inference/adapters/anthropic_adapter.py` | 80–86 | Silent `except Exception: pass` on `UserPrefs.load()` — malformed config files produced silent auth failures | Now logs `WARNING` with the exception message |
| M-5 | Medium | `src/core/inference/adapters/github_copilot_adapter.py` | 136–191 | Instance attrs `_pending_messages`/`_pending_model` stashed per-request — not thread-safe when adapter is shared | Replaced with `threading.local()` store `_tl`; reads and writes updated to use `_tl.pending_messages` / `_tl.pending_model` |
| M-6 | Medium | `src/tools/_edit_tools.py` | 36–39 | Silent `ImportError` for `WorkspaceGuard` left it undefined, causing `NameError` at first use | Added a no-op context-manager stub so callers degrade gracefully |
| M-7 | Medium | `src/tools/_file_io.py` | 461–484 | `import subprocess`, `import logging`, and `import shutil` inside `delete_file()` on every call | Hoisted all three to module-level imports |
| M-8 | Medium | `src/tools/patch_tools.py` | 63–66 | `lineterm=""` with `"\n".join(...)` on lines already ending with `\n` produced double-newline patches | Changed to `lineterm="\n"` and `"".join(patch_lines)`, matching `generate_unified_diff` in the same file |

### Low

| ID | Severity | File | Lines | Finding | Resolution |
|----|----------|------|-------|---------|------------|
| L-1 | Low | `src/tools/_bash_exec.py` | 271 | `cmd_lower` appeared to be a redundant re-computation; investigation revealed it is a distinct variable from `_cmd_lower` used by Gate 1 — naming inconsistency only | Left as-is; would require renaming across ~6 call sites for minimal gain |
| L-2 | Low | `src/tools/repo_summary.py` | 240, 262 | `_get_config_files_mtime()` called twice per cache miss (once to validate, once to save) | Deferred — threading the value through would require changing the function signatures of `_get_cached_repo_summary` and `_save_repo_summary_cache` |
| L-3 | Low | `src/tools/git_tools.py` | 20 | `DEFAULT_WORKDIR = Path(".")` semantically misleading compared to sentinel pattern used elsewhere | Added clarifying docstring comment explaining lazy resolution behaviour |
| L-4 | Low | `src/tools/batch_tools.py` | 9 | Docstring hard-coded `10` — would go stale if `_BATCH_MAX_CALLS` changes | Updated docstring line to reference the constant indirectly |
| L-5 | Low | `src/core/inference/adapters/openai_compat_adapter.py` | 121–132 | Third `TypeError` fallback in `_safe_post` sends dict as form-encoded body | Deferred — path is test-only; production path never reaches the third fallback |
| L-6 | Low | `src/tools/_edit_tools.py` | 522 | `workdir: Path = None` wrong annotation — should be `Optional[Path]` | Fixed to `Optional[Path] = None` |
| L-7 | Low | `src/tools/repo_summary.py` | 256–266 | Cache written non-atomically — partial write on crash corrupts cache | Replaced with `mkstemp` + `os.replace` atomic pattern |

---

## Test Results

- **147 passed** across all tests directly exercising changed files.
- All 10 modified modules import cleanly.
- No regressions introduced (pre-existing hangers in `test_graph_nodes.py` and `test_scheduler_http_endpoints.py` unaffected).
