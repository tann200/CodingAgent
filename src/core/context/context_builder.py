from __future__ import annotations
from typing import Callable, Dict, List, Optional
import math
import json


class ContextBuilder:
    def __init__(self, token_estimator: Optional[Callable[[str], int]] = None):
        self.token_estimator: Callable[[str], int] = (
            token_estimator
            if token_estimator is not None
            else (lambda s: math.ceil(len(s) / 4))
        )  # Default to len/4 if no estimator provided

    def build_prompt(
        self,
        identity: str,
        role: str,
        active_skills: List[str],
        task_description: str,
        tools: List[Dict],
        conversation: List[Dict],
        max_tokens: int = 6000,
    ) -> List[Dict[str, str]]:
        # Token budgeting rules
        identity_quota = min(math.ceil(0.12 * max_tokens), 800)
        role_quota = min(math.ceil(0.12 * max_tokens), 800)
        tools_quota = min(math.ceil(0.06 * max_tokens), 400)
        # Remaining budget for conversation, task, and format
        remaining_budget = max_tokens - (identity_quota + role_quota + tools_quota)
        # Conversation quota: allow remaining budget to be used for conversation
        # (tests expect conversation_quota = remaining_budget)
        conversation_quota = max(0, remaining_budget)

        built_messages: List[Dict[str, str]] = []

        # 1. System Blocks as separate messages in expected test order
        identity_msg = self._build_system_message("identity", identity, identity_quota)
        built_messages.append(identity_msg)

        role_msg = self._build_system_message("role", role, role_quota)
        built_messages.append(role_msg)

        if active_skills:
            skills_raw = "\n".join(active_skills)
            skills_msg = self._build_system_message("active_skills", skills_raw, 200)
            built_messages.append(skills_msg)

        # Task placed in system messages as tests expect
        task_msg = self._build_system_message("task", task_description, 400)
        built_messages.append(task_msg)

        # Tools
        tools_raw = ""
        for tool in tools:
            tools_raw += f"name: {tool['name']}\ndescription: {tool['description']}\n"
        tools_msg = self._build_system_message("tools", tools_raw, tools_quota)
        built_messages.append(tools_msg)

        # 2. Conversation Logic
        filtered_conv = [m for m in (conversation or []) if m.get("role") in ["user", "assistant"]]

        truncated_conversation: List[Dict[str, str]] = []
        if conversation_quota > 0 and filtered_conv:
            total_conv_tokens = 0
            for message in reversed(filtered_conv):
                msg_json = json.dumps(message)
                # Use the message content length/token estimate (tests expect this behavior)
                message_token_count = self.token_estimator(message.get('content', ''))

                if total_conv_tokens + message_token_count <= conversation_quota:
                    truncated_conversation.insert(0, message)
                    total_conv_tokens += message_token_count
                else:
                    break

            # If we dropped messages, add a system-level note before the preserved conversation
            if len(truncated_conversation) < len(filtered_conv):
                built_messages.append({"role": "system", "content": "[CONTEXT FULL — conversation truncated]"})

            # Append truncated conversation messages (most recent preserved)
            built_messages.extend(truncated_conversation)
            # If nothing fit but there were messages, include a truncated last message so the agent has some context
            if not truncated_conversation and filtered_conv:
                last_msg = filtered_conv[-1]
                tcontent = self._truncate_text(last_msg.get('content', ''), conversation_quota)
                built_messages.append({"role": last_msg.get('role', 'user'), "content": tcontent})
        else:
            # conversation_quota <= 0: insert a system-level note indicating context full
            built_messages.append({"role": "system", "content": "[CONTEXT FULL — conversation truncated]"})

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
        # Format: <tag>
        # {content}
        # </tag>
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
