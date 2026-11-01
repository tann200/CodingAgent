"""Compaction Node for the unified 6-node 2026 LangGraph cognitive pipeline.

Performs TurnGroup-atomic 2-stage deterministic history reduction:
- Groups [assistant(tool_calls), tool, tool, ...] into indivisible TurnGroups so
  compaction NEVER orphans a role="tool" message or leaves a dangling tool_call_id.
- Stage A: Replaces stale large role="tool" payloads in older TurnGroups with
  compact provenance stubs while preserving tool_call_id 1:1.
- Stage B: Folds older middle TurnGroups into a structured <context_checkpoint>
  summary when message count exceeds the window threshold.
- Monotonically bounded by policy.compaction_limit (0 for nano, 1 for compact, 2 for standard/frontier).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping

from langchain_core.runnables import RunnableConfig

from src.core.orchestration.graph.nodes.node_utils import span_node as _span_node
from src.core.orchestration.graph.nodes.tool_output_truncation import prune_tool_outputs
from src.core.orchestration.graph.state import StateLike, replace_state_list

logger = logging.getLogger(__name__)

_STAGE_A_PRESERVE_RECENT_GROUPS = 3
_STAGE_A_TOOL_CHAR_CAP = 360
_STAGE_B_MESSAGE_THRESHOLD = 30
_STAGE_B_TAIL_MESSAGE_BUDGET = 18


def group_atomic_turns(history: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Partition history into indivisible TurnGroups.

    An assistant message with native ``tool_calls`` and all immediately following
    ``role == "tool"`` messages form a single TurnGroup. Legacy assistant +
    ``tool_execution_result`` user pairs are also grouped together. Orphan leading
    ``role == "tool"`` messages without a preceding ``assistant.tool_calls`` are
    dropped so provider APIs never fail validation.
    """
    groups: List[List[Dict[str, Any]]] = []
    idx = 0
    n = len(history)
    while idx < n:
        raw = history[idx]
        if not isinstance(raw, Mapping):
            idx += 1
            continue
        msg = dict(raw)
        role = msg.get("role")

        if role == "tool":
            # Orphan tool message with no preceding assistant.tool_calls — drop safely
            logger.debug("compact_node: dropping orphan role='tool' message at index %d", idx)
            idx += 1
            continue

        if role == "assistant" and msg.get("tool_calls"):
            group = [msg]
            idx += 1
            while idx < n and isinstance(history[idx], Mapping) and history[idx].get("role") == "tool":
                group.append(dict(history[idx]))
                idx += 1
            groups.append(group)
            continue

        if (
            role == "assistant"
            and idx + 1 < n
            and isinstance(history[idx + 1], Mapping)
            and history[idx + 1].get("role") == "user"
            and "tool_execution_result" in str(history[idx + 1].get("content") or "")
        ):
            groups.append([msg, dict(history[idx + 1])])
            idx += 2
            continue

        groups.append([msg])
        idx += 1
    return groups


def _stub_stale_tool_outputs(groups: List[List[Dict[str, Any]]]) -> List[List[Dict[str, Any]]]:
    """Stage A: Replace large role='tool' payloads in older TurnGroups with compact stubs."""
    if len(groups) <= _STAGE_A_PRESERVE_RECENT_GROUPS:
        return groups

    cutoff = len(groups) - _STAGE_A_PRESERVE_RECENT_GROUPS
    out: List[List[Dict[str, Any]]] = []
    for g_idx, group in enumerate(groups):
        if g_idx >= cutoff:
            out.append(group)
            continue
        new_group: List[Dict[str, Any]] = []
        for msg in group:
            if msg.get("role") == "tool":
                content = str(msg.get("content") or "")
                if len(content) > _STAGE_A_TOOL_CHAR_CAP and '"compacted": true' not in content.lower():
                    tool_name = str(msg.get("name") or "tool")
                    stub = json.dumps(
                        {
                            "compacted": True,
                            "tool": tool_name,
                            "original_chars": len(content),
                            "preview": content[:140],
                            "hint": "Output compacted; re-read target slice if needed.",
                        },
                        ensure_ascii=False,
                    )
                    stubbed_msg = dict(msg)
                    stubbed_msg["content"] = stub
                    new_group.append(stubbed_msg)
                    continue
            new_group.append(msg)
        out.append(new_group)
    return out


def _build_checkpoint_summary(
    folded_groups: List[List[Dict[str, Any]]],
    state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build a structured <context_checkpoint> system summary for folded TurnGroups."""
    tools_used: List[str] = []
    folded_msg_count = 0
    for grp in folded_groups:
        folded_msg_count += len(grp)
        for m in grp:
            if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
                for tc in m["tool_calls"]:
                    if isinstance(tc, Mapping):
                        fn = tc.get("function") if isinstance(tc.get("function"), Mapping) else tc
                        tname = str((fn or {}).get("name") or "")
                        if tname and tname not in tools_used:
                            tools_used.append(tname)
            elif m.get("role") == "tool" and m.get("name"):
                tname = str(m["name"])
                if tname not in tools_used:
                    tools_used.append(tname)

    task = str(state.get("task") or "").strip()
    mod_files = list(state.get("modified_files") or [])
    lines = [
        "<context_checkpoint>",
        f"Compacted {len(folded_groups)} older turn groups ({folded_msg_count} messages).",
    ]
    if task:
        lines.append(f"Active goal: {task[:240]}")
    if mod_files:
        lines.append(f"Modified files so far: {', '.join(str(f) for f in mod_files[:10])}")
    if tools_used:
        lines.append(f"Tools executed in compacted turns: {', '.join(tools_used[:12])}")
    lines.append("</context_checkpoint>")
    return {"role": "system", "content": "\n".join(lines)}


def compact_history_atomic(
    history: List[Dict[str, Any]],
    state: Mapping[str, Any] | None = None,
    max_messages: int = _STAGE_B_MESSAGE_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Run deterministic 2-stage TurnGroup-atomic compaction on history."""
    state_map: Mapping[str, Any] = state or {}
    pruned = prune_tool_outputs(list(history))
    groups = group_atomic_turns(pruned)
    groups = _stub_stale_tool_outputs(groups)

    flat = [m for grp in groups for m in grp]
    if len(flat) <= max_messages or len(groups) <= 3:
        return flat

    # Stage B: Preserve initial anchor group(s) (up to 2 single-message non-tool anchors)
    head_groups: List[List[Dict[str, Any]]] = []
    head_idx = 0
    while (
        head_idx < min(2, len(groups) - 2)
        and len(groups[head_idx]) == 1
        and groups[head_idx][0].get("role") in ("system", "user")
    ):
        head_groups.append(groups[head_idx])
        head_idx += 1

    # Select whole TurnGroups from the tail up to _STAGE_B_TAIL_MESSAGE_BUDGET
    tail_groups_rev: List[List[Dict[str, Any]]] = []
    tail_msgs = 0
    tail_idx = len(groups) - 1
    while tail_idx >= head_idx:
        grp = groups[tail_idx]
        if tail_groups_rev and (tail_msgs + len(grp) > _STAGE_B_TAIL_MESSAGE_BUDGET):
            break
        tail_groups_rev.append(grp)
        tail_msgs += len(grp)
        tail_idx -= 1
    tail_groups = list(reversed(tail_groups_rev))

    folded_groups = groups[head_idx : len(groups) - len(tail_groups)]
    if not folded_groups:
        return flat

    checkpoint_msg = _build_checkpoint_summary(folded_groups, state_map)
    compacted: List[Dict[str, Any]] = []
    for grp in head_groups:
        compacted.extend(grp)
    compacted.append(checkpoint_msg)
    for grp in tail_groups:
        compacted.extend(grp)
    return compacted


async def compact_node(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """Thin OTel-span wrapper — delegates to _compact_node_impl."""
    with _span_node("compact", {"turn": state.get("turn_count", 0)}):
        return await _compact_node_impl(state, config)


async def _compact_node_impl(state: StateLike, config: RunnableConfig) -> Dict[str, Any]:
    """TurnGroup-atomic deterministic history reduction."""
    logger.info("=== compact_node START ===")
    run_state = dict(state.get("run") or {})
    runtime_state = dict(state.get("runtime") or {})
    policy = dict(runtime_state.get("policy") or {})

    raw_limit = policy.get("compaction_limit") if "compaction_limit" in policy else None
    compaction_limit = int(raw_limit) if raw_limit is not None else 1
    compactions_taken = int(run_state.get("compactions") or 0) + 1
    run_state["compactions"] = compactions_taken

    if compactions_taken > compaction_limit:
        logger.warning(
            "compact_node: compaction limit exceeded (%d > %d) — routing to finalize",
            compactions_taken,
            compaction_limit,
        )
        run_state["status"] = "failed"
        run_state["outcome"] = "failed"
        errors = list(state.get("errors") or [])
        errors.append("context_overflow_exhausted")
        return {
            "run": run_state,
            "errors": errors,
            "_budget_compaction": False,
        }

    history = list(state.get("history") or [])
    initial_len = len(history)
    compacted = compact_history_atomic(history, state=state)

    logger.info(
        "compact_node: compaction %d/%d complete (messages: %d -> %d)",
        compactions_taken,
        compaction_limit,
        initial_len,
        len(compacted),
    )
    run_state["status"] = "running"
    run_state["outcome"] = "continue"

    errors = [e for e in (state.get("errors") or []) if e != "context_overflow"]

    return {
        "run": run_state,
        "history": replace_state_list(compacted),
        "errors": errors,
        "_budget_compaction": False,
        "_compacted_history": compacted,
    }
