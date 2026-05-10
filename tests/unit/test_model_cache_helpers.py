from src.core.inference.model_cache import (
    extract_models_from_api_response,
    get_cached_models_if_fresh,
    store_cached_models,
)


def test_get_cached_models_if_fresh_returns_value_inside_ttl():
    cache = {"lm_studio": ["a"]}
    cache_time = {"lm_studio": 100.0}

    result = get_cached_models_if_fresh(
        provider_key="lm_studio",
        cache=cache,
        cache_time=cache_time,
        ttl=300,
        now=200.0,
    )

    assert result == ["a"]


def test_store_cached_models_updates_cache_and_time():
    cache = {}
    cache_time = {}

    store_cached_models(
        provider_key="lm_studio",
        models=["x"],
        cache=cache,
        cache_time=cache_time,
        now=123.0,
    )

    assert cache["lm_studio"] == ["x"]
    assert cache_time["lm_studio"] == 123.0


def test_extract_models_from_api_response_reads_ids_and_strings():
    result = extract_models_from_api_response(
        {
            "models": [
                {"id": "a"},
                {"name": "b"},
                "c",
                {"model": "d"},
            ]
        },
        valid_str=lambda value: bool(value),
    )

    assert result == ["a", "b", "c", "d"]
