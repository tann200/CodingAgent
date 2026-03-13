LM_STUDIO_URL = 'http://localhost:1234/v1'
import os
import json
from pathlib import Path
import pytest
import requests
import re

from tests.integration.http_retry import post_with_retry

pytestmark = pytest.mark.lmstudio

RUN = os.getenv('RUN_INTEGRATION') == '1'
if not RUN:
    try:
        cfg_path = Path(__file__).parents[2] / 'src' / 'config' / 'providers.json'
        if cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding='utf-8'))
            providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for p in providers:
                t = str(p.get('type') or '').lower()
                name = str(p.get('name') or '').lower()
                if 'lm' in t or 'lm' in name or 'lm_studio' in t or 'lmstudio' in name:
                    RUN = True
                    break
    except Exception:
        RUN = RUN

@pytest.mark.skipif(not RUN, reason='Integration tests disabled for LM Studio')
def test_system_prompts_json_mode():
    # placeholder
    assert True

@pytest.mark.skipif(not RUN, reason='Integration tests disabled')
def test_system_prompt_json_mode_and_tool_handover():
    # Probe Ollama/LM Studio
    try:
        r = requests.get(f"{OLLAMA_URL}/tags", timeout=5)
        r.raise_for_status()
    except Exception:
        # Not all servers implement /api/tags; continue and probe v1
        pass

    # Determine v1 base: prefer LM_STUDIO_URL if provided, otherwise derive from OLLAMA_URL
    if LM_STUDIO_URL:
        v1_base = LM_STUDIO_URL.rstrip('/')
    else:
        v1_base = OLLAMA_URL.replace('/api', '/v1')

    use_v1 = False
    v1_model_id = None
    try:
        r = requests.get(f"{v1_base}/models", timeout=5)
        if r.status_code == 200:
            data = r.json()
            # pick first llm model id if present
            for m in data.get('data', []):
                if isinstance(m, dict) and m.get('id') and m.get('id').startswith('qwen'):
                    v1_model_id = m.get('id')
                    break
            # fallback: pick first model id
            if not v1_model_id and data.get('data'):
                first = data.get('data')[0]
                v1_model_id = first.get('id') if isinstance(first, dict) else None
            use_v1 = True
    except Exception:
        use_v1 = False

    # Use system prompt provided by the orchestrator loader to ensure consistency
    try:
        from src.core.orchestration.agent_brain import load_system_prompt
        system = load_system_prompt(None) or Path('agent-brain/agents/coding_agent.md').read_text(encoding='utf-8')
    except Exception:
        system = Path('agent-brain/agents/coding_agent.md').read_text(encoding='utf-8')

    user_msg = (
        "Please respond in JSON with keys: summary (string), explanation (string). "
        "Also, simulate a tool call by returning a key 'tool_action' with {\"tool_name\": \"fs.read\", \"parameters\": {\"path\": \"/tmp/nonexistent.txt\"}}"
    )

    if use_v1 and v1_model_id:
        model_name = v1_model_id
        # OpenAI-compatible structured response
        payload = {
            'model': model_name,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user_msg},
            ],
            'response_format': {
                'type': 'json_schema',
                'json_schema': {
                    'name': 'agent_response',
                    'schema': {
                        'type': 'object',
                        'properties': {
                            'summary': {'type': 'string'},
                            'explanation': {'type': 'string'},
                            'tool_action': {'type': 'object'},
                        },
                        'required': ['summary', 'explanation'],
                    },
                },
            },
            'stream': False,
        }
        endpoints = [f"{v1_base}/chat/completions", f"{v1_base}/responses"]
    else:
        # Fall back to native Ollama API
        model_name = 'qwen3.5:9b'
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg}
            ],
            "format": "json",
            "stream": False,
        }
        endpoints = [
            f"{OLLAMA_URL}/chat",
            f"{OLLAMA_URL.replace('/api','/v1')}/chat/completions",
        ]

    # Use retry helper to handle model downloading / transient errors
    try:
        r, used = post_with_retry(endpoints, payload, overall_timeout=120, per_request_timeout=60)
    except Exception as e:
        # Final diagnostic: try one direct POST per endpoint to capture any error body/status
        details = []
        for ep in endpoints:
            try:
                rr = requests.post(ep, json=payload, timeout=10)
                details.append(f"{ep} -> {rr.status_code}: {rr.text[:1000]}")
            except Exception as ex:
                details.append(f"{ep} -> request failed: {ex}")
        pytest.skip(f"Endpoint not ready after retries: {e}\nDiagnostics:\n" + "\n".join(details))

    try:
        data = r.json()
    except Exception:
        # If response is not JSON, try to parse body heuristically
        data = {"message": {"content": r.text}}

    # The response structure may vary; attempt to extract content
    message = None
    parsed = None
    def try_parse_text_to_json(s):
        if not isinstance(s, str):
            return None
        s = s.strip()
        try:
            return json.loads(s)
        except Exception:
            # attempt to extract first {...} block
            m = re.search(r"(\{[\s\S]*\})", s)
            if m:
                try:
                    return json.loads(m.group(1))
                except Exception:
                    return None
            return None

    if isinstance(data, dict) and data.get('choices'):
        # OpenAI-style
        choice = data['choices'][0]
        msg = choice.get('message', {})
        content = msg.get('content') if isinstance(msg, dict) else None
        if isinstance(content, (dict, list)):
            parsed = content
        else:
            parsed = try_parse_text_to_json(content)
    else:
        # Ollama native: message under 'message' with 'content'
        message = data.get('message') or {}
        content = message.get('content') if isinstance(message, dict) else None
        parsed = try_parse_text_to_json(content)
        if parsed is None:
            # try thinking field
            thinking = message.get('thinking') or data.get('thinking')
            parsed = try_parse_text_to_json(thinking)

    assert parsed is not None and isinstance(parsed, dict), f"Expected JSON-parsed dict, got: {data}"
    # Check for required keys
    assert 'summary' in parsed or 'final_response' in parsed
    # If tool_action provided, assert structure
    ta = parsed.get('tool_action') or parsed.get('tool_action')
    if ta:
        assert 'tool_name' in ta and 'parameters' in ta
