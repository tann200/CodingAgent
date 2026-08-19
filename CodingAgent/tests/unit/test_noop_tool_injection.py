from __future__ import annotations

import asyncio

from src.core.inference.llm_manager import get_provider_manager, call_model


class FakeProxyAdapter:
    def __init__(self):
        self.name = "litellm"
        self.provider = {"name": "LiteLLM", "type": "litellm"}
        self.default_model = "gpt-4o"
        self.last_call_kwargs = None

    def generate(self, messages, model=None, stream=False, format_json=False, **kwargs):
        # record kwargs the manager passed in
        self.last_call_kwargs = dict(kwargs or {})
        return {"ok": True, "choices": [{"message": {"content": "ok"}}]}


class FakeOpenAdapter:
    def __init__(self):
        self.name = "openai"
        self.provider = {"name": "OpenAI", "type": "openai"}
        self.default_model = "gpt-4"
        self.last_call_kwargs = None

    def generate(self, messages, model=None, stream=False, format_json=False, **kwargs):
        self.last_call_kwargs = dict(kwargs or {})
        return {"ok": True, "choices": [{"message": {"content": "ok"}}]}


def test_noop_injected_for_proxy_adapter(monkeypatch):
    mgr = get_provider_manager()
    # register fake proxy adapter and mark manager initialized to avoid probing
    proxy = FakeProxyAdapter()
    mgr._providers["litellm"] = proxy
    mgr._initialized = True

    # History has tool calls → noop should be injected
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "user", "content": "call a tool"},
    ]
    # call asynchronously
    resp = asyncio.run(call_model(messages, provider="litellm", tools=None))
    assert resp.get("ok") is True
    # The adapter should have seen an injected tools arg containing our noop
    assert proxy.last_call_kwargs is not None
    tools = proxy.last_call_kwargs.get("tools")
    assert isinstance(tools, list) and len(tools) == 1
    noop = tools[0]
    assert noop.get("type") == "function"
    fn = noop.get("function")
    assert isinstance(fn, dict)
    assert fn.get("name") == "_noop"

def test_noop_not_injected_when_history_lacks_tool_calls(monkeypatch):
    """Noop should NOT be injected when history has no tool calls (e.g. compaction)."""
    mgr = get_provider_manager()
    proxy = FakeProxyAdapter()
    mgr._providers["litellm"] = proxy
    mgr._initialized = True

    messages = [{"role": "user", "content": "summarize the history"}]
    resp = asyncio.run(call_model(messages, provider="litellm", tools=None))
    assert resp.get("ok") is True
    # No history of tool calls → no noop injection
    assert proxy.last_call_kwargs is not None
    assert proxy.last_call_kwargs.get("tools") is None


def test_noop_not_injected_for_non_proxy(monkeypatch):
    mgr = get_provider_manager()
    opena = FakeOpenAdapter()
    mgr._providers["openai"] = opena
    mgr._initialized = True

    messages = [{"role": "user", "content": "call a tool"}]
    resp = asyncio.run(call_model(messages, provider="openai", tools=None))
    assert resp.get("ok") is True
    # open adapter should NOT receive injected tools when none provided
    assert opena.last_call_kwargs is not None
    assert opena.last_call_kwargs.get("tools") is None
