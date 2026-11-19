"""MLX-LM local server adapter for Apple Silicon.

Connects to ``mlx_lm.server`` (or MLX OpenAI-compatible endpoints) at
``http://localhost:8080/v1`` by default, forwards MLX KV-cache / context hints
cleanly, and exposes server discovery telemetry.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Union

import requests

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

_logger = logging.getLogger(__name__)


def discover_mlx_server(
    base_url: str = "http://localhost:8080/v1",
    timeout: float = 2.0,
) -> Dict[str, Any]:
    """Probe an MLX OpenAI-compatible server and return availability + models."""
    root = base_url.rstrip("/")
    models_url = f"{root}/models" if root.endswith("/v1") else f"{root}/v1/models"
    try:
        resp = requests.get(models_url, timeout=timeout)
        if resp.status_code != 200:
            return {"available": False, "base_url": base_url, "models": []}
        data = resp.json()
        raw_models = data.get("data", []) if isinstance(data, dict) else []
        models = [
            str(m.get("id"))
            for m in raw_models
            if isinstance(m, dict) and m.get("id")
        ]
        return {"available": True, "base_url": base_url, "models": models}
    except Exception as exc:
        _logger.debug("discover_mlx_server failed for %s: %s", base_url, exc)
        return {"available": False, "base_url": base_url, "models": []}


class MlxAdapter(OpenAICompatibleAdapter):
    """Adapter for Apple Silicon ``mlx_lm.server`` OpenAI-compatible endpoint."""

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
            or os.getenv("MLX_LM_URL")
            or os.getenv("MLX_BASE_URL")
            or "http://localhost:8080/v1"
        )
        super().__init__(
            base_url=resolved_url,
            api_key=api_key or os.getenv("MLX_API_KEY") or "mlx-local",
            default_model=default_model or os.getenv("MLX_MODEL"),
            models=models,
            name=name or "mlx_lm",
            **kwargs,
        )
        self.config_path = config_path

    def _build_payload(
        self,
        messages: Union[List[Dict[str, Any]], str],
        model_name: str,
        kwargs: Dict[str, Any],
    ) -> tuple[Dict[str, Any], Optional[str]]:
        kw = dict(kwargs)
        kv_quant = kw.pop("kv_cache_quant", None)
        num_ctx = kw.pop("num_ctx", None)
        payload, ep = super()._build_payload(messages, model_name, kw)
        # Forward MLX KV quantization / context cap hints when supported by server
        if kv_quant:
            if isinstance(kv_quant, str) and kv_quant.strip().lower() in ("q4", "q4_0"):
                payload.setdefault("kv_bits", 4)
            else:
                payload.setdefault("kv_bits", 8)
        if isinstance(num_ctx, int) and num_ctx > 0:
            payload.setdefault("max_kv_size", num_ctx)
        return payload, ep


MlxLmAdapter = MlxAdapter
