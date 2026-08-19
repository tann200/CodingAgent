"""Tests for OpenAICompatibleAdapter — P1-10."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(**kwargs) -> OpenAICompatibleAdapter:
    defaults = {
        "base_url": "http://localhost:1234/v1",
        "api_key": "test-key",
        "default_model": "llama-3",
        "models": ["llama-3"],
        "name": "test_adapter",
    }
    defaults.update(kwargs)
    return OpenAICompatibleAdapter(**defaults)


def _mock_response(json_data: Any = None, status_code: int = 200, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    r.json.return_value = json_data or {}
    r.headers = {}
    if status_code >= 400:
        r.raise_for_status.side_effect = requests.exceptions.HTTPError(response=r)
    else:
        r.raise_for_status.return_value = None
    return r


def _openai_chat_response(content: str = "Hello", tool_calls: list | None = None) -> dict:
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"message": msg, "finish_reason": "stop"}],
        "model": "llama-3",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


# ---------------------------------------------------------------------------
# __init__ — sanitization
# ---------------------------------------------------------------------------

class TestInit:
    def test_basic_construction(self):
        adapter = _make_adapter()
        assert adapter.base_url == "http://localhost:1234/v1"
        assert adapter.api_key == "test-key"
        assert adapter.default_model == "llama-3"
        assert adapter.name == "test_adapter"
        assert adapter.context_window == 0

    def test_empty_model_placeholder_rejected(self):
        adapter = _make_adapter(default_model="   ")
        assert adapter.default_model is None

    def test_models_list_sanitized(self):
        adapter = _make_adapter(models=["good-model", "  ", "another"])
        # Blank string should be filtered out
        assert "  " not in adapter.models
        assert "good-model" in adapter.models

    def test_no_base_url(self):
        adapter = _make_adapter(base_url=None)
        assert adapter.base_url is None

    def test_no_api_key(self):
        adapter = _make_adapter(api_key=None)
        assert adapter.api_key is None


# ---------------------------------------------------------------------------
# _compose — URL builder
# ---------------------------------------------------------------------------

class TestCompose:
    def test_v1_base_url(self):
        adapter = _make_adapter(base_url="http://localhost:1234/v1")
        url = adapter._compose("chat/completions")
        assert url == "http://localhost:1234/v1/chat/completions"

    def test_bare_host_adds_api_v1(self):
        adapter = _make_adapter(base_url="http://localhost:1234")
        url = adapter._compose("chat/completions")
        assert url == "http://localhost:1234/api/v1/chat/completions"

    def test_no_base_url_returns_none(self):
        adapter = _make_adapter(base_url=None)
        assert adapter._compose("chat/completions") is None

    def test_api_in_url_passthrough(self):
        adapter = _make_adapter(base_url="http://host/api")
        url = adapter._compose("completions")
        assert url == "http://host/api/completions"


# ---------------------------------------------------------------------------
# _models_endpoints
# ---------------------------------------------------------------------------

class TestModelsEndpoints:
    def test_v1_base_url(self):
        adapter = _make_adapter(base_url="http://localhost/v1")
        eps = adapter._models_endpoints()
        assert "http://localhost/v1/models" in eps

    def test_bare_url_returns_both_variants(self):
        adapter = _make_adapter(base_url="http://localhost:1234")
        eps = adapter._models_endpoints()
        assert len(eps) == 2

    def test_no_base_url_returns_empty(self):
        adapter = _make_adapter(base_url=None)
        assert adapter._models_endpoints() == []


# ---------------------------------------------------------------------------
# _headers
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_bearer_auth_when_api_key_set(self):
        adapter = _make_adapter(api_key="secret")
        headers = adapter._headers()
        assert headers["Authorization"] == "Bearer secret"

    def test_no_auth_header_when_no_key(self):
        adapter = _make_adapter(api_key=None)
        headers = adapter._headers()
        assert "Authorization" not in headers

    def test_content_type_always_set(self):
        adapter = _make_adapter()
        assert adapter._headers()["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# get_models_from_api
# ---------------------------------------------------------------------------

class TestGetModelsFromApi:
    def test_returns_empty_when_no_base_url(self):
        adapter = _make_adapter(base_url=None)
        result = adapter.get_models_from_api()
        assert result == {"models": []}

    def test_data_shape_openai(self):
        adapter = _make_adapter()
        resp = _mock_response({"data": [{"id": "provider/llama-3", "name": "llama-3"}]})
        with patch("requests.get", return_value=resp):
            result = adapter.get_models_from_api()
        assert len(result["models"]) == 1
        assert result["models"][0]["name"] == "llama-3"

    def test_models_shape_lmstudio(self):
        adapter = _make_adapter()
        resp = _mock_response({"models": [{"id": "llama-3", "display_name": "LLaMA 3"}]})
        with patch("requests.get", return_value=resp):
            result = adapter.get_models_from_api()
        assert result["models"][0]["display_name"] == "LLaMA 3"

    def test_bare_list(self):
        adapter = _make_adapter()
        resp = _mock_response(["model-a", "model-b"])
        with patch("requests.get", return_value=resp):
            result = adapter.get_models_from_api()
        names = [m["name"] for m in result["models"]]
        assert "model-a" in names

    def test_http_error_skips_endpoint(self):
        adapter = _make_adapter()
        resp = _mock_response(status_code=404)
        with patch("requests.get", return_value=resp):
            result = adapter.get_models_from_api()
        assert result == {"models": []}

    def test_connection_error_returns_empty(self):
        adapter = _make_adapter()
        with patch("requests.get", side_effect=requests.exceptions.ConnectionError):
            result = adapter.get_models_from_api()
        assert result == {"models": []}

    def test_short_name_extracted_from_provider_slash_model(self):
        adapter = _make_adapter()
        resp = _mock_response({"data": [{"id": "openai/gpt-4o"}]})
        with patch("requests.get", return_value=resp):
            result = adapter.get_models_from_api()
        assert result["models"][0]["name"] == "gpt-4o"


# ---------------------------------------------------------------------------
# validate_connection
# ---------------------------------------------------------------------------

class TestValidateConnection:
    def test_returns_true_on_200(self):
        adapter = _make_adapter()
        with patch("requests.get", return_value=_mock_response(status_code=200)):
            assert adapter.validate_connection() is True

    def test_returns_false_on_404(self):
        adapter = _make_adapter()
        with patch("requests.get", return_value=_mock_response(status_code=404)):
            assert adapter.validate_connection() is False

    def test_returns_false_on_no_url(self):
        adapter = _make_adapter(base_url=None)
        assert adapter.validate_connection() is False

    def test_returns_false_on_exception(self):
        adapter = _make_adapter()
        with patch("requests.get", side_effect=Exception("timeout")):
            assert adapter.validate_connection() is False


# ---------------------------------------------------------------------------
# _sanitize_kwargs
# ---------------------------------------------------------------------------

class TestSanitizeKwargs:
    def test_non_serializable_dropped(self):
        adapter = _make_adapter()
        result = adapter._sanitize_kwargs({"ok": 123, "bad": object()})
        assert "ok" in result
        assert "bad" not in result

    def test_all_serializable_passthrough(self):
        adapter = _make_adapter()
        kwargs = {"temperature": 0.7, "max_tokens": 100, "stop": ["END"]}
        result = adapter._sanitize_kwargs(kwargs)
        assert result == kwargs


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------

class TestBuildPayload:
    def test_messages_list_builds_chat_payload(self):
        adapter = _make_adapter()
        msgs = [{"role": "user", "content": "hi"}]
        payload, ep = adapter._build_payload(msgs, "llama-3", {})
        assert payload["messages"] == msgs
        assert payload["model"] == "llama-3"
        assert ep is not None

    def test_string_messages_build_responses_payload(self):
        adapter = _make_adapter()
        payload, ep = adapter._build_payload("Hello", "llama-3", {})
        assert payload["input"] == "Hello"
        assert ep is not None

    def test_no_base_url_returns_none_ep(self):
        adapter = _make_adapter(base_url=None)
        payload, ep = adapter._build_payload([], "llama-3", {})
        assert ep is None


# ---------------------------------------------------------------------------
# _apply_provider_flags
# ---------------------------------------------------------------------------

class TestApplyProviderFlags:
    def test_tools_copied_to_functions(self):
        adapter = _make_adapter()
        payload = {"tools": [{"name": "read_file"}]}
        adapter._apply_provider_flags(payload)
        assert "functions" in payload

    def test_functions_not_overwritten_if_present(self):
        adapter = _make_adapter()
        existing = [{"name": "existing"}]
        payload = {"tools": [{"name": "read_file"}], "functions": existing}
        adapter._apply_provider_flags(payload)
        assert payload["functions"] is existing

    def test_disable_thinking_injects_think_false(self):
        adapter = _make_adapter()
        adapter.provider = {"disable_thinking": True}  # type: ignore[attr-defined]
        payload: dict = {}
        adapter._apply_provider_flags(payload)
        assert payload.get("think") is False

    def test_skip_functions_compat_respects_flag(self):
        adapter = _make_adapter()
        adapter._skip_functions_compat = True  # type: ignore[attr-defined]
        payload = {"tools": [{"name": "bash"}]}
        adapter._apply_provider_flags(payload)
        assert "functions" not in payload


# ---------------------------------------------------------------------------
# _parse_error_response
# ---------------------------------------------------------------------------

class TestParseErrorResponse:
    def _make_http_error(self, body: Any) -> requests.exceptions.HTTPError:
        resp = MagicMock()
        resp.json.return_value = body
        resp.status_code = 400
        err = requests.exceptions.HTTPError()
        err.response = resp
        return err

    def test_context_overflow_detected(self):
        adapter = _make_adapter()
        body = {"error": {"message": "context_length_exceeded: input is too long"}}
        he = self._make_http_error(body)
        result = adapter._parse_error_response(he, None)
        assert result.get("context_overflow") is True

    def test_generic_error_no_overflow(self):
        adapter = _make_adapter()
        body = {"error": {"message": "rate limit exceeded"}}
        he = self._make_http_error(body)
        result = adapter._parse_error_response(he, None)
        assert result.get("context_overflow") is not True
        assert "meta" in result

    def test_string_body_overflow(self):
        adapter = _make_adapter()
        resp = MagicMock()
        resp.json.side_effect = Exception("not json")
        resp.text = "maximum context length exceeded. prompt is too long"
        resp.status_code = 400
        he = requests.exceptions.HTTPError()
        he.response = resp
        result = adapter._parse_error_response(he, resp)
        assert result.get("context_overflow") is True


# ---------------------------------------------------------------------------
# generate / _chat_internal
# ---------------------------------------------------------------------------

class TestGenerate:
    def test_successful_response(self):
        adapter = _make_adapter()
        raw = _openai_chat_response("Hello!")
        with patch.object(adapter, "_execute_with_retry", return_value=_mock_response(raw)):
            result = adapter.generate([{"role": "user", "content": "hi"}], model="llama-3")
        assert result["ok"] is True
        assert result["choices"][0]["message"]["content"] == "Hello!"
        assert result["prompt_tokens"] == 10
        assert result["completion_tokens"] == 5

    def test_no_model_returns_error(self):
        adapter = _make_adapter(models=[], default_model=None)
        result = adapter.generate([{"role": "user", "content": "hi"}])
        assert result["ok"] is False
        assert "no_model_configured" in result.get("error", "")

    def test_http_error_returns_ok_false(self):
        adapter = _make_adapter()
        resp = _mock_response(status_code=500)
        with patch.object(adapter, "_execute_with_retry", return_value=resp):
            result = adapter.generate([{"role": "user", "content": "hi"}], model="llama-3")
        assert result["ok"] is False

    def test_request_exception_returns_error(self):
        adapter = _make_adapter()
        with patch.object(adapter, "_execute_with_retry",
                          side_effect=requests.exceptions.ConnectionError):
            result = adapter.generate([{"role": "user", "content": "hi"}], model="llama-3")
        assert result["ok"] is False

    def test_stream_returns_raw_response(self):
        adapter = _make_adapter()
        fake_stream = MagicMock()
        with patch.object(adapter, "_execute_with_retry", return_value=fake_stream):
            result = adapter.generate([], model="llama-3", stream=True)
        assert result["ok"] is True
        assert result["raw"] is fake_stream

    def test_reasoning_content_fallback(self):
        """Empty content falls back to reasoning_content when present."""
        adapter = _make_adapter()
        raw = {
            "choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "my reasoning"}, "finish_reason": "stop"}],
            "model": "llama-3",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        with patch.object(adapter, "_execute_with_retry", return_value=_mock_response(raw)):
            result = adapter.generate([{"role": "user", "content": "hi"}], model="llama-3")
        assert result["choices"][0]["message"]["content"] == "my reasoning"

    def test_tool_calls_preserved_in_choices(self):
        adapter = _make_adapter()
        tcs = [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]
        raw = _openai_chat_response(tool_calls=tcs)
        with patch.object(adapter, "_execute_with_retry", return_value=_mock_response(raw)):
            result = adapter.generate([{"role": "user", "content": "hi"}], model="llama-3")
        assert "tool_calls" in result["choices"][0]

    def test_cache_tokens_extracted(self):
        adapter = _make_adapter()
        raw = _openai_chat_response()
        raw["usage"]["cache_creation_input_tokens"] = 50
        raw["usage"]["cache_read_input_tokens"] = 30
        with patch.object(adapter, "_execute_with_retry", return_value=_mock_response(raw)):
            result = adapter.generate([{"role": "user", "content": "hi"}], model="llama-3")
        assert result["cache_creation_input_tokens"] == 50
        assert result["cache_read_input_tokens"] == 30


# ---------------------------------------------------------------------------
# extract_tool_calls
# ---------------------------------------------------------------------------

class TestExtractToolCalls:
    def test_openai_nested_choices(self):
        adapter = _make_adapter()
        resp = {
            "choices": [{
                "message": {
                    "tool_calls": [
                        {"function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}
                    ]
                }
            }]
        }
        result = adapter.extract_tool_calls(resp)
        assert len(result) == 1
        assert result[0]["name"] == "read_file"

    def test_top_level_tool_calls(self):
        adapter = _make_adapter()
        resp = {
            "tool_calls": [
                {"function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
            ]
        }
        result = adapter.extract_tool_calls(resp)
        assert result[0]["name"] == "bash"

    def test_function_call_legacy(self):
        adapter = _make_adapter()
        resp = {"function_call": {"name": "glob", "arguments": '{"pattern": "*.py"}'}}
        result = adapter.extract_tool_calls(resp)
        assert result[0]["name"] == "glob"

    def test_no_tool_calls_returns_empty(self):
        adapter = _make_adapter()
        resp = {"choices": [{"message": {"content": "plain text"}}]}
        result = adapter.extract_tool_calls(resp)
        assert result == []

    def test_args_json_string_parsed(self):
        adapter = _make_adapter()
        resp = {
            "tool_calls": [
                {"function": {"name": "write_file", "arguments": '{"path": "b.py", "content": "x"}'}}
            ]
        }
        result = adapter.extract_tool_calls(resp)
        assert isinstance(result[0]["args"], dict)
        assert result[0]["args"]["path"] == "b.py"

    def test_non_dict_response_returns_empty(self):
        adapter = _make_adapter()
        assert adapter.extract_tool_calls("not a dict") == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _preprocess_messages — SYSTEM_PROMPT_DYNAMIC_BOUNDARY
# ---------------------------------------------------------------------------

class TestPreprocessMessages:
    def test_sentinel_stripped(self):
        sentinel = "---DYNAMIC_BOUNDARY---"
        msgs = [
            {"role": "system", "content": f"Static instructions\n\n{sentinel}\n\nDynamic context"},
        ]
        with patch(
            "src.core.inference.adapters.openai_compat_adapter.OpenAICompatibleAdapter._preprocess_messages",
            wraps=OpenAICompatibleAdapter._preprocess_messages,
        ):
            with patch(
                "src.core.context.context_builder.SYSTEM_PROMPT_DYNAMIC_BOUNDARY",
                sentinel,
                create=True,
            ):
                result = OpenAICompatibleAdapter._preprocess_messages(msgs)
        # The sentinel should be gone
        assert sentinel not in result[0]["content"]

    def test_non_system_messages_passthrough(self):
        msgs = [{"role": "user", "content": "hello"}]
        result = OpenAICompatibleAdapter._preprocess_messages(msgs)
        assert result == msgs


# ---------------------------------------------------------------------------
# _execute_with_retry
# ---------------------------------------------------------------------------

class TestExecuteWithRetry:
    def test_retries_on_500(self):
        adapter = _make_adapter()
        fail = _mock_response(status_code=500)
        ok = _mock_response(_openai_chat_response())
        side_effects = [fail, fail, ok]

        call_count = 0

        def _post_side_effect(*args, **kwargs):
            nonlocal call_count
            r = side_effects[min(call_count, len(side_effects) - 1)]
            call_count += 1
            return r

        with patch.object(adapter, "_safe_post", side_effect=_post_side_effect), \
             patch("time.sleep"):
            r = adapter._execute_with_retry(
                "http://localhost/v1/chat/completions", {}, stream=False
            )
        assert r.status_code == 200
        assert call_count == 3

    def test_connection_error_retried(self):
        adapter = _make_adapter()
        ok = _mock_response(_openai_chat_response())

        call_count = 0

        def _post_se(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise requests.exceptions.ConnectionError("refused")
            return ok

        with patch.object(adapter, "_safe_post", side_effect=_post_se), \
             patch("time.sleep"):
            r = adapter._execute_with_retry(
                "http://localhost/v1/chat/completions", {}, stream=False
            )
        assert r.status_code == 200

    def test_stream_returns_immediately(self):
        adapter = _make_adapter()
        fake = MagicMock()
        with patch.object(adapter, "_safe_post", return_value=fake):
            r = adapter._execute_with_retry("http://x", {}, stream=True)
        assert r is fake
