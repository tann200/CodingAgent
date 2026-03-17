import pytest
from src.ui.textual_app_impl import TextualAppImpl


class DummyMessage:
    def __init__(self, role, content):
        self.role = role
        self.content = content


def test_textual_app_strips_markup(monkeypatch, capsys):
    app = TextualAppImpl()

    # Simulate adding a message with markup
    msg = DummyMessage('assistant', '[bold]Hello[/bold] [dim]there[/dim]')

    # Call the internal writer; we expect no literal [bold] tags in output
    text = app._render_message_safe(msg.content)
    assert '[bold]' not in text
    assert 'Hello' in text
    assert 'there' in text

