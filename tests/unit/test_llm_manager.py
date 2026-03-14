import asyncio
import json
from src.core.llm_manager import ProviderManager, _provider_manager, get_structured_llm
from src.core.orchestration.event_bus import EventBus
from src.core.user_prefs import UserPrefs


def test_provider_manager_initialize_and_list(tmp_path):
    # create a providers_sample.json in tmp_path
    sample = {
        "name": "ollama",
        "base_url": "http://localhost:11434/api",
        "type": "ollama",
        "models": [
            {"name": "qwen3.5:9b"}
        ]
    }
    pfile = tmp_path / 'providers_sample.json'
    pfile.write_text(json.dumps(sample), encoding='utf-8')

    pm = ProviderManager(providers_config_path=str(pfile))
    # ensure no exception
    asyncio.run(pm.initialize())
    lst = pm.list_providers()
    assert isinstance(lst, list)
    assert 'ollama' in lst


from unittest.mock import patch, MagicMock

def test_get_structured_llm_missing_model_emits_event(monkeypatch, tmp_path):
    # create providers sample in tmp_path
    sample = {
        "name": "ollama",
        "base_url": "http://localhost:11434/api",
        "type": "ollama",
        "models": [
            {"name": "qwen3.5:9b"}
        ]
    }
    pfile = tmp_path / 'providers_sample.json'
    pfile.write_text(json.dumps(sample), encoding='utf-8')

    # configure global provider manager to use this file
    _provider_manager.providers_config_path = str(pfile)
    pm = _provider_manager
    # reset state so initialize reloads
    pm._initialized = False
    pm._providers = {}

    # set event bus and capture events
    bus = EventBus()
    events = []
    bus.subscribe('provider.model.missing', lambda payload: events.append(payload))
    pm.set_event_bus(bus)

    # ensure initialized
    with patch('requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        # LM Studio probe responses return full ids; keep consistent
        mock_response.json.return_value = {"models": [{"name": "qwen/qwen3.5-9b"}]}
        mock_get.return_value = mock_response
        asyncio.run(pm.initialize())

    # write prefs that request a missing model
    prefs_path = tmp_path / 'prefs.json'
    prefs_data = {"selected_model_provider": "ollama", "selected_model_name": "nonexistent:1"}
    prefs_path.write_text(json.dumps(prefs_data), encoding='utf-8')

    # monkeypatch UserPrefs.load to return our prefs (avoid recursion)
    def _load(path=None):
        return UserPrefs(data=prefs_data, path=prefs_path)
    monkeypatch.setattr('src.core.llm_manager.UserPrefs.load', staticmethod(_load))

    # call get_structured_llm
    client, resolved = asyncio.run(get_structured_llm())
    assert client is not None
    # resolved should be None because model missing
    assert resolved is None
    assert len(events) >= 1

