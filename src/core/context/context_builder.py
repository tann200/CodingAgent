from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
import math
import json

class ContextBuilder:
    def __init__(self, token_estimator: Optional[Callable[[str], int]] = None):
        self.token_estimator: Callable[[str], int] = token_estimator if token_estimator is not None else (lambda s: math.ceil(len(s) / 4)) # Default to len/4 if no estimator provided

    def build_prompt(self, identity: str, role: str, active_skills: List[str], task_description: str, tools: List[Dict], conversation: List[Dict], max_tokens: int = 6000) -> List[Dict[str,str]]:
        # Token budgeting rules
        identity_quota = min(math.ceil(0.12 * max_tokens), 800)
        role_quota = min(math.ceil(0.12 * max_tokens), 800)
        tools_quota = min(math.ceil(0.06 * max_tokens), 400)
        conversation_quota = max(0, max_tokens - (identity_quota + role_quota + tools_quota + 500)) # buffer for tags

        built_messages: List[Dict[str, str]] = []

        # 1. System Block (Identity + Role + Skills + Tools)
        # We consolidate these into a single system message for better compatibility
        system_parts = []
        
        system_parts.append(f"<identity>\n{identity}\n</identity>")
        system_parts.append(f"<role>\n{role}\n</role>")
        
        if active_skills:
            system_parts.append(f"<active_skills>\n{chr(10).join(active_skills)}\n</active_skills>")
            
        tools_text = ""
        for tool in tools:
            tools_text += f"name: {tool['name']}\ndescription: {tool['description']}\n"
        system_parts.append(f"<available_tools>\n{tools_text}\n</available_tools>")
        
        # 1.5 Mandatory Output Format (Last part of system instructions)
        format_instr = (
            "<output_format>\n"
            "You MUST think step-by-step. Write your internal reasoning inside <think> tags.\n"
            "To execute an action, you MUST use the provided XML tool format. NEVER use JSON tool calls.\n"
            "Format your tool calls exactly like this:\n"
            "<tool>\n"
            "name: the_tool_name\n"
            "arguments: {\"arg_name\": \"arg_value\"}\n"
            "</tool>\n"
            "Wait for the user to provide the tool execution result before proceeding.\n"
            "</output_format>"
        )
        system_parts.append(format_instr)
        
        built_messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # 2. Conversation Logic
        # Filter msg_mgr to only include User and Assistant messages (strip system prompts)
        filtered_conv = [m for m in conversation if m.get("role") in ["user", "assistant"]]
        
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
        
        # Final check: is the last message Assistant or is the list missing User?
        if not built_messages or built_messages[-1].get("role") != "user":
            prompt_content = f"<task>\n{task_description}\n</task>\n\nExecute the next action using the <tool> format."
            built_messages.append({"role": "user", "content": prompt_content})
        else:
            # If the last message is already User, we can either wrap it in <task> 
            # or just leave it. For continuity, let's wrap it if it doesn't have it.
            last_msg = built_messages[-1]
            if "<task>" not in last_msg.get("content", ""):
                last_msg["content"] = f"<task>\n{last_msg['content']}\n</task>\n\nExecute the next action using the <tool> format."
        
        return built_messages
        
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
                if self.token_estimator(text[:i+1]) <= max_tokens:
                    truncated_text = text[:i+1]
                else:
                    break
            return truncated_text # No marker in this case

        # Now, we know there's enough space for at least the marker.
        # Calculate budget for the actual content before the marker.
        content_budget_for_truncation = max(0, max_tokens - marker_tokens)
        
        truncated_text = text
        original_text_tokens = self.token_estimator(text)

        # Truncate content to fit content_budget_for_truncation
        if original_text_tokens > content_budget_for_truncation:
            # Heuristic for character limit based on average chars/token for efficiency
            approx_chars_per_token = len(text) / original_text_tokens if original_text_tokens > 0 else 4
            target_char_limit = max(0, int(content_budget_for_truncation * approx_chars_per_token))
            
            if len(truncated_text) > target_char_limit:
                truncated_text = truncated_text[:target_char_limit]

            # Fine-tune by removing characters one by one until within budget
            while self.token_estimator(truncated_text) > content_budget_for_truncation and len(truncated_text) > 0:
                truncated_text = truncated_text[:-1]
            
            # If truncation actually occurred (original text was longer than what fits in content_budget_for_truncation)
            # and we have space for the marker, add it.
            if self.token_estimator(text) > self.token_estimator(truncated_text) and self.token_estimator(truncated_text + marker) <= max_tokens:
                return truncated_text + marker
            else:
                # If we couldn't fit the marker, or no effective truncation, just return the content within max_tokens
                return truncated_text if self.token_estimator(truncated_text) <= max_tokens else "" # Should already fit, but defensive
        else:
            # Content already fits within the budget for content + marker, so no truncation needed and no marker added.
            return text

    def _build_system_message(self, tag: str, raw_content: str, total_quota: int) -> Dict[str, str]:
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
                if self.token_estimator(ideal_full_msg[:i+1]) <= total_quota:
                    truncated_msg = ideal_full_msg[:i+1]
                else:
                    break
            return {"role": "system", "content": truncated_msg}

        # 3. We have budget for the wrapper + marker + some content.
        content_budget = total_quota - wrapper_tokens
        
        # Heuristic starting point for content truncation
        approx_chars_per_token = len(raw_content) / self.token_estimator(raw_content) if self.token_estimator(raw_content) > 0 else 4
        target_char_limit = max(0, int(content_budget * approx_chars_per_token))
        
        truncated_content = raw_content[:target_char_limit]

        # Function to test a specific truncated content length
        def test_fit(content_candidate):
            return self.token_estimator(f"<{tag}>\n{content_candidate}\n\n[TRUNCATED]\n</{tag}>") <= total_quota

        # If it's too big, shrink it
        while not test_fit(truncated_content) and len(truncated_content) > 0:
            truncated_content = truncated_content[:-1]
            
        return {"role": "system", "content": f"<{tag}>\n{truncated_content}\n\n[TRUNCATED]\n</{tag}>"}


