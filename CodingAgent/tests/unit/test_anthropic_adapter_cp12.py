"""Tests for AnthropicAdapter — CP-12: cache_control wiring.

Covers:
  1. _preprocess_messages splits on sentinel and sets cache_control on static block
  2. Dynamic block has no cache_control
  3. Short static block below threshold skips cache_control (Anthropic ≥ 1024 tokens)
  4. No sentinel → message passed through unchanged as string
  5. Non-system messages are untouched
  6. Both blocks present, static large enough → correct content-block array
  7. Empty dynamic part handled gracefully
  8. Empty static part handled gracefully
  9. Adapter instantiation without API key (no crash)
  10. _headers includes x-api-key, anthropic-version, anthropic-beta
  11. _headers omits x-api-key when no key
  12. Adapter uses BASE_URL = https://api.anthropic.com/v1
  13. Multiple system messages: only ones with sentinel are transformed
  14. ProviderManager alias Adapter points to AnthropicAdapter
  15. sentinel removed from output text (not present in block text)
"""


# ruff: noqa: E501
from __future__ import annotations

from unittest.mock import patch

from src.core.inference.adapters.anthropic_adapter import (
    AnthropicAdapter,
    Adapter,
    _ANTHROPIC_BASE_URL,
    _MIN_STATIC_CHARS_FOR_CACHE,
)
from src.core.context.context_builder import SYSTEM_PROMPT_DYNAMIC_BOUNDARY

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LARGE_STATIC = "A" * (_MIN_STATIC_CHARS_FOR_CACHE + 100)
_SMALL_STATIC = "A" * (_MIN_STATIC_CHARS_FOR_CACHE - 1)


def _make_messages(system_content: str):
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Hello"},
    ]


# ---------------------------------------------------------------------------
# _preprocess_messages tests
# ---------------------------------------------------------------------------


class TestPreprocessMessages:
    def test_sentinel_splits_message_into_two_blocks(self):
        content = (
            f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic part here."
        )
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        sys_msg = result[0]
        assert isinstance(sys_msg["content"], list), (
            "system content should be a list of blocks"
        )
        assert len(sys_msg["content"]) == 2

    def test_static_block_has_cache_control_when_large(self):
        content = (
            f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic part here."
        )
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        static_block = result[0]["content"][0]
        assert static_block["type"] == "text"
        assert "cache_control" in static_block
        assert static_block["cache_control"] == {"type": "ephemeral"}

    def test_dynamic_block_has_no_cache_control(self):
        content = (
            f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic part here."
        )
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        dynamic_block = result[0]["content"][1]
        assert dynamic_block["type"] == "text"
        assert "cache_control" not in dynamic_block

    def test_dynamic_block_text_is_correct(self):
        content = (
            f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic part here."
        )
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        dynamic_block = result[0]["content"][1]
        assert dynamic_block["text"] == "Dynamic part here."

    def test_static_block_text_is_correct(self):
        content = (
            f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic part here."
        )
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        static_block = result[0]["content"][0]
        assert static_block["text"] == _LARGE_STATIC

    def test_short_static_block_omits_cache_control(self):
        content = (
            f"{_SMALL_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic part."
        )
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        static_block = result[0]["content"][0]
        assert "cache_control" not in static_block, (
            "cache_control should be omitted for short static blocks"
        )

    def test_no_sentinel_leaves_message_as_string(self):
        content = "Plain system prompt without sentinel."
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        sys_msg = result[0]
        assert isinstance(sys_msg["content"], str)
        assert sys_msg["content"] == content

    def test_non_system_messages_untouched(self):
        sentinel_content = (
            f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic."
        )
        msgs = [
            {"role": "system", "content": sentinel_content},
            {"role": "user", "content": "Do something"},
            {"role": "assistant", "content": "Sure"},
        ]
        result = AnthropicAdapter._preprocess_messages(msgs)

        # User and assistant messages should be unchanged
        assert result[1] == {"role": "user", "content": "Do something"}
        assert result[2] == {"role": "assistant", "content": "Sure"}

    def test_sentinel_not_in_block_text(self):
        content = (
            f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic part here."
        )
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        for block in result[0]["content"]:
            assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in block["text"], (
                "Sentinel should not appear in the text of any block"
            )

    def test_empty_dynamic_part_no_dynamic_block(self):
        # Sentinel at very end — nothing after it
        content = f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}"
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        blocks = result[0]["content"]
        # Only the static block; empty dynamic is omitted
        assert len(blocks) == 1
        assert blocks[0]["text"] == _LARGE_STATIC

    def test_empty_static_part_no_static_block(self):
        # Sentinel at very start — nothing before it
        content = f"{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic only."
        msgs = _make_messages(content)
        result = AnthropicAdapter._preprocess_messages(msgs)

        blocks = result[0]["content"]
        # Only the dynamic block; empty static is omitted
        assert len(blocks) == 1
        assert blocks[0]["text"] == "Dynamic only."

    def test_multiple_system_messages_only_sentinel_ones_transformed(self):
        msgs = [
            {"role": "system", "content": "No sentinel here."},
            {
                "role": "system",
                "content": f"{_LARGE_STATIC}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nDynamic.",
            },
        ]
        result = AnthropicAdapter._preprocess_messages(msgs)

        assert isinstance(result[0]["content"], str), "First msg unchanged"
        assert isinstance(result[1]["content"], list), "Second msg transformed"


# ---------------------------------------------------------------------------
# Adapter instantiation and configuration tests
# ---------------------------------------------------------------------------


class TestAnthropicAdapterInit:
    def test_instantiation_without_key_does_not_crash(self):
        with patch("src.core.user_prefs.UserPrefs.load") as mock_prefs:
            mock_prefs.return_value.get_provider_key.return_value = None
            with patch.dict("os.environ", {}, clear=False):
                # Remove ANTHROPIC_API_KEY if present
                import os

                os.environ.pop("ANTHROPIC_API_KEY", None)
                adapter = AnthropicAdapter()
        assert adapter is not None
        assert adapter.api_key is None

    def test_api_key_from_constructor(self):
        adapter = AnthropicAdapter(api_key="sk-test-key")
        assert adapter.api_key == "sk-test-key"

    def test_api_key_from_env_var(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-env-key"}):
            with patch("src.core.user_prefs.UserPrefs.load") as mock_prefs:
                mock_prefs.return_value.get_provider_key.return_value = None
                adapter = AnthropicAdapter()
        assert adapter.api_key == "sk-env-key"

    def test_base_url_is_anthropic(self):
        adapter = AnthropicAdapter(api_key="sk-x")
        assert adapter.base_url == _ANTHROPIC_BASE_URL

    def test_default_model_set(self):
        adapter = AnthropicAdapter(api_key="sk-x")
        assert adapter.default_model == AnthropicAdapter.DEFAULT_MODEL

    def test_custom_models_list(self):
        adapter = AnthropicAdapter(api_key="sk-x", models=["claude-3-opus-20240229"])
        assert "claude-3-opus-20240229" in adapter.models


# ---------------------------------------------------------------------------
# Headers tests
# ---------------------------------------------------------------------------


class TestAnthropicAdapterHeaders:
    def test_headers_include_x_api_key(self):
        adapter = AnthropicAdapter(api_key="sk-test")
        headers = adapter._headers()
        assert headers.get("x-api-key") == "sk-test"

    def test_headers_include_anthropic_version(self):
        adapter = AnthropicAdapter(api_key="sk-test")
        headers = adapter._headers()
        assert "anthropic-version" in headers

    def test_headers_include_anthropic_beta_prompt_caching(self):
        adapter = AnthropicAdapter(api_key="sk-test")
        headers = adapter._headers()
        assert "anthropic-beta" in headers
        assert "prompt-caching" in headers["anthropic-beta"]

    def test_headers_omit_x_api_key_when_no_key(self):
        adapter = AnthropicAdapter(api_key=None)
        with patch("src.core.user_prefs.UserPrefs.load") as mock_prefs:
            mock_prefs.return_value.get_provider_key.return_value = None
            with patch.dict("os.environ", {}, clear=False):
                import os

                os.environ.pop("ANTHROPIC_API_KEY", None)
                adapter2 = AnthropicAdapter()
        headers = adapter2._headers()
        assert "x-api-key" not in headers

    def test_headers_no_bearer_authorization(self):
        # Anthropic uses x-api-key, not Authorization: Bearer
        adapter = AnthropicAdapter(api_key="sk-test")
        headers = adapter._headers()
        assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# ProviderManager alias test
# ---------------------------------------------------------------------------


class TestAliases:
    def test_adapter_alias_is_anthropic_adapter(self):
        assert Adapter is AnthropicAdapter
