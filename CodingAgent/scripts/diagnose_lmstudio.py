"""Diagnostic helper to reproduce LM Studio requests and capture responses.

This script now requires LM_STUDIO_URL to be provided via environment variable
or as the first CLI argument. This prevents accidentally targeting Ollama's
`/api` endpoint when you intend to test LM Studio's OpenAI-compatible `/v1` API.

Run this while you have `lms log stream` running in a separate terminal. It will:
- query /v1/models
- pick a model (prefers qwen models and prefers ones containing '9b')
- POST a structured request to /v1/chat/completions requesting JSON via response_format
- print full response (status, headers, body)

Usage:
LM_STUDIO_URL should point to your LM Studio base (example: http://localhost:1234/v1)
You can pass it as an env var or as the first CLI arg.

Examples:
# run LM Studio logs in terminal A:
#   npx lmstudio install-cli  # if CLI not installed
#   lms log stream
# then in terminal B run:
#   LM_STUDIO_URL=http://localhost:1234/v1 python3 scripts/diagnose_lmstudio.py
# or
#   python3 scripts/diagnose_lmstudio.py http://localhost:1234/v1

"""
import os
import sys
import requests
import json
from pathlib import Path

# Prefer explicit LM_STUDIO_URL via env var or CLI arg
LM_STUDIO_URL = os.getenv('LM_STUDIO_URL') or (sys.argv[1] if len(sys.argv) > 1 else None)
if not LM_STUDIO_URL:
    print("ERROR: LM_STUDIO_URL is required. Set the environment variable or pass it as the first argument.")
    print("Example: LM_STUDIO_URL=http://localhost:1234/v1 python3 scripts/diagnose_lmstudio.py")
    sys.exit(2)

v1_base = LM_STUDIO_URL.rstrip('/')
print('Using v1 base:', v1_base)

try:
    r = requests.get(f"{v1_base}/models", timeout=10)
    print('/v1/models status', r.status_code)
    print('models list (truncated):')
    print(r.text[:2000])
    data = r.json()
except Exception as e:
    print('Failed to fetch /v1/models:', e)
    sys.exit(1)

# Build candidate list preferring qwen and '9b'
candidates = []
for m in data.get('data', []):
    if isinstance(m, dict) and m.get('id'):
        candidates.append(m.get('id'))

# Prefer models containing '9b' (common for large models), then 'latest', then any qwen
def sort_key(mid: str):
    score = 0
    if '9b' in mid:
        score -= 10
    if 'latest' in mid:
        score -= 5
    if mid.startswith('qwen'):
        score -= 1
    return score

candidates = sorted(candidates, key=sort_key)

if not candidates:
    print('No models found in /v1/models response. Aborting.')
    sys.exit(1)

print('Model candidates (ordered):')
for c in candidates:
    print(' -', c)

# read system prompt
try:
    system = Path('agent-brain/system_prompt_coding.mdmd').read_text(encoding='utf-8')
except Exception:
    system = "You are a helpful coding assistant."
    print('Warning: could not read system prompt file, using fallback.')

user_msg = 'Please respond in JSON with keys summary and explanation and include a tool_action field to simulate tool calling.'

payload_template = {
    'messages': [
        {'role':'system','content': system},
        {'role':'user','content': user_msg},
    ],
    'response_format': {
        'type': 'json_schema',
        'json_schema': {
            'name': 'agent_response',
            'schema': {
                'type': 'object',
                'properties': {
                    'summary': {'type':'string'},
                    'explanation': {'type':'string'},
                    'tool_action': {'type':'object'},
                },
                'required': ['summary','explanation']
            }
        }
    },
    'stream': False,
}

# Try each candidate until one succeeds or all fail
for model_id in candidates:
    payload = dict(payload_template)
    payload['model'] = model_id
    print(f"\nTrying model: {model_id} -> POST {v1_base}/chat/completions")
    try:
        resp = requests.post(f"{v1_base}/chat/completions", json=payload, timeout=120)
    except Exception as e:
        print('POST request failed:', e)
        continue

    print('/v1/chat/completions status', resp.status_code)
    print('content-type:', resp.headers.get('content-type'))
    body = resp.text or ''
    print('\nBody (first 8000 chars):\n')
    print(body[:8000])

    # If LM Studio returned an error indicating model failed to load, show actionable guidance
    if resp.status_code >= 500:
        try:
            err = resp.json().get('error', {})
            msg = err.get('message') if isinstance(err, dict) else None
            if msg and 'failed to load' in msg.lower():
                print('\nDetected model load failure for', model_id)
                print('This usually means the model is not loaded or resources are insufficient.')
                print('Actionable steps:')
                print(' - Open the LM Studio app and load the model into memory via the Developer or Chat tab.')
                print(' - Or use the lms CLI to load the model, for example:')
                print("     lms load ", model_id)
                print(' - If using Ollama, ensure the model is pulled and available (e.g. ollama pull/run).')
                # continue to next candidate
                continue
        except Exception:
            pass

    # If we get a 200-ish response, try parsing JSON and show structure
    if 200 <= resp.status_code < 300:
        try:
            parsed = resp.json()
            print('\nParsed JSON keys:', list(parsed.keys()))
            print(json.dumps(parsed, indent=2)[:8000])
        except Exception as e:
            print('\nCould not parse JSON response:', e)
            print('Raw body snippet:\n', body[:2000])
        print('\nSucceeded with model:', model_id)
        break

print('\nDone. If you have `lms log stream` running in another terminal, paste both outputs here (logs + this script output).')

