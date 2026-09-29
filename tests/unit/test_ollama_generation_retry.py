"""Regression tests for Ollama non-streaming generation retry.

Ollama returns a retryable 5xx while a model is paged into VRAM and drops the
connection under host memory pressure. Both are transient, so a single failure
must not cost the user the whole turn. The streaming path is deliberately NOT
retried (replaying a partially consumed stream duplicates output).
"""

import time
import unittest
from unittest.mock import MagicMock, mock_open, patch

import requests

from src.core.inference.adapters.ollama_adapter import (
    _MAX_GENERATION_ATTEMPTS,
    OllamaAdapter,
)

_CONFIG = '{"base_url": "http://127.0.0.1:11434", "models": ["qwen3.5:9b"]}'


def _adapter() -> OllamaAdapter:
    with patch("pathlib.Path.read_text", return_value=_CONFIG), patch(
        "builtins.open", new_callable=mock_open, read_data=_CONFIG
    ):
        return OllamaAdapter("dummy_path")


def _response(status: int) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    return resp


class TestOllamaGenerationRetry(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = _adapter()
        # Retry sleeps are real; collapse them so tests stay fast.
        patcher = patch("src.core.inference.adapters.ollama_adapter.time.sleep")
        self.addCleanup(patcher.stop)
        self.sleep = patcher.start()

    def test_success_on_first_attempt_makes_one_request(self) -> None:
        ok = _response(200)
        with patch.object(self.adapter, "_call_requests", return_value=ok) as call:
            out = self.adapter._post_generation_with_retry("http://x/api/generate", {"p": 1})
        self.assertIs(out, ok)
        self.assertEqual(call.call_count, 1)
        self.sleep.assert_not_called()

    def test_retries_5xx_then_succeeds(self) -> None:
        ok = _response(200)
        with patch.object(
            self.adapter,
            "_call_requests",
            side_effect=[_response(503), _response(500), ok],
        ) as call:
            out = self.adapter._post_generation_with_retry("http://x/api/generate", {"p": 1})
        self.assertIs(out, ok)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_gives_up_after_max_attempts_and_returns_last_response(self) -> None:
        with patch.object(
            self.adapter, "_call_requests", return_value=_response(502)
        ) as call:
            out = self.adapter._post_generation_with_retry("http://x/api/generate", {"p": 1})
        self.assertEqual(call.call_count, _MAX_GENERATION_ATTEMPTS)
        self.assertEqual(out.status_code, 502)
        self.assertEqual(self.sleep.call_count, _MAX_GENERATION_ATTEMPTS - 1)

    def test_non_retryable_status_is_not_retried(self) -> None:
        bad = _response(400)
        with patch.object(self.adapter, "_call_requests", return_value=bad) as call:
            out = self.adapter._post_generation_with_retry("http://x/api/generate", {"p": 1})
        self.assertIs(out, bad)
        self.assertEqual(call.call_count, 1)
        self.sleep.assert_not_called()

    def test_retries_transient_connection_error(self) -> None:
        ok = _response(200)
        with patch.object(
            self.adapter,
            "_call_requests",
            side_effect=[requests.exceptions.ConnectionError("reset"), ok],
        ) as call:
            out = self.adapter._post_generation_with_retry("http://x/api/generate", {"p": 1})
        self.assertIs(out, ok)
        self.assertEqual(call.call_count, 2)

    def test_non_retryable_exception_propagates_immediately(self) -> None:
        with patch.object(
            self.adapter,
            "_call_requests",
            side_effect=requests.exceptions.InvalidURL("bad url"),
        ) as call:
            with self.assertRaises(requests.exceptions.InvalidURL):
                self.adapter._post_generation_with_retry("http://x/api/generate", {"p": 1})
        self.assertEqual(call.call_count, 1)
        self.sleep.assert_not_called()

    def test_never_requests_streaming(self) -> None:
        """Streaming must be off: the retry contract only covers non-streaming."""
        with patch.object(self.adapter, "_call_requests", return_value=_response(200)) as call:
            self.adapter._post_generation_with_retry("http://x/api/generate", {"p": 1})
        self.assertIs(call.call_args.kwargs["stream"], False)

    def test_uses_shared_resilience_timeout_by_default(self) -> None:
        with patch.object(self.adapter, "_call_requests", return_value=_response(200)) as call:
            self.adapter._post_generation_with_retry("http://x/api/generate", {"p": 1})
        self.assertEqual(call.call_args.kwargs["timeout"], 120.0)

    def test_backoff_is_jittered_and_capped(self) -> None:
        """The wait must come from the shared capped-jitter policy, not a raw 2**n."""
        from src.core.inference.adapters.ollama_adapter import _MAX_GENERATION_BACKOFF

        # Unpatch sleep so the real wait is observed; two attempts, one wait.
        self.sleep.stop()
        self.addCleanup(lambda: None)
        with patch.object(self.adapter, "_call_requests", return_value=_response(500)):
            start = time.monotonic()
            self.adapter._post_generation_with_retry(
                "http://x/api/generate", {"p": 1}, max_attempts=2
            )
            elapsed = time.monotonic() - start
        self.assertLessEqual(elapsed, _MAX_GENERATION_BACKOFF + 5.0)

    def test_permanent_malformed_request_error_is_not_retried(self) -> None:
        """`is_retryable_exception` treats every status-less error as transient;
        permanent `requests` config errors must still fail fast."""
        for exc_type in (
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
            requests.exceptions.TooManyRedirects,
        ):
            with self.subTest(exc=exc_type.__name__):
                with patch.object(
                    self.adapter, "_call_requests", side_effect=exc_type("boom")
                ) as call:
                    with self.assertRaises(exc_type):
                        self.adapter._post_generation_with_retry(
                            "http://x/api/generate", {"p": 1}
                        )
                self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
