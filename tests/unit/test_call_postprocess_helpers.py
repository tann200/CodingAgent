import asyncio

from src.core.inference.call_postprocess import (
    attempt_model_fallback,
    is_error_result,
    publish_llm_response_hook,
    record_token_usage,
    update_circuit_breaker_for_result,
)


class _Breaker:
    def __init__(self):
        self.successes = 0
        self.failures = 0

    def record_success(self):
        self.successes += 1

    def record_failure(self):
        self.failures += 1


class _Monitor:
    def __init__(self):
        self.calls = []

    def record_usage(self, session_id, prompt_tokens, completion_tokens, total_tokens):
        self.calls.append((session_id, prompt_tokens, completion_tokens, total_tokens))


class _HookRegistry:
    def __init__(self):
        self.calls = []

    def call(self, hook_name, payload):
        self.calls.append((hook_name, payload))


def test_is_error_result_detects_top_level_and_meta_errors():
    assert is_error_result({"ok": False}) is True
    assert is_error_result({"error": "boom"}) is True
    assert is_error_result({"meta": {"error": "bad"}}) is True
    assert is_error_result({"ok": True}) is False


def test_attempt_model_fallback_returns_successful_alternative():
    async def _run():
        calls = []

        async def _get_available_models(_base_url, _api_key, _provider):
            return ["primary", "backup"]

        async def _call_model_internal(messages, provider, model, stream, format_json, tools, **kwargs):
            calls.append(model)
            if model == "backup":
                return {"ok": True, "text": "fallback ok"}
            return {"ok": False, "error": "broken"}

        success_calls = []
        result = await attempt_model_fallback(
            enabled=True,
            current_result={"ok": False, "error": "broken"},
            current_model="primary",
            provider="openai",
            messages=[{"role": "user", "content": "hi"}],
            stream=False,
            format_json=False,
            tools=None,
            kwargs={},
            max_fallbacks=2,
            get_available_models=_get_available_models,
            call_model_internal=_call_model_internal,
            on_success=lambda: success_calls.append(True),
        )

        assert result == {"ok": True, "text": "fallback ok"}
        assert calls == ["backup"]
        assert success_calls == [True]

    asyncio.run(_run())


def test_update_circuit_breaker_for_result_records_failure_and_success():
    breaker = _Breaker()

    update_circuit_breaker_for_result(
        provider_key="openai",
        result={"ok": False, "error": "bad"},
        get_circuit_breaker=lambda _provider: breaker,
    )
    update_circuit_breaker_for_result(
        provider_key="openai",
        result={"ok": True},
        get_circuit_breaker=lambda _provider: breaker,
    )

    assert breaker.failures == 1
    assert breaker.successes == 1


def test_record_token_usage_records_top_level_usage():
    monitor = _Monitor()

    record_token_usage(
        session_id="session-1",
        result={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        get_token_budget_monitor=lambda: monitor,
    )

    assert monitor.calls == [("session-1", 3, 4, 7)]


def test_publish_llm_response_hook_sends_expected_payload():
    registry = _HookRegistry()

    publish_llm_response_hook(
        enabled=True,
        hook_registry=registry,
        hook_name="llm.response",
        result={"ok": True, "text": "hello"},
        model="gpt-4o",
        provider="openai",
    )

    assert registry.calls == [
        (
            "llm.response",
            {
                "content": "hello",
                "model": "gpt-4o",
                "provider": "openai",
                "ok": True,
            },
        )
    ]
