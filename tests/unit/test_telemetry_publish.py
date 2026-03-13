class DummyEventBus:
    def __init__(self):
        self.events = []
    def publish(self, name, payload):
        self.events.append((name, payload))

from src.core.inference.adapter_wrappers import AdapterWrapper


def test_publish_model_response_on_wrapper():
    class MockAdapter:
        def chat(self, messages, model=None, stream=False, format_json=False, **kwargs):
            return {
                'model': model or 'test-model',
                'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}
            }

    bus = DummyEventBus()
    mock = MockAdapter()
    wrapper = AdapterWrapper(mock, provider_name='lm_studio', event_bus=bus)
    out = wrapper.generate([{'role':'user','content':'hi'}], model='test-model')
    # ensure wrapper returned normalized payload
    assert out.get('ok') is True
    # ensure telemetry event was published
    assert any(e[0] == 'model.response' for e in bus.events)
    # inspect payload keys
    evt = [e for e in bus.events if e[0] == 'model.response'][0]
    assert 'provider' in evt[1]
    assert 'model' in evt[1]
    assert 'prompt_tokens' in evt[1]
    assert 'completion_tokens' in evt[1]
    assert 'latency' in evt[1]

