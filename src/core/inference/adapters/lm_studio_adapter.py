"""LM Studio adapter.

Extends OpenAICompatibleAdapter with LM Studio-specific config loading
(providers.json + LM_STUDIO_* env vars) and short-name → full-id model
resolution.

All HTTP/inference logic lives in OpenAICompatibleAdapter.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

import logging

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter

_logger = logging.getLogger(__name__)


class LmStudioAdapter(OpenAICompatibleAdapter):
    DEFAULT_TIMEOUT: float = 120.0

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        providers_config_path: Optional[str] = None,
        config_path: Optional[str] = None,
        name: Optional[str] = None,
        models: Optional[List[str]] = None,
        **kwargs,
    ):
        # ----------------------------------------------------------------
        # 1. Start with explicit constructor args
        # ----------------------------------------------------------------
        _base_url = base_url
        _api_key = api_key
        _default_model = default_model or kwargs.pop("model", None)
        _models: List[str] = list(models) if models else []

        # ----------------------------------------------------------------
        # 2. Fill missing values from providers.json
        # ----------------------------------------------------------------
        if providers_config_path is None:
            try:
                providers_config_path = str(
                    Path(__file__).parents[3] / "config" / "providers.json"
                )
            except Exception:
                providers_config_path = None

        _providers_from_config: list = []
        if providers_config_path:
            try:
                ppath = Path(providers_config_path)
                if ppath.exists():
                    raw = json.loads(ppath.read_text(encoding="utf-8"))
                    _providers_from_config = (
                        raw
                        if isinstance(raw, list)
                        else ([raw] if isinstance(raw, dict) else [])
                    )
                    for p in _providers_from_config:
                        try:
                            ptype = str(p.get("type") or "").lower()
                            pname = str(p.get("name") or "").lower()
                            if ptype == "lm_studio" or pname == "lm_studio":
                                if not _base_url:
                                    _base_url = (
                                        p.get("base_url")
                                        or p.get("baseUrl")
                                        or p.get("url")
                                    )
                                if not _api_key:
                                    _api_key = p.get("api_key") or p.get("apiKey")
                                if not _default_model:
                                    ms = p.get("models") or []
                                    if isinstance(ms, list) and ms:
                                        _default_model = (
                                            ms[0]
                                            if isinstance(ms[0], str)
                                            else (
                                                ms[0].get("id") or ms[0].get("name")
                                                if isinstance(ms[0], dict)
                                                else None
                                            )
                                        )
                                break
                        except Exception:
                            continue
            except Exception:
                pass

        # ----------------------------------------------------------------
        # 3. Fall back to environment variables
        # ----------------------------------------------------------------
        if not _base_url:
            _base_url = os.getenv("LM_STUDIO_URL")
        if not _api_key:
            _api_key = os.getenv("LM_STUDIO_API_KEY")
        if not _default_model:
            _default_model = os.getenv("LM_STUDIO_MODEL")

        # Fall back to first element of models list if still unset
        if not _default_model and _models:
            _default_model = _models[0]

        # ----------------------------------------------------------------
        # 4. Resolve provider dict (backwards-compat attributes)
        # ----------------------------------------------------------------
        self.config_path = Path(config_path) if config_path else None
        self.provider: Optional[Dict[str, Any]] = None

        if self.config_path and self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    self.provider = data[0] if isinstance(data[0], dict) else None
                elif isinstance(data, dict):
                    self.provider = data
            except Exception:
                warnings.warn(
                    f"LmStudioAdapter: failed to read provider config at {self.config_path}"
                )

        if not self.provider:
            candidates = _providers_from_config or []
            if not candidates:
                try:
                    cfg = Path(__file__).parents[3] / "config" / "providers.json"
                    if cfg.exists():
                        raw2 = json.loads(cfg.read_text(encoding="utf-8"))
                        candidates = raw2 if isinstance(raw2, list) else [raw2]
                except Exception:
                    pass
            for p in candidates:
                try:
                    ptype = (p.get("type") or "").lower()
                    pname = (p.get("name") or "").lower()
                    if ptype == "lm_studio" or pname == "lm_studio":
                        self.provider = p
                        break
                except Exception:
                    continue

        self.missing_provider = self.provider is None
        _name = name or (self.provider.get("name") if self.provider else "lm_studio")

        super().__init__(
            base_url=_base_url,
            api_key=_api_key,
            default_model=_default_model,
            models=_models,
            name=_name,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # LM Studio-specific short-name → full-id resolution
    # ------------------------------------------------------------------

    def resolve_model_name(self, model_name: str) -> str:
        """Resolve a short LM Studio model name to its full id when possible.

        If model_name already contains ``/`` it is returned unchanged.
        Otherwise the static model list is checked first (no network call);
        only if no match is found there is a single API probe.
        """
        try:
            if not model_name or "/" in model_name:
                return model_name

            def variants(s: str):
                vs = {s}
                vs.add(s.replace(":", "-"))
                vs.add(s.replace("-", ":"))
                vs.add(s.replace(":", "/"))
                vs.add(s.replace("-", "/"))
                return vs

            if self.models:
                for m in self.models:
                    raw_key = m.get("id") or m.get("key") or m.get("name") if isinstance(m, dict) else m
                    if raw_key:
                        short = str(raw_key).split("/")[-1]
                        if (
                            short == model_name
                            or model_name in variants(short)
                            or short in variants(model_name)
                        ):
                            return str(raw_key)

            api_models = self.get_models_from_api()
            if isinstance(api_models.get("models"), list):
                for m in api_models["models"]:
                    if isinstance(m, dict):
                        raw_key = m.get("id") or m.get("key") or m.get("name")
                        short = str(raw_key).split("/")[-1] if raw_key else None
                        if short and (
                            short == model_name
                            or model_name in variants(short)
                            or short in variants(model_name)
                        ):
                            return str(raw_key)
        except Exception:
            pass
        return model_name
