import json
import logging
from pathlib import Path
from typing import Dict, Any

from src.core.orchestration.graph.state import AgentState
from src.core.context.context_builder import ContextBuilder
from src.core.llm_manager import call_model
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.memory.distiller import distill_context

logger = logging.getLogger(__name__)


async def analysis_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Analysis Layer: Explores the repository to gather relevant context before planning.
    Uses repository intelligence tools to find relevant files, symbols, and dependencies.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info("=== analysis_node START ===")

    try:
        orchestrator = config.get("configurable", {}).get("orchestrator")
        if orchestrator is None:
            logger.error("analysis_node: orchestrator is None in config")
            return {
                "analysis_summary": "Orchestrator not found",
                "relevant_files": [],
                "key_symbols": [],
            }
    except Exception as e:
        logger.error(f"analysis_node: failed to get orchestrator: {e}")
        return {
            "analysis_summary": f"Error: {e}",
            "relevant_files": [],
            "key_symbols": [],
        }

    task = state.get("task") or ""
    working_dir = state.get("working_dir", ".")

    relevant_files = []
    key_symbols = []
    analysis_summary = ""

    def _call_tool_if_exists(tool_name, **kwargs):
        try:
            t = orchestrator.tool_registry.get(tool_name)
            if t and callable(t.get("fn")):
                return t["fn"](**kwargs)
        except Exception as e:
            logger.warning(f"analysis_node: tool {tool_name} failed: {e}")
        return None

    try:
        results = []

        sc = _call_tool_if_exists("search_code", query=task, workdir=working_dir)
        if sc:
            results_data = sc.get("results") if isinstance(sc, dict) else sc
            if isinstance(results_data, list):
                for r in results_data[:5]:
                    fp = r.get("file_path") or r.get("file")
                    if fp and fp not in relevant_files:
                        relevant_files.append(fp)

        fs = _call_tool_if_exists(
            "find_symbol", name=task.split()[0] if task else "", workdir=working_dir
        )
        if fs and isinstance(fs, dict):
            fp = fs.get("file_path")
            if fp and fp not in relevant_files:
                relevant_files.append(fp)
            sym = fs.get("symbol_name")
            if sym and sym not in key_symbols:
                key_symbols.append(sym)

        gl = _call_tool_if_exists("glob", pattern="**/*.py", workdir=working_dir)
        if gl and isinstance(gl, dict):
            items = gl.get("items", [])
            for item in items[:20]:
                fp = item.get("name") if isinstance(item, dict) else item
                if fp and fp.endswith(".py") and fp not in relevant_files:
                    relevant_files.append(fp)

        if relevant_files:
            analysis_summary = (
                f"Found {len(relevant_files)} relevant files for task: {task[:50]}..."
            )
        else:
            analysis_summary = f"No specific files found. Task: {task[:50]}..."

        logger.info(
            f"analysis_node: found {len(relevant_files)} files, {len(key_symbols)} symbols"
        )

    except Exception as e:
        logger.error(f"analysis_node: analysis failed: {e}")
        analysis_summary = f"Analysis failed: {e}"

    return {
        "analysis_summary": analysis_summary,
        "relevant_files": relevant_files,
        "key_symbols": key_symbols,
    }


def _notify_provider_limit(error_msg: str) -> None:
    """Send UI notification when provider/context limit is reached."""
    error_lower = error_msg.lower()
    if any(
        x in error_lower
        for x in [
            "disconnected",
            "connection",
            "timeout",
            "memory",
            "slot",
            "batch",
            "kv cache",
            "context",
            "attention",
            "memory slot",
            "ubatch",
            "total tokens",
        ]
    ):
        try:
            from src.core.orchestration.event_bus import get_event_bus

            bus = get_event_bus()
            bus.publish(
                "ui.notification",
                {
                    "level": "warning",
                    "message": error_msg,
                    "source": "provider",
                },
            )
        except Exception:
            pass


async def perception_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Perception Layer: Responsible for generating the next action or thought.
    Enforces 'Thinking' and 'XML Tool Usage' before moving to execution.
    Also handles task decomposition for multi-step tasks.
    """
    import logging
    import re

    logger = logging.getLogger(__name__)
    logger.info("=== perception_node START ===")

    # Check for cancellation
    cancel_event = state.get("cancel_event")
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("perception_node: Task canceled by user")
        return {
            "history": state.get("history", []),
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "last_result": {"ok": False, "error": "Task canceled by user"},
            "errors": ["canceled"],
        }

    # Validate config and orchestrator
    try:
        orchestrator = config.get("configurable", {}).get("orchestrator")
        if orchestrator is None:
            logger.error("perception_node: orchestrator is None in config")
            return {
                "history": [],
                "next_action": None,
                "rounds": (state.get("rounds") or 0) + 1,
                "errors": ["orchestrator not found in config"],
            }
    except Exception as e:
        logger.error(f"perception_node: failed to get orchestrator: {e}")
        return {
            "history": [],
            "next_action": None,
            "rounds": (state.get("rounds") or 0) + 1,
            "errors": [f"config error: {e}"],
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
    task = state.get("task") or ""
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0

    # Only decompose on fresh task (first round)
    if state.get("rounds", 0) == 0 and task and (not current_plan or current_step == 0):
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
                builder = ContextBuilder()
                decomp_prompt = f"""Analyze this task and break it down into small, independent steps. 
For each step, provide a brief description (max 10 words).
Return ONLY a JSON array of step objects with format:
[{{"description": "step 1 description"}}, {{"description": "step 2 description"}}, ...]

Task: {task}

Respond with ONLY valid JSON, no markdown, no explanation."""

                messages = [
                    {
                        "role": "system",
                        "content": "You are a task planning assistant. Break down complex tasks into simple steps.",
                    },
                    {"role": "user", "content": decomp_prompt},
                ]

                resp = await call_model(messages, stream=False, format_json=False)

                content = ""
                if isinstance(resp, dict):
                    if resp.get("choices"):
                        ch = resp["choices"][0].get("message")
                        if isinstance(ch, dict):
                            content = ch.get("content") or ""

                # Parse the response
                plan_steps = []
                try:
                    # Try to extract JSON from response
                    json_match = re.search(r"\[.*\]", content, re.DOTALL)
                    if json_match:
                        import json

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
                    # Store the plan and set first step as current
                    return {
                        "history": [],
                        "next_action": None,
                        "rounds": 0,
                        "current_plan": plan_steps,
                        "current_step": 0,
                        "task": plan_steps[0]["description"],  # Focus on first step
                        "task_decomposed": True,
                        "original_task": task,  # Keep original for reference
                    }
            except Exception as e:
                logger.warning(f"Task decomposition failed: {e}")

    # Pre-retrieval: consult repo intelligence tools if available (search_code, find_symbol, find_references)
    retrieved_snippets = []
    try:
        if orchestrator and hasattr(orchestrator, "tool_registry"):
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
    builder = ContextBuilder()
    tools_list = [
        {"name": n, "description": m.get("description", "")}
        for n, m in orchestrator.tool_registry.tools.items()
    ]

    # Assemble the tiered context
    messages = builder.build_prompt(
        identity=state["system_prompt"],
        role=f"Working Directory: {state['working_dir']}",
        active_skills=[],
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
    try:
        raw_resp = call_model(
            messages,
            provider=provider,
            model=model,
            stream=False,
            format_json=False,
            tools=None,
            **llm_kwargs,
        )
        if hasattr(raw_resp, "__await__"):
            resp = await raw_resp
        else:
            resp = raw_resp
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

    try:
        logger.info(f"perception_node: extracted content: {repr(content)[:1000]}")
    except Exception:
        pass

    # Parse tools
    tool_call = parse_tool_block(content)
    try:
        logger.info(f"perception_node: parsed tool_call: {repr(tool_call)}")
    except Exception:
        pass

    # Preserve plan state if already exists
    current_plan = state.get("current_plan")
    current_step = state.get("current_step")
    task_decomposed = state.get("task_decomposed")
    original_task = state.get("original_task")

    result = {
        "history": [{"role": "assistant", "content": content}],
        "next_action": tool_call,
        "rounds": state["rounds"] + 1,
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


async def planning_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Planning Layer: Converts perception outputs into a structured plan.
    Outputs a `current_plan` (list of steps) and sets `current_step` index.
    This is a lightweight, defensive implementation that prefers LLM-generated
    plans but falls back to a simple one-step plan when no plan is detected.
    """
    # Validate orchestrator
    try:
        orchestrator = config.get("configurable", {}).get("orchestrator")
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
        system_prompt = str(s.get("system_prompt") or "")
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

        messages = builder.build_prompt(
            identity=system_prompt,
            role=f"Planner for task: {task}",
            active_skills=[],
            task_description=full_task,
            tools=[],
            conversation=history,
            max_tokens=1500,
        )
        resp = await call_model(messages, stream=False, format_json=False)
        content = ""
        if isinstance(resp, dict):
            if resp.get("choices"):
                ch = resp["choices"][0].get("message")
                if isinstance(ch, dict):
                    content = ch.get("content") or ""
                elif isinstance(ch, str):
                    content = ch
        # Very small heuristic parser: split into numbered lines if present
        plan_lines = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line[0].isdigit() or line.startswith("-"):
                plan_lines.append(line)
        if not plan_lines and content:
            # fallback: single step plan
            plan_lines = [content.strip()]

        if plan_lines:
            steps = []
            for l in plan_lines:
                steps.append({"description": l, "action": None})
            return {"current_plan": steps, "current_step": 0}
    except Exception:
        pass

    # Default: no plan
    return {"current_plan": current_plan, "current_step": current_step}


async def execution_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Execution Layer: Programmatically enforces Operational Workflows.
    Specifically: Read-Before-Edit and Sandbox constraints.
    Handles multi-step plan execution by advancing through steps.
    """
    # Check for cancellation
    cancel_event = state.get("cancel_event")
    if cancel_event and hasattr(cancel_event, "is_set") and cancel_event.is_set():
        logger.info("execution_node: Task canceled by user")
        return {
            "last_result": {"ok": False, "error": "Task canceled by user"},
            "errors": ["canceled"],
            "next_action": None,
        }

    # Validate orchestrator
    try:
        orchestrator = config.get("configurable", {}).get("orchestrator")
        if orchestrator is None:
            logger.error("execution_node: orchestrator is None")
            return {
                "last_result": None,
                "errors": ["orchestrator not found"],
            }
    except Exception as e:
        logger.error(f"execution_node: failed to get orchestrator: {e}")
        return {
            "last_result": None,
            "errors": [f"config error: {e}"],
        }

    try:
        action = state["next_action"]
    except Exception as e:
        logger.error(f"execution_node: failed to get next_action: {e}")
        action = None

    # Handle multi-step plan execution
    current_plan = state.get("current_plan") or []
    current_step = state.get("current_step") or 0
    original_task = state.get("original_task")
    task_decomposed = state.get("task_decomposed", False)

    # If we have a plan but no action, we need to generate one for the current step
    if not action and current_plan and current_step < len(current_plan):
        current_step_desc = current_plan[current_step].get("description", "")
        logger.info(
            f"No action provided, generating tool for step: {current_step_desc}"
        )

        # Call LLM to generate a tool for this step
        try:
            builder = ContextBuilder()
            tools_list = [
                {"name": n, "description": m.get("description", "")}
                for n, m in orchestrator.tool_registry.tools.items()
            ]

            step_prompt = f"""Execute this specific step: {current_step_desc}

Working directory: {state.get("working_dir")}
Original task: {original_task or state.get("task")}

Generate the appropriate tool call to complete this step. Respond with ONLY a tool call in the required XML format."""

            messages = builder.build_prompt(
                identity=state.get("system_prompt", ""),
                role=f"Executing step {current_step + 1}/{len(current_plan)}",
                active_skills=[],
                task_description=step_prompt,
                tools=tools_list,
                conversation=state.get("history", []),
                max_tokens=4000,
            )

            resp = await call_model(messages, stream=False, format_json=False)

            content = ""
            if isinstance(resp, dict):
                if resp.get("choices"):
                    ch = resp["choices"][0].get("message")
                    if isinstance(ch, dict):
                        content = ch.get("content") or ""
                elif resp.get("message"):
                    content = resp.get("message", {}).get("content", "")

            # Parse the tool call
            tool_call = parse_tool_block(content)
            if tool_call:
                logger.info(f"Generated tool call for step: {tool_call}")
                # Update the step with the action
                current_plan[current_step]["action"] = tool_call
                action = tool_call
        except Exception as e:
            logger.error(f"Failed to generate tool for step: {e}")
            _notify_provider_limit(str(e))

    # If we have a plan and haven't finished it, check if we need to advance
    if current_plan and current_step < len(current_plan):
        current_step_desc = current_plan[current_step].get("description", "")

        # Update the task to focus on the current step
        if task_decomposed and original_task:
            # Construct context for current step
            progress_msg = f"Working on step {current_step + 1}/{len(current_plan)} of multi-step task.\n"
            progress_msg += f"Current step: {current_step_desc}\n"
            if current_step > 0:
                progress_msg += f"Completed: {', '.join([s.get('description', '') for s in current_plan[:current_step] if s.get('completed')])}"
            logger.info(
                f"Plan execution: step {current_step + 1}/{len(current_plan)} - {current_step_desc}"
            )

    if not action:
        return {"last_result": None}

    tool_name = action["name"]
    args = action.get("arguments", {})
    path_arg = args.get("path") or args.get("file_path")

    # Hard Rule Enforcement: Read-Before-Edit
    if tool_name == "edit_file" and path_arg:
        try:
            resolved = str((Path(state["working_dir"]) / path_arg).resolve())
            # Check both the state (immutability) AND the orchestrator (current session)
            if (
                resolved not in state["verified_reads"]
                and resolved not in orchestrator._session_read_files
            ):
                err_msg = (
                    f"Logic violation: You must read '{path_arg}' before editing it."
                )
                return {
                    "last_result": {"ok": False, "error": err_msg},
                    "history": [
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "tool_execution_result": {
                                        "ok": False,
                                        "error": err_msg,
                                    }
                                }
                            ),
                        }
                    ],
                    "next_action": None,  # Reset to force re-planning
                }
        except Exception:
            pass

    # Loop Prevention: Check for repeated tool calls
    if orchestrator and hasattr(orchestrator, "_check_loop_prevention"):
        trace_before = orchestrator._read_execution_trace()
        loop_detected = orchestrator._check_loop_prevention(tool_name, args)
        if loop_detected:
            loop_msg = "[LOOP DETECTED] Repeated tool calls blocked; consider alternate strategy."
            try:
                orchestrator.msg_mgr.append("system", loop_msg)
            except Exception:
                pass
            return {
                "last_result": {"ok": False, "error": loop_msg},
                "history": [
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "tool_execution_result": {
                                    "ok": False,
                                    "error": loop_msg,
                                }
                            }
                        ),
                    }
                ],
                "next_action": None,
            }

    # Workflow Enforcement 2: Sandbox Preflight
    preflight = orchestrator.preflight_check(action)
    if not preflight.get("ok"):
        error_content = f"[SANDBOX VIOLATION] {preflight.get('error')}"
        # Also publish to the orchestrator's history directly to guarantee it's captured
        try:
            orchestrator.msg_mgr.append("user", error_content)
        except Exception as e:
            logger.error(
                f"Failed to append sandbox violation to orchestrator history: {e}"
            )

        return {
            "last_result": preflight,
            "history": [
                {
                    "role": "user",
                    "content": error_content,
                }
            ],
            "next_action": None,
        }

    # Execute tool
    res = orchestrator.execute_tool(action)

    # Successful tool execution
    verified_update = []
    plan_advance = {}

    # Check for multi-step plan completion
    if current_plan and current_step < len(current_plan):
        # Check if execution was successful
        if res.get("ok"):
            # Mark current step as completed
            current_plan[current_step]["completed"] = True
            next_step = current_step + 1

            if next_step < len(current_plan):
                # Move to next step
                plan_advance = {
                    "current_step": next_step,
                    "current_plan": current_plan,
                    "task": current_plan[next_step].get("description", ""),
                }
                logger.info(
                    f"Step {current_step + 1} complete, advancing to step {next_step + 1}"
                )
            else:
                # Plan complete
                plan_advance = {
                    "current_step": next_step,
                    "current_plan": current_plan,
                    "task": original_task or "Task complete",
                }
                logger.info("All plan steps completed")

    if res.get("ok"):
        # Log to trace
        import datetime

        orchestrator._append_execution_trace(
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "goal": state["task"],
                "tool": tool_name,
                "args": args,
                "result_summary": str(res)[:100],
            }
        )

        # Check actual tool logic success (different from tool wrapper success)
        actual_res = res.get("result", {})

        # Log failure for debugging if needed
        if tool_name == "edit_file" and actual_res.get("status") != "ok":
            import logging

            logging.getLogger("coding_agent").info(f"PATCH FAILED: {actual_res}")

        status = actual_res.get("status")
        if status is None:
            if actual_res.get("items") is not None:
                status = "ok"
            elif "content" in actual_res:
                status = "ok"

        if status == "ok":
            if (
                tool_name in ["read_file", "fs.read", "list_dir", "list_files"]
                and path_arg
            ):
                try:
                    resolved = str((Path(state["working_dir"]) / path_arg).resolve())
                    verified_update = [resolved]
                except Exception:
                    pass

        # Sync session state immediately to ensure the next turn in THIS graph run sees it
        if verified_update:
            for path in verified_update:
                orchestrator._session_read_files.add(path)

    return {
        "last_result": res,
        "verified_reads": verified_update,
        "history": [
            {"role": "user", "content": json.dumps({"tool_execution_result": res})}
        ],
        "next_action": None,  # Reset after execution
        **plan_advance,
    }


async def verification_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Verification Layer: Run tests / linters / syntax checks on proposed edits.
    Also validates file deletions to ensure files are actually deleted.
    This node is intentionally conservative: it will only run verification tools when
    the state indicates a recent edit or when the `current_plan` requests validation.
    """
    import logging

    logger = logging.getLogger(__name__)
    orchestrator = config.get("configurable", {}).get("orchestrator")

    # Decide whether verification is needed
    last_result = state.get("last_result") or {}
    need_verify = False
    verification_type = None

    # Check if last action was a deletion
    if isinstance(last_result, dict):
        r = last_result.get("result") or {}
        if isinstance(r, dict) and r.get("status") == "ok" and r.get("deleted"):
            # This was a delete_file call - verify the file is actually gone
            deleted_path = r.get("path")
            if deleted_path:
                workdir = Path(state.get("working_dir", "."))
                full_path = (
                    workdir / deleted_path
                    if not Path(deleted_path).is_absolute()
                    else Path(deleted_path)
                )
                if full_path.exists():
                    logger.warning(
                        f"Verification FAILED: {deleted_path} still exists after deletion"
                    )
                    return {
                        "verification_result": {
                            "deletion_verification": "FAILED",
                            "error": f"File still exists: {deleted_path}",
                            "path": deleted_path,
                        }
                    }
                else:
                    logger.info(
                        f"Verification PASSED: {deleted_path} successfully deleted"
                    )
                    return {
                        "verification_result": {
                            "deletion_verification": "PASSED",
                            "path": deleted_path,
                        }
                    }

    # If the last action was an edit_file that reported ok, run verification
    try:
        if isinstance(last_result, dict):
            r = last_result.get("result") or {}
            if isinstance(r, dict) and r.get("status") == "ok" and r.get("path"):
                need_verify = True
                verification_type = "edit"
    except Exception:
        pass

    results = {}
    if need_verify:
        try:
            from src.tools import verification_tools

            wd = Path(state.get("working_dir"))
            results["tests"] = verification_tools.run_tests(str(wd))
            results["linter"] = verification_tools.run_linter(str(wd))
            results["syntax"] = verification_tools.syntax_check(str(wd))
        except Exception as e:
            results["error"] = str(e)

    return {"verification_result": results}


async def memory_update_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Memory Update Layer: Persists distilled context to TASK_STATE.md.
    """
    import logging

    logger = logging.getLogger(__name__)
    orchestrator = config.get("configurable", {}).get("orchestrator")
    try:
        history_len = len(state.get("history", []))
        working_dir = state.get("working_dir", "unknown")
        logger.info(
            f"memory_update_node: distilling {history_len} messages from {working_dir}"
        )
        # Trigger distillation to sync TASK_STATE.md
        distill_context(state["history"], working_dir=Path(state["working_dir"]))
        logger.info(f"memory_update_node: distillation complete")
    except Exception as e:
        logger.error(f"memory_update_node: distillation failed: {e}")
    return {}


async def debug_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Debug Node: Analyzes verification failures and attempts to fix issues.
    Called when verification fails, with max retry limit.
    """
    import logging

    logger = logging.getLogger(__name__)
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
Respond with ONLY a tool call in XML format."""

    try:
        from src.core.context.context_builder import ContextBuilder
        from src.core.llm_manager import call_model
        from src.core.orchestration.tool_parser import parse_tool_block

        builder = ContextBuilder()
        tools_list = [
            {"name": n, "description": m.get("description", "")}
            for n, m in orchestrator.tool_registry.tools.items()
        ]

        messages = builder.build_prompt(
            identity=state.get("system_prompt", ""),
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


async def step_controller_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Step Controller: Enforces single-step execution from the plan.
    Validates that the current step matches the planned action.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info("=== step_controller_node START ===")

    current_plan = state.get("current_plan")
    if not isinstance(current_plan, list):
        current_plan = []
    current_step = state.get("current_step")
    if not isinstance(current_step, int):
        current_step = 0
    step_controller_enabled = bool(state.get("step_controller_enabled", True))

    if not step_controller_enabled or not current_plan:
        logger.info("step_controller_node: disabled or no plan, passing through")
        return {}

    plan_len = len(current_plan)
    if current_step >= plan_len:
        logger.info(
            f"step_controller_node: step {current_step} >= plan length {plan_len}"
        )
        return {"next_action": None}

    current_step_data = current_plan[current_step]
    step_description = str(current_step_data.get("description", ""))
    planned_action = current_step_data.get("action")

    logger.info(
        f"step_controller_node: enforcing step {current_step + 1}/{plan_len}: {step_description}"
    )

    return {
        "step_description": step_description,
        "planned_action": planned_action,
    }
