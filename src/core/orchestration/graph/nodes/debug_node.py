import logging
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import _resolve_orchestrator
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


def _classify_error(error_summary: str) -> str:
    """Classify error into a known category for more targeted prompts."""
    s = error_summary.lower()
    if "syntaxerror" in s or "indentationerror" in s or "invalid syntax" in s:
        return "syntax_error"
    if "importerror" in s or "modulenotfounderror" in s or "no module named" in s:
        return "import_error"
    if "assertionerror" in s or ("failed" in s and "test" in s):
        return "test_failure"
    if "e501" in s or "e302" in s or "flake8" in s or "pylint" in s or "ruff" in s:
        return "lint_error"
    if "typeerror" in s or "attributeerror" in s or "nameerror" in s:
        return "runtime_error"
    return "unknown_error"


TYPE_GUIDANCE = {
    "syntax_error": "Fix the syntax error. Check indentation and missing colons/parentheses.",
    "import_error": "Fix the import. Check module name spelling and that the module is installed.",
    "test_failure": "A test assertion failed. Read the failing test, understand what it expects, then fix the implementation.",
    "lint_error": "Fix the lint issue. Common issues: line too long (split it), missing blank lines, unused imports.",
    "runtime_error": "Fix the runtime error. Check attribute names, type mismatches, and None checks.",
    "unknown_error": "Analyze the error carefully and generate a targeted fix.",
}


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

        # Step C: Attempt automated rollback when debug retries are exhausted
        try:
            rollback_mgr = getattr(orchestrator, "rollback_manager", None)
            if rollback_mgr and rollback_mgr.current_snapshot:
                result = rollback_mgr.rollback()
                logger.info(f"Auto-rollback result: {result}")
                rollback_mgr.cleanup_old_snapshots(keep_last=5)
        except Exception as rb_err:
            logger.warning(f"Rollback failed: {rb_err}")

        return {
            "next_action": None,
            "errors": [
                f"Max debug attempts ({max_attempts}) reached — rollback attempted"
            ],
        }

    next_attempt = current_attempt + 1

    # Classify the error for more targeted fixes
    error_type = _classify_error(error_summary)

    # Persist error to session store
    try:
        orchestrator = config.get("configurable", {}).get("orchestrator") if config else None
        if orchestrator and hasattr(orchestrator, "session_store"):
            orchestrator.session_store.add_error(
                session_id=getattr(orchestrator, "_current_task_id", "unknown"),
                error_type=error_type,
                error_message=error_summary[:500],
                context={"attempt": current_attempt + 1},
            )
    except Exception:
        pass  # never block execution

    fix_prompt = f"""You are a debugging assistant. Attempt {next_attempt}/{max_attempts}.

Task: {task}
Error type: {error_type}
Error details: {error_summary}

Guidance: {TYPE_GUIDANCE[error_type]}

Generate a YAML tool call to fix the issue. Use edit_file, write_file, or bash as appropriate."""

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
