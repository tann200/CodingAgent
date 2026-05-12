# Refactoring Implementation Plan

Generated: 2026-05-12

This document tracks all identified code quality issues and the plan/status for addressing each.

---

## Priority Legend

| Priority | Label | Description |
|---|---|---|
| P1 | High | Correctness/debuggability risk; implement first |
| P2 | Medium | Design debt; implement after P1 |
| P3 | Low | Polish/style; implement last |

---

## P1 — High Priority

### 1. Replace `except: pass` with `logger.debug()` (772 instances)

**Problem:** 772 bare `except: pass` or `except Exception: pass` blocks silently discard exceptions across credentials loading, LSP, inference adapters, config loading, and prompt assembly. Failures become invisible.

**Files most affected:**
- `src/core/config_loader.py` (lines 165, 378, 386, 459)
- `src/core/context/context_builder.py` (lines 167, 558, 675, 862, 915, 941, 993, 1041)
- `src/core/context/prompt_blocks.py` (lines 47, 54, 61, 69)
- `src/core/context/sanitization.py` (line 63)
- `src/core/context/static_prompt_parts.py` (lines 153, 179, 198, 266, 360)
- `src/core/credentials.py` (lines 102, 127, 132, 144, 160)
- `src/core/indexing/lsp_client.py` (lines 393, 399, 540, 561)
- `src/core/indexing/lsp_manager.py` (lines 75, 188)
- `src/core/indexing/repo_indexer.py` (lines 228, 271, 440, 449)
- `src/core/indexing/symbol_graph.py` (line 482)
- `src/core/inference/adapters/ollama_adapter.py` (lines 57, 310, 348, 592, 671)
- `src/core/inference/adapters/openai_compat_adapter.py` (lines 379, 385, 396)
- `src/core/inference/adapters/github_copilot_auth.py` (lines 145, 177, 649)
- `src/core/inference/adapters/github_copilot_adapter.py` (line 221)
- `src/core/inference/adapters/groq_adapter.py` (line 77)
- `src/core/inference/adapters/litellm_adapter.py` (line 96)
- `src/core/inference/adapters/lm_studio_adapter.py` (lines 106, 165, 302)
- `src/core/evaluation/scenario_evaluator.py` (line 36)

**Approach:**
- Replace `except ...: pass` → `except ... as exc: logger.debug("...: %s", exc)`
- Keep intentional no-ops that have explanatory comments (e.g. `sqlite_session_store.py:507`)
- Ensure a logger is present in each module (`logging.getLogger(__name__)`)

**Tests:** Run `tests/unit/` after each file group. Add targeted tests verifying that debug log is emitted on failure where feasible.

**Status:** [ ] Pending

---

### 2. Split `SqliteSessionStore` (1,138 lines)

**File:** `src/core/memory/sqlite_session_store.py`

**Problem:** One class conflates connection management, schema versioning/migrations, CRUD operations, snapshot/rollback logic, FTS5 index management, and sidecar JSON writing.

**Plan — extract into collaborators:**

| New Class | Responsibility | New File |
|---|---|---|
| `ConnectionManager` | Reader/writer SQLite connection pool, WAL mode, thread-safety | `sqlite_connection_manager.py` |
| `SchemaManager` | Schema creation, version detection, v1→v2→v3 migrations | `sqlite_schema_manager.py` |
| `SnapshotManager` | Snapshot create/restore/list/delete, sidecar JSON | `sqlite_snapshot_manager.py` |
| `SqliteSessionStore` (slimmed) | CRUD delegation to above collaborators | `sqlite_session_store.py` (reduced) |

**Tests:** Existing `tests/unit/test_sqlite_session_store.py` must remain green. Add unit tests for each new collaborator.

**Status:** [ ] Pending

---

### 3. Split `OpenAICompatibleAdapter._chat_internal()` (278 lines)

**File:** `src/core/inference/adapters/openai_compat_adapter.py`

**Problem:** `_chat_internal()` handles request construction, streaming SSE parsing, non-streaming JSON decoding, native tool call extraction, context overflow detection, and retry logic all in one method.

**Plan — extract private methods:**

| New Method | Responsibility |
|---|---|
| `_build_request_payload()` | Assemble the JSON payload dict |
| `_parse_streaming_response()` | SSE chunk iteration → text + tool calls |
| `_parse_nonstreaming_response()` | JSON decode + tool call extraction |
| `_detect_context_overflow()` | Check response for context length errors |

**Tests:** Existing adapter tests must pass. Add unit tests for each extracted method with mock HTTP responses.

**Status:** [ ] Pending

---

### 4. Split `frontier_loop_node()` (548 lines)

**File:** `src/core/orchestration/graph/nodes/frontier_loop_node.py`

**Problem:** The main `frontier_loop_node()` function contains: turn management, message normalization, LLM calling, tool call extraction, tool dispatch, result assembly, and loop termination — all inline.

**Plan — extract sub-functions:**

| New Function | Responsibility |
|---|---|
| `_prepare_turn_messages()` | Build messages for LLM call from state |
| `_dispatch_tool_calls()` | Execute tool calls from LLM response |
| `_assemble_turn_result()` | Build state delta from tool results |
| `_should_terminate_loop()` | Evaluate loop exit conditions |

**Tests:** Benchmark scenarios + unit tests must pass after each extraction.

**Status:** [ ] Pending

---

### 5. Split `MCPStdioServer._handle_request()` (342 lines)

**File:** `src/core/orchestration/mcp_stdio_server.py`

**Problem:** `_handle_request()` dispatches all MCP methods (`initialize`, `tools/list`, `tools/call`, `$/cancelRequest`, etc.) in a single 342-line if/elif chain mixing protocol and business logic.

**Plan — extract per-method handlers:**

| New Method | MCP Method Handled |
|---|---|
| `_handle_initialize()` | `initialize` |
| `_handle_tools_list()` | `tools/list` |
| `_handle_tools_call()` | `tools/call` |
| `_handle_cancel()` | `$/cancelRequest` |
| `_dispatch_request()` | Route to above handlers via dispatch table |

**Tests:** Add unit tests for each handler with mock request dicts.

**Status:** [ ] Pending

---

## P2 — Medium Priority

### 6. Add `__all__` to `__init__.py` re-exports

**Files:**
- `src/core/inference/__init__.py` (~50 re-exported symbols)
- `src/core/auth/__init__.py`
- `src/core/mcp/__init__.py`

**Problem:** All symbols imported for re-export without `__all__`. Linters flag them all as unused imports; `from module import *` is undefined.

**Approach:** Add `__all__ = [...]` listing all intentionally re-exported symbols.

**Tests:** Import smoke tests; verify no `F401` linter errors.

**Status:** [ ] Pending

---

### 7. Extract `SessionStore` routing adapter into proper strategy pattern

**File:** `src/core/memory/session_store.py`

**Problem:** `SessionStore` exists solely to route between `JsonlSessionStore` and `SqliteSessionStore` — an anti-pattern caused by the dual-store design. Both stores have near-identical public interfaces but no common abstract base.

**Plan:**
- Introduce `AbstractSessionStore` base class / Protocol in `abstract_session_store.py`
- Make both `SqliteSessionStore` and `JsonlSessionStore` implement it
- Replace `SessionStore` routing logic with a factory function `create_session_store(config) -> AbstractSessionStore`

**Tests:** Existing session store tests must pass. Add Protocol compliance tests.

**Status:** [ ] Pending

---

## P3 — Low Priority

### 8. Replace `__import__()` anti-pattern with top-level imports (9 occurrences)

**Problem:** `__import__()` used inline for `asyncio`, `pathlib`, `json`, `datetime`, and a dynamic module import in a lambda. Slower, hides dependencies, breaks static analysis.

**Files:**
- `src/core/orchestration/graph/nodes/execution_helpers.py` (lines 445, 450, 1150)
- `src/core/orchestration/event_bus.py` (lines 130, 143, 155)
- `src/core/orchestration/agent_session_manager.py` (line 219)
- `src/core/io_utils.py` (line 106)
- `src/core/inference/llm_manager.py` (line 807)
- `src/tools/todo_tools.py` (lines 681, 754)

**Approach:** Move each to a top-level `import` statement. For the dynamic import in `llm_manager.py:807`, extract the lambda into a named function with a proper `importlib.import_module()` call.

**Tests:** Unit tests must pass unchanged (behavior-preserving).

**Status:** [ ] Pending

---

## Implementation Order

```
Step 1: P1.1 — except:pass → logger.debug (lowest risk, highest debuggability gain)
Step 2: Tests
Step 3: P3.1 — __import__() cleanup (low risk, behavior-preserving)
Step 4: Tests
Step 5: P2.1 — __all__ in __init__.py files
Step 6: Tests
Step 7: P1.3 — Split _chat_internal() (self-contained, good test coverage)
Step 8: Tests
Step 9: P1.5 — Split MCPStdioServer._handle_request()
Step 10: Tests
Step 11: P1.4 — Split frontier_loop_node()
Step 12: Tests
Step 13: P2.2 — AbstractSessionStore + factory
Step 14: Tests
Step 15: P1.2 — Split SqliteSessionStore (largest, most complex — last)
Step 16: Tests
```

---

## Progress Tracking

| Step | Item | Status |
|---|---|---|
| 1 | P1.1 except:pass → logger.debug | ✅ Complete |
| 2 | P3.1 __import__() cleanup | ✅ Complete |
| 3 | P2.1 __all__ in __init__.py | ✅ Complete |
| 4 | P1.3 Split _chat_internal() | ✅ Complete |
| 5 | P1.5 Split _handle_request() | ✅ Complete |
| 6 | P1.4 Split frontier_loop_node() | ✅ Complete |
| 7 | P2.2 AbstractSessionStore | ✅ Complete |
| 8 | P1.2 Split SqliteSessionStore | ✅ Complete |
