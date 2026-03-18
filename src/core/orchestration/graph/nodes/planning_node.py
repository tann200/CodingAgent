import asyncio
import logging
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


async def planning_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Planning Layer: Converts perception outputs into a structured plan.
    Uses the 'strategic' role from AgentBrainManager for planning-specific context.
    """
    # Get AgentBrainManager for role-specific prompts
    brain = get_agent_brain_manager()
    strategic_role = brain.get_role("strategic") or "You are a strategic planner."

    # Validate orchestrator
    try:
        orchestrator = _resolve_orchestrator(state, config)
        if orchestrator is None:
            logger.error("planning_node: orchestrator is None")
            return {
                "current_plan": state.get("current_plan", []),
                "current_step": state.get("current_step", 0),
                "errors": ["orchestrator not found"],
            }
    except Exception as e:
        logger.error(f"planning_node: failed to get orchestrator: {e}")
        return {
            "current_plan": state.get("current_plan", []),
            "current_step": state.get("current_step", 0),
            "errors": [f"config error: {e}"],
        }

    # Treat state as a plain dict for flexible lookups
    s = dict(state)

    # If the perception already provided a next_action, try to build a simple plan
    task = str(s.get("task") or "")

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
        return {"current_plan": current_plan, "current_step": current_step}

    # Fallback: ask the model for a short plan (non-blocking best effort)
    try:
        builder = ContextBuilder()
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

        full_task = f"Task: {task}{repo_context}\n\nGenerate a step-by-step plan. Each step must reference specific files explicitly."

        # Use strategic role from AgentBrainManager
        messages = builder.build_prompt(
            identity=strategic_role,
            role=f"Planner for task: {task}",
            active_skills=[],
            task_description=full_task,
            tools=[],
            conversation=history,
            max_tokens=1500,
        )

        cancel_event = state.get("cancel_event")
        if not cancel_event:
            cancel_event = getattr(orchestrator, "cancel_event", None)

        raw_resp = call_model(messages, stream=False, format_json=False)

        # Interrupt Polling: Check cancel_event every 0.2s during LLM generation
        if hasattr(raw_resp, "__await__"):
            llm_task = asyncio.create_task(raw_resp)
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
                        "errors": ["canceled"],
                    }
                await asyncio.sleep(0.2)
            resp = await llm_task
        else:
            resp = raw_resp

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

        if steps:
            # Persist plan to session store
            try:
                import json as _json
                orchestrator = config.get("configurable", {}).get("orchestrator") if config else None
                if orchestrator and hasattr(orchestrator, "session_store"):
                    orchestrator.session_store.add_plan(
                        session_id=getattr(orchestrator, "_current_task_id", "unknown"),
                        plan=_json.dumps(steps),
                        status="created",
                    )
            except Exception:
                pass  # never block execution
            return {"current_plan": steps, "current_step": 0}
    except Exception as e:
        logger.error(f"planning_node: plan generation failed: {e}")

    # Default: no plan
    return {"current_plan": current_plan, "current_step": current_step}


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
    plan_lines = []

    # Pattern for numbered items: 1., 2., 1), 2), etc.
    numbered_pattern = r"^\s*(\d+[\.\)]\s+)(.+)$"
    # Pattern for bullet items: -, *, •, etc.
    bullet_pattern = r"^\s*([\-\*•]\s+)(.+)$"

    for line in content.splitlines():
        line = line.strip()
        if not line:
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

        # Try numbered pattern
        match = re.match(numbered_pattern, line)
        if match:
            desc = match.group(2).strip()
            if desc:
                plan_lines.append(desc)
            continue

        # Try bullet pattern
        match = re.match(bullet_pattern, line)
        if match:
            desc = match.group(2).strip()
            if desc:
                plan_lines.append(desc)
            continue

        # If line looks like a step description (not too long, has action words)
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
        if len(line) < 200 and any(word in lower_line for word in action_words):
            plan_lines.append(line)

    if plan_lines:
        steps = [{"description": desc, "action": None} for desc in plan_lines]
        logger.info(f"planning_node: parsed regex plan with {len(steps)} steps")
        return steps

    # Strategy 4: Last resort - treat entire content as single step if it's reasonable
    if content and len(content.strip()) < 500:
        # Check if it looks like a valid response
        stripped = content.strip()
        if stripped and not stripped.startswith("```"):
            logger.info(f"planning_node: falling back to single-step plan")
            return [{"description": stripped[:200], "action": None}]

    return []
