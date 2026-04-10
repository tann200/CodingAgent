# Agent Loop Improvement Analysis

**Scope:** Comparison of `opencode` (TypeScript, Vercel AI SDK) and the local `CodingAgent` (Python, LangGraph)  
**Problem:** Simple tasks cause the agent to enter loops and consume excessive tokens  
**Date:** April 2026

---

## 1. Root Causes of Looping and Token Waste

### 1.1 No Hard Step Cap With Real Teeth

**CodingAgent (`inference_loop.py`):**  
The outer loop allows `max_rounds=12`, each with `recursion_limit=50`. Inside a single round the LangGraph graph can visit: `perception_node → execution_node → step_controller_node → verification_node → debug_node → replan_node → evaluation_node` — and each of those can cycle back. With 12 rounds × 50 recursion steps, a single user request can trigger **up to 600 graph traversals** before any hard stop. None of the individual nodes enforce a global turn budget.

**opencode (`session/prompt.ts`):**  
`runLoop` is a `while (true)` loop. The `agent.steps` configuration provides a soft limit: when `step >= maxSteps`, a `MAX_STEPS` reminder is injected into the message but the loop is **not stopped** — the LLM is merely told it is running out of steps. This is advisory only and a sufficiently confused model will ignore it.

**Improvement:**  
Implement a hard, non-bypassable global token budget and step ceiling. When either is exceeded, the session terminates with a `budget_exceeded` reason, not merely a reminder. Separate "operational" limits (per-turn tool count) from "session" limits (total turns).

---

### 1.2 Doom-Loop Detection is Too Narrow

**opencode (`session/processor.ts`):**  
Doom-loop detection checks the last 3 consecutive tool calls in **the current assistant message only**, and requires the tool name AND input to be **byte-identical**. A loop where the LLM alternates between two tools, or uses slightly different arguments each time (e.g., reading the same file with different line offsets), is completely invisible to this check.

**CodingAgent (`loop_guards.py`):**  
Has a cooldown mechanism per tool call, but `debug_node` resets the per-error-type counter each time a new error type is encountered. With 6 error types, up to 18 separate debug LLM calls can occur before the `MAX_TOTAL_DEBUG=9` hard cap fires (whichever is lower). The per-type reset creates a bypass path.

**Common Pattern — The "Read → Edit → Read Again" Loop:**  
Both systems allow the agent to read a file it already read in the same session, re-read it after editing, verify the edit by reading again, and then re-read to "confirm" before the next step. Each re-read is a redundant tool call that injects potentially thousands of tokens of duplicate content back into the context.

**Improvements:**
- Broaden doom-loop detection to track tool calls across the **entire session**, not just the current message.
- Detect semantic equivalence: reading file `X` with offset 0 and reading file `X` with offset 1 should count as the same intent.
- Track a "tool call fingerprint" set per session: `hash(tool_name + canonical_args)`. Repeated fingerprints within a sliding window of N turns trigger escalating warnings, then a hard stop.
- For read operations specifically: once a file is read and its content is in context (or was in context and hasn't changed on disk), block re-reads unless the file was modified since the last read.

---

### 1.3 System Prompt Rebuilt Every Turn

**opencode (`session/llm.ts`, `session/prompt.ts`):**  
`SystemPrompt.environment()` is called on every LLM call and regenerates: model name, working directory, workspace root, current git status, platform, and `new Date().toDateString()`. Git status in particular is a shell-exec that can return many lines of diff output. This means every turn pays the token cost of the environment block even when nothing has changed.

**CodingAgent (`context_builder.py`, `agent_brain.py`):**  
The system prompt is injected twice per turn: once from `SOUL.md` inside `agent_brain.py` and once reconstructed by `context_builder.py`. The duplication means every LLM call carries 2× the base system prompt token cost. Additionally, `_build_system_message()` uses an O(N) character-level shrink loop (remove one character, re-tokenise, repeat) that can invoke the tokeniser tens of thousands of times for a large file.

**Improvements:**
- Cache the environment block across turns. Only regenerate when a relevant fact changes (directory, git head, date crossing midnight).
- Deduplicate system prompt injection. There must be a single authoritative assembly point.
- Replace the O(N) character-shrink loop with a binary-search or chunk-level approach: measure in 500-token chunks, not one character at a time.
- Mark static system prompt sections with provider cache-control headers (Anthropic supports `cache_control: {"type": "ephemeral"}` on system blocks) to reduce cost on repeated calls.

---

### 1.4 Context Grows Unboundedly Before Compaction

**opencode:**  
Compaction triggers only when `totalTokens >= model.limit.input - buffer`. For a model with a 200k context window, the agent can accumulate 180k+ tokens of conversation history before any summarisation fires. By then, the model is operating in a degraded state (known attention quality issues at large contexts) and the compaction summary call itself costs another full-context LLM invocation.

**CodingAgent:**  
`context_builder.py` truncates aggressively but the truncation is character-level (see above). There is no proactive pruning — old tool outputs stay in context until manually truncated, which means a session that reads 20 large files will carry all of them throughout the rest of the conversation.

**Improvements:**
- Implement proactive context management: after each tool result, estimate whether the result will still be needed 3+ turns from now. If not, mark it for pruning immediately rather than waiting for overflow.
- opencode's `compaction.prune()` (which prunes tool outputs beyond the last 40k tokens) is a good model — backport this approach to CodingAgent.
- For CodingAgent, implement a "sliding window" over conversation history that retains: the system prompt, the original task description, the last N assistant+user exchanges, and any tool results referenced in the current plan step. Everything else is summarised or dropped.
- For simple tasks (no plan, single-step execution), skip the full conversation history injection entirely and use only: system prompt + task + last tool result.

---

### 1.5 Extra LLM Calls on Every Task Completion

**CodingAgent (`evaluation_node.py`):**  
When rule-based checks say the task is complete, a **second LLM call** (`call_model()`, "WF-2 semantic verdict") fires to confirm. This semantic check uses a minimal 2-message prompt with no conversation history, so it can contradict the rule-based verdict by judging the plan alone without seeing actual file changes. If `get_active_settings()` fails to import (e.g. misconfiguration), this defaults to enabled — meaning the extra call fires on every task completion under any configuration failure.

**CodingAgent (`verification_node.py`):**  
When `current_plan` is empty (fast-path, no-plan execution), `at_final_step = True` on every turn. This triggers a full `pytest` suite run after **every single tool call**, not just the last one. A 5-tool no-plan task therefore runs pytest 5 times.

**Improvements:**
- Gate the semantic evaluation LLM call behind an explicit feature flag that defaults to **disabled**. Enable it only when the user opts in.
- Fix `at_final_step` to be `False` unless we are genuinely at the last step of a plan or at the explicit end of a no-plan sequence. A no-plan task should not treat every step as the final step.
- Merge the rule-based and semantic checks: run semantic only when rule-based gives an ambiguous result (partial completion), not on every clear-success.

---

### 1.6 Planning Overhead for Simple Tasks

**CodingAgent (`planning_node.py`):**  
Every task goes through a planning LLM call. For a task like "add a comment to function X" or "what does this variable do?", a full plan is generated, stored, and then a second LLM call is made for execution. The planning call uses `ContextBuilder` fresh each time, which involves disk I/O and tokenisation. Additionally, cross-session plan resume uses 80% Jaccard word overlap — a heuristic prone to false positives that can resume a prior plan for a semantically different task.

**opencode:**  
No mandatory planning phase. Simple tasks go directly to execution. Agents can optionally use `todowrite` but it is not forced.

**Improvements:**
- Classify task complexity before planning. Simple tasks (single file read, single question, single edit) should bypass the planning node entirely and route directly to execution.
- The complexity classifier can itself be rule-based (no LLM): if the task description matches a single-action pattern (verb + file/function name, no conjunctions, no conditionals), skip planning.
- If planning is triggered, cap the plan to a maximum of N steps. A plan with more than 10 steps is almost certainly over-engineered for most coding tasks and should trigger user confirmation.
- Replace the Jaccard-based cross-session resume with an explicit user opt-in (`--resume`) or a shorter TTL (e.g. only resume plans created in the last 30 minutes in the same working directory).

---

### 1.7 Execution Node Redundancy and False Re-Reads

**CodingAgent (`execution_node.py`):**  
The read-before-write (RBW) guard is checked in **two separate places** with asymmetric error injection. When the `tool_execution_pipeline` blocks a write, no `history` message is appended to the LangGraph state — the LLM never receives the "you must read first" correction. The agent then retries the write, gets blocked again silently, and retries again. This creates a micro-loop that is invisible to doom-loop detection.

Additionally, `manage_todo` modifies `TODO.md` on disk without updating `_session_read_files`, so `TODO.md` bypasses the RBW guard. The agent may write to `TODO.md` without reading it first, get a stale view on the next read, and loop.

**Improvements:**
- Unify the RBW check to a single location. The pipeline should always inject a `history` message on block, not just return a bare dict.
- Add `TODO.md` and other agent-internal files to `_session_read_files` automatically at session start (pre-populate the set with known files the agent will write to).
- Log every RBW block as a structured event visible to the loop guard logic — treat repeated RBW blocks on the same file as a loop signal.

---

### 1.8 Debug Node Can Multiply LLM Calls

**CodingAgent (`debug_node.py`):**  
The debug loop resets its per-error-type counter when the error type changes. With `max_attempts=3` per type and 6 possible types (`syntax_error`, `import_error`, `test_failure`, `lint_error`, `runtime_error`, `unknown_error`), up to 18 debug LLM calls can occur before the hard `MAX_TOTAL_DEBUG=9` cap fires. The hard cap should be the binding constraint but the per-type reset is a latent path to bypass it if the cap is ever raised or if new error types are added.

**Improvement:**  
Remove the per-error-type counter reset entirely. Use only `total_debug_attempts` with a single hard cap. The error type should be used for routing (what kind of fix to attempt), not for resetting retry budgets.

---

## 2. Architectural Patterns Worth Adopting

### 2.1 opencode: Event-Driven Streaming Over Polling

opencode's processor consumes a **live stream of typed events** from the LLM. Tool calls are dispatched as they arrive, not after the full response is buffered. This means:
- Latency to first tool result is minimised
- The loop can short-circuit mid-stream if overflow is detected (`Stream.takeUntil(() => ctx.needsCompaction)`)
- Doom-loop detection fires before the model finishes its current message

CodingAgent uses a request/response model — it waits for the full LLM response before dispatching tools. For simple tasks this doesn't matter, but for multi-tool turns it adds latency and prevents early exit.

**Recommendation:** Adopt streaming tool dispatch in CodingAgent. Dispatch each tool as its `tool_call` delta completes, accumulating results in parallel while the model continues generating. This enables early-exit when a loop is detected mid-generation.

### 2.2 opencode: Subagent Isolation via Child Sessions

opencode's `task` tool creates a fully isolated child session with its own message history, permission set, and LLM call budget. The parent agent gets back a single text summary. This means:
- Subagent token consumption is bounded and separate
- Subagent loops cannot contaminate the parent context
- Multiple subagents can run in parallel without sharing state

CodingAgent's delegation nodes share the same orchestrator state, meaning a looping delegated agent inflates the parent session's token count and step counters.

**Recommendation:** Isolate subagent execution behind a clean interface: subagent gets a task description and returns a result string. No shared state, no shared context window.

### 2.3 opencode: Permission Gates as Circuit Breakers

Every destructive tool call in opencode goes through `permission.ask()`, which can be pre-configured as `"allow"`, `"deny"`, or `"ask"`. The doom-loop handler also routes through `permission.ask("doom_loop")` — the same circuit-breaker mechanism used for destructive operations. This makes loop interruption consistent with the rest of the permission model.

CodingAgent's loop guards (`loop_guards.py`) are separate from the tool permission/approval system. A loop detected by the guard sets a flag, but the flag check is in the orchestrator, not inline with tool dispatch.

**Recommendation:** Unify loop detection with the tool permission system. Every tool call should go through a single gate that checks: (a) is this tool allowed? (b) is this call a loop signal? (c) is there remaining budget? A single "gate" function is easier to audit, test, and tune than scattered checks across multiple nodes.

---

## 3. Implementation Plan

Each fix below includes the exact file path, line numbers, the current code, the replacement code, and a rationale. Fixes are grouped by priority tier.

---

### P0 — Fix Immediately (Bugs Causing Loops Today)

---

#### P0-A: Fix `at_final_step` — redundant pytest on every no-plan tool call

**File:** `src/core/orchestration/graph/nodes/verification_node.py`  
**Lines:** 132–136

**Current code:**
```python
current_plan = state.get("current_plan") or []
current_step = int(state.get("current_step") or 0)
at_final_step = (not current_plan) or (current_step >= len(current_plan) - 1)
step_requests_verification = _step_requests_verification(state)
run_full_suite = at_final_step or step_requests_verification
```

**Problem:** `at_final_step` is `True` whenever `current_plan` is empty, which is the normal state for fast-path no-plan tasks. Every single tool call on a no-plan task therefore triggers a full pytest run. For a task that calls 5 tools, pytest runs 5 times instead of once.

**Fix:** Only set `at_final_step = True` for no-plan tasks if there is an explicit signal that execution is complete — either `evaluation_result` is set to `"complete"`, or the action being verified is `None` (no further tool calls pending):

```python
current_plan = state.get("current_plan") or []
current_step = int(state.get("current_step") or 0)
evaluation_done = state.get("evaluation_result") in ("complete", "pass")
next_action = state.get("next_action")
if current_plan:
    at_final_step = current_step >= len(current_plan) - 1
else:
    # No-plan fast path: only treat as final if execution has concluded
    at_final_step = evaluation_done or (next_action is None)
step_requests_verification = _step_requests_verification(state)
run_full_suite = at_final_step or step_requests_verification
```

**Estimated impact:** Eliminates N−1 redundant pytest runs on every fast-path task. A 5-tool no-plan task drops from 5 full test suite runs to 1.

---

#### P0-B: Unify the RBW guard — pipeline must inject a `history` message on block

**Files:**
- `src/core/orchestration/graph/nodes/execution_node.py` — lines 464–472 (Check #1: injects history, correct)
- `src/core/orchestration/tool_execution_pipeline.py` — lines 128–146 (Check #2: returns bare dict, incorrect)

**Problem:** When the pipeline's RBW check fires (Check #2), it returns `{"error": "..."}` with no `history` key. The LLM never receives the "you must read first" correction. On the next turn, `next_action` still points to the same write, Check #2 fires again, and the micro-loop repeats silently until a doom-loop guard eventually fires.

**Current code in `tool_execution_pipeline.py` (lines 134–146):**
```python
# Check #2 in pipeline
if tool_name in MODIFYING_TOOLS:
    path_arg = _extract_path_arg(tool_name, args)
    if path_arg:
        rbw_err = check_read_before_write(
            tool_name, path_arg, state, working_dir, orch._session_read_files
        )
        if rbw_err:
            return rbw_err   # ← bare dict, no "history" key
```

**Fix:** When Check #2 fires, format the error as a proper history entry so the LLM sees it:

```python
if tool_name in MODIFYING_TOOLS:
    path_arg = _extract_path_arg(tool_name, args)
    if path_arg:
        rbw_err = check_read_before_write(
            tool_name, path_arg, state, working_dir, orch._session_read_files
        )
        if rbw_err:
            error_msg = rbw_err.get("error", "Read-before-write guard blocked this operation.")
            return {
                **rbw_err,
                "history": [{"role": "tool", "content": error_msg, "tool_use_id": tool_call_id}],
                "last_result": {"error": error_msg},
            }
```

Additionally, remove Check #1 from `execution_node.py` (lines 464–472) once the pipeline version correctly injects history. Having two checks creates the divergence; the pipeline check should be the single authoritative gate since it is closer to the actual tool execution. If removal of Check #1 is considered risky initially, add an assertion that verifies both checks agree on the same files set.

**Estimated impact:** Eliminates the silent RBW micro-loop. The LLM will receive the correction on the first block and self-correct rather than retrying blindly.

---

### P1 — High Impact, Low Risk (One-Day Changes)

---

#### P1-A: Gate the semantic evaluation LLM call behind an explicit opt-in flag

**File:** `src/core/orchestration/graph/nodes/evaluation_node.py`  
**Lines:** 139–195

**Current code (lines 139–149):**
```python
_semantic_enabled = True   # ← defaults to True
try:
    from src.core.orchestration.project_settings import (
        get_active_settings as _gas,
    )
    _ps = _gas()
    if _ps is not None:
        _semantic_enabled = _ps.enable_semantic_evaluation
except Exception:
    pass               # ← on any import/settings failure, stays True
```

**Problem:** The flag defaults to `True` and is only lowered by a successful settings load. Any misconfiguration, missing settings key, or import error leaves semantic evaluation permanently enabled. This fires an extra LLM call on every task completion under degraded configurations.

**Fix:** Invert the default to `False`. The feature should require explicit enablement:

```python
_semantic_enabled = False   # ← default off; must be explicitly enabled
try:
    from src.core.orchestration.project_settings import (
        get_active_settings as _gas,
    )
    _ps = _gas()
    if _ps is not None:
        _semantic_enabled = bool(_ps.enable_semantic_evaluation)
except Exception:
    pass   # stays False on any failure
```

Additionally, update `project_settings.py` to ensure `enable_semantic_evaluation` defaults to `False` in the `ProjectSettings` model/dataclass definition so that a fresh install does not accidentally enable it.

**Estimated impact:** Saves 1 LLM call per task completion for all users not explicitly using semantic evaluation. For a session with 10 completed tasks, this removes 10 otherwise-unnecessary LLM calls.

---

#### P1-B: Remove per-error-type debug counter reset

**File:** `src/core/orchestration/graph/nodes/debug_node.py`  
**Lines:** 106–117

**Current code:**
```python
# Line 106
next_attempt = current_attempt + 1
# Line 107
next_total = total_debug_attempts + 1
# ...
# Line 112 — comment
# W6 fix: reset attempt counter when error type changes between debug cycles
# Line 113
if last_error_type and error_type != last_error_type:
    logger.info(
        f"debug_node: error type changed {last_error_type!r} → {error_type!r}, resetting attempt counter"
    )
    current_attempt = 0   # ← Line 117: resets local var but next_attempt already computed above
```

**Problem 1:** The reset at line 117 sets `current_attempt = 0`, but `next_attempt` was already computed at line 106 as `current_attempt + 1`. The reset therefore has no effect on what gets persisted — `next_attempt` still increments. This is a latent bug: the intent was to allow 3 more attempts after a type change, but it only resets the display/logging value.

**Problem 2 (architectural):** Even if the reset were effective, it would allow the debug node to cycle through 6 error types × 3 attempts = 18 LLM calls before `MAX_TOTAL_DEBUG` (9) stops it. The per-type reset creates a bypass pathway whenever the cap is raised or new error types are added.

**Fix:** Remove the reset block entirely. Use only `total_debug_attempts` as the binding constraint. The error type is used only for routing what kind of fix to attempt, not for resetting budgets:

```python
next_attempt = current_attempt + 1
next_total = total_debug_attempts + 1
# Remove lines 112–117 entirely.
# Error type routing happens downstream; no counter reset needed.
```

Update return dicts at lines 220–225 and 229–234 to no longer persist `debug_attempts` separately from `total_debug_attempts`, or keep it as an alias but do not use it as a cap:

```python
return {
    "next_action": tool_call,
    "debug_attempts": next_total,          # alias to total — single source of truth
    "total_debug_attempts": next_total,
    "last_debug_error_type": error_type,
}
```

**Estimated impact:** The debug loop is now strictly bounded at `MAX_TOTAL_DEBUG` (currently 9) LLM calls per task, regardless of how many error type transitions occur.

---

#### P1-C: Deduplicate system prompt injection — single assembly point

**Files:**
- `src/core/context/context_builder.py` — lines 484–756: builds and injects SOUL.md content at lines 488–492
- `src/core/orchestration/agent_brain.py` — lines 229–233: `AgentBrainManager.compile_system_prompt()` also appends SOUL.md content under `<operating_principles>`

**Problem:** SOUL.md is loaded by `context_builder.py` at line 123 (`self.soul = self._read_text_cached(soul_path)`) and injected into the system prompt at line 492 inside `<identity>` tags. It is also loaded by `AgentBrainManager._load_all()` at lines 90–97 and injected by `compile_system_prompt()` at lines 229–233 inside `<operating_principles>` tags. Every LLM call pays for SOUL.md content twice under different XML tags.

**Fix:**

Step 1 — Make `ContextBuilder` the single owner of system prompt assembly. Remove the SOUL.md injection from `AgentBrainManager.compile_system_prompt()` (lines 229–233):

```python
# REMOVE this block from agent_brain.py compile_system_prompt():
# soul = self.get_identity("soul")
# if soul:
#     parts.append("\n<operating_principles>")
#     parts.append(soul)
#     parts.append("</operating_principles>")
```

Step 2 — If `AgentBrainManager.compile_system_prompt()` output is still being concatenated with `ContextBuilder`'s system prompt somewhere upstream (check all callers of `compile_system_prompt()`), remove the concatenation or ensure only one is used per call.

Step 3 — Add an assertion in the test suite that verifies the assembled system prompt does not contain the SOUL.md identity block more than once.

**Estimated impact:** Halves the system prompt token cost for the identity/principles section. For a SOUL.md of ~500 tokens, this saves ~500 tokens × 2 (input + output attention) on every LLM call in the session.

---

#### P1-D: Replace O(N) character-shrink loop with chunk-level binary search

**File:** `src/core/context/context_builder.py`

**Location 1 — `_truncate_text()`, lines 856–860:**
```python
while (
    self.token_estimator(truncated_text) > content_budget_for_truncation
    and len(truncated_text) > 0
):
    truncated_text = truncated_text[:-1]   # ← removes 1 char, re-tokenises
```

**Location 2 — `_build_system_message()`, lines 931–932:**
```python
while not test_fit(truncated_content) and len(truncated_content) > 0:
    truncated_content = truncated_content[:-1]   # ← same O(N) pattern
```

**Problem:** For a 50,000-character document that is 100 tokens over budget, this loop invokes the tokeniser ~100 times (each removal sheds ~1 token for typical prose). But for a document that is 5,000 tokens over budget, the loop sheds one character per iteration and may need to call the tokeniser 10,000+ times before converging.

**Fix:** Replace both loops with a chunk-level binary search. Token estimates are approximately linear with character count for typical code/prose, so binary search on character index converges in O(log N) tokeniser calls:

```python
def _truncate_to_budget(self, text: str, budget: int) -> str:
    """Binary-search truncation. O(log N) tokeniser calls instead of O(N)."""
    if self.token_estimator(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if self.token_estimator(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid
    return text[:lo]
```

Replace both loop sites with calls to `self._truncate_to_budget(text, budget)`.

**Estimated impact:** For a 50k-character file that is 1,000 tokens over budget, token estimator calls drop from ~1,000 to ~17 (log₂(50,000) ≈ 16). For a 200k-character file, the improvement is ~5,000× fewer tokeniser calls per truncation invocation.

---

### P2 — Medium Impact, Moderate Effort (Multi-Day Changes)

---

#### P2-A: Add task complexity classifier to bypass planning for simple tasks

**File:** `src/core/orchestration/graph/builder.py` — routing after `perception`  
**Current entry point:** `workflow.set_entry_point("perception")` (line 867)  
**Current routing:** `route_after_perception` (lines 282–346) routes to `"analysis"` → `"planning"` for all non-execution states

**Problem:** Every task routes to `planning_node` unless `next_action` is already set (continuation) or the task is already in progress. A task like "what does this function do?" or "add a docstring to function X" triggers a full planning LLM call generating a multi-step plan, then executes it — all for what should be a 1–2 tool call response.

**Fix:** Insert a rule-based complexity classifier between `perception` and `analysis`. The classifier inspects the task description without an LLM call and decides: `"simple"` (route directly to `execution`) or `"complex"` (route to `analysis` → `planning` as today).

**New function to add in `builder.py` or a new `src/core/orchestration/task_classifier.py`:**

```python
import re

# Patterns that indicate a single-action task requiring no plan
_SIMPLE_PATTERNS = [
    r"^(what|explain|describe|summarise|summarize)\b.{0,80}$",   # explanation requests
    r"^(show|print|display|list)\b.{0,60}$",                     # display requests
    r"^add (a |an )?(docstring|comment|type hint)\b",            # single annotation
    r"^rename (function|variable|class|method)\b",               # single rename
    r"^(fix|correct) (the )?(typo|spelling)\b",                  # trivial text fix
    r"^(read|open|show me)\b.{0,40}\.(py|ts|js|md|txt|json)\b", # read one file
]
_SIMPLE_RE = re.compile("|".join(_SIMPLE_PATTERNS), re.IGNORECASE)

# Patterns that always require a plan
_COMPLEX_MARKERS = [
    " and ", " then ", " also ", " additionally ", " multiple ",
    "refactor", "migrate", "implement", "create", "build", "add feature",
]

def classify_task_complexity(task: str) -> str:
    """Returns 'simple' or 'complex'. No LLM call."""
    if not task or len(task) > 300:
        return "complex"
    task_lower = task.lower().strip()
    if any(marker in task_lower for marker in _COMPLEX_MARKERS):
        return "complex"
    if _SIMPLE_RE.match(task.strip()):
        return "simple"
    # Default: treat as complex to avoid under-planning
    return "complex"
```

**Route change in `route_after_perception` (lines 282–346):**

```python
def route_after_perception(state):
    # ... existing continuation/next_action checks ...
    task = state.get("task", "")
    complexity = state.get("task_complexity") or classify_task_complexity(task)
    if complexity == "simple":
        return "execution"   # bypass analysis and planning entirely
    return "analysis"
```

**Graph wiring change (after line 867):**
```python
workflow.add_conditional_edges(
    "perception",
    route_after_perception,
    {
        "execution": "execution",
        "analysis": "analysis",
        "memory_sync": "memory_sync",
    },
)
```

**Safeguard:** If a "simple" task's execution fails or returns `replan_required = True`, route to `analysis` at that point. The fast-path exit is only for the initial routing; the graph still handles escalation naturally.

**Estimated impact:** Eliminates the planning LLM call for an estimated 40–60% of user tasks (all single-action requests). Reduces average turns for simple tasks from ~5 (plan + perceive + execute + verify + evaluate) to ~2 (perceive + execute).

---

#### P2-B: Replace Jaccard cross-session plan resume with explicit opt-in

**File:** `src/core/orchestration/graph/nodes/planning_node.py`  
**Lines:** `_load_last_plan()` function — called at top of every `planning_node` invocation  
**Current behaviour:** Resumes a prior plan if word-overlap between current task and prior plan's task field is ≥ 80% (Jaccard similarity)

**Problem:** Jaccard word overlap is not a reliable proxy for semantic similarity. "Fix the login bug in auth.py" vs "Test the login bug in auth.py" shares high overlap but requires a completely different plan. False resumptions inherit stale plans that may reference files that no longer exist, steps that are already done, or a wrong approach.

**Fix:**

Step 1 — Add a TTL to saved plans. Only consider resuming a plan if it was saved within the last 30 minutes AND the working directory is identical:

```python
import time

def _load_last_plan(task: str, working_dir: str) -> Optional[dict]:
    plan = _read_persisted_plan()
    if not plan:
        return None
    # TTL check: only resume within 30 minutes
    saved_at = plan.get("saved_at", 0)
    if time.time() - saved_at > 1800:
        return None
    # Working directory must match exactly
    if plan.get("working_dir") != working_dir:
        return None
    # Remove Jaccard check entirely; rely on TTL + explicit --resume flag
    return plan
```

Step 2 — Honour an explicit `resume_session` flag in the agent state (set by the user passing `--resume` or a TUI action) that bypasses the TTL:

```python
if state.get("resume_session"):
    return _read_persisted_plan()   # explicit opt-in, no TTL
```

Step 3 — When saving a plan to disk, always record `saved_at` and `working_dir`:

```python
plan_record = {
    **plan,
    "saved_at": time.time(),
    "working_dir": str(working_dir),
}
_write_persisted_plan(plan_record)
```

**Estimated impact:** Eliminates false plan resumptions that cause the agent to continue an irrelevant prior plan. The 80% Jaccard threshold was a heuristic with no empirical basis; the TTL + explicit opt-in approach is both safer and more predictable.

---

#### P2-C: Pre-populate `_session_read_files` with agent-internal files

**File:** `src/core/orchestration/orchestrator.py` (or wherever `_session_read_files` is initialised at session start)

**Problem:** `manage_todo` in `execution_node.py` (lines 1005–1013) modifies `TODO.md` on disk without updating `_session_read_files`. On a subsequent turn, if the agent tries to write `TODO.md` again, the RBW guard fires — but the previous write wasn't tracked, so the guard's view of what was last read is stale, potentially blocking legitimate writes or allowing stale reads.

**Fix:** At session initialisation, pre-populate `_session_read_files` with all agent-internal files that the orchestrator is allowed to write without an explicit prior read:

```python
# In Orchestrator.__init__ or session_start():
_AGENT_INTERNAL_FILES = {"TODO.md", ".agent-context/state.json", "PLAN.md"}

def _init_session_read_files(self) -> set:
    """Pre-populate with agent-internal files to avoid spurious RBW blocks."""
    pre_read = set()
    for fname in _AGENT_INTERNAL_FILES:
        fpath = Path(self.working_dir) / fname
        if fpath.exists():
            pre_read.add(str(fpath.resolve()))
    return pre_read

self._session_read_files = self._init_session_read_files()
```

Also update `manage_todo` to explicitly add `TODO.md` to `_session_read_files` after modifying it:

```python
# After manage_todo() call in execution_node.py (line 1013):
todo_path = str((Path(working_dir) / "TODO.md").resolve())
if orchestrator and hasattr(orchestrator, "_session_read_files"):
    orchestrator._session_read_files.add(todo_path)
```

**Estimated impact:** Prevents the `TODO.md` RBW bypass loop. Also prevents false RBW blocks on plan and state files that the agent legitimately owns.

---

#### P2-D: Broaden doom-loop detection to cross-message tool fingerprinting

**File:** `src/core/orchestration/loop_guards.py`  
**Current doom-loop check (lines 238–350):** Uses a `RECENT_CALLS_WINDOW = 10` ring buffer of call fingerprints, but the fingerprint includes the full serialised args JSON, so `read_file("foo.py", offset=0)` and `read_file("foo.py", offset=10)` are treated as different calls.

**Problem:** The most common real-world loop is not "exact same call 3 times" but rather:
- Alternating reads: `read_file(foo.py, 0)` → `read_file(foo.py, 100)` → `read_file(foo.py, 200)` (reading the same file in increments when the agent already has the full content)
- Tool alternation: `read_file` → `grep` → `read_file` → `grep` on the same target, cycling without progress

**Fix — Part 1: Canonical fingerprints for read operations**

In `check_doom_loop()` (line 270), normalise read tool arguments before hashing:

```python
def _canonical_fingerprint(tool_name: str, args: dict) -> str:
    """Normalise args for loop detection. Strips pagination offsets from reads."""
    if tool_name in ("read_file", "fs.read", "Read"):
        # Treat all reads of the same path as the same call regardless of offset/limit
        canonical_args = {"path": args.get("path") or args.get("filePath", "")}
    elif tool_name in ("grep", "search_code"):
        # Treat same pattern+path as same call regardless of include/context lines
        canonical_args = {
            "pattern": args.get("pattern", ""),
            "path": args.get("path", ""),
        }
    else:
        canonical_args = args
    return f"{tool_name}:{json.dumps(canonical_args, sort_keys=True, default=str)}"
```

Replace line 270 `fingerprint = f"{tool_name}:..."` with `fingerprint = _canonical_fingerprint(tool_name, args)`.

**Fix — Part 2: Detect alternating loops**

Currently the doom-loop fires only when the **exact same fingerprint** appears `DOOM_LOOP_THRESHOLD` times consecutively. Extend to detect any pattern where the agent is not making forward progress:

```python
# After updating the ring buffer (line 275), check for alternating pattern:
if len(recent_calls) >= RECENT_CALLS_WINDOW:
    unique_in_window = len(set(recent_calls[-RECENT_CALLS_WINDOW:]))
    total_in_window = RECENT_CALLS_WINDOW
    # If the last 10 calls use only 2 distinct tools, flag as potential loop
    if unique_in_window <= 2:
        # Escalate: ask permission for alternating_loop
        doom_result = _ask_doom_permission("alternating_loop", recent_calls, event_bus)
        if doom_result:
            return doom_result, recent_calls
```

**Estimated impact:** Catches the "read in chunks" pattern and the "read/grep alternation" pattern, which are among the most common sources of runaway token consumption on exploration tasks.

---

### P3 — Lower Priority (Efficiency Improvements)

---

#### P3-A: Cache static system prompt sections; regenerate only on change

**File:** `src/core/context/context_builder.py`  
**Relevant lines:** The `build_prompt()` function assembles `system_parts` from scratch on every call (line 484 onward), including re-reading SOUL.md (`self.soul` via `_read_text_cached`), re-computing tool descriptions, and re-joining everything.

**Fix:** Introduce a two-tier cache on `ContextBuilder`:
- **Static tier** (session-lifetime): SOUL.md, tool descriptions, format instructions. These do not change during a session. Cache the token count and serialised string; skip re-assembly if the inputs haven't changed.
- **Dynamic tier** (per-turn): Environment block (git status, date, working directory). Only regenerate if a tracked fact has changed since the last turn.

```python
class ContextBuilder:
    def __init__(self, ...):
        ...
        self._static_system_cache: Optional[str] = None
        self._static_system_hash: Optional[str] = None
        self._dynamic_env_cache: Optional[str] = None
        self._dynamic_env_facts: dict = {}

    def _build_static_system(self) -> str:
        """SOUL.md + tool list + format instructions. Cached for session lifetime."""
        import hashlib
        inputs = self.soul + str(self._get_tool_descriptions())
        h = hashlib.md5(inputs.encode()).hexdigest()
        if h == self._static_system_hash and self._static_system_cache:
            return self._static_system_cache
        result = self._assemble_static_parts()
        self._static_system_cache = result
        self._static_system_hash = h
        return result

    def _build_dynamic_env(self, state: dict) -> str:
        """Git status, date, working dir. Regenerate only when facts change."""
        current_facts = {
            "wd": state.get("working_dir"),
            "date": datetime.date.today().isoformat(),
            "git_head": self._get_git_head(state.get("working_dir")),
        }
        if current_facts == self._dynamic_env_facts and self._dynamic_env_cache:
            return self._dynamic_env_cache
        result = self._assemble_env_block(current_facts)
        self._dynamic_env_facts = current_facts
        self._dynamic_env_cache = result
        return result
```

**Estimated impact:** Eliminates repeated SOUL.md disk reads, tool-description serialisation, and format-instruction assembly on every LLM call. For a session with 20 turns, this avoids 19 redundant assemblies of the static portion of the system prompt.

---

#### P3-B: Implement proactive context pruning for tool outputs

**File:** `src/core/context/context_builder.py` (the `build_prompt()` conversation history assembly section)

**Inspired by:** opencode's `compaction.prune()` which marks tool outputs as compacted once they exceed 40k tokens from the end of the conversation.

**Current behaviour:** All tool outputs from all previous turns are included in the context window until the total exceeds the budget, at which point `_truncate_text()` cuts the entire conversation tail. This is both wasteful (carries stale large tool outputs) and blunt (may truncate recent relevant content to fit old irrelevant content).

**Fix:** After each turn, identify tool results that are no longer referenced by the current plan step or by the last N turns of conversation. Mark them as prunable. On the next prompt build, replace prunable tool results with a short summary:

```python
def _should_prune_tool_result(
    self,
    tool_result: dict,
    current_plan_step: Optional[str],
    recent_history: list,
    turns_ago: int,
) -> bool:
    """True if this tool result is old enough and not referenced in recent context."""
    if turns_ago < 3:
        return False  # Always keep last 3 turns of tool results
    content = str(tool_result.get("content", ""))
    token_count = self.token_estimator(content)
    if token_count < 200:
        return False  # Not worth pruning small results
    # Check if any recent message or current plan step references the file/content
    if current_plan_step:
        # Extract file paths from tool result and check if plan step mentions them
        referenced_paths = _extract_paths_from_content(content)
        plan_mentions = any(p in current_plan_step for p in referenced_paths)
        if plan_mentions:
            return False
    return True  # Safe to prune

def _summarise_tool_result(self, tool_result: dict) -> dict:
    """Replace large tool output with a token-efficient stub."""
    content = str(tool_result.get("content", ""))
    tokens = self.token_estimator(content)
    stub = f"[Tool result pruned — {tokens} tokens. Re-read if needed.]"
    return {**tool_result, "content": stub}
```

Apply during conversation history assembly in `build_prompt()`, iterating backwards from the oldest message and pruning eligible results before computing total token count.

**Estimated impact:** For a session that reads 10 large files early in the conversation, proactive pruning can reclaim 10,000–50,000 tokens of context space, allowing the agent to operate at peak attention quality throughout instead of degrading as context fills.

---

## 4. Summary Table

| ID | File | Lines | Change Type | Estimated Token Saving | Status |
|---|---|---|---|---|---|
| P0-A | `verification_node.py` | 132–136 | Bug fix | N-1 pytest runs eliminated | **DONE** |
| P0-B | `tool_execution_pipeline.py` | 128–146 | Bug fix | Ends RBW micro-loops | **DONE** |
| P1-A | `evaluation_node.py` | 139–149 | Default flip | 1 LLM call per completion | **DONE** |
| P1-B | `debug_node.py` | 106–117 | Remove reset | Caps debug at MAX_TOTAL_DEBUG | **DONE** |
| P1-C | `agent_brain.py` + `context_builder.py` | 229–233 / 488–492 | Remove duplicate | ~500 tokens per LLM call | **DONE** |
| P1-D | `context_builder.py` | 856–860, 931–932 | Algorithm swap | 1000× faster truncation | **DONE** |
| P2-A | `builder.py` | 282–370 | New routing | Bypasses analysis node for simple first-round tasks | **DONE** |
| P2-B | `planning_node.py` | `_load_last_plan()` | Logic change | Ends false plan resumptions | **DONE** |
| P2-C | `task_lifecycle.py` | `start_new_task_impl` L44–66 | Initialisation | Ends TODO.md RBW loop | **DONE** |
| P2-D | `loop_guards.py` | `check_doom_loop` + new helpers | Algorithm extend | Catches alternating-tool + paginated loops | **DONE** |
| P3-A | `context_builder.py` | `_build_static_system_prefix` + cache | Cache layer | Saves static assembly per turn (session-lifetime cache) | **DONE** |
| P3-B | `context_builder.py` | `_prune_stale_tool_outputs` + `build_prompt` | Pruning logic | Reclaims 10k–50k tokens for long sessions | **DONE** |

**All P0–P3 fixes implemented and validated. 3205 tests pass, 4 skipped, 1 pre-existing flaky integration test (`test_delegate_task_valid_roles`) that hangs only when run after the full suite due to global executor state pollution — not caused by these changes.**

---

## 5. Token Cost Breakdown — Typical "Simple" Task

To illustrate the problem concretely, here is a token flow for a task like **"add a docstring to function X in file Y"** in CodingAgent as currently implemented:

```
Turn 1: Planning LLM call
  - System prompt × 2 (duplication): ~2,000 tokens
  - Task description: ~50 tokens
  - Planning LLM response: ~500 tokens
  Subtotal: ~2,550 tokens

Turn 2: Perception (read task, build context)
  - System prompt × 2: ~2,000 tokens
  - Conversation history so far: ~600 tokens
  - File content (read file Y): ~3,000 tokens (full file)
  Subtotal: ~5,600 tokens

Turn 3: Execution (edit file Y)
  - System prompt × 2: ~2,000 tokens
  - Conversation history: ~6,200 tokens
  - Tool call + result: ~200 tokens
  Subtotal: ~8,400 tokens

Turn 4: Verification (read file Y again to confirm)
  - System prompt × 2: ~2,000 tokens
  - Conversation history: ~8,600 tokens
  - Re-read file Y: ~3,000 tokens
  Subtotal: ~13,600 tokens

Turn 5: Semantic evaluation LLM call
  - Minimal prompt: ~500 tokens
  Subtotal: ~500 tokens

Turn 6: pytest run (because at_final_step=True after every tool call)
  × 3 tool calls = 3 full pytest runs (not LLM tokens, but wall-clock latency)

TOTAL LLM INPUT TOKENS: ~30,650 tokens
ACTUAL INFORMATION CONTENT: ~3,600 tokens (file content + task description)
OVERHEAD RATIO: ~8.5×
```

With the fixes above (single system prompt, skip planning for simple task, no re-read, no semantic eval):
```
Optimal flow:
  - System prompt × 1: ~1,000 tokens
  - Task + file content: ~3,050 tokens
  - Edit + result: ~200 tokens
TOTAL: ~4,250 tokens
OVERHEAD RATIO: ~1.2×
```

That is roughly **7× fewer tokens** for a simple task.

---

## 6. Summary

The core issues are:

1. **No task complexity gating** — every task, simple or complex, goes through planning, multi-node execution, semantic evaluation, and full verification.
2. **System prompt duplication** — every LLM call pays 2× the system prompt cost.
3. **Unbounded re-reading** — files read in previous turns are read again, injecting thousands of duplicate tokens.
4. **Misaligned loop guards** — doom-loop detection is too narrow (same tool, same args, consecutive) to catch the most common real-world loops (alternating tools, slightly varied args, cross-message cycles).
5. **Extra LLM calls baked in** — semantic evaluation and per-error-type debug resets add LLM calls that are not justified for most tasks.
6. **Silent failures** — RBW blocks in the pipeline don't inject history messages, causing silent micro-loops the LLM never sees and can't self-correct.

The opencode architecture avoids most of these by design: no mandatory planning phase, single system prompt assembly, streaming tool dispatch with mid-stream loop detection, child-session isolation for subagents, and proactive context pruning. The most impactful single change for CodingAgent would be task complexity classification to bypass planning for simple tasks, followed by deduplicating the system prompt injection and fixing the `at_final_step` bug that triggers redundant pytest runs.
