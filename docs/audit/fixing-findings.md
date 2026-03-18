System Refactor Directive: Critical Security & Fast-Path Routing

Primary Directives:
You are tasked with resolving the CRITICAL and HIGH security vulnerabilities identified in the recent system audit, followed by implementing a "Fast-Path" in the LangGraph architecture to prevent over-engineering simple tasks.
Crucial: Do not remove existing capabilities. Enhance and secure the existing logic.

Phase 1: Critical Security Fixes

1.1 Secure the Bash Tool Allowlist (CRITICAL)

Target File: src/tools/file_tools.py
Context: The bash tool currently allows commands like pip, npm, and curl, which opens the system to arbitrary code execution and malware injection.
Action:

Locate the bash tool implementation and its command allowlist.

Remove package managers and network fetchers from the allowlist (pip, npm, node, cargo, curl, wget).

Restrict the bash tool to safe, read-only system utilities (e.g., ls, grep, find, cat, echo, pwd) and safe compilation/test commands if strictly necessary for the workspace.

Ensure shell operators (&&, |, >) remain blocked unless executed within a strictly isolated ExecutionSandbox.

1.2 Enforce Fail-Closed Sandbox Validation (CRITICAL)

Target File: src/core/orchestration/orchestrator.py
Context: Sandbox validation currently fails open. If the ExecutionSandbox import fails or throws an exception, the orchestrator prints a warning and executes the tool anyway.
Action:

Locate the sandbox validation block in orchestrator.py (around line 846+).

Change the except Exception block to explicitly return a failure payload: {"ok": False, "error": f"Sandbox validation aborted: {e}"}.

Ensure no code modification tool (edit_file, write_file, apply_patch) can bypass this return statement.

1.3 Prevent Symlink Path Traversal (HIGH)

Target File: src/tools/file_tools.py
Context: The _safe_resolve function does not adequately protect against symlinks that point outside the working_dir.
Action:

Update _safe_resolve to explicitly resolve all symlinks using os.path.realpath or Path.resolve(strict=True).

Compare the fully resolved target path against the fully resolved working_dir.

If the resolved path does not start with the resolved working_dir, raise a ValueError or return a strict permission denied error.

Phase 2: Simple Task Routing (Fast-Path)

Context: The system currently forces simple 1-step tasks through the entire cognitive pipeline (Perception → Analysis → Planning → Execution), which wastes tokens, increases latency, and causes hallucinations.

2.1 Implement Conditional Fast-Path Routing

Target File: src/core/orchestration/graph/builder.py
Action:

Add a new conditional routing function after perception_node:

def route_after_perception(state: AgentState) -> str:
    # If perception already generated a valid tool call, skip heavy planning
    if state.get("next_action"):
        return "execution"
    return "analysis"


Update the graph edges. Replace the standard edge graph.add_edge("perception", "analysis") with the conditional edge:

graph.add_conditional_edges(
    "perception",
    route_after_perception,
    {
        "execution": "execution",
        "analysis": "analysis"
    }
)


2.2 Clean Up Node Bypasses

Target Files: src/core/orchestration/graph/nodes/analysis_node.py and planning_node.py
Action:

Since builder.py now handles the routing, verify that analysis_node and planning_node do not accidentally overwrite or drop the next_action state if they are somehow forced to run.

Ensure they gracefully pass through state.get("next_action") to the output dictionary.

Phase 3: Documentation Updates

Update docs/ARCHITECTURE.md: Update the pipeline diagrams and documentation to reflect the new conditional "Fast-Path" branching from Perception directly to Execution for simple tasks.

Update docs/audit/fixes-applied.md (Create if it doesn't exist): Log the resolution of the CRITICAL bash and sandbox vulnerabilities identified in the audit.