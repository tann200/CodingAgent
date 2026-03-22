from __future__ import annotations
from typing import Callable, Dict, List, Optional
import math
import json
from collections import OrderedDict
from pathlib import Path

# F10: Import dynamic token budget helper (lazy — avoids circular imports at module load).
def _default_max_tokens() -> int:
    try:
        from src.core.inference.provider_context import get_context_budget
        return get_context_budget()
    except Exception:
        return 6000

# Module-level caches keyed by absolute file path (NEW-20).
# ContextBuilder is re-instantiated on every node call, so instance-level caches
# were always empty and provided zero benefit.  Module-level caches persist across
# calls as long as the process is alive and the file has not changed on disk.
# F15: Use OrderedDict with a max-size cap to prevent unbounded memory growth.
_TEXT_CACHE: OrderedDict = OrderedDict()   # path → (mtime, content); max 256 entries
_JSON_CACHE: OrderedDict = OrderedDict()   # path → (mtime, parsed);  max 256 entries
_CACHE_MAX = 256


def _today_iso() -> str:
    """Return today's date as YYYY-MM-DD (local time)."""
    from datetime import date
    return date.today().isoformat()


class ContextBuilder:
    def __init__(
        self,
        token_estimator: Optional[Callable[[str], int]] = None,
        working_dir: Optional[str] = None,
    ):
        self.token_estimator: Callable[[str], int] = (
            token_estimator
            if token_estimator is not None
            else (lambda s: math.ceil(len(s) / 4))
        )  # Default to len/4 if no estimator provided
        # Resolve working directory — use provided path, else cwd.
        # Nodes should pass state["working_dir"] so files are found in the right location (NEW-10).
        self._agent_context_dir: Path = (
            Path(working_dir) if working_dir else Path.cwd()
        ) / ".agent-context"

    @staticmethod
    def _read_text_cached(path: Path) -> Optional[str]:
        """Read a text file, returning cached content if mtime unchanged."""
        if not path.exists():
            return None
        try:
            key = str(path)
            mtime = path.stat().st_mtime
            if key in _TEXT_CACHE and _TEXT_CACHE[key][0] == mtime:
                _TEXT_CACHE.move_to_end(key)
                return _TEXT_CACHE[key][1]
            content = path.read_text(encoding="utf-8").strip()
            # F15: Evict oldest entry when cache is full
            if len(_TEXT_CACHE) >= _CACHE_MAX:
                _TEXT_CACHE.popitem(last=False)
            _TEXT_CACHE[key] = (mtime, content)
            return content
        except Exception:
            return None

    @staticmethod
    def _read_json_cached(path: Path) -> Dict:
        """Read a JSON file, returning cached parsed dict if mtime unchanged."""
        if not path.exists():
            return {}
        try:
            key = str(path)
            mtime = path.stat().st_mtime
            if key in _JSON_CACHE and _JSON_CACHE[key][0] == mtime:
                _JSON_CACHE.move_to_end(key)
                return _JSON_CACHE[key][1]
            data = json.loads(path.read_text(encoding="utf-8"))
            # F15: Evict oldest entry when cache is full
            if len(_JSON_CACHE) >= _CACHE_MAX:
                _JSON_CACHE.popitem(last=False)
            _JSON_CACHE[key] = (mtime, data)
            return data
        except Exception:
            return {}

    def _get_task_state_content(self) -> Optional[str]:
        """Get TASK_STATE.md content with module-level mtime caching."""
        return self._read_text_cached(self._agent_context_dir / "TASK_STATE.md")

    def _get_todo_content(self) -> Optional[str]:
        """Get TODO.md content with module-level mtime caching."""
        return self._read_text_cached(self._agent_context_dir / "TODO.md")

    def _get_summary_cache(self) -> Dict:
        """Get file_summaries.json with module-level mtime caching."""
        return self._read_json_cached(self._agent_context_dir / "file_summaries.json")

    def _sanitize_text(self, text: str) -> str:
        """Sanitize file / conversation text to reduce prompt-injection risk.
        CRITICAL: Fenced code blocks are NOT removed. Stripping code blocks
        destroys agent tool calls and causes infinite loops.
        - Remove top-level prompt-injection lines like "ignore all instructions".
        - Collapse long comment blocks (keep first/last few lines).
        """
        if not text:
            return text

        # 1) Remove obvious prompt-injection lines
        lines = text.splitlines()
        cleaned_lines = []
        removed_any = False
        for ln in lines:
            s = ln.strip().lower()
            # heuristics for prompt-injection: match substrings anywhere
            if (
                "ignore all instructions" in s
                or "do not follow" in s
                or "disregard previous" in s
                or "forget all previous" in s
            ):
                # skip this line
                removed_any = True
                continue
            cleaned_lines.append(ln)
        text = "\n".join(cleaned_lines)

        # 4) Collapse very long comment blocks (consecutive comment lines > 20)
        collapsed = []
        comment_block = []
        for ln in text.splitlines():
            if ln.strip().startswith("#") or ln.strip().startswith("//"):
                comment_block.append(ln)
            else:
                if len(comment_block) > 20:
                    # keep first 3 and last 3
                    collapsed.extend(comment_block[:3])
                    collapsed.append(
                        f"[COMMENT BLOCK TRUNCATED - {len(comment_block)} lines]"
                    )
                    collapsed.extend(comment_block[-3:])
                    removed_any = True
                else:
                    collapsed.extend(comment_block)
                comment_block = []
                collapsed.append(ln)
        # flush tail comment block
        if comment_block:
            if len(comment_block) > 20:
                collapsed.extend(comment_block[:3])
                collapsed.append(
                    f"[COMMENT BLOCK TRUNCATED - {len(comment_block)} lines]"
                )
                collapsed.extend(comment_block[-3:])
                removed_any = True
            else:
                collapsed.extend(comment_block)

        sanitized = "\n".join(collapsed)

        # Best-effort audit log for sanitization events
        if removed_any:
            try:
                cwd = Path.cwd()
                ac = cwd / ".agent-context"
                if ac.exists():
                    logp = ac / "context_sanitization.log"
                    with open(logp, "a", encoding="utf-8") as f:
                        f.write("SANITIZE: removed suspicious content\n")
            except Exception:
                # never fail sanitization due to logging issues
                pass

        return sanitized

    def build_prompt(
        self,
        identity: str,
        role: str,
        active_skills: List[str],
        task_description: str,
        tools: List[Dict],
        conversation: List[Dict],
        max_tokens: Optional[int] = None,
        retrieved_snippets: Optional[List[Dict]] = None,
    ) -> List[Dict[str, str]]:
        # F10: Use dynamic token budget when max_tokens is not explicitly provided.
        if max_tokens is None:
            max_tokens = _default_max_tokens()
        # Token budgeting rules
        identity_quota = min(math.ceil(0.12 * max_tokens), 800)
        role_quota = min(math.ceil(0.12 * max_tokens), 800)
        tools_quota = min(math.ceil(0.06 * max_tokens), 400)
        conversation_quota = max(
            0, max_tokens - (identity_quota + role_quota + tools_quota + 500)
        )  # buffer for tags

        built_messages: List[Dict[str, str]] = []

        # 1. System Block (Identity + Role + Skills + Tools)
        # We consolidate these into a single system message for better compatibility
        system_parts = []

        # sanitize identity/role/task_description
        safe_identity = self._sanitize_text(identity)
        safe_role = self._sanitize_text(role)
        safe_task_description = self._sanitize_text(task_description)

        system_parts.append(f"<identity>\n{safe_identity}\n</identity>")
        system_parts.append(f"<role>\n{safe_role}\n</role>")

        # 1a. Session summary — auto-injected from TASK_STATE.md so the agent
        #     always has access to prior context without needing a tool call.
        #     (Mirrors the compaction injection used by Claude Code / OpenCode.)
        try:
            ts_content = self._get_task_state_content()
            # Only inject when there is meaningful content beyond the empty template
            _empty = "# Current Task\n\n# Completed Steps\n\n# Next Step"
            if (
                ts_content
                and ts_content.strip() != _empty.strip()
                and len(ts_content) > 60
            ):
                system_parts.append(
                    f"<session_summary>\n{ts_content}\n</session_summary>"
                )
        except Exception:
            pass  # never fail prompt building due to missing TASK_STATE.md

        # 1b. Task progress — auto-injected from TODO.md when it exists.
        #     TODO.md is the authoritative, deterministic plan tracker (written by planning_node,
        #     updated by execution_node). It takes precedence over TASK_STATE.md for step status.
        try:
            todo_content = self._get_todo_content()
            if todo_content and len(todo_content) > 20:
                system_parts.append(
                    f"<task_progress>\n{todo_content}\n</task_progress>"
                )
        except Exception:
            pass  # never fail prompt building due to missing TODO.md

        # 1b. Repository Intelligence block (if any retrieved snippets provided)
        repo_block = ""
        if retrieved_snippets:
            try:
                # Use cached file summaries
                summary_cache = self._get_summary_cache()

                repo_entries = []
                for snip in retrieved_snippets[:10]:
                    # each snippet expected to be dict with keys: file_path, snippet, reason
                    fp = snip.get("file_path")
                    if fp and fp in summary_cache:
                        entry_text = summary_cache.get(fp)
                    else:
                        entry_text = snip.get("snippet") or snip.get("content") or ""
                    # sanitize entry
                    entry_text = self._sanitize_text(str(entry_text))
                    repo_entries.append(f"File: {fp or 'unknown'}\n{entry_text}\n---\n")

                if repo_entries:
                    repo_block = (
                        "<repository_intelligence>\n"
                        + "\n".join(repo_entries)
                        + "\n</repository_intelligence>"
                    )
                    system_parts.append(repo_block)
            except Exception:
                # best-effort: do not fail prompt build
                pass

        if active_skills:
            # sanitize each skill string
            safe_skills = [self._sanitize_text(s) for s in active_skills]
            system_parts.append(
                f"<active_skills>\n{chr(10).join(safe_skills)}\n</active_skills>"
            )

        tools_text = ""
        for tool in tools:
            # keep tool descriptions short; sanitize tool descriptions too
            desc = tool.get("description", "")
            desc = self._sanitize_text(desc)
            tools_text += f"name: {tool['name']}\ndescription: {desc}\n"
        system_parts.append(f"<available_tools>\n{tools_text}\n</available_tools>")

        # 1.5 Mandatory Output Format (Last part of system instructions)
        format_instr = (
            "<output_format>\n"
            "You MUST think step-by-step. Write your internal reasoning inside <think> tags.\n"
            "To execute an action, you MUST use the provided markdown YAML tool format.\n"
            "Format your tool calls exactly like this using a fenced code block:\n"
            "```yaml\n"
            "name: the_tool_name\n"
            "arguments:\n"
            "  arg_name: arg_value\n"
            "```\n"
            "IMPORTANT: Use markdown YAML format (not XML). Do not use <tool> tags.\n"
            "After executing a tool, your response will include the tool's result.\n"
            "If the tool result completes the user's task, do NOT make more tool calls.\n"
            "Simply summarize the result or indicate task completion.\n"
            "Only call another tool if the result requires follow-up action.\n"
            "</output_format>"
        )
        system_parts.append(format_instr)

        built_messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # 2. Conversation Logic
        # Filter msg_mgr to only include User and Assistant messages (strip system prompts)
        filtered_conv = [
            {
                "role": m.get("role"),
                "content": self._sanitize_text(m.get("content", "")),
            }
            for m in conversation
            if m.get("role") in ["user", "assistant"]
        ]

        truncated_conversation: List[Dict[str, str]] = []
        if conversation_quota > 0 and filtered_conv:
            total_conv_tokens = 0
            for message in reversed(filtered_conv):
                msg_json = json.dumps(message)
                message_token_count = self.token_estimator(msg_json)

                if total_conv_tokens + message_token_count <= conversation_quota:
                    truncated_conversation.insert(0, message)
                    total_conv_tokens += message_token_count
                else:
                    break

        # 3. Task / Prompt Logic
        # Ensure the last message is always USER for local model templates
        # If the history ends in ASSISTANT, we must append a "Proceed" user message.
        # If the history is empty, the task itself is the USER message.

        # Add conversation
        built_messages.extend(truncated_conversation)

        # Ensure there's a user message after system for Qwen compatibility
        # If conversation starts with assistant, insert task as user message first
        if (
            truncated_conversation
            and truncated_conversation[0].get("role") == "assistant"
        ):
            # Insert task as user message before the assistant messages
            prompt_content = f"<task>\n{safe_task_description}\n</task>\n<context>\nToday's date: {_today_iso()}\n</context>\n\nExecute the next action using the YAML tool format."
            # Insert at index 1 (after system message)
            built_messages.insert(1, {"role": "user", "content": prompt_content})
        # Final check: is the last message Assistant or is the list missing User?
        elif not built_messages or built_messages[-1].get("role") != "user":
            prompt_content = f"<task>\n{safe_task_description}\n</task>\n<context>\nToday's date: {_today_iso()}\n</context>\n\nExecute the next action using the YAML tool format."
            built_messages.append({"role": "user", "content": prompt_content})
        else:
            # If the last message is already User, we can either wrap it in <task>
            # or just leave it. For continuity, let's wrap it if it doesn't have it.
            last_msg = built_messages[-1]
            if "<task>" not in last_msg.get("content", ""):
                last_msg["content"] = (
                    f"<task>\n{last_msg['content']}\n</task>\n\nExecute the next action using the YAML tool format."
                )

        return built_messages

    def _truncate_text(self, text: str, max_tokens: int) -> str:
        # First, handle the base case: if text already fits, no truncation needed.
        if self.token_estimator(text) <= max_tokens:
            return text

        marker = "\n\n[TRUNCATED]"
        marker_tokens = self.token_estimator(marker)

        # If max_tokens is too small to even fit the marker, return empty or what minimal fits.
        # This ensures we don't try to add a marker that itself exceeds the budget.
        if max_tokens < marker_tokens:
            # Try to fit as much of the original text as possible within max_tokens
            truncated_text = ""
            for i in range(len(text)):
                if self.token_estimator(text[: i + 1]) <= max_tokens:
                    truncated_text = text[: i + 1]
                else:
                    break
            return truncated_text  # No marker in this case

        # Now, we know there's enough space for at least the marker.
        # Calculate budget for the actual content before the marker.
        content_budget_for_truncation = max(0, max_tokens - marker_tokens)

        truncated_text = text
        original_text_tokens = self.token_estimator(text)

        # Truncate content to fit content_budget_for_truncation
        if original_text_tokens > content_budget_for_truncation:
            # Heuristic for character limit based on average chars/token for efficiency
            approx_chars_per_token = (
                len(text) / original_text_tokens if original_text_tokens > 0 else 4
            )
            target_char_limit = max(
                0, int(content_budget_for_truncation * approx_chars_per_token)
            )

            if len(truncated_text) > target_char_limit:
                truncated_text = truncated_text[:target_char_limit]

            # Fine-tune by removing characters one by one until within budget
            while (
                self.token_estimator(truncated_text) > content_budget_for_truncation
                and len(truncated_text) > 0
            ):
                truncated_text = truncated_text[:-1]

            # If truncation actually occurred (original text was longer than what fits in content_budget_for_truncation)
            # and we have space for the marker, add it.
            if (
                self.token_estimator(text) > self.token_estimator(truncated_text)
                and self.token_estimator(truncated_text + marker) <= max_tokens
            ):
                return truncated_text + marker
            else:
                # If we couldn't fit the marker, or no effective truncation, just return the content within max_tokens
                return (
                    truncated_text
                    if self.token_estimator(truncated_text) <= max_tokens
                    else ""
                )  # Should already fit, but defensive
        else:
            # Content already fits within the budget for content + marker, so no truncation needed and no marker added.
            return text

    def _build_system_message(
        self, tag: str, raw_content: str, total_quota: int
    ) -> Dict[str, str]:
        # We need the final message to be <= total_quota
        # Format: <tag>\n{content}\n</tag>
        # If content needs truncation, format: <tag>\n{content}\n\n[TRUNCATED]\n</tag>

        # 1. Check if it fits without truncation
        ideal_full_msg = f"<{tag}>\n{raw_content}\n</{tag}>"
        if self.token_estimator(ideal_full_msg) <= total_quota:
            return {"role": "system", "content": ideal_full_msg}

        # 2. It doesn't fit. We need to truncate.
        # Construct the minimal wrapper with the marker to see how much budget we have for the content.
        wrapper_with_marker = f"<{tag}>\n\n\n[TRUNCATED]\n</{tag}>"
        wrapper_tokens = self.token_estimator(wrapper_with_marker)

        if total_quota <= wrapper_tokens:
            # We don't even have enough budget for the tags and the marker.
            # Just return whatever we can fit of the ideal full message, no marker guarantees.
            truncated_msg = ""
            for i in range(len(ideal_full_msg)):
                if self.token_estimator(ideal_full_msg[: i + 1]) <= total_quota:
                    truncated_msg = ideal_full_msg[: i + 1]
                else:
                    break
            return {"role": "system", "content": truncated_msg}

        # 3. We have budget for the wrapper + marker + some content.
        content_budget = total_quota - wrapper_tokens

        # Heuristic starting point for content truncation
        approx_chars_per_token = (
            len(raw_content) / self.token_estimator(raw_content)
            if self.token_estimator(raw_content) > 0
            else 4
        )
        target_char_limit = max(0, int(content_budget * approx_chars_per_token))

        truncated_content = raw_content[:target_char_limit]

        # Function to test a specific truncated content length
        def test_fit(content_candidate):
            return (
                self.token_estimator(
                    f"<{tag}>\n{content_candidate}\n\n[TRUNCATED]\n</{tag}>"
                )
                <= total_quota
            )

        # If it's too big, shrink it
        while not test_fit(truncated_content) and len(truncated_content) > 0:
            truncated_content = truncated_content[:-1]

        return {
            "role": "system",
            "content": f"<{tag}>\n{truncated_content}\n\n[TRUNCATED]\n</{tag}>",
        }
