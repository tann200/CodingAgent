import asyncio
import json
import logging
import re
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.context.context_builder import ContextBuilder
from src.core.inference.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    _notify_provider_limit,
)
from src.core.orchestration.agent_brain import get_agent_brain_manager

logger = logging.getLogger(__name__)


async def perception_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Perception Layer: Responsible for generating the next action or thought.
    Uses the 'operational' role from AgentBrainManager.
    Dynamic skill injection: If task involves debugging/searching, injects 'context_hygiene' skill.
    """
    # Get AgentBrainManager for role-specific prompts
    brain = get_agent_brain_manager()
    operational_role = brain.get_role("operational") or "You are a coding assistant."

    logger.info("=== perception_node START ===")

    # Resolve orchestrator first (needed for dynamic cancel_event lookup)
    orchestrator = _resolve_orchestrator(state, config)
    if orchestrator is None:
        logger.error("perception_node: orchestrator is None in config")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": ["orchestrator not found in config"],
        }

    # Check for cancellation - dynamically resolve from orchestrator if not in state
    cancel_event = state.get("cancel_event")
    if not cancel_event:
        cancel_event = getattr(orchestrator, "cancel_event", None)
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("perception_node: Task canceled by user")
        return {
            "history": state.get("history", []),
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "last_result": {"ok": False, "error": "Task canceled by user"},
            "errors": ["canceled"],
            "empty_response_count": 0,
        }

    # Validate call_model is available
    if not callable(call_model):
        logger.error(f"perception_node: call_model is not callable: {call_model}")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": ["call_model not available"],
        }

    try:
        adapter = orchestrator.adapter
        if adapter is None:
            logger.warning("perception_node: orchestrator.adapter is None")
    except Exception as e:
        logger.error(f"perception_node: failed to get adapter: {e}")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": [f"adapter error: {e}"],
        }

    # Task Decomposition: Check if this is a fresh task (round 0) that needs decomposition
    # F12: Decomposition in perception_node is redundant because planning_node already
    # handles multi-step decomposition and has better context.  Keep the block so existing
    # tests continue to work, but gate it to ensure it never runs (always False).
    # planning_node is the canonical home for decomposition going forward.
    task = state.get("task") or ""
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0

    # F12: Decomposition disabled in perception_node — planning_node owns this responsibility.
    if False and state.get("rounds", 0) == 0 and task and (not current_plan or current_step == 0):
        # Heuristic: check if task looks multi-step
        multi_step_indicators = [
            # Multiple imperative verbs or "and" connecting actions
            re.search(r"\band\b", task, re.IGNORECASE),
            # Numbered lists
            re.search(r"^\d+[\.\)]\s", task, re.MULTILINE),
            # Commas with multiple distinct actions
            re.search(
                r",.*?(?:then|and|after|before|also|add|create|delete|update|modify|fix)",
                task,
                re.IGNORECASE,
            ),
            # Common multi-action patterns
            re.search(
                r"(?:create|delete|update|modify|fix|add|remove).*(?:and|then|after).*(?:create|delete|update|modify|fix|add|remove)",
                task,
                re.IGNORECASE,
            ),
        ]

        needs_decomposition = any(multi_step_indicators)

        if needs_decomposition:
            logger.info("Task appears multi-step, attempting decomposition")
            try:
                builder = ContextBuilder(working_dir=state.get("working_dir"))
                decomp_prompt = f"""Analyze this task and break it down into small, independent steps.
For each step, provide a brief description (max 10 words).
Return ONLY a JSON array of step objects with format:
[{{"description": "step 1 description"}}, {{"description": "step 2 description"}}, ...]

Task: {task}

Respond with ONLY valid JSON, no markdown, no explanation. /no_think"""

                messages = [
                    {
                        "role": "system",
                        "content": "You are a task planning assistant. Break down complex tasks into simple steps.",
                    },
                    {"role": "user", "content": decomp_prompt},
                ]

                from src.core.inference.thinking_utils import (
                    budget_max_tokens,
                    get_active_model_id,
                    strip_thinking,
                )
                _model_id = get_active_model_id()
                # Use a generous budget: thinking models may emit a reasoning
                # block before the JSON even with /no_think in the prompt.
                # 600 tokens gives ~400 for thinking + ~200 for the JSON array.
                _decomp_max_tok = budget_max_tokens(600, _model_id)
                resp = await call_model(messages, stream=False, format_json=False, max_tokens=_decomp_max_tok)

                content = ""
                finish_reason = ""
                if isinstance(resp, dict):
                    if resp.get("choices"):
                        ch = resp["choices"][0]
                        finish_reason = ch.get("finish_reason", "")
                        msg = ch.get("message")
                        if isinstance(msg, dict):
                            content = msg.get("content") or ""
                # Part A: strip <think> blocks from reasoning models
                if content:
                    content = strip_thinking(content)

                # If the response was truncated (finish_reason="length"), the
                # JSON is likely incomplete — skip decomposition rather than
                # parse garbage.  planning_node will handle it instead.
                if finish_reason == "length":
                    logger.warning(
                        "perception_node: decomposition truncated (finish_reason=length); "
                        "skipping — planning_node will decompose"
                    )
                    content = ""

                # Parse the response
                plan_steps = []
                try:
                    # Try to extract JSON from response
                    json_match = re.search(r"\[.*\]", content, re.DOTALL)
                    if json_match:
                        steps_data = json.loads(json_match.group(0))
                        if isinstance(steps_data, list):
                            for step in steps_data:
                                if isinstance(step, dict) and step.get("description"):
                                    plan_steps.append(
                                        {
                                            "description": step["description"],
                                            "action": None,
                                            "pending": True,
                                        }
                                    )
                except Exception as e:
                    logger.warning(f"Failed to parse decomposition response: {e}")

                if plan_steps:
                    logger.info(f"Decomposed task into {len(plan_steps)} steps")
                    # Store the plan and set first step as current.
                    # Increment rounds (not reset to 0) so re-entry after plan_validator
                    # rejection does not re-trigger decomposition (guard checks rounds == 0).
                    return {
                        "history": [],
                        "next_action": None,
                        "rounds": (state.get("rounds") or 0) + 1,
                        "current_plan": plan_steps,
                        "current_step": 0,
                        "task": plan_steps[0]["description"],  # Focus on first step
                        "task_decomposed": True,
                        "original_task": task,  # Keep original for reference
                    }
            except Exception as e:
                logger.warning(f"Task decomposition failed: {e}")

    # Pre-retrieval: consult repo intelligence tools if available (search_code, find_symbol, find_references)
    # F9: Skip pre-retrieval on rounds > 0 — context was already gathered in round 0.
    # Repeated pre-retrieval adds tokens and latency without new information.
    retrieved_snippets = []
    try:
        if state.get("rounds", 0) == 0 and orchestrator and hasattr(orchestrator, "tool_registry"):
            # helper to call a tool if registered
            def _call_tool_if_exists(tool_name, **kwargs):
                try:
                    t = orchestrator.tool_registry.get(tool_name)
                    if t and callable(t.get("fn")):
                        return t["fn"](**kwargs)
                except Exception:
                    pass
                return None

            query = state.get("task") or ""
            # search_code
            try:
                sc = _call_tool_if_exists(
                    "search_code", query=query, workdir=state.get("working_dir")
                )
                if sc:
                    # support both dict{results:[]} and list
                    results = sc.get("results") if isinstance(sc, dict) else None
                    if results:
                        for r in results:
                            retrieved_snippets.append(
                                {
                                    "file_path": r.get("file_path") or r.get("file"),
                                    "snippet": r.get("snippet")
                                    or r.get("text")
                                    or r.get("content"),
                                    "reason": "search_code",
                                }
                            )
                    elif isinstance(sc, list):
                        for r in sc:
                            if isinstance(r, dict):
                                retrieved_snippets.append(
                                    {
                                        "file_path": r.get("file_path")
                                        or r.get("file"),
                                        "snippet": r.get("snippet")
                                        or r.get("text")
                                        or r.get("content"),
                                        "reason": "search_code",
                                    }
                                )
            except Exception:
                pass

            # find_symbol
            try:
                fs = _call_tool_if_exists(
                    "find_symbol", name=query, workdir=state.get("working_dir")
                )
                if fs and isinstance(fs, dict) and fs.get("file_path"):
                    retrieved_snippets.append(
                        {
                            "file_path": fs.get("file_path"),
                            "snippet": fs.get("snippet"),
                            "reason": "find_symbol",
                        }
                    )
            except Exception:
                pass

            # find_references
            try:
                fr = _call_tool_if_exists(
                    "find_references", name=query, workdir=state.get("working_dir")
                )
                if fr and isinstance(fr, list):
                    for r in fr:
                        if isinstance(r, dict):
                            retrieved_snippets.append(
                                {
                                    "file_path": r.get("file_path"),
                                    "snippet": r.get("excerpt") or r.get("context"),
                                    "reason": "find_references",
                                }
                            )
            except Exception:
                pass
    except Exception:
        retrieved_snippets = []

    # Setup prompt
    builder = ContextBuilder(working_dir=state.get("working_dir"))
    tools_list = [
        {"name": n, "description": m.get("description", "")}
        for n, m in orchestrator.tool_registry.tools.items()
    ]

    # Dynamic skill injection: if task involves debugging or deep searching
    active_skills = []
    task_lower = state.get("task", "").lower()
    if any(
        kw in task_lower
        for kw in ["debug", "fix", "error", "bug", "search", "find", "analyze"]
    ):
        context_hygiene_skill = brain.get_skill("context_hygiene")
        if context_hygiene_skill:
            active_skills.append(context_hygiene_skill)
            logger.info(
                "perception_node: injected context_hygiene skill for debugging/searching task"
            )

    # Assemble the tiered context
    messages = builder.build_prompt(
        identity=operational_role,
        role=f"Working Directory: {state['working_dir']}",
        active_skills=active_skills,
        task_description=state["task"],
        tools=tools_list,
        conversation=state["history"],
        retrieved_snippets=retrieved_snippets,
        max_tokens=6000,
    )

    # Determine model/provider
    provider = "None"
    model = "None"
    if adapter:
        logger.info(f"perception_node: adapter type: {type(adapter)}")
        if hasattr(adapter, "provider") and isinstance(adapter.provider, dict):
            provider = (
                adapter.provider.get("name") or adapter.provider.get("type") or "None"
            )
            logger.info(f"perception_node: provider from adapter: {provider}")
        if hasattr(adapter, "models") and adapter.models:
            model = adapter.models[0]
            logger.info(f"perception_node: model from adapter: {model}")
    else:
        logger.warning("perception_node: adapter is None!")

    # Determine deterministic overrides if orchestrator requests them
    llm_kwargs = {}
    try:
        if orchestrator and getattr(orchestrator, "deterministic", False):
            llm_kwargs["temperature"] = 0.0
            seed = getattr(orchestrator, "seed", None)
            if seed is not None:
                llm_kwargs["seed"] = seed
        else:
            llm_kwargs["temperature"] = 0.4
    except Exception:
        pass

    # LLM Inference
    logger.info(
        f"perception_node: calling call_model with provider={provider}, model={model}"
    )

    # Dynamically resolve cancel_event from orchestrator if not in state
    cancel_event = state.get("cancel_event")
    if not cancel_event and orchestrator:
        cancel_event = getattr(orchestrator, "cancel_event", None)

    try:
        # F14: call_model is always async; use create_task directly.
        llm_task = asyncio.create_task(
            call_model(
                messages,
                provider=provider,
                model=model,
                stream=False,
                format_json=False,
                tools=None,
                **llm_kwargs,
            )
        )
        # Interrupt Polling: Check cancel_event every 0.2s during LLM generation
        while not llm_task.done():
            if (
                cancel_event
                and hasattr(cancel_event, "is_set")
                and cancel_event.is_set()
            ):
                llm_task.cancel()
                logger.info("perception_node: Task canceled mid-generation")
                return {
                    "history": state.get("history", []),
                    "next_action": None,
                    "rounds": state.get("rounds", 0) + 1,
                    "errors": ["canceled"],
                }
            await asyncio.sleep(0.2)
        resp = await llm_task
    except asyncio.CancelledError:
        logger.info("perception_node: Task cancelled")
        return {
            "history": state.get("history", []),
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "errors": ["canceled"],
        }
    except Exception as e:
        logger.error(f"call_model failed: {e}")
        resp = {"ok": False, "error": str(e)}
        _notify_provider_limit(str(e))

    # Debug: log raw response for troubleshooting
    try:
        logger.info(
            f"perception_node: raw LLM resp content: {repr(resp.get('choices', [{}])[0].get('message', {}).get('content', '')[:100])}"
        )
    except Exception:
        pass

    # Debug: log raw response for troubleshooting
    try:
        logger.info(f"perception_node: raw LLM resp: {repr(resp)[:1000]}")
    except Exception:
        pass

    # Extract response
    ch = None
    if isinstance(resp, dict):
        if resp.get("choices"):
            ch = resp["choices"][0].get("message")
        elif resp.get("message"):
            ch = resp.get("message")

    content = ""
    if isinstance(ch, str):
        content = ch
    elif isinstance(ch, dict):
        content = ch.get("content") or ""

    # UI Sync: Forward raw content immediately to TUI so user can see thinking
    if content and orchestrator and hasattr(orchestrator, "msg_mgr"):
        try:
            orchestrator.msg_mgr.append("assistant", content)
        except Exception as e:
            logger.debug(f"UI sync failed: {e}")

    try:
        logger.info(f"perception_node: extracted content: {repr(content)[:1000]}")
    except Exception:
        pass

    # Infinite Loop Prevention: Detect empty/stripped responses
    empty_response_count = state.get("empty_response_count", 0)
    content_stripped = content.strip() if content else ""

    # Check if content is empty or just contains thinking blocks
    is_empty_response = (
        not content_stripped
        or content_stripped.replace("<think>", "").replace("</think>", "").strip() == ""
    )

    if is_empty_response:
        empty_response_count += 1
        # Use info instead of warning to avoid log spam
        logger.info(
            f"perception_node: Empty/stripped response (count: {empty_response_count})"
        )

        # After 3 consecutive empty responses, force stop the loop
        if empty_response_count >= 3:
            logger.error(
                "perception_node: 3 consecutive empty responses - breaking loop"
            )
            return {
                "history": state.get("history", []),
                "next_action": None,
                "rounds": state.get("rounds", 0) + 1,
                "last_result": {
                    "ok": False,
                    "error": "Infinite loop detected: model produced 3 consecutive empty responses",
                },
                "errors": ["infinite_loop_empty_response"],
                "empty_response_count": 0,
            }

        # Inject corrective prompt for empty responses
        corrective_prompt = (
            "\n\n<system_reminder>\n"
            "You must output a valid YAML tool call. Do not output empty responses or thinking blocks only.\n"
            "If you cannot determine the next action, use the 'respond' tool to explain what you need.\n"
            "</system_reminder>\n"
        )
        # FIX: Return only NEW messages for LangGraph to append, not the full history
        new_messages = [
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": corrective_prompt
                + "\n\nPlease provide a valid YAML tool call for your next action.",
            },
        ]

        return {
            "history": new_messages,
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "empty_response_count": empty_response_count,
        }

    # Reset empty response counter on successful content
    empty_response_count = 0

    # Parse tools
    # If the assistant content already contains a tool_execution_result (i.e. a previous
    # tool run result), we should not parse it as a new tool block. Parsing and
    # executing could cause immediate repetition of the same tool call.
    tool_call = None
    try:
        # Check if we should skip parsing based on prior history.
        # We should skip ONLY if:
        # 1. The current content itself contains tool_execution_result (we're re-parsing the same thing)
        # 2. NOT because the last message was a tool result (we need to parse new responses!)
        prior_history = state.get("history") or []
        try:
            if isinstance(prior_history, list) and prior_history:
                last_msg = prior_history[-1]
                if last_msg.get("role") == "tool":
                    logger.info("perception_node: last message was a tool result")
        except Exception:
            pass

        # Parse if: content exists AND content doesn't contain tool_execution_result
        # We should ALWAYS try to parse, even after tool results - we need new tool calls!
        if (
            content
            and "tool_execution_result" not in content
            and '"tool_execution_result"' not in content
        ):
            tool_call = parse_tool_block(content)
        else:
            logger.info(
                "perception_node: skipping parse_tool_block because content contains tool_execution_result"
            )
            tool_call = None
    except Exception:
        tool_call = None
    try:
        logger.info(f"perception_node: parsed tool_call: {repr(tool_call)}")
    except Exception:
        pass

    # ULTIMATE FALLBACK: If tool_call is None and content was supposed to have YAML,
    # treat as empty to trigger loop breaker
    content_stripped = content.strip() if content else ""
    thinking_only = (
        content_stripped.replace("<think>", "").replace("</think>", "").strip() == ""
    )

    if tool_call is None and (not content_stripped or thinking_only):
        # No valid tool found and content is empty/thinking-only
        empty_response_count = state.get("empty_response_count", 0) + 1
        # Use info instead of warning to avoid log spam
        logger.info(
            f"perception_node: No tool call extracted (count: {empty_response_count})"
        )

        if empty_response_count >= 3:
            logger.error(
                "perception_node: 3 consecutive failed tool extractions - breaking loop"
            )
            return {
                "history": [{"role": "assistant", "content": content or ""}],
                "next_action": None,
                "rounds": state.get("rounds", 0) + 1,
                "last_result": {
                    "ok": False,
                    "error": "Infinite loop detected: model failed to generate valid tool calls 3 times",
                },
                "errors": ["infinite_loop_no_tool"],
                "empty_response_count": 0,
            }

        # Inject corrective prompt
        corrective_prompt = (
            "\n\n<system_reminder>\n"
            "CRITICAL: You MUST output a valid YAML tool call block. "
            "Format:\n```yaml\nname: tool_name\narguments:\n  key: value\n```\n"
            "Do NOT output thinking blocks only. Do NOT output empty content.\n"
            "</system_reminder>\n"
        )
        new_messages = [
            {"role": "assistant", "content": content or ""},
            {
                "role": "user",
                "content": corrective_prompt
                + "\n\nProvide a valid YAML tool call now.",
            },
        ]
        return {
            "history": new_messages,
            "next_action": None,
            "rounds": state.get("rounds", 0) + 1,
            "empty_response_count": empty_response_count,
        }

    # Preserve plan state if already exists
    current_plan = state.get("current_plan")
    current_step = state.get("current_step")
    task_decomposed = state.get("task_decomposed")
    original_task = state.get("original_task")

    # FIX: Return ONLY the new message, not the full history.
    # LangGraph's operator.add reducer will handle appending to the existing history.
    # This prevents the exponential duplication bug (2→4→8→16→32→64 messages).
    # Also: don't add empty content to history - it confuses the model!
    if content and content.strip():
        new_messages = [{"role": "assistant", "content": content}]
    else:
        # Skip adding empty content to history - it causes confusion
        new_messages = []

    result = {
        "history": new_messages,
        "next_action": tool_call,
        "rounds": state["rounds"] + 1,
        "empty_response_count": empty_response_count,
    }

    # Preserve plan-related fields
    if current_plan is not None:
        result["current_plan"] = current_plan
    if current_step is not None:
        result["current_step"] = current_step
    if task_decomposed is not None:
        result["task_decomposed"] = task_decomposed
    if original_task is not None:
        result["original_task"] = original_task

    return result
