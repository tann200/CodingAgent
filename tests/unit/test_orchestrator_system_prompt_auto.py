import pytest
from types import SimpleNamespace

from src.core.orchestration.orchestrator import Orchestrator

pytestmark = pytest.mark.skip(reason="Requires live LLM backend - run individually with RUN_INTEGRATION=1")


def test_orchestrator_inserts_system_prompt(tmp_path, monkeypatch):
    adapter = SimpleNamespace()
    adapter.provider = {'name': 'testprov'}
    adapter.models = ['small-7b']

    async def fake_call_model(messages, provider=None, model=None, stream=False, format_json=False, tools=None, **kw):
        return {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}

    monkeypatch.setattr('src.core.inference.llm_manager._call_model_internal', fake_call_model)
    monkeypatch.setattr('src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync', lambda: None)
    monkeypatch.setattr('src.core.orchestration.orchestrator.Orchestrator._background_model_check', lambda self: None)

    orch = Orchestrator(adapter=adapter, working_dir=str(tmp_path), allow_external_working_dir=True, message_max_tokens=8000)
    orch.msg_mgr.clear()
    _ = orch.run_agent_once(None, [{'role': 'user', 'content': 'hello'}], {})
    msgs = orch.msg_mgr.all()
    assert msgs, 'MessageManager should contain messages after run_agent_once'
    assert msgs[0].get('role') == 'system', 'First message should be system prompt set by orchestrator'
    content = msgs[0].get('content', '')
    assert 'operational' in content.lower()


def test_system_prompt_replaced_when_different(tmp_path, monkeypatch):
    adapter = SimpleNamespace()
    adapter.provider = {'name': 'testprov'}
    adapter.models = ['small-7b']

    async def fake_call_model(messages, provider=None, model=None, stream=False, format_json=False, tools=None, **kw):
        return {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}

    monkeypatch.setattr('src.core.inference.llm_manager._call_model_internal', fake_call_model)
    monkeypatch.setattr('src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync', lambda: None)
    monkeypatch.setattr('src.core.orchestration.orchestrator.Orchestrator._background_model_check', lambda self: None)

    orch = Orchestrator(adapter=adapter, working_dir=str(tmp_path), allow_external_working_dir=True, message_max_tokens=8000)
    orch.msg_mgr.clear()
    orch.msg_mgr.append('system', 'OLD PROMPT')
    orch.run_agent_once(None, [{'role': 'user', 'content': 'hello again'}], {})
    msgs = orch.msg_mgr.all()
    assert msgs[0].get('role') == 'system'
    assert msgs[0].get('content') != 'OLD PROMPT'
    content = msgs[0].get('content', '')
    assert 'operational' in content.lower()
