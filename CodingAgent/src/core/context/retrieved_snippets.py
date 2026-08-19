"""Helpers for retrieved snippet budgeting and filtering."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def build_context_controller_descriptors(
    retrieved_snippets: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert retrieved snippet mappings to ContextController file descriptors."""
    descriptors: list[dict[str, Any]] = []
    for snippet in retrieved_snippets:
        content = snippet.get("snippet") or snippet.get("content") or ""
        descriptors.append(
            {
                "path": snippet.get("file_path", ""),
                "content": content,
                "line_count": len(content.splitlines()),
                "estimated_tokens": max(1, len(content) // 4),
            }
        )
    return descriptors


def filter_retrieved_snippets_by_budget(
    retrieved_snippets: list[dict[str, Any]],
    *,
    included_descriptors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only snippets whose file paths survived budget enforcement."""
    included_paths = {desc["path"] for desc in included_descriptors}
    filtered = [
        snippet
        for snippet in retrieved_snippets
        if snippet.get("file_path", "") in included_paths
    ]
    return filtered
