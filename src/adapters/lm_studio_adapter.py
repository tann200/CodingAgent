"""Minimal LM Studio adapter for tests and integration.

This implementation is intentionally small and dependency-free to avoid import
issues during test collection. It provides the interface expected by
`ProviderManager` and other parts of the codebase.
"""
from __future__ import annotations

import warnings
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests
import logging

from src.core.inference.llm_client import LLMClient
from src.core.inference.telemetry import with_telemetry

_logger = logging.getLogger(__name__)


class LmStudioAdapter(LLMClient):
    DEFAULT_TIMEOUT: float = 120.0

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None, default_model: Optional[str] = None, providers_config_path: Optional[str] = None, config_path: Optional[str] = None, name: Optional[str] = None, models: Optional[List[str]] = None, **kwargs):
        # Prefer explicit args first. Then prefer configured values from providers.json
        # (the project's canonical source of provider configuration). Finally,
        # fall back to environment variables only if nothing was configured.
        self.base_url = base_url
        self.api_key = api_key
        self.default_model = default_model or kwargs.get('model')
        self.models = models or []


        if providers_config_path is None:
            try:
                providers_config_path = str(Path(__file__).parents[1] / 'config' / 'providers.json')
            except Exception:
                providers_config_path = None

        # Try loading providers.json to fill missing values
        if providers_config_path:
            try:
                ppath = Path(providers_config_path)
                if ppath.exists():
                    import json
                    raw = json.loads(ppath.read_text(encoding='utf-8'))
                    providers = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
                    for p in providers:
                        try:
                            ptype = str(p.get('type') or '').lower()
                            pname = str(p.get('name') or '').lower()
                            # Match explicit 'lm_studio' provider type or name
                            if ptype == 'lm_studio' or pname == 'lm_studio':
                                # pick base_url and model if not already provided
                                if not self.base_url:
                                    self.base_url = p.get('base_url') or p.get('baseUrl') or p.get('url')
                                if not self.api_key:
                                    self.api_key = p.get('api_key') or p.get('apiKey')
                                if not self.default_model:
                                    models = p.get('models') or []
                                    if isinstance(models, list) and models:
                                        self.default_model = models[0] if isinstance(models[0], str) else (models[0].get('id') if isinstance(models[0], dict) else None)
                                # stop after first matching provider
                                break
                        except Exception:
                            continue
            except Exception:
                pass
        # Finally, if still missing values, fall back to environment variables (legacy behavior)
        if not self.base_url:
            self.base_url = os.getenv('LM_STUDIO_URL')
        if not self.api_key:
            self.api_key = os.getenv('LM_STUDIO_API_KEY')
        if not self.default_model:
            self.default_model = os.getenv('LM_STUDIO_MODEL')

        # final fallback: ensure base_url may default to empty string so callers can detect missing config
        if not self.base_url:
            self.base_url = None
        if not self.default_model:
            self.default_model = None

        # Load provider dict if a config_path was provided and exists
        self.config_path = Path(config_path) if config_path else None
        self.provider: Optional[Dict[str, Any]] = None
        if self.config_path and self.config_path.exists():
            try:
                data = json.loads(self.config_path.read_text(encoding='utf-8'))
                # if file is a list, take first element
                if isinstance(data, list) and data:
                    self.provider = data[0] if isinstance(data[0], dict) else None
                elif isinstance(data, dict):
                    self.provider = data
            except Exception:
                warnings.warn(f"LMStudioAdapter: failed to read provider config at {self.config_path}")
                self.provider = None
        # If no provider config was provided, try to load from project's src/config/providers.json
        if not self.provider:
            try:
                cfg = Path(__file__).parents[1] / 'config' / 'providers.json'
                if cfg.exists():
                    raw = json.loads(cfg.read_text(encoding='utf-8'))
                    candidates = raw if isinstance(raw, list) else [raw]
                    for p in candidates:
                        # match by type/name or base_url
                        try:
                            ptype = (p.get('type') or '').lower()
                            pname = (p.get('name') or '').lower()
                            if ptype == 'lm_studio' or pname == 'lm_studio':
                                # prefer exact base_url match if provided
                                b = p.get('base_url')
                                if base_url and b and str(base_url).rstrip('/') == str(b).rstrip('/'):
                                    self.provider = p
                                    break
                                # otherwise take first lm_studio provider
                                if ptype == 'lm_studio' or pname == 'lm_studio':
                                    self.provider = p
                                    break
                        except Exception:
                            continue
            except Exception:
                pass

        # configure fields
        self.name = name or (self.provider.get('name') if self.provider else 'lm_studio')
        # allow overriding base_url/api_key/default_model from provider dict if not provided
        if not self.base_url and self.provider:
            self.base_url = self.provider.get('base_url') or self.provider.get('baseUrl') or self.provider.get('url')
        if not self.api_key and self.provider:
            self.api_key = self.provider.get('api_key') or self.provider.get('apiKey')
        if not self.default_model and self.provider:
            if self.models:
                self.default_model = self.models[0]
        # missing_provider indicates whether a provider definition was found
        self.missing_provider = False if self.provider else True

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _compose(self, path: str) -> Optional[str]:
        if not self.base_url:
            return None
        base = str(self.base_url).rstrip('/')
        # If base already contains an API version or /api segment, append path directly
        lower = base.lower()
        if '/v' in lower or '/api' in lower:
            # Avoid double slashes
            return f"{base}/{path.lstrip('/')}"
        # Otherwise assume base is host only; use common v1 prefix
        return f"{base}/api/v1/{path.lstrip('/')}"

    def get_models_from_api(self) -> Dict[str, Any]:
        """Call LM Studio /v1/models (or /models) and normalize to {'models': [...]}
        Returns empty list on any error.
        """
        # Primary LM Studio endpoints to query (prefer v1 path)
        endpoints: List[str] = []
        if self.base_url:
            base = str(self.base_url).rstrip('/')
            # If user provided a URL already containing /v1, use it directly
            if base.endswith('/v1'):
                endpoints.append(f"{base}/models")
            else:
                endpoints.append(f"{base}/v1/models")
                endpoints.append(f"{base}/models")
        if not endpoints:
            warnings.warn('LMStudioAdapter.get_models_from_api: no base_url configured')
            return {"models": []}

        for ep in endpoints:
            try:
                r = requests.get(ep, headers=self._headers(), timeout=self.DEFAULT_TIMEOUT)
            except requests.exceptions.RequestException as e:
                warnings.warn(f"LMStudioAdapter.get_models_from_api request failed for {ep}: {e}")
                continue

            # accept only successful responses
            if r.status_code >= 400:
                warnings.warn(f"LMStudioAdapter.get_models_from_api: {ep} returned status {r.status_code}")
                continue

            try:
                data = r.json()
            except Exception:
                warnings.warn(f"LMStudioAdapter.get_models_from_api: non-JSON response from {ep}")
                continue

            # support {"data":[...]} or {"models":[...]} or list
            raw = None
            if isinstance(data, dict):
                if 'models' in data and isinstance(data['models'], list):
                    raw = data['models']
                elif 'data' in data and isinstance(data['data'], list):
                    raw = data['data']
            elif isinstance(data, list):
                raw = data

            if not raw:
                warnings.warn(f"LMStudioAdapter.get_models_from_api: unexpected JSON shape from {ep}")
                continue

            out: List[Dict[str, Any]] = []
            for item in raw:
                if isinstance(item, dict):
                    raw_key = item.get('key') or item.get('id') or item.get('name')
                    if raw_key is None:
                        continue
                    try:
                        short = str(raw_key).split('/')[-1]
                    except Exception:
                        short = str(raw_key)
                    display = item.get('display_name') or item.get('name') or short
                    # preserve full id (raw_key) so we can map short -> full id when calling LM Studio
                    out.append({"name": short, "display_name": display, "id": raw_key, "key": short})
                elif isinstance(item, str):
                    try:
                        short = str(item).split('/')[-1]
                    except Exception:
                        short = str(item)
                    out.append({"name": short, "display_name": short, "id": item, "key": short})
            if out:
                return {"models": out}

        # Nothing worked
        return {"models": []}

    def validate_connection(self) -> bool:
        # Validate by checking /v1/models only (LM Studio recommended health check)
        ep = self._compose('models')
        if not ep:
            return False
        try:
            r = requests.get(ep, headers=self._headers(), timeout=self.DEFAULT_TIMEOUT)
            return r.status_code == 200
        except Exception:
            return False


    @with_telemetry
    def generate(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        provider: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Synchronous call: return normalized payload."""
        try:
            # Use original chat logic to do the network call
            raw_response = self._chat_internal(messages, model=model, stream=stream, timeout=timeout, **kwargs)
            
            # If it's a streaming response, we just return it as 'raw' for now, or handle stream
            if stream:
                return {
                    "ok": True,
                    "provider": "lm_studio",
                    "model": model or self.default_model or "unknown",
                    "latency": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "choices": [],
                    "raw": raw_response
                }
            
            # Check if internal chat returned an error
            if isinstance(raw_response, dict) and "meta" in raw_response and raw_response["meta"].get("error"):
                return {
                    "ok": False,
                    "provider": "lm_studio",
                    "model": model or self.default_model or "unknown",
                    "error": raw_response.get("user_message", raw_response["meta"]["error"]),
                    "raw": raw_response
                }
                
            if isinstance(raw_response, dict) and "error" in raw_response:
                return {
                    "ok": False,
                    "provider": "lm_studio",
                    "model": model or self.default_model or "unknown",
                    "error": raw_response["error"],
                    "raw": raw_response
                }
            
            # Normalize successful response
            content_text = ""
            prompt_tokens = 0
            completion_tokens = 0
            choices = []
            finish_reason = "stop"
            
            if raw_response and "choices" in raw_response and raw_response["choices"]:
                for c in raw_response["choices"]:
                    msg = c.get("message", {})
                    choice_obj = {
                        "message": {"role": msg.get("role", "assistant"), "content": msg.get("content", "")},
                        "finish_reason": c.get("finish_reason", "stop")
                    }
                    if "tool_calls" in msg:
                        choice_obj["tool_calls"] = msg["tool_calls"]
                    choices.append(choice_obj)
            
            if raw_response and "usage" in raw_response:
                prompt_tokens = raw_response["usage"].get("prompt_tokens", 0)
                completion_tokens = raw_response["usage"].get("completion_tokens", 0)
                
            return {
                "ok": True,
                "provider": "lm_studio",
                "model": raw_response.get("model", model or self.default_model or "unknown"),
                "latency": 0.0, # Handled by telemetry wrapper
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "choices": choices,
                "raw": raw_response
            }

        except Exception as e:
            return {
                "ok": False,
                "provider": "lm_studio",
                "model": model or self.default_model or "unknown",
                "error": str(e),
                "raw": {}
            }

    def _chat_internal(self, messages: Union[List[Dict[str, Any]], str], model: Optional[str] = None, stream: bool = False, format_json: bool = False, timeout: Optional[float] = None, **kwargs) -> Any:
        # Prefer explicit model parameter or static provider-configured model list.
        # Do NOT call the models API here to avoid implicitly loading models on the server.
        model_name = model
        if not model_name:
            # Use statically configured models (from providers.json) if present
            if isinstance(self.models, list) and len(self.models) > 0:
                # self.models already holds authoritative ids (full ids if provided)
                model_name = self.models[0]
            else:
                # No model configured locally — do not attempt to probe the server which might trigger loads
                return {"error": "no_model_configured", "user_message": "No model configured for LM Studio adapter. Add a model to providers.json or pass model explicitly."}
        # If model provided is a short name, resolve using static provider config only (do not call API)
        model_name = self.resolve_model_name(model_name)
        payload: Dict[str, Any]
        if isinstance(messages, (list, tuple)):
            # include extra kwargs (options, response_format, etc.) to allow calling code to pass runtime options
            payload = {"model": model_name, "messages": messages, **kwargs}
            ep = self._compose('chat/completions') or self._compose('responses')
        else:
            payload = {"model": model_name, "input": str(messages)}
            ep = self._compose('responses') or self._compose('chat')
        if not ep:
            return {"message": {"role": "assistant", "content": "", "parsed": None}}
        try:
            # log payload for debugging when AGENT_DEBUG or adapter debug enabled
            try:
                import os as _os
                if _os.getenv('AGENT_DEBUG') in ('1', 'true', 'True'):
                    # Log keys and truncated payload for debugging
                    _logger.debug('LMStudioAdapter.chat POST %s payload keys: %s', ep, list(payload.keys()))
                    try:
                        import json as _json
                        txt = _json.dumps(payload, default=str)
                        _logger.debug('LMStudioAdapter.chat payload (truncated): %s', txt[:2000])
                    except Exception:
                        pass
                    # if tools provided, show names
                    if 'tools' in payload and isinstance(payload['tools'], list):
                        try:
                            names = [t.get('function', {}).get('name') if isinstance(t, dict) else str(t) for t in payload['tools']]
                            _logger.debug('LMStudioAdapter.chat tools: %s', names)
                        except Exception:
                            _logger.debug('LMStudioAdapter.chat tools present but failed to enumerate')
            except Exception:
                pass

            # For OpenAI compatibility, some clients expect 'functions' key; copy tools to functions if present
            try:
                if 'tools' in payload and 'functions' not in payload:
                    payload['functions'] = payload['tools']
            except Exception:
                pass

            r = self._safe_post(ep, self._headers(), payload, timeout=self.DEFAULT_TIMEOUT, stream=stream)
            if stream:
                return r
            try:
                r.raise_for_status()
            except Exception as he:
                # include response body if available to surface model load errors
                resp = getattr(he, 'response', None) or r
                body = None
                try:
                    body = resp.json()
                except Exception:
                    try:
                        body = resp.text
                    except Exception:
                        body = str(he)
                # Build a helpful user-facing message and suggestions when possible
                user_message = None
                suggestions = []
                try:
                    # LM Studio returns nested error message in body['error']['message']
                    if isinstance(body, dict):
                        err = body.get('error') or body.get('errors')
                        if isinstance(err, dict):
                            msg = err.get('message') or ''
                            msg_lower = str(msg).lower()
                            if 'insufficient system resources' in msg_lower or 'failed to load model' in msg_lower or 'requires approximately' in msg_lower:
                                user_message = str(msg)
                                # try to extract approximate GB requirement
                                import re
                                m = re.search(r"requires approximately ([0-9]+(?:\.[0-9]+)?) ?GB", msg, re.IGNORECASE)
                                if m:
                                    gb = m.group(1)
                                    suggestions.append(f"This model needs ~{gb} GB RAM to load. Consider using a smaller model or increasing available system memory.")
                                suggestions.append("Open LM Studio Developer tab and adjust model loading guardrails or load a smaller model.")
                                suggestions.append("Alternatively, update the provider config to use a smaller model and re-run tests.")
                            else:
                                # generic error message from provider
                                user_message = str(msg)
                except Exception:
                    user_message = None

                meta = {"error": "http_error", "status_code": getattr(resp, 'status_code', None), "body": body}
                result = {"meta": meta}
                if user_message:
                    result.update({"user_message": user_message, "suggestions": suggestions})
                return result
            try:
                return r.json()
            except Exception:
                return {"message": {"role": "assistant", "content": r.text, "parsed": None}}
        except requests.exceptions.RequestException as e:
            warnings.warn(f'LMStudioAdapter.chat request failed: {e}')
            return {"error": "request_exception", "message": str(e)}
        except Exception as e:
            warnings.warn(f'LMStudioAdapter.chat unexpected error: {e}')
            return {"error": "unexpected", "message": str(e)}

    def extract_tool_calls(self, chat_response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract structured tool call data from a chat response.

        Returns list of {"name":..., "args":...} for each tool call detected.
        """
        if isinstance(chat_response, dict):
            # check for top-level 'tool_calls' key (OpenAI function calling)
            if 'tool_calls' in chat_response:
                calls = chat_response['tool_calls']
                if isinstance(calls, list):
                    out = []
                    for c in calls:
                        if isinstance(c, dict):
                            # require name and args (OpenAI function calling)
                            name = c.get('name')
                            args = c.get('arguments') or c.get('args')
                            if name and isinstance(args, dict):
                                out.append({"name": name, "args": args})
                    return out
            # check for legacy 'function_call' key (previous experimental approach)
            if 'function_call' in chat_response:
                call = chat_response['function_call']
                if isinstance(call, dict):
                    name = call.get('name')
                    args = call.get('arguments') or call.get('args')
                    if name and isinstance(args, dict):
                        return [{"name": name, "args": args}]
        return []

    def resolve_model_name(self, model_name: str) -> str:
        """Resolve a short model name (no slash) to the LM Studio full id when possible.
        If model_name already contains a '/', assume it's a full id and return as-is.
        Otherwise, check configured provider static models (do NOT call the API) and find an entry whose short name matches; return the full id if found, else return the original name.
        """
        try:
            if not model_name or '/' in model_name:
                return model_name
            # normalize candidate short names for comparison (consider ':' and '-' variants)
            def variants(s: str):
                vs = {s}
                vs.add(s.replace(':', '-'))
                vs.add(s.replace('-', ':'))
                vs.add(s.replace(':', '/'))
                vs.add(s.replace('-', '/'))
                return vs

            # check configured provider static models only to avoid server probes
            if self.models:
                for m in self.models:
                    if isinstance(m, dict):
                        raw_key = m.get('id') or m.get('key') or m.get('name')
                    else:
                        raw_key = m
                    if raw_key:
                        short = str(raw_key).split('/')[-1]
                        if short == model_name or model_name in variants(short) or short in variants(model_name):
                            return str(raw_key)

            # If not found in static config, try the API
            api_models = self.get_models_from_api()
            if api_models and isinstance(api_models.get('models'), list):
                for m in api_models['models']:
                    if isinstance(m, dict):
                        raw_key = m.get('id') or m.get('key') or m.get('name')
                        short = str(raw_key).split('/')[-1] if raw_key else None
                        if short == model_name or model_name in variants(short) or short in variants(model_name):
                            return str(raw_key)
        except Exception:
            pass
        # not found, return original
        return model_name

    def _safe_post(self, url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: Optional[float] = None, stream: bool = False):
        """Post helper that tolerates test monkeypatches with different signatures.

        Returns a requests-like response object or raises the underlying exception.
        """
        if timeout is None:
            timeout = self.DEFAULT_TIMEOUT
        try:
            # Try canonical signature
            return requests.post(url, headers=headers, json=payload, timeout=timeout, stream=stream)
        except TypeError:
            # Try without timeout
            try:
                return requests.post(url, headers=headers, json=payload, stream=stream)
            except TypeError:
                # Try positional variants used by test fakes
                try:
                    return requests.post(url, payload, stream)
                except TypeError:
                    try:
                        return requests.post(url, payload)
                    except Exception:
                        raise
        except requests.exceptions.RequestException:
            raise

