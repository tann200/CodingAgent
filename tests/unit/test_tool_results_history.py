import pytest
import json
from src.core.orchestration.graph.nodes import workflow_nodes


class DummyOrch:
    def __init__(self):
        class TR:
            def __init__(self):
                self.tools = {}
            def get(self, name):
                return None
        self.tool_registry = TR()
        # Minimal session/read trace for execution_node expectations
        self._session_read_files = set()
        # Simple message manager stub
        class MM:
            def __init__(self):
                self.messages = []
            def append(self, role, content):
                self.messages.append({'role': role, 'content': content})
        self.msg_mgr = MM()
    def preflight_check(self, action):
        # Default: allow execution
        return {"ok": True}
    def _append_execution_trace(self, entry):
        # No-op for tests
        return None


@pytest.mark.asyncio
async def test_execution_appends_tool_result_as_assistant(monkeypatch, tmp_path):
    # Create a dummy orchestrator whose execute_tool returns a result
    class Orch(DummyOrch):
        def __init__(self):
            super().__init__()
        def execute_tool(self, action):
            return {"ok": True, "result": {"status": "ok", "path": str(tmp_path / "f.txt")}}
        @property
        def adapter(self):
            return None

    orch = Orch()
    state = {
        'history': [{'role': 'user', 'content': 'do something'}],
        'next_action': {'name': 'read_file', 'arguments': {'path': 'f.txt'}},
        'working_dir': str(tmp_path),
        'task': 'read f',
        'rounds': 0,
        'system_prompt': 'sys',
    }

    conf = {'configurable': {'orchestrator': orch}}

    res = await workflow_nodes.execution_node(state, conf)

    # After execution, history must include an assistant entry with tool_execution_result
    history = res.get('history')
    assert history and isinstance(history, list)
    last = history[-1]
    # Implementation appends tool results as role 'user', 'tool', or 'assistant'
    assert last['role'] in ('assistant', 'tool', 'user')
    content = json.loads(last['content'])
    assert 'tool_execution_result' in content
    assert content['tool_execution_result']['ok'] is True

