from src.core.context.token_truncation import (
    truncate_text_to_max_tokens,
    truncate_to_token_budget,
)


def test_truncate_to_token_budget_binary_search_prefix():
    text = "abcdefghij"
    result = truncate_to_token_budget(
        text,
        4,
        token_estimator=lambda s: len(s),
    )
    assert result == "abcd"


def test_truncate_text_to_max_tokens_adds_marker_when_it_fits():
    text = "abcdefghij"
    result = truncate_text_to_max_tokens(
        text,
        8,
        token_estimator=lambda s: len(s),
    )
    assert result.endswith("[TRUNCATED]") or result != text


def test_truncate_text_to_max_tokens_returns_prefix_when_marker_cannot_fit():
    text = "abcdefghij"
    result = truncate_text_to_max_tokens(
        text,
        2,
        token_estimator=lambda s: len(s),
    )
    assert len(result) <= 2
