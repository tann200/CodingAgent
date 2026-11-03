import pytest
from unittest.mock import MagicMock
from tui.tui_src.ui.bus import StreamChunkEvent, AgentFinalResponse
from tui.tui_src.ui.components.chat_mixin import ChatDisplayMixin
from tui.tui_src.ui.components.artifact import AgentArtifact
from tui.tui_src.ui.components.stream_view import StreamView


class DummyChatApp(ChatDisplayMixin):
    def __init__(self):
        self.mounted = []
        self._current_stream = None
        self._active_stream_view = None
        self.is_streaming = False
        self.active_role = "lead_architect"
        self._chat_log = MagicMock()
        self.call_later_queue = []

    def query_one(self, selector, expected_type=None):
        if selector == "#chat_log":
            return self._chat_log
        raise KeyError(selector)

    def call_later(self, fn, *args, **kwargs):
        self.call_later_queue.append((fn, args, kwargs))

    async def _mount_chat_widget(self, w):
        self.mounted.append(w)

    def _update_status_bar(self):
        pass


@pytest.mark.asyncio
async def test_stream_view_detached_on_final_response():
    app = DummyChatApp()

    # 1. Stream chunks arrive
    ev_chunk1 = StreamChunkEvent(chunk="Hello ", is_partial=True)
    app._chat_handle_stream_chunk(ev_chunk1)

    assert app._current_stream is not None
    assert app._active_stream_view is app._current_stream
    stream_widget = app._active_stream_view

    # Mock stream widget remove
    stream_widget.remove = MagicMock()

    ev_chunk2 = StreamChunkEvent(chunk="world!", is_partial=False)
    app._chat_handle_stream_chunk(ev_chunk2)

    # Stream is finalized, but _active_stream_view still tracks the widget until final response
    assert app._current_stream is None
    assert app._active_stream_view is stream_widget

    # 2. Final response arrives
    ev_final = AgentFinalResponse(content="Hello world!")
    await app._chat_handle_final_response(ev_final)

    # Stream view should have been removed and detached
    stream_widget.remove.assert_called_once()
    assert getattr(stream_widget, "_detached", False) is True
    assert app._active_stream_view is None

    # Only the AgentArtifact is mounted
    assert len(app.mounted) == 1
    assert isinstance(app.mounted[0], AgentArtifact)
    assert app.mounted[0]._content_str == "Hello world!"
