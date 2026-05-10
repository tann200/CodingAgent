from src.core.context.prompt_cache import (
    compute_static_prompt_cache_key,
    get_static_prompt_cache_entry,
    store_static_prompt_cache_entry,
)


def test_compute_static_prompt_cache_key_includes_provider_variant_and_workdir():
    key = compute_static_prompt_cache_key(
        role_name="operational",
        active_skills=["debug"],
        tools=[{"name": "read_file", "description": "Read a file."}],
        model_tier="small",
        provider_capabilities={
            "provider_family": "anthropic",
            "model": "claude-3-5-sonnet",
        },
        model_name="claude-3-5-sonnet",
        use_native_tools=False,
        is_simple_mode=False,
        provider_variant="gemma4",
        working_dir="/workspace/project",
    )

    assert key[0] == "operational"
    assert key[1] == ("debug",)
    assert key[4] == "anthropic"
    assert key[5] == "claude-3-5-sonnet"
    assert key[6] == "claude-3-5-sonnet"
    assert key[9] == "gemma4"
    assert key[10] == "/workspace/project"


def test_compute_static_prompt_cache_key_distinguishes_exact_model_names():
    base_kwargs = dict(
        role_name="operational",
        active_skills=[],
        tools=[{"name": "read_file", "description": "Read a file."}],
        model_tier="frontier",
        provider_capabilities={"provider_family": "openai", "model": "gpt-4o"},
        use_native_tools=True,
        is_simple_mode=False,
        provider_variant="",
        working_dir="/workspace/project",
    )

    key_a = compute_static_prompt_cache_key(model_name="gpt-4o", **base_kwargs)
    key_b = compute_static_prompt_cache_key(
        model_name="gpt-4.5",
        **{
            **base_kwargs,
            "provider_capabilities": {
                "provider_family": "openai",
                "model": "gpt-4.5",
            },
        },
    )

    assert key_a != key_b


def test_static_prompt_cache_store_and_get_round_trip():
    cache = {}
    key = ("operational",)

    store_static_prompt_cache_entry(cache=cache, cache_key=key, value="prompt")

    assert get_static_prompt_cache_entry(cache=cache, cache_key=key) == "prompt"
