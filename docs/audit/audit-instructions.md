# Comprehensive Coding Agent Audit Instructions

You are performing a **deep technical audit** of a local coding agent system. Your task is to analyze the entire repository and architecture and produce a **comprehensive engineering audit**.

The goal of the audit is to determine whether the system is **robust, safe, deterministic, and production-ready as a local coding agent for LLMs**, and to identify **architectural weaknesses, missing safeguards, and reliability gaps**.

You must audit the system across **ALL major categories used to evaluate coding agents**.

---

# Primary Objective

Perform a **full-spectrum audit** of the system covering:

* Architecture correctness
* Reasoning workflow
* Tool usage and safety
* Planning and execution reliability
* Repository awareness
* Memory and context management
* Safeguards and failure handling
* Evaluation and testing
* Usability and developer experience
* Observability and debugging
* Performance and token efficiency
* Security risks
* Extensibility and maintainability
* Missing capabilities compared to modern coding agents

The final output must identify:

* **Critical flaws**
* **Design weaknesses**
* **Missing safeguards**
* **Incomplete implementations**
* **Over-engineered components**
* **Features that exist but are not integrated**
* **Capabilities missing compared to strong coding agents**

---

# Input Sources

You must analyze:

1. The full repository code
2. The architecture documentation
3. the gap analysis document
4. implementation plans
5. tool definitions
6. orchestration logic
7. LangGraph workflows
8. role and skill definitions
9. memory system
10. repository indexing system
11. evaluation and verification tools
12. UI / CLI layer
13. sandbox and safety mechanisms

Focus especially on:

* orchestration pipeline
* tool execution
* planning and reasoning
* code modification safety
* repository awareness
* failure recovery

---

# Required Audit Categories

You MUST evaluate the system in the following categories.

---

# 1. Core Architecture

Evaluate whether the high-level architecture is sound.

Check for:

* separation of concerns
* modularity
* clear system boundaries
* orchestration correctness
* graph workflow correctness
* determinism vs uncontrolled LLM behavior
* tight coupling between components
* dead or unused subsystems

Questions to answer:

* Is the architecture logically sound?
* Are components doing too many responsibilities?
* Are critical capabilities missing from the architecture?
* Are there redundant or unused components?

---

# 2. Reasoning Workflow

Audit the cognitive workflow.

Evaluate:

* perception
* analysis
* planning
* execution
* verification
* debugging

Check whether:

* reasoning is deterministic
* planning is enforced
* execution follows plan
* verification is mandatory
* failure triggers debugging
* retry limits exist

Identify missing workflow components such as:

* step controller
* debug loop
* analysis layer
* plan persistence
* plan-step verification

---

# 3. Tool System Audit

Evaluate the tool architecture.

Check:

* tool registration
* tool validation
* tool contracts
* argument validation
* failure handling
* idempotency
* execution safety

Look for risks such as:

* unrestricted shell access
* dangerous file operations
* destructive actions
* uncontrolled recursion
* infinite tool loops

Also evaluate:

* tool discoverability
* tool documentation quality
* tool grouping
* tool determinism

---

# 4. Repository Awareness

Audit the system’s ability to understand codebases.

Check:

* code indexing
* symbol graph
* semantic search
* reference tracking
* test mapping
* repository summarization

Determine:

* whether repository intelligence exists
* whether it is actually used during planning
* whether retrieval happens automatically
* whether planning is repo-aware

Identify gaps such as:

* missing retrieval-before-planning
* unused symbol graph
* incomplete indexing coverage

---

# 5. Code Modification Safety

Audit safeguards around editing code.

Check whether the agent:

* reads files before editing
* validates patches
* checks syntax
* runs tests
* verifies linting
* prevents partial corruption
* supports patch rollback

Evaluate whether:

* edits are atomic
* changes are reversible
* validation occurs before writing files

---

# 6. Memory and Context Management

Audit the memory system.

Evaluate:

* task state persistence
* context distillation
* conversation memory
* vector memory
* session store
* execution trace tracking

Check for:

* context window protection
* token budgeting
* memory rot prevention
* stale memory reuse
* cross-task contamination

Identify missing features such as:

* plan persistence
* decision memory
* environment awareness
* retrieval-augmented planning

---

# 7. Failure Handling

Evaluate robustness when things go wrong.

Check for:

* tool failure recovery
* LLM hallucination mitigation
* retry limits
* debug loops
* rollback mechanisms
* crash recovery

Look for missing capabilities such as:

* structured debugging
* error classification
* automated recovery strategies

---

# 8. Safeguards and Security

Evaluate system safety.

Check for:

* filesystem sandboxing
* restricted directories
* command injection risks
* prompt injection resilience
* tool misuse prevention
* workspace isolation

Determine whether:

* the agent can accidentally delete repositories
* the agent can execute dangerous shell commands
* safeguards are enforced at runtime

---

# 9. Evaluation and Testing

Audit the evaluation system.

Check whether the project has:

* deterministic tests
* agent scenario tests
* regression tests
* tool tests
* integration tests
* performance benchmarks

Determine whether the system can reliably measure:

* success rate
* edit accuracy
* tool usage correctness
* failure cases

Identify missing evaluation frameworks.

---

# 10. Observability

Evaluate system observability.

Check for:

* logging
* telemetry
* run tracing
* tool call recording
* performance metrics
* debugging visibility

Determine whether developers can easily diagnose failures.

---

# 11. Performance and Efficiency

Evaluate efficiency.

Check for:

* token budgeting
* unnecessary prompt injection
* excessive context loading
* slow retrieval pipelines
* redundant LLM calls

Identify performance bottlenecks.

---

# 12. Usability and Developer Experience

Evaluate how easy the system is to use.

Check:

* CLI usability
* UI clarity
* configuration complexity
* debugging workflow
* onboarding difficulty

Determine whether developers can:

* run the system easily
* understand failures
* extend the system safely

---

# 13. Extensibility

Evaluate future scalability.

Check whether the architecture supports:

* multi-agent expansion
* hub-and-spoke architecture
* new tools
* new workflows
* new reasoning nodes

Identify components that would block future development.

---

# 14. Over-Engineering Analysis

Identify components that are:

* too complex for current needs
* not integrated with the system
* unnecessary for MVP reliability

Examples to check:

* unused advanced memory systems
* dormant research features
* unused agents
* unused toolsets

---

# 15. Missing Capabilities

Compare the system to strong coding agents and identify missing features such as:

* step controller
* repo-aware planning
* deterministic plan execution
* automated debugging loop
* repository intelligence integration
* scenario evaluation framework
* structured plan memory

---

# Required Output Format

Produce a report with the following structure:

1. **Executive Summary**
2. **Architecture Strengths**
3. **Critical Architectural Flaws**
4. **High-Risk Safety Issues**
5. **Major Missing Capabilities**
6. **Workflow Reliability Issues**
7. **Tool System Weaknesses**
8. **Repository Awareness Gaps**
9. **Memory System Evaluation**
10. **Evaluation and Testing Gaps**
11. **Usability Problems**
12. **Performance Bottlenecks**
13. **Over-Engineered Components**
14. **Prioritized Fix List**

---

# Severity Levels

Classify each issue:

Critical
High
Medium
Low

Critical issues are those that could:

* corrupt repositories
* cause infinite loops
* break deterministic execution
* make the agent unreliable.

---

# Final Deliverable

At the end of the report produce a **prioritized engineering roadmap**:

Phase 1 — Critical stability fixes
Phase 2 — Robustness improvements
Phase 3 — Capability improvements
Phase 4 — Advanced features

Each task must include:

* description
* location in the repository
* estimated complexity
* expected impact
