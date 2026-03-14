from src.core.user_prefs import UserPrefs


def test_user_prefs_save_and_load(tmp_path):
    p = tmp_path / 'prefs.json'
    data = {"selected_model_provider": "ollama", "providers": {"ollama": {"api_key": "secret"}}}
    prefs = UserPrefs(data=data, path=p)
    prefs.save()
    assert p.exists()
    loaded = UserPrefs.load(path=str(p))
    assert loaded.get_provider_setting('ollama', 'api_key') == 'secret'

