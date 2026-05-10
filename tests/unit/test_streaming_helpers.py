from src.core.inference.streaming import (
    decode_sse_line,
    extract_stream_deltas,
    finalize_stream,
    parse_sse_chunk,
    publish_stream_chunk,
    split_thinking_content,
)


class _EventBus:
    def __init__(self):
        self.events = []

    def publish(self, event_name, payload):
        self.events.append((event_name, payload))


def test_decode_sse_line_handles_data_and_done_markers():
    assert decode_sse_line(b"data: hello") == "hello"
    assert decode_sse_line("data: [DONE]") == "[DONE]"
    assert decode_sse_line("event: ping") is None


def test_parse_sse_chunk_returns_dict_for_valid_json():
    assert parse_sse_chunk('{"choices": []}') == {"choices": []}
    assert parse_sse_chunk("not json") is None


def test_extract_stream_deltas_prefers_structured_reasoning():
    chunk = {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "thinking",
                    "content": "answer",
                }
            }
        ]
    }

    delta, reasoning, content = extract_stream_deltas(chunk)
    assert delta["content"] == "answer"
    assert reasoning == "thinking"
    assert content == "answer"


def test_publish_stream_chunk_emits_tokens_only_for_non_reasoning():
    bus = _EventBus()

    publish_stream_chunk(bus=bus, chunk="hello", is_reasoning=False)
    publish_stream_chunk(bus=bus, chunk="thought", is_reasoning=True)

    assert bus.events == [
        ("response.stream_chunk", {"chunk": "hello", "is_reasoning": False}),
        ("model.token", {"text": "hello", "partial": True}),
        ("llm.token", {"text": "hello", "partial": True, "is_reasoning": False}),
        ("response.stream_chunk", {"chunk": "thought", "is_reasoning": True}),
    ]


def test_split_thinking_content_splits_before_think_and_collects_text():
    published = []

    reasoning, content, inside_think, text_parts, used_original = split_thinking_content(
        content_delta="Answer<think>hidden",
        inside_think=False,
        tag_split_enabled=True,
        publish_chunk=lambda chunk, is_reasoning: published.append((chunk, is_reasoning)),
    )

    assert reasoning == ""
    assert content == ""
    assert inside_think is True
    assert text_parts == ["Answer"]
    assert used_original is True
    assert published == [("Answer", False), ("hidden", True)]


def test_split_thinking_content_closes_think_block_and_returns_after_text():
    published = []

    reasoning, content, inside_think, text_parts, used_original = split_thinking_content(
        content_delta="hidden</think>Final",
        inside_think=True,
        tag_split_enabled=True,
        publish_chunk=lambda chunk, is_reasoning: published.append((chunk, is_reasoning)),
    )

    assert reasoning == ""
    assert content == "Final"
    assert inside_think is False
    assert text_parts == []
    assert used_original is True
    assert published == [("hidden", True)]


def test_finalize_stream_publishes_full_text_and_end_event():
    bus = _EventBus()

    result = finalize_stream(bus=bus, accumulated=["Hel", "lo"])

    assert result == "Hello"
    assert bus.events == [
        ("model.token", {"text": "", "partial": False, "full": "Hello"}),
        ("llm.token", {"text": "", "partial": False, "full": "Hello"}),
        ("response.stream_end", {"full_text": "Hello"}),
    ]
