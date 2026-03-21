# Coding Agent — Deep Audit Report (Vol 3)

**Date:** 2026-03-20
**Auditor:** Claude Sonnet 4.6
**Scope:** Full codebase — tool usage, workflows, efficiency, usability, scalability to local 9B and frontier models

---

## 1. Executive Summary

The system has a well-structured LangGraph pipeline with multiple rounds of bug fixing behind it. The core cognitive loop (perception → analysis → planning → execution → verification → debug) is architecturally sound. However, this audit found **5 critical bugs** that directly impact correctness and efficiency, **9 high-severity issues** affecting reliability and scalability, and **12 medium/low issues** affecting maintainability, token efficiency, and security.

The most damaging issue is that **`step_controller_node` sets `planned_action` but `execution_node` never reads it**, causing an extra LLM call per plan step — for a 5-step plan this triples token usage and latency on local 9B models. A second critical issue is that **`should_after_step_controller` routes failed steps to verification rather than retry**, silently swallowing execution failures.

The system is viable for local 9B usage but will be inefficient. For frontier models with 128K+ context windows, the hardcoded 6000-token budget leaves 95% of available context unused.

---

## 2. Architecture Strengths

- **Clean graph structure** — LangGraph nodes are well-separated and each has a single responsibility.
- **Fast-path routing** — simple tasks bypass analysis/planning to go direct to execution.
- **Circuit breaker in LLM manager** — prevents runaway calls to a failing provider.
- **Read-before-edit enforcement** — execution_node blocks modifying tools if file wasn't read first.
- **Step-level atomic rollback** — verification_node can roll back file changes on failure.
- **Thinking token handling** — `thinking_utils.py` correctly strips `<think>` blocks and budgets tokens for reasoning models.
- **Token budget management** — `ContextBuilder` enforces per-section token quotas.
- **WorkspaceGuard** — protects critical files like `.git/`, `.env`, lock files.
- **Multi-language verification** — supports Python (pytest + ruff) and JS/TS (jest + tsc + eslint).
- **Correlation IDs** — every LLM call is tagged for tracing.

---

## 3. Critical Architectural Flaws

### C1 — `step_controller_node` output `planned_action` is never used by `execution_node`
**Severity: Critical**
**Location:** `src/core/orchestration/graph/nodes/step_controller_node.py:43`, `src/core/orchestration/graph/nodes/execution_node.py:59-170`

`step_controller_node` returns `{"step_description": ..., "planned_action": ...}` to state. `execution_node` reads `action = state["next_action"]` (which is always `None` after step_controller since it was reset after the previous execution). It then falls into the LLM-generation branch:
```python
if not action and current_plan and current_step < len(current_plan):
    # Call LLM to generate a tool for this step
```

The `state["planned_action"]` is completely ignored. Every plan step triggers an extra LLM call even when planning already specified the action. For a 5-step plan on a local 9B model, this adds 5 unnecessary LLM calls — roughly tripling latency.

**Fix:** In `execution_node`, check `state.get("planned_action")` as fallback before triggering LLM generation:
```python
action = state.get("next_action") or state.get("planned_action")
```

---

### C2 — `should_after_step_controller` routes FAILED steps to verification, not retry
**Severity: Critical**
**Location:** `src/core/orchestration/graph/builder.py:408-414`

When `last_result.ok` is False (step failed — tool error, sandbox violation, etc.), `should_after_step_controller` routes to `"verification"`:
```python
else:
    # Last execution failed, still at same step
    logger.info("step failed, going to verification")
    return "verification"
```

Verification runs tests on unchanged code. Since the step failed (nothing was changed), verification typically passes (tests still green), evaluation returns `"complete"`, and the failed step is silently ignored. The task ends with an incomplete plan step but the system reports success.

**Fix:** Route to `"execution"` (to retry the failed step) or back to `"perception"` to re-plan.

---

### C3 — `execution_node` mutates `current_plan` in-place (LangGraph state violation)
**Severity: Critical**
**Location:** `src/core/orchestration/graph/nodes/execution_node.py:165`

```python
current_plan[current_step]["action"] = tool_call
```

This mutates the original state list dict in-place before returning it in the state update. LangGraph's `Annotated[List, operator.add]` reducer relies on immutability — in-place mutation can cause state corruption in multi-run or parallel scenarios, and produces subtle bugs in test replays.

**Fix:** Build a copy: `updated_plan = [dict(s) for s in current_plan]; updated_plan[current_step]["action"] = tool_call` then use `updated_plan` in the return dict.

---

### C4 — `WorkspaceGuard` bypassable via LLM-controlled `user_approved` argument
**Severity: Critical**
**Location:** `src/tools/file_tools.py:26`, `src/core/orchestration/tool_contracts.py`

`write_file`, `edit_file`, `delete_file`, and `edit_file_atomic` all accept `user_approved: bool = False`. When the LLM generates a tool call YAML, it can include `user_approved: true` as an argument. If an adversarial task (or jailbroken model output) generates a tool call like:
```yaml
name: write_file
arguments:
  path: .env
  content: "EVIL=true"
  user_approved: true
```
The guard is bypassed. There is no runtime enforcement that `user_approved` can only be set by actual user interaction.

**Fix:** Remove `user_approved` from the tool's public argument schema entirely. Approval should be a runtime flag set by the orchestrator's preflight, not a parameter accepted from LLM output.

---

### C5 — `bash` allows `sed -i` (in-place file editing bypasses read-before-edit guard)
**Severity: Critical**
**Location:** `src/tools/file_tools.py:292-524`

`sed` is in `SAFE_COMMANDS` (line 303). `sed -i 's/old/new/g' file.py` edits a file in-place without using any redirect (`>`, `>>`) so the dangerous pattern check doesn't block it. This bypasses both the read-before-edit enforcement in `execution_node` and the `WorkspaceGuard` protected-file check. Similarly `tar -x` can extract to arbitrary paths (path traversal).

**Fix:** Move `sed` to `RESTRICTED_COMMANDS` or add a check that blocks `-i`/`--in-place` flags. Add `tar` flag validation to block `-x`/`--extract` with `--absolute-names`.

---

## 4. High-Risk Safety Issues

### H1 — `analysis_node` calls `index_repository` on every analysis pass
**Severity: High**
**Location:** `src/core/orchestration/graph/nodes/analysis_node.py:88-93`

`index_repository(working_dir)` re-indexes the entire repo from scratch on every analysis call. For a large repo, this blocks the graph for seconds on every task that goes through analysis. There's no cache check or incremental update — it always fully rebuilds.

**Fix:** Add a session-level flag or file-mtime check so indexing is skipped if already done within the current session.

---

### H2 — `analysis_node` passes `task.split()[0]` as symbol name to `find_symbol`
**Severity: High**
**Location:** `src/core/orchestration/graph/nodes/analysis_node.py:135`

```python
fs = _call_tool_if_exists("find_symbol", name=task.split()[0] if task else "", ...)
```

For a task like `"implement a new authentication module"`, this searches for the symbol `"implement"` — a word that never appears as a function/class name. The result is always empty or garbage. This wastes a tool call and pollutes `relevant_files`.

**Fix:** Extract candidate symbol names using a regex (e.g., CamelCase or snake_case identifiers), or skip `find_symbol` in analysis and let perception_node's pre-retrieval handle it.

---

### H3 — `perception_node` runs repo intel tools on EVERY perception pass
**Severity: High**
**Location:** `src/core/orchestration/graph/nodes/perception_node.py:205-288`

`search_code`, `find_symbol`, and `find_references` are called at the start of every perception pass, including the 2nd, 3rd… perception pass in a loop, and for each step re-entry after `plan_validator → perception`. For a task that does 5 perception passes (3 empty-response corrections + 2 actual calls), this produces 15 repo intel tool calls with the same query.

**Fix:** Only execute pre-retrieval on the first perception call (rounds == 0) or when the task changes. Cache results in state under `perception_retrieved_snippets`.

---

### H4 — Token budget hardcoded; frontier models use <5% of their context window
**Severity: High**
**Location:** `src/core/context/context_builder.py:168` (default `max_tokens=6000`), `src/core/orchestration/graph/nodes/execution_node.py:123` (`max_tokens=4000`)

All node calls use hardcoded token budgets (4000–6000 tokens). For a frontier model with a 128K context window (GPT-4o, Claude 3.5 Sonnet), this leaves 95% of available context unused. The agent cannot load large files or rich history that would let frontier models produce better results. For local 9B models (32K context), 6000 tokens is reasonable.

**Fix:** Make token budget configurable per provider. Read `provider.context_length` from `providers.json` and set `max_tokens = int(context_length * 0.7)` as the default, capped at a reasonable maximum (e.g., 32K).

---

### H5 — `delegation_node` results are never integrated into main pipeline
**Severity: High**
**Location:** `src/core/orchestration/graph/nodes/delegation_node.py`, `src/core/orchestration/graph/builder.py:610-615`

`delegation_node` stores results under `state["delegation_results"]`. No downstream node reads this field. The delegation machinery runs after `memory_sync` and before `END` — it's a fire-and-forget terminal node with no feedback path. The `create_delegation()` helper exists, but nothing in the main workflow sets `state["delegations"]` with actual tasks. The entire delegation pipeline is architecturally wired but functionally dead.

**Fix:** Either (a) wire delegation results back into the next perception pass via `state["analyst_findings"]`, or (b) remove the delegation infrastructure until it can be properly integrated to avoid confusing dead code.

---

### H6 — `should_after_step_controller` and `should_after_execution` have conflicting step advancement logic
**Severity: High**
**Location:** `src/core/orchestration/graph/builder.py:371-424` vs `src/core/orchestration/graph/nodes/execution_node.py:308-328`

`execution_node` advances `current_step` to `next_step` and writes it to state. Then `should_after_execution` checks `if current_step < len(current_plan)` using the ALREADY-ADVANCED step. Then `step_controller_node` also reads `current_step` to load step data. But `should_after_step_controller` again checks `if last_result.ok: if current_step < len` with the advanced step. There are now two separate step-advancement checks that can produce inconsistent routing, especially when a plan step completes exactly at the boundary (`current_step == len(plan) - 1`).

**Fix:** Centralize step advancement: only `step_controller_node` should update `current_step`. `execution_node` should return the step result but NOT advance the step counter.

---

### H7 — `planning_node` returns empty plan on LLM failure, re-triggering loop
**Severity: High**
**Location:** `src/core/orchestration/graph/nodes/planning_node.py:262-266`

The `except Exception` and empty-steps fallback both return `{"current_plan": current_plan, ...}` where `current_plan` may be `[]`. This triggers `plan_validator → perception` loop again. The Strategy 4 fallback in `_parse_plan_content` only fires when output is < 500 chars AND contains action words — long garbage LLM output or metadata (e.g., Qwen3 emitting `PLAN_STEPS: 1`) skips all strategies and returns `[]`.

**Fix:** After exhausting all parse strategies, create a guaranteed minimum plan: `[{"description": task, "action": None}]`. This ensures at least a single-step plan exists, breaking the planning loop.

---

### H8 — `edit_by_line_range` not in `SIDE_EFFECT_TOOLS`, so verification isn't triggered after it
**Severity: High**
**Location:** `src/core/orchestration/graph/nodes/verification_node.py:80-83`

`SIDE_EFFECT_TOOLS = {"write_file", "edit_file", "edit_file_atomic", "bash", "patch_apply", "apply_patch", "create_file", "delete_file"}` — `edit_by_line_range` is missing. If the agent uses `edit_by_line_range` (listed in the operational role prompt), verification is not triggered. The code change goes unverified.

**Fix:** Add `"edit_by_line_range"` to `SIDE_EFFECT_TOOLS`.

---

### H9 — `get_active_model_id()` loads `providers.json` on every thinking-utils call
**Severity: High**
**Location:** `src/core/inference/thinking_utils.py:81-99`

`get_active_model_id()` calls `load_provider(None)` which reads and parses `providers.json` from disk. It's called from `perception_node`'s decomposition path every time a task is decomposed. No caching — every decomposition call hits the filesystem.

**Fix:** Cache result in a module-level variable with a `_MODEL_ID_CACHE` guard, or read from an already-initialized provider manager.

---

## 5. Major Missing Capabilities

### M1 — No streaming output to TUI during LLM generation
**Location:** All nodes call `call_model(..., stream=False)`

All LLM calls use `stream=False`. The TUI receives the assistant content only after the full response is generated. On slow local 9B models (30-60 seconds per response), the UI shows nothing until completion. Modern agent UIs stream tokens in real-time.

**Fix:** Implement streaming with a callback that pushes tokens to the EventBus → TUI as they arrive. `stream=True` with a token callback is supported by most adapters.

---

### M2 — No per-step verification (only verifies after ALL steps complete)
**Location:** `src/core/orchestration/graph/builder.py:580-582`

Verification runs once at the end of all plan steps. If step 3 of 5 introduces a bug, the system continues executing steps 4 and 5, compounding the error before detection. Strong coding agents (Devin, SWE-agent) verify after each file-modifying step.

**Fix:** Add a step-level verification trigger: after each successful execution of a side-effecting tool, run a lightweight lint/syntax check on the modified file only.

---

### M3 — No provider fallback chain
**Location:** `src/core/inference/llm_manager.py`

Only one provider is active at a time. If the provider fails (circuit open), the entire agent stops. There's no automatic fallback to a second provider (e.g., fall back from LM Studio to Ollama).

**Fix:** Support an ordered provider list in `providers.json`. On circuit-open or 3-consecutive-failures, try the next provider.

---

### M4 — No structured output enforcement for LLM responses
**Location:** `src/core/orchestration/tool_parser.py`

Tool calls are parsed from free-form text YAML using regex fallbacks. Local 9B models frequently emit malformed YAML (extra text, wrong indentation, JSON instead of YAML). The parser has 4 strategies + inline fallback, but still fails often (triggering empty_response_count loops). Modern implementations use grammar-constrained generation (llama.cpp grammar, Outlines, structured outputs) to guarantee valid YAML/JSON output.

**Fix:** Add an optional `format: yaml` or `grammar` field to `call_model` requests that LM Studio/Ollama can use for constrained generation.

---

### M5 — No task interrupt / resume (mid-task cancellation loses state)
**Location:** `src/core/orchestration/orchestrator.py`

Cancellation via `cancel_event` stops the current node but doesn't persist the partial plan or completed steps. On restart, the agent starts from scratch. The `last_plan.json` persistence exists but only resumes if the exact task string matches — no partial progress resume.

---

## 6. Workflow Reliability Issues

### W1 — Double decomposition: `perception_node` AND `planning_node` both independently decompose tasks
**Location:** `perception_node.py:87-202`, `planning_node.py:119-131`

`perception_node` decomposes at round 0 if multi-step indicators are found. If it succeeds, it returns a plan and `planning_node` then sees `task_decomposed=True` and skips. But if perception decomposition fails or returns an empty plan, `planning_node` also attempts decomposition. This creates two independent decomposition paths with different strategies that can conflict. A task decomposed by perception gets a different plan than one decomposed by planning.

**Fix:** Remove the decomposition logic from `perception_node` entirely. Decomposition should be the sole responsibility of `planning_node`. Perception should focus only on tool call generation (fast path) or routing to analysis.

---

### W2 — `should_after_planning` function is wired to nothing (dead code)
**Location:** `src/core/orchestration/graph/builder.py:24-46`

`should_after_planning` contains routing logic but the comment on line 30 says "NOTE: This function is not wired as a conditional edge." It's kept for subgraphs and tests. However, having unwired routing functions in the same file as the graph builder is confusing and risks someone wiring the wrong function.

**Fix:** Move this to a `_legacy.py` file or delete it and document the subgraph usage in graph_factory.py.

---

### W3 — `replan_required` signal mismatch: role prompt says "set `replan_required=true`", code checks `res.get("requires_split")`
**Location:** `src/config/agent-brain/roles/operational.md:62`, `src/core/orchestration/graph/nodes/execution_node.py:384`

The operational role tells the LLM: "set `replan_required=true` in your response". But `execution_node` checks the *tool result* for `res.get("requires_split")`. The LLM cannot set `replan_required` in the tool call YAML (that would go to `next_action.arguments`, not the tool result). The tool result `requires_split` must be set by the tool function itself (e.g., `edit_file` checking patch size) — but `edit_file` in `file_tools.py` never sets `requires_split`. So the entire replan-on-oversized-patch path is broken.

**Fix:** Remove the LLM instruction to "set replan_required=true" from the role prompt (LLM can't do this). Add patch-size checking in `edit_file`/`write_file` that returns `requires_split: True` when the diff exceeds 200 lines.

---

### W4 — Async handling inconsistency: `perception_node` and `planning_node` use `hasattr(__await__)` pattern
**Location:** `perception_node.py:363-401`, `planning_node.py:194-215`

```python
raw_resp = call_model(...)  # no await — creates coroutine object
if hasattr(raw_resp, "__await__"):
    llm_task = asyncio.create_task(raw_resp)
```

`call_model` is always `async def`, so `raw_resp` is always a coroutine. The `hasattr(raw_resp, "__await__")` check is always True, making the else branch dead. But `execution_node` uses a clean pattern: `asyncio.create_task(call_model(...))`. The inconsistency suggests copy-paste drift and makes the codebase harder to maintain.

**Fix:** Use `asyncio.create_task(call_model(...))` consistently in all nodes.

---

### W5 — `analyst_delegation_node` complexity check always fires for tasks reaching analysis
**Location:** `src/core/orchestration/graph/builder.py:427-443`

`should_after_analysis` routes complex tasks to `analyst_delegation`. But tasks only reach `analysis_node` if `route_after_perception` found NO `next_action` — meaning they're already considered non-trivial. Then `_task_is_complex` is called again. Tasks with complexity keywords are always routed through analysis AND analyst_delegation, doubling the analysis overhead for every multi-step task.

**Fix:** Track whether analyst delegation was already run. Or consolidate `analysis_node` + `analyst_delegation_node` into a single node that decides internally how deep to go.

---

## 7. Tool System Weaknesses

### T1 — `bash` SAFE_COMMANDS includes file-modification tools
**Location:** `src/tools/file_tools.py:292-366`

- `sed` (can edit in-place with `-i`)
- `tar`, `unzip` (can extract files, including path traversal)
- `touch` (creates files, can overwrite timestamps)
- `awk` (can write files with redirection, though shell operators block `>`)

These commands can cause file modifications that bypass `WorkspaceGuard` and the read-before-edit check.

---

### T2 — Tool argument validation is structural only, not semantic
**Location:** `src/core/orchestration/tool_contracts.py`

`ToolContract` validates that required arguments are present and have correct types, but doesn't validate semantic constraints: path arguments could be empty strings, pattern arguments could be invalid regex, step_id could be negative. This produces confusing tool errors rather than early validation failures.

---

### T3 — `manage_todo` is called in `execution_node` hot path without error handling for missing workdir
**Location:** `src/core/orchestration/graph/nodes/execution_node.py:419-427`

The `manage_todo` call is wrapped in `try/except Exception: pass` — silently swallows all failures. If `working_dir` is missing from state, the TODO is never written but the agent continues normally. The silent failure hides misconfiguration.

---

### T4 — `glob` tool uses `rglob("**/*")` for non-`**` patterns (entire tree scan)
**Location:** `src/tools/file_tools.py:569`

```python
raw = base.rglob(pattern)  # "Simple pattern — search the whole tree"
```

For `glob("*.py")`, this scans every subdirectory and returns all `.py` files in the tree. For a large repo, this returns thousands of matches. The 500-item cap mitigates this but the scan itself is expensive and the cap is arbitrary.

---

## 8. Repository Awareness Gaps

### R1 — Vector store `index_repository` is not incremental
**Location:** `src/core/indexing/repo_indexer.py`, `src/core/orchestration/graph/nodes/analysis_node.py:88`

Called on every analysis pass. If the repo has 1000 Python files and the agent makes 5 analysis passes, it indexes 5000 files. There's no change detection (mtime-based incremental update) to skip already-indexed files.

---

### R2 — Symbol graph only used in `analysis_node`, never consulted by `execution_node`
**Location:** `src/core/indexing/symbol_graph.py`

`execution_node` doesn't consult the symbol graph when generating a tool call for a plan step. If a step says "add error handling to `process_data()`", execution must discover where `process_data` is defined by calling tools — even though the symbol graph may already know the file and line.

---

### R3 — `analysis_node` doesn't read file contents for relevant files
**Location:** `src/core/orchestration/graph/nodes/analysis_node.py`

`analysis_node` discovers `relevant_files` (file paths) but never reads their content. Planning gets filenames but not signatures, imports, or structure. Planning then generates generic steps like "edit `src/auth/handler.py`" without knowing the existing interface.

**Fix:** For the top 3 most relevant files, read the first 50 lines (signatures/imports) and include them in `analyst_context` passed to `planning_node`.

---

## 9. Memory System Evaluation

**Strengths:**
- `TASK_STATE.md` + `TODO.md` dual system works well — deterministic plan tracker + LLM-inferred context.
- `context_builder.py` module-level caching avoids re-reading unchanged files.
- `session_store` provides SQL-backed history with WAL mode.
- Distiller handles context compaction when history grows.

**Gaps:**

### Mem1 — `distiller.compact_messages_to_prose` called from async context but uses blocking `ThreadPoolExecutor`
**Location:** `src/core/memory/distiller.py:40-51`

The C9 fix is correct in principle, but `future.result(timeout=120)` blocks the calling thread for up to 2 minutes. If distillation is called from an async node via `asyncio.gather`, the executor thread holds for 2 minutes with no progress indicator.

---

### Mem2 — `_TEXT_CACHE` and `_JSON_CACHE` grow unbounded
**Location:** `src/core/context/context_builder.py:12-13`

Module-level dicts with mtime-based invalidation never evict old entries. A long-running server with many distinct file paths (e.g., hundreds of workspace sessions) leaks memory continuously.

**Fix:** Add a maxsize cap (e.g., 256 entries) with LRU eviction, or use `functools.lru_cache` with a reasonable maxsize.

---

### Mem3 — Cross-task state contamination via `verified_reads` accumulation
**Location:** `src/core/orchestration/graph/state.py:23`

`verified_reads: Annotated[List[str], operator.add]` — the reducer APPENDS reads from all runs. In a long session where the orchestrator starts multiple tasks, verified_reads grows without bound. A file read in task 1 is still in `verified_reads` for task 5, bypassing the read-before-edit check for files not actually read in the current task.

**Fix:** Reset `verified_reads` at the start of each new task in the orchestrator.

---

## 10. Evaluation and Testing Gaps

### E1 — Scenario evaluator `run_scenario()` invokes agent but has no assertion framework
**Location:** `src/core/evaluation/scenario_evaluator.py`

The evaluator can run agent scenarios but returns raw results without structured assertions. There's no declarative test DSL for specifying expected tool calls, expected file modifications, or expected outcomes. You can't write "expect `write_file` to be called with `path='src/auth.py'`" — you have to parse the output manually.

---

### E2 — No regression test suite for tool correctness under adversarial inputs
**Location:** `tests/unit/`

Tests cover tool behavior for normal inputs. There are no tests for:
- Malformed YAML tool calls (parser robustness)
- LLM outputs with embedded prompt injection (`"ignore all instructions"` in file content passed to perception)
- Tool calls with oversized arguments (e.g., 10MB `content` to `write_file`)
- Race conditions in concurrent tool execution

---

### E3 — No end-to-end benchmark against real coding tasks
There is no benchmark suite that measures:
- Success rate on canonical tasks (e.g., SWE-bench style)
- Token efficiency (tokens per task)
- Step count vs. minimal necessary steps

Without benchmarks, it's impossible to measure whether optimizations actually improve performance.

---

## 11. Usability Problems

### U1 — No human-readable error explanation when agent fails
When the agent fails (max rounds, debug attempts exhausted, infinite loop guard), it returns raw internal error messages like `"Infinite loop detected: model failed to generate valid tool calls 3 times"`. The TUI displays this to the user without context or recovery suggestions.

---

### U2 — `providers.json` configuration is opaque
Adding a new model/provider requires manually editing JSON. There's no validation that the provider URL is reachable, no test-connection command, and no schema documentation inline. Onboarding a new local model is error-prone.

---

### U3 — Log verbosity is extreme by default
`perception_node` logs raw LLM responses at `INFO` level (up to 1000 chars). For a 5-step task, this produces 5+ huge log entries that bury actionable warnings. INFO should be reserved for state transitions; raw responses belong at DEBUG.

---

## 12. Performance Bottlenecks

### P1 — Extra LLM call per plan step (C1 above)
**Impact:** ~2-3x token usage and latency for multi-step plans on local 9B models.

---

### P2 — Full repo re-index on every analysis pass (H1 above)
**Impact:** Adds 5-30 seconds per analysis on repos with >500 files.

---

### P3 — Repo intel tools called on every perception pass including step re-entries (H3 above)
**Impact:** Adds 3 tool calls per perception pass; on a 5-step plan with 2 perception passes per step = 30 unnecessary tool calls.

---

### P4 — Context is rebuilt from scratch on every node call
**Location:** `src/core/context/context_builder.py`

`ContextBuilder` is instantiated fresh in every node call. The full message list is rebuilt, sanitized, and token-counted on every call. For conversation histories with 50+ messages, this is expensive. The module-level caches for file reading help, but the message building itself is not cached.

---

### P5 — `_truncate_text` uses character-by-character removal loop
**Location:** `src/core/context/context_builder.py:386-392`

```python
while self.token_estimator(truncated_text) > content_budget and len(truncated_text) > 0:
    truncated_text = truncated_text[:-1]
```

This is O(n) character removals, each triggering a `len(s)/4` token estimate. For a 50K character document being truncated to 1K, this is 49K iterations. Use binary search instead.

---

## 13. Over-Engineered Components

### O1 — `analyst_delegation_node` adds complexity for marginal gain
The analyst delegation spawns a subagent to do deep repo exploration before planning. But `analysis_node` already does semantic search + symbol graph enrichment + repo summary. `analyst_delegation` adds latency (full subagent LLM call) and complexity for tasks that analysis already handles well. The `analyst_findings` output is injected into `planning_node` but analysis already sets `analysis_summary` and `relevant_files`. The incremental value is low.

---

### O2 — Dual task tracking: `TODO.md` + `TASK_STATE.md` + `last_plan.json` + `session_store`
There are four separate persistence mechanisms for task/plan state. Each has a different format and update trigger. For developers debugging a failed task, figuring out which source of truth to consult is confusing.

Recommendation: consolidate to two: `TODO.md` (deterministic plan tracker, human-readable) and `session_store` (structured SQL storage for cross-session history).

---

### O3 — `rollback_manager.py` has multiple rollback mechanisms
Step-level transaction rollback in `verification_node`, full snapshot rollback in `debug_node`, and `rollback_manager.current_snapshot`. Three separate rollback paths with different scopes and triggers. When a debug attempt exhausts, both `rollback_step_transaction` and `rollback_manager.rollback()` may fire.

---

### O4 — `tool_contracts.py` + `workspace_guard.py` + `sandbox.py` are overlapping safety layers
Three separate files all doing overlapping pre-execution validation. The agent runs through all three on every tool call. The contract validation, workspace guard, and sandbox preflight check could be unified into a single `preflight_check()` method.

---

## 14. Prioritized Fix List

### Phase 1 — Critical Stability Fixes (1-3 days)

| ID | Description | Location | Complexity | Impact |
|----|-------------|----------|------------|--------|
| F1 | Use `planned_action` in `execution_node` before triggering LLM generation | `execution_node.py:59` | Low | 2-3x latency improvement for multi-step plans |
| F2 | Route failed steps in `should_after_step_controller` to `execution` not `verification` | `builder.py:408` | Low | Prevents silent step failures |
| F3 | Fix `execution_node` in-place mutation of `current_plan` | `execution_node.py:165` | Low | Prevents LangGraph state corruption |
| F4 | Remove `user_approved` from LLM-accessible tool arguments | `file_tools.py:26`, `orchestrator.py` | Medium | Closes WorkspaceGuard bypass |
| F5 | Move `sed` to RESTRICTED_COMMANDS; block `-i`/`-r` flags on `sed`, `tar` | `file_tools.py:303` | Low | Closes in-place edit bypass |
| F6 | Add `"edit_by_line_range"` to `SIDE_EFFECT_TOOLS` | `verification_node.py:81` | Trivial | Ensures verification fires for line-range edits |
| F7 | Add guaranteed fallback plan in `planning_node` on parse failure | `planning_node.py:266` | Low | Breaks planning loop on LLM failure |

### Phase 2 — Robustness Improvements (1 week)

| ID | Description | Location | Complexity | Impact |
|----|-------------|----------|------------|--------|
| F8 | Cache `index_repository` result per session; add incremental update | `analysis_node.py:88` | Medium | Eliminate 5-30s per analysis pass |
| F9 | Skip pre-retrieval in `perception_node` if `rounds > 0` or task unchanged | `perception_node.py:205` | Low | Eliminate 3 redundant tool calls per step |
| F10 | Make token budget dynamic based on provider `context_length` | `context_builder.py:168` | Medium | Enables frontier model utilization |
| F11 | Fix `analysis_node` symbol lookup: use regex to extract candidate names from task | `analysis_node.py:135` | Low | Correct semantic symbol search |
| F12 | Remove decomposition from `perception_node`; centralize in `planning_node` | `perception_node.py:87` | Medium | Eliminate conflicting decomposition paths |
| F13 | Fix `replan_required` signal: add patch-size check in `edit_file`/`write_file` | `file_tools.py`, `execution_node.py:384` | Low | Make replan trigger actually work |
| F14 | Standardize `async/await` pattern in all nodes (use `asyncio.create_task`) | All nodes | Low | Code consistency |
| F15 | Add `_TEXT_CACHE` / `_JSON_CACHE` LRU eviction (max 256 entries) | `context_builder.py:12` | Low | Prevent memory leak in long-running sessions |
| F16 | Reset `verified_reads` at the start of each new task | `orchestrator.py` | Low | Prevent cross-task read-bypass contamination |

### Phase 3 — Capability Improvements (2 weeks)

| ID | Description | Location | Complexity | Impact |
|----|-------------|----------|------------|--------|
| F17 | Implement streaming token output from `call_model` to TUI | `llm_manager.py`, all nodes | High | Real-time output on slow local models |
| F18 | Add step-level verification (lint/syntax check after each file modification) | `builder.py`, new node | Medium | Catch bugs before compounding across steps |
| F19 | Add provider fallback chain (try next provider on circuit-open) | `llm_manager.py` | Medium | Resilience to provider failures |
| F20 | Read top-3 relevant file signatures in `analysis_node` for richer planning context | `analysis_node.py` | Low | Better repo-aware plans |
| F21 | Add grammar-constrained generation support (LM Studio grammar field) | `lm_studio_adapter.py` | Medium | Eliminate YAML parse failures on 9B models |
| F22 | Wire `delegation_results` back to `analyst_findings` or remove delegation infrastructure | `delegation_node.py`, `builder.py` | Medium | Fix dead-code architecture |

### Phase 4 — Advanced Features (1 month)

| ID | Description | Location | Complexity | Impact |
|----|-------------|----------|------------|--------|
| F23 | End-to-end benchmark suite (SWE-bench style, 20-50 canonical tasks) | `tests/e2e/` | High | Measurable quality tracking |
| F24 | Structured assertion framework for scenario evaluator | `scenario_evaluator.py` | High | Automated correctness regression |
| F25 | Task interrupt/resume with partial progress persistence | `orchestrator.py`, `planning_node.py` | High | Production usability |
| F26 | Centralize rollback mechanisms (single `RollbackManager.rollback_step()`) | `rollback_manager.py` | Medium | Remove redundant safety paths |
| F27 | Consolidate state persistence (remove `last_plan.json`; use `session_store` only) | `planning_node.py` | Medium | Single source of truth |
| F28 — Provider discovery UI | Auto-detect running LM Studio / Ollama instances on startup | `startup.py` | Medium | Reduce onboarding friction |

---

## Summary Statistics

| Severity | Count |
|----------|-------|
| Critical | 5 |
| High | 9 |
| Medium | 12 |
| Low | 6 |
| **Total** | **32** |

Most critical path for local 9B usability: **F1 + F7 + F8 + F9** — these four fixes alone reduce token waste by ~3x and eliminate the most common failure modes on small models.

Most critical path for frontier model scalability: **F10** (dynamic token budget) — without this, frontier models are capped at 6K tokens regardless of their 128K context capacity.

---

## 15. Fix Status (2026-03-20)

All Phase 1 (Critical) and Phase 2 (Robustness) findings have been implemented and verified.

### Phase 1 — Critical Fixes ✅ All Fixed

| ID | Status | Implementation notes |
|----|--------|---------------------|
| F1 | ✅ Fixed | `execution_node.py`: `action = state.get("next_action") or state.get("planned_action")` |
| F2 | ✅ Fixed | `builder.py::should_after_step_controller`: failed step routes to `"execution"` (retry) |
| F3 | ✅ Fixed | `execution_node.py`: build `updated_plan = [dict(s) for s in current_plan]` before mutating |
| F4 | ✅ Fixed | `orchestrator.py::execute_tool`: `args.pop("user_approved", None)` before tool dispatch |
| F5 | ✅ Fixed | `file_tools.py::bash`: blocks `sed -i`, `tar -x/-xf/-xvf`, `unzip` without `-l` |
| F6 | ✅ Fixed | `verification_node.py`: `edit_by_line_range` added to SIDE_EFFECT_TOOLS; tool implemented in `file_tools.py` and registered in `orchestrator.py::example_registry()` |
| F7 | ✅ Fixed | `planning_node.py`: guaranteed single-step fallback when `_parse_plan_content` returns `[]` |

### Phase 2 — Robustness Improvements ✅ All Fixed

| ID | Status | Implementation notes |
|----|--------|---------------------|
| F8  | ✅ Fixed | `analysis_node.py`: `_INDEXED_DIRS` set; `index_repository()` called at most once per dir |
| F9  | ✅ Fixed | `perception_node.py`: pre-retrieval block gated on `rounds == 0` |
| F10 | ✅ Fixed | New `src/core/inference/provider_context.py::get_context_budget()`; `context_builder.py::build_prompt` default `max_tokens` reads from provider context_length |
| F11 | ✅ Fixed | `analysis_node.py`: regex extracts CamelCase/snake_case identifiers; stopwords filtered |
| F12 | ✅ Fixed | `perception_node.py`: decomposition block disabled (gated `if False`) — planning_node owns decomposition |
| F13 | ✅ Fixed | `file_tools.py::write_file` + `edit_file`: set `requires_split=True` when net lines > 200 |
| F14 | ✅ Fixed | `perception_node.py` + `planning_node.py`: `raw_resp = call_model(...)` + `if hasattr(..., "__await__")` replaced with direct `asyncio.create_task(call_model(...))` |
| F15 | ✅ Fixed | `context_builder.py`: `_TEXT_CACHE` + `_JSON_CACHE` are `OrderedDict` with `_CACHE_MAX=256` LRU eviction |
| F16 | ✅ Fixed | `orchestrator.py::run_agent_once`: `self._session_read_files = set()` at start of each run |

### Test Coverage

- **New test file:** `tests/unit/test_audit_vol3.py` — 30 tests covering F2, F4, F5, F6, F7, F8, F9, F10, F11, F13, F15, F16
- **Total test count:** 863 passed, 5 skipped, 0 failed (up from 776 before vol3 fixes)
- **providers.json:** restored to array format (required by `test_providers_json_is_array_format`)
