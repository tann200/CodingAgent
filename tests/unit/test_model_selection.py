from src.core.inference.model_selection import (
    canonical_provider,
    load_provider,
    lmstudio_full_id,
    normalize_models_for_provider,
    resolve_requested_model,
    resolve_config_path,
    save_provider,
    set_provider_active,
    select_model_name,
)

import json


def test_select_model_name_returns_first_when_unrequested():
    assert select_model_name(["a", "b"], None) == "a"


def test_select_model_name_prefers_exact_match():
    assert select_model_name(["provider/model-a", "provider/model-b"], "provider/model-b") == "provider/model-b"


def test_select_model_name_matches_short_suffix():
    assert select_model_name(["provider/model-a", "provider/model-b"], "model-b") == "provider/model-b"


def test_select_model_name_reads_dict_ids_keys_and_names():
    assert select_model_name([{"id": "model-a"}, {"key": "model-b"}, {"name": "model-c"}], "model-c") == "model-c"


def test_select_model_name_returns_none_when_requested_missing_or_empty():
    assert select_model_name([], "x") is None
    assert select_model_name(["provider/model-a"], "missing") is None


def test_lmstudio_full_id_normalizes_colon_models_and_preserves_full_ids():
    assert lmstudio_full_id("qwen3.5:9b") == "qwen/qwen3.5-9b"
    assert lmstudio_full_id("vendor/model-name") == "vendor/model-name"
    assert lmstudio_full_id("plain-model") == "plain-model"


def test_canonical_provider_delegates_to_provider_config_normalizer():
    assert canonical_provider(
        "LM Studio",
        canonical_provider_name_fn=lambda name: "lm_studio" if name else "",
    ) == "lm_studio"


def test_normalize_models_for_provider_delegates_shared_normalizer():
    calls = []

    result = normalize_models_for_provider(
        {"name": "lm studio", "models": [{"name": "qwen3.5:9b"}]},
        normalize_provider_models_fn=lambda provider, **kwargs: calls.append((provider, kwargs)) or ["qwen/qwen3.5-9b"],
        valid_str_fn=lambda value: bool(value),
        canonical_provider_fn=lambda name: "lm_studio",
        lmstudio_full_id_fn=lmstudio_full_id,
    )

    assert result == ["qwen/qwen3.5-9b"]
    assert calls and calls[0][0]["name"] == "lm studio"


def test_resolve_config_path_delegates_to_provider_config_resolver(tmp_path):
    expected = tmp_path / "providers.json"

    result = resolve_config_path(
        None,
        resolve_providers_config_path_fn=lambda path, current_file: expected,
        current_file="/tmp/current.py",
    )

    assert result == expected


def test_set_provider_active_delegates_with_injected_collaborators():
    calls = []

    set_provider_active(
        provider_type="lm_studio",
        active=True,
        set_provider_active_flag_fn=lambda **kwargs: calls.append(kwargs),
        resolve_config_path_fn=lambda path=None: __import__("pathlib").Path("/tmp/providers.json"),
        canonical_provider_fn=lambda name: "lm_studio",
        lock=object(),
        logger=object(),
    )

    assert calls and calls[0]["provider_type"] == "lm_studio"
    assert calls[0]["active"] is True


def test_load_provider_reads_and_parses_json(tmp_path):
    config_path = tmp_path / "providers.json"
    config_path.write_text('{"name": "test"}', encoding="utf-8")

    result = load_provider(
        None,
        resolve_config_path_fn=lambda path=None: config_path,
    )

    assert result == {"name": "test"}


def test_load_provider_returns_none_for_invalid_or_missing_content(tmp_path):
    invalid_path = tmp_path / "providers-invalid.json"
    invalid_path.write_text("not-json", encoding="utf-8")

    assert load_provider(
        None,
        resolve_config_path_fn=lambda path=None: invalid_path,
    ) is None
    assert load_provider(
        None,
        resolve_config_path_fn=lambda path=None: tmp_path / "missing.json",
    ) is None


def test_save_provider_writes_new_config_file(tmp_path):
    config_path = tmp_path / "providers.json"

    assert save_provider(
        {"name": "test", "type": "ollama"},
        path=None,
        initial_path=None,
        resolve_config_path_fn=lambda path=None: config_path,
        logger=type("Logger", (), {"debug": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})(),
        atomic_write_json_importer=lambda: lambda target, data, logger=None: target.write_text(json.dumps(data), encoding="utf-8") or True,
    )

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"name": "test", "type": "ollama"}


def test_save_provider_preserves_list_format_when_updating_existing_provider(tmp_path):
    config_path = tmp_path / "providers.json"
    config_path.write_text(
        json.dumps([
            {"name": "existing", "type": "ollama", "base_url": "http://old"},
            {"name": "other", "type": "openai"},
        ]),
        encoding="utf-8",
    )

    assert save_provider(
        {"name": "existing", "type": "ollama", "base_url": "http://new"},
        path=None,
        initial_path=None,
        resolve_config_path_fn=lambda path=None: config_path,
        logger=type("Logger", (), {"debug": lambda *args, **kwargs: None, "warning": lambda *args, **kwargs: None})(),
        atomic_write_json_importer=lambda: lambda target, data, logger=None: target.write_text(json.dumps(data), encoding="utf-8") or True,
    )

    assert json.loads(config_path.read_text(encoding="utf-8")) == [
        {"name": "existing", "type": "ollama", "base_url": "http://new"},
        {"name": "other", "type": "openai"},
    ]


def test_resolve_requested_model_returns_selected_match_without_publishing_event():
    events = []

    class _Bus:
        def publish(self, event_name, payload):
            events.append((event_name, payload))

    result = resolve_requested_model(
        ["provider/model-a", "provider/model-b"],
        "model-b",
        select_model_name_fn=select_model_name,
        event_bus=_Bus(),
        provider_key="provider",
    )

    assert result == "provider/model-b"
    assert events == []


def test_resolve_requested_model_publishes_missing_model_event():
    events = []

    class _Bus:
        def publish(self, event_name, payload):
            events.append((event_name, payload))

    result = resolve_requested_model(
        ["provider/model-a"],
        "missing",
        select_model_name_fn=select_model_name,
        event_bus=_Bus(),
        provider_key="provider",
    )

    assert result is None
    assert events == [
        (
            "provider.model.missing",
            {
                "provider": "provider",
                "requested": "missing",
                "available": ["provider/model-a"],
            },
        )
    ]
