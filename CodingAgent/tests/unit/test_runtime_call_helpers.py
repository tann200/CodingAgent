import asyncio

from src.core.inference.runtime_call import (
    call_adapter_with_fallbacks,
    instantiate_runtime_adapter,
    prepare_call_extra_args,
    select_runtime_provider_config,
)


def test_select_runtime_provider_config_matches_name_and_falls_back_first():
    providers = [
        {"name": "first", "type": "openai"},
        {"name": "second", "type": "ollama"},
    ]

    assert select_runtime_provider_config(raw=providers, provider="second") == providers[1]
    assert select_runtime_provider_config(raw=providers, provider="missing") == providers[0]


def test_instantiate_runtime_adapter_preserves_runtime_fallback_sequence():
    class _Adapter:
        def __init__(self, *args, **kwargs):
            if kwargs:
                raise TypeError("kwargs unsupported")
            if args and isinstance(args[0], str):
                self.source = args[0]
                return
            raise TypeError("needs string")

    adapter = instantiate_runtime_adapter(
        provider_config={"type": "openai", "base_url": "https://api.example.com"},
        providers_config_path=None,
        resolve_adapter_class=lambda **_kwargs: (_Adapter, None),
        instantiate_adapter=lambda **_kwargs: (None, "failed"),
        normalize_models_for_provider=lambda provider: list(provider.get("models", [])),
        camelize=lambda text: text.title(),
    )

    assert adapter is not None
    assert adapter.source == "https://api.example.com"


def test_prepare_call_extra_args_injects_noop_for_proxy_only():
    messages_with_tools = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
    ]
    proxy_args = prepare_call_extra_args(
        kwargs={},
        tools=None,
        is_proxy_adapter=lambda _adapter: True,
        adapter=object(),
        messages=messages_with_tools,
    )
    non_proxy_args = prepare_call_extra_args(
        kwargs={},
        tools=None,
        is_proxy_adapter=lambda _adapter: False,
        adapter=object(),
        messages=messages_with_tools,
    )

    assert proxy_args["tools"][0]["function"]["name"] == "_noop"
    assert "tools" not in non_proxy_args


def test_prepare_call_extra_args_noop_skipped_without_tool_history():
    messages_no_tools = [{"role": "user", "content": "hello"}]
    result = prepare_call_extra_args(
        kwargs={},
        tools=None,
        is_proxy_adapter=lambda _adapter: True,
        adapter=object(),
        messages=messages_no_tools,
    )
    assert "tools" not in result


def test_call_adapter_with_fallbacks_prefers_chat_and_consumes_stream():
    class _Response:
        def iter_lines(self):
            return iter(())

    class _Adapter:
        def chat(self, messages, model=None, stream=False, format_json=False, **kwargs):
            return _Response()

    async def _run_with_correlation(_loop, _executor, fn):
        return fn()

    async def _run():
        result = await call_adapter_with_fallbacks(
            adapter=_Adapter(),
            messages=[{"role": "user", "content": "hi"}],
            model="test-model",
            stream=True,
            format_json=False,
            call_extra_args={},
            run_with_correlation=_run_with_correlation,
            consume_sse_stream=lambda _response, _model: "stream text",
        )
        assert result == {"ok": True, "text": "stream text", "streamed": True}

    asyncio.run(_run())


def test_call_adapter_with_fallbacks_falls_back_to_positional_generate():
    class _Adapter:
        def generate(self, messages, model=None, stream=False, format_json=False, **kwargs):
            if model is not None:
                raise TypeError("positional only")
            return {"ok": True, "count": len(messages)}

    async def _run_with_correlation(_loop, _executor, fn):
        return fn()

    async def _run():
        result = await call_adapter_with_fallbacks(
            adapter=_Adapter(),
            messages=[{"role": "user", "content": "hi"}],
            model="test-model",
            stream=False,
            format_json=False,
            call_extra_args={},
            run_with_correlation=_run_with_correlation,
            consume_sse_stream=lambda _response, _model: "unused",
        )
        assert result == {"ok": True, "count": 1}

    asyncio.run(_run())
