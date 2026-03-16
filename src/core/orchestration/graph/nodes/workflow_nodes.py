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


async def perception_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Perception Layer: Responsible for generating the next action or thought.
    Enforces 'Thinking' and 'XML Tool Usage' before moving to execution.
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info("=== perception_node START ===")
    orchestrator = config.get("configurable", {}).get("orchestrator")
    adapter = orchestrator.adapter

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
                sc = _call_tool_if_exists("search_code", query=query, workdir=state.get("working_dir"))
                if sc:
                    # support both dict{results:[]} and list
                    if isinstance(sc, dict) and sc.get("results"):
                        for r in sc.get("results"):
                            retrieved_snippets.append({
                                "file_path": r.get("file_path") or r.get("file"),
                                "snippet": r.get("snippet") or r.get("text") or r.get("content"),
                                "reason": "search_code",
                            })
                    elif isinstance(sc, list):
                        for r in sc:
                            if isinstance(r, dict):
                                retrieved_snippets.append({
                                    "file_path": r.get("file_path") or r.get("file"),
                                    "snippet": r.get("snippet") or r.get("text") or r.get("content"),
                                    "reason": "search_code",
                                })
            except Exception:
                pass

            # find_symbol
            try:
                fs = _call_tool_if_exists("find_symbol", name=query, workdir=state.get("working_dir"))
                if fs and isinstance(fs, dict) and fs.get("file_path"):
                    retrieved_snippets.append({"file_path": fs.get("file_path"), "snippet": fs.get("snippet"), "reason": "find_symbol"})
            except Exception:
                pass

            # find_references
            try:
                fr = _call_tool_if_exists("find_references", name=query, workdir=state.get("working_dir"))
                if fr and isinstance(fr, list):
                    for r in fr:
                        if isinstance(r, dict):
                            retrieved_snippets.append({"file_path": r.get("file_path"), "snippet": r.get("excerpt") or r.get("context"), "reason": "find_references"})
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
        if hasattr(adapter, "provider") and isinstance(adapter.provider, dict):
            provider = (
                adapter.provider.get("name") or adapter.provider.get("type") or "None"
            )
        if hasattr(adapter, "models") and adapter.models:
            model = adapter.models[0]

    # Determine deterministic overrides if orchestrator requests them
    llm_kwargs = {}
    try:
        if orchestrator and getattr(orchestrator, "deterministic", False):
            llm_kwargs["temperature"] = 0.0
            seed = getattr(orchestrator, "seed", None)
            if seed is not None:
                llm_kwargs["seed"] = seed
    except Exception:
        pass

    # LLM Inference
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

    # Update state: record assistant turn and the identified action
    return {
        "history": [{"role": "assistant", "content": content}],
        "next_action": tool_call,
        "rounds": state["rounds"] + 1,
    }


async def planning_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Planning Layer: Converts perception outputs into a structured plan.
    Outputs a `current_plan` (list of steps) and sets `current_step` index.
    This is a lightweight, defensive implementation that prefers LLM-generated
    plans but falls back to a simple one-step plan when no plan is detected.
    """
    orchestrator = config.get("configurable", {}).get("orchestrator")

    # Treat state as a plain dict for flexible lookups
    s = dict(state)

    # If the perception already provided a next_action, try to build a simple plan
    task = s.get("task") or ""

    # Minimal planner: if next_action exists, make a one-step plan; otherwise ask the LLM
    current_plan = s.get("current_plan") or []
    current_step = s.get("current_step") or 0

    if s.get("next_action"):
        # Construct a trivial plan wrapping the existing action
        step = {
            "action": s.get("next_action"),
            "description": "Execute the requested tool",
        }
        current_plan = [step]
        current_step = 0
        return {"current_plan": current_plan, "current_step": current_step}

    # Fallback: ask the model for a short plan (non-blocking best effort)
    try:
        builder = ContextBuilder()
        messages = builder.build_prompt(
            identity=s.get("system_prompt"),
            role=f"Planner for task: {task}",
            active_skills=[],
            task_description=task,
            tools=[],
            conversation=s.get("history", []),
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
    """
    orchestrator = config.get("configurable", {}).get("orchestrator")
    action = state["next_action"]

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
    }


async def verification_node(state: AgentState, config: Any) -> Dict[str, Any]:
    """
    Verification Layer: Run tests / linters / syntax checks on proposed edits.
    This node is intentionally conservative: it will only run verification tools when
    the state indicates a recent edit or when the `current_plan` requests validation.
    """
    orchestrator = config.get("configurable", {}).get("orchestrator")

    # Decide whether verification is needed
    last_result = state.get("last_result") or {}
    need_verify = False
    # If the last action was an edit_file that reported ok, run verification
    try:
        if isinstance(last_result, dict):
            r = last_result.get("result") or {}
            if isinstance(r, dict) and r.get("status") == "ok" and r.get("path"):
                need_verify = True
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
