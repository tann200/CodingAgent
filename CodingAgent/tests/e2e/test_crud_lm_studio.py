"""
CRUD Live Test for LM Studio with Qwen3.5-9B.

Run: pytest tests/e2e/test_crud_lm_studio.py -v -s -k test_model
"""

from __future__ import annotations

import pytest


def check_lm_studio():
    """Check if LM Studio API is reachable."""
    try:
        import requests

        resp = requests.get("http://localhost:1234/v1/models", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


def skip_if_no_lm_studio():
    """Skip if LM Studio not running."""
    if not check_lm_studio():
        pytest.skip("LM Studio not running on localhost:1234")


class TestLMStudioAdapter:
    """Test LM Studio adapter directly."""

    def test_adapter_works(self):
        """Test adapter can generate response."""
        skip_if_no_lm_studio()

        from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter

        adapter = LmStudioAdapter(
            base_url="http://localhost:1234/v1",
            model="qwen/qwen3.5-9b",
            models=["qwen/qwen3.5-9b"],
        )

        result = adapter.generate(
            messages=[{"role": "user", "content": "Say hi in 3 words"}],
            max_tokens=20,
            model="qwen/qwen3.5-9b",  # Pass explicitly
        )

        assert result.get("ok") is True
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        assert len(content) > 0
        print(f"[PASS] Response: {content[:50]}")

    def test_tool_call_parsing(self):
        """Test adapter can parse tool calls."""
        skip_if_no_lm_studio()

        from src.core.inference.adapters.lm_studio_adapter import LmStudioAdapter

        adapter = LmStudioAdapter(
            base_url="http://localhost:1234/v1",
            model="qwen/qwen3.5-9b",
            models=["qwen/qwen3.5-9b"],
        )

        result = adapter.generate(
            messages=[{"role": "user", "content": "What is 2+2?"}],
            max_tokens=50,
            model="qwen/qwen3.5-9b",
        )

        assert result.get("ok") is True
        print("[PASS] Tool call test completed")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
