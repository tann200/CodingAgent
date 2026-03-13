# Local CodingAgent MVP Implementation Tasklist

This document outlines the strict Minimum Viable Product (MVP) roadmap required to achieve a "boringly reliable" local coding agent before moving on to the advanced 7-tier SWE-agent architecture. 

**Goal:** The agent must reliably execute a `perceive → decide → act` loop for 20+ continuous steps without tool hallucinations, JSON syntax errors, prompt collapse, or losing task state.

---

## 1. Current State Analysis & Architectural Gaps

| Component | Current Codebase State | MVP Target State | Gap & Required Action |
| :--- | :--- | :--- | :--- |
| **LLM Inference** | `llm_manager.py` handles provider discovery, config parsing, and routing `call_model` directly to unstructured adapter methods (`adapter.chat`). | **Unified Inference Client:** A rigid abstraction layer that enforces a unified input/output schema. | **Gap:** `llm_manager.py` is doing too much (config + network + parsing). **Action:** Introduce `src/core/inference/llm_client.py` as the strict abstract base class that adapters *must* implement to guarantee deterministic responses. |
| **Agent Loop** | `orchestrator.py` -> `run_agent_once` acts as a monolithic while-loop relying on native JSON OpenAI function calling. | **Minimal Loop with Text Tools:** A strict `perceive → decide → act` loop using plaintext/XML tool calls (NO JSON tools). | **Gap:** Native JSON tool schemas break small local models. **Action:** Implement `<tool>name...</tool>` XML parser in the orchestrator. Retain core read/search/bash tools but simplify their schemas. |
| **Prompt Assembly** | `agent_brain.py` dynamically builds giant prompts. It loads a single monolithic persona (`agents/coding_agent.md`), completely lacking modular roles or dynamic skills. | **ContextBuilder Module:** Strict hierarchical prompt assembly (`IDENTITY -> ROLE -> TASK -> TOOLS -> CONVERSATION`) capped at 6k tokens with isolated roles. | **Gap:** Monolithic personas cause context drift and lack adaptability. **Action:** Build `ContextBuilder`, split personas into distinct `roles/` (e.g., Strategic vs Operational), and enforce strict token drops. |
| **Memory / Context** | `message_manager.py` permanently deletes old messages when hitting the token limit. | **Memory MVP (Tier 2/3):** Implement `.agent-context/TASK_STATE.md` and `ACTIVE.md`. Implement a distillation hook. | **Gap:** Total amnesia on long tasks. **Action:** Scaffold `.agent-context/`. Add `TASK_STATE` distillation LLM call every 5 steps. |
| **Loop Prevention** | None. The agent can get stuck in infinite search/edit loops until the `max_rounds` threshold is hit. | **Execution Trace:** Maintain an `execution_trace.json`. If a tool repeats identically, force a strategy change. | **Gap:** Infinite tool looping. **Action:** Add JSON trace logging and loop-detection heuristics to the execution loop. |

---

## 2. Detailed MVP Implementation Roadmap

### Phase 0: LLM Provider Stability
*Why `llm_client.py` instead of `llm_manager.py`?* `llm_manager.py` is a high-level router that handles configurations, event buses, and provider discovery. It passes data to adapters, but the adapters themselves currently return completely different payload shapes depending on the backend. We need a strict `LLMClient` Interface to enforce that all adapters return the exact same parsed text, token usage stats, and latency metrics to the Orchestrator, regardless of whether it's Ollama or LM Studio.

- [x] **Task 0.1:** Create abstract `src/core/inference/llm_client.py` interface with `generate(...)`.
- [x] **Task 0.2:** Update adapters (`lm_studio_adapter.py`, `ollama_adapter.py`) to inherit from this interface and enforce standardized output payloads.
- [x] **Task 0.3:** Implement a telemetry wrapper to strictly log: `prompt_tokens`, `completion_tokens`, `latency`, `model_name`, and `provider`.

### Phase 1: Minimal Agent Loop & Plaintext Tools
*Kill JSON tool schemas but retain essential system capabilities.*
- [ ] **Task 1.1:** Refactor `src/tools/registry.py`. Remove nested JSON schema representations in favor of ultra-lean plaintext descriptions (e.g., `bash(command) -> Execute shell command`).
- [ ] **Task 1.2:** Define the MVP Toolset. *Must include*: `search_code(query)`, `read_file(path)`, `edit_file(path, patch)`, `run_tests()`, `bash(command)`, and `glob(pattern)`.
- [ ] **Task 1.3:** Update `src/tools/file_tools.py` `edit_file` to support precise *Unified Diff* patching rather than blind file replacement.
- [ ] **Task 1.4:** Rewrite `run_agent_once` logic into a strict state machine avoiding native JSON function calls.
- [ ] **Task 1.5:** Implement an XML tool parser inside the Orchestrator. The model *must* output block formats like:
```text
<tool>
name: bash
command: ls -la
</tool>
```

### Phase 2: ContextBuilder & `agent-brain/` Restructuring
*Replace monolithic personas with budgeted hierarchical assembly.*

**Gap Analysis on `agent-brain/`:**
Currently, `agent_brain.py` blindly loads files like `agents/coding_agent.md`. These files are **monolithic personas**—combining tone, rules, tools, and identity into one massive text block. `SOUL.md` (which dictates "Get Shit Done" doctrine) *does not conflict* with roles; rather, `SOUL.md` is the immutable **Identity** of the system, whereas the `roles/` folder defines the specific **Job** the system is currently doing. `workflows/` as markdown files are a bad pattern that causes mode-mixing and will be deprecated in favor of runtime orchestrator loops. `skills/` currently exist but lack an injection mechanism.

- [x] **Task 2.1:** Restructure `agent-brain/`: Delete `agents/`, `templates/`, and `workflows/`.
- [x] **Task 2.2:** Create `agent-brain/identity/` and move `SOUL.md` and `LAWS.md` inside. This is the Tier 0 immutable identity injected into *every* prompt.
- [x] **Task 2.3:** Create `agent-brain/roles/`. Write `strategic.md` (for planning) and `operational.md` (for execution). These replace `coding_agent.md`.
- [x] **Task 2.4:** Standardize `agent-brain/skills/` formats. Ensure every skill has: `Skill Name`, `When to Use`, `Strategy`, `Execution Steps`.
- [x] **Task 2.5:** Create `src/core/context/context_builder.py`.
- [x] **Task 2.6:** Implement `ContextBuilder.build_prompt()` enforcing strict XML block ordering: `<identity>`, `<role>`, `<active_skills>`, `<task>`, `<tools>`, `<conversation>`.
- [x] **Task 2.7:** Implement Token Budgeting using `tiktoken` (or `len(chars)/4`). Enforce a hard cap (e.g., 6000 tokens). Implement Priority Drop Logic (Drop Conversation first, *never* drop Identity or Role).

### Phase 3: Memory MVP & Context Distillation
*Implement the minimum required system to survive long tasks.*
- [x] **Task 3.1:** Update `orchestrator.py` initialization to scaffold the `.agent-context/` directory in the current working directory.
- [x] **Task 3.2:** Generate `TASK_STATE.md` (Current Task, Completed Steps, Next Step) and `ACTIVE.md` if they do not exist.
- [x] **Task 3.3:** Implement the **Context Distillation Hook** in the message manager: Every 5 steps, run a fast LLM call to summarize the truncated messages and overwrite `TASK_STATE.md`.
- [x] **Task 3.4:** Create `execution_trace.json`. Inside the agent loop, log `{"goal": "...", "steps": [{"tool": "...", "args": "..."}]}`.
- [x] **Task 3.5:** Implement **Loop Prevention**: Before executing a tool, read the last 3 trace steps. If they are identical, reject the tool call and inject a system prompt forcing a strategy change.

### Phase 4: Automated Stability Testing Suite
*Prove the MVP is "boringly reliable".*
- [x] **Task 4.1:** Create `scripts/test_agent_stability.py`.
- [x] **Task 4.2:** Build Scenario 1: Agent finds `ProviderManager` (Assert sequence: search -> read).
- [x] **Task 4.3:** Build Scenario 2: Agent fixes a dummy syntax error (Assert sequence: read -> edit).
- [x] **Task 4.4:** Build Scenario 3: Agent executes a bash command and acts on output (Assert sequence: bash -> read -> edit).

---

## MVP Final Gate
The system is ready to progress to advanced Phase 5 SWE-agent features (Graph Memory, LangGraph micro-agents, Sandboxing) **ONLY WHEN**:
1. Agent executes 20+ tool calls without crashing.
2. Agent never emits an invalid XML tool format.
3. Agent completes basic tasks across all test scenarios.
4. Prompt strictly never exceeds the token budget.
5. Both LM Studio and Ollama providers output deterministic tool calls.

---

# Implementation Details (Phase 0 → Phase 4)

The following sections transform the high-level MVP tasks into concrete, unambiguous implementation instructions developers should follow. Each Phase lists exact files to add/modify, API signatures, data shapes, tests to create, and small runnable templates or prompts. Add these into the repo as the source-of-truth for implementation.

IMPORTANT: These details are normative for the MVP — follow them exactly unless you intentionally change the contract and update dependent tests/docs.

PHASE 0 — LLM Provider Stability (Concrete Implementation)

Files to add/modify
- `src/core/inference/llm_client.py` (new): canonical LLM client interface (see exact API below).
- `src/core/inference/telemetry.py` (new): telemetry helper to publish `model.response` events.
- `src/core/llm_manager.py`: adapt to instantiate and call LLMClient instances instead of relying on raw adapter return shapes.
- `src/adapters/lm_studio_adapter.py` and `src/adapters/ollama_adapter.py`: implement LLMClient contract (normalize outputs).

LLMClient exact interface (copy into `src/core/inference/llm_client.py`)

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

class LLMClient(ABC):
    @abstractmethod
    def generate(self,
                 messages: List[Dict[str, str]],
                 model: Optional[str] = None,
                 stream: bool = False,
                 timeout: Optional[float] = None,
                 provider: Optional[str] = None,
                 **kwargs) -> Dict[str, Any]:
        """Synchronous call: return normalized payload (see below)."""

    async def agenerate(self, *args, **kwargs) -> Dict[str, Any]:
        """Optional async wrapper; adapters may implement via `asyncio.to_thread`."""
```

Normalized return payload (contract every adapter must follow)

- Always return a dict with these keys (present even on errors):
  - `ok`: bool
  - `provider`: canonical provider id (e.g., `lm_studio`, `ollama`)
  - `model`: canonical model id when known (e.g., `qwen/qwen3.5-9b`)
  - `latency`: seconds (float)
  - `prompt_tokens`, `completion_tokens`, `total_tokens`: int
  - `choices`: list of choice objects each having:
      - `message`: {"role": "assistant", "content": "..."}
      - optional `tool_calls`: list of {"name": str, "arguments": dict}
      - `finish_reason`: str (e.g., "stop")
  - `raw`: original provider response for debugging

On error, set `ok=False` and include `error` key describing the failure.

Telemetry contract
- Adapters or LLMClient wrapper must publish an EventBus event `model.response` after each call with payload:
  - `{ "provider":..., "model":..., "prompt_tokens":..., "completion_tokens":..., "total_tokens":..., "latency":..., "ts": <timestamp> }`
- Implement `src/core/inference/telemetry.py` helper to do the publish and to guard missing event bus.

Tests (Phase 0)
- `tests/unit/test_llm_client_contract.py`:
  - Mock a provider adapter to return provider raw payload; assert `llm_manager` receives normalized payload keys and that telemetry event `model.response` is published.
- `tests/unit/test_adapter_fallbacks.py`:
  - Ensure adapters fall back to provider config in `src/config/providers.json` when no env var set.

PHASE 1 — Minimal Agent Loop & Plaintext Tools (Concrete Implementation)

Files to add/modify
- `src/core/orchestration/tool_parser.py` (new) — strict tool-block parser. See signature below.
- `src/tools/registry.py` — simplify `register_tool` signature: `register_tool(name, fn, description='', side_effects=None)`.
- `src/tools/file_tools.py` — implement `edit_file(path, patch)` that applies a Unified Diff.
- `src/core/orchestration/orchestrator.py` — refactor `run_agent_once` into a state-machine that uses the tool parser and LLMClient output contract.

Tool-block parser (exact contract)
- Function: `parse_tool_block(text: str) -> Optional[Dict[str, Any]]`
  - Behavior: find the first `<tool>` ... `</tool>` block that begins and ends on its own lines; parse fields inside where either:
    - `name: foo` and YAML-like `key: value` lines (safe subset), OR
    - a single `args: { ...json... }` line where JSON provides arguments.
  - Return shape: `{"name": <str>, "arguments": <dict>}` or None when not parseable.

Example accepted block
```
<tool>
name: edit_file
args: {"path":"src/foo.py","patch":"@@ -1 +1 @@\n- old\n+ new\n"}
</tool>
```

Orchestrator loop (state-machine skeleton in `run_agent_once`)
1. Ensure system prompt via `msg_mgr.set_system_prompt(load_system_prompt(name))`.
2. Append the user message(s) to `msg_mgr`.
3. Build prompt messages via `ContextBuilder.build_prompt(...)` (Phase 2).
4. Call `llm_client.generate(messages=messages, model=selected_model)`.
5. Normalize: look for `choices[0].tool_calls` or parse assistant `content` with `parse_tool_block()`.
6. If `tool_call` present:
   a. preflight = `self.preflight_check(tool_call)` — if not ok, append system message and stop/continue accordingly.
   b. publish `tool.execute.start`
   c. res = `self.execute_tool(tool_call)`
   d. publish `tool.execute.finish` or `tool.execute.error`
   e. Append a `role: tool` message to `msg_mgr` of content `json.dumps({"name":...,"result":...})`.
   f. possibly trigger distillation if step count % 5 == 0.
7. If no tool calls, return final assistant message (or parsed result).
8. Ensure loop prevention using `execution_trace.json` (Phase 3).

`edit_file` unified-diff behavior (implementation notes)
- Use Python `difflib` utilities or a lightweight `patch`-like function that applies unified diff hunks to an original file content and returns the patched content or an error.
- `edit_file` must never blindly overwrite a path outside `Orchestrator.working_dir` — validate using `_is_within_working_dir`.

Tests (Phase 1)
- `tests/unit/test_tool_parser.py` covering valid/invalid tool blocks.
- `tests/unit/test_edit_file_unified_diff.py` verifying unified-diff apply and failure modes.
- `tests/integration/test_agent_loop_plaintext_tools.py` using a deterministic mock adapter that emits `<tool>` blocks; assert orchestrator executes expected tool sequence and appends tool-role messages.

PHASE 2 — ContextBuilder & `agent-brain/` Restructuring (Concrete Implementation)

Files to add/modify
- `src/core/context/context_builder.py` (new) — implement build_prompt and token budgeting.
- Restructure `agent-brain/` on disk (suggested layout):
  - `agent-brain/identity/SOUL.md` (immutable identity)
  - `agent-brain/roles/strategic.md`
  - `agent-brain/roles/operational.md`
  - `agent-brain/skills/*.md`

`ContextBuilder.build_prompt` API (exact)
- Signature:
```python
class ContextBuilder:
    def __init__(self, token_estimator: Callable[[str], int] = None):
        ...
    def build_prompt(self, identity: str, role: str, tools: List[Dict], conversation: List[Dict], max_tokens: int = 6000) -> List[Dict[str,str]]:
        """Returns a list of messages in this order: system(identity), system(role), system(tools), conversation (chronological).
        Enforces hard token budget and truncation rules (see below).
        """
```

Token budgeting rules (exact numbers to implement)
- Default `max_tokens = 6000`.
- Reservations:
  - `identity_quota = min(ceil(0.12 * max_tokens), 800)`
  - `role_quota = min(ceil(0.12 * max_tokens), 800)`
  - `tools_quota = min(ceil(0.06 * max_tokens), 400)`
  - `conversation_quota = max_tokens - (identity_quota + role_quota + tools_quota)`
- Truncation rules:
  - Never drop identity or role entirely. If identity/role exceed their quotas, truncate with a clearly visible marker `\n\n[TRUNCATED]`.
  - Drop oldest conversation messages first to fit conversation_quota.
  - If conversation_quota <= 0, still include identity+role+tools and append system note `[CONTEXT FULL — conversation truncated]`.

Tests (Phase 2)
- `tests/unit/test_context_builder.py`: build prompts with artificially large identity/role and conversation to assert quotas and truncations behave exactly as specified.

PHASE 3 — Memory MVP & Context Distillation (Concrete Implementation)

Filesystem layout (inside `Orchestrator.working_dir`)
- `.agent-context/`
  - `TASK_STATE.md` — Markdown summary with sections: Current Task, Completed Steps (bullet list), Next Step
  - `ACTIVE.md` — single-line string describing the active goal
  - `execution_trace.json` — JSON array of trace entries

Distillation hook
- File: `src/core/memory/distiller.py` (new)
- Signature:
```python
def distill_context(messages: List[Dict[str,str]], max_summary_tokens: int = 512, llm_client: LLMClient = None) -> Dict[str, Any]:
    """Return {"current_task": str, "completed_steps": [str], "next_step": str}
    """
```
- Distillation prompt (template):
  - system: "You are a succinct summarizer for agent memory. Given recent messages, produce strictly a JSON object with keys current_task, completed_steps, next_step. Do not add other commentary."
  - user: "Recent messages:\n\n<insert truncated conversation messages>\n\nReturn only JSON."
- Schedule: run distillation every 5 tool executions OR when `message_manager._truncate_to_window()` triggers a truncation.
- On failure to parse JSON from LLM, log telemetry and skip updating `TASK_STATE.md`.

`execution_trace.json` schema (each entry)
```json
{
  "ts": "2026-03-11T10:54:32Z",
  "tool": "edit_file",
  "args": {"path": "src/x.py"},
  "result_summary": "wrote 30 bytes",
  "assistant_message_excerpt": "Issued edit_file for src/x.py"
}
```

Loop-detection algorithm (exact)
- Before executing tool_call:
  1. Read the last 5 entries of `execution_trace.json`.
  2. If the same `tool` + identical `args` pair appear 3 times consecutively in the last 5 entries, block the execution.
  3. On block: append `system` message: "[LOOP DETECTED] Repeated tool calls blocked; consider alternate strategy." and publish telemetry event `execution.loop_detected` with details.

Tests (Phase 3)
- `tests/unit/test_distiller.py` to assert distillation output schema.
- `tests/integration/test_loop_prevention.py` using mock adapter to simulate repeated tool calls and assert block behavior and telemetry event.

PHASE 4 — Automated Stability Testing Suite (Concrete Implementation)

Files to add
- `tests/integration/mocks/deterministic_adapter.py` — deterministic LLMClient that returns a scripted sequence of `choices` and `<tool>` blocks for scenarios.
- `scripts/test_agent_stability.py` — runner that executes scenarios headless against an Orchestrator using `DeterministicAdapter` and asserts expected tool sequences.

Scenario definitions (examples)
- Scenario A: `provider_probe` — expect sequence: `search_code` → `read_file`
- Scenario B: `fix_syntax` — expect: `read_file` → `edit_file` → `run_tests` → success
- Scenario C: `bash_then_act` — expect: `bash` → `read_file` → `edit_file`

Test runner contract
- Script should accept `--scenario <name>` and `--working-dir` and return non-zero exit on failure. Tests call the script and assert exit code and output traces.

CI and reproducibility
- For integration tests that require LM Studio or Ollama, rely on `src/config/providers.json` detection or inject a small `tests/integration/conftest.py` that reads `src/config/providers.json` and sets `LM_STUDIO_URL` and `OLLAMA_URL` environment variables (do not rely on manual env var setup).

Developer commands (examples)

Run only unit tests you changed:
```bash
pytest tests/unit/test_tool_parser.py -q
pytest tests/unit/test_llm_client_contract.py -q
```

Run a single integration scenario locally (deterministic adapter):
```bash
python scripts/test_agent_stability.py --scenario fix_syntax --working-dir /tmp/agent_run
```

A small checklist to mark progress in the doc (copy to top of this file when implementing)
- [x] Phase 0 — LLMClient + telemetry
- [x] Phase 1 — Tool parser, edit_file, state machine
- [x] Phase 2 — ContextBuilder & token budgeting
- [x] Phase 3 — Distillation, .agent-context, trace + loop detection
- [x] Phase 4 — Deterministic scenarios + stability runner

---

Appendix: Quick reference snippets

1) Distillation system prompt (exact text):
```
System: You are a succinct summarizer used to distill long conversation history into a short machine-readable TASK_STATE. Return strictly a JSON object with keys: current_task (string), completed_steps (array of short strings), next_step (string). Do not add commentary or explanation.

User: Here are the recent messages: <PASTE MESSAGES>

Return only JSON.
```

2) Example `providers.json` minimal entry (put in `src/config/providers.example.json`):
```json
{
  "name": "lm_studio_local",
  "type": "lm_studio",
  "base_url": "http://localhost:1234",
  "api_key": "",
  "models": ["qwen/qwen3.5-9b"]
}
```

3) Tool block canonical example (must be recognized by `parse_tool_block`):
```
<tool>
name: edit_file
args: {"path": "src/main.py", "patch": "@@ -1 +1 @@\n- old\n+ new\n"}
</tool>
```

---

Additions to README / developer notes (recommended)
- Document that `src/config/providers.json` is authoritative for provider discovery in tests and adapter initialization; prefer this over env vars.
- Add `docs/DEVELOPMENT.md` with the short commands above and how to add a local providers.json for integration testing.

---

If you'd like, I can now:
- Commit the new scaffolding files for Phase 0 (`llm_client.py`, telemetry helper) and run unit tests.
- Or implement Phase 1 `tool_parser.py` and `edit_file` unified-diff utility next.

Which Phase should I implement first in code? (I recommend Phase 0 then Phase 1.)
