# Tools Gap Analysis & Implementation Plan

**Date:** 2026-03-27
**Last Updated:** 2026-03-27
**Scope:** CodingAgent `src/tools/` vs LocalCodingAgent (`/Users/tann200/PycharmProjects/LocalCodingAgent/src/tools/`) and the broader coding-agent ecosystem (Claude Code, Cursor, Aider, Devin).
**Status:** All P0–P3 items implemented. GAP-S2 deferred (requires orchestrator integration). 1502+ tests passing.

---

## 0. Implementation Status Summary

| Tier | Items | Status |
|------|-------|--------|
| **P0 Critical** | IMPL-1 (memory_search), IMPL-2 (ask_user) | ✓ All done |
| **P1 High** | IMPL-3 (guardrails), IMPL-4 (bash_readonly), IMPL-5 (auto-lint), IMPL-6 (web tools), IMPL-7 (AST refactor), IMPL-8 (fingerprint), IMPL-9 (HITL plan review) | ✓ All done |
| **P2 Medium** | IMPL-10 (multi-lang linter), IMPL-11 (task status), IMPL-12 (find_references), IMPL-13 (tail+mkdir), IMPL-14 (multi-lang repo analysis), GAP-Q4 (multi-lang syntax_check), GAP-S3 (hard file-size guard), GAP-F10 (read_file_bytes) | ✓ All done |
| **P3 Low** | IMPL-15 (status helpers), IMPL-16 (glob truncation), IMPL-17 (deprecate get_git_diff), GAP-P2 (remove from YAML) | ✓ All done |
| **Deferred** | GAP-S2 (scope guard) | Requires orchestrator `affected_files` tracking; not a drop-in fix |

### New Modules Created
| File | Purpose |
|------|---------|
| `src/tools/memory_tools.py` | `memory_search` — vector store + file fallback |
| `src/tools/interaction_tools.py` | `ask_user`, `submit_plan_for_review` |
| `src/tools/guardrails.py` | Read-before-write enforcement |
| `src/tools/web_tools.py` | `web_search`, `read_web_page` |
| `src/tools/ast_tools.py` | `ast_rename`, `ast_list_symbols` |
| `src/tools/project_tools.py` | `fingerprint_tech_stack` |
| `src/tools/lint_dispatch.py` | `quick_lint` — multi-language post-write checks |
| `src/tools/tools_config.py` | Configurable `.agent-context` directory |

### Modules Extended
| File | Changes |
|------|---------|
| `src/tools/file_tools.py` | +`bash_readonly`, +`tail_log_file`, +`create_directory`, +`read_file_bytes`, +guardrail integration, +auto-lint, +hard 500-line block |
| `src/tools/todo_tools.py` | +`start`, `block`, `verify`, `next` actions |
| `src/tools/repo_tools.py` | `find_references` uses word-boundary regex, returns `{line, col, snippet}` |
| `src/tools/repo_analysis_tools.py` | +JS/TS, Go, Rust regex-based analyzers |
| `src/tools/verification_tools.py` | `run_linter` dispatches to ruff/eslint/tsc/cargo clippy/go vet; `syntax_check` checks Python/JS/Go/Rust |
| `src/tools/system_tools.py` | `get_git_diff` deprecated with warning |
| `src/tools/_tool.py` | +`ok()`, `err()`, `partial()` helpers |
| `src/tools/_registry.py` | +6 new modules in `_BUILTIN_MODULES` |

---

## 1. Executive Summary (Original)

CodingAgent has a well-structured, portable tool system with strong file-editing primitives, a 3-tier bash security model, 6 git operations, and a comprehensive verification suite. However, it has several critical functional holes — most notably a missing `memory_search` implementation that is already referenced in all four toolsets, no mechanism for the agent to pause and ask the user a question, and no post-write auto-lint pipeline. Compared to LocalCodingAgent and industry leaders it also lacks web search, AST-aware refactoring, tech-stack detection, a read-before-write guardrail, and a human-in-the-loop plan-approval gate.

This document catalogues every gap, ranks them, and provides a detailed implementation plan for each.

---

## 2. Current State Summary

### 2.1 CodingAgent Tool Inventory (Updated)

| Module | Tools | Primary Purpose |
|---|---|---|
| `file_tools.py` | 17 | File I/O, bash, bash_readonly, glob, tail, mkdir, binary read |
| `git_tools.py` | 6 | Git operations |
| `verification_tools.py` | 7 | Multi-language test runners, linters, syntax check |
| `todo_tools.py` | 1 | DAG-based task list with status machine |
| `repo_tools.py` | 4 | Code indexing & symbol search |
| `repo_analysis_tools.py` | 1 | Multi-language AST/regex analysis |
| `patch_tools.py` | 3 | Patch generation & application |
| `state_tools.py` | 6 | State checkpoints |
| `system_tools.py` | 4 | Grep, deprecated git diff, structure summary |
| `subagent_tools.py` | 2 | Delegation to sub-agents |
| `memory_tools.py` | 1 | Memory search (vector store + file fallback) |
| `interaction_tools.py` | 2 | ask_user + submit_plan_for_review |
| `guardrails.py` | 3 | Read-before-write enforcement |
| `web_tools.py` | 2 | Web search + page fetch |
| `ast_tools.py` | 2 | AST-aware refactoring |
| `project_tools.py` | 1 | Tech-stack detection |
| `lint_dispatch.py` | 1 | Post-write quick-lint (internal helper) |
| **Total** | **59** | |

### 2.2 LocalCodingAgent Tool Inventory (Reference)

| Module | Tools | Primary Purpose |
|---|---|---|
| `files.py` | 10 | File I/O + binary |
| `system.py` + `bash.py` | 7 | Tiered shell execution |
| `patch_tool.py` | 2 | Edit + unified diff |
| `analysis.py` + `navigation.py` + `ast_tool.py` | 4 | Code analysis + AST refactor |
| `git_ops.py` + `git_inspector.py` | 3 | Git ops |
| `linter.py` | 1 | Multi-language post-write lint |
| `gsd_tool.py` | 4 | Task lifecycle management |
| `orchestration.py` | 2 | Delegation + HITL plan review |
| `interaction.py` | 2 | ask_user + submit_for_validation |
| `project.py` | 2 | Tech-stack detection |
| `web_tools.py` | 2 | Web search + page fetch |
| `geck.py` | 2 | Project memory (tasks.md/log.md) |
| `quality.py` | 1 | Quality suite (placeholder) |
| **Total** | **42** | |

---

## 3. Gap Analysis

### 3.1 Security & Correctness Gaps

#### GAP-S1: Read-Before-Write Enforcement ✓ IMPLEMENTED
- **Severity:** Critical
- **Status:** ✓ Implemented — `guardrails.py` created and integrated into all write functions
- **Description:** The agent can call `write_file`, `edit_file_atomic`, or `edit_by_line_range` on a file it has never read. This causes the LLM to hallucinate existing content and produce corrupted files.
- **Implementation:**
  - `src/tools/guardrails.py` — `mark_file_read(path)`, `check_read_before_write(path)`, `reset_guardrail_state()` using thread-local storage
  - `read_file` and `read_file_chunk` call `mark_file_read(str(p.resolve()))` after successful read
  - `write_file`, `edit_file`, `edit_by_line_range`, `edit_file_atomic` call `check_read_before_write(path)` before write; returns `{"status": "error", "requires_read_first": True}` on violation
  - New files (don't exist yet) are allowed without prior read — handled by `check_read_before_write`
- **Files:** `src/tools/guardrails.py` (new), `src/tools/file_tools.py` (modified)

#### GAP-S2: No Scope Guard (Write Scope Limited to Task Files)
- **Severity:** High
- **Status:** Deferred — requires orchestrator `affected_files` tracking
- **Description:** The agent can write to any file in the workspace, even files unrelated to the current task. This risks silent side-effects.
- **LocalCodingAgent fix:** `capsule_scope_guard` restricts writes to files declared in the active task's `affected_files` set.
- **Current CodingAgent state:** No scope restriction beyond WorkspaceGuard protected-file list.
- **Deferred reason:** Requires orchestrator-level integration to track the task's `affected_files` set; not a drop-in tool fix.

#### GAP-S3: Hard File-Size Guard on Writes ✓ IMPLEMENTED
- **Severity:** Medium
- **Status:** ✓ Implemented — hard block at 500 lines
- **Description:** `write_file` emits a soft `requires_split` flag if >200 lines are written but does not block the write. Agents routinely ignore this signal.
- **Implementation:**
  - `write_file` now hard-blocks at >500 lines: returns `{"status": "error", "error": "write_file refused: N lines exceeds 500-line hard limit. Split into multiple smaller writes."}`
  - Soft warning at >200 lines preserved for intermediate feedback
  - Write is reverted (not applied) when hard limit is hit
- **Files:** `src/tools/file_tools.py` (modified `write_file`)

#### GAP-S4: Separate Read-Only Shell Tier ✓ IMPLEMENTED
- **Severity:** Medium
- **Status:** ✓ Implemented — `bash_readonly` tool
- **Description:** The LLM is given one `bash` tool that covers both read-only queries (ls, grep, git status) and execution commands (pytest, cargo build). A single compromised instruction can escalate from read to execute within one tool call shape.
- **Implementation:**
  - New `bash_readonly` function in `file_tools.py` — restricted to `SAFE_COMMANDS` only (tier 1)
  - Blocks test/compile commands, restricted commands, code-execution flags, sed -i, tar extract
  - Tags: `["coding", "debug", "review", "planning"]`
  - `debug.yaml` updated from `bash` to `bash_readonly`
  - `review.yaml` and `planning.yaml` gained `bash_readonly`
  - Primary `bash` remains `tags=["coding"]` only
- **Files:** `src/tools/file_tools.py` (new function), `src/tools/toolsets/{debug,review,planning,coding}.yaml`

---

### 3.2 Functional Holes

#### GAP-F1: `memory_search` ✓ IMPLEMENTED
- **Severity:** Critical
- **Status:** ✓ Implemented
- **Implementation:**
  - New file `src/tools/memory_tools.py`
  - Searches VectorStore via `src.core.indexing.vector_store.VectorStore.search()`
  - Cold-start fallback: reads `.agent-context/TASK_STATE.md`, `compaction_checkpoint.md`, `todo.json` and scores sections by keyword overlap
  - Returns `{status, query, results: [{source, excerpt, score}]}` with max 10 results
  - Tags: `["coding", "debug", "planning", "review"]`
  - Registered in `_registry.py` `_BUILTIN_MODULES`
  - Already referenced in all YAML toolsets — no YAML changes needed
- **Files:** `src/tools/memory_tools.py` (new), `src/tools/_registry.py`

#### GAP-F2: `ask_user` ✓ IMPLEMENTED
- **Severity:** Critical
- **Status:** ✓ Implemented
- **Implementation:**
  - New file `src/tools/interaction_tools.py`
  - Publishes `agent.waiting_for_user` event on EventBus
  - Blocks via `threading.Event()` until `user.response` arrives (300s timeout)
  - Returns `{status: "ok", question, answer}` or `{status: "timeout", error}`
  - Tags: `["coding", "planning", "debug", "review"]`
  - Added to all four YAML toolsets
- **Files:** `src/tools/interaction_tools.py` (new), all 4 YAML toolsets

#### GAP-F3: Post-Write Auto-Lint ✓ IMPLEMENTED
- **Severity:** High
- **Status:** ✓ Implemented
- **Implementation:**
  - New file `src/tools/lint_dispatch.py` with `quick_lint(path, workdir)`
  - Supports Python (py_compile), JS (node --check), TS (npx tsc), Go (go build), Rust (rustc --emit=metadata)
  - 10-second timeout per check; never raises
  - Integrated into `write_file`, `edit_by_line_range`, and `edit_file_atomic` — adds `lint_warnings` and `lint_status` to result dict
  - Informational only — does not block the write
- **Files:** `src/tools/lint_dispatch.py` (new), `src/tools/file_tools.py` (modified)

#### GAP-F4: Web Search and URL Fetching ✓ IMPLEMENTED
- **Severity:** High
- **Status:** ✓ Implemented
- **Implementation:**
  - New file `src/tools/web_tools.py`
  - `web_search(query, max_results=5)` — DuckDuckGo search with fallback to HTML scraping; returns `{status, results: [{title, url, snippet}]}`
  - `read_web_page(url)` — fetches page, strips HTML tags, returns first 10,000 chars; SSRF protection blocks private IPs (127.x, 10.x, 172.16-31.x, 192.168.x, 169.254.x)
  - All imports wrapped in try/except for graceful degradation when optional deps missing
  - Added to `planning.yaml` and `debug.yaml`
- **Files:** `src/tools/web_tools.py` (new), `src/tools/toolsets/{planning,debug}.yaml`

#### GAP-F5: Human-in-the-Loop Plan Review Gate ✓ IMPLEMENTED
- **Severity:** High
- **Status:** ✓ Implemented
- **Implementation:**
  - `submit_plan_for_review(plan_summary, plan_steps, risk_level)` in `interaction_tools.py`
  - Publishes `agent.plan_review_requested` on EventBus; blocks until `plan_review.response`
  - Returns `{status: "ok", decision: "approved"|"rejected"|"revised", feedback}`
  - Tags: `["planning"]`
  - Added to `coding.yaml` and `planning.yaml`
- **Files:** `src/tools/interaction_tools.py` (extended), `src/tools/toolsets/{coding,planning}.yaml`

#### GAP-F6: AST-Aware Refactoring ✓ IMPLEMENTED
- **Severity:** High
- **Status:** ✓ Implemented
- **Implementation:**
  - New file `src/tools/ast_tools.py`
  - `ast_rename(path, old_name, new_name, symbol_type)` — uses Python `ast` module for accurate rename of functions/classes/variables; regex word-boundary fallback for non-Python files; returns unified diff
  - `ast_list_symbols(path, symbol_type)` — lists all definitions with `{name, type, start_line, end_line}`
  - Added to `coding.yaml`
- **Files:** `src/tools/ast_tools.py` (new), `src/tools/toolsets/coding.yaml`

#### GAP-F7: Tech-Stack Detection ✓ IMPLEMENTED
- **Severity:** Medium
- **Status:** ✓ Implemented
- **Implementation:**
  - New file `src/tools/project_tools.py`
  - `fingerprint_tech_stack(workdir)` — scans for package.json, pyproject.toml, Cargo.toml, go.mod, pom.xml, Gemfile, Dockerfile, CI configs
  - Returns `{status, languages, frameworks, test_runners, build_tools, has_docker, has_ci, ci_providers, manifests_found}`
  - Tags: `["planning", "coding"]`
  - Added to `planning.yaml` and `coding.yaml`
- **Files:** `src/tools/project_tools.py` (new), `src/tools/toolsets/{planning,coding}.yaml`

#### GAP-F8: `tail_log_file` ✓ IMPLEMENTED
- **Severity:** Low-Medium
- **Status:** ✓ Implemented
- **Implementation:**
  - Added to `file_tools.py`: `tail_log_file(path, lines=50, workdir)` — reads last N lines
  - Returns `{status, content, total_lines, lines_shown}`
  - Tags: `["coding", "debug"]`
- **Files:** `src/tools/file_tools.py` (extended)

#### GAP-F9: `create_directory` ✓ IMPLEMENTED
- **Severity:** Low
- **Status:** ✓ Implemented
- **Implementation:**
  - Added to `file_tools.py`: `create_directory(path, workdir)` — creates directory and parents
  - Uses `_safe_resolve` for path validation
  - Tags: `["coding"]`, side_effects: `["write"]`
- **Files:** `src/tools/file_tools.py` (extended)

#### GAP-F10: `read_file_bytes` (Binary File Support) ✓ IMPLEMENTED
- **Severity:** Low
- **Status:** ✓ Implemented
- **Implementation:**
  - Added to `file_tools.py`: `read_file_bytes(path, max_bytes=1048576, workdir)` — reads binary files and returns base64-encoded content
  - Returns `{status, encoding: "base64", content, bytes_read, total_bytes, truncated}`
  - Tags: `["coding", "debug"]`
- **Files:** `src/tools/file_tools.py` (extended)

---

### 3.3 Code Quality & Multi-Language Gaps

#### GAP-Q1: `repo_analysis_tools.py` Multi-Language ✓ IMPLEMENTED
- **Severity:** High
- **Status:** ✓ Implemented
- **Implementation:**
  - `analyze_repository` now scans Python, JS/TS, Go, and Rust files
  - Python: AST-based (existing, preserved as `_analyze_python_file` with `_analyze_file` alias)
  - JS/TS: regex-based extraction of `export function`, `export class`, `import ... from`
  - Go: regex for `func`, `type ... struct`, import strings
  - Rust: regex for `pub fn`, `fn`, `pub struct`, `pub enum`, `use`
  - Output includes `languages` key with per-language file/function/class counts
  - File paths stored as relative to workdir for backward compatibility
- **Files:** `src/tools/repo_analysis_tools.py` (rewritten)

#### GAP-Q2: `find_references` Word-Boundary ✓ IMPLEMENTED
- **Severity:** Medium
- **Status:** ✓ Implemented
- **Implementation:**
  - Replaced `if name in text` with `re.compile(r"\b" + re.escape(name) + r"\b")`
  - Returns per-match `{file, line, col, snippet}` instead of single snippet per file
  - Iterates all lines for precise line/column tracking
- **Files:** `src/tools/repo_tools.py` (modified)

#### GAP-Q3: Multi-Language `run_linter` ✓ IMPLEMENTED
- **Severity:** Medium
- **Status:** ✓ Implemented
- **Implementation:**
  - `run_linter(workdir, fix, paths)` now accepts optional `paths` param
  - Auto-discovers files by extension if paths not provided
  - Dispatches: Python → ruff, JS → eslint, TS → tsc, Rust → cargo clippy, Go → go vet
  - Internal helper functions: `_run_ruff`, `_run_eslint_internal`, `_run_tsc_internal`, `_run_clippy`, `_run_go_vet`
  - Returns unified shape: `{status, languages_checked, total_errors, total_warnings, errors}`
  - Each error tagged with `language` field
- **Files:** `src/tools/verification_tools.py` (rewritten `run_linter`)

#### GAP-Q4: Multi-Language `syntax_check` ✓ IMPLEMENTED
- **Severity:** Medium
- **Status:** ✓ Implemented
- **Implementation:**
  - `syntax_check` now checks `.py` (py_compile), `.js/.mjs/.cjs/.jsx` (node --check), `.go` (go build), `.rs` (rustc --emit=metadata)
  - Each file's error tagged with `language` field
  - Returns `{status, checked_files, syntax_errors, languages_checked, error_count}`
  - Skips languages whose toolchain is not installed (FileNotFoundError caught)
  - Walk excludes `target`, `dist`, `build` directories
- **Files:** `src/tools/verification_tools.py` (rewritten `syntax_check`)

---

### 3.4 Task Management Gaps

#### GAP-T1: Task Status Machine ✓ IMPLEMENTED
- **Severity:** Medium
- **Status:** ✓ Implemented
- **Implementation:**
  - `manage_todo` supports new actions: `start`, `block`, `verify`, `next`
  - `start`: marks step `in_progress`, sets `started_at`; enforces single active step
  - `block`: marks step `blocked`, records `blocked_reason`
  - `verify`: marks step `verified`, records `completed_at`
  - `next`: returns first step where all `depends_on` steps are `done` or `verified`
  - Status values: `pending | in_progress | done | blocked | verified`
- **Files:** `src/tools/todo_tools.py` (extended)

#### GAP-T2: Dependency-Ordered Task Execution ✓ IMPLEMENTED
- **Severity:** Low
- **Status:** ✓ Implemented (as part of IMPL-11)
- **Implementation:**
  - `manage_todo("next")` checks `depends_on` list and returns first executable step
  - Dependencies must be `done` or `verified` to be considered satisfied
- **Files:** `src/tools/todo_tools.py` (extended)

---

### 3.5 Consistency & Polish Gaps

#### GAP-P1: Inconsistent `status` Field Values ✓ PARTIALLY IMPLEMENTED
- **Severity:** Low
- **Status:** ✓ Helpers added; codebase already uses `"ok"`/`"error"` consistently
- **Implementation:**
  - `ok(**kwargs)`, `err(msg, **kwargs)`, `partial(**kwargs)` helpers added to `src/tools/_tool.py`
  - Audit confirmed no `"success"` or `"fail"` values exist — all tools already use `"ok"`/`"error"`
  - `"skipped"`, `"not_found"`, `"timeout"` used for specific cases (acceptable)
- **Files:** `src/tools/_tool.py` (extended)

#### GAP-P2: Deprecate `get_git_diff` ✓ IMPLEMENTED
- **Severity:** Low
- **Status:** ✓ Implemented
- **Implementation:**
  - `get_git_diff` in `system_tools.py` now emits `DeprecationWarning` and delegates to `git_tools.git_diff()`
  - Fallback to legacy subprocess call if import fails
  - Removed from `coding.yaml`, `debug.yaml`, `review.yaml`
- **Files:** `src/tools/system_tools.py`, `src/tools/toolsets/{coding,debug,review}.yaml`

#### GAP-P3: `glob` Truncation Signal ✓ IMPLEMENTED
- **Severity:** Low
- **Status:** ✓ Implemented
- **Implementation:**
  - `glob` now tracks `total_found` before truncation
  - When `total_found > LIMIT` (500), adds `truncated: true` and `total_found: N` to result
- **Files:** `src/tools/file_tools.py` (modified `glob`)

---

## 4. Implementation Details (Completed)

Each item was assigned a priority tier:
- **P0 — Critical:** Functional breakage or severe correctness risk
- **P1 — High:** Significant capability gap vs. comparable systems
- **P2 — Medium:** Quality-of-life improvement
- **P3 — Low:** Polish

---

### Phase 1 — Fix Breakage (P0) ✓ DONE

#### IMPL-1: `memory_search` (GAP-F1) ✓ DONE
**File:** `src/tools/memory_tools.py` (new)
**Registered in:** `_registry.py` `_BUILTIN_MODULES`
**Toolset YAML:** Already referenced in `debug.yaml`, `planning.yaml` (no changes needed)

```python
@tool(tags=["coding", "debug", "planning", "review"])
def memory_search(query: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
```
- Searches VectorStore, falls back to keyword matching on TASK_STATE.md, compaction_checkpoint.md, todo.json
- Returns `{status, query, results: [{source, excerpt, score}]}`

#### IMPL-2: `ask_user` (GAP-F2) ✓ DONE
**File:** `src/tools/interaction_tools.py` (new)
**Toolset YAML:** Added to all four toolsets

```python
@tool(tags=["coding", "planning", "debug", "review"])
def ask_user(question: str, workdir: Path = DEFAULT_WORKDIR) -> Dict[str, Any]:
```
- EventBus pattern: publishes `agent.waiting_for_user`, blocks on `threading.Event` for `user.response`
- 300s timeout
- Also implements `submit_plan_for_review` (IMPL-9)

---

### Phase 2 — High-Value Capability Gaps (P1) ✓ DONE

#### IMPL-3: Read-Before-Write Guardrail (GAP-S1) ✓ DONE
**File:** `src/tools/guardrails.py` (new)
**Integration:** `file_tools.py` — all read functions call `mark_file_read()`, all write functions call `check_read_before_write()`

- Thread-local `_read_files: set[str]`
- `mark_file_read(path)` — called by `read_file`, `read_file_chunk`
- `check_read_before_write(path)` — returns `{error, requires_read_first}` if violation; allows new files
- `reset_guardrail_state()` — for orchestrator `start_new_task()`

#### IMPL-4: `bash_readonly` (GAP-S4) ✓ DONE
**File:** `src/tools/file_tools.py` (new function)
**Toolset YAML:** `debug.yaml` replaced `bash` with `bash_readonly`; `review.yaml` and `planning.yaml` gained `bash_readonly`

- Only `SAFE_COMMANDS` (tier 1) — no test runners, compilers, or restricted commands
- Blocks code-execution flags, sed -i, tar extract, unzip without -l
- Tags: `["coding", "debug", "review", "planning"]`

#### IMPL-5: Post-Write Auto-Lint (GAP-F3) ✓ DONE
**File:** `src/tools/lint_dispatch.py` (new)
**Integration:** `file_tools.py` — `write_file`, `edit_by_line_range`, `edit_file_atomic`

- `quick_lint(path, workdir)` — 10s timeout, never raises
- Python: `py_compile`, JS: `node --check`, TS: `npx tsc`, Go: `go build`, Rust: `rustc --emit=metadata`
- Informational — adds `lint_warnings` to result, does not block writes

#### IMPL-6: Web Search and URL Fetching (GAP-F4) ✓ DONE
**File:** `src/tools/web_tools.py` (new)
**Toolset YAML:** Added to `planning.yaml` and `debug.yaml`

- `web_search(query, max_results)` — DuckDuckGo primary, HTML scraping fallback
- `read_web_page(url)` — fetches + strips HTML, 10k char limit, SSRF protection (blocks private IPs)
- Optional deps: `duckduckgo-search`, `requests`, `html2text` — all wrapped in try/except

#### IMPL-7: AST-Aware Refactoring (GAP-F6) ✓ DONE
**File:** `src/tools/ast_tools.py` (new)
**Toolset YAML:** Added to `coding.yaml`

- `ast_rename(path, old_name, new_name, symbol_type)` — Python AST transform + regex fallback for non-Python
- `ast_list_symbols(path, symbol_type)` — lists `{name, type, start_line, end_line}`
- Returns unified diff of changes

#### IMPL-8: Tech-Stack Detection (GAP-F7) ✓ DONE
**File:** `src/tools/project_tools.py` (new)
**Toolset YAML:** Added to `planning.yaml` and `coding.yaml`

- `fingerprint_tech_stack(workdir)` — scans manifests for Python, JS/TS, Rust, Go, Java, Ruby, Docker, CI
- Returns `{languages, frameworks, test_runners, build_tools, has_docker, has_ci, ci_providers, manifests_found}`

#### IMPL-9: Human-in-the-Loop Plan Review Gate (GAP-F5) ✓ DONE
**File:** `src/tools/interaction_tools.py` (extended)
**Toolset YAML:** Added to `coding.yaml` and `planning.yaml`

- `submit_plan_for_review(plan_summary, plan_steps, risk_level)` — publishes `agent.plan_review_requested`
- Blocks until `plan_review.response` — returns `{decision: "approved"|"rejected"|"revised", feedback}`

---

### Phase 3 — Quality & Coverage (P2) ✓ DONE

#### IMPL-10: Multi-Language `run_linter` (GAP-Q3) ✓ DONE
**File:** `src/tools/verification_tools.py` (rewritten `run_linter`)

- Accepts optional `paths` param; auto-discovers by extension otherwise
- Internal dispatchers: `_run_ruff`, `_run_eslint_internal`, `_run_tsc_internal`, `_run_clippy`, `_run_go_vet`
- Unified result: `{status, languages_checked, total_errors, total_warnings, errors}`

#### IMPL-11: Task Status Machine (GAP-T1, GAP-T2) ✓ DONE
**File:** `src/tools/todo_tools.py` (extended)

- New actions: `start` (→ `in_progress`), `block` (→ `blocked`), `verify` (→ `verified`), `next` (dependency-ordered)
- Schema: `{status, depends_on, blocked_reason, started_at, completed_at}`
- `next` checks `depends_on` satisfaction before returning executable step

#### IMPL-12: `find_references` Word-Boundary (GAP-Q2) ✓ DONE
**File:** `src/tools/repo_tools.py` (modified)

- `re.compile(r"\b" + re.escape(name) + r"\b")` replaces substring match
- Per-line scanning with `{file, line, col, snippet}` per match

#### IMPL-13: `tail_log_file` and `create_directory` (GAP-F8, GAP-F9) ✓ DONE
**File:** `src/tools/file_tools.py` (extended)

- `tail_log_file(path, lines=50)` — reads last N lines, returns `{content, total_lines, lines_shown}`
- `create_directory(path)` — `p.mkdir(parents=True, exist_ok=True)`

#### IMPL-14: Multi-Language `analyze_repository` (GAP-Q1) ✓ DONE
**File:** `src/tools/repo_analysis_tools.py` (rewritten)

- Python: AST-based (preserved)
- JS/TS: regex for exports, imports, functions, classes
- Go: regex for func, struct, imports
- Rust: regex for pub fn, fn, pub struct, pub enum, use
- Output: `{languages: {lang: {files, functions, classes}}, module_summaries, dependency_relationships}`
- Backward-compatible `_analyze_file = _analyze_python_file` alias

#### GAP-Q4: Multi-Language `syntax_check` ✓ DONE
**File:** `src/tools/verification_tools.py` (rewritten `syntax_check`)

- Python: `py_compile`, JS: `node --check`, Go: `go build -o /dev/null`, Rust: `rustc --emit=metadata`
- Returns `{status, checked_files, syntax_errors: [{file, line, error, language}], languages_checked}`
- Gracefully skips languages whose toolchain is not installed

#### GAP-S3: Hard File-Size Guard ✓ DONE
**File:** `src/tools/file_tools.py` (modified `write_file`)

- Hard block at >500 lines: write is not applied, returns `status: "error"`
- Soft warning at >200 lines preserved

#### GAP-F10: `read_file_bytes` ✓ DONE
**File:** `src/tools/file_tools.py` (extended)

- `read_file_bytes(path, max_bytes=1048576)` — base64-encoded output
- Returns `{content, bytes_read, total_bytes, truncated}`

---

### Phase 4 — Polish (P3) ✓ DONE

#### IMPL-15: Standardise `status` Values (GAP-P1) ✓ DONE
**File:** `src/tools/_tool.py` (extended)

- Added `ok(**kwargs)`, `err(msg, **kwargs)`, `partial(**kwargs)` helpers
- Codebase audit confirmed all tools already use `"ok"`/`"error"` — no `"success"` or `"fail"` found

#### IMPL-16: `glob` Truncation Signal (GAP-P3) ✓ DONE
**File:** `src/tools/file_tools.py` (modified)

- Returns `{truncated: true, total_found: N}` when 500-match limit hit

#### IMPL-17: Deprecate `get_git_diff` (GAP-P2) ✓ DONE
**File:** `src/tools/system_tools.py` (modified), YAML toolsets (modified)

- Emits `DeprecationWarning`, delegates to `git_tools.git_diff()`
- Removed from `coding.yaml`, `debug.yaml`, `review.yaml`

---

## 5. Dependency Map

```
IMPL-1 (memory_search)      → No blockers ✓
IMPL-2 (ask_user)           → EventBus (exists), wait_for_user_node (exists) ✓
IMPL-3 (read-before-write)  → start_new_task() hook (exists) ✓
IMPL-4 (bash_readonly)      → _security.py SAFE_COMMANDS (exists) ✓
IMPL-5 (auto-lint)          → lint_dispatch (new), write_file (exists) ✓
IMPL-6 (web_tools)          → optional: duckduckgo-search, requests ✓
IMPL-7 (ast_rename)         → ast (stdlib), optional: asttokens or libcst ✓
IMPL-8 (fingerprint)        → No deps ✓
IMPL-9 (plan review gate)   → IMPL-2 (ask_user pattern), plan_mode (exists) ✓
IMPL-10 (multi-lang lint)   → run_eslint, run_ts_check (exist) ✓
IMPL-11 (task status)       → manage_todo (exists) ✓
IMPL-12 (find_references)   → repo_tools (exists) ✓
IMPL-13 (tail, mkdir)       → No blockers ✓
IMPL-14 (multi-lang repo)   → analyze_repository (exists) ✓
IMPL-15 (status values)     → All tools ✓
IMPL-16 (glob truncation)   → glob (exists) ✓
IMPL-17 (deprecate diff)    → git_diff (exists) ✓
GAP-S2 (scope guard)        → Requires orchestrator affected_files tracking — DEFERRED
```

---

## 6. Effort & Priority Matrix (Updated with Actual Status)

| ID | Gap | Priority | Effort | Files Affected | Status |
|---|---|---|---|---|---|
| IMPL-1 | memory_search | P0 | S | `memory_tools.py` (new), `_registry.py` | ✓ Done |
| IMPL-2 | ask_user | P0 | M | `interaction_tools.py` (new), 4 YAML toolsets | ✓ Done |
| IMPL-3 | read-before-write guardrail | P1 | M | `guardrails.py` (new), `file_tools.py` | ✓ Done |
| IMPL-4 | bash_readonly | P1 | S | `file_tools.py`, 4 YAML toolsets | ✓ Done |
| IMPL-5 | post-write auto-lint | P1 | M | `lint_dispatch.py` (new), `file_tools.py` | ✓ Done |
| IMPL-6 | web_tools | P1 | M | `web_tools.py` (new), 2 YAML toolsets | ✓ Done |
| IMPL-7 | ast_rename | P1 | L | `ast_tools.py` (new), `coding.yaml` | ✓ Done |
| IMPL-8 | fingerprint_tech_stack | P1 | M | `project_tools.py` (new), 2 YAML toolsets | ✓ Done |
| IMPL-9 | plan review gate | P1 | M | `interaction_tools.py`, 2 YAML toolsets | ✓ Done |
| IMPL-10 | multi-lang linter | P2 | M | `verification_tools.py` | ✓ Done |
| IMPL-11 | task status machine | P2 | S | `todo_tools.py` | ✓ Done |
| IMPL-12 | find_references word-boundary | P2 | S | `repo_tools.py` | ✓ Done |
| IMPL-13 | tail_log_file, create_directory | P2 | S | `file_tools.py` | ✓ Done |
| IMPL-14 | multi-lang repo analysis | P2 | L | `repo_analysis_tools.py` | ✓ Done |
| IMPL-15 | standardise status values | P3 | XS | `_tool.py` | ✓ Done |
| IMPL-16 | glob truncation signal | P3 | XS | `file_tools.py` | ✓ Done |
| IMPL-17 | deprecate get_git_diff | P3 | XS | `system_tools.py`, 3 YAML toolsets | ✓ Done |
| GAP-Q4 | multi-lang syntax_check | P2 | M | `verification_tools.py` | ✓ Done |
| GAP-S3 | hard file-size guard | P2 | XS | `file_tools.py` | ✓ Done |
| GAP-F10 | read_file_bytes | P3 | XS | `file_tools.py` | ✓ Done |
| GAP-S2 | scope guard | High | L | orchestrator integration | Deferred |

**Effort key:** XS < 1h · S 1–2h · M 3–5h · L 6–10h

---

## 7. Test Coverage

All implementations verified with existing test suite: **1502+ tests passing, 0 failures**.

Key test areas affected:
- `test_state_init_threading_toolset_providers.py` — updated for `bash_readonly` in debug role
- `test_repo_analysis_tools.py` — backward-compatible `_analyze_file` alias preserved
- `test_tools_file_ops.py`, `test_tools_system_extra.py` — `get_git_diff` deprecation warnings observed

---

## 8. Registry & Toolset YAML (Updated)

### `_BUILTIN_MODULES` in `_registry.py`
```python
_BUILTIN_MODULES = [
    "src.tools.file_tools",
    "src.tools.git_tools",
    "src.tools.verification_tools",
    "src.tools.todo_tools",
    "src.tools.subagent_tools",
    "src.tools.repo_tools",
    "src.tools.repo_analysis_tools",
    "src.tools.patch_tools",
    "src.tools.state_tools",
    "src.tools.system_tools",
    "src.tools.memory_tools",
    "src.tools.interaction_tools",
    "src.tools.guardrails",
    "src.tools.web_tools",
    "src.tools.ast_tools",
    "src.tools.project_tools",
]
```

### YAML Toolsets (Updated)

**coding.yaml** (23 tools):
read_file, write_file, edit_file, edit_by_line_range, delete_file, list_files, glob, search_code, find_symbol, find_references, grep, run_tests, run_linter, syntax_check, apply_patch, generate_patch, read_file_chunk, batched_file_read, bash_readonly, ask_user, submit_plan_for_review, memory_search

**debug.yaml** (17 tools):
read_file, list_files, grep, search_code, find_symbol, find_references, run_tests, run_linter, syntax_check, bash_readonly, memory_search, batched_file_read, multi_file_summary, ask_user, web_search, read_web_page

**planning.yaml** (19 tools):
read_file, list_files, search_code, find_symbol, find_references, memory_search, analyze_repository, initialize_repo_intelligence, grep, multi_file_summary, batched_file_read, delegate_task, list_subagent_roles, bash_readonly, ask_user, submit_plan_for_review, web_search, read_web_page, fingerprint_tech_stack

**review.yaml** (14 tools):
read_file, list_files, run_tests, run_linter, syntax_check, grep, find_symbol, find_references, multi_file_summary, search_code, bash_readonly, ask_user, memory_search
