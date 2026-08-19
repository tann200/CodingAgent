import json
import logging
from typing import Any, Mapping


logger = logging.getLogger(__name__)


def _build_planning_task_description(
    *,
    state: Mapping[str, Any],
    task: str,
    analysis_summary: str,
    relevant_files: list[str],
    key_symbols: list[str],
    repo_lookup_symbols: list[dict[str, Any]],
    plan_step_limit: int,
) -> str:
    """Build the strategic planning task description with repo-aware context."""
    repo_context = ""
    if relevant_files or key_symbols:
        repo_context = "\n\nRepository Context:\n"
        if relevant_files:
            repo_context += (
                f"- Relevant files: {', '.join(str(f) for f in relevant_files[:10])}\n"
            )
        if key_symbols:
            repo_context += (
                f"- Key symbols: {', '.join(str(sym) for sym in key_symbols[:10])}\n"
            )
        if analysis_summary and analysis_summary != "No analysis available":
            repo_context += f"- Analysis: {analysis_summary}\n"

    analyst_findings = state.get("analyst_findings") or ""
    analyst_context = ""
    if analyst_findings:
        analyst_context = f"\n\nAnalyst Findings:\n{analyst_findings}\n"
        logger.info("planning_node: injecting analyst_findings into prompt")

    call_graph = state.get("call_graph")
    test_map = state.get("test_map")
    graph_context = ""
    if call_graph:
        graph_context += (
            f"\n\nCall Graph (symbol → callers):\n"
            f"```json\n{json.dumps(call_graph, indent=2)}\n```"
        )
    if test_map:
        graph_context += (
            f"\n\nTest Map (module → test files):\n"
            f"```json\n{json.dumps(test_map, indent=2)}\n```"
        )
    if graph_context:
        logger.info("planning_node: injecting call_graph/test_map into prompt")

    if not call_graph and state.get("working_dir") and repo_lookup_symbols:
        graph_context += (
            f"\n\n## Relevant Symbols\n"
            f"```json\n{json.dumps(repo_lookup_symbols, indent=2)}\n```"
        )
        logger.info(
            "planning_node: RA-1 injected %d symbols from index",
            len(repo_lookup_symbols),
        )

    test_hint = ""
    if test_map and isinstance(test_map, dict):
        test_files = []
        for _module, tests in test_map.items():
            if isinstance(tests, list):
                test_files.extend(tests[:2])
        if test_files:
            unique_tests = list(dict.fromkeys(test_files))[:4]
            test_hint = (
                f"\n\nTest Coverage Hint: The following test files are relevant to "
                f"the modules being modified. Consider adding a verification step "
                f"to run these tests after the implementation steps: "
                f"{', '.join(unique_tests)}"
            )
            logger.info(
                "planning_node: injecting test hint (%d files)",
                len(unique_tests),
            )

    return f"""Task: {task}{repo_context}{analyst_context}{graph_context}{test_hint}

Analyze the task and create a dependency graph of subtasks.

Output format (JSON DAG):
```json
{{
  "root_task": "Original task description",
  "steps": [
    {{
      "step_id": "step_0",
      "description": "Independent task that can run first",
      "files": ["file1.py", "file2.py"],
      "depends_on": []
    }},
    {{
      "step_id": "step_1",
      "description": "Task depending on step_0",
      "files": ["file3.py"],
      "depends_on": ["step_0"]
    }}
  ]
}}
```

Rules:
- Tasks modifying the SAME file must have dependency relationship
- A task can start when ALL tasks in its `depends_on` list are complete
- Identify the MAXIMUM parallelism possible
- List all files affected by each step
- Maximum {plan_step_limit} steps total. If the task needs more, split it and delegate parts.

--- EXAMPLES ---

Example 1 (sequential dependency):
```json
{{
  "root_task": "Update authentication to use JWT",
  "steps": [
    {{"step_id": "step_0", "description": "Read auth/models.py to understand existing User model", "files": ["auth/models.py"], "depends_on": []}},
    {{"step_id": "step_1", "description": "Add JWT token fields to User model", "files": ["auth/models.py"], "depends_on": ["step_0"]}},
    {{"step_id": "step_2", "description": "Update login view to issue JWT tokens", "files": ["auth/views.py"], "depends_on": ["step_1"]}}
  ]
}}
```

Example 2 (parallel tasks):
```json
{{
  "root_task": "Add input validation to registration and login forms",
  "steps": [
    {{"step_id": "step_0", "description": "Add email validation to registration form", "files": ["forms/register.py"], "depends_on": []}},
    {{"step_id": "step_1", "description": "Add password strength check to registration form", "files": ["forms/register.py"], "depends_on": ["step_0"]}},
    {{"step_id": "step_2", "description": "Add rate limiting to login form (independent)", "files": ["forms/login.py"], "depends_on": []}}
  ]
}}
```

Respond ONLY with valid JSON, no additional text."""
