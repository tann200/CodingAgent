"""mock_adapter.py — Deterministic mock LLM adapter for tests.

S0-C: Provides ``MockAdapter`` — a fully deterministic LLM adapter that can be
injected into any test that uses the real graph nodes without running a live
provider.  This unblocks CI for all integration-style tests.

Usage::

    from src.core.inference.adapters.mock_adapter import MockAdapter

    adapter = MockAdapter(responses=["Hello world", '{"tool": "read_file", "args": {}}'])
    # First call → "Hello world", second call → the tool JSON, then repeats last.

    # With callables:
    adapter = MockAdapter(responses=[lambda msgs: "You asked: " + msgs[-1]["content"]])

    # Strict mode — raises StopIteration when script is exhausted:
    adapter = MockAdapter(responses=["only one"], strict=True)

``conftest.py`` fixture::

    @pytest.fixture
    def mock_llm_adapter(monkeypatch):
        def _factory(responses):
            adapter = MockAdapter(responses=responses)
            monkeypatch.setattr("src.core.inference.llm_manager.get_adapter", lambda: adapter)
            return adapter
        return _factory
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, List, Optional, Union

ResponseEntry = Union[str, Callable[[List[Dict[str, Any]]], str]]


class MockAdapter:
    """Deterministic mock LLM adapter driven by a response script.

    Attributes:
        responses:  Ordered list of strings or callables.  When a callable is
                    given it receives the full messages list and should return
                    a string.
        strict:     If True, raises ``StopIteration`` when the script is
                    exhausted.  If False (default), the last entry is repeated.
        call_count: Number of ``generate()`` calls made so far.
    """

    REQUIRES_API_KEY: bool = False
    name: str = "mock"
    default_model: str = "mock-model"
    models: List[str] = ["mock-model"]
    # ``provider`` dict mirrors what real adapters expose so the Orchestrator
    # can read provider name / type without special-casing the mock.
    provider: Dict[str, str] = {"name": "mock", "type": "mock"}

    def __init__(
        self,
        responses: Optional[List[ResponseEntry]] = None,
        strict: bool = False,
    ) -> None:
        self._responses: List[ResponseEntry] = list(responses or [""])
        self._strict = strict
        self.call_count: int = 0
        self.call_log: List[List[Dict[str, Any]]] = []  # record of all messages lists

    # ------------------------------------------------------------------
    # Public interface matching OpenAICompatibleAdapter.generate()
    # ------------------------------------------------------------------

    def generate(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        provider: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Return the next scripted response as a normalised payload."""
        # Record call for inspection in tests
        self.call_log.append(list(messages))
        text = self._next_response(messages)
        self.call_count += 1

        if stream:
            # Minimal streaming response — wraps text as a single chunk generator
            def _stream_gen() -> Iterator[str]:
                yield text

            return {
                "ok": True,
                "provider": "mock",
                "model": model or self.default_model,
                "latency": 0.0,
                "prompt_tokens": len(text) // 4,
                "completion_tokens": len(text) // 4,
                "total_tokens": len(text) // 2,
                "choices": [],
                "raw": _stream_gen(),
            }

        return {
            "ok": True,
            "provider": "mock",
            "model": model or self.default_model,
            "latency": 0.0,
            "prompt_tokens": len(text) // 4,
            "completion_tokens": len(text) // 4,
            "total_tokens": len(text) // 2,
            "content": text,
            "choices": [
                {
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }

    # Compatibility properties expected by ApiClientProtocol
    @property
    def model_name(self) -> str:
        return self.default_model

    @property
    def provider_name(self) -> str:
        return self.provider.get("name", "mock")

    def get_models_from_api(self) -> Dict[str, Any]:
        """Return a minimal models dict so provider detection works."""
        return {"ok": True, "models": [self.default_model], "provider": "mock"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_response(self, messages: List[Dict[str, Any]]) -> str:
        idx = self.call_count
        if idx < len(self._responses):
            entry = self._responses[idx]
        elif self._strict:
            raise StopIteration(
                f"MockAdapter: script exhausted after {len(self._responses)} calls"
            )
        else:
            entry = self._responses[-1]

        if callable(entry):
            return str(entry(messages))
        return str(entry)

    def reset(self) -> None:
        """Reset call counter and log so the adapter can be reused."""
        self.call_count = 0
        self.call_log.clear()
