"""OpenAI-compatible HTTP adapter base class.

Handles the full OpenAI wire protocol: chat/completions, /models listing,
Bearer token auth, streaming, tool/function calls.

Both LmStudioAdapter and OpenRouterAdapter extend this class.  They only need
to override:
  - __init__   — provider-specific config loading (files, env vars, user prefs)
  - _headers() — extra request headers (e.g. HTTP-Referer for OpenRouter)
  - resolve_model_name() — short-name → full-id mapping (LM Studio only)
  - _models_endpoints()  — URL strategy for model listing (optional)
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Union

import requests

from src.core.inference.llm_client import LLMClient
from src.core.inference.telemetry import with_telemetry

_logger = logging.getLogger(__name__)


class OpenAICompatibleAdapter(LLMClient):
    """Base adapter for any OpenAI-compatible REST endpoint.

    Subclasses call ``super().__init__()`` with already-resolved values.
    All config loading (files, env vars, secrets) belongs in subclass
    constructors — this class only owns HTTP and protocol logic.
    """

    DEFAULT_TIMEOUT: float = 120.0

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        models: Optional[List[str]] = None,
        name: str = "openai_compat",
        **kwargs,
    ):
        self.base_url = base_url or None
        self.api_key = api_key or None
        self.default_model = default_model or kwargs.get("model") or None
        self.models: List[str] = models or []
        self.name = name

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _compose(self, path: str) -> Optional[str]:
        """Build a full URL for *path*.

        If base_url already contains a version segment (/v1, /api…) the path is
        appended directly.  Otherwise a ``/api/v1/`` prefix is added so bare
        host-only URLs work without extra config.
        """
        if not self.base_url:
            return None
        base = str(self.base_url).rstrip("/")
        lower = base.lower()
        if "/v" in lower or "/api" in lower:
            return f"{base}/{path.lstrip('/')}"
        return f"{base}/api/v1/{path.lstrip('/')}"

    def _models_endpoints(self) -> List[str]:
        """Ordered list of URLs to try when listing models.

        Override in subclasses that have a fixed canonical URL (e.g. OpenRouter).
        """
        if not self.base_url:
            return []
        base = str(self.base_url).rstrip("/")
        if base.endswith("/v1"):
            return [f"{base}/models"]
        return [f"{base}/v1/models", f"{base}/models"]

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _safe_post(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: Optional[float] = None,
        stream: bool = False,
    ):
        """POST with tolerance for test monkeypatches that use slimmer signatures."""
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT
        try:
            return requests.post(
                url, headers=headers, json=payload, timeout=timeout, stream=stream
            )
        except TypeError:
            try:
                return requests.post(url, headers=headers, json=payload, stream=stream)
            except TypeError:
                try:
                    return requests.post(url, payload, stream)
                except TypeError:
                    return requests.post(url, payload)

    # ------------------------------------------------------------------
    # Model discovery
    # ------------------------------------------------------------------

    def get_models_from_api(self) -> Dict[str, Any]:
        """GET /models and return ``{"models": [...]}``.  Empty list on error.

        Accepts both ``{"data": [...]}`` (OpenAI/OpenRouter) and
        ``{"models": [...]}`` (LM Studio) response shapes, as well as a bare
        top-level list.
        """
        endpoints = self._models_endpoints()
        if not endpoints:
            _logger.debug(
                "%s.get_models_from_api: no base_url configured",
                self.__class__.__name__,
            )
            return {"models": []}

        for ep in endpoints:
            try:
                r = requests.get(
                    ep, headers=self._headers(), timeout=self.DEFAULT_TIMEOUT
                )
            except requests.exceptions.RequestException as exc:
                _logger.warning(
                    "%s.get_models_from_api request failed for %s: %s",
                    self.__class__.__name__,
                    ep,
                    exc,
                )
                continue

            if r.status_code >= 400:
                _logger.debug(
                    "%s.get_models_from_api: %s returned %s",
                    self.__class__.__name__,
                    ep,
                    r.status_code,
                )
                continue

            try:
                data = r.json()
            except Exception:
                _logger.debug(
                    "%s.get_models_from_api: non-JSON from %s",
                    self.__class__.__name__,
                    ep,
                )
                continue

            raw = None
            if isinstance(data, dict):
                if "models" in data and isinstance(data["models"], list):
                    raw = data["models"]
                elif "data" in data and isinstance(data["data"], list):
                    raw = data["data"]
            elif isinstance(data, list):
                raw = data

            if not raw:
                _logger.debug(
                    "%s.get_models_from_api: unexpected shape from %s",
                    self.__class__.__name__,
                    ep,
                )
                continue

            out: List[Dict[str, Any]] = []
            for item in raw:
                if isinstance(item, dict):
                    raw_key = item.get("id") or item.get("key") or item.get("name")
                    if not raw_key:
                        continue
                    short = str(raw_key).split("/")[-1]
                    display = item.get("display_name") or item.get("name") or short
                    out.append(
                        {
                            "name": short,
                            "display_name": display,
                            "id": raw_key,
                            "key": short,
                        }
                    )
                elif isinstance(item, str):
                    short = str(item).split("/")[-1]
                    out.append(
                        {"name": short, "display_name": short, "id": item, "key": short}
                    )

            if out:
                return {"models": out}

        return {"models": []}

    def validate_connection(self) -> bool:
        ep = self._compose("models")
        if not ep:
            return False
        try:
            r = requests.get(ep, headers=self._headers(), timeout=self.DEFAULT_TIMEOUT)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Model name resolution — identity; LmStudioAdapter overrides this
    # ------------------------------------------------------------------

    def resolve_model_name(self, model_name: str) -> str:
        """Return *model_name* unchanged.

        Override in subclasses that need short-name → full-id mapping.
        """
        return model_name

    # ------------------------------------------------------------------
    # CP-12: Prompt-cache boundary preprocessing
    # ------------------------------------------------------------------

    @staticmethod
    def _preprocess_messages(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Strip the ``SYSTEM_PROMPT_DYNAMIC_BOUNDARY`` sentinel from messages.

        OpenAI-compatible endpoints do not support the Anthropic
        ``cache_control`` content-block format, so the sentinel is simply
        removed.  Subclasses that wrap a native Anthropic Messages API endpoint
        should override this method to split the system content on the sentinel
        and set ``cache_control: {"type": "ephemeral"}`` on the static block.

        The sentinel appears as a standalone paragraph in the system message
        content.  We strip it (and any surrounding blank lines it introduces)
        so the model never sees the raw internal marker.
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
                    # Remove the sentinel and collapse the extra blank line it
                    # introduces (the sentinel is surrounded by "\n\n" joins).
                    cleaned = content.replace(
                        f"\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\n", "\n\n"
                    ).replace(SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "")
                    out.append({**msg, "content": cleaned})
                    continue
            out.append(msg)
        return out

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def _chat_internal(
        self,
        messages: Union[List[Dict[str, Any]], str],
        model: Optional[str] = None,
        stream: bool = False,
        format_json: bool = False,
        timeout: Optional[float] = None,
        **kwargs,
    ) -> Any:
        model_name = model
        if not model_name:
            if isinstance(self.models, list) and self.models:
                model_name = self.models[0]
            else:
                return {
                    "error": "no_model_configured",
                    "user_message": (
                        f"No model configured for {self.name} adapter. "
                        "Add a model to providers.json or pass model explicitly."
                    ),
                }

        model_name = self.resolve_model_name(model_name)

        # CP-12: Strip the dynamic-boundary sentinel from message content before
        # sending.  OpenAI-compatible endpoints do not support the Anthropic
        # cache_control content-block format; we simply remove the sentinel line
        # so the model never sees the raw internal marker.  A future native
        # Anthropic-Messages-API adapter should override _preprocess_messages()
        # to split on the sentinel and set cache_control instead.
        if isinstance(messages, (list, tuple)):
            messages = self._preprocess_messages(list(messages))

        if isinstance(messages, (list, tuple)):
            payload: Dict[str, Any] = {
                "model": model_name,
                "messages": messages,
                **kwargs,
            }
            ep = self._compose("chat/completions") or self._compose("responses")
        else:
            payload = {"model": model_name, "input": str(messages)}
            ep = self._compose("responses") or self._compose("chat")

        if not ep:
            return {"message": {"role": "assistant", "content": "", "parsed": None}}

        try:
            if os.getenv("AGENT_DEBUG") in ("1", "true", "True"):
                _logger.debug(
                    "%s.chat POST %s payload keys: %s",
                    self.__class__.__name__,
                    ep,
                    list(payload.keys()),
                )

            # OpenAI compatibility: copy tools → functions if functions key absent
            try:
                if "tools" in payload and "functions" not in payload:
                    payload["functions"] = payload["tools"]
            except Exception:
                pass

            # GAP-NEW-5: inject disable_thinking flag for providers that request it.
            # Passed as extra_body so it reaches the provider without polluting the
            # standard OpenAI schema.  Ollama and LM Studio both accept "think": false.
            _disable_think = False
            try:
                _prov_cfg = getattr(self, "provider", None) or {}
                if isinstance(_prov_cfg, dict):
                    _disable_think = bool(_prov_cfg.get("disable_thinking", False))
            except Exception:
                pass
            if _disable_think and "think" not in payload:
                payload["think"] = False

            # P2-1: Retry with exponential backoff on transient errors.
            # GAP-NEW-1: Parse Retry-After header on 429 to avoid needless delay
            # or re-triggering the rate limit.
            _MAX_RETRIES = 3
            _RETRYABLE_STATUS = {429, 500, 502, 503, 504}
            r = None
            last_exc: Optional[Exception] = None
            for _attempt in range(_MAX_RETRIES):
                try:
                    r = self._safe_post(
                        ep,
                        self._headers(),
                        payload,
                        timeout=self.DEFAULT_TIMEOUT,
                        stream=stream,
                    )
                    if stream:
                        return r
                    if r.status_code not in _RETRYABLE_STATUS:
                        break
                    _logger.warning(
                        "%s.chat attempt %d/%d: status %d, retrying",
                        self.__class__.__name__,
                        _attempt + 1,
                        _MAX_RETRIES,
                        r.status_code,
                    )
                except requests.exceptions.ConnectionError as _ce:
                    last_exc = _ce
                    _logger.warning(
                        "%s.chat attempt %d/%d: connection error, retrying",
                        self.__class__.__name__,
                        _attempt + 1,
                        _MAX_RETRIES,
                    )
                if _attempt < _MAX_RETRIES - 1:
                    # GAP-NEW-1: honour Retry-After header from provider (caps at 30s).
                    _retry_wait = 2 ** _attempt  # default: 1s, 2s
                    if r is not None and r.status_code == 429:
                        for _hdr in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset"):
                            _hval = r.headers.get(_hdr)
                            if _hval:
                                try:
                                    _retry_wait = min(float(_hval), 30.0)
                                except ValueError:
                                    pass
                                break
                    time.sleep(_retry_wait)

            if last_exc is not None and r is None:
                raise last_exc

            if stream:
                return r

            # After the retry loop, r is guaranteed non-None here:
            # if r were still None we'd have raised last_exc above, or
            # ConnectionError would have propagated.
            if r is None:  # pragma: no cover — defensive; satisfies type narrowing
                raise RuntimeError("HTTP response is None after retry loop (unexpected)")

            try:
                r.raise_for_status()
            except Exception as he:
                resp = getattr(he, "response", None) or r
                body = None
                try:
                    body = resp.json()
                except Exception:
                    try:
                        body = resp.text
                    except Exception:
                        body = str(he)

                user_message = None
                suggestions: List[str] = []
                # REACT-OVF: detect context-overflow error messages so the
                # caller (perception_node) can trigger compaction instead of
                # treating the turn as a generic failure.
                _context_overflow = False
                _OVERFLOW_PATTERNS = (
                    "context_length_exceeded",
                    "context length exceeded",
                    "maximum context length",
                    "context window",
                    "prompt is too long",
                    "tokens exceeds",
                    "exceeds the model's maximum",
                    "input is too long",
                    # LM Studio: "request (101084 tokens) exceeds the available context size (4096 tokens)"
                    "exceeds the available context",
                    "available context size",
                    # Ollama / llama.cpp
                    "exceeds context length",
                    "context length is",
                )
                try:
                    if isinstance(body, dict):
                        err = body.get("error") or body.get("errors")
                        if isinstance(err, dict):
                            msg = err.get("message") or ""
                            msg_lower = str(msg).lower()
                            if any(p in msg_lower for p in _OVERFLOW_PATTERNS):
                                _context_overflow = True
                                user_message = str(msg)
                            elif (
                                "insufficient system resources" in msg_lower
                                or "failed to load model" in msg_lower
                                or "requires approximately" in msg_lower
                            ):
                                user_message = str(msg)
                                m = re.search(
                                    r"requires approximately ([0-9]+(?:\.[0-9]+)?) ?GB",
                                    msg,
                                    re.IGNORECASE,
                                )
                                if m:
                                    suggestions.append(
                                        f"This model needs ~{m.group(1)} GB RAM to load."
                                    )
                                suggestions.append(
                                    "Consider using a smaller model or increasing available memory."
                                )
                            else:
                                user_message = str(msg)
                        elif isinstance(err, str):
                            err_lower = err.lower()
                            if any(p in err_lower for p in _OVERFLOW_PATTERNS):
                                _context_overflow = True
                                user_message = err
                    elif isinstance(body, str):
                        body_lower = body.lower()
                        if any(p in body_lower for p in _OVERFLOW_PATTERNS):
                            _context_overflow = True
                except Exception:
                    pass

                meta = {
                    "error": "http_error",
                    "status_code": getattr(resp, "status_code", None),
                    "body": body,
                }
                if _context_overflow:
                    meta["context_overflow"] = True
                result: Dict[str, Any] = {"meta": meta}
                if _context_overflow:
                    result["context_overflow"] = True
                if user_message:
                    result.update(
                        {"user_message": user_message, "suggestions": suggestions}
                    )
                return result

            try:
                return r.json()
            except Exception:
                return {
                    "message": {"role": "assistant", "content": r.text, "parsed": None}
                }

        except requests.exceptions.RequestException as exc:
            _logger.warning("%s.chat request failed: %s", self.__class__.__name__, exc)
            return {"error": "request_exception", "message": str(exc)}
        except Exception as exc:
            _logger.warning(
                "%s.chat unexpected error: %s", self.__class__.__name__, exc
            )
            return {"error": "unexpected", "message": str(exc)}

    @with_telemetry
    def generate(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        provider: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Synchronous call: return normalized payload."""
        try:
            raw_response = self._chat_internal(
                messages, model=model, stream=stream, timeout=timeout, **kwargs
            )

            if stream:
                return {
                    "ok": True,
                    "provider": self.name,
                    "model": model or self.default_model or "unknown",
                    "latency": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "choices": [],
                    "raw": raw_response,
                }

            if (
                isinstance(raw_response, dict)
                and "meta" in raw_response
                and raw_response["meta"].get("error")
            ):
                return {
                    "ok": False,
                    "provider": self.name,
                    "model": model or self.default_model or "unknown",
                    "error": raw_response.get(
                        "user_message", raw_response["meta"]["error"]
                    ),
                    "raw": raw_response,
                }

            if isinstance(raw_response, dict) and "error" in raw_response:
                return {
                    "ok": False,
                    "provider": self.name,
                    "model": model or self.default_model or "unknown",
                    "error": raw_response["error"],
                    "raw": raw_response,
                }

            prompt_tokens = 0
            completion_tokens = 0
            choices: List[Dict[str, Any]] = []

            if raw_response and "choices" in raw_response and raw_response["choices"]:
                for c in raw_response["choices"]:
                    msg = c.get("message", {})
                    # Reasoning models (e.g. qwen3.5-9b in thinking mode) sometimes
                    # emit the actual response in reasoning_content / thinking and
                    # leave content as an empty string.  Fall back so the agent gets
                    # the answer rather than an empty string.
                    content = msg.get("content", "") or ""
                    if not content.strip():
                        content = (
                            msg.get("reasoning_content")
                            or msg.get("thinking")
                            or content
                        )
                    choice_obj: Dict[str, Any] = {
                        "message": {
                            "role": msg.get("role", "assistant"),
                            "content": content,
                        },
                        "finish_reason": c.get("finish_reason", "stop"),
                    }
                    if "tool_calls" in msg:
                        choice_obj["tool_calls"] = msg["tool_calls"]
                    choices.append(choice_obj)

            if raw_response and "usage" in raw_response:
                prompt_tokens = raw_response["usage"].get("prompt_tokens", 0)
                completion_tokens = raw_response["usage"].get("completion_tokens", 0)

            # CP-9: Extract Anthropic prompt-cache token counts when present.
            # Anthropic returns these in the usage block of both streaming and
            # non-streaming responses (also present when Claude is accessed via
            # the GitHub Copilot / OpenRouter OpenAI-compat endpoint).
            cache_creation_input_tokens: int = 0
            cache_read_input_tokens: int = 0
            if raw_response and "usage" in raw_response:
                _u = raw_response["usage"]
                cache_creation_input_tokens = int(
                    _u.get("cache_creation_input_tokens", 0) or 0
                )
                cache_read_input_tokens = int(_u.get("cache_read_input_tokens", 0) or 0)

            return {
                "ok": True,
                "provider": self.name,
                "model": raw_response.get(
                    "model", model or self.default_model or "unknown"
                ),
                "latency": 0.0,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "choices": choices,
                "raw": raw_response,
            }

        except Exception as exc:
            return {
                "ok": False,
                "provider": self.name,
                "model": model or self.default_model or "unknown",
                "error": str(exc),
                "raw": {},
            }

    def extract_tool_calls(self, chat_response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract ``[{"name": ..., "args": ...}]`` from an OpenAI chat response."""
        if isinstance(chat_response, dict):
            if "tool_calls" in chat_response:
                calls = chat_response["tool_calls"]
                if isinstance(calls, list):
                    out = []
                    for c in calls:
                        if isinstance(c, dict):
                            name = c.get("name")
                            args = c.get("arguments") or c.get("args")
                            if name and isinstance(args, dict):
                                out.append({"name": name, "args": args})
                    return out
            if "function_call" in chat_response:
                call = chat_response["function_call"]
                if isinstance(call, dict):
                    name = call.get("name")
                    args = call.get("arguments") or call.get("args")
                    if name and isinstance(args, dict):
                        return [{"name": name, "args": args}]
        return []
