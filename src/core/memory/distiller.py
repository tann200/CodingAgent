import json
import logging
from typing import Any, Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


def _call_llm_sync(messages: list, format_json: bool = False) -> str:
    """Shared helper: call the LLM synchronously and return content string."""
    import asyncio
    import inspect
    from src.core.inference.llm_manager import call_model

    try:
        candidate = call_model(
            messages=messages,
            format_json=format_json,
            stream=False,
            tools=None,
        )
    except Exception as e:
        logger.error(f"_call_llm_sync: call_model raised: {e}")
        return ""

    if inspect.isawaitable(candidate):
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, candidate)
                    resp = future.result()
            else:
                resp = asyncio.run(candidate)
        except RuntimeError:
            resp = asyncio.run(candidate)
    else:
        resp = candidate

    if isinstance(resp, dict):
        if resp.get("choices") and isinstance(resp.get("choices"), list):
            return resp["choices"][0].get("message", {}).get("content", "")
        elif resp.get("message"):
            return resp.get("message", {}).get("content", "")
    return ""


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
        content = str(m.get("content", ""))[:1000]
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
    lines = [f"[CONTEXT COMPACTED — {len(messages)} messages summarized, LLM unavailable]", ""]
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

    safe_msgs = []
    for m in messages[-20:]:
        safe_msgs.append(
            {
                "role": m.get("role", "unknown"),
                "content": str(m.get("content", ""))[:500],
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
        "OUTPUT ONLY VALID JSON. NO MARKDOWN. NO EXPLANATION."
    )

    distilled_state: Dict[str, Any] = {}

    try:
        content = _call_llm_sync(
            [{"role": "user", "content": prompt}], format_json=True
        )
        if content:
            import re
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                distilled_state = json.loads(match.group(0))
            else:
                distilled_state = json.loads(content)
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
            lines.extend(["", "# Completed Steps"])
            for step in distilled_state.get("completed_steps", []):
                lines.append(f"- {step}")
            errors = distilled_state.get("errors_resolved", [])
            if errors:
                lines.extend(["", "# Errors Resolved"])
                for err in errors:
                    lines.append(f"- {err}")
            lines.extend(["", "# Next Step", distilled_state.get("next_step", "None")])
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

    return distilled_state
