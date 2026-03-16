import json
import logging
from typing import Any, Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


def distill_context(
    messages: List[Dict[str, str]],
    max_summary_tokens: int = 512,
    llm_client: Any = None,
    working_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return {"current_task": str, "completed_steps": [str], "next_step": str}"""
    logger.info(f"distill_context called with {len(messages)} messages")
    if not messages:
        return {}

    prompt = """System: You are a concise task state tracker. Your ONLY output must be valid JSON - no markdown, no code blocks, no explanation, no thinking tags. RESPOND IN ENGLISH ONLY.

Output format:
{
  "current_task": "brief description of current task",
  "completed_steps": ["step 1", "step 2"],
  "next_step": "what comes next"
}

Respond in English only. Keep descriptions under 10 words each.

User: Here are the recent messages:
<PASTE MESSAGES>
"""
    # Safe dump of messages
    safe_msgs = []
    for m in messages[-20:]:
        safe_msgs.append(
            {
                "role": m.get("role", "unknown"),
                "content": str(m.get("content", ""))[:500],
            }
        )
    msg_str = json.dumps(safe_msgs, indent=2)
    prompt = prompt.replace("<PASTE MESSAGES>", msg_str)

    prompt += "\n\nOUTPUT ONLY VALID JSON. NO MARKDOWN. NO EXPLANATION."

    distilled_state = {}

    # Disable thinking for faster distillation
    extra_params: Dict[str, Any] = {}

    try:
        import asyncio
        from src.core.llm_manager import call_model

        # Safe async execution within synchronous context
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        asyncio.run,
                        call_model(
                            messages=[{"role": "user", "content": prompt}],
                            format_json=True,
                            stream=False,
                            tools=None,
                        ),
                    )
                    resp = future.result()
            else:
                resp = asyncio.run(
                    call_model(
                        messages=[{"role": "user", "content": prompt}],
                        format_json=True,
                        stream=False,
                        tools=None,
                    )
                )
        except RuntimeError:
            resp = asyncio.run(
                call_model(
                    messages=[{"role": "user", "content": prompt}],
                    format_json=True,
                    stream=False,
                    tools=None,
                )
            )

        content = ""
        if isinstance(resp, dict):
            if resp.get("choices") and isinstance(resp.get("choices"), list):
                content = resp["choices"][0].get("message", {}).get("content", "")
            elif resp.get("message"):
                content = resp.get("message", {}).get("content", "")

        if content:
            import re

            match = re.search(r"\{.*?\}", content, re.DOTALL)
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
                "# Completed Steps",
            ]
            for step in distilled_state.get("completed_steps", []):
                lines.append(f"- {step}")
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
                    summary_path = working_dir / ".agent-context" / "file_summaries.json"
                    summaries = {}
                    for fdata in repo_index.get("files", []):
                        p = working_dir / fdata.get("path")
                        if p.exists() and p.is_file():
                            try:
                                text = p.read_text(encoding="utf-8")
                                lines = text.splitlines()
                                if len(lines) > 200:
                                    # keep head and tail
                                    summary = "\n".join(lines[:10] + ["[...skipped...]"] + lines[-10:])
                                else:
                                    summary = "\n".join(lines[:200])
                                summaries[str(fdata.get("path"))] = summary
                            except Exception:
                                continue
                    try:
                        summary_path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Failed to write repo_memory.json: {e}")

    return distilled_state
