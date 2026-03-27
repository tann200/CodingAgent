import json
import logging
from typing import Any, Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


def _call_llm_sync(messages: list, format_json: bool = False, **kwargs) -> str:
    """Shared helper: call the LLM synchronously and return content string.

    C9 fix: asyncio.run() cannot be called when another event loop is already
    running (e.g. when distill_context is invoked from an async node via
    asyncio.gather).  When a running loop is detected we spin up a fresh
    ThreadPoolExecutor thread — that thread has no event loop, so asyncio.run()
    works correctly and the coroutine gets its own isolated loop.
    """
    import asyncio
    import inspect
    import concurrent.futures
    from src.core.inference.llm_manager import call_model

    try:
        candidate = call_model(
            messages=messages,
            format_json=format_json,
            stream=False,
            tools=None,
            **kwargs,
        )
    except Exception as e:
        logger.error(f"_call_llm_sync: call_model raised: {e}")
        return ""

    if inspect.isawaitable(candidate):
        try:
            asyncio.get_running_loop()
            # Running loop detected — must NOT call asyncio.run() here (C9).
            # Submit to a new thread that has no event loop so asyncio.run() is safe.
            # SCAN-6 fix: cancel the future BEFORE the pool's context manager
            # calls shutdown(wait=True).  Without this, a TimeoutError from
            # future.result() is caught and "" is returned, but then __exit__
            # blocks indefinitely waiting for the background asyncio.run() thread
            # to finish its LLM call.
            _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                future = _pool.submit(asyncio.run, candidate)
                try:
                    resp = future.result(timeout=120)
                except Exception as thread_err:
                    logger.error(
                        f"_call_llm_sync: thread executor failed: {thread_err}"
                    )
                    future.cancel()
                    return ""
            finally:
                _pool.shutdown(wait=False)
        except RuntimeError:
            # No running loop — safe to call asyncio.run() directly.
            resp = asyncio.run(candidate)
    else:
        resp = candidate

    content = ""
    if isinstance(resp, dict):
        if resp.get("choices") and isinstance(resp.get("choices"), list):
            content = resp["choices"][0].get("message", {}).get("content", "") or ""
        elif resp.get("message"):
            content = resp.get("message", {}).get("content", "") or ""

    # Part A: strip <think>...</think> blocks produced by reasoning models
    # (Qwen3, DeepSeek-R1-Distill, QwQ).  Safe no-op for all other models.
    if content:
        from src.core.inference.thinking_utils import strip_thinking

        content = strip_thinking(content)
    return content


def compact_messages_to_prose(
    messages: List[Dict[str, str]],
    working_dir: Optional[Path] = None,
) -> str:
    """
    Generate a rich prose summary of *messages* for inline injection into
    conversation history.  The returned string replaces the dropped messages
    so the agent can continue working without losing prior context.

    This mirrors the compaction approach used by Claude Code / OpenCode /
    Kilocode: the summary is inserted as a conversation turn, not just
    written to a file.
    """
    if not messages:
        return ""

    # Build a readable transcript from the messages to summarize
    parts: List[str] = []
    for m in messages:
        role = m.get("role", "unknown").upper()
        content = str(m.get("content", ""))[:3000]
        parts.append(f"[{role}]: {content}")
    transcript = "\n\n".join(parts)

    prompt = (
        "You are a coding-session historian. Summarize the conversation excerpt "
        "below for a coding AI agent.\n\n"
        "The summary REPLACES these messages in the agent's conversation history. "
        "The agent must be able to continue working from your summary alone.\n\n"
        "Include ALL sections that are relevant:\n"
        "- **Task**: What the user requested\n"
        "- **Files Touched**: Files read, created, modified, or deleted (exact paths)\n"
        "- **Actions Taken**: Key steps and tool calls executed\n"
        "- **Errors & Fixes**: Errors encountered and how they were resolved\n"
        "- **Current State**: What is complete, what is still in progress\n\n"
        "Rules: plain prose (no JSON), max 600 words, preserve exact file paths, "
        "include critical code snippets if needed.\n\n"
        f"Conversation:\n\n{transcript}\n\nWrite the summary now:"
    )

    try:
        content = _call_llm_sync([{"role": "user", "content": prompt}])
        if content:
            logger.info(
                f"compact_messages_to_prose: {len(content)} chars for {len(messages)} msgs"
            )
            return content.strip()
    except Exception as e:
        logger.error(f"compact_messages_to_prose failed: {e}")

    return _fallback_compact(messages)


def _fallback_compact(messages: List[Dict]) -> str:
    """Simple text dump used when LLM summarization fails."""
    lines = [
        f"[CONTEXT COMPACTED — {len(messages)} messages summarized, LLM unavailable]",
        "",
    ]
    for m in messages[-8:]:
        role = m.get("role", "?")
        content = str(m.get("content", ""))[:300]
        lines.append(f"[{role}]: {content}")
    return "\n".join(lines)


def distill_context(
    messages: List[Dict[str, str]],
    max_summary_tokens: int = 512,
    llm_client: Any = None,
    working_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Distill conversation history into a structured session summary.

    Returns a dict with keys: current_task, completed_steps, next_step,
    files_modified, errors_resolved, current_state.
    Also writes the result to TASK_STATE.md in the working directory.
    """
    logger.info(f"distill_context called with {len(messages)} messages")
    if not messages:
        return {}

    # P2-3 / HR-2 fix: When conversation exceeds 50 messages, compact the history
    # inline so the actual context window is reduced — not just written to a file.
    # The compacted list is stored in distill_context's return value under the key
    # "_compacted_history" so the caller (memory_update_node) can replace state["history"].
    _compacted_history: Optional[List[Dict[str, str]]] = None
    if len(messages) >= 50:
        logger.info(
            f"distill_context: {len(messages)} messages >= 50, triggering compaction"
        )
        try:
            summary = compact_messages_to_prose(messages, working_dir=working_dir)
            if summary:
                if working_dir:
                    cp_path = (
                        working_dir / ".agent-context" / "compaction_checkpoint.md"
                    )
                    try:
                        cp_path.parent.mkdir(parents=True, exist_ok=True)
                        cp_path.write_text(summary, encoding="utf-8")
                        logger.info(
                            f"distill_context: compaction checkpoint written to {cp_path}"
                        )
                    except Exception as _we:
                        logger.warning(
                            f"distill_context: failed to write checkpoint: {_we}"
                        )
                # HR-2 fix: return the compacted message list so the caller can
                # replace state["history"] and actually reduce context window size.
                _compacted_history = [
                    {"role": "system", "content": "Session Summary:\n" + summary},
                ]
                logger.info(
                    f"distill_context: compaction reduced {len(messages)} msgs → "
                    f"{len(_compacted_history)} msg"
                )
        except Exception as _ce:
            logger.warning(f"distill_context: compaction failed: {_ce}")

    safe_msgs = []
    # HR-14 fix: process more messages to avoid missing the original task statement.
    # Use min(50, len(messages)) to include early messages that may contain the task.
    msg_window = min(len(messages), 50)
    for m in messages[-msg_window:]:
        # Increase truncation limit for error messages that may contain critical details
        limit = (
            3000
            if m.get("role") in ("tool", "user")
            and "error" in str(m.get("content", "")).lower()
            else 500
        )
        safe_msgs.append(
            {
                "role": m.get("role", "unknown"),
                "content": str(m.get("content", ""))[:limit],
            }
        )
    msg_str = json.dumps(safe_msgs, indent=2)

    prompt = (
        "System: You are a concise task state tracker. "
        "Your ONLY output must be valid JSON — no markdown, no code blocks, "
        "no explanation, no thinking tags. RESPOND IN ENGLISH ONLY.\n\n"
        "Output format:\n"
        "{\n"
        '  "current_task": "brief description of current task",\n'
        '  "current_state": "one sentence on where we are in the task",\n'
        '  "files_modified": ["path/to/file.py", "other/file.ts"],\n'
        '  "completed_steps": ["step 1", "step 2"],\n'
        '  "errors_resolved": ["brief error and fix description"],\n'
        '  "next_step": "what comes next"\n'
        "}\n\n"
        "Keep each string under 15 words. Use relative file paths.\n\n"
        f"User: Here are the recent messages:\n{msg_str}\n\n"
        "OUTPUT ONLY VALID JSON. NO MARKDOWN. NO EXPLANATION. /no_think"
    )

    distilled_state: Dict[str, Any] = {}

    # Part B: reasoning models (DeepSeek-R1-Distill) cannot suppress thinking
    # tokens, so they consume max_tokens budget before the real answer starts.
    # Double the allocation for those models; base budget is sufficient for
    # Qwen3 (where /no_think works) and all non-thinking models.
    from src.core.inference.thinking_utils import budget_max_tokens, get_active_model_id

    _model_id = get_active_model_id()
    _max_tok = budget_max_tokens(400, _model_id)

    try:
        content = _call_llm_sync(
            [{"role": "user", "content": prompt}], format_json=True, max_tokens=_max_tok
        )
        if content:
            import re

            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                distilled_state = json.loads(match.group(0))
            else:
                distilled_state = json.loads(content)

            # P2-4: Validate required keys to detect malformed LLM output early
            _REQUIRED_KEYS = {"current_task", "current_state", "next_step"}
            missing = _REQUIRED_KEYS - set(distilled_state.keys())
            if missing:
                logger.warning(
                    f"distill_context: output missing required keys: {missing}. "
                    "Falling back to empty state."
                )
                distilled_state = {}

            logger.info(f"Distillation result: {distilled_state}")
    except Exception as e:
        logger.error(f"Distillation failed: {e}")
        return {}

    if distilled_state and working_dir:
        try:
            agent_context = working_dir / ".agent-context"
            task_state_path = agent_context / "TASK_STATE.md"
            lines = [
                "# Current Task",
                distilled_state.get("current_task", "None"),
                "",
                "# Current State",
                distilled_state.get("current_state", "None"),
            ]
            files_modified = distilled_state.get("files_modified", [])
            if files_modified:
                lines.extend(["", "# Files Modified"])
                for f in files_modified:
                    lines.append(f"- {f}")

            # Prefer TODO.json for step completion — it is deterministic and exact.
            # Fall back to LLM-inferred completed_steps only when no TODO exists.
            todo_json_path = agent_context / "todo.json"
            if todo_json_path.exists():
                try:
                    import json as _json

                    todo_steps = _json.loads(todo_json_path.read_text())
                    done_steps = [s["description"] for s in todo_steps if s.get("done")]
                    pending_steps = [
                        s["description"] for s in todo_steps if not s.get("done")
                    ]
                    lines.extend(["", "# Completed Steps (from TODO)"])
                    for step in done_steps:
                        lines.append(f"- [x] {step}")
                    if pending_steps:
                        lines.extend(["", "# Pending Steps"])
                        for step in pending_steps:
                            lines.append(f"- [ ] {step}")
                        lines.extend(["", "# Next Step", pending_steps[0]])
                    else:
                        lines.extend(["", "# Next Step", "All steps complete"])
                except Exception:
                    # Fallback to LLM-inferred steps if todo.json is unreadable
                    lines.extend(["", "# Completed Steps"])
                    for step in distilled_state.get("completed_steps", []):
                        lines.append(f"- {step}")
                    lines.extend(
                        ["", "# Next Step", distilled_state.get("next_step", "None")]
                    )
            else:
                lines.extend(["", "# Completed Steps"])
                for step in distilled_state.get("completed_steps", []):
                    lines.append(f"- {step}")
                lines.extend(
                    ["", "# Next Step", distilled_state.get("next_step", "None")]
                )

            errors = distilled_state.get("errors_resolved", [])
            if errors:
                lines.extend(["", "# Errors Resolved"])
                for err in errors:
                    lines.append(f"- {err}")

            task_state_path.write_text("\n".join(lines))
        except Exception as e:
            logger.error(f"Failed to write TASK_STATE.md: {e}")

    # Also attempt to produce a lightweight repo_memory.json summarizing modules if repo_index is available
    try:
        if working_dir:
            index_path = working_dir / ".agent-context" / "repo_index.json"
            if index_path.exists():
                with open(index_path, "r", encoding="utf-8") as f:
                    repo_index = json.load(f)
                repo_memory = {"modules": []}
                for fdata in repo_index.get("files", []):
                    repo_memory["modules"].append(
                        {"path": fdata.get("path"), "imports": fdata.get("imports", [])}
                    )
                mem_path = working_dir / ".agent-context" / "repo_memory.json"
                mem_path.write_text(json.dumps(repo_memory, indent=2))
                # Build a lightweight file summary cache for large files to speed prompt building
                try:
                    summary_path = (
                        working_dir / ".agent-context" / "file_summaries.json"
                    )
                    summaries = {}
                    for fdata in repo_index.get("files", []):
                        p = working_dir / fdata.get("path")
                        if p.exists() and p.is_file():
                            try:
                                text = p.read_text(encoding="utf-8")
                                lines = text.splitlines()
                                if len(lines) > 200:
                                    # keep head and tail
                                    summary = "\n".join(
                                        lines[:10] + ["[...skipped...]"] + lines[-10:]
                                    )
                                else:
                                    summary = "\n".join(lines[:200])
                                summaries[str(fdata.get("path"))] = summary
                            except Exception:
                                continue
                    try:
                        summary_path.write_text(
                            json.dumps(summaries, indent=2), encoding="utf-8"
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Failed to write repo_memory.json: {e}")

    # P3-7: Persist distilled summary to VectorStore for semantic recall across sessions.
    if distilled_state:
        try:
            from src.core.indexing.vector_store import VectorStore

            _vs = VectorStore(working_dir=working_dir)
            _summary_text = (
                f"Task: {distilled_state.get('current_task', '')}\n"
                f"State: {distilled_state.get('current_state', '')}\n"
                f"Next: {distilled_state.get('next_step', '')}"
            )
            _vs.add_memory(_summary_text, metadata=distilled_state)
            logger.info("distill_context: summary persisted to VectorStore")
        except Exception as _ve:
            logger.warning(
                f"distill_context: VectorStore persist failed (non-critical): {_ve}"
            )

    # HR-2 fix: include compacted history in return value so memory_update_node
    # can replace state["history"] and actually reduce the context window.
    if _compacted_history is not None:
        distilled_state["_compacted_history"] = _compacted_history

    return distilled_state
