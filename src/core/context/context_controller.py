"""
Context Budget Controller - Manages context token limits to prevent overflow.

This is critical for local LLM agents which have limited context windows.
"""

import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple


class ContextController:
    """Manages context budget to prevent token overflow."""

    DEFAULT_MAX_TOKENS = 6000
    LARGE_FILE_THRESHOLD = 500  # Lines
    SUMMARY_TARGET_LINES = 100

    def __init__(self, max_context_tokens: int = DEFAULT_MAX_TOKENS):
        self.max_context_tokens = max_context_tokens

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

        # Keep first N lines as summary
        # Extract imports and function definitions
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
        """
        Enforce context budget by dropping least relevant files and summarizing large files.

        Returns:
            (included_files, excluded_files)
        """
        included = []
        excluded = []

        # Calculate budget used by system prompt and conversation
        system_tokens = self.estimate_tokens(system_prompt)
        conversation_tokens = sum(
            self.estimate_tokens(str(msg.get("content", "")))
            for msg in conversation_history
        )

        available_tokens = (
            self.max_context_tokens - system_tokens - conversation_tokens - 500
        )  # Buffer

        # Sort files by line count (smaller first) to fit more
        sorted_files = sorted(files, key=lambda f: f.get("line_count", 0))

        current_tokens = 0

        for file_info in sorted_files:
            file_tokens = file_info.get(
                "estimated_tokens", file_info.get("line_count", 0) // 4
            )

            # Check if adding this file would exceed budget
            if current_tokens + file_tokens > available_tokens:
                # Try summarizing if file is large
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
                        continue

                # Can't fit, exclude
                excluded.append(file_info)
                continue

            included.append(file_info)
            current_tokens += file_tokens

        return included, excluded

    def get_relevant_snippets(
        self, content: str, query: str, max_tokens: int = 500
    ) -> List[str]:
        """Extract relevant code sections based on query."""
        lines = content.split("\n")

        # Simple keyword matching for relevant sections
        query_words = set(query.lower().split())
        relevant_line_indices = []

        for i, line in enumerate(lines, 1):
            line_lower = line.lower()
            if any(word in line_lower for word in query_words):
                # Include surrounding context
                start = max(0, i - 3)
                end = min(len(lines), i + 3)
                relevant_line_indices.extend(range(start, end))

        if not relevant_line_indices:
            # Return first portion if no matches
            max_lines = max_tokens // 4
            return ["\n".join(lines[:max_lines])]

        # Deduplicate and sort
        relevant_lines = sorted(set(relevant_line_indices))
        snippets = []

        # Group consecutive lines
        current_group = []
        for idx in relevant_lines:
            if current_group and idx != current_group[-1] + 1:
                snippets.append("\n".join(current_group))
                current_group = []
            current_group.append(lines[idx])

        if current_group:
            snippets.append("\n".join(current_group))

        return snippets[:5]  # Limit to 5 snippets


def create_context_controller(max_tokens: int = 6000) -> ContextController:
    """Factory function to create ContextController."""
    return ContextController(max_context_tokens=max_tokens)
