"""LM Studio adapter.

Extends OpenAICompatibleAdapter with LM Studio-specific config loading
(providers.json + LM_STUDIO_* env vars) and short-name → full-id model
resolution.

All HTTP/inference logic lives in OpenAICompatibleAdapter.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import logging

from src.core.inference.adapters.openai_compat_adapter import OpenAICompatibleAdapter
from src.core.utils.strings import valid_str as _valid_str

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
        # Sanitize incoming models list to avoid leaking MagicMock placeholders
        _models: List[str] = []
        if models:
            for m in models:
                if isinstance(m, dict):
                    fid = m.get("id") or m.get("key") or m.get("name") or m.get("model")
                else:
                    fid = m
                if fid and _valid_str(fid):
                    _models.append(str(fid).strip())

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
                        except Exception as exc:
                            _logger.debug("lm_studio_adapter: model discovery inner error: %s", exc)
                            continue
            except Exception as exc:
                _logger.debug("lm_studio_adapter: model discovery failed: %s", exc)

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

        # Sanitise _default_model to avoid MagicMock placeholders
        try:
            if _default_model is not None and not _valid_str(_default_model):
                _default_model = None
            elif _default_model is not None:
                _default_model = str(_default_model).strip()
        except Exception as exc:
            _logger.debug("lm_studio_adapter: _default_model sanitize failed: %s", exc)
            try:
                if isinstance(_default_model, str) and _default_model.strip():
                    _default_model = _default_model.strip()
                else:
                    _default_model = None
            except Exception:
                _default_model = None

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
                _logger.warning(
                    "LmStudioAdapter: failed to read provider config at %s",
                    self.config_path,
                )

        if not self.provider:
            candidates = _providers_from_config or []
            if not candidates:
                try:
                    cfg = Path(__file__).parents[3] / "config" / "providers.json"
                    if cfg.exists():
                        raw2 = json.loads(cfg.read_text(encoding="utf-8"))
                        candidates = raw2 if isinstance(raw2, list) else [raw2]
                except Exception as exc:
                    _logger.debug("lm_studio_adapter: failed to load providers.json: %s", exc)
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
        _name: str = (
            name
            or (self.provider.get("name") if self.provider else None)
            or "lm_studio"
        )

        super().__init__(
            base_url=_base_url,
            api_key=_api_key,
            default_model=_default_model,
            models=_models,
            name=_name,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # LM Studio-specific context-window query (/api/v0/models)
    # ------------------------------------------------------------------

    def get_loaded_context_length(self, model_name: str = "") -> int:
        """Return the loaded context length for *model_name* from LM Studio's
        native ``/api/v0/models`` endpoint.

        LM Studio exposes ``loaded_context_length`` (the context size the model
        was actually loaded with) and ``max_context_length`` on each model entry.
        We prefer ``loaded_context_length`` for the active model because it
        reflects the real token budget available in the current session.

        Returns 0 when the endpoint is unreachable or the model is not found.
        """
        import requests as _requests

        if not self.base_url:
            return 0
        base = str(self.base_url).rstrip("/")
        # Derive the v0 API root from the base_url (strip /v1 suffix if present)
        if base.endswith("/v1"):
            api_root = base[:-3]
        else:
            api_root = base
        url = f"{api_root}/api/v0/models"
        try:
            r = _requests.get(
                url, headers=self._headers(), timeout=self.DEFAULT_TIMEOUT
            )
            if r.status_code != 200:
                return 0
            data = r.json()
        except Exception:
            return 0

        items = []
        if isinstance(data, dict):
            items = data.get("data") or data.get("models") or []
        elif isinstance(data, list):
            items = data

        target = str(model_name or self.default_model or "").lower()
        best_ctx = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or item.get("name") or "").lower()
            # Match if the target is a substring or equals the id
            if target and target not in item_id and item_id not in target:
                continue
            loaded = item.get("loaded_context_length") or 0
            max_ctx = item.get("max_context_length") or 0
            ctx = loaded or max_ctx
            if ctx > best_ctx:
                best_ctx = ctx
        return best_ctx

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
                    raw_key = (
                        m.get("id") or m.get("key") or m.get("name")
                        if isinstance(m, dict)
                        else m
                    )
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
        except Exception as exc:
            _logger.debug("lm_studio_adapter: resolve_model_name failed: %s", exc)
        return model_name
