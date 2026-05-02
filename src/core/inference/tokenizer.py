"""tokenizer.py — Accurate token counting for the CodingAgent.

D-06 / S0-A: Replace all ``len(text) // 4`` heuristics with this module.

Priority order:
1. tiktoken (OpenAI models, works offline after first download)
2. HuggingFace transformers (Qwen3, Gemma, local models) — v2 Phase 3
3. Character heuristic: ``len(text) // 3.5`` (more accurate than //4)

Usage::

    from src.core.inference.tokenizer import count_tokens, fits_in_budget

    n = count_tokens("hello world", model_hint="gpt-4o")
    ok = fits_in_budget(messages, budget=8192, model_hint="qwen3")
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Dict, List, Optional


@lru_cache(maxsize=8)
def _get_encoder(encoding_name: str):
    """Return a cached tiktoken encoder or None if tiktoken is unavailable."""
    try:
        import tiktoken  # type: ignore[import]

        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return None


# v2 Phase 3: HuggingFace tokenizer cache
_HF_TOKENIZERS: Dict[str, Any] = {}


def _get_hf_tokenizer(model_hint: Optional[str]) -> Optional[Any]:
    """Get HuggingFace tokenizer for local models (Qwen3, Gemma, etc.).

    Uses a simple LRU cache to avoid repeated loading.
    Returns None if transformers not available or model not supported.
    """
    if not model_hint:
        return None
    m = model_hint.lower()

    # Check if this is a local model that benefits from HF tokenizer
    hf_models = (
        "qwen3",
        "qwen2",
        "gemma",
        "llama",
        "mistral",
        "phi",
        "deepseek",
        "qwq",
    )
    if not any(k in m for k in hf_models):
        return None

    # Check cache
    if m in _HF_TOKENIZERS:
        return _HF_TOKENIZERS[m]

    # Try to load
    try:
        from transformers import AutoTokenizer  # type: ignore[import]

        try:
            tokenizer = AutoTokenizer.from_pretrained(
                f"models/{model_hint}",  # Local model path
                local_files_only=True,
            )
            _HF_TOKENIZERS[m] = tokenizer
            return tokenizer
        except Exception:
            pass

        # Try from HuggingFace Hub (requires internet)
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                _hf_model_name(model_hint),
                trust_remote_code=True,
            )
            _HF_TOKENIZERS[m] = tokenizer
            return tokenizer
        except Exception:
            pass
    except Exception:
        pass

    return None


def _hf_model_name(model_hint: str) -> str:
    """Map model hint to HuggingFace model name."""
    m = model_hint.lower()
    if "qwen3" in m:
        return "Qwen/Qwen2.5-7B"  # Fallback tokenizer
    if "gemma" in m:
        return "google/gemma-2b"
    if "llama" in m:
        return "meta-llama/Llama-2-7b"
    return model_hint  # Try as-is


def _encoding_for_model(model_hint: Optional[str]) -> str:
    """Map a model name to the most appropriate tiktoken encoding."""
    if not model_hint:
        return "cl100k_base"
    m = model_hint.lower()
    if any(k in m for k in ("gpt-4o", "o1", "o3", "o4")):
        return "o200k_base"
    if any(k in m for k in ("gpt-4", "gpt-3.5", "gpt-4-turbo", "claude")):
        return "cl100k_base"
    # Local models (Ollama, LM Studio, OpenRouter local): cl100k as proxy
    return "cl100k_base"


def count_tokens(text: str, model_hint: Optional[str] = None) -> int:
    """Return token count for *text*.

    Priority:
    1. HuggingFace tokenizer (local models: Qwen3, Gemma, Llama) — v2 Phase 3
    2. tiktoken (OpenAI, Claude, cloud models)
    3. Character heuristic: len(text) / 3.5 (fallback)

    Args:
        text:       The string to count tokens for.
        model_hint: Model name used to select tokenizer.
    """
    if not text:
        return 0

    # v2 Phase 3: Try HuggingFace first for local models
    if model_hint:
        hf_tok = _get_hf_tokenizer(model_hint)
        if hf_tok is not None:
            try:
                return len(hf_tok.encode(text))
            except Exception:
                pass

    # tiktoken or fallback
    encoding_name = _encoding_for_model(model_hint)
    enc = _get_encoder(encoding_name)
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    # Fallback: 3.5 chars per token (better than //4 for code)
    return max(1, math.ceil(len(text) / 3.5))


def count_messages_tokens(
    messages: List[Dict[str, Any]],
    model_hint: Optional[str] = None,
) -> int:
    """Return total token count for a list of chat messages.

    Counts the ``content`` field of each message.  Tool-call arguments and
    system messages are included.
    """
    total = 0
    for msg in messages:
        # BUG-FIX: skip non-dict messages to avoid AttributeError
        if not isinstance(msg, dict):
            continue
        content = msg.get("content") or ""
        if isinstance(content, list):
            # OpenAI multi-part content blocks
            for block in content:
                if isinstance(block, dict):
                    total += count_tokens(
                        str(block.get("text") or block.get("content") or ""), model_hint
                    )
        else:
            total += count_tokens(str(content), model_hint)
        # Also count tool call arguments if present
        for tc in msg.get("tool_calls") or []:
            args = ""
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                args = str(fn.get("arguments") or "")
            total += count_tokens(args, model_hint)
    return total


def fits_in_budget(
    messages: List[Dict[str, Any]],
    budget: int,
    model_hint: Optional[str] = None,
) -> bool:
    """Return True if the messages fit within *budget* tokens."""
    return count_messages_tokens(messages, model_hint) <= budget


def clear_tokenizer_cache(model_hint: Optional[str] = None) -> None:
    """Clear HuggingFace tokenizer cache.

    Args:
        model_hint: If provided, clear only that model's tokenizer.
                    If None, clear all cached tokenizers.
    """
    global _HF_TOKENIZERS

    if model_hint:
        m = model_hint.lower()
        if m in _HF_TOKENIZERS:
            del _HF_TOKENIZERS[m]
    else:
        _HF_TOKENIZERS.clear()
