"""tokenizer.py — Accurate token counting for the CodingAgent.

D-06 / S0-A: Replace all ``len(text) // 4`` heuristics with this module.

Priority order:
1. tiktoken (OpenAI models, works offline after first download)
2. Character heuristic: ``len(text) // 3.5`` (more accurate than //4)

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
        import tiktoken  # optional dependency  # type: ignore[import]

        return tiktoken.get_encoding(encoding_name)
    except Exception:
        return None


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

    Uses tiktoken when available; falls back to character heuristic otherwise.
    The fallback uses ``len(text) / 3.5`` which is more accurate than the
    traditional ``/ 4`` for mixed code+prose content.

    Args:
        text:       The string to count tokens for.
        model_hint: Model name used to select the tiktoken encoding.  If None
                    or unknown, defaults to ``cl100k_base``.
    """
    if not text:
        return 0
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
