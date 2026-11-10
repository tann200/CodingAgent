# Tools & Graph Workflows Engineering Audit (2026-11-09)

**Subsystem:** Core Tools (`src/tools/`), Execution Pipeline, Sandbox, & LangGraph Workflows  
**Auditor:** Antigravity Advanced Agentic Engineering Team  
**Evaluation Date:** November 9, 2026  
**Status:** Deep Inspection Completed — Actionable Remediation Defined  

---

## 1. Executive Summary

A deep technical audit was conducted across the tool execution engine (`src/tools/`), the bubblewrap sandbox harness (`src/tools/sandbox.py`), tool execution pipelines (`src/core/orchestration/tool_execution_pipeline.py`), transaction settlement guards (`src/core/orchestration/rollback_manager.py`), and LangGraph workflow nodes (`finalize_node.py`, `verification_node.py`, `frontier_loop_node.py`).

The architecture provides strong foundational invariants: explicit workspace confinement, read-before-write validation, pre-write diff calculation, and automated verification before commit. However, deep code analysis revealed critical reliability and safety vulnerabilities:
1. **Unhandled `UnicodeDecodeError` in `write_file` and `RollbackManager`:** Overwriting or snapshotting pre-existing binary files (e.g. SQLite databases, images, `.wasm`, `.pyc`) raises unhandled `UnicodeDecodeError` inside `p.read_text(encoding="utf-8")`, crashing tool execution turns and failing transaction rollbacks.
2. **Tool Discovery Decorator Propagation:** In `src/tools/_registry.py`, wrapped or mocked tools that do not preserve `TOOL_ATTR` (`__tool_definition__`) cause `validate_toolsets(fail_closed=True)` to abort with `KeyError`.
3. **Multi-File Transaction Rollback Journal Incompleteness:** When steps generate multiple newly created files and subsequently fail verification, all newly created files must be tracked and removed without leaving orphaned files.
4. **Sandbox Prefix Isolation:** Standard container environments require explicit read-only bindings for `/opt`, `sys.prefix`, and `sys.base_prefix`, with pre-created mount targets for working directories located in `/tmp`.

---

## 2. Architecture Strengths

1. **Transactional Settlement Guard:**
   File mutations are staged under snapshot journals and only committed when verification nodes confirm syntax, lint, and test suites pass. Failing runs trigger automated rollback to the pre-transaction baseline.
2. **Fail-Closed Sandbox Architecture:**
   The bubblewrap sandbox confines tool execution to approved workspace boundaries, preventing symlink escape and unauthorized network egress.
3. **AST-Level Bash Security Analysis:**
   Bash commands undergo abstract syntax tree evaluation, classifying commands into safe, dangerous (requiring operator approval), or blocked tiers.
4. **Idempotency & Diff Preview Gates:**
   Identical file content writes are short-circuited as `no_change` operations, avoiding unnecessary disk churn and model turns.

---

## 3. Critical Architectural Flaws

### Issue TOOL-C1: Unhandled `UnicodeDecodeError` in `write_file` and Snapshotting
- **Location:** `src/tools/_file_io.py:211`, `src/core/orchestration/rollback_manager.py:178,643`
- **Severity:** Critical
- **Failure Mechanism:**
  When an agent overwrites a file or creates a transaction snapshot, the system attempts `p.read_text(encoding="utf-8")`. If the target file contains binary data or non-UTF-8 encodings, Python raises `UnicodeDecodeError`, aborting execution immediately.
- **Impact:** Crashes tool calls and breaks rollback capabilities whenever binary or legacy encoded files exist in the workspace.
- **Remediation:** Safely catch `UnicodeDecodeError` and fall back to safe decoding (`errors="replace"`) or binary detection.

### Issue TOOL-C2: Tool Registry Discovery Fragility with Wrappers
- **Location:** `src/tools/_registry.py:180-220`
- **Severity:** Critical
- **Failure Mechanism:**
  `ToolRegistry.discover()` inspects attributes for `hasattr(obj, TOOL_ATTR)`. Functions decorated or monkey-patched during testing or runtime instrumentation without `@functools.wraps` drop `TOOL_ATTR`, triggering fail-closed registry aborts.
- **Impact:** False-positive system halts during toolset validation.
- **Remediation:** Enforce `@functools.wraps` and explicit `__tool_definition__` propagation in all tool wrappers.

---

## 4. High-Risk Safety Issues

### Issue TOOL-H1: Container Mount Isolation for Non-Standard Python Runtimes
- **Location:** `src/tools/sandbox.py:440-475`
- **Severity:** High
- **Failure Mechanism:**
  Bubblewrap sandbox containers default to standard system directories (`/usr`, `/lib`, `/bin`). When running under virtual environments or CI containers located under `/opt` or non-standard prefixes, tools fail to locate Python interpreters and standard libraries. Furthermore, mounting `/tmp` working directories fails if target directories are not pre-created inside the container tmpfs.
- **Remediation:** Ro-bind `/opt`, `sys.prefix`, and `sys.base_prefix`, and inject `--dir <path>` before `--bind <path> <path>` for temporary directory hierarchies.

### Issue TOOL-H2: Orphaned New Files on Multi-File Transaction Rollback
- **Location:** `src/core/orchestration/rollback_manager.py:150-160`, `src/core/orchestration/graph/nodes/finalize_node.py:80-95`
- **Severity:** High
- **Failure Mechanism:**
  When a multi-file tool call or multi-step turn creates multiple files that previously did not exist, each path must be recorded in the snapshot journal with `exists=False`. If any created file fails to be recorded, settlement guard rollback leaves orphaned files on disk after failed verification.
- **Remediation:** Ensure all mutated and created target paths are registered with `exists=False` before write operations execute.

---

## 5. Major Missing Capabilities

1. **Selective Rollback Capabilities:**
   Rollback was historically all-or-nothing; granular rollback by path or file set allows rolling back individual erroneous steps while retaining valid edits.
2. **Pre-Write Binary Detection:**
   File editing tools should explicitly reject binary modification attempts before attempting textual patch diffing.

---

## 6. Workflow Reliability Issues

1. **Mid-Run In-Flight Operator Steering Ingestion:**
   Operator guidance injected during multi-turn frontier loops must be drained and prepended to prompt context on subsequent turns.
2. **Cancellation Signal Verification Abort:**
   User cancellations must immediately mark verification as failed and trigger settlement rollback rather than committing unverified code.

---

## 7. Tool System Weaknesses

1. **Atomic Edit AST Parsing for In-Place Edits:**
   `edit_file_atomic` requires pre-write AST validation by constructing the projected output text prior to disk write, ensuring syntax validity.

---

## 8. Repository Awareness Gaps

1. **Targeted Verification Heuristics:**
   Edits to non-executable asset files (`.css`, `.md`, `.json`, `.svg`) should not trigger expensive project-wide test suites.

---

## 9. Memory System Evaluation

1. **Transaction Journal Disk Persistence:**
   Transaction journals should be persisted to `.codingAgent/snapshots/` to enable recovery across process restarts.

---

## 10. Evaluation and Testing Gaps

1. **Hermetic Subprocess Mocking in Pipeline Tests:**
   Unit tests executing against temporary directories must hermetically mock host subprocesses (`pytest`, `ruff`) to prevent environment contamination.

---

## 11. Usability Problems

1. **Dangerous Command Approval Blocking:**
   AST security analysis must permit user-approved `BashRiskLevel.DANGEROUS` operations (such as package installs) to proceed rather than blocking unconditionally.

---

## 12. Performance Bottlenecks

1. **Redundant Syntax Validation Passes:**
   Files validated during pre-write sandbox checks should not undergo duplicate syntax verification passes within the write tool implementation.

---

## 13. Over-Engineered Components

1. **Complex Custom Fallback Handlers:**
   Consolidating file reading and writing around central `io_utils.py` and `safe_resolve` simplifies maintenance and eliminates duplicated error-handling logic.

---

## 14. Prioritized Fix List & Roadmap

### Phase 1 — Critical Stability & Safety
1. **Safe File Reading in File I/O & Rollback:** Catch `UnicodeDecodeError` in `write_file` and `RollbackManager` snapshot creation; use fallback decoding with `errors="replace"`.
2. **Multi-File Creation Rollback Guarantee:** Verify that all newly created files in transactions are tracked with `exists=False` and cleanly unlinked on settlement rollback.
3. **Pre-Write AST Validation for `edit_file_atomic`:** Construct candidate text and run AST validation before writing.

### Phase 2 — Workflow Robustness & Container Safeguards
1. **Maintain Sandbox Isolation:** Retain `/opt`, `sys.prefix`, and `--dir` container isolation fixes in `sandbox.py`.
2. **Hermetic Test Harness Guarantees:** Ensure all pipeline orchestration tests mock external verification commands.
3. **Targeted Asset Verification:** Bypass heavy test suites when only non-executable asset files are modified.
