"""Pure token-budget truncation helpers for context building."""

from __future__ import annotations

from typing import Callable, Optional

# A2.4: Shared token estimator shared with pruning (tool_output_truncation) so
# truncation and pruning agree on token counts.  Prefer the accurate tokenizer.
try:
    from src.core.inference.tokenizer import count_tokens as _count_tokens
except Exception:  # pragma: no cover - graceful degradation
    _count_tokens = None  # type: ignore[assignment]


def estimate_text_tokens(text: str) -> int:
    """Return an approximate token count for *text* (shared estimator)."""
    if not text:
        return 0
    if _count_tokens is not None:
        try:
            return _count_tokens(text)
        except Exception:
            pass
    return max(1, len(text) // 4)


def _resolve_estimator(
    token_estimator: Optional[Callable[[str], int]] = None,
) -> Callable[[str], int]:
    return token_estimator if token_estimator is not None else estimate_text_tokens


def truncate_to_token_budget(
    text: str,
    budget: int,
    *,
    token_estimator: Optional[Callable[[str], int]] = None,
) -> str:
    """Return the longest prefix whose token count fits within budget."""
    est = _resolve_estimator(token_estimator)
    if est(text) <= budget:
        return text
    lo, hi = 0, len(text)
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if est(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid
    return text[:lo]


def truncate_text_to_max_tokens(
    text: str,
    max_tokens: int,
    *,
    token_estimator: Optional[Callable[[str], int]] = None,
) -> str:
    """Truncate text to max_tokens and append a marker when space allows."""
    est = _resolve_estimator(token_estimator)
    if est(text) <= max_tokens:
        return text

    marker = "\n\n[TRUNCATED]"
    marker_tokens = est(marker)
    if max_tokens < marker_tokens:
        return truncate_to_token_budget(text, max_tokens, token_estimator=est)

    content_budget = max(0, max_tokens - marker_tokens)
    truncated_text = text
    original_text_tokens = est(text)

    if original_text_tokens > content_budget:
        approx_chars_per_token = len(text) / original_text_tokens if original_text_tokens > 0 else 4
        target_char_limit = max(0, int(content_budget * approx_chars_per_token))
        if len(truncated_text) > target_char_limit:
            truncated_text = truncated_text[:target_char_limit]

        truncated_text = truncate_to_token_budget(
            truncated_text,
            content_budget,
            token_estimator=est,
        )

        if est(text) > est(truncated_text) and est(
            truncated_text + marker
        ) <= max_tokens:
            return truncated_text + marker

        return truncated_text if est(truncated_text) <= max_tokens else ""

    return text
