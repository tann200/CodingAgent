CODING AGENT DEVELOPMENT TASKLIST
P0 — Core Capability Upgrades
Task 1 — Introduce Planning Node

Extend the graph.

New pipeline:

Perception
→ Planning
→ Execution
→ Memory

Planning node outputs structured plan:

Plan:
1 locate target file
2 read file
3 modify function
4 validate change

Add to state:

current_plan
current_step
Task 2 — Implement Repo Index

Add repository intelligence.

Minimum components:

file index
symbol index
function map
import graph

Store in:

.agent-context/repo_index.json

Expose tools:

find_symbol
find_references
search_code
Task 3 — Code Search Tool

Add semantic code search.

Implementation:

embeddings
vector index
top-k retrieval

Libraries:

sentence-transformers
faiss
lancedb

Tool:

search_code(query)
Task 4 — Execution Verification

Add verification node.

Pipeline becomes:

Perception
→ Planning
→ Execution
→ Verification
→ Memory

Verification tools:

run_tests
run_linter
syntax_check
Task 5 — Patch Generation Layer

Replace LLM patch generation.

New workflow:

LLM outputs edit intent
system generates diff

Example intent:

replace function foo with new implementation
P1 — Memory Intelligence
Task 6 — Memory Retrieval

Upgrade memory usage.

Add:

memory_search(query)

Ranking:

recency
semantic similarity
task relevance
Task 7 — Repository Knowledge Memory

Store repo insights:

module summaries
dependency relationships
bug fixes

File:

.agent-context/repo_memory.json
Task 8 — Loop Prevention Improvement

Current loop prevention uses execution_trace.

Add:

duplicate action detection
dead-end detection
retry limit
P2 — Agent Reliability
Task 9 — Structured Tool Contracts

Define schema for each tool.

Example:

{
 tool: str
 args: dict
 result: dict
 error: str
}
Task 10 — Deterministic Mode

Add optional deterministic runs.

temperature = 0
seed control

Used for debugging.

Task 11 — Cost Tracking

Add metrics:

tokens
latency
tool calls
iterations
P3 — Hub-and-Spoke Preparation

You said your goal is hub-and-spoke architecture.

Prepare the current system.

Task 12 — Agent Role Abstraction

Refactor roles into config.

Examples:

planner
coder
reviewer
researcher
Task 13 — Message Bus Upgrade

Your event_bus is a good start.

Extend to support:

multi-agent message routing
agent identity
message priority
Task 14 — Graph Modularity

Allow dynamic graph composition.

Example:

planner_graph
coder_graph
review_graph
Recommended Next Architecture

After improvements:

User Task
 ↓
Planner Node
 ↓
Repo Intelligence
 ↓
Execution Node
 ↓
Verification Node
 ↓
Memory Update

Then hub-and-spoke becomes:

Coordinator
 ├ Planner
 ├ Coder
 ├ Reviewer
 └ Researcher
The Single Most Important Next Feature

Implement repo intelligence (index + code search).

This single feature will increase success rate more than any other improvement.

