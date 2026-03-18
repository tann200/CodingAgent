import logging
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


async def debug_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Debug Node: Analyzes verification failures and attempts to fix issues.
    Uses the 'debugger' role from AgentBrainManager.
    """
    # Get AgentBrainManager for role-specific prompts
    brain = get_agent_brain_manager()
    debugger_role = brain.get_role("debugger") or "You are a debugging assistant."

    logger.info("=== debug_node START ===")

    try:
        orchestrator = config.get("configurable", {}).get("orchestrator")
        if orchestrator is None:
            logger.error("debug_node: orchestrator is None")
            return {"next_action": None, "errors": ["orchestrator not found"]}
    except Exception as e:
        logger.error(f"debug_node: failed to get orchestrator: {e}")
        return {"next_action": None, "errors": [str(e)]}

    current_attempt: int = int(state.get("debug_attempts") or 0)
    max_attempts: int = int(state.get("max_debug_attempts") or 3)
    last_result = state.get("last_result") or {}
    verification_result = state.get("verification_result") or {}
    task = str(state.get("task") or "")

    logger.info(f"debug_node: attempt {current_attempt + 1}/{max_attempts}")

    error_summary = ""
    if last_result.get("error"):
        error_summary = f"Tool error: {last_result.get('error')}"
    elif verification_result:
        v = verification_result.get("tests", {})
        if v.get("status") == "fail":
            error_summary = f"Test failure: {v.get('stdout', '')[:500]}"
        v = verification_result.get("linter", {})
        if v.get("status") == "fail":
            error_summary += f" Linter: {v.get('stdout', '')[:200]}"

    if current_attempt >= max_attempts:
        logger.warning("debug_node: max attempts reached, giving up")
        return {
            "next_action": None,
            "errors": [f"Max debug attempts ({max_attempts}) reached"],
        }

    next_attempt = current_attempt + 1

    fix_prompt = f"""You are a debugging assistant. The previous attempt failed.

Task: {task}

Error: {error_summary}

Analyze the error and generate a FIX tool call to correct the issue.
If the issue is a test failure, fix the code.
If the issue is a linter error, fix the formatting.
If the issue is a syntax error, correct the code.

Generate a tool call to fix the issue. Use the appropriate tool (edit_file, write_file, etc).
Respond with ONLY a tool call in YAML format."""

    try:
        builder = ContextBuilder()
        tools_list = [
            {"name": n, "description": m.get("description", "")}
            for n, m in orchestrator.tool_registry.tools.items()
        ]

        messages = builder.build_prompt(
            identity=debugger_role,
            role="Debugging",
            active_skills=[],
            task_description=fix_prompt,
            tools=tools_list,
            conversation=state.get("history", []),
            max_tokens=4000,
        )

        provider = "None"
        model = "None"
        if orchestrator.adapter:
            if hasattr(orchestrator.adapter, "provider") and isinstance(
                orchestrator.adapter.provider, dict
            ):
                provider = orchestrator.adapter.provider.get("name") or "None"
            if hasattr(orchestrator.adapter, "models") and orchestrator.adapter.models:
                model = orchestrator.adapter.models[0]

        resp = call_model(
            messages, provider=provider, model=model, stream=False, format_json=False
        )

        content = ""
        if isinstance(resp, dict):
            if resp.get("choices"):
                ch = resp["choices"][0].get("message")
                if isinstance(ch, dict):
                    content = ch.get("content") or ""

        tool_call = parse_tool_block(content)
        if tool_call:
            logger.info(f"debug_node: generated fix tool: {tool_call}")
            return {
                "next_action": tool_call,
                "debug_attempts": next_attempt,
            }
        else:
            logger.warning("debug_node: no tool generated for fix")
            return {
                "next_action": None,
                "debug_attempts": next_attempt,
                "errors": ["Debug node could not generate fix"],
            }

    except Exception as e:
        logger.error(f"debug_node: failed to generate fix: {e}")
        return {
            "next_action": None,
            "debug_attempts": next_attempt,
            "errors": [str(e)],
        }
