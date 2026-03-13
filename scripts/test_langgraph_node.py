#!/usr/bin/env python3
import os
import sys
# ensure repo root is on sys.path so `import src...` works when running the script directly
ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import json
from src.core.orchestration.langgraph_node import SimpleAgentRunner

class MockAdapter:
    def __init__(self, responses):
        # responses is an iterator of dicts to return on each chat call
        self._iter = iter(responses)
    def chat(self, history, stream=False, format_json=False):
        try:
            return next(self._iter)
        except StopIteration:
            return None
    def extract_tool_calls(self, resp):
        # If resp contains 'tool_call' or message.function_call, normalize and return
        if not resp:
            return []
        # Ollama-style tool_call in message content
        msg = resp.get('message') if isinstance(resp, dict) else None
        calls = []
        # check for top-level response.tool_call
        if isinstance(resp, dict) and resp.get('response') and isinstance(resp.get('response'), dict) and resp['response'].get('tool_call'):
            calls.append(resp['response'].get('tool_call'))
        # check message.tool_calls
        if isinstance(msg, dict) and msg.get('tool_calls'):
            calls.extend(msg.get('tool_calls'))
        return calls

# Case 1: OpenAI-style function_call
responses1 = [
    # First call: assistant asks to call 'echo' with JSON string arguments
    {
        'message': {'role':'assistant', 'content':'', 'function_call': {'name':'echo', 'arguments':'{"text":"hello"}' }},
        'response': None
    },
    # Second call after tool result: assistant returns final JSON in response
    {
        'message': {'role':'assistant', 'content':'',},
        'response': {'summary':'ok','explanation':'echoed'},
    }
]

adapter1 = MockAdapter(responses1)
runner1 = SimpleAgentRunner(adapter1, tools={'echo': lambda text: {'echoed': text}})
messages = [{'role':'system','content':'sys'},{'role':'user','content':'call tool'}]
out1 = runner1.run(messages)
print('--- Test 1 ---')
print(json.dumps(out1, indent=2))

# Case 2: Already-valid parsed JSON
responses2 = [
    {
        'message': {'role':'assistant','content':''},
        'response': {'summary':'S','explanation':'E'}
    }
]
adapter2 = MockAdapter(responses2)
runner2 = SimpleAgentRunner(adapter2, tools={})
out2 = runner2.run(messages)
print('--- Test 2 ---')
print(json.dumps(out2, indent=2))
