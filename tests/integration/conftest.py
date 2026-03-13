import os
import json
from pathlib import Path
import pytest


def pytest_sessionstart(session):
    """Before tests run, load src/config/providers.json if present and populate env vars used by integration tests.

    This keeps tests provider-driven and avoids relying on the user to set environment variables manually.
    """
    try:
        cfg_path = Path(__file__).parents[2] / 'src' / 'config' / 'providers.json'
        if cfg_path.exists():
            raw = json.loads(cfg_path.read_text(encoding='utf-8'))
            providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            for p in providers:
                try:
                    ptype = str(p.get('type') or '').lower()
                    pname = str(p.get('name') or '').lower()
                    base = p.get('base_url') or p.get('baseUrl') or p.get('url') or p.get('base')
                    if not base:
                        continue
                    if 'lm' in ptype or 'lm' in pname or 'lm_studio' in ptype or 'lmstudio' in pname:
                        # set LM_STUDIO_URL for tests that expect it
                        os.environ.setdefault('LM_STUDIO_URL', str(base))
                    if 'ollama' in ptype or 'ollama' in pname:
                        # tests expect OLLAMA_URL to include /api prefix in some usages
                        candidate = str(base)
                        # if base likely missing /api, do not modify; tests rely on their own fallback
                        os.environ.setdefault('OLLAMA_URL', candidate)
                except Exception:
                    continue
    except Exception:
        # be silent; integration skip logic will handle missing providers
        pass


@pytest.fixture(scope='session', autouse=True)
def providers_config(tmp_path_factory):
    """Create a temporary providers.json for integration tests and set the ProviderManager path.

    This ensures adapters prefer providers.json over environment variables during tests.
    If LM_STUDIO_URL or OLLAMA_URL env vars are present, they are used to populate providers.json
    so tests continue to run in CI without additional setup.
    """
    tmpdir = tmp_path_factory.mktemp('providers')
    providers = []
    # prefer explicit env vars if present
    lm_url = os.getenv('LM_STUDIO_URL')
    ollama_url = os.getenv('OLLAMA_URL') or os.getenv('OLLAMA_BASE_URL')
    if lm_url:
        providers.append({"name": "lm_studio", "type": "lm_studio", "base_url": lm_url, "models": []})
    if ollama_url:
        providers.append({"name": "ollama", "type": "ollama", "base_url": ollama_url, "models": []})
    # fallback to a default local host for LM Studio (non-destructive)
    if not providers:
        providers.append({"name": "lm_studio", "type": "lm_studio", "base_url": "http://localhost:1234/v1", "models": []})

    providers_path = tmpdir / 'providers.json'
    providers_path.write_text(json.dumps(providers), encoding='utf-8')

    # Ensure the project's provider manager uses this path
    try:
        import src.core.llm_manager as lm
        lm._provider_manager.providers_config_path = str(providers_path)
        # reset initialized state so tests get a fresh load
        lm._provider_manager._initialized = False
        lm._provider_manager._providers = {}
        lm._provider_manager._models_cache = {}
    except Exception:
        pass

    yield str(providers_path)
