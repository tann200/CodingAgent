import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


def _get_last_plan_path(workdir: str) -> Path:
    """Get the path to the last plan JSON file."""
    return Path(workdir) / ".agent-context" / "last_plan.json"


def _load_last_plan(workdir: str) -> Dict[str, Any]:
    """Load the last plan from JSON file if it exists."""
    plan_path = _get_last_plan_path(workdir)
    if plan_path.exists():
        try:
            data = json.loads(plan_path.read_text())
            logger.info(f"planning_node: loaded last plan from {plan_path}")
            return data
        except Exception as e:
            logger.warning(f"planning_node: failed to load last plan: {e}")
    return {}


def _save_last_plan(workdir: str, plan: list, task: str, step: int = 0) -> None:
    """Save the current plan to JSON file for cross-session persistence."""
    from datetime import datetime

    plan_path = _get_last_plan_path(workdir)
    try:
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "plan": plan,
            "task": task,
            "current_step": step,
            "saved_at": datetime.now().isoformat(),
        }
        plan_path.write_text(json.dumps(data, indent=2))
        logger.info(f"planning_node: saved plan to {plan_path}")
    except Exception as e:
        logger.warning(f"planning_node: failed to save last plan: {e}")


async def planning_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Planning Layer: Converts perception outputs into a structured plan.
    Uses the 'strategic' role from ContextBuilder (loaded from agent-brain).
    """
    # P1-2: Increment inner planning-loop counter FIRST so all return paths carry it
    plan_attempts = int(state.get("plan_attempts") or 0) + 1

    # P2-9: Reset plan_mode_approved so each fresh plan requires re-approval.
    # Without this, a stale True from a prior approval skips the gate on subsequent plan cycles.

    # Validate orchestrator
    try:
        orchestrator = _resolve_orchestrator(state, config)
        if orchestrator is None:
            logger.error("planning_node: orchestrator is None")
            return {
                "current_plan": state.get("current_plan", []),
                "current_step": state.get("current_step", 0),
                "plan_attempts": plan_attempts,
                "plan_mode_approved": None,
                "errors": ["orchestrator not found"],
            }
    except Exception as e:
        logger.error(f"planning_node: failed to get orchestrator: {e}")
        return {
            "current_plan": state.get("current_plan", []),
            "current_step": state.get("current_step", 0),
            "plan_attempts": plan_attempts,
            "plan_mode_approved": None,
            "errors": [f"config error: {e}"],
        }

    # Treat state as a plain dict for flexible lookups
    s = dict(state)
    s["plan_attempts"] = plan_attempts

    # 4.4: Cross-session plan persistence - Load last plan if current is empty
    working_dir = s.get("working_dir", ".")
    current_plan = s.get("current_plan")
    current_step = s.get("current_step", 0)
    task = str(s.get("task") or "")

    if not isinstance(current_plan, list) or len(current_plan) == 0:
        # Try to load from last_plan.json
        last_plan_data = _load_last_plan(working_dir)
        if last_plan_data and last_plan_data.get("plan"):
            loaded_plan = last_plan_data["plan"]
            loaded_task = last_plan_data.get("task", "")
            loaded_step = last_plan_data.get("current_step", 0)

            # Only resume if the task is similar enough — exact match or high word overlap.
            # Exact match handles identical re-submissions; fuzzy match handles minor
            # rephrasing of the same task (e.g. trimmed whitespace, punctuation).
            def _task_similar(a: str, b: str) -> bool:
                if a == b:
                    return True
                a_words = set(re.sub(r"[^a-z0-9]", " ", a.lower()).split())
                b_words = set(re.sub(r"[^a-z0-9]", " ", b.lower()).split())
                if not a_words or not b_words:
                    return False
                overlap = len(a_words & b_words) / max(len(a_words), len(b_words))
                return overlap >= 0.8

            if loaded_plan and task and _task_similar(loaded_task, task):
                logger.info(
                    f"planning_node: resuming from saved plan with {len(loaded_plan)} steps"
                )
                return {
                    "current_plan": loaded_plan,
                    "current_step": loaded_step,
                    "task_decomposed": True,
                    "plan_resumed": True,
                    "plan_attempts": plan_attempts,
                    "plan_mode_approved": None,
                }

    # If the perception already provided a next_action, try to build a simple plan

    # Minimal planner: if next_action exists, make a one-step plan; otherwise ask the LLM
    current_plan = s.get("current_plan")
    if not isinstance(current_plan, list):
        current_plan = []
    current_step = s.get("current_step")
    if not isinstance(current_step, int):
        current_step = 0
    task_decomposed = bool(s.get("task_decomposed", False))

    # If we already have a decomposed plan with steps, use it
    if task_decomposed and current_plan and current_step < len(current_plan):
        plan_len = len(current_plan)
        logger.info(f"Using decomposed plan: step {current_step + 1}/{plan_len}")
        step_desc = ""
        if current_step < len(current_plan):
            step_desc = str(current_plan[current_step].get("description", ""))
        return {
            "current_plan": current_plan,
            "current_step": current_step,
            "task": step_desc,
            "plan_attempts": plan_attempts,
            "plan_mode_approved": None,
        }

    next_action = s.get("next_action")
    if next_action:
        # Construct a trivial plan wrapping the existing action
        step = {
            "action": next_action,
            "description": "Execute the requested tool",
        }
        current_plan = [step]
        current_step = 0
        # 4.4: Persist simple plan for cross-session persistence
        _save_last_plan(working_dir, current_plan, task, current_step)
        return {"current_plan": current_plan, "current_step": current_step, "plan_attempts": plan_attempts, "plan_mode_approved": None}

    # Fallback: ask the model for a short plan (non-blocking best effort)
    try:
        builder = ContextBuilder(working_dir=state.get("working_dir"))
        history = s.get("history")
        if not isinstance(history, list):
            history = []

        # Build repo-aware context from analysis output
        analysis_summary = str(s.get("analysis_summary") or "No analysis available")
        relevant_files = s.get("relevant_files") or []
        key_symbols = s.get("key_symbols") or []

        repo_context = ""
        if relevant_files or key_symbols:
            repo_context = "\n\nRepository Context:\n"
            if relevant_files:
                repo_context += f"- Relevant files: {', '.join(str(f) for f in relevant_files[:10])}\n"
            if key_symbols:
                repo_context += (
                    f"- Key symbols: {', '.join(str(s) for s in key_symbols[:10])}\n"
                )
            if analysis_summary and analysis_summary != "No analysis available":
                repo_context += f"- Analysis: {analysis_summary}\n"

        # #56: Inject analyst subagent deep-dive findings when available
        analyst_findings = s.get("analyst_findings") or ""
        analyst_context = ""
        if analyst_findings:
            analyst_context = f"\n\nAnalyst Findings:\n{analyst_findings}\n"
            logger.info("planning_node: injecting analyst_findings into prompt")

        # P3-1: Inject call graph and test map as structured JSON blocks
        import json as _json
        call_graph = s.get("call_graph")
        test_map = s.get("test_map")
        graph_context = ""
        if call_graph:
            graph_context += (
                f"\n\nCall Graph (symbol → callers):\n"
                f"```json\n{_json.dumps(call_graph, indent=2)}\n```"
            )
        if test_map:
            graph_context += (
                f"\n\nTest Map (module → test files):\n"
                f"```json\n{_json.dumps(test_map, indent=2)}\n```"
            )
        if graph_context:
            logger.info("planning_node: injecting call_graph/test_map into prompt")

        # P4-5: Auto-suggest test steps when test_map identifies relevant test files.
        test_hint = ""
        if test_map:
            test_files = []
            for module, tests in test_map.items():
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
                logger.info(f"planning_node: injecting test hint ({len(unique_tests)} files)")

        # MC-4 fix: Request structured JSON output with specific schema to eliminate
        # 4-strategy parsing fragility. The LLM is more likely to produce consistent
        # JSON when explicitly instructed with the expected format.
        # P3-6: Few-shot DAG examples increase valid JSON DAG output rate.
        full_task = f"""Task: {task}{repo_context}{analyst_context}{graph_context}{test_hint}

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

        # Use strategic role from AgentBrainManager
        provider_capabilities = {}
        if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
            provider_capabilities = orchestrator.get_provider_capabilities()

        messages = builder.build_prompt(
            role_name="strategic",
            active_skills=[],
            task_description=full_task,
            tools=[],
            conversation=history,
            max_tokens=3000,  # P5 fix: 1500 truncated complex multi-step plans
            provider_capabilities=provider_capabilities,
        )

        cancel_event = state.get("cancel_event")
        if not cancel_event:
            cancel_event = getattr(orchestrator, "cancel_event", None)

        # F14: call_model is always async; use create_task directly.
        # GAP 2: Hardcode temperature for planning (0.3 for slight creativity)
        llm_task = asyncio.create_task(
            call_model(
                messages,
                stream=False,
                format_json=False,
                temperature=0.3,
                session_id=state.get("session_id"),
            )
        )
        while not llm_task.done():
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                llm_task.cancel()
                logger.info("planning_node: Task canceled mid-generation")
                return {
                    "current_plan": current_plan,
                    "current_step": current_step,
                    "plan_attempts": plan_attempts,
                    "plan_mode_approved": None,
                    "errors": ["canceled"],
                }
            await asyncio.sleep(0.2)
        try:
            resp = await llm_task
        except asyncio.CancelledError:
            raise  # propagate — node itself was cancelled; do not swallow

        content = ""
        if isinstance(resp, dict):
            if resp.get("choices"):
                ch = resp["choices"][0].get("message")
                if isinstance(ch, dict):
                    content = ch.get("content") or ""
                elif isinstance(ch, str):
                    content = ch

        # Robust plan parsing with multiple fallback strategies
        steps = _parse_plan_content(content)

        # Try to parse as DAG first
        from src.core.orchestration.dag_parser import (
            _parse_dag_content,
            _convert_flat_to_dag,
        )

        dag = _parse_dag_content(content)
        if dag:
            # Successfully parsed DAG format
            steps = [
                {
                    "step_id": s.step_id,
                    "description": s.description,
                    "files": s.files,
                    "depends_on": s.depends_on,
                }
                for s in dag.steps.values()
            ]
            logger.info(f"planning_node: parsed DAG plan with {len(steps)} steps")
        elif steps:
            # Fall back to flat list converted to DAG
            dag = _convert_flat_to_dag(steps)
            logger.info(
                f"planning_node: converted flat plan to DAG with {len(steps)} steps"
            )

        # MC-6: Guard against runaway plans that would never complete.
        MAX_PLAN_STEPS = 50
        if steps and len(steps) > MAX_PLAN_STEPS:
            logger.warning(
                f"planning_node: plan has {len(steps)} steps which exceeds "
                f"MAX_PLAN_STEPS={MAX_PLAN_STEPS}; truncating to first {MAX_PLAN_STEPS} steps"
            )
            steps = steps[:MAX_PLAN_STEPS]

        if steps:
            # Persist plan to session store
            try:
                import json as _json

                # Use the orchestrator already resolved at the top of the function (NEW-9).
                # The previous re-fetch via config.get() failed on RunnableConfig objects.
                if orchestrator and hasattr(orchestrator, "session_store"):
                    orchestrator.session_store.add_plan(
                        session_id=getattr(orchestrator, "_current_task_id", "unknown"),
                        plan=_json.dumps(steps),
                        status="created",
                    )
            except Exception:
                pass  # never block execution

            # 4.4: Persist plan to JSON file for cross-session persistence
            _save_last_plan(working_dir, steps, task, 0)

            # Write human-readable TODO.md so user can see the plan
            try:
                from src.tools.todo_tools import manage_todo

                step_descriptions = [
                    s.get("description", f"Step {i + 1}") for i, s in enumerate(steps)
                ]
                manage_todo(
                    action="create", workdir=working_dir, steps=step_descriptions
                )
                logger.info(f"planning_node: wrote TODO.md with {len(steps)} steps")
            except Exception as _te:
                logger.warning(f"planning_node: failed to write TODO.md: {_te}")

            from src.core.orchestration.dag_parser import _convert_flat_to_dag

            dag = _convert_flat_to_dag(steps)
            waves = dag.topological_sort_waves() if dag.validate() else None
            return {
                "current_plan": steps,
                "current_step": 0,
                "plan_dag": {"steps": steps},
                "execution_waves": waves,
                "current_wave": 0,
                "plan_attempts": plan_attempts,
                "plan_mode_approved": None,
            }
    except Exception as e:
        logger.error(f"planning_node: plan generation failed: {e}")

    # F7: Guaranteed fallback — never return an empty plan.
    # An empty plan causes perception → planning → perception loops.
    # Return a single-step plan from the task description so execution can proceed.
    if not current_plan and task:
        fallback_plan = [{"description": task[:200], "action": None}]
        logger.warning("planning_node: plan parse failed, using single-step fallback")
        from src.core.orchestration.dag_parser import _convert_flat_to_dag

        dag = _convert_flat_to_dag(fallback_plan)
        waves = dag.topological_sort_waves() if dag.validate() else None
        return {
            "current_plan": fallback_plan,
            "current_step": 0,
            "plan_dag": {"steps": fallback_plan},
            "execution_waves": waves,
            "current_wave": 0,
            "plan_attempts": plan_attempts,
            "plan_mode_approved": None,
        }

    from src.core.orchestration.dag_parser import _convert_flat_to_dag

    dag = _convert_flat_to_dag(current_plan)
    waves = dag.topological_sort_waves() if dag.validate() else None
    return {
        "current_plan": current_plan,
        "current_step": current_step,
        "plan_dag": {"steps": current_plan},
        "execution_waves": waves,
        "plan_attempts": plan_attempts,
        "current_wave": 0,
        "plan_mode_approved": None,  # P2-9: reset approval gate for each new plan cycle
    }


def _parse_plan_content(content: str) -> list:
    """
    Robust plan parsing with multiple fallback strategies.

    Tries in order:
    1. JSON array extraction (most robust)
    2. Markdown code block JSON
    3. Structured regex parsing for numbered/bulleted lists
    4. Line-by-line fallback
    """
    if not content:
        return []

    # Strategy 1: Try JSON array extraction
    import re
    import json

    # Look for JSON array in content
    json_match = re.search(r"\[[\s\S]*\]", content)
    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, list) and len(parsed) > 0:
                steps = []
                for item in parsed:
                    if isinstance(item, dict):
                        desc = (
                            item.get("description")
                            or item.get("step")
                            or item.get("text")
                            or str(item)
                        )
                        steps.append({"description": desc, "action": None})
                    elif isinstance(item, str):
                        steps.append({"description": item, "action": None})
                if steps:
                    logger.info(
                        f"planning_node: parsed JSON plan with {len(steps)} steps"
                    )
                    return steps
        except (json.JSONDecodeError, Exception):
            pass

    # Strategy 2: Look for markdown code block with JSON
    code_block_match = re.search(
        r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", content, re.IGNORECASE
    )
    if code_block_match:
        try:
            parsed = json.loads(code_block_match.group(1))
            if isinstance(parsed, list):
                steps = []
                for item in parsed:
                    if isinstance(item, dict):
                        desc = item.get("description") or item.get("step") or str(item)
                        steps.append({"description": desc, "action": None})
                    elif isinstance(item, str):
                        steps.append({"description": item, "action": None})
                if steps:
                    logger.info(
                        f"planning_node: parsed code block JSON with {len(steps)} steps"
                    )
                    return steps
        except (json.JSONDecodeError, Exception):
            pass

    # Strategy 3: Structured regex for numbered/bulleted lists
    # Match patterns like: "1. Step description" or "- Step description" or "* Step description"
    # WR-5 fix: collect structured lines (numbered/bullet) and free-text lines
    # separately.  If ANY structured lines are found, use only those — this
    # prevents analysis-context preamble sentences (which appear BEFORE the
    # numbered list) from becoming spurious plan steps.
    structured_lines = []
    freetext_lines = []

    # Pattern for numbered items: 1., 2., 1), 2), etc.
    numbered_pattern = r"^\s*(\d+[\.\)]\s+)(.+)$"
    # Pattern for bullet items: -, *, •, etc.
    bullet_pattern = r"^\s*([\-\*•]\s+)(.+)$"

    action_words = [
        "read",
        "write",
        "edit",
        "create",
        "delete",
        "update",
        "modify",
        "add",
        "remove",
        "run",
        "test",
        "check",
        "verify",
        "install",
        "import",
    ]

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        # Skip markdown table rows (start and end with |)
        if line.startswith("|") and line.endswith("|"):
            continue

        # Skip file/directory listing lines e.g. ".DS_Store (file)", "src/ (directory)"
        if re.match(r"^\.?\S+\s+\((?:file|directory|dir)\)$", line, re.IGNORECASE):
            continue

        # Skip conversational filler lines
        lower_line = line.lower()
        skip_phrases = [
            "here is",
            "here's",
            "plan:",
            "steps:",
            "task:",
            "to do:",
            "sure,",
            "okay,",
        ]
        if any(lower_line.startswith(phrase) for phrase in skip_phrases):
            continue
        if lower_line in ["no steps needed", "no plan needed", "i cannot", "i'm sorry"]:
            continue

        # Skip metadata lines (e.g. PLAN_STEPS: 1, COMPLEXITY: simple)
        if re.match(r"^[A-Z_]+\s*[:=]\s*\S", line):
            continue

        # Try numbered pattern
        match = re.match(numbered_pattern, line)
        if match:
            desc = match.group(2).strip()
            if desc:
                structured_lines.append(desc)
            continue

        # Try bullet pattern
        match = re.match(bullet_pattern, line)
        if match:
            desc = match.group(2).strip()
            if desc:
                structured_lines.append(desc)
            continue

        # Free-text fallback (only used when no structured lines found)
        if len(line) < 200 and any(word in lower_line for word in action_words):
            freetext_lines.append(line)

    # Prefer structured lines; fall back to free-text only when none were found.
    plan_lines = structured_lines if structured_lines else freetext_lines

    if plan_lines:
        steps = [{"description": desc, "action": None} for desc in plan_lines]
        logger.info(f"planning_node: parsed regex plan with {len(steps)} steps")
        return steps

    # Strategy 4: Last resort - only if content looks like a genuine task description
    # (contains action words and is not metadata / file listing output)
    if content and len(content.strip()) < 500:
        stripped = content.strip()
        lower_stripped = stripped.lower()
        # Reject metadata-style output (PLAN_STEPS: 1 etc.)
        if re.match(r"^[A-Z_]+\s*[:=]", stripped):
            return []
        # Reject if the content looks like a file/directory listing
        # (every non-empty line matches the "name (file|directory)" pattern)
        non_empty_lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        if non_empty_lines and all(
            re.match(r"^\.?\S+\s+\((?:file|directory|dir)\)$", ln, re.IGNORECASE)
            for ln in non_empty_lines
        ):
            return []
        # Require at least one action word so bare file listings / status messages
        # don't become single-step plans; count whole-word occurrences to avoid
        # false positives like "test_dir" matching "test".
        if (
            stripped
            and not stripped.startswith("```")
            and any(
                re.search(r"\b" + word + r"\b", lower_stripped) for word in action_words
            )
        ):
            logger.info("planning_node: falling back to single-step plan")
            return [{"description": stripped[:200], "action": None}]

    return []
