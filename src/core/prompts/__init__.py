"""
src.core.prompts — System prompt assembly package.

Public surface
--------------
    SystemPromptBuilder   — Two-part, model-adaptive prompt builder.
    PromptContext         — Input dataclass for one turn of prompt assembly.
    reload_templates      — Flush the in-process template cache.
"""

from __future__ import annotations

from src.core.prompts.system_prompt_builder import (
    PromptContext,
    SystemPromptBuilder,
    reload_templates,
)

__all__ = [
    "SystemPromptBuilder",
    "PromptContext",
    "reload_templates",
]
