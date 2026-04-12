"""frontier_loop_node.py — Tight LLM+tool loop for LARGE/FRONTIER models (TASK-5).

Implements a claw-code–style ``run_turn()`` inner loop that keeps LLM and tool
calls within a single node, eliminating N×(perception → analysis → planning →
execution) round-trips through the LangGraph state machine for capable models.

How it works
------------
1. Build a context prompt (task + history + relevant files).
2. Call the LLM.
3. Parse any tool calls from the response.
4. Execute each tool call (via orchestrator.execute_tool).
5. Append assistant message + tool results to conversation history.
6. Loop from (2) until one of the exit conditions fires:
   - LLM returns no tool calls ("complete" / natural language reply).
   - Tool call budget (max_tool_calls) is exhausted.
   - Plan-mode write gate is pending approval.
   - Context overflow detected.
   - Maximum internal turns reached (_MAX_FRONTIER_TURNS).

On exit the node sets:
   - ``last_result``           — outcome of last tool call (or None on natural reply).
   - ``tool_call_count``       — cumulative tool calls across all turns.
   - ``_frontier_loop_turns``  — internal turn count (for observability / tests).
   - ``awaiting_plan_approval``— True when plan-mode blocked a write tool.
   - ``errors``                — includes "context_overflow" when detected.

The node is NOT wired into the default ``compile_agent_graph()`` graph.
It is used by the tier-aware graph built by TASK-6 (``build_tier_graph()``).
External code that wants the frontier graph can call::

    from src.core.orchestration.graph.builder import build_tier_graph
    graph = build_tier_graph("frontier")

Registration note
-----------------
frontier_loop_node is imported by builder.py (TASK-6) only when the active tier
is LARGE or FRONTIER; other tiers continue to use the standard 16-node graph.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Mapping, cast

from src.core.orchestration.graph.state import AgentState
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    _notify_provider_limit,
)

logger = logging.getLogger(__name__)

# Maximum turns within one invocation of frontier_loop_node.
# Each "turn" is one LLM call + its tool calls.
# This bounds the node's wall-clock time and prevents runaway infinite loops.
# Individual tool-call budget is still governed by state["max_tool_calls"].
_MAX_FRONTIER_TURNS = 20

# OP-9: Cap per-tool output that enters conversation history.
_TOOL_OUTPUT_MAX_BYTES = 50_000
_TOOL_LARGE_TEXT_FIELDS = ("output", "content", "diff", "text", "stdout", "stderr")


# Expose a module-level call_model proxy so tests can patch
async def call_model(*args, **kwargs):
    """Proxy to the real call_model in src.core.inference.llm_manager.

    Keeping this as a tiny proxy avoids import-time cycles and makes the
    symbol patchable from tests (they patch this module's attribute).
    """
    from src.core.inference.llm_manager import call_model as _call

    return await _call(*args, **kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate_tool_output(res: dict) -> dict:
    """Cap any large text fields in a tool result before it enters history."""
    try:
        serialized = json.dumps(res, default=str)
    except Exception:
        return res
    if len(serialized.encode("utf-8", errors="replace")) <= _TOOL_OUTPUT_MAX_BYTES:
        return res
    truncated = dict(res)
    for field in _TOOL_LARGE_TEXT_FIELDS:
        val = truncated.get(field)
        if not isinstance(val, str) or len(val) < 500:
            continue
        try:
            current_size = len(
                json.dumps(truncated, default=str).encode("utf-8", errors="replace")
            )
        except Exception:
            break
        if current_size <= _TOOL_OUTPUT_MAX_BYTES:
            break
        excess = current_size - _TOOL_OUTPUT_MAX_BYTES
        new_len = max(200, len(val) - excess - 80)
        omitted = len(val) - new_len
        truncated[field] = (
            val[:new_len]
            + f"\n…[frontier_loop: {omitted} chars truncated — output exceeded 50 KB limit]"
        )
        truncated["_output_truncated"] = True
    return truncated


def _extract_content_text(response: Any) -> str:
    """Extract text content from an LLM response object."""
    try:
        content = response.content if hasattr(response, "content") else response
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("text", ""))
            return "\n".join(p for p in parts if p)
    except Exception:
        pass
    return ""


def _is_context_overflow(error_msg: str) -> bool:
    """Return True if the error message indicates a context-window overflow."""
    patterns = [
        "context length",
        "context window",
        "exceeds the available context",
        "max_tokens",
        "token limit",
        "input too long",
        "prompt is too long",
    ]
    low = error_msg.lower()
    return any(p in low for p in patterns)


def _plan_mode_blocks(orchestrator: Any, tool_name: str) -> bool:
    """Return True when plan-mode is active and the tool is blocked pending approval."""
    try:
        pm = getattr(orchestrator, "plan_mode", None)
        if pm and getattr(pm, "enabled", False):
            approved = getattr(orchestrator, "_plan_mode_approved", None)
            if approved is not True:
                from src.core.orchestration.plan_mode import PlanMode

                return tool_name in PlanMode.BLOCKED_TOOLS
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


async def frontier_loop_node(
    state: AgentState,
    config: Any,
) -> Dict[str, Any]:
    """Tight LLM+tool loop for LARGE/FRONTIER models.

    Keeps looping internally (call LLM → execute tools → loop) rather than
    exiting after every tool call and re-entering via the full LangGraph routing
    pipeline.  This dramatically reduces state-machine overhead for capable models
    that produce high-quality tool plans on the first try.

    Returns a partial AgentState update dict.
    """
    orchestrator = _resolve_orchestrator(state, config)
    task: str = state.get("task") or ""
    history: list = list(state.get("conversation_history") or [])
    tool_call_count: int = int(state.get("tool_call_count") or 0)
    max_tool_calls: int = int(state.get("max_tool_calls") or 30)
    model_tier: str = (state.get("model_tier") or "frontier").lower()
    errors: list = list(state.get("errors") or [])

    last_result: Dict[str, Any] | None = None
    turns_taken: int = 0

    logger.info(
        "frontier_loop_node: starting (task=%r, tool_calls=%d/%d, tier=%s)",
        task[:80],
        tool_call_count,
        max_tool_calls,
        model_tier,
    )

    # Publish "working" notification for the TUI.
    # Ensure `bus` is always defined so static analysis cannot flag it as
    # possibly-unbound later in the function.
    bus: Any = getattr(orchestrator, "event_bus", None) if orchestrator else None
    try:
        if bus:
            bus.publish(
                "agent.status",
                {"status": "working", "node": "frontier_loop", "task": task[:100]},
            )
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    while turns_taken < _MAX_FRONTIER_TURNS:
        turns_taken += 1

        # ---- Budget check ------------------------------------------------
        if tool_call_count >= max_tool_calls:
            logger.warning(
                "frontier_loop_node: tool budget exhausted (%d/%d), exiting loop",
                tool_call_count,
                max_tool_calls,
            )
            break

        # ---- Build prompt ------------------------------------------------
        try:
            from src.core.context.context_builder import ContextBuilder
            from src.core.inference.provider_context import get_context_budget
            from src.core.orchestration.event_bus import run_with_correlation
            import functools

            # Construct ContextBuilder with the working_dir (tests and callers
            # pass orchestrator.working_dir).  Passing the orchestrator itself
            # is incorrect for the current constructor signature.
            cb = ContextBuilder(working_dir=getattr(orchestrator, "working_dir", None))

            # Use the per-tier context budget helper correctly.
            token_budget = get_context_budget(model_tier=model_tier)

            # Determine tools schema early so the static system prefix can include it.
            tools_schema: list[Dict[str, Any]] = []
            if orchestrator:
                try:
                    reg = getattr(orchestrator, "tool_registry", None)
                    if reg and hasattr(reg, "get_openai_functions"):
                        tools_schema = reg.get_openai_functions()
                except Exception:
                    pass

            # Provider capabilities (optional) — may help ContextBuilder pick provider-specific
            # prompt partials.
            provider_caps = None
            try:
                if orchestrator and hasattr(orchestrator, "get_provider_capabilities"):
                    provider_caps = orchestrator.get_provider_capabilities()
            except Exception:
                provider_caps = None

            # build_prompt is CPU / I/O bound; run it in an executor while propagating
            # ContextVars (correlation id) using run_with_correlation.
            loop = asyncio.get_running_loop()
            partial_fn = functools.partial(
                cb.build_prompt,
                "operational",  # role_name
                [],  # active_skills
                task,  # task_description
                tools_schema or [],
                history,  # conversation
                token_budget,  # max_tokens
                None,  # retrieved_snippets
                provider_caps,  # provider_capabilities
                None,  # context_controller
                model_tier,  # model_tier
            )
            built_messages = await run_with_correlation(loop, None, partial_fn)
            # build_prompt returns a list of messages; fall back to a minimal system
            # prompt when it fails to produce messages.
            messages = list(built_messages) if isinstance(built_messages, list) else []
            if not messages:
                messages = [
                    {
                        "role": "system",
                        "content": f"You are a capable coding assistant. Task: {task}",
                    }
                ]
        except Exception as exc:
            logger.warning("frontier_loop_node: context build failed: %s", exc)
            messages = [
                {
                    "role": "system",
                    "content": f"You are a capable coding assistant. Task: {task}",
                }
            ]

        # ---- LLM call ----------------------------------------------------
        try:
            # Use the module-level call_model proxy so tests can patch this module's
            # symbol (tests patch "src.core.orchestration.graph.nodes.frontier_loop_node.call_model").
            llm_client = (
                getattr(orchestrator, "llm_client", None) if orchestrator else None
            )
            tools_schema: list[Dict[str, Any]] = []
            if orchestrator:
                try:
                    reg = getattr(orchestrator, "tool_registry", None)
                    if reg and hasattr(reg, "get_openai_functions"):
                        tools_schema = reg.get_openai_functions()
                except Exception:
                    pass

            response = await call_model(
                client=llm_client,
                messages=messages,
                tools=tools_schema or None,
                state=state,
                node="frontier_loop",
            )
        except Exception as exc:
            error_str = str(exc)
            logger.warning("frontier_loop_node: LLM call failed: %s", error_str)
            _notify_provider_limit(error_str)
            if _is_context_overflow(error_str):
                errors = list(errors) + ["context_overflow"]
            break

        # Detect context overflow in response content
        content_text = _extract_content_text(response)
        if _is_context_overflow(content_text):
            logger.warning("frontier_loop_node: context overflow in LLM response")
            errors = list(errors) + ["context_overflow"]
            break

        # ---- Parse tool calls --------------------------------------------
        try:
            from src.core.orchestration.tool_parser import parse_tool_block

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls and content_text:
                # Fallback: try parsing JSON/YAML tool blocks from text
                parsed = parse_tool_block(content_text)
                if parsed and isinstance(parsed, dict) and parsed.get("tool"):
                    tool_calls = [parsed]
        except Exception as exc:
            logger.debug("frontier_loop_node: tool parse error: %s", exc)
            tool_calls = []

        # ---- Append assistant message to history -------------------------
        history = list(history)
        history.append({"role": "assistant", "content": content_text or ""})

        # ---- No tool calls → task done (natural language reply) ----------
        if not tool_calls:
            logger.info(
                "frontier_loop_node: no tool calls in turn %d — task complete",
                turns_taken,
            )
            break

        # ---- Execute tool calls ------------------------------------------
        for tc in tool_calls:
            # Normalise tool call (OpenAI function-calling format vs. plain dict)
            if isinstance(tc, dict):
                tool_name: str = (
                    tc.get("name")
                    or tc.get("tool")
                    or (tc.get("function", {}) or {}).get("name", "")
                )
                raw_args = (
                    tc.get("args")
                    or tc.get("arguments")
                    or tc.get("input")
                    or (tc.get("function", {}) or {}).get("arguments", {})
                    or {}
                )
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except Exception:
                        raw_args = {}
                tool_args: dict = raw_args if isinstance(raw_args, dict) else {}
            else:
                # OpenAI ToolCall object
                try:
                    tool_name = tc.function.name
                    raw_args = tc.function.arguments
                    tool_args = (
                        json.loads(raw_args)
                        if isinstance(raw_args, str)
                        else (raw_args or {})
                    )
                except Exception:
                    continue

            if not tool_name:
                continue

            logger.info(
                "frontier_loop_node: executing tool %r (turn %d)",
                tool_name,
                turns_taken,
            )

            # --- Plan-mode gate -------------------------------------------
            if orchestrator and _plan_mode_blocks(orchestrator, tool_name):
                logger.info(
                    "frontier_loop_node: plan-mode blocked '%s' — suspending for approval",
                    tool_name,
                )
                # Persist state and signal wait_for_user via state flag
                return {
                    "conversation_history": history,
                    "tool_call_count": tool_call_count,
                    "last_result": last_result,
                    "errors": errors,
                    "_frontier_loop_turns": turns_taken,
                    "awaiting_plan_approval": True,
                    "next_action": {"tool": tool_name, "args": tool_args},
                }

            # --- Execute --------------------------------------------------
            action = {"tool": tool_name, "args": tool_args}
            try:
                if orchestrator:
                    # Use asyncio.to_thread for long-running tools (MEDIUM+ tier)
                    if model_tier in ("large", "frontier", "medium"):
                        from src.core.orchestration.event_bus import (
                            run_with_correlation,
                        )

                        loop = asyncio.get_running_loop()
                        tool_result = await run_with_correlation(
                            loop, None, orchestrator.execute_tool, action
                        )
                    else:
                        tool_result = orchestrator.execute_tool(action)
                else:
                    tool_result = {"ok": False, "error": "orchestrator unavailable"}
            except Exception as exc:
                logger.warning(
                    "frontier_loop_node: tool '%s' raised: %s", tool_name, exc
                )
                tool_result = {"ok": False, "error": str(exc)}

            # Ensure tool_result is a dict before passing to _truncate_tool_output
            if not isinstance(tool_result, dict):
                # Preserve some diagnostic information when possible
                tool_result = (
                    {}
                    if tool_result is None
                    else {"ok": False, "error": str(tool_result)}
                )
            # Help static type checkers: assert the type for _truncate_tool_output
            tool_result = cast(Dict[str, Any], tool_result)
            tool_result = _truncate_tool_output(tool_result)
            tool_call_count += 1
            last_result = tool_result

            # Publish tool result event for TUI
            try:
                if bus:
                    bus.publish(
                        "tool.result",
                        {
                            "tool": tool_name,
                            "result": tool_result,
                            "turn": turns_taken,
                        },
                    )
            except Exception:
                pass

            # Append tool result to history so the LLM sees it next turn
            result_str = json.dumps(tool_result, default=str)
            history.append(
                {
                    "role": "tool",
                    "name": tool_name,
                    "content": result_str,
                }
            )

            # Re-check budget after each tool call
            if tool_call_count >= max_tool_calls:
                logger.warning(
                    "frontier_loop_node: tool budget exhausted after '%s' (%d/%d)",
                    tool_name,
                    tool_call_count,
                    max_tool_calls,
                )
                break

        # Check budget again after the full tool-call batch
        if tool_call_count >= max_tool_calls:
            break

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------
    logger.info(
        "frontier_loop_node: exited after %d turns, %d tool calls",
        turns_taken,
        tool_call_count,
    )

    try:
        if bus:
            bus.publish(
                "agent.status",
                {
                    "status": "idle",
                    "node": "frontier_loop",
                    "turns": turns_taken,
                    "tool_calls": tool_call_count,
                },
            )
    except Exception:
        pass

    return {
        "conversation_history": history,
        "tool_call_count": tool_call_count,
        "last_result": last_result,
        "errors": errors,
        "_frontier_loop_turns": turns_taken,
        # Clear plan-mode flag (we only set it above on mid-loop pause)
        "awaiting_plan_approval": False,
    }
