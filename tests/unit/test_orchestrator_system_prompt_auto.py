from types import SimpleNamespace

from src.core.orchestration.orchestrator import Orchestrator


def test_orchestrator_inserts_system_prompt(tmp_path, monkeypatch):
    adapter = SimpleNamespace()
    adapter.provider = {'name': 'testprov'}
    adapter.models = ['small-7b']

    # Monkeypatch call_model to prevent network calls
    async def fake_call_model(messages, provider=None, model=None, stream=False, format_json=False, tools=None):
        # Return a trivial assistant message
        return {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}

    monkeypatch.setattr('src.core.llm_manager.call_model', fake_call_model)

    orch = Orchestrator(adapter=adapter, working_dir=str(tmp_path), allow_external_working_dir=True, message_max_tokens=8000)
    # ensure empty history
    orch.msg_mgr.clear()
    # Run agent once (system prompt should be auto-loaded and set)
    res = orch.run_agent_once(None, [{'role': 'user', 'content': 'hello'}], {})
    # check message manager has a system prompt at top
    msgs = orch.msg_mgr.all()
    assert msgs, 'MessageManager should contain messages after run_agent_once'
    assert msgs[0].get('role') == 'system', 'First message should be system prompt set by orchestrator'
    content = msgs[0].get('content', '')
    assert 'operational' in content.lower()


def test_system_prompt_replaced_when_different(tmp_path, monkeypatch):
    adapter = SimpleNamespace()
    adapter.provider = {'name': 'testprov'}
    adapter.models = ['small-7b']

    async def fake_call_model(messages, provider=None, model=None, stream=False, format_json=False, tools=None):
        return {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]}

    monkeypatch.setattr('src.core.llm_manager.call_model', fake_call_model)

    orch = Orchestrator(adapter=adapter, working_dir=str(tmp_path), allow_external_working_dir=True, message_max_tokens=8000)
    # manually insert a different system prompt
    orch.msg_mgr.clear()
    orch.msg_mgr.append('system', 'OLD PROMPT')
    # Run agent once; orchestrator should replace the top system prompt
    orch.run_agent_once(None, [{'role': 'user', 'content': 'hello again'}], {})
    msgs = orch.msg_mgr.all()
    assert msgs[0].get('role') == 'system'
    assert msgs[0].get('content') != 'OLD PROMPT'
    content = msgs[0].get('content', '')
    assert 'operational' in content.lower()

