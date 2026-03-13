# Unified Implementation Plan
## Hybrid Architecture: Claude Code + Hermes Agent + SWE-Agent + Current CodingAgent

This document defines the **single coherent implementation roadmap** for evolving the current CodingAgent into a **stable SWE-grade autonomous development agent**.

It merges the best architectural ideas from:

- Claude Code
- Hermes Agent
- SWE-agent / SWE-bench systems
- The existing CodingAgent architecture

while **respecting the current implementation plan** and **avoiding duplicate or conflicting logic**.

The goal is:

> Build a **stable minimal working system first**, then incrementally add intelligence layers.

The architecture is divided into **three stages**:

1. **Stabilization Layer (MVP)**
2. **Cognitive Agent Layer**
3. **Advanced SWE Agent Layer**

Each stage explicitly lists:

- components to implement
- integration points with your current system
- constraints preventing duplicate logic

---

# STAGE 1 — SYSTEM STABILIZATION (MINIMAL VIABLE PRODUCT)

This stage ensures the system **runs reliably before advanced intelligence is introduced.**

Target outcome:

A **stable coding agent that can:**

- read project files
- plan tasks
- execute safe edits
- run tests
- avoid infinite loops
- maintain session memory

No advanced autonomy yet.

---

# 1. Replace the Monolithic Orchestrator with a Minimal Cognitive Loop

Current system:


run_agent_once()
while loop


This mixes all responsibilities.

Instead implement **a minimal pipeline wrapper** but keep the existing orchestrator logic.

New structure:


orchestrator.py
run_pipeline()

pipeline/
perceive()
plan()
execute()
verify()


This is **NOT yet LangGraph**, just structural separation.

Avoids the current "god function".

---

# 2. Implement ContextBuilder (Highest Priority)

Current issue:


agent_brain loads
SOUL.md
LAWS.md
persona
available_tools


But **no token budgeting exists.**

Create:


src/core/context/context_builder.py


Responsibilities:


assemble_system_prompt()

insert:
identity
role
skills
toolset
memory
task state

respect token budget


Final prompt layout:

<identity> SOUL.md </identity> <laws> LAWS.md </laws> <role> strategic | operational </role> <skills> selected skill files </skills>

<task_state>
TASK_STATE.md
</task_state>

<available_tools>
toolset definition
</available_tools>


This **replaces direct prompt construction in agent_brain.py**.

Do NOT duplicate this logic elsewhere.

---

# 3. Scaffold `.agent-context` Memory

Create automatically on startup:


.agent-context/

ACTIVE.md
TASK_STATE.md
PLANS.md
NEXT.md
execution_trace.json
edits.log
session.db


These files become the **agent’s working mind**.

Do not store reasoning inside conversation history anymore.

---

# 4. Replace Message Dropping with Context Distillation

Current behavior:


MessageManager drops oldest messages


This causes context collapse.

Replace with:


if tokens > threshold:
summarize older messages
update TASK_STATE.md
drop summarized messages


This creates **fake long context** for small models.

Only MessageManager performs this logic.

Avoid duplicating summarization elsewhere.

---

# 5. Introduce Toolsets (Hermes Pattern)

Current issue:


all tools injected every run


Bad for small models.

Add:


tools/toolsets/

coding.yaml
debug.yaml
review.yaml
planning.yaml


Example:


coding.yaml

read_file
write_patch
search_code
run_tests


The ContextBuilder selects the toolset based on **role**.

Tool registry remains unchanged.

---

# 6. Introduce Execution Trace (Loop Prevention)

Create:


.agent-context/execution_trace.json


Structure:


{
"goal": "...",
"steps": [
{ "tool": "...", "args": "...", "result": "..."}
]
}


Before executing a tool:


if identical tool+args repeated:
block execution
request new plan


Prevents infinite loops.

---

# 7. Add SQLite Session Store (Hermes Pattern)

Create:


.agent-context/session.db


Tables:


messages
tool_calls
errors
plans
decisions


Used for:

- conversation retrieval
- debugging
- trajectory generation

Not used in prompt building directly.

---

# MVP COMPLETION CRITERIA

The system is **stable** when it can:

1. run full loop without crashing
2. generate a plan
3. modify code safely
4. run tests
5. prevent infinite loops
6. preserve task state across runs

Once these work reliably, move to Stage 2.

---

# STAGE 2 — COGNITIVE AGENT LAYER

Now we upgrade the agent's reasoning capabilities.

---

# 1. Introduce LangGraph Cognitive Pipeline

Replace the minimal pipeline with:


Perception
Understanding
Planning
Execution
Verification
Reflection
Memory Update


Graph:


Perception
↓
Understanding
↓
Planning
↓
Execution
↓
Verification
↓
Reflection
↓
Memory Update


Each node receives **structured state**.

Avoid calling the LLM inside every node.

Only these nodes use LLM:


understanding
planning
reflection


---

# 2. Implement Dual-Brain Architecture

Split reasoning into two agents.


Strategic Brain
Operational Brain


Strategic brain:


planning
architecture decisions
workflow selection


Operational brain:


tool execution
code edits
debugging
verification


Routing rule:


planning tasks → strategic role
execution tasks → operational role


The ContextBuilder switches **role prompts** accordingly.

---

# 3. Add Graph-Based Code Memory

Replace naive file search.

Create:


.agent-context/symbol_graph.db


Graph nodes:


file
class
function
test
module


Edges:


calls
imports
tested_by
defines


Graph built from AST parsing.

Agent queries:


find functions calling X
find tests for module Y


This replaces large context retrieval.

---

# 4. Incremental AST Indexing

Do not rebuild index every run.

Instead:


watch filesystem
reparse changed files
update graph


Store AST metadata:


symbols
docstrings
imports
function signatures


---

# 5. Execution Sandbox (SWE-Agent Pattern)

Before committing edits:


apply patch to temp workspace
run:
AST validation
ruff
mypy
pytest


If failure:


send structured failure to agent


Agent retries.

---

# 6. Self-Debugging Loop

When tests fail:


analyze failure
locate relevant code
patch fix
re-run tests


Max retries:


3 attempts


Prevents infinite debug loops.

---

# STAGE 3 — ADVANCED SWE AGENT CAPABILITIES

Once the system is stable and intelligent.

---

# 1. Autonomous Refactoring Loops

Agent can detect:


dead code
duplicate logic
poor architecture


Workflow:


detect smell
propose refactor
simulate
review
apply


---

# 2. Multi-Agent Code Review

Add second agent:


review_agent


Flow:


execution agent writes patch
review agent critiques
execution agent revises


This dramatically improves reliability.

---

# 3. Dream Cycle Memory Consolidation

Background process:


cluster vector memories
summarize
store high level knowledge


Prevents vector store growth.

---

# 4. Skill Learning System

Inspired by Hermes.

Agent writes new skills:


agent-brain/skills/


Example:


fastapi_endpoint_creation.md
pytest_failure_diagnosis.md


Skills added after successful task completion.

---

# 5. Trajectory Logging (Training Data)

Store runs:


.agent-context/trajectories/


Format:


task
plan
tool sequence
patch
tests
success/failure


This enables training future models.

---

# FINAL ARCHITECTURE SUMMARY


UI
↓
Orchestrator
↓
LangGraph Cognitive Pipeline
↓
Strategic Brain (planner)
Operational Brain (executor)
↓
ContextBuilder
↓
Toolsets
↓
Execution Sandbox
↓
Memory System
Tier0 identity
Tier1 skills
Tier2 task state
Tier3 session files
Tier4 vector memory
Tier5 symbol graph


---

# GUARANTEES AGAINST DOUBLE LOGIC

This plan explicitly prevents duplication:

| Responsibility | Component |
|---|---|
prompt assembly | ContextBuilder |
conversation storage | MessageManager |
session storage | SQLite |
loop detection | execution_trace |
planning | strategic brain |
execution | operational brain |
verification | sandbox |

Each function has **exactly one owner**.

---

# FINAL DEVELOPMENT ORDER

Strict implementation sequence:

1. ContextBuilder
2. `.agent-context` memory
3. Message distillation
4. toolsets
5. execution_trace loop prevention
6. SQLite session memory
7. minimal pipeline
8. LangGraph pipeline
9. dual brain
10. AST index
11. execution sandbox
12. self-debugging loop
13. review agents
14. refactor loops
15. dream memory

Never skip steps.

---

This sequence produces a **stable local SWE agent architecture optimized for 9B–14B m