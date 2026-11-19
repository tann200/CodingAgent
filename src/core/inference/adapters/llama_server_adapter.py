"""llama.cpp ``llama-server`` local OpenAI-compatible adapter.

Connects to ``llama-server`` (default ``http://localhost:8080/v1``), enables
``cache_prompt`` prefix reuse for multi-turn agent loops, and queries ``/props``
to discover the live ``n_ctx`` window allocated on the server.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Union

import requests

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

_logger = logging.getLogger(__name__)


def discover_llama_server(
    base_url: str = "http://localhost:8080/v1",
    timeout: float = 2.0,
) -> Dict[str, Any]:
    """Probe a running ``llama-server`` instance (/props and /v1/models)."""
    root = base_url.rstrip("/")
    root_no_v1 = root[:-3] if root.endswith("/v1") else root
    models: List[str] = []
    n_ctx: Optional[int] = None
    available = False
    try:
        resp = requests.get(f"{root_no_v1}/props", timeout=timeout)
        if resp.status_code == 200:
            available = True
            props = resp.json() if hasattr(resp, "json") else {}
            if isinstance(props, dict):
                gen_settings = props.get("default_generation_settings") or {}
                if isinstance(gen_settings, dict) and gen_settings.get("n_ctx"):
                    n_ctx = int(gen_settings["n_ctx"])
                model_path = props.get("model_path") or ""
                if model_path:
                    models.append(os.path.basename(str(model_path)))
    except Exception as exc:
        _logger.debug("discover_llama_server /props failed: %s", exc)

    if not models:
        try:
            m_resp = requests.get(f"{root_no_v1}/v1/models", timeout=timeout)
            if m_resp.status_code == 200:
                available = True
                m_data = m_resp.json()
                for entry in (m_data.get("data") or []) if isinstance(m_data, dict) else []:
                    if isinstance(entry, dict) and entry.get("id"):
                        models.append(str(entry["id"]))
        except Exception:
            pass

    return {
        "available": available,
        "base_url": base_url,
        "models": models,
        "n_ctx": n_ctx,
    }


class LlamaServerAdapter(OpenAICompatibleAdapter):
    """Adapter for ``llama.cpp`` ``llama-server`` OpenAI-compatible endpoint."""

    DEFAULT_TIMEOUT: float = 120.0
    supports_native_tools: bool = True

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        models: Optional[List[str]] = None,
        name: Optional[str] = None,
        config_path: Optional[str] = None,
        **kwargs: Any,
    ):
        resolved_url = (
            base_url
            or os.getenv("LLAMA_SERVER_URL")
            or "http://localhost:8080/v1"
        )
        super().__init__(
            base_url=resolved_url,
            api_key=api_key or os.getenv("LLAMA_SERVER_API_KEY") or "llama-local",
            default_model=default_model or os.getenv("LLAMA_SERVER_MODEL"),
            models=models,
            name=name or "llama_server",
            **kwargs,
        )
        self.config_path = config_path

    def get_server_props(self, timeout: float = 2.0) -> Dict[str, Any]:
        """Fetch ``/props`` metadata from ``llama-server`` (including live ``n_ctx``)."""
        return discover_llama_server(self.base_url or "http://localhost:8080/v1", timeout=timeout)

    def _build_payload(
        self,
        messages: Union[List[Dict[str, Any]], str],
        model_name: str,
        kwargs: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Optional[str]]:
        kw = dict(kwargs)
        disable_thinking = bool(kw.pop("disable_thinking", False))
        if not disable_thinking:
            prov_cfg = getattr(self, "provider", None)
            if isinstance(prov_cfg, dict):
                disable_thinking = bool(prov_cfg.get("disable_thinking", False))
        payload, ep = super()._build_payload(messages, model_name, kw)
        if isinstance(messages, (list, tuple)):
            # Enable llama.cpp KV prefix slot reuse for multi-turn coding loops
            payload.setdefault("cache_prompt", True)
            if disable_thinking:
                ctk = payload.get("chat_template_kwargs")
                if isinstance(ctk, dict):
                    ctk.setdefault("enable_thinking", False)
                elif ctk is None:
                    payload["chat_template_kwargs"] = {"enable_thinking": False}
                payload.setdefault("think", False)
        return payload, ep
