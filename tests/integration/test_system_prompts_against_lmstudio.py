import os
import json
from pathlib import Path
import pytest
from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter

# Mark as LMStudio integration
pytestmark = pytest.mark.lmstudio

# Enable integration tests when RUN_INTEGRATION=1 or when an lm_studio provider is configured
RUN = os.getenv('RUN_INTEGRATION') == '1'
# Auto-detect a configured lmstudio provider only when NOT running in CI.
# GitHub Actions and the project's python-tests.yml both set CI=true, so
# live-backend tests are always skipped there.
if not RUN and not os.getenv('CI'):
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
def test_system_prompts_behaviour_lmstudio():
    # explicit base to avoid provider config confusion
    base = os.getenv('LM_STUDIO_URL', 'http://localhost:1234/v1')
    adapter = LmStudioAdapter(base_url=base, models=["qwen/qwen3.5-9b"])

    # read system prompts (markdown filenames)
    coding_path = Path('agent-brain') / 'roles' / 'operational.md'
    planner_path = Path('agent-brain') / 'roles' / 'strategic.md'
    assert coding_path.exists(), f"Missing system prompt: {coding_path}"
    assert planner_path.exists(), f"Missing system prompt: {planner_path}"

    coding = coding_path.read_text(encoding='utf-8')
    planner = planner_path.read_text(encoding='utf-8')

    # helper to detect model-load errors returned by LM Studio
    def _is_model_load_error(resp):
        try:
            if not isinstance(resp, dict):
                return False
            # Check normalized error message
            err_msg = str(resp.get('error', '')).lower()
            if 'failed to load model' in err_msg or 'failed to load' in err_msg:
                return True
            if 'request_exception' in err_msg or 'read timed out' in err_msg:
                return True # treat connection timeout as a load error/unavailable for tests
            raw = resp.get('raw', {})
            if not isinstance(raw, dict):
                return False
            meta = raw.get('meta', {})
            if not isinstance(meta, dict):
                return False
            body = meta.get('body')
            if isinstance(body, dict):
                err = body.get('error') or body.get('errors')
                if isinstance(err, dict):
                    msg = err.get('message') or ''
                    if 'failed to load model' in str(msg).lower() or 'failed to load' in str(msg).lower():
                        return True
            if meta.get('status_code') in (400, 500):
                return True
        except Exception:
            return False
        return False

    # send a simple prompt using coding prompt
    messages = [
        {"role": "system", "content": coding},
        {"role": "user", "content": "Explain why the sky is blue."}
    ]
    res = adapter.generate(messages, stream=False, format_json=False)
    assert isinstance(res, dict)
    if _is_model_load_error(res):
        pytest.xfail(f"LM Studio model not loaded or failed to load: {res.get('error')}. Load the configured model in LM Studio and re-run integration tests.")
    # otherwise expect ok
    assert res.get('ok') is True, f"Expected ok=True, got {res}"

    # planner prompt
    messages = [
        {"role": "system", "content": planner},
        {"role": "user", "content": "Produce steps to add a new tool to the orchestrator."}
    ]
    pres = adapter.generate(messages, stream=False, format_json=False)
    assert isinstance(pres, dict)
    if _is_model_load_error(pres):
        pytest.xfail(f"LM Studio model not loaded or failed to load: {pres.get('error')}. Load the configured model in LM Studio and re-run integration tests.")
    assert pres.get('ok') is True, f"Expected ok=True, got {pres}"
