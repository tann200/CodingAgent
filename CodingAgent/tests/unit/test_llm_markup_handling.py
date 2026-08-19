"""Tests for markup stripping utility (migrated from src.ui — LEGACY-02).

Previously tested ``TextualAppImpl._render_message_safe``; now tests the
pure-function equivalent ``src.core.utils.strip_markup`` directly.
"""

from src.core.utils import strip_markup


def test_strip_markup_removes_bold_and_dim():
    result = strip_markup("[bold]Hello[/bold] [dim]there[/dim]")
    assert "[bold]" not in result
    assert "Hello" in result
    assert "there" in result


def test_strip_markup_non_string_coerced():
    result = strip_markup(42)  # type: ignore[arg-type]
    assert result == "42"


def test_strip_markup_plain_text_unchanged():
    plain = "Hello, world!"
    assert strip_markup(plain) == plain


def test_strip_markup_nested_tags():
    result = strip_markup("[bold][italic]nested[/italic][/bold]")
    assert result == "nested"


def test_strip_markup_empty_string():
    assert strip_markup("") == ""
