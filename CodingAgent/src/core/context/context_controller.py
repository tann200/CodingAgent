"""
Context Budget Controller - Manages context token limits to prevent overflow.

This is critical for local LLM agents which have limited context windows.
"""

import math
import re
from typing import List, Dict, Any, Tuple, Optional


class ContextController:
    """Manages context budget to prevent token overflow."""

    DEFAULT_MAX_TOKENS = 6000
    LARGE_FILE_THRESHOLD = 500
    SUMMARY_TARGET_LINES = 100
    _RESERVED_OVERHEAD_TOKENS = 500

    # Budget fractions for each context category (F-63: named constants).
    _BUDGET_RELEVANT_FILES = 0.08
    _BUDGET_BUGS_FOUND = 0.05
    _BUDGET_RESEARCH = 0.06
    _BUDGET_OTHER = 0.03

    def __init__(
        self,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_context_tokens: Optional[int] = None,
    ):
        self.max_tokens = max_tokens
        self.max_context_tokens = max_context_tokens or max_tokens

        self._context_budget = {
            "relevant_files": math.ceil(self._BUDGET_RELEVANT_FILES * max_tokens),
            "bugs_found": math.ceil(self._BUDGET_BUGS_FOUND * max_tokens),
            "research": math.ceil(self._BUDGET_RESEARCH * max_tokens),
            "other": math.ceil(self._BUDGET_OTHER * max_tokens),
        }
        self._used_tokens = 0
        self._last_system_tokens = 0
        self._last_conversation_tokens = 0
        self._last_available_tokens = max(
            0, self.max_context_tokens - self._RESERVED_OVERHEAD_TOKENS
        )

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimation (4 chars per token)."""
        return max(1, len(text) // 4)

    def prioritize_files(
        self, files: List[Dict[str, Any]], relevance_scores: Dict[str, float]
    ) -> List[Dict[str, Any]]:
        """Sort files by relevance score (highest first)."""
        return sorted(
            files,
            key=lambda f: relevance_scores.get(f.get("path", ""), 0.0),
            reverse=True,
        )

    def should_summarize(self, file_info: Dict[str, Any]) -> bool:
        """Determine if file should be summarized due to size."""
        lines = file_info.get("line_count", 0)
        return lines > self.LARGE_FILE_THRESHOLD

    def summarize_file_content(
        self, content: str, target_lines: int = SUMMARY_TARGET_LINES
    ) -> str:
        """Summarize large file to target line count."""
        lines = content.split("\n")
        if len(lines) <= target_lines:
            return content

        important_patterns = [
            r"^import\s+",
            r"^from\s+\S+\s+import",
            r"^def\s+",
            r"^class\s+",
            r"^async\s+def\s+",
        ]

        important_lines = []
        for i, line in enumerate(lines[:target_lines], 1):
            for pattern in important_patterns:
                if re.match(pattern, line):
                    important_lines.append(f"{i}: {line}")
                    break

        summary = (
            "\n".join(important_lines)
            if important_lines
            else "\n".join(lines[:target_lines])
        )
        return f"{summary}\n\n... {len(lines) - target_lines} more lines ..."

    def enforce_budget(
        self,
        files: List[Dict[str, Any]],
        conversation_history: List[Dict[str, Any]],
        system_prompt: str = "",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Enforce context budget by dropping least relevant files and summarizing large files."""
        included = []
        excluded = []

        system_tokens = self.estimate_tokens(system_prompt)
        conversation_tokens = sum(
            self.estimate_tokens(str(msg.get("content", "")))
            for msg in conversation_history
        )

        available_tokens = max(
            0,
            self.max_context_tokens
            - system_tokens
            - conversation_tokens
            - self._RESERVED_OVERHEAD_TOKENS,
        )
        self._last_system_tokens = system_tokens
        self._last_conversation_tokens = conversation_tokens
        self._last_available_tokens = available_tokens

        sorted_files = sorted(files, key=lambda f: f.get("line_count", 0))

        current_tokens = 0

        for file_info in sorted_files:
            file_tokens = file_info.get(
                "estimated_tokens", file_info.get("line_count", 0) // 4
            )

            if current_tokens + file_tokens > available_tokens:
                if self.should_summarize(file_info):
                    summarized_tokens = self.SUMMARY_TARGET_LINES // 4
                    if current_tokens + summarized_tokens <= available_tokens:
                        file_info = file_info.copy()
                        file_info["content"] = self.summarize_file_content(
                            file_info.get("content", ""), self.SUMMARY_TARGET_LINES
                        )
                        file_info["summarized"] = True
                        included.append(file_info)
                        current_tokens += summarized_tokens
                    else:
                        excluded.append(file_info)
                else:
                    excluded.append(file_info)
            else:
                included.append(file_info)
                current_tokens += file_tokens

        self._used_tokens = current_tokens
        return included, excluded

    def extract_relevant_snippets(
        self, content: str, query: str, max_tokens: int = 500
    ) -> List[str]:
        """Extract relevant snippets based on query keywords."""
        lines = content.split("\n")
        relevant_line_indices = []

        query_keywords = re.findall(r"\w+", query.lower())

        for i, line in enumerate(lines):
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in query_keywords):
                relevant_line_indices.append(i)

        if not relevant_line_indices:
            max_lines = max_tokens // 4
            return ["\n".join(lines[:max_lines])]

        relevant_lines = sorted(set(relevant_line_indices))
        snippets = []

        current_group: list[str] = []
        last_idx = -1
        for idx in relevant_lines:
            if current_group and idx != last_idx + 1:
                snippets.append("\n".join(current_group))
                current_group = []
            current_group.append(lines[idx])
            last_idx = idx

        if current_group:
            snippets.append("\n".join(current_group))

        return snippets[:5]

    def get_budget_status(self) -> Dict[str, Any]:
        """Get current status for the most recent enforced snippet budget.

        ``max_tokens`` and ``usage_ratio`` reflect the effective file/snippet budget
        after subtracting system prompt, conversation history, and fixed reserved
        overhead from ``max_context_tokens``.
        """
        remaining_tokens = max(0, self._last_available_tokens - self._used_tokens)
        return {
            "max_tokens": self._last_available_tokens,
            "max_context_tokens": self.max_context_tokens,
            "configured_max_tokens": self.max_tokens,
            "used_tokens": self._used_tokens,
            "remaining_tokens": remaining_tokens,
            "system_tokens": self._last_system_tokens,
            "conversation_tokens": self._last_conversation_tokens,
            "reserved_tokens": self._RESERVED_OVERHEAD_TOKENS,
            "budgets": self._context_budget,
            "usage_ratio": self._used_tokens / self._last_available_tokens
            if self._last_available_tokens > 0
            else 0,
        }

    def get_relevant_snippets(
        self, content: str, query: str, max_tokens: int = 500
    ) -> List[str]:
        """Alias for extract_relevant_snippets."""
        return self.extract_relevant_snippets(content, query, max_tokens)


def create_context_controller(max_tokens: int = 6000) -> ContextController:
    """Factory function to create ContextController."""
    return ContextController(max_tokens=max_tokens)


def get_context_controller(max_tokens: int = 6000) -> ContextController:
    """Get context controller with percentage-based budgets."""
    return ContextController(max_tokens=max_tokens)
