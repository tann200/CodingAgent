"""Pure token-budget truncation helpers for context building."""

from __future__ import annotations

from typing import Callable


def truncate_to_token_budget(
    text: str,
    budget: int,
    *,
    token_estimator: Callable[[str], int],
) -> str:
    """Return the longest prefix whose token count fits within budget."""
    if token_estimator(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if token_estimator(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid
    return text[:lo]


def truncate_text_to_max_tokens(
    text: str,
    max_tokens: int,
    *,
    token_estimator: Callable[[str], int],
) -> str:
    """Truncate text to max_tokens and append a marker when space allows."""
    if token_estimator(text) <= max_tokens:
        return text

    marker = "\n\n[TRUNCATED]"
    marker_tokens = token_estimator(marker)
    if max_tokens < marker_tokens:
        return truncate_to_token_budget(text, max_tokens, token_estimator=token_estimator)

    content_budget = max(0, max_tokens - marker_tokens)
    truncated_text = text
    original_text_tokens = token_estimator(text)

    if original_text_tokens > content_budget:
        approx_chars_per_token = len(text) / original_text_tokens if original_text_tokens > 0 else 4
        target_char_limit = max(0, int(content_budget * approx_chars_per_token))
        if len(truncated_text) > target_char_limit:
            truncated_text = truncated_text[:target_char_limit]

        truncated_text = truncate_to_token_budget(
            truncated_text,
            content_budget,
            token_estimator=token_estimator,
        )

        if token_estimator(text) > token_estimator(truncated_text) and token_estimator(
            truncated_text + marker
        ) <= max_tokens:
            return truncated_text + marker

        return truncated_text if token_estimator(truncated_text) <= max_tokens else ""

    return text
