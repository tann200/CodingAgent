import concurrent.futures
import json
import logging
import traceback
import re
import tempfile
import os
import shutil
from typing import Any, Dict, List, Optional
from pathlib import Path

# ruff: noqa: E501

logger = logging.getLogger(__name__)

# HR-1 fix: module-level singleton executor so _call_llm_sync does not create a new
# ThreadPoolExecutor per invocation.  Creating one per call means that on LLM timeout
# the worker thread is leaked (asyncio.run() cannot be preempted, and future.cancel()
# has no effect on an already-started task).  A persistent singleton avoids the
# thread-creation overhead and keeps the leaked-thread count bounded to 1.
_DISTILLER_LLM_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _get_distiller_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _DISTILLER_LLM_EXECUTOR
    if _DISTILLER_LLM_EXECUTOR is None:
        _DISTILLER_LLM_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="distiller_llm"
        )
    return _DISTILLER_LLM_EXECUTOR


# TASK-07: Number of recent messages to keep after compaction so the agent
# retains immediate context alongside the summary.
_KEEP_RECENT = 6


def _estimate_tokens(messages: List[Dict[str, str]]) -> int:
    """Token estimate using tiktoken when available, else char heuristic.

    D-06/S0-A: delegates to ``count_messages_tokens`` for accurate counting.
    """
    try:
        from src.core.inference.tokenizer import count_messages_tokens

        return count_messages_tokens(messages)
    except Exception:
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return max(1, int(total_chars / 3.5))


def _call_llm_sync(messages: list, format_json: bool = False, **kwargs) -> str:
    """Shared helper: call the LLM synchronously and return content string.

    C9 fix: asyncio.run() cannot be called when another event loop is already
    running (e.g. when distill_context is invoked from an async node via
    asyncio.gather).  When a running loop is detected we submit to the
    module-level singleton executor whose worker thread has no event loop,
    so asyncio.run() works correctly inside that isolated thread.
    HR-1 fix: uses _get_distiller_executor() (singleton) instead of creating
    a fresh executor per call.

    SM-1: If ``model`` is not already in *kwargs*, attempt to inject the
    configured ``small_model`` from config so background tasks (compaction,
    distillation, title generation) use a lightweight model rather than
    burning the main frontier model's token budget.
    """
    import asyncio
    import inspect
    from src.core.inference.llm_manager import call_model

    # SM-1: inject small_model when not explicitly overridden by caller
    if "model" not in kwargs:
        try:
            from src.core.config_loader import get_small_model as _gsm

            _sm = _gsm()
            if _sm:
                kwargs["model"] = _sm
        except Exception:
            pass

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
            # Submit to the module-level singleton executor whose thread has no
            # event loop, so asyncio.run() is safe there.
            # HR-1 fix: reuse the singleton instead of creating a per-call pool so
            # timeout-leaked threads stay bounded to the one persistent worker.
            _pool = _get_distiller_executor()
            # Ensure ContextVars are propagated into the distiller executor thread.
            import contextvars as _cv

            _ctx = _cv.copy_context()
            future = _pool.submit(_ctx.run, asyncio.run, candidate)
            try:
                resp = future.result(timeout=120)
            except Exception as thread_err:
                logger.error(f"_call_llm_sync: thread executor failed: {thread_err}")
                future.cancel()
                return ""
        except RuntimeError:
            # No running loop — safe to call asyncio.run() directly.
            resp = asyncio.run(candidate)
    else:
        resp = candidate

    content = ""
    if isinstance(resp, dict):
        _choices = resp.get("choices")
        if _choices and isinstance(_choices, list) and len(_choices) > 0:
            _msg = (
                _choices[0].get("message", {}) if isinstance(_choices[0], dict) else {}
            )
            content = _msg.get("content", "") if isinstance(_msg, dict) else ""
        elif resp.get("message"):
            _msg = resp.get("message", {})
            content = _msg.get("content", "") if isinstance(_msg, dict) else ""

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

    # OP-2: Use a structured prompt so the summary is machine-parseable and
    # mirrors the Goal/Instructions/Discoveries/Accomplished/Relevant-files
    # schema used by OpenCode's compact.rs — this makes it easier for the agent
    # to resume mid-task without losing orientation.
    prompt = (
        "You are a coding-session historian. Summarize the conversation excerpt "
        "below for a coding AI agent.\n\n"
        "The summary REPLACES these messages in the agent's conversation history. "
        "The agent must be able to continue working from your summary alone.\n\n"
        "Use EXACTLY this structure (include every section even if empty):\n\n"
        "## Goal\n"
        "One sentence — what the user asked for.\n\n"
        "## Instructions\n"
        "Constraints, rules, and conventions established during this session.\n\n"
        "## Discoveries\n"
        "What was learned: file locations, API shapes, error root causes, test "
        "results, key symbols found.\n\n"
        "## Accomplished\n"
        "Completed steps with exact file paths and the changes made to each.\n\n"
        "## Relevant Files\n"
        "Exact paths of every file read, created, modified, or deleted.\n\n"
        "## Current State\n"
        "What is done, what is still in progress, and the immediate next step.\n\n"
        "Rules: plain prose (no JSON), max 700 words total, preserve exact file "
        "paths, include critical code snippets where essential.\n\n"
        f"Conversation:\n\n{transcript}\n\nWrite the structured summary now:"
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

    # TASK-07: Replace message-count trigger (≥50) with token-estimate trigger.
    # Read threshold from config; fall back to 6000 estimated tokens.
    _compact_token_threshold = 6000
    try:
        from src.core.config_loader import get as _cfg_get

        _compact_token_threshold = int(
            _cfg_get("compact_token_threshold", 6000) or 6000
        )
    except Exception:
        pass

    _estimated_tokens = _estimate_tokens(messages)
    _compacted_history: Optional[List[Dict[str, str]]] = None
    if _estimated_tokens >= _compact_token_threshold:
        logger.info(
            "distill_context: estimated %d tokens >= threshold %d, triggering compaction "
            "(was: %d messages)",
            _estimated_tokens,
            _compact_token_threshold,
            len(messages),
        )
        try:
            summary = compact_messages_to_prose(messages, working_dir=working_dir)
            if summary:
                # Write a compaction checkpoint if we have a working dir
                if working_dir:
                    try:
                        from src.tools.tools_config import agent_context_path

                        agent_context = agent_context_path(working_dir)
                    except Exception:
                        agent_context = working_dir / ".agent-context"

                    cp_path = agent_context / "compaction_checkpoint.md"
                    try:
                        cp_path.parent.mkdir(parents=True, exist_ok=True)
                        # Use mkstemp -> replace to avoid partial files for .md
                        fd, tmp_path = tempfile.mkstemp(
                            dir=str(cp_path.parent), suffix=".tmp"
                        )
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as f:
                                f.write(summary)
                            try:
                                os.replace(tmp_path, str(cp_path))
                            except Exception:
                                shutil.move(tmp_path, str(cp_path))
                        except Exception as _we:
                            logger.warning(
                                f"distill_context: failed to write checkpoint: {_we}"
                            )
                            try:
                                if os.path.exists(tmp_path):
                                    os.unlink(tmp_path)
                            except Exception:
                                pass
                        else:
                            logger.info(
                                f"distill_context: compaction checkpoint written to {cp_path}"
                            )
                    except Exception as _we:
                        logger.warning(
                            f"distill_context: failed to write checkpoint: {_we}"
                        )

                # TASK-08: Compact summary as System message + keep recent msgs
                # + append continuation signal.
                # Pattern from claw's compact.rs: system summary first, then
                # recent messages so the agent retains immediate context.
                _recent = (
                    messages[-_KEEP_RECENT:] if len(messages) > _KEEP_RECENT else []
                )
                # OP-8: Prefix the compacted message with [COMPACTED] so the TUI
                # and log consumers can identify it as a synthetic summary rather
                # than an original conversation turn.
                _compacted_history = (
                    [
                        {
                            "role": "system",
                            "content": "[COMPACTED] <summary>\n"
                            + summary
                            + "\n</summary>",
                        }
                    ]
                    + _recent
                    + [
                        {
                            "role": "user",
                            "content": (
                                "Continue from the session summary above. "
                                "What is the current task?"
                            ),
                        }
                    ]
                )
                logger.info(
                    "distill_context: compaction reduced %d msgs → %d msg "
                    "(1 system summary + %d recent + 1 continuation)",
                    len(messages),
                    len(_compacted_history),
                    len(_recent),
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
            try:
                from src.tools.tools_config import agent_context_path

                agent_context = agent_context_path(working_dir)
            except Exception:
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
                    # Use lock-aware loader when available to avoid races with writers
                    try:
                        from src.tools.todo_tools import _load_todo_json

                        todo_steps = _load_todo_json(str(agent_context.parent))
                    except Exception:
                        todo_steps = json.loads(todo_json_path.read_text())

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

            # Write TASK_STATE.md atomically using mkstemp -> replace
            task_state_path.parent.mkdir(parents=True, exist_ok=True)
            content_text = "\n".join(lines)
            fd = None
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(task_state_path.parent), suffix=".tmp"
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content_text)
                try:
                    os.replace(tmp_path, str(task_state_path))
                except Exception:
                    shutil.move(tmp_path, str(task_state_path))
            except Exception:
                logger.exception("Failed to write TASK_STATE.md to %s", task_state_path)
                try:
                    if fd is not None:
                        os.close(fd)
                except Exception:
                    pass
                try:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to write TASK_STATE.md: {e}")

    # Also attempt to produce a lightweight repo_memory.json summarizing modules if repo_index is available
    try:
        if working_dir:
            try:
                from src.tools.tools_config import agent_context_path

                _agent_ctx = agent_context_path(working_dir)
            except Exception:
                _agent_ctx = working_dir / ".agent-context"

            index_path = _agent_ctx / "repo_index.json"
            if index_path.exists():
                with open(index_path, "r", encoding="utf-8") as f:
                    repo_index = json.load(f)
                repo_memory = {"modules": []}
                for fdata in repo_index.get("files", []):
                    repo_memory["modules"].append(
                        {"path": fdata.get("path"), "imports": fdata.get("imports", [])}
                    )
                mem_path = _agent_ctx / "repo_memory.json"
                mem_path.parent.mkdir(parents=True, exist_ok=True)
                # Prefer atomic_write_json when available
                try:
                    from src.core.io_utils import atomic_write_json

                    logger.debug(
                        "distill_context: attempting atomic_write_json for %s", mem_path
                    )
                    ok = atomic_write_json(mem_path, repo_memory, logger=logger)
                    if not ok:
                        logger.warning(
                            "distill_context: atomic_write_json returned False for %s; falling back to mkstemp",
                            mem_path,
                        )
                        fd, tmp_path = tempfile.mkstemp(
                            dir=str(mem_path.parent), suffix=".tmp"
                        )
                        try:
                            with os.fdopen(fd, "w", encoding="utf-8") as f:
                                json.dump(repo_memory, f, indent=2)
                            try:
                                os.replace(tmp_path, str(mem_path))
                            except Exception:
                                shutil.move(tmp_path, str(mem_path))
                        except Exception:
                            logger.exception(
                                "Failed fallback write of repo_memory.json to %s",
                                mem_path,
                            )
                except Exception:
                    logger.debug(
                        "distill_context: atomic_write_json unavailable or failed for %s; falling back\n%s",
                        mem_path,
                        traceback.format_exc(),
                    )
                    fd, tmp_path = tempfile.mkstemp(
                        dir=str(mem_path.parent), suffix=".tmp"
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(repo_memory, f, indent=2)
                        try:
                            os.replace(tmp_path, str(mem_path))
                        except Exception:
                            shutil.move(tmp_path, str(mem_path))
                    except Exception:
                        logger.exception(
                            "Failed to write repo_memory.json to %s", mem_path
                        )

                # Build a lightweight file summary cache for large files to speed prompt building
                try:
                    summary_path = _agent_ctx / "file_summaries.json"
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
                        summary_path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            from src.core.io_utils import atomic_write_json

                            logger.debug(
                                "distill_context: attempting atomic_write_json for %s",
                                summary_path,
                            )
                            ok = atomic_write_json(
                                summary_path, summaries, logger=logger
                            )
                            if not ok:
                                logger.warning(
                                    "distill_context: atomic_write_json returned False for %s; falling back to mkstemp",
                                    summary_path,
                                )
                                fd, tmp_path = tempfile.mkstemp(
                                    dir=str(summary_path.parent), suffix=".tmp"
                                )
                                try:
                                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                                        json.dump(summaries, f, indent=2)
                                    try:
                                        os.replace(tmp_path, str(summary_path))
                                    except Exception:
                                        shutil.move(tmp_path, str(summary_path))
                                except Exception:
                                    pass
                        except Exception:
                            logger.debug(
                                "distill_context: atomic_write_json unavailable or failed for %s; falling back\n%s",
                                summary_path,
                                traceback.format_exc(),
                            )
                            fd, tmp_path = tempfile.mkstemp(
                                dir=str(summary_path.parent), suffix=".tmp"
                            )
                            try:
                                with os.fdopen(fd, "w", encoding="utf-8") as f:
                                    json.dump(summaries, f, indent=2)
                                try:
                                    os.replace(tmp_path, str(summary_path))
                                except Exception:
                                    shutil.move(tmp_path, str(summary_path))
                            except Exception:
                                pass
                    except Exception:
                        pass
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Failed to write repo_memory.json: {e}")

    # P3-7: Persist distilled summary to VectorStore for semantic recall across sessions.
    if distilled_state and working_dir:
        try:
            from src.core.indexing.vector_store import VectorStore

            _vs = VectorStore(workdir=str(working_dir))
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


# ---------------------------------------------------------------------------
# ORCH-W5: Internal utility agent calls
# ---------------------------------------------------------------------------


def call_internal_agent(
    agent_id: str,
    messages: List[Dict[str, str]],
    *,
    max_tokens: int = 256,
) -> str:
    """Make a one-shot LLM call using an internal AgentDefinition.

    Internal agents (mode="internal") are never shown to users and execute
    without a tool loop.  The agent's ``prompt_override`` is prepended as a
    system message.  Returns the model's text response, or "" on error.

    Args:
        agent_id: Registry ID of an internal AgentDefinition ("title", "compaction").
        messages: Conversation messages to send (without system message — that
                  comes from the agent's prompt_override).
        max_tokens: Max response tokens.

    Returns:
        The model's response text, stripped of whitespace.
    """
    try:
        from src.core.orchestration.agent_types import get_agent_registry

        agent = get_agent_registry().get(agent_id)
        if agent is None:
            logger.warning(
                "call_internal_agent: agent %r not found in registry", agent_id
            )
            return ""
        if agent.mode != "internal":
            logger.warning(
                "call_internal_agent: agent %r has mode=%r, expected 'internal'",
                agent_id,
                agent.mode,
            )
            return ""
        system_msg: str = agent.prompt_override or ""
        full_messages: List[Dict[str, str]] = []
        if system_msg:
            full_messages.append({"role": "system", "content": system_msg})
        full_messages.extend(messages)
        return _call_llm_sync(full_messages, max_new_tokens=max_tokens)
    except Exception as exc:
        logger.error("call_internal_agent(%r) failed: %s", agent_id, exc)
        return ""


def generate_session_title(first_user_message: str) -> str:
    """Generate a short session title from the user's first message.

    Uses the "title" internal agent (ORCH-W5).  Falls back to the first
    12 words of the message if the LLM call fails or returns empty.

    Args:
        first_user_message: The user's opening message for the session.

    Returns:
        A concise 3-7 word title string.
    """
    if not first_user_message or not first_user_message.strip():
        return "New session"
    messages = [{"role": "user", "content": first_user_message[:800]}]
    title = call_internal_agent("title", messages, max_tokens=32)
    if title:
        # Trim to at most 80 chars to prevent runaway output from noisy models
        return title[:80].strip()
    # Fallback: first N words of the task
    words = first_user_message.split()
    return " ".join(words[:10]).rstrip(".,;:!?")[:80]
