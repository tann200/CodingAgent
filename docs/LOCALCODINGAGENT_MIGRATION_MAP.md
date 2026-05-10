# Migration Map: CodingAgent <-> LocalCodingAgent

**Primary audience:** `CodingAgent` maintainers  
**Peer repository:** `/Users/tann200/PycharmProjects/LocalCodingAgent`  
**Date:** 2026-05-08

## Purpose

This document describes how `CodingAgent` should converge with `LocalCodingAgent` and how `LocalCodingAgent` should converge with `CodingAgent`.

The goal is not to merge the products into one shape. The goal is to import the strongest patterns from each codebase without carrying over their weakest assumptions.

## Executive Summary

`CodingAgent` has the broader platform architecture.

`LocalCodingAgent` has the cleaner execution story.

That means the correct strategy for `CodingAgent` is:

1. stabilize current platform seams first
2. import simplification patterns from `LocalCodingAgent`
3. keep `CodingAgent`'s stronger abstractions where they are already good

The correct strategy for `LocalCodingAgent` is:

1. keep its current workflow and subprocess model
2. import `CodingAgent`'s strongest abstractions incrementally
3. avoid importing `CodingAgent`'s platform breadth until the core abstractions land cleanly

## Ground Truth Before Migration

The first migration step for both repos is to distrust optimistic summary docs and use live code plus live tests.

Observed drift in `CodingAgent`:

- `README.md` reports `3844` passing unit tests.
- `docs/codingagent-architecture.md` reports `500+ tests passing` and a `240 tests` tree summary.
- `docs/CODEBASE_AUDIT_2026-05-07.md` still reports active breakages in `tool_execution_pipeline.py`, `_bash_exec.py`, `_file_io.py`, and `_tool.py` ownership.

Observed drift in `LocalCodingAgent`:

- `README.md` reports `1078 tests passing`.
- `docs/tasks/implementation-tasklist.md` uses `925+` and still describes backlog that partly overlaps with already-existing code patterns.

**Rule:** do not plan migration from doc headlines. Plan migration from the current implementation and a fresh test baseline.

## Comparison At A Glance

| Dimension | CodingAgent | LocalCodingAgent | Direction of import into CodingAgent |
|---|---|---|---|
| Product shape | broad platform: CLI, TUI, server, scheduler, indexing, MCP | focused coding TUI with LangGraph + subprocess delegation | import execution simplicity |
| Workflow | 16-node cognitive pipeline plus variant loops | 9-node mode-driven flow | strong candidate to simplify around |
| Tools | decorator-based registry and permission taxonomy | curated manual registry and mode allowlists | keep Coding registry; import Local clarity |
| Delegation | in-process role/session delegation | subprocess-isolated engineer/QA capsules | strong candidate to import selectively |
| Prompt architecture | more diffuse but feature-rich | GECK doctrine is cohesive | strong candidate to import |
| Guardrails | advanced but spread across more seams | pure declarative guardrails | strong candidate to import |
| Memory | broader session and repo memory stack | narrower episodic retrieval accelerator | keep Coding memory scope |
| UI contract | explicit TUI contract | working implementation with telemetry bridge | import bridge discipline |
| Current risk | higher, due to refactor drift | lower, due to smaller system surface | import Local simplification patterns |

## Shared Migration Rules

1. Do not merge the graphs.
2. Do not port large unstable modules wholesale.
3. Migrate contracts first, implementation second.
4. Use shims and adapters during convergence.
5. Keep product identity intact while capabilities converge.

## What CodingAgent Should Import From LocalCodingAgent

## 1. Prompt Doctrine: A Single Source Of Truth

### Why it matters

One of `LocalCodingAgent`'s strongest design decisions is GECK:

- `GECK/SOUL.md`
- `GECK/LAWS.md`
- `GECK/agents/*.md`
- `GECK/workflows/*.md`

This gives the system:

1. clearer identity
2. less prompt duplication
3. easier review of behavioral changes
4. cleaner role and workflow separation

`CodingAgent` already has role, skill, and identity machinery, but its doctrine is more distributed across config and orchestration layers.

### Recommended target state in CodingAgent

Adopt a GECK-like doctrine layer, even if the name stays different.

### Migration sequence

1. Create one canonical identity file.
2. Create one canonical laws or invariants file.
3. Create role-specific prompt files that are loaded by one loader.
4. Reduce duplicated inline prompt fragments in orchestration code.

### What not to do

Do not port Local's exact file names or folder names blindly. Import the architecture, not the branding.

## 2. Subprocess Capsule Execution For Write-Critical Work

### Why it matters

`LocalCodingAgent` uses subprocess isolation for `implement` and `review` style work. That is slower, but it creates strong boundaries for high-risk writing tasks.

Relevant Local files:

- `src/core/orchestration/subprocess_agent.py`
- `src/core/orchestration/state.py` (`TaskChunk`, `FeaturePlan`, `ExecutionCapsule`)

### Why this helps CodingAgent specifically

`CodingAgent` already has rich in-process delegation and role metadata. Its problem is not lack of breadth. Its problem is operational complexity and drift.

Subprocess capsules would help with:

1. high-risk write steps
2. reviewer passes
3. long-running isolated sub-tasks
4. reducing parent-session contamination

### Recommended target state in CodingAgent

Hybrid delegation:

1. keep in-process delegation for lightweight analysis and planning
2. use subprocess isolation for implementation and verification capsules

### Migration sequence

1. Define a capsule payload format in `CodingAgent` terms.
2. Add subprocess-backed execution for one narrow role first, likely reviewer or operational writer.
3. Keep the existing `delegate_task` path for low-risk tasks.
4. Add crash recovery markers and subagent run metadata only after the subprocess path is stable.

## 3. Chunked Delivery Loop: Plan -> Implement -> Review -> Finalize

### Why it matters

`LocalCodingAgent` encodes a very software-engineering-specific loop:

1. decompose into chunks
2. implement a scoped chunk
3. review against acceptance criteria
4. finalize and move on

`CodingAgent` has more cognitive richness, but that richness can make the common case harder to follow.

### Recommended target state in CodingAgent

Do not replace the full pipeline. Instead, introduce a chunk contract that downstream execution must satisfy.

This can sit beneath the current planner:

1. planner produces chunked work items
2. execution operates on one chunk or one wave
3. verification runs per chunk
4. evaluation aggregates at the task level

### Benefit

This imports Local's strongest delivery discipline without discarding `CodingAgent`'s broader orchestration capabilities.

## 4. Pure Declarative Guardrails

### Why it matters

`LocalCodingAgent/src/tools/guardrails.py` is easy to read and easy to reason about. Each guardrail is a pure function.

`CodingAgent` has stronger overall safety goals, but some ownership has drifted across tool modules, preview logic, approval, and permission handling.

The audit already points at this kind of split ownership as a repeated failure mode.

### Recommended target state in CodingAgent

Keep the advanced safety architecture, but centralize pure guardrail functions behind a stable contract:

`(tool_name, args, state) -> result`

### Migration sequence

1. define the pure guardrail interface
2. consolidate read-before-write, deny patterns, and file-size or scope logic into one ownership module
3. let approval and preview layers sit on top, not inside low-level file tools

### Benefit

This makes the system easier to test and reduces the alias-drift pattern documented in the audit.

## 5. Telemetry Bridge Pattern For UI Isolation

### Why it matters

`CodingAgent` already wants the TUI to be swappable and has a strong TUI spec. `LocalCodingAgent` contributes a practical implementation pattern: stable core telemetry plus a UI-specific bridge.

### Recommended target state in CodingAgent

Retain the EventBus as the system-wide primitive, but define a bridge layer between core events and UI widget updates.

### Benefit

This reduces the chance that TUI widget code grows direct dependency on orchestration internals, which would violate `CodingAgent/docs/PRINCIPLES.md`.

## 6. Execution Simplicity And Smaller Common-Case Paths

### Why it matters

`LocalCodingAgent` is smaller. That means its common-case flows are easier to audit and easier to reason about.

`CodingAgent` should import this bias wherever it does not compromise capability.

### Where to apply it

1. keep a smaller default path for normal coding tasks
2. use the richer platform features only when needed
3. reduce special-case branches in the most common execution path

### Related current assets in CodingAgent

This already aligns with:

- `docs/PRINCIPLES.md`
- `docs/REFACTORING_PLAN.md`
- `src/core/inference/workflow_selector.py`

The missing piece is not concept. The missing piece is consistent execution.

## What LocalCodingAgent Should Import From CodingAgent

This section is included because convergence is bidirectional and the two projects should move toward compatible abstractions.

## 1. Tool Decorator, ToolDefinition, And Registry Metadata

### Why it matters

This is one of `CodingAgent`'s best abstractions and it maps directly to `LocalCodingAgent` backlog work.

Relevant CodingAgent files:

- `src/tools/_tool.py`
- `src/tools/_registry.py`

Relevant Local backlog:

- `docs/tasks/implementation-tasklist.md` `TASK-12`

### Recommended target state in LocalCodingAgent

Introduce a minimal decorator and metadata layer for new tools first, then progressively adapt the registry.

### Important caveat

`LocalCodingAgent` should import the abstraction, not the full registry machinery and not the plugin complexity.

## 2. Permission Taxonomy

### Why it matters

`CodingAgent`'s `PermissionKind` enum is a cleaner conceptual model than Local's implicit semantics.

### Recommended target state in LocalCodingAgent

Keep mode gating for UX, but annotate tools with semantic permission kinds.

## 3. Repo Indexing And Symbol Intelligence

### Why it matters

This is the biggest capability gap in `LocalCodingAgent`.

Relevant CodingAgent files:

- `src/core/indexing/repo_indexer.py`
- `src/core/indexing/symbol_graph.py`
- `src/core/indexing/lsp_manager.py`

### Recommended target state in LocalCodingAgent

Add indexing and symbol lookup incrementally, storing results in the existing Local runtime area first.

## 4. EventBus Correlation IDs And Optional Tracing

### Why it matters

`LocalCodingAgent` already has durable telemetry. `CodingAgent` adds a useful correlation and tracing model.

### Recommended target state in LocalCodingAgent

Add correlation IDs and SQLite execution spans first, then consider optional OTel export much later.

## 5. TUI/Core Public Contract

### Why it matters

`CodingAgent/docs/TUI_SPEC.md` is an architectural asset. `LocalCodingAgent` should not adopt the whole TUI implementation, but it should document the public contract between controller, core, and UI widgets.

## 6. Packaging And CI

### Why it matters

`CodingAgent`'s `pyproject.toml` and CI setup are stronger than what is currently visible in `LocalCodingAgent`.

### Recommended target state in LocalCodingAgent

Adopt `pyproject.toml`, extras, and CI incrementally without forcing an immediate install-path migration.

## Detailed Subsystem Crosswalk

| Subsystem | CodingAgent today | LocalCodingAgent today | Best migration direction |
|---|---|---|---|
| Prompt doctrine | more distributed | GECK single source of truth | CodingAgent should import Local prompt discipline |
| Graph topology | richer, heavier, more variants | smaller, mode-driven | keep both separate |
| State model | large TypedDict with many operational fields | dataclass-heavy state and capsule types | neither should port wholesale |
| Tool metadata | strong decorator and registry abstraction | simpler manual registry | Local should import Coding metadata model |
| Permission semantics | explicit taxonomy | more implicit semantics | Local should import Coding permission kinds |
| Guardrails | stronger safety ambition, more ownership spread | pure declarative guardrails | Coding should import Local clarity |
| Delegation | in-process subagent/session model | subprocess capsules | Coding should add subprocess option |
| Memory and indexing | broader platform and repo intelligence | narrower retrieval accelerator | Local should import indexing only |
| UI contract | formal and explicit | practical bridge-oriented implementation | both should converge |
| Telemetry | EventBus, metrics, tracer | persistent SQLite telemetry | both should converge on correlation + spans |
| Packaging | `pyproject.toml`, extras, CI | more app-centric setup | Local should import Coding packaging maturity |

## Explicit Do-Not-Migrate List

Do not migrate these into `CodingAgent` as first-wave changes:

1. `AppServiceLocator` as-is
2. Local's exact dataclass `AgentState`
3. Local's narrower provider model
4. Local's exact UI implementation

Do not migrate these into `LocalCodingAgent` as first-wave changes:

1. `CodingAgent`'s 16-node graph
2. HTTP server and scheduler stack
3. plugin-heavy registry behavior
4. runtime directory rename to `.codingAgent`
5. any module still implicated by `docs/CODEBASE_AUDIT_2026-05-07.md`

## Recommended Roadmap For CodingAgent

## Wave 0: Stabilize The Current Platform

1. fix the blockers identified in `docs/CODEBASE_AUDIT_2026-05-07.md`
2. establish one trustworthy execution path for tool dispatch
3. remove schema-generation ownership ambiguity
4. update docs to reflect live status rather than aspirational status

## Wave 1: Import Local Execution Discipline

1. create a doctrine layer for prompt identity, laws, roles, and workflows
2. define chunk and capsule contracts for coding tasks
3. add subprocess isolation for high-risk write or review phases
4. centralize pure declarative guardrails

## Wave 2: Converge Safely

1. align EventBus and telemetry bridge semantics
2. align approval and verification vocabulary
3. use chunked execution inside the richer platform where it clearly helps

## Recommended Roadmap For LocalCodingAgent

## Wave 0: Establish Baseline

1. rerun tests
2. resolve active backlog that already aligns with imported abstractions
3. document UI and telemetry contracts

## Wave 1: Import CodingAgent Abstractions

1. `@agent_tool` and `ToolDefinition`
2. `PermissionKind`
3. correlation IDs and execution spans
4. packaging and CI

## Wave 2: Import Capability Features

1. repo indexing and symbol intelligence
2. model and hardware profiling
3. optional eventing and server transport only if needed

## File-Level Landing Map

| Desired capability | Import source | CodingAgent landing zone |
|---|---|---|
| prompt doctrine | `LocalCodingAgent/GECK/*` pattern | new doctrine and instruction loader surfaces |
| subprocess capsules | `LocalCodingAgent/src/core/orchestration/subprocess_agent.py` | `src/core/orchestration/` subprocess service |
| chunk contract | `LocalCodingAgent/src/core/orchestration/state.py` | planning and execution payload models |
| pure guardrails | `LocalCodingAgent/src/tools/guardrails.py` | centralized guardrail ownership module |
| telemetry bridge pattern | `LocalCodingAgent/src/ui/controller.py` and telemetry bridge pattern | `tui/src/ui/` bridge layer |

| Desired capability | Import source | LocalCodingAgent landing zone |
|---|---|---|
| decorator-based tool metadata | `CodingAgent/src/tools/_tool.py` | `src/tools/decorators.py` |
| richer registry seam | `CodingAgent/src/tools/_registry.py` | `src/tools/registry.py` |
| permission taxonomy | `CodingAgent/src/tools/_tool.py` | `src/tools/permissions.py` or `src/tools/decorators.py` |
| repo indexing | `CodingAgent/src/core/indexing/repo_indexer.py` | new `src/core/indexing/` |
| correlation IDs | `CodingAgent/src/core/orchestration/event_bus.py` | `src/core/telemetry.py` or new `src/core/event_bus.py` |
| TUI contract style | `CodingAgent/docs/TUI_SPEC.md` | new Local controller and UI contract docs |

## Final Recommendation

`CodingAgent` should not try to become `LocalCodingAgent`.

It should become a more stable and more legible `CodingAgent` by importing Local's strongest qualities:

1. cohesive prompt doctrine
2. subprocess isolation for high-risk work
3. chunk-based delivery discipline
4. pure declarative guardrails
5. tighter bridge seams between core and UI

`LocalCodingAgent` should not try to become the full `CodingAgent` platform.

It should remain the smaller, more coherent product while importing the strongest `CodingAgent` abstractions:

1. tool metadata and schema generation
2. permission taxonomy
3. repo indexing and symbol intelligence
4. TUI/core contract discipline
5. packaging and CI maturity
6. correlation IDs and execution spans
