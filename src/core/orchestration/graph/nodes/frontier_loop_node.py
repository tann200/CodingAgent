"""frontier_loop_node.py — Tight LLM+tool loop for LARGE/FRONTIER models (TASK-5).

Keeps LLM+tool calls within a single node instead of round-tripping
through the LangGraph state machine for every tool call.

Exit conditions: no tool calls in response, tool budget exhausted,
plan-mode write gate, context overflow, max turns reached.

On exit sets: last_result, tool_call_count, _frontier_loop_turns,
awaiting_plan_approval, errors.

Used by the tier-aware ``build_tier_graph()``."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, cast

from src.core.orchestration.graph.state import StateLike
from src.core.orchestration.graph.nodes.execution_helpers import (
    build_read_then_write_result,
    build_tool_history_messages,
)
from src.core.orchestration.graph.nodes.tool_output_truncation import (
    TOOL_LARGE_TEXT_FIELDS,
    TOOL_OUTPUT_MAX_BYTES,
    prune_tool_outputs,
    truncate_tool_output,
)
from src.core.orchestration.graph.nodes.node_utils import (
    _resolve_orchestrator,
    _notify_provider_limit,
)
from src.core.orchestration.tool_parser import parse_tool_block
from src.core.inference.kv_cache_governor import (
    CompactionAction,
    KVCacheGovernor,
    create_governor_for_model,
)

logger = logging.getLogger(__name__)
from src.core.logger import logger as guilogger  # noqa: E402
from src.core.messaging.event_types import AgentStatus, ToolResult

# Core tools for small/local models.  Larger tiers get core + extras up to
# their ``get_tool_limit()`` via ``_filter_tools_for_tier``.
_CORE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "read_file",
        "write_file",
        "list_files",
        "bash",
        "edit_file_atomic",
        "grep",
        "glob",
        "run_tests",
        "delegate_task",
    }
)

def _filter_tools_for_tier(tools_schema: list, model_tier: str) -> list:
    if not tools_schema:
        return tools_schema
    try:
        from src.core.inference.model_tiers import ModelTier, get_tool_limit
        tier = ModelTier(model_tier.lower())
    except (ValueError, AttributeError):
        return tools_schema
    if tier == ModelTier.SMALL:
        return [t for t in tools_schema if t.get("function", {}).get("name") in _CORE_TOOL_NAMES]
    limit = get_tool_limit(tier)
    core = [t for t in tools_schema if t.get("function", {}).get("name") in _CORE_TOOL_NAMES]
    extras = [t for t in tools_schema if t.get("function", {}).get("name") not in _CORE_TOOL_NAMES]
    expanded = list(core)
    remaining = limit - len(core)
    if remaining > 0 and extras:
        expanded.extend(extras[:remaining])
    return expanded

# Maximum turns within one invocation of frontier_loop_node.
# Each "turn" is one LLM call + its tool calls.
# This bounds the node's wall-clock time and prevents runaway infinite loops.
# Individual tool-call budget is still governed by state["max_tool_calls"].
_MAX_FRONTIER_TURNS = 20

# OP-9: Cap per-tool output that enters conversation history.
_TOOL_OUTPUT_MAX_BYTES = TOOL_OUTPUT_MAX_BYTES
_TOOL_LARGE_TEXT_FIELDS = TOOL_LARGE_TEXT_FIELDS

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
    return truncate_tool_output(res, marker_label="frontier_loop")

def _extract_content_text(response: Any) -> str:
    """Extract text content from an LLM response object."""
    try:
        if isinstance(response, dict):
            choices = response.get("choices", [])
            if choices and isinstance(choices, list):
                msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                if isinstance(msg, dict):
                    content = msg.get("content")
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
        content = response.content if hasattr(response, "content") else response  # type: ignore[union-attr]
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
    patterns = (
        "maximum context length", "context window limit", "context window exceeded",
        "exceeds the available context", "input is too long", "prompt is too long",
        "context length limit reached", "token limit exceeded", "max tokens exceeded",
        "reduce the length", "too many tokens",
    )
    low = error_msg.lower()
    return sum(1 for p in patterns if p in low) >= 2

def _normalize_tool_call(tc: Any) -> tuple[str, dict]:
    """Return a normalized (tool_name, tool_args) tuple."""
    if isinstance(tc, dict):
        tool_name = (
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
        return tool_name, raw_args if isinstance(raw_args, dict) else {}

    try:
        tool_name = tc.function.name
        raw_args = tc.function.arguments
        tool_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        return tool_name, tool_args if isinstance(tool_args, dict) else {}
    except Exception:
        return "", {}

def _render_tool_calls_text(tool_calls: list[Any]) -> str:
    """Serialize tool calls into parseable assistant history content."""
    blocks: list[str] = []
    for tc in tool_calls:
        tool_name, tool_args = _normalize_tool_call(tc)
        if not tool_name:
            continue
        try:
            blocks.append(
                json.dumps(
                    {"name": tool_name, "arguments": tool_args},
                    ensure_ascii=True,
                    default=str,
                )
            )
        except Exception:
            continue
    return "\n".join(blocks)

def _append_tool_call_history(history: list[dict], tool_calls: list[Any]) -> list[dict]:
    """Ensure history contains a parseable assistant tool-call record."""
    rendered = _render_tool_calls_text(tool_calls)
    if not rendered:
        return history

    updated = list(history)
    if updated and updated[-1].get("role") == "assistant":
        prior_content = updated[-1].get("content") or ""
        if not parse_tool_block(prior_content):
            merged = (
                f"{prior_content.rstrip()}\n\n{rendered}" if str(prior_content).strip() else rendered
            )
            updated[-1] = {**updated[-1], "content": merged}
            return updated

    updated.append({"role": "assistant", "content": rendered})
    return updated


def _extract_tool_call_from_text(content: str) -> tuple[dict | None, str]:
    """Scan *content* for an embedded JSON tool-call block and return (tool_call_dict, remaining_text).

    Handles:
    1. Pure JSON: ``{"name": "read_file", "arguments": {...}}``
    2. JSON embedded at the end of a thinking block (e.g. Gemma returns reasoning text
       followed by a newline-separated JSON object).

    Returns ``(None, content)`` when no tool call JSON is found.
    """
    # Fast-path: entire content is a JSON tool call
    _stripped = content.strip()
    try:
        _obj = json.loads(_stripped)
        if isinstance(_obj, dict) and ("name" in _obj or "tool" in _obj):
            _tc_name = _obj.get("name") or _obj.get("tool", "")
            _tc_args = _obj.get("arguments") or _obj.get("args") or {}
            if isinstance(_tc_args, str):
                try:
                    _tc_args = json.loads(_tc_args)
                except Exception:
                    _tc_args = {}
            if _tc_name:
                return {"name": _tc_name, "arguments": _tc_args}, ""
    except Exception:
        pass

    # Slow-path: scan for a balanced JSON object containing "name" or "tool" key.
    # Walk through content right-to-left looking for '{' that starts a balanced JSON
    # object at or near the end of the string.
    def _try_parse_balanced_json_at(text: str, start: int) -> tuple[dict | None, int]:
        """Try to parse a balanced JSON object starting at text[start]. Returns (obj, end_idx) or (None, -1)."""
        if text[start] != '{':
            return None, -1
        depth = 0
        in_str = False
        escape = False
        i = start
        while i < len(text):
            ch = text[i]
            if escape:
                escape = False
            elif ch == '\\' and in_str:
                escape = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            obj = json.loads(candidate)
                            return obj, i + 1
                        except Exception:
                            return None, -1
            i += 1
        return None, -1

    # Find all '{' positions in reverse order (prefer last occurrence)
    positions = [i for i, ch in enumerate(content) if ch == '{']
    for pos in reversed(positions):
        obj, end = _try_parse_balanced_json_at(content, pos)
        if obj is not None and isinstance(obj, dict) and ("name" in obj or "tool" in obj):
            _tc_name = obj.get("name") or obj.get("tool", "")
            _tc_args = obj.get("arguments") or obj.get("args") or {}
            if isinstance(_tc_args, str):
                try:
                    _tc_args = json.loads(_tc_args)
                except Exception:
                    _tc_args = {}
            if _tc_name:
                _remaining = content[:pos].strip()
                return {"name": _tc_name, "arguments": _tc_args}, _remaining

    return None, content


def _normalize_messages_for_native_tools(messages: list[dict]) -> list[dict]:
    """Reformat message history for native tool-calling API (OpenAI format).

    When using native tool calling, the conversation history must follow the
    OpenAI protocol:
      - assistant messages that called a tool must have a ``tool_calls`` array
        (not just text content)
      - tool results must use ``role="tool"`` with ``tool_call_id``

    This function post-processes a message list that was built using the legacy
    text-based format (tool calls as JSON text in assistant content, tool results
    as ``role="user"`` with ``{"tool_execution_result": ...}`` JSON) and converts
    it into the correct native format.

    Messages that are not tool-related (plain user/assistant text) are left as-is.
    """
    normalized: list[dict] = []
    _id_counter = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "") or ""

        # ---- Detect assistant messages that contain a tool call as JSON text ----
        if role == "assistant" and content and not msg.get("tool_calls"):
            _tc_dict, _remaining = _extract_tool_call_from_text(content)
            if _tc_dict:
                _id_counter += 1
                _call_id = f"call_{_id_counter}"
                _native_tc = {
                    "id": _call_id,
                    "type": "function",
                    "function": {
                        "name": _tc_dict["name"],
                        "arguments": json.dumps(_tc_dict.get("arguments") or {}),
                    },
                }
                # Emit the assistant message with tool_calls (and optional thinking text)
                _asst_msg: dict = {
                    "role": "assistant",
                    "content": _remaining or "",  # use empty string, not None
                    "tool_calls": [_native_tc],
                    "_call_id": _call_id,
                }
                normalized.append(_asst_msg)
                continue

        # ---- Detect assistant messages that already have tool_calls ----
        if role == "assistant" and msg.get("tool_calls"):
            # Carry the last tool_call id for pairing with tool results
            _tc_list = msg["tool_calls"]
            _last_id = None
            if _tc_list and isinstance(_tc_list[-1], dict):
                _last_id = _tc_list[-1].get("id")
            _new_msg = dict(msg)
            if _last_id:
                _new_msg["_call_id"] = _last_id
            normalized.append(_new_msg)
            continue

        # ---- Detect user messages that are tool execution results ----
        if role == "user" and content:
            _stripped = content.strip()
            # Strip off any appended task block before trying to parse JSON
            # (append_task_message wraps the last user msg with <task> tags)
            _json_part = _stripped
            if "<task>" in _json_part:
                _json_part = _json_part[: _json_part.index("<task>")].strip()
            try:
                _obj = json.loads(_json_part)
                if isinstance(_obj, dict) and "tool_execution_result" in _obj:
                    # Find the most recent assistant message's call_id
                    _call_id = "call_0"
                    for _prev in reversed(normalized):
                        if _prev.get("_call_id"):
                            _call_id = _prev["_call_id"]
                            break
                    _result = _obj["tool_execution_result"]
                    if isinstance(_result, dict):
                        _result_text = (
                            _result.get("content")
                            or _result.get("output")
                            or json.dumps(_result)
                        )
                    else:
                        _result_text = str(_result)
                    normalized.append(
                        {
                            "role": "tool",
                            "tool_call_id": _call_id,
                            "content": _result_text,
                        }
                    )
                    continue
            except Exception:
                pass

        # ---- Pass through all other messages ----
        normalized.append(dict(msg))

    # Remove internal carry fields before returning
    for _m in normalized:
        _m.pop("_call_id", None)

    return normalized


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
# Turn helpers extracted from the main loop
# ---------------------------------------------------------------------------


async def _prepare_turn_messages(
    orchestrator: Any,
    task: str,
    history: list,
    model_tier: str,
    turns_taken: int,
) -> list:
    """Build the messages list for one LLM turn.

    Returns a list of chat messages ready to pass to ``call_model``.
    Falls back to a minimal system prompt on any error.
    """
    try:
        from src.core.context.context_builder import ContextBuilder
        from src.core.inference.provider_context import get_context_budget
        from src.core.orchestration.event_bus import run_with_correlation
        from src.core.orchestration.provider_capabilities import (
            resolve_provider_capabilities as _resolve_pc,
        )
        import functools

        cb = ContextBuilder(working_dir=getattr(orchestrator, "working_dir", None))

        guilogger.info("[frontier_loop_node] turn=%d history_before_prune=%s",
                       turns_taken, [(m.get("role"), str(m.get("content", ""))[:300]) for m in history])
        history = prune_tool_outputs(history)

        token_budget = get_context_budget(model_tier=model_tier)

        tools_schema: list = []
        if orchestrator:
            try:
                reg = getattr(orchestrator, "tool_registry", None)
                if reg and hasattr(reg, "get_openai_functions"):
                    tools_schema = reg.get_openai_functions()
            except Exception:
                pass

        tools_schema = _filter_tools_for_tier(tools_schema, model_tier)

        provider_caps = _resolve_pc(orchestrator)

        loop = asyncio.get_running_loop()
        partial_fn = functools.partial(
            cb.build_prompt,
            "operational",
            [],
            task,
            tools_schema or [],
            history,
            token_budget,
            None,
            provider_caps,
            None,
            model_tier,
        )
        built_messages = await run_with_correlation(loop, None, partial_fn)
        messages = list(built_messages) if isinstance(built_messages, list) else []
        if not messages:
            messages = [
                {
                    "role": "system",
                    "content": f"You are a capable coding assistant. Task: {task}",
                }
            ]

        if (
            provider_caps.get("provider_family") == "local"
            and provider_caps.get("supports_native_tools")
        ):
            _sys_content = (
                "You are a coding assistant with access to tools. "
                "IMPORTANT: You MUST use the provided function-calling tools to complete tasks. "
                "Do NOT write code or answers in plain text. "
                "Always call a tool (read_file, write_file, bash, etc.) to take action. "
                f"Current task: {task}"
            )
            _has_sys = False
            for _i, _m in enumerate(messages):
                if isinstance(_m, dict) and _m.get("role") == "system":
                    messages[_i] = {"role": "system", "content": _sys_content}
                    _has_sys = True
                    break
            if not _has_sys:
                messages = [{"role": "system", "content": _sys_content}] + messages

            messages = _normalize_messages_for_native_tools(messages)
            guilogger.info(
                "[frontier_loop_node] turn=%d after_normalization=%s",
                turns_taken,
                [(m.get("role"), str(m.get("content") or m.get("tool_calls") or "")[:200]) for m in messages],
            )
        return messages
    except Exception as exc:
        logger.warning("frontier_loop_node: context build failed: %s", exc)
        return [
            {
                "role": "system",
                "content": f"You are a capable coding assistant. Task: {task}",
            }
        ]


async def _call_llm_for_turn(
    messages: list,
    provider_name: "str | None",
    model_name: "str | None",
    orchestrator: Any,
    turns_taken: int,
    model_tier: str = "frontier",
) -> "tuple[Any, str, list]":
    """Call the LLM and extract ``(response, content_text, tool_calls)``.

    Raises any LLM exception so the caller can decide how to handle it.
    """
    tools_schema: list = []
    if orchestrator:
        try:
            reg = getattr(orchestrator, "tool_registry", None)
            if reg and hasattr(reg, "get_openai_functions"):
                tools_schema = reg.get_openai_functions()
        except Exception:
            pass

    tools_schema = _filter_tools_for_tier(tools_schema, model_tier)

    guilogger.info(
        "[frontier_loop_node] turn=%d messages_preview=%s",
        turns_taken,
        [(m.get("role"), str(m.get("content") or m.get("tool_calls") or "")[:300]) for m in messages],
    )
    guilogger.info(
        "[frontier_loop_node] turn=%d messages_has_tool_calls=%s",
        turns_taken,
        [bool(m.get("tool_calls")) for m in messages],
    )

    _llm_timeout: int | None = 120
    try:
        from src.core.orchestration.project_settings import get_active_settings as _gas_fl

        _ps_fl = _gas_fl()
        if _ps_fl is not None:
            _llm_timeout = _ps_fl.max_llm_wait_seconds or None
    except Exception:
        pass

    response = await asyncio.wait_for(
        call_model(
            messages=messages,
            provider=provider_name,
            model=model_name,
            tools=tools_schema or None,
        ),
        timeout=_llm_timeout,
    )

    guilogger.info(
        "[frontier_loop_node] turn=%d tools_sent=%d finish_reason=%s tool_calls_len=%s content_len=%s raw_choices=%s raw_response=%s",
        turns_taken,
        len(tools_schema),
        response.get("finish_reason") if isinstance(response, dict) else "n/a",
        len(response.get("tool_calls") or []) if isinstance(response, dict) else "n/a",
        len(response.get("content", "") or "") if isinstance(response, dict) else "n/a",
        str(response.get("choices", []))[:300] if isinstance(response, dict) else "n/a",
        str({k: v for k, v in response.items() if k not in ("choices", "raw")})[:300] if isinstance(response, dict) else "n/a",
    )

    content_text = _extract_content_text(response)

    # Extract native tool calls
    if isinstance(response, dict):
        choices = response.get("choices", [])
        if choices and len(choices) > 0:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            msg = choice.get("message", {}) if isinstance(choice, dict) else {}
            tool_calls: list[Any] = []
            if isinstance(msg, dict):
                tool_calls = msg.get("tool_calls") or []
            if not tool_calls and isinstance(choice, dict):
                tool_calls = choice.get("tool_calls") or []
        else:
            tool_calls = []
    else:
        tool_calls = getattr(response, "tool_calls", None) or []

    # Text-based fallback
    if not tool_calls and content_text:
        try:
            import re

            content_for_parse = re.sub(
                r"", "", content_text, flags=re.DOTALL
            ).strip()
            content_for_parse = re.sub(
                r"<\|channel\|>thought.*?<\|/channel\|>",
                "",
                content_for_parse,
                flags=re.DOTALL,
            ).strip()
            if content_for_parse:
                parsed = parse_tool_block(content_for_parse)
                if parsed and isinstance(parsed, dict) and (parsed.get("name") or parsed.get("tool")):
                    tool_calls = [parsed]
                    logger.info("frontier_loop_node: parsed tool call: %r", parsed.get("name"))
        except Exception as exc:
            logger.debug("frontier_loop_node: tool parse error: %s", exc)
            tool_calls = []

    # Synthesize tool-call text into content when needed
    synthesized_tool_text = ""
    if tool_calls and not parse_tool_block(content_text or ""):
        synthesized_tool_text = _render_tool_calls_text(tool_calls)
    if synthesized_tool_text:
        content_text = (
            f"{content_text.rstrip()}\n\n{synthesized_tool_text}"
            if content_text.strip()
            else synthesized_tool_text
        )

    return response, content_text, tool_calls


async def _dispatch_tool_calls(
    tool_calls: list,
    history: list,
    state: "StateLike",
    orchestrator: Any,
    model_tier: str,
    bus: Any,
    turns_taken: int,
    tool_call_count: int,
    max_tool_calls: int,
) -> "tuple[list, int, Dict[str, Any] | None]":
    """Execute a batch of tool calls and return ``(history, tool_call_count, last_result)``.

    Stops executing once the budget is reached.  Callers should check
    ``tool_call_count >= max_tool_calls`` after this returns.
    """
    last_result: "Dict[str, Any] | None" = None

    for tc in tool_calls:
        tool_name, tool_args = _normalize_tool_call(tc)
        if not tool_name:
            continue

        logger.info("frontier_loop_node: executing tool %r (turn %d)", tool_name, turns_taken)

        # Execute
        action = {"name": tool_name, "arguments": tool_args}
        try:
            if orchestrator:
                if model_tier in ("large", "frontier", "medium"):
                    from src.core.orchestration.event_bus import run_with_correlation

                    loop = asyncio.get_running_loop()
                    tool_result = await run_with_correlation(
                        loop, None, orchestrator.execute_tool, action
                    )
                else:
                    tool_result = orchestrator.execute_tool(action)
            else:
                tool_result = {"ok": False, "error": "orchestrator unavailable"}
        except Exception as exc:
            logger.warning("frontier_loop_node: tool '%s' raised: %s", tool_name, exc)
            tool_result = {"ok": False, "error": str(exc)}

        if not isinstance(tool_result, dict):
            tool_result = (
                {} if tool_result is None else {"ok": False, "error": str(tool_result)}
            )
        tool_result = cast(Dict[str, Any], tool_result)
        tool_result = _truncate_tool_output(tool_result)
        tool_call_count += 1
        last_result = tool_result

        try:
            if bus:
                bus.publish_typed(ToolResult(tool=tool_name, result=tool_result, turn=turns_taken))
        except Exception:
            pass

        # Update file-tracking state
        path_arg = None
        if isinstance(tool_args, dict):
            raw_path = tool_args.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                path_arg = raw_path

        files_read = dict(state.get("files_read") or {})
        tool_last_used = dict(state.get("tool_last_used") or {})
        if path_arg and tool_name in ("read_file", "fs.read"):
            try:
                resolved_path = str(
                    (Path(str(state.get("working_dir") or ".")) / path_arg).resolve()
                )
                files_read[resolved_path] = True
                tool_last_used[f"{tool_name}:{path_arg}"] = tool_call_count
            except Exception:
                pass

        legacy_history = build_tool_history_messages(
            result=tool_result,
            truncate_tool_output=_truncate_tool_output,
        )
        read_then_write_result = build_read_then_write_result(
            state={**state, "tool_call_count": tool_call_count - 1},
            result=tool_result,
            tool_name=tool_name,
            path_arg=path_arg,
            working_dir=str(state.get("working_dir") or "."),
            truncate_tool_output=_truncate_tool_output,
            tool_last_used=tool_last_used,
            files_read=files_read,
        )
        if read_then_write_result and read_then_write_result.get("history"):
            legacy_history = list(read_then_write_result.get("history") or []) + legacy_history

        history = list(history)
        history.extend(legacy_history)

        if tool_call_count >= max_tool_calls:
            logger.warning(
                "frontier_loop_node: tool budget exhausted after '%s' (%d/%d)",
                tool_name,
                tool_call_count,
                max_tool_calls,
            )
            break

    return history, tool_call_count, last_result


# ---------------------------------------------------------------------------
# Main node
# ---------------------------------------------------------------------------


async def frontier_loop_node(
    state: StateLike,
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
    history: list = list(state.get("history") or [])
    tool_call_count: int = int(state.get("tool_call_count") or 0)
    max_tool_calls: int = int(state.get("max_tool_calls") or 30)
    model_tier: str = (state.get("model_tier") or "frontier").lower()
    errors: list = list(state.get("errors") or [])
    pending_action: Dict[str, Any] | None = (
        state.get("next_action") if isinstance(state.get("next_action"), dict) else None
    )

    # GAP-FRONTIER-3 fix: inject analyst_findings into task context so the LLM
    # benefits from pre-loop analysis produced by analyst_delegation_node.
    analyst_findings: str = (state.get("analyst_findings") or "").strip()
    if analyst_findings:
        task = f"{task}\n\n<analyst_findings>\n{analyst_findings}\n</analyst_findings>"
        logger.info("frontier_loop_node: injecting analyst_findings (%d chars)", len(analyst_findings))
    provider_name: str | None = None
    model_name: str | None = None

    try:
        from src.core.inference.provider_utils import resolve_provider_capabilities

        caps = resolve_provider_capabilities(orchestrator, getattr(orchestrator, "adapter", None) if orchestrator else None)
        provider_name = caps.get("provider_name") or None
        model_name = caps.get("model") or None
    except Exception:
        provider_name = None
        model_name = None

    kv_gov: KVCacheGovernor | None = None
    try:
        if model_name:
            kv_gov = create_governor_for_model(model_name)
    except Exception:
        pass

    last_result: Dict[str, Any] | None = None
    turns_taken: int = 0

    logger.info(
        "frontier_loop_node: starting (task=%r, tool_calls=%d/%d, tier=%s)",
        task[:80],
        tool_call_count,
        max_tool_calls,
        model_tier,
    )

    bus: Any = getattr(orchestrator, "event_bus", None) if orchestrator else None
    try:
        if bus:
            bus.publish_typed(AgentStatus(status="working", node="frontier_loop", task=task[:100]))
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

        tool_calls: list = []
        content_text: str = ""

        if pending_action is not None:
            tool_calls = [pending_action]
            pending_action = None
            history = _append_tool_call_history(history, tool_calls)
            logger.info(
                "frontier_loop_node: using precomputed next_action before LLM call"
            )
        else:
            # ---- Build prompt then call LLM --------------------------------
            messages = await _prepare_turn_messages(
                orchestrator=orchestrator,
                task=task,
                history=history,
                model_tier=model_tier,
                turns_taken=turns_taken,
            )

            try:
                response, content_text, tool_calls = await _call_llm_for_turn(
                    messages=messages,
                    provider_name=provider_name,
                    model_name=model_name,
                    orchestrator=orchestrator,
                    turns_taken=turns_taken,
                    model_tier=model_tier,
                )
            except Exception as exc:
                error_str = str(exc)
                logger.warning("frontier_loop_node: LLM call failed: %s", error_str)
                _notify_provider_limit(error_str)
                if _is_context_overflow(error_str):
                    errors = list(errors) + ["context_overflow"]
                break

            logger.info(
                "frontier_loop_node: turn %d response — content_len=%d, "
                "tool_calls_in_response=%r, finish_reason=%r",
                turns_taken,
                len(content_text),
                bool(
                    isinstance(response, dict)
                    and response.get("choices", [{}])[0]
                    .get("message", {})
                    .get("tool_calls")
                )
                if isinstance(response, dict) and response.get("choices")
                else "n/a",
                (
                    response.get("choices", [{}])[0].get("finish_reason")
                    if isinstance(response, dict) and response.get("choices")
                    else "n/a"
                ),
            )

            if _is_context_overflow(content_text):
                logger.warning("frontier_loop_node: context overflow in LLM response")
                errors = list(errors) + ["context_overflow"]
                break

            if kv_gov is not None and isinstance(response, dict):
                resp_total = int(response.get("total_tokens") or 0)
                if resp_total <= 0:
                    resp_total = int(response.get("prompt_tokens") or 0) + int(response.get("completion_tokens") or 0)
                if resp_total > 0:
                    kv_state = kv_gov.on_context_update(resp_total)
                    if kv_state.action in (CompactionAction.COMPACT, CompactionAction.FORCE_COMPACT):
                        logger.warning(
                            "frontier_loop_node: KV Cache %s at %d tokens (%.0f%% of %d)",
                            kv_state.action.value,
                            resp_total,
                            kv_state.usage_ratio * 100,
                            kv_gov.max_tokens,
                        )
                        errors = list(errors) + ["context_overflow"]
                        break

            # Append assistant message
            history = list(history)
            history.append({"role": "assistant", "content": content_text or ""})

        # ---- No tool calls → task done -----------------------------------
        if not tool_calls:
            last_result = {
                "ok": True,
                "completed_without_tool": True,
                "_completion_detected": True,
            }
            content_preview = (content_text or "")[:200]
            logger.info(
                "frontier_loop_node: no tool calls in turn %d — task complete. "
                "Content preview: %r",
                turns_taken,
                content_preview,
            )
            break

        # ---- Plan-mode gate ----------------------------------------------
        for tc in tool_calls:
            tool_name, tool_args = _normalize_tool_call(tc)
            if tool_name and orchestrator and _plan_mode_blocks(orchestrator, tool_name):
                logger.info(
                    "frontier_loop_node: plan-mode blocked '%s' — suspending for approval",
                    tool_name,
                )
                return {
                    "history": history,
                    "tool_call_count": tool_call_count,
                    "last_result": last_result,
                    "errors": errors,
                    "_frontier_loop_turns": turns_taken,
                    "awaiting_plan_approval": True,
                    "next_action": {"name": tool_name, "arguments": tool_args},
                }

        # ---- Execute tool calls ------------------------------------------
        history, tool_call_count, batch_last_result = await _dispatch_tool_calls(
            tool_calls=tool_calls,
            history=history,
            state=state,
            orchestrator=orchestrator,
            model_tier=model_tier,
            bus=bus,
            turns_taken=turns_taken,
            tool_call_count=tool_call_count,
            max_tool_calls=max_tool_calls,
        )
        if batch_last_result is not None:
            last_result = batch_last_result

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
            bus.publish_typed(AgentStatus(status="idle", node="frontier_loop", turns=turns_taken, tool_calls=tool_call_count))
    except Exception:
        pass

    result: Dict[str, Any] = {
        "history": history,
        "tool_call_count": tool_call_count,
        "last_result": last_result,
        "errors": errors,
        "_frontier_loop_turns": turns_taken,
        "awaiting_plan_approval": False,
    }
    if "context_overflow" in errors:
        result["_budget_compaction"] = True
        result["_should_distill"] = True
        if last_result is None or last_result.get("ok") is not False:
            result["last_result"] = {"ok": False, "error": "Context window overflow — compaction triggered"}
    return result
