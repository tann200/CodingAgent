import asyncio
import pytest


async def _immediate_model(messages, **kwargs):
    # simple fake model that returns a predictable response
    return {"ok": True, "messages": messages}


async def _cancelled_model(messages, **kwargs):
    # model that sleeps and then simulates being cancelled
    await asyncio.sleep(0.1)
    raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_call_model_with_timeout_uses_injected_call_model():
    from src.core.inference.llm_helpers import call_model_with_timeout

    state = {"history": [], "rounds": 0, "session_id": "s1"}
    early, resp = await call_model_with_timeout(
        messages=[{"role": "user", "content": "hi"}],
        provider=None,
        model="m",
        state=state,
        orchestrator=None,
        llm_kwargs={},
        call_model_fn=_immediate_model,
    )

    assert early is None
    assert resp["ok"] is True
    assert resp["messages"][0]["content"] == "hi"


@pytest.mark.asyncio
async def test_call_model_with_timeout_cancelled_error_propagates():
    from src.core.inference.llm_helpers import call_model_with_timeout

    state = {"history": [], "rounds": 0, "session_id": "s2"}
    early, resp = await call_model_with_timeout(
        messages=[{"role": "user", "content": "bye"}],
        provider=None,
        model="m",
        state=state,
        orchestrator=None,
        llm_kwargs={},
        call_model_fn=_cancelled_model,
    )

    # when the task is cancelled, call_model_with_timeout should return an early
    # result (first element) and None for the resp
    assert early is not None
    assert isinstance(early, dict)
    assert early.get("errors") == ["canceled"]
    assert resp is None


@pytest.mark.asyncio
async def test_call_model_with_timeout_honors_streaming_flag(monkeypatch):
    from src.core.inference.llm_helpers import call_model_with_timeout
    import src.core.inference.llm_helpers as llm_helpers

    # Force streaming enabled
    monkeypatch.setattr(llm_helpers, "_STREAMING_ENABLED", True)

    captured_kwargs = {}

    async def _mock_call(messages, **kwargs):
        captured_kwargs.update(kwargs)
        return {"ok": True}

    state = {"history": [], "rounds": 0, "session_id": "s3"}
    await call_model_with_timeout(
        messages=[{"role": "user", "content": "test"}],
        provider=None,
        model="m",
        state=state,
        orchestrator=None,
        llm_kwargs={},
        call_model_fn=_mock_call,
    )

    assert captured_kwargs.get("stream") is True

    # Force streaming disabled
    monkeypatch.setattr(llm_helpers, "_STREAMING_ENABLED", False)
    captured_kwargs.clear()

    await call_model_with_timeout(
        messages=[{"role": "user", "content": "test"}],
        provider=None,
        model="m",
        state=state,
        orchestrator=None,
        llm_kwargs={},
        call_model_fn=_mock_call,
    )

    assert captured_kwargs.get("stream") is False


@pytest.mark.asyncio
async def test_call_model_with_timeout_forwards_tools():
    from src.core.inference.llm_helpers import call_model_with_timeout

    captured_kwargs = {}

    async def _mock_call(messages, **kwargs):
        captured_kwargs.update(kwargs)
        return {"ok": True}

    state = {"history": [], "rounds": 0, "session_id": "s4"}
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    await call_model_with_timeout(
        messages=[{"role": "user", "content": "test"}],
        provider=None,
        model="m",
        state=state,
        orchestrator=None,
        llm_kwargs={},
        tools=tools,
        call_model_fn=_mock_call,
    )

    assert captured_kwargs.get("tools") == tools
