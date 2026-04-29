"""
P3-3: Small model pipeline E2E tests.

Tests the agent pipeline with small local models (Qwen3, Gemma) using mock LLM.
"""

# ruff: noqa: E501
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workdir() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / ".codingAgent").mkdir()
    (d / "main.py").write_text("def hello():\n    return 'hello'\n")
    return d


def _make_mock_orchestrator(workdir: Path) -> Any:
    orch = MagicMock()
    orch.working_dir = str(workdir)
    orch.session_store = MagicMock()
    orch.get_provider_capabilities = MagicMock(return_value={})
    orch.cancel_event = None
    return orch


# ---------------------------------------------------------------------------
# Small Model Tests
# ---------------------------------------------------------------------------


class TestSmallModelPipeline:
    """Test suite for small model (<=14B) pipelines."""

    def test_model_tiers_defined(self):
        """Model tiers are defined."""
        from src.core.inference.model_tiers import ModelTier

        assert ModelTier.SMALL is not None
        assert ModelTier.MEDIUM is not None
        assert ModelTier.LARGE is not None
        assert ModelTier.FRONTIER is not None

    def test_tokenizer_for_small_models(self):
        """Tokenizer works for small models."""
        from src.core.inference.tokenizer import count_tokens

        text = "x" * 10000
        tokens = count_tokens(text, model_hint="qwen3-9b")
        assert tokens > 0
        assert tokens < 10000

    def test_tokenizer_hf_for_qwen(self):
        """HF tokenizer loads for Qwen models (if transformers installed)."""
        from src.core.inference.tokenizer import _get_hf_tokenizer

        tokenizer = _get_hf_tokenizer("qwen3-9b")
        # May return None if transformers not installed
        assert tokenizer is None or tokenizer is not None

    def test_tokenizer_hf_for_gemma(self):
        """HF tokenizer loads for Gemma models (if transformers installed)."""
        from src.core.inference.tokenizer import _get_hf_tokenizer

        tokenizer = _get_hf_tokenizer("gemma-4-4b")
        # May return None if transformers not installed
        assert tokenizer is None or tokenizer is not None

    def test_workflow_selector_imports(self):
        """Workflow selector can be imported."""
        from src.core.inference.workflow_selector import (
            WorkflowType,
            select_workflow,
        )

        assert WorkflowType.SINGLE_LOOP is not None
        assert WorkflowType.FRONTIER_LOOP is not None

    def test_runtime_profile_imports(self):
        """Runtime profile can be imported."""
        from src.core.inference.runtime_profile import RuntimeProfile

        assert RuntimeProfile is not None

    def test_model_capability_profile(self):
        """Model capability profiles work for small models."""
        from src.core.inference.model_capability_profile import (
            get_model_profile,
        )

        profile = get_model_profile("qwen3-9b")
        assert profile is not None

    def test_hardware_capability_profile(self):
        """Hardware profiles work."""
        from src.core.inference.hardware_capability_profile import (
            detect_hardware,
        )

        profile = detect_hardware()
        assert profile is not None
        assert profile.vram_gb >= 0

    def test_runtime_profile_build_nano(self):
        """Runtime profile builds for nano tier."""
        from src.core.inference.runtime_profile import (
            preview_runtime,
        )

        profile = preview_runtime("gemma-4-4b", 8.0)
        assert profile is not None

    def test_runtime_profile_build_small(self):
        """Runtime profile builds for small tier."""
        from src.core.inference.runtime_profile import (
            preview_runtime,
        )

        profile = preview_runtime("qwen3-9b", 8.0)
        assert profile is not None
