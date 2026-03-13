# Advanced SWE-Agent Memory & Cognitive Pipeline Architecture (2025-2026 Edition)

This document is the definitive, comprehensive architecture and implementation plan for the local CodingAgent. It combines Tiered Memory, the Cognitive Pipeline, Graph-Defined Workflows, Execution Sandboxes, and Dual-Brain orchestration. It includes a Gap Analysis comparing the current codebase to the target state.

**Core Design Principles for Local 9B–14B Models:**
1. **Structure > Raw Context:** Formatted state files beat massive context windows.
2. **Tools > Reasoning:** The LLM acts as the decision-maker; deterministic tools act as the reasoning engine.
3. **Skills > Generic Prompts:** Narrow, modular instructions beat monolithic personas.
4. **Planning > Improvisation:** Explicit cognitive stages prevent context collapse and tool loops.
5. **Files as Consciousness:** Parameters are unconscious; state files are the agent's actual "mind."

---

## PART I: THE GAP ANALYSIS

Before implementing the new architecture, we must reconcile the current state of the codebase against the target plan.

### 1. Current State vs. Target Architecture

| Component | Current State in Codebase | Target State (This Document) | Architectural Gap |
| :--- | :--- | :--- | :--- |
| **System Prompts & Roles** | `agent_brain.py` dynamically loads `SOUL.md`, `LAWS.md`, and a monolithic `coding_agent.md` persona. It injects a large `<available_tools>` XML block without token budgeting. | Dedicated `ContextBuilder` that strictly budgets tokens. Supports multiple dynamic roles (`strategic`, `operational`) and modular skills (`debugging`, `refactoring`). | **Large.** The system lacks a `ContextBuilder`, Task Classifiers, and modular Skill injections. The `agent-brain/` folder relies on monolithic personas rather than distinct roles and skills. |
| **Memory Management** | `MessageManager` stores raw conversation and permanently *drops* the oldest messages when hitting `max_tokens`. Zero persistent project memory exists. | **Tiered Memory.** Uses a 3-Stage Decay (Distillation to `TASK_STATE.md`). Reads `.agent-context/ACTIVE.md` and maintains `execution_trace.json`. Uses LanceDB + AST Indexing. | **Complete Gap.** There is no `.agent-context/` folder logic, no message distillation triggers, and no vector or AST indexing implemented. |
| **Orchestration & Graph** | `orchestrator.py` uses a monolithic `run_agent_once` while-loop to handle everything. `langgraph_node.py` is currently just a light wrapper around a tool-calling loop, not an explicit state machine. | **Cognitive Pipeline (LangGraph).** A 7-node explicitly separated pipeline: Perception → Understanding → Planning → Execution → Verification → Reflection → Memory Update. | **Massive Conflict.** The current orchestrator acts as a single "God Agent". The target plan completely dismantles this in favor of separated cognitive nodes (Graph-Defined Workflows). |
| **Tool Execution** | Tools execute blindly in `orchestrator.execute_tool` without validation. If they fail, raw errors are dumped straight back to the LLM context. | **Execution Sandbox.** Tools generate patches that are applied virtually, syntax-checked (AST), statically analyzed (ruff), and tested (pytest) before committing. | **Large.** Tools lack structural parsing (JSON validation), isolation, and the SWE-bench Self-Debugging failure parser. |

### 2. First Actionable Steps (The Groundwork)

To bridge the gap and achieve initial proper working conditions (LLM loaded, basic tiered memory working, proper role prompts, and stable orchestration), follow this sequence:

1.  **Scaffold `.agent-context/` (Tier 2 & 3 Memory)**
    *   Update `orchestrator.py` `__init__` to detect and create `working_dir/.agent-context/`.
    *   Generate empty `TASK_STATE.md`, `ACTIVE.md`, and `execution_trace.json` files on boot.
2.  **Build `ContextBuilder` & Restructure `agent-brain/`**
    *   Rename `agent-brain/agents/` to `agent-brain/roles/`. Create `agent-brain/identity/` and `agent-brain/skills/`.
    *   Create `src/core/context/context_builder.py` to handle prompt assembly using strict token budgets and XML tags.
3.  **Upgrade `MessageManager` (The Distillation Hook)**
    *   Instead of dropping messages on token overflow, trigger a fast LLM call to distill older messages into `TASK_STATE.md` (Current Task, Decisions, Next Actions).
4.  **Refactor Orchestrator to "Node-Ready" State**
    *   Modify `run_agent_once` to call the `ContextBuilder`.
    *   Implement basic Loop Prevention: Check `execution_trace.json` before executing a tool to stop infinite loops.

---

## PART II: THE 7-TIER MEMORY HIERARCHY

To solve the "stateless goldfish" problem and avoid context collapse, memory is divided into chronological, semantic, and structural tiers.

### Tier 0: Core Directives (System Root)
**Location:** `agent-brain/identity/`
*   `SOUL.md`: Core identity, tone, and operating principles.
*   `LAWS.md`: Immutable constraints, safety guardrails, and boundaries.

### Tier 1: Global Learnings, Skills & Roles (System Root)
**Location:** `agent-brain/`
*   `learnings/LEARNINGS.md`: Successful patterns and general tool knowledge.
*   `failures/FAILURES.md`: A strict log of mistakes (e.g., "Regex replace failed on large files, always use AST parsing"). Loaded *before* reasoning to prevent repeating errors.
*   `skills/`: Modular capabilities (`python_refactor.md`, `debugging.md`, `api_design.md`). Each skill defines: `Skill Name`, `When to Use`, `Strategy`, `Step-by-Step Process`, `Common Mistakes`, and `Relevant Tools`.
*   `roles/`: Behavioral templates (`strategic.md`, `operational.md`, `reviewer.md`). Roles modify the reasoning style and restrict allowed tools.

### Tier 2: Working Memory (In-Memory & Distilled)
**Location:** `MessageManager` (RAM) & `.agent-context/TASK_STATE.md`
*   **Three-Stage Decay:** Instead of dropping old messages, use: `Full Message → Summarized Turn → Discarded`.
*   **Context Distillation (The "Fake 32k Context"):** Compresses conversation history into a structured `TASK_STATE.md` file containing: `Current Task`, `Completed Steps`, `Important Decisions`, `Known Constraints`, `Next Actions`.
*   **Trigger:** Runs when token limits approach or every N tool steps.

### Tier 3: Session Continuity & Project State (Project Root)
**Location:** `.agent-context/`
*   `NOW.md`: Immediate priorities read every session.
*   `ACTIVE.md`: Ongoing workflows and medium-term context.
*   `NEXT.md`: Interruption buffer. Written to when a session ends or crashes. Read at startup.
*   `PLANS.md`: LangGraph planner steps and progress.
*   `STRUCTURE.json`: Generated codebase topology (entrypoints, frameworks).
*   `execution_trace.json`: Tracks reasoning steps (`goal`, `steps_taken`). Used for **Loop Prevention**. If the agent repeats an action identically, the system forces a new strategy.
*   `edits.log`: Tracks timestamp, file, edit_summary, lines_changed.
*   `HEALTH.md`: Agent self-diagnostics (memory size, retrieval latency).

### Tier 4: Self-Healing Semantic Memory (Vector Store)
**Location:** `.agent-context/vector_store/` (LanceDB / Local Embeddings like `nomic-embed-text`)
*   **Typed Memories:** Schema includes `id`, `vector`, `text`, `type` (e.g., `architecture_decision`, `bug_pattern`), `importance` (1-10), `timestamp`, `tags`.
*   **Memory Gatekeeper:** Only stores verified facts (architectural decisions, API contracts). Discards conversational filler.
*   **Dream Cycle (Consolidation):** A periodic background process (e.g., at session end) that clusters related memories, summarizes them into a single high-level memory, and applies an aging decay function (`importance *= 0.95`).

### Tier 5: Graph-Based Code Index & LSP
**Location:** `.agent-context/symbol_graph.db` & `src/core/code_intelligence/`
*   **Graph Memory:** Represents code structurally. Nodes (`Function`, `Class`, `File`, `Test`) connect via Edges (`calls`, `imports`, `tested_by`).
*   **Incremental AST Indexing:** A `watchdog` file watcher monitors changes. When a file saves, it parses the *new* AST and updates the DB instantly, avoiding full-repo scans.
*   **LSP Manager (`lsp_manager.py`):** Automatically spawns language servers (`pyright`, `tsserver`) to provide deterministic semantic tools (`goto_definition`, `find_references`).

---

## PART III: CONTEXT BUILDER & BUDGET OPTIMIZER

The Orchestrator delegates prompt construction to `src/core/context/context_builder.py` to prevent "Lost in the Middle" syndrome.

### Strict Context Budgeting (Example: 8k Target)
*   Identity + Laws: `600 tokens`
*   Role: `300 tokens`
*   Skills: `900 tokens`
*   Task State (Distilled): `800 tokens`
*   Retrieved Context: `2000 tokens` (Diversity ensured via MMR - Maximal Marginal Relevance)
*   Conversation: `2000 tokens`
*   Tools: `1000 tokens`

### Deterministic Workspace Truth (OpenClaw Anchoring)
Workspace files *always* override memory. An anchored `<workspace_state>` XML block is injected containing the project structure and active files, preventing the model from hallucinating non-existent files.

### Final Prompt Injection Structure
```xml
<core_identity>
  [SOUL.md + LAWS.md]
</core_identity>
<workspace_state>
  [Anchored Project Structure & Entrypoints]
</workspace_state>
<agent_role>
  [STRATEGIC, OPERATIONAL, REVIEWER]
</agent_role>
<active_skills>
  [Injected by Task Classifier, e.g., Debugging]
</active_skills>
<task_state>
  [TASK_STATE.md (Distilled Context)]
</task_state>
<retrieved_memories>
  [Graph Expansion + Vector hits, budgeted]
</retrieved_memories>
<available_tools>
  [Compact XML tool schemas]
</available_tools>
```

---

## PART IV: THE COGNITIVE PIPELINE ARCHITECTURE (LANGGRAPH)

Instead of a single monolithic "God Agent" loop (like `run_agent_once`), we use the **Cognitive Pipeline**. This splits cognition into explicit stages, acting as a Dual-Brain system.

### Stage 1: Perception Layer (System 2 - Slow Brain)
*   **Purpose:** Convert raw user input/repo state into structured JSON facts (`task_type`, `priority`, `mentioned_files`). Prevents small models from getting overwhelmed.

### Stage 2: Understanding Layer
*   **Purpose:** Interpret the problem semantically. Outputs the `goal`, `suspected_files`, and `required_actions`.

### Stage 3: Planning Layer
*   **Purpose:** Generate the explicit Task Graph (`PLANS.md`). Queries Vector Store for architecture context. Zero file-editing tools.

### Stage 4: Execution Layer (System 1 - Fast Brain)
*   **Purpose:** Perform steps sequentially. Runs in a specific **Working Mode** (`Role + Tools + Skills + Prompt`). Heavily utilizes Tool-Driven Reasoning (e.g., `find_references("ProviderManager")` instead of internal textual reasoning).

### Stage 5: Verification Layer (Execution Sandbox)
*   **Purpose:** Validate changes *before* committing. 
*   **Sandbox Pipeline:** 
    1.  LLM outputs a structured patch (Unified Diff or JSON).
    2.  Applied to a virtual filesystem overlay.
    3.  Runs syntax checks (`ast.parse`), static analysis (`ruff`), and tests (`pytest`).
*   **SWE-Bench Debug Loop:** If verification fails, a **Failure Analyzer** parses the log into structured JSON (`error_type`, `file`, `line`, `stack_trace`) and feeds it back to Execution for up to 5 iterations.

### Stage 6: Reflection & Review
*   **Multi-Agent Code Review:** Before final commit, a separate `Reviewer Agent` (with no edit tools) scans for logic/security errors. A `Fix Agent` resolves these structured issues.
*   **Reflection:** Answers: "Did we succeed? What mistakes happened?"

### Stage 7: Memory Update Layer
*   **Purpose:** Persists insights. Updates `ACTIVE.md`, writes to the Semantic Memory, appends to `FAILURES.md`, and records global learnings.

---

## PART V: MULTI-STAGE CODE RETRIEVAL (DETERMINISTIC PRE-RETRIEVAL)

Most agents fail by asking the LLM to decide how to search. We use a **Code Intelligence Router**.

**Pipeline:** `User Question → Intent Classifier → Search Strategy`
1.  *Find Function / Symbol Definition* → Route to **AST Index** (`symbol_index.db`).
2.  *Usage / Types* → Route to **LSP Server** (`find_references`).
3.  *Debug Error / Text* → Route to **Ripgrep** (10x faster than embeddings).
4.  *Architecture Question* → Route to **Vector Store** (LanceDB).

*The LLM receives the exact answer (e.g., `orchestrator.py:120`) before reasoning begins.*

---

## PART VI: AUTONOMOUS REFACTORING LOOPS (PROACTIVE AGENT)

SWE agents shouldn't just react. A background LangGraph pipeline continuously improves the codebase.
1.  **Repo Scanner:** Extracts cyclomatic complexity, unused imports, and duplicate functions via AST.
2.  **Quality Analyzer:** Detects violations against rules (e.g., functions > 60 lines).
3.  **Refactor Planner:** Proposes extraction, splits, or renames.
4.  **Sandbox Executor:** Applies changes, runs tests. If tests pass, it merges autonomously.


---

---

## PART VIII: OPEN QUESTIONS FOR IMPLEMENTATION

As we move from architectural theory to implementation, the following questions must be resolved:

1. **The Intent Classifier Mechanism:**
   In Part V (Multi-Stage Code Retrieval), the pipeline states: `User Question → Intent Classifier → Search Strategy`.
   *Question:* Should the Intent Classifier be implemented as a distinct, fast, single LLM call (e.g., zero-shot classification prompt) that returns an enum (`AST`, `LSP`, `RIPGREP`, `VECTOR`), or should it be a hardcoded set of heuristics/regexes to save inference time?

2. **The Execution Sandbox Virtualization:**
   In Part IV (The Cognitive Pipeline / Verification Layer), the plan states the sandbox uses a "virtual filesystem overlay". 
   *Question:* How deep should the virtualization go for local Python execution? Should we just copy the target files into a `/tmp/agent-sandbox/` directory and run `pytest` there, or do we want to utilize Python's `unittest.mock.patch` / `ast` parsing strictly in memory to prevent the agent from accidentally writing malicious scripts to disk during the test phase?

---

## PART VIII: THE IMPLEMENTATION ROADMAP (RISK MITIGATION & STAGING)

To prevent architectural collapse during development, we must follow a strict, staged implementation order. Attempting to build the entire 7-tier memory, LangGraph pipeline, and Execution Sandbox simultaneously will make the system impossible to debug.

Below is the highly detailed, phase-by-phase implementation plan based on risk-mitigation analysis.

### Phase 0: LLM Stability & Unification
Before building agents, the foundational LLM communication must be rock-solid.
*   **Action:** Create a unified `src/core/inference/llm_client.py` interface (`generate(prompt, temperature, max_tokens)`).
*   **Action:** Ensure `LMStudioAdapter` and `OllamaAdapter` strictly adhere to this interface to prevent vendor-specific parsing bugs.

### Phase 1: Core Agent & Context Hygiene (The MVP)
Establish the basic reasoning engine without complex memory stores.
*   **Action:** Clean up `agent-brain/`. Remove `agents/`, `templates/`, and `workflows/`. Create `identity/`, `roles/`, `skills/`, and `learnings/`.
*   **Action:** Build `src/core/context/context_builder.py`. Implement **Tiered Prompt Assembly**:
    *   *Tier A (Never drop):* SOUL, LAWS
    *   *Tier B:* Role
    *   *Tier C:* Skills
    *   *Tier D:* Memory
    *   *Tier E (First to drop):* Conversation
*   **Action:** Implement **Token Control** in ContextBuilder (using `tiktoken` or `chars / 4` estimation).
*   **Action:** Compress the Tool Schema format. Move away from heavy JSON Schemas towards ultra-lean plaintext schemas (`search_code(query) -> grep style search`). Force the model to output tool calls in a simple XML block (`<tool>name: edit\nargs: ...</tool>`) instead of nested JSON objects.

### Phase 2: Task Classification & Skill Injection
The agent cannot choose its own skills blindly.
*   **Action:** Create `src/core/context/task_classifier.py`.
*   **Action:** Implement a fast pre-flight LLM call that analyzes the user request and outputs: `task_type`, `required_skills`, `required_tools`, `working_mode`, `retrieval_strategy`.
*   **Action:** Pass this classification to the `ContextBuilder` so it statically injects the correct files from `agent-brain/skills/`.
*   **Action:** Enforce **Role-Based Tool Control**. If the role is "Reviewer", filter the tool registry so only `search_code` is available.

### Phase 3: Memory MVP (Tier 2 & 3)
Do not build vector or graph databases yet. Start with explicit Markdown state.
*   **Action:** Scaffold `.agent-context/` logic.
*   **Action:** Implement `TASK_STATE.md` (Current Task, Completed Steps, Decisions, Constraints, Next Actions) and `ACTIVE.md`.
*   **Action:** Implement the **Context Hygiene Tool (Distillation)**. Every N steps, the system must summarize the conversation, append it to `TASK_STATE.md`, and clear the raw conversation to save tokens.
*   **Action:** Implement `execution_trace.json` formatted as `{"goal": "...", "steps": [{"tool": "...", "args": "..."}]}` to detect and block infinite loops.

### Phase 4: Deterministic Retrieval
Replace blind LLM guesses with standard CLI tools.
*   **Action:** Implement `ripgrep` search tool.
*   **Action:** Implement a fast, basic Regex-based Symbol Index (prior to building the full AST graph).

### Phase 5: Graph Memory & Full AST Indexing
Now introduce complex code intelligence.
*   **Important Caveat:** *Vector stores should NEVER contain code embeddings.* They are strictly for architecture decisions and bug facts. 
*   **Action:** Build `.agent-context/symbol_graph.db` using SQLite.
*   **Action:** Define Nodes (`Module`, `Function`, `Class`, `Test`, `Variable`) and Edges (`calls`, `imports`, `inherits`, `defines`, `tested_by`).
*   **Action:** Implement Incremental AST updates via file hashing (never rescan the whole repo).

### Phase 6: The Execution Sandbox
*   **Action:** Build a simple `/tmp/.agent-context/sandbox/` directory. Do not overcomplicate with in-memory filesystems yet.
*   **Action:** Force the LLM to output **Unified Diff Patches** instead of raw file rewrites for precise, low-token edits.
*   **Action:** Implement the SWE-Bench Debug Loop with categorized failures (`syntax_error`, `test_failure`, `runtime_error`, `logic_error`) to adjust the retry strategy automatically.

### Phase 7: Multi-Agent & Dream Cycle
Add the final layers of autonomy.
*   **Action:** Implement the Reviewer Agent to double-check the Implementer Agent's work before sandbox commit.
*   **Action:** Build the background Dream Cycle to consolidate and summarize the vector store.
*   **Action:** Implement the Refactor Agent, but restrict it to *Proposing* refactors to the user (`y/n`) rather than autonomously committing dangerous sweeps.

---

## PART IX: RESOLVED OPEN QUESTIONS

1. **Tool Call Format:** Move away from nested JSON. Small models perform significantly better with simple structured plaintext or XML blocks (e.g., `<tool>name: open_file\nargs: path</tool>`).
2. **Intent Classifier:** Must be implemented as a distinct layer (`src/core/context/task_classifier.py`) prior to Context Building.
3. **Execution Sandbox Virtualization:** Keep it simple for MVP. Copy files to a physical `/tmp/` directory, apply unified diffs, run `pytest`, and copy back if successful.


## PART VII: CURRENT SYSTEM MAP

```text
Repository: CodingAgent

├── agent-brain
│   ├── agents
│   │   ├── coding_agent.md
│   │   ├── full_stack_engineer.md
│   │   └── qa_lead.md
│   ├── skills
│   │   ├── context_hygiene.md
│   │   └── dry.md
│   ├── templates
│   │   ├── architecture.md
│   │   ├── concerns.md
│   │   ├── conventions.md
│   │   ├── stack.md
│   │   ├── structure.md
│   │   └── testing.md
│   ├── workflows
│   │   ├── debug.md
│   │   └── plan_phase.md
│   ├── LAWS.md
│   ├── SOUL.md
│   ├── system_prompt_coding.md
│   ├── system_prompt_planner.md
│   └── system_prompts.md
├── docs
│   ├── ARCHITECTURE.md
│   ├── FINAL_AUDIT_REPORT.md
│   ├── memory-implementation.md
│   ├── NEW_AUDIT_INSTRUCTIONS.md
│   ├── system_capability_report.md
│   ├── tooloptimization.md
│   └── tuispec.md
├── scripts
│   ├── add_provider.py
│   ├── check_providers_and_models.py
│   ├── diagnose_lmstudio.py
│   ├── fetch_ollama.py
│   ├── generate_system_map.py
│   ├── list_prompts.py
│   ├── run_generate.py
│   ├── run_tests_settings.py
│   ├── run_tui.py
│   ├── simulate_tui.py
│   ├── start_tui.py
│   ├── test_langgraph_node.py
│   ├── test_tools.py
│   ├── validate_ollama.py
│   └── wait_for_model.py
├── src
│   ├── adapters
│   │   ├── lm_studio_adapter.py
│   │   └── ollama_adapter.py
│   ├── config
│   │   └── providers.json
│   ├── core
│   │   ├── orchestration
│   │   │   ├── agent_brain.py
│   │   │   ├── event_bus.py
│   │   │   ├── langgraph_node.py
│   │   │   ├── message_manager.py
│   │   │   ├── orchestrator.py
│   │   │   └── schema.json
│   │   ├── telemetry
│   │   │   ├── consumer.py
│   │   │   └── metrics.py
│   │   ├── llm_manager.py
│   │   ├── logger.py
│   │   ├── startup.py
│   │   └── user_prefs.py
│   ├── data
│   ├── tools
│   │   ├── file_tools.py
│   │   ├── registry.py
│   │   └── system_tools.py
│   ├── ui
│   │   ├── components
│   │   │   ├── __init__.py
│   │   │   └── log_panel.py
│   │   ├── styles
│   │   │   ├── main.tcss
│   │   │   └── README.md
│   │   ├── views
│   │   │   ├── __init__.py
│   │   │   ├── main_view.py
│   │   │   ├── provider_panel.py
│   │   │   └── settings_panel.py
│   │   ├── __init__.py
│   │   ├── app.py
│   │   ├── README.md
│   │   ├── textual_app.py
│   │   └── textual_app_impl.py
│   ├── main.py
│   └── tmp_app_started.log
├── pyproject.toml
├── README.md
├── requirements.txt
```
