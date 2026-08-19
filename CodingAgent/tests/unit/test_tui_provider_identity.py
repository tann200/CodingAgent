from __future__ import annotations


def test_settings_store_normalizes_provider_ids_and_matches_type_aliases() -> None:
    from tui.tui_src.ui.settings import SettingsStore

    store = SettingsStore.__new__(SettingsStore)
    store._data = {}
    store._available_providers = []

    store.apply_system_settings(
        settings_dict={"default_provider": "none"},
        providers=[
            {
                "name": "GitHub Copilot",
                "type": "github_copilot",
                "models": ["gpt-5"],
            },
            {
                "name": "Local LM Studio",
                "type": "lm_studio",
                "models": ["qwen2.5-coder"],
            },
        ],
    )

    copilot = store.get_provider_by_id("github_copilot")
    assert copilot is not None
    assert copilot["id"] == "github_copilot"
    assert store.get_provider_by_id("GitHub Copilot") == copilot
    assert store.get_models_for_provider("lm_studio") == ["qwen2.5-coder"]


def test_settings_store_flat_models_include_provider_id() -> None:
    from tui.tui_src.ui.settings import SettingsStore

    store = SettingsStore.__new__(SettingsStore)
    store._data = {}
    store._available_providers = []

    store.apply_system_settings(
        settings_dict={},
        providers=[
            {"name": "GitHub Copilot", "type": "github_copilot", "models": ["gpt-5"]}
        ],
    )

    assert store.get_all_models_flat() == [
        {
            "provider_name": "GitHub Copilot",
            "provider_id": "github_copilot",
            "model": "gpt-5",
        }
    ]


def test_palette_provider_menu_uses_normalized_provider_id() -> None:
    from tui.tui_src.ui.features.palette.logic import get_provider_menu

    items = get_provider_menu(
        [{"name": "GitHub Copilot", "type": "github_copilot", "id": "github_copilot"}]
    )

    assert items[0]["action"] == "setup_prov:github_copilot:GitHub Copilot"
