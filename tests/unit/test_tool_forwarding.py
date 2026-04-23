from __future__ import annotations

import asyncio

from src.core.inference.llm_manager import get_provider_manager, call_model


class FakeProxyAdapterForward:
    def __init__(self):
        self.name = "litellm"
        self.provider = {"name": "LiteLLM", "type": "litellm"}
        self.default_model = "gpt-4o"
        self.last_call_kwargs = None

    def generate(self, messages, model=None, stream=False, format_json=False, **kwargs):
        # record kwargs the manager passed in
        self.last_call_kwargs = dict(kwargs or {})
        return {"ok": True, "choices": [{"message": {"content": "ok"}}]}


def test_explicit_tools_forwarded(monkeypatch):
    mgr = get_provider_manager()
    proxy = FakeProxyAdapterForward()
    mgr._providers["litellm"] = proxy
    mgr._initialized = True

    messages = [{"role": "user", "content": "call a tool"}]
    explicit_tools = [
        {
            "type": "function",
            "function": {
                "name": "do_it",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    resp = asyncio.run(call_model(messages, provider="litellm", tools=explicit_tools))
    assert resp.get("ok") is True
    assert proxy.last_call_kwargs is not None
    # The adapter should receive the exact same tools list (no noop injected)
    assert proxy.last_call_kwargs.get("tools") is explicit_tools
