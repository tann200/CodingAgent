from src.core.inference.llm_client import LLMClient
from src.core.inference.telemetry import with_telemetry

class DummyAdapter(LLMClient):
    @with_telemetry
    def generate(self, messages, model=None, stream=False, timeout=None, provider=None, **kwargs):
        # Return standard mock normalized payload
        return {
            "ok": True,
            "provider": "dummy",
            "model": model or "dummy-model",
            "latency": 0.0,
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "choices": [{"message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}],
            "raw": {"test": "data"}
        }

def test_llm_client_contract_and_telemetry():
    adapter = DummyAdapter()
    
    messages = [{"role": "user", "content": "Hi"}]
    res = adapter.generate(messages, model="test-model")
    
    assert res["ok"] is True
    assert res["provider"] == "dummy"
    assert res["model"] == "test-model"
    assert res["prompt_tokens"] == 10
    assert res["completion_tokens"] == 20
    assert res["total_tokens"] == 30
    assert "choices" in res
    assert res["choices"][0]["message"]["content"] == "Hello"
    
    # Check that latency was injected by with_telemetry
    assert "latency" in res
    assert isinstance(res["latency"], float)
    assert res["latency"] > 0.0
