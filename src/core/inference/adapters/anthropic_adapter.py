"""Anthropic adapter.

Extends OpenAICompatibleAdapter for the native Anthropic Messages API
(https://api.anthropic.com/v1).

Key differences from vanilla OpenAI-compat adapters:

CP-12 — prompt-cache boundary wiring
    The ``SYSTEM_PROMPT_DYNAMIC_BOUNDARY`` sentinel inserted by
    ``ContextBuilder`` is used here to split the system prompt into two
    content blocks.  The static (pre-sentinel) block receives
    ``cache_control: {"type": "ephemeral"}`` so Anthropic caches it across
    turns.  The dynamic (post-sentinel) block is left undecorated so it is
    re-evaluated every turn.

    This overrides ``OpenAICompatibleAdapter._preprocess_messages()`` which
    only strips the sentinel without setting cache_control.

Authentication:
    API key loaded from:
      1. Constructor argument
      2. ``UserPrefs`` (~/.config/codingagent/prefs.json, key "anthropic")
      3. ``ANTHROPIC_API_KEY`` environment variable

ProviderManager convention:
    type = "anthropic" in providers.json → imports this module →
    _camelize("anthropic") = "Anthropic" → looks for "AnthropicAdapter",
    then falls back to module-level "Adapter".  Both aliases are provided.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Union

import requests

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter
from src.core.utils.strings import valid_str as _valid_str

_logger = logging.getLogger(__name__)

_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"

# Minimum token count for a static block to be worth caching.
# Anthropic requires ≥ 1024 tokens for cache_control to take effect; we use
# a conservative word-count heuristic (≈ 4 chars/token) rather than a real
# tokeniser so we don't introduce a heavy dependency.
_MIN_STATIC_CHARS_FOR_CACHE = 512  # ≈ 128 tokens — small but safe floor


class AnthropicAdapter(OpenAICompatibleAdapter):
    """Adapter for the native Anthropic Messages API.

    Inherits HTTP/retry logic from OpenAICompatibleAdapter and adapts for Anthropic:
      - Fixed base URL and /v1/messages endpoint
      - API-key header format (``x-api-key`` + ``anthropic-version``)
      - CP-12: ``_preprocess_messages()`` — splits on the dynamic boundary
        sentinel and sets ``cache_control`` on the static block
      - Messages API payload shape: top-level ``system``, required ``max_tokens``,
        alternating user/assistant messages, and ``input_schema`` tool definitions
      - Response normalization: maps Anthropic message blocks into OpenAI-compatible
        ``choices`` and ``tool_calls`` structure.
    """

    BASE_URL = _ANTHROPIC_BASE_URL
    REQUIRES_API_KEY = True

    # Default to claude-3-5-sonnet (broadly available, good cache hit rate)
    DEFAULT_MODEL = "claude-3-5-sonnet-20241022"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        api_key: Optional[str] = None,
        models: Optional[List[str]] = None,
        name: str = "anthropic",
        # Accept (and silently override) generic args passed by ProviderManager
        base_url: Optional[str] = None,  # always overridden by BASE_URL
        config_path: Optional[str] = None,
        **kwargs,
    ):
        # Resolve API key: constructor > UserPrefs > env var
        if not api_key:
            try:
                from src.core.user_prefs import UserPrefs  # type: ignore[import]

                api_key = UserPrefs.load().get_provider_key("anthropic")
            except Exception as _prefs_exc:
                _logger.warning("AnthropicAdapter: failed to load UserPrefs: %s", _prefs_exc)
        if not api_key:
            api_key = os.environ.get("ANTHROPIC_API_KEY") or None

        # Sanitize models list to avoid leaking MagicMock placeholders
        resolved_models: List[str] = []
        if models:
            for m in models:
                if isinstance(m, dict):
                    fid = m.get("id") or m.get("key") or m.get("name") or m.get("model")
                else:
                    fid = m
                if fid and _valid_str(fid):
                    resolved_models.append(str(fid).strip())
        if not resolved_models:
            resolved_models = [self.DEFAULT_MODEL]
        default_model = resolved_models[0]

        effective_base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL") or self.BASE_URL
        ).rstrip("/")

        super().__init__(
            base_url=effective_base_url,
            api_key=api_key,
            default_model=default_model,
            models=resolved_models,
            name=name,
            **{k: v for k, v in kwargs.items() if k not in ("model",)},
        )

    # ------------------------------------------------------------------
    # Anthropic-specific HTTP headers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        """Return headers required by the Anthropic Messages API.

        Uses ``x-api-key`` (not ``Authorization: Bearer``) and must include
        ``anthropic-version``.  The ``anthropic-beta: prompt-caching-2024-07-31``
        header enables prompt caching so our ``cache_control`` blocks are
        honoured.
        """
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    # ------------------------------------------------------------------
    # CP-12: Prompt-cache boundary preprocessing
    # ------------------------------------------------------------------

    @staticmethod
    def _preprocess_messages(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Split system prompt on the dynamic-boundary sentinel and wire cache_control.

        The Anthropic Messages API accepts ``system`` as an array of content
        blocks rather than a plain string.  This method converts any system
        message whose content contains ``SYSTEM_PROMPT_DYNAMIC_BOUNDARY`` into
        two blocks:

        1. Static block  — everything before the sentinel.
           ``cache_control: {"type": "ephemeral"}`` is attached so Anthropic
           caches this block across turns (prompt caching).
        2. Dynamic block — everything after the sentinel.
           No ``cache_control`` so it is re-evaluated every turn.

        When the sentinel is absent the system message is left as a plain
        string (OpenAI-compat wire format still works for Anthropic).
        """
        try:
            from src.core.context.context_builder import (  # type: ignore[import]
                SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
            )
        except Exception:
            return messages

        out: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system" and isinstance(msg.get("content"), str):
                content: str = msg["content"]
                if SYSTEM_PROMPT_DYNAMIC_BOUNDARY in content:
                    # Split into static (before sentinel) and dynamic (after sentinel).
                    # The sentinel is surrounded by "\n\n" joins in the builder.
                    parts = content.split(SYSTEM_PROMPT_DYNAMIC_BOUNDARY, maxsplit=1)
                    static_text = parts[0].strip()
                    dynamic_text = parts[1].strip() if len(parts) > 1 else ""

                    blocks: List[Dict[str, Any]] = []

                    if static_text:
                        static_block: Dict[str, Any] = {
                            "type": "text",
                            "text": static_text,
                        }
                        # Only attach cache_control when the static block is
                        # large enough for Anthropic's caching to be effective.
                        if len(static_text) >= _MIN_STATIC_CHARS_FOR_CACHE:
                            static_block["cache_control"] = {"type": "ephemeral"}
                        blocks.append(static_block)

                    if dynamic_text:
                        blocks.append({"type": "text", "text": dynamic_text})

                    if blocks:
                        out.append({**msg, "content": blocks})
                        continue

                    # Degenerate case: sentinel present but no content —
                    # fall through to the plain-string path below.

            out.append(msg)
        return out

    # ------------------------------------------------------------------
    # Anthropic Messages API payload construction & response handling
    # ------------------------------------------------------------------

    def _apply_provider_flags(self, payload: Dict[str, Any]) -> None:
        """Anthropic rejects functions/think and other OpenAI flags; do not mutate."""
        pass

    def _build_payload(
        self,
        messages: Union[List[Dict[str, Any]], str],
        model_name: str,
        kwargs: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Optional[str]]:
        """Build request payload for Anthropic native Messages API (/v1/messages)."""
        ep = f"{self.BASE_URL}/messages"

        # Determine max_tokens (required by Anthropic)
        max_tokens = (
            kwargs.pop("max_tokens", None)
            or kwargs.pop("max_completion_tokens", None)
            or self.DEFAULT_MAX_TOKENS
        )
        try:
            max_tokens = int(max_tokens)
        except (ValueError, TypeError):
            max_tokens = self.DEFAULT_MAX_TOKENS

        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        elif not isinstance(messages, (list, tuple)):
            messages = []

        # 1. Extract system messages
        system_blocks: List[Dict[str, Any]] = []
        conversation_messages: List[Dict[str, Any]] = []

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                if isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict):
                            system_blocks.append(blk)
                        elif isinstance(blk, str) and blk.strip():
                            system_blocks.append({"type": "text", "text": blk})
                elif isinstance(content, str) and content.strip():
                    system_blocks.append({"type": "text", "text": content})
            else:
                conversation_messages.append(msg)

        # 2. Convert conversation messages into Anthropic alternating turns
        out_messages: List[Dict[str, Any]] = []

        for msg in conversation_messages:
            role = msg.get("role", "user")
            raw_content = msg.get("content")
            tool_calls = msg.get("tool_calls") or []

            blocks: List[Dict[str, Any]] = []

            if role == "assistant":
                if isinstance(raw_content, str) and raw_content.strip():
                    blocks.append({"type": "text", "text": raw_content})
                elif isinstance(raw_content, list):
                    for b in raw_content:
                        if isinstance(b, dict):
                            blocks.append(b)
                        elif isinstance(b, str) and b.strip():
                            blocks.append({"type": "text", "text": b})

                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tc_id = tc.get("id") or ""
                    fn = tc.get("function") or {}
                    fn_name = fn.get("name") or tc.get("name") or ""
                    raw_args = fn.get("arguments") or tc.get("parameters") or tc.get("input") or {}
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args)
                        except Exception:
                            parsed_args = {"raw_input": raw_args}
                    elif isinstance(raw_args, dict):
                        parsed_args = raw_args
                    else:
                        parsed_args = {"value": raw_args}

                    blocks.append({
                        "type": "tool_use",
                        "id": tc_id,
                        "name": fn_name,
                        "input": parsed_args,
                    })

                target_role = "assistant"

            elif role == "tool":
                # Tool execution result -> Anthropic maps to user role with tool_result block
                tool_use_id = msg.get("tool_call_id") or msg.get("id") or ""
                if isinstance(raw_content, (dict, list)):
                    content_str = json.dumps(raw_content)
                else:
                    content_str = str(raw_content) if raw_content is not None else ""
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content_str,
                })
                target_role = "user"

            else:  # user or other
                target_role = "user"
                if isinstance(raw_content, str):
                    if raw_content:
                        blocks.append({"type": "text", "text": raw_content})
                elif isinstance(raw_content, list):
                    for b in raw_content:
                        if isinstance(b, dict):
                            blocks.append(b)
                        elif isinstance(b, str) and b.strip():
                            blocks.append({"type": "text", "text": b})

            if not blocks:
                # Anthropic disallows empty message content
                blocks = [{"type": "text", "text": " "}]

            # Ensure role alternation: if same role as previous message, merge blocks
            if out_messages and out_messages[-1]["role"] == target_role:
                prev_content = out_messages[-1]["content"]
                if isinstance(prev_content, list):
                    prev_content.extend(blocks)
                else:
                    out_messages[-1]["content"] = [{"type": "text", "text": str(prev_content)}] + blocks
            else:
                out_messages.append({"role": target_role, "content": blocks})

        # Anthropic requires the first message to have role 'user'
        if out_messages and out_messages[0]["role"] != "user":
            out_messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "Start"}]})
        elif not out_messages:
            out_messages.append({"role": "user", "content": [{"type": "text", "text": "Hello"}]})

        # 3. Convert tools
        raw_tools = kwargs.pop("tools", None)
        anthropic_tools: List[Dict[str, Any]] = []
        if isinstance(raw_tools, list):
            for t in raw_tools:
                if not isinstance(t, dict):
                    continue
                if "function" in t and isinstance(t["function"], dict):
                    fn = t["function"]
                    schema = fn.get("parameters") or {"type": "object", "properties": {}}
                    anthropic_tools.append({
                        "name": fn.get("name", ""),
                        "description": fn.get("description", ""),
                        "input_schema": schema,
                    })
                elif "input_schema" in t:
                    anthropic_tools.append(t)
                else:
                    anthropic_tools.append({
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                    })

        # Clean OpenAI-specific keys
        for dead_key in (
            "functions",
            "function_call",
            "response_format",
            "n",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "user",
        ):
            kwargs.pop(dead_key, None)

        # 4. Stop sequences
        stop_val = kwargs.pop("stop", None) or kwargs.pop("stop_sequences", None)
        stop_sequences = None
        if isinstance(stop_val, str):
            stop_sequences = [stop_val]
        elif isinstance(stop_val, (list, tuple)):
            stop_sequences = [str(s) for s in stop_val if s]

        stream = bool(kwargs.get("stream", False))

        payload: Dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "messages": out_messages,
        }
        if stream:
            payload["stream"] = True
        if system_blocks:
            payload["system"] = system_blocks
        if anthropic_tools:
            payload["tools"] = anthropic_tools
        if stop_sequences:
            payload["stop_sequences"] = stop_sequences

        for opt_k in ("temperature", "top_p", "top_k", "metadata"):
            if opt_k in kwargs and kwargs[opt_k] is not None:
                payload[opt_k] = kwargs[opt_k]

        return payload, ep

    @staticmethod
    def _anthropic_to_openai_response(
        raw_json: Dict[str, Any],
        model_name: str,
    ) -> Dict[str, Any]:
        """Convert Anthropic /v1/messages JSON response to OpenAI chat.completion shape."""
        if "choices" in raw_json:
            return raw_json

        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        content = raw_json.get("content") or []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tc_id = block.get("id") or ""
                    fn_name = block.get("name") or ""
                    fn_input = block.get("input") or {}
                    if not isinstance(fn_input, str):
                        fn_args = json.dumps(fn_input)
                    else:
                        fn_args = fn_input
                    tool_calls.append({
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": fn_name,
                            "arguments": fn_args,
                        },
                    })

        stop_reason = raw_json.get("stop_reason")
        if stop_reason == "tool_use":
            finish_reason = "tool_calls"
        elif stop_reason == "max_tokens":
            finish_reason = "length"
        elif stop_reason in ("end_turn", "stop_sequence"):
            finish_reason = "stop"
        else:
            finish_reason = "stop"

        combined_text = "".join(text_parts)
        message: Dict[str, Any] = {
            "role": raw_json.get("role") or "assistant",
            "content": combined_text,
        }
        if tool_calls:
            message["tool_calls"] = tool_calls

        usage = raw_json.get("usage") or {}
        in_tok = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
        out_tok = usage.get("output_tokens", 0) if isinstance(usage, dict) else 0

        normalized_usage: Dict[str, Any] = {
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
        }
        if isinstance(usage, dict):
            if "cache_creation_input_tokens" in usage:
                normalized_usage["cache_creation_input_tokens"] = usage["cache_creation_input_tokens"]
            if "cache_read_input_tokens" in usage:
                normalized_usage["cache_read_input_tokens"] = usage["cache_read_input_tokens"]

        return {
            "id": raw_json.get("id"),
            "object": "chat.completion",
            "model": raw_json.get("model") or model_name,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": normalized_usage,
            "_raw_anthropic": raw_json,
        }

    def _chat_internal(
        self,
        messages: Union[List[Dict[str, Any]], str],
        model: Optional[str] = None,
        stream: bool = False,
        format_json: bool = False,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Any:
        model_name = model or (self.models[0] if self.models else self.default_model)
        result = super()._chat_internal(
            messages,
            model=model,
            stream=stream,
            format_json=format_json,
            timeout=timeout,
            **kwargs,
        )
        if stream:
            return result
        if isinstance(result, dict) and ("content" in result or result.get("type") == "message"):
            return self._anthropic_to_openai_response(result, model_name or self.DEFAULT_MODEL)
        return result

    def extract_tool_calls(self, chat_response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract tool calls from either normalized or raw Anthropic responses."""
        if isinstance(chat_response, dict) and "content" in chat_response and "role" in chat_response:
            calls = []
            for b in chat_response.get("content", []):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    fn_name = b.get("name") or ""
                    fn_input = b.get("input") or {}
                    calls.append({"name": fn_name, "args": fn_input})
            return calls
        return super().extract_tool_calls(chat_response)

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    def _models_endpoints(self) -> List[str]:
        return [_ANTHROPIC_MODELS_URL]

    def get_models_from_api(self) -> Dict[str, Any]:
        """List available Anthropic models."""

        if not self.api_key:
            return {
                "models": [
                    {
                        "id": self.DEFAULT_MODEL,
                        "name": self.DEFAULT_MODEL,
                        "display_name": self.DEFAULT_MODEL,
                        "key": self.DEFAULT_MODEL,
                    }
                ]
            }
        try:
            r = requests.get(
                _ANTHROPIC_MODELS_URL,
                headers=self._headers(),
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            _logger.warning("AnthropicAdapter.get_models_from_api failed: %s", exc)
            return {
                "models": [
                    {
                        "id": self.DEFAULT_MODEL,
                        "name": self.DEFAULT_MODEL,
                        "display_name": self.DEFAULT_MODEL,
                        "key": self.DEFAULT_MODEL,
                    }
                ]
            }

        raw = data.get("data") or data.get("models") or []
        if not isinstance(raw, list):
            raw = []

        out: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("name")
            if not model_id:
                continue
            display = item.get("display_name") or item.get("name") or str(model_id)
            out.append(
                {
                    "id": model_id,
                    "name": model_id,
                    "display_name": display,
                    "key": model_id,
                }
            )

        if not out:
            out = [
                {
                    "id": self.DEFAULT_MODEL,
                    "name": self.DEFAULT_MODEL,
                    "display_name": self.DEFAULT_MODEL,
                    "key": self.DEFAULT_MODEL,
                }
            ]

        return {"models": out}

    # ------------------------------------------------------------------
    # Connection health check
    # ------------------------------------------------------------------

    def validate_connection(self) -> bool:
        """Return True if the API key is present and the models endpoint responds."""

        if not self.api_key:
            return False
        try:
            r = requests.get(
                _ANTHROPIC_MODELS_URL,
                headers=self._headers(),
                timeout=10,
            )
            return r.status_code in (200, 206)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# ProviderManager aliases
# _camelize("anthropic") → "Anthropic" → looks for "AnthropicAdapter" first,
# then falls back to module-level "Adapter".
# ---------------------------------------------------------------------------
Adapter = AnthropicAdapter
