from unittest.mock import patch
import asyncio

from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter
from src.core.inference.llm_manager import call_model, get_provider_manager


def make_response(status_code=200, json_data=None, text_data=None):
    class Resp:
        def __init__(self):
            self.status_code = status_code
            self._json = json_data
            self.text = text_data or ""

        def json(self):
            if self._json is None:
                raise ValueError("no json")
            return self._json

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception("http error")

    return Resp()


def test_resolve_model_name_uses_api(monkeypatch):
    # Mock requests.get for /v1/models
    sample = {"data": [{"id": "qwen/qwen3.5-9b"}, {"id": "other/1b"}]}

    with patch("requests.get") as mock_get:
        mock_get.return_value = make_response(200, sample)
        adapter = LmStudioAdapter(base_url="http://localhost:1234/v1", models=[])
        resolved = adapter.resolve_model_name("qwen3.5-9b")
        # Prefer the canonical full id
        assert resolved == "qwen/qwen3.5-9b"


def test_chat_returns_user_message_on_model_load_error(monkeypatch):
    # Simulate LM Studio returning a model-load error body
    body = {
        "error": {
            "message": 'Failed to load model "qwen/qwen3.5-9b". Error: insufficient system resources. requires approximately 8.89 GB'
        }
    }

    def fake_post(*args, **kwargs):
        # accept any signature and return a response-like object with 400
        return make_response(400, body)

    with patch("requests.post", new=fake_post):
        adapter = LmStudioAdapter(base_url="http://localhost:1234/v1", models=[])
        messages = [
            {"role": "system", "content": "hi"},
            {"role": "user", "content": "test"},
        ]
        # use long model id when calling
        res = adapter.generate(messages, model="qwen/qwen3.5-9b")
        assert isinstance(res, dict)
        assert res.get("ok") is False
        assert "insufficient system resources" in res.get("error", "")


def test_llm_manager_fallback(monkeypatch):
    # Create a MockAdapter with chat that first returns a model-load error, then success for alt model
    class MockAdapter:
        def __init__(self):
            self.calls = []

        def get_models_from_api(self):
            return {
                "models": [
                    {"name": "qwen3.5-9b", "id": "qwen/qwen3.5-9b"},
                    {"name": "smallmodel", "id": "small/smallv1"},
                ]
            }

        def generate(self, messages, model=None, stream=False, format_json=False):
            return self.chat(messages, model, stream, format_json)

        def chat(self, messages, model=None, stream=False, format_json=False):
            self.calls.append(model)
            # expect long model ids
            if model == "qwen/qwen3.5-9b":
                raise Exception("Model not available")
            # success for smallmodel
            return {
                "ok": True,
                "message": {"role": "assistant", "content": "ok from " + str(model)},
            }

    # Register mock adapter in provider manager
    pm = get_provider_manager()
    # ensure initialized state
    pm._providers["lm_studio"] = MockAdapter()
    pm._initialized = True

    # enable fallback via env
    monkeypatch.setenv("LLM_MANAGER_ENABLE_MODEL_FALLBACK", "1")

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]

    # run the async call_model via asyncio.run with long id
    resp = asyncio.run(
        call_model(
            messages,
            provider="lm_studio",
            model="qwen/qwen3.5-9b",
            stream=False,
            format_json=False,
        )
    )
    assert isinstance(resp, dict)
    # should have retried with smallmodel
    assert "message" in resp
    assert "ok from" in resp["message"]["content"]

    # cleanup
    pm._providers.pop("lm_studio", None)
    pm._initialized = False
