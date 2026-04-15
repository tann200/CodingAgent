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
