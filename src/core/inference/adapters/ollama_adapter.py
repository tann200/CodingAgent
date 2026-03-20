from __future__ import annotations
from typing import Any, Dict, List, Optional
from pathlib import Path
import json
import warnings
import logging
import requests

# Import helper shims from llm_manager with safe fallbacks
try:
    from src.core.inference.llm_manager import (
        lm_resolve_config_path,
        lm_load_provider,
        lm_save_provider,
        lm_select_model_name,
        lm_call_requests,
        lm_post_stream_compatible,
        LM_DEFAULT_TIMEOUT,
    )
except Exception:
    # Provide minimal fallbacks to keep adapter operational during tests
    def lm_resolve_config_path(p=None):
        return Path(p) if p else Path("config/providers.json")

    def lm_load_provider(p=None):
        try:
            with open(p or "config/providers.json", "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    def lm_save_provider(data, path=None, initial_path=None):
        try:
            target = (
                Path(initial_path)
                if initial_path
                else Path(path or "config/providers.json")
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data), encoding="utf-8")
            return True
        except Exception:
            return False

    def lm_select_model_name(models, requested=None):
        if not models:
            return None
        for m in models:
            if isinstance(m, dict):
                if (m.get("id") or m.get("name") or m.get("key")) == requested:
                    return requested
        return models[0] if models else None

    def lm_call_requests(method, url, **kwargs):
        return getattr(requests, method.lower())(url, **kwargs)

    def lm_post_stream_compatible(url, json_data=None, headers=None, timeout=None):
        return requests.post(url, json=json_data, timeout=timeout)

    LM_DEFAULT_TIMEOUT = 5

from src.core.inference.llm_client import LLMClient
from src.core.inference.telemetry import with_telemetry

_logger = logging.getLogger(__name__)

# Standard Ollama Generation Options
OLLAMA_OPTIONS = {
    "temperature",
    "seed",
    "num_predict",
    "top_k",
    "top_p",
    "tfs_z",
    "typical_p",
    "repeat_last_n",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "mirostat",
    "mirostat_tau",
    "mirostat_eta",
    "penalize_newline",
    "stop",
    "num_keep",
    "num_ctx",
}


class OllamaAdapter(LLMClient):
    # Increased timeout from 5 to 120 to allow for local model loading into VRAM
    DEFAULT_TIMEOUT = 120.0

    def __init__(self, config_path=None, name=None, base_url=None, api_key=None, models=None):
        # Keep initial_config_path for save semantics
        self._initial_config_path = Path(config_path) if config_path else None
        # Resolve provider from config_path only if no explicit base_url provided
        self.config_path = lm_resolve_config_path(config_path)
        self.provider = None
        if base_url is None:
            # attempt to load provider from config file (backwards compatibility)
            self.provider = lm_load_provider(self.config_path)
        if isinstance(self.provider, list) and len(self.provider) > 0:
            # Find the first ollama provider
            found = None
            for p in self.provider:
                if (
                    str(p.get("type")).lower() == "ollama"
                    or str(p.get("name")).lower() == "ollama"
                ):
                    found = p
                    break
            self.provider = found or self.provider[0]

        # If base_url provided by caller, prefer that and consider provider present
        self.base_url = base_url or (
            self.provider.get("base_url")
            if self.provider
            else "http://localhost:11434/api"
        )
        self.api_key = api_key or (
            self.provider.get("api_key") if self.provider else None
        )
        # models: prefer explicit models arg, then provider.models from config, otherwise empty list
        if models is not None:
            self.models = list(models)
        elif self.provider and isinstance(self.provider.get("models"), list):
            self.models = self.provider.get("models")
        else:
            self.models = []
        # missing_provider: False if either provider config exists or a base_url was explicitly provided
        self.missing_provider = False if (self.provider or base_url) else True

    def _save_provider(self):
        return lm_save_provider(
            self.provider, self.config_path, self._initial_config_path
        )

    def _select_model_name(self, model):
        return lm_select_model_name(self.models, model)

    def _base_variants(self) -> List[str]:
        if not self.base_url:
            return ["http://localhost:11434"]
        b = self.base_url.rstrip("/")
        variants = [b]
        if b.endswith("/api"):
            variants.append(b[:-4])
            variants.append(b + "/v1")
        if b.endswith("/v1"):
            variants.append(b[:-3])
            variants.append(b + "/api")
        # ensure common forms
        variants.append(b + "/api")
        variants.append(b + "/v1")
        # dedupe
        out = []
        seen = set()
        for v in variants:
            if v not in seen:
                seen.add(v)
                out.append(v)
        return out

    def _make_endpoints(self, path: str) -> List[str]:
        out = []
        for base in self._base_variants():
            out.append(base.rstrip("/") + "/" + path.lstrip("/"))
        return out

    def get_models_from_api(self):
        # Prefer canonical Ollama API: base may already include '/api'
        base = self.base_url.rstrip("/")
        candidates = []
        if base.endswith("/api"):
            candidates.append(base + "/tags")
            candidates.append(base[:-4] + "/tags")
        else:
            candidates.append(base + "/api/tags")
            candidates.append(base + "/tags")
        candidates.append(base + "/v1/tags")
        tried = []
        for url in candidates:
            try:
                tried.append(url)
                response = requests.get(url, timeout=10)
                try:
                    data = response.json()
                except Exception:
                    data = None
                if response.status_code >= 400:
                    if isinstance(data, dict) and data.get("error"):
                        continue
                    response.raise_for_status()
                # Ollama tags response often includes 'models' or a list of model dicts
                models_raw = None
                if isinstance(data, dict):
                    if "models" in data and isinstance(data["models"], list):
                        models_raw = data["models"]
                    elif "tags" in data and isinstance(data["tags"], list):
                        models_raw = data["tags"]
                    else:
                        # find first list in values
                        for v in data.values():
                            if isinstance(v, list):
                                models_raw = v
                                break
                elif isinstance(data, list):
                    models_raw = data
                if not models_raw:
                    continue
                models_out = []
                for item in models_raw:
                    if isinstance(item, dict):
                        raw_key = (
                            item.get("name")
                            or item.get("model")
                            or item.get("id")
                            or item.get("key")
                        )
                        name = (
                            str(raw_key).split("/")[-1] if raw_key is not None else None
                        )
                        if name:
                            models_out.append(name)
                    elif isinstance(item, str):
                        name = str(item).split("/")[-1]
                        models_out.append(name)
                if models_out:
                    return {"models": models_out}
            except requests.exceptions.ConnectionError:
                continue
            except Exception as e:
                warnings.warn(f"get_models_from_api failed for {url}: {e}")
                continue
        warnings.warn(f"get_models_from_api endpoints tried: {tried}")
        print(
            "Error: Ollama service not reachable or returned unexpected response for tags endpoints"
        )
        return {"models": []}

    def update_models_list(self):
        api_models = self.get_models_from_api()
        # Accept both [{'name':...},...] and ['name', ...] shapes from adapters/tests
        raw = api_models.get("models", []) or []
        normalized = []
        for m in raw:
            if isinstance(m, dict) and m.get("name"):
                normalized.append(m.get("name"))
            elif isinstance(m, str):
                normalized.append(m)
        if self.provider is None:
            self.provider = {}
        self.provider["models"] = normalized
        # Attempt to persist but don't fail tests if write fails
        self._save_provider()
        self.models = self.provider["models"]
        return self.models

    def get_model_info(self, model_name=None):
        if not self.models and model_name is None:
            print("Error: No models to load.")
            return {}
        model_name = self._select_model_name(model_name)
        if model_name is None:
            print("Error: Unable to find a model.")
            return {}
        # Try show endpoint variants
        endpoints = self._make_endpoints("/api/show") + self._make_endpoints("/show")
        payload = {"model": model_name}
        for url in endpoints:
            try:
                resp = self._call_requests("post", url, json=payload, timeout=20)
                if isinstance(resp, dict) and resp.get("meta"):
                    return resp
                # If _call_requests returned requests.Response, handle
                try:
                    if hasattr(resp, "status_code"):
                        if resp.status_code >= 400:
                            continue
                        return resp.json()
                except Exception:
                    pass
                if isinstance(resp, dict):
                    return resp
            except Exception:
                continue
        warnings.warn("get_model_info: no show endpoint succeeded")
        return {}

    def _parse_json_response_field(self, response_text):
        """Try to parse a response string as JSON and return python object."""
        if response_text is None:
            return None
        if isinstance(response_text, (dict, list)):
            return response_text
        try:
            text = response_text.strip()
            return json.loads(text)
        except Exception:
            return response_text

    def _extract_ollama_options(self, kwargs: dict) -> tuple[dict, dict]:
        """Separates Ollama-specific options from generic kwargs."""
        options = {}
        payload_kwargs = {}
        for k, v in kwargs.items():
            if k in OLLAMA_OPTIONS:
                options[k] = v
            else:
                payload_kwargs[k] = v
        return options, payload_kwargs

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
        """Generate a completion using Ollama. Returns normalized LLMClient payload."""
        try:
            raw_response = self._chat_internal(
                messages, model=model, stream=stream, timeout=timeout, **kwargs
            )

            if stream:
                return {
                    "ok": True,
                    "provider": "ollama",
                    "model": model or "unknown",
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
                    "provider": "ollama",
                    "model": model or "unknown",
                    "error": raw_response["meta"]["error"],
                    "raw": raw_response,
                }

            # Normalize Ollama response
            msg = raw_response.get("message", {})
            choice = {
                "message": {
                    "role": msg.get("role", "assistant"),
                    "content": msg.get("content", ""),
                },
                "finish_reason": "stop",
            }
            if "tool_calls" in msg:
                choice["tool_calls"] = msg["tool_calls"]

            p_tokens = raw_response.get("prompt_eval_count", 0)
            c_tokens = raw_response.get("eval_count", 0)

            return {
                "ok": True,
                "provider": "ollama",
                "model": raw_response.get("model", model or "unknown"),
                "latency": 0.0,
                "prompt_tokens": p_tokens,
                "completion_tokens": c_tokens,
                "total_tokens": p_tokens + c_tokens,
                "choices": [choice],
                "raw": raw_response,
            }
        except Exception as e:
            return {
                "ok": False,
                "provider": "ollama",
                "model": model or "unknown",
                "error": str(e),
                "raw": {},
            }

    def _generate_internal(
        self, prompt, model=None, stream=False, format_json=False, **kwargs
    ):
        """Original generate logic for backward compatibility."""
        model_name = self._select_model_name(model)
        if model_name is None:
            raise ValueError("No model available to generate from.")
        url = f"{self.base_url}/generate"

        # FIX: Extract Ollama options correctly
        options, payload_kwargs = self._extract_ollama_options(kwargs)

        payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": stream,
            **payload_kwargs,
        }
        if options:
            payload["options"] = options

        if format_json and "format" not in payload:
            payload["format"] = "json"

        try:
            if stream:
                try:
                    response = requests.post(
                        url, json=payload, stream=True, timeout=120
                    )
                except Exception:
                    response = self._post_stream_compatible(url, payload)
            else:
                response = self._call_requests(
                    "post", url, json=payload, stream=stream, timeout=120
                )
            if isinstance(response, dict) and response.get("meta"):
                return response
            response.raise_for_status()
            if stream:

                def _stream_gen(resp):
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        try:
                            chunk = (
                                line.decode()
                                if isinstance(line, (bytes, bytearray))
                                else str(line)
                            )
                            yield json.loads(chunk)
                        except Exception:
                            try:
                                yield {"raw": chunk}
                            except Exception:
                                yield {"raw_bytes": line}

                return _stream_gen(response)
            else:
                data = response.json()
                if format_json:
                    parsed = self._parse_json_response_field(data.get("response"))
                    if parsed is None or parsed == "" or parsed == data.get("response"):
                        meta = (
                            data.get("meta", {})
                            if isinstance(data.get("meta", {}), dict)
                            else {}
                        )
                        fallback_text = None
                        if isinstance(meta, dict):
                            fallback_text = meta.get("thinking") or meta.get("thoughts")
                        if not fallback_text:
                            fallback_text = data.get("thinking") or data.get("thoughts")
                        if fallback_text:
                            parsed_fb = self._parse_json_response_field(fallback_text)
                            if isinstance(parsed_fb, (dict, list)):
                                parsed = parsed_fb
                    return {
                        "model": data.get("model"),
                        "created_at": data.get("created_at"),
                        "response_raw": data.get("response"),
                        "response": parsed,
                        "meta": {
                            k: v
                            for k, v in data.items()
                            if k not in ("model", "created_at", "response")
                        },
                    }
                return data
        except requests.exceptions.ConnectionError:
            msg = "Ollama service not available: connection error"
            warnings.warn(msg)
            return {"meta": {"error": msg}}
        except Exception as e:
            warnings.warn(f"generate failed: {e}")
            return {"meta": {"error": "generate_failed", "exception": str(e)}}

    def _chat_internal(
        self,
        messages,
        model=None,
        stream=False,
        format_json=False,
        timeout=None,
        **kwargs,
    ):
        """Chat with the model using messages (list of {role, content})."""
        model_name = self._select_model_name(model)
        if model_name is None:
            raise ValueError("No model available for chat.")
        url = f"{self.base_url}/chat"

        # FIX: Extract Ollama options correctly so temperature/seed actually apply
        options, payload_kwargs = self._extract_ollama_options(kwargs)

        payload = {
            "model": model_name,
            "messages": messages,
            "stream": stream,
            **payload_kwargs,
        }
        if options:
            payload["options"] = options

        if format_json and "format" not in payload:
            payload["format"] = "json"

        try:
            if stream:
                try:
                    # Extended timeout for VRAM loading
                    response = requests.post(
                        url, json=payload, stream=True, timeout=120
                    )
                except Exception:
                    response = self._post_stream_compatible(url, payload)
            else:
                response = self._call_requests(
                    "post", url, json=payload, stream=stream, timeout=120
                )
            if isinstance(response, dict) and response.get("meta"):
                return response
            response.raise_for_status()
            if stream:

                def _stream_gen(resp):
                    for line in resp.iter_lines():
                        if not line:
                            continue
                        try:
                            chunk = (
                                line.decode()
                                if isinstance(line, (bytes, bytearray))
                                else str(line)
                            )
                            yield json.loads(chunk)
                        except Exception:
                            try:
                                yield {"raw": chunk}
                            except Exception:
                                yield {"raw_bytes": line}

                return _stream_gen(response)
            else:
                data = response.json()
                message = data.get("message") or {}
                content = message.get("content") if isinstance(message, dict) else None
                parsed = (
                    self._parse_json_response_field(content) if format_json else content
                )

                if format_json and (
                    parsed is None or parsed == "" or parsed == content
                ):
                    meta = (
                        data.get("meta", {})
                        if isinstance(data.get("meta", {}), dict)
                        else {}
                    )
                    fallback_text = None
                    if isinstance(meta, dict):
                        fallback_text = meta.get("thinking") or meta.get("thoughts")
                    if not fallback_text:
                        fallback_text = data.get("thinking") or data.get("thoughts")
                    if fallback_text:
                        parsed_fb = self._parse_json_response_field(fallback_text)
                        if isinstance(parsed_fb, (dict, list)):
                            parsed = parsed_fb

                if not format_json and isinstance(content, str):
                    try:
                        maybe = json.loads(content)
                        if isinstance(maybe, (dict, list)):
                            parsed = maybe
                    except Exception:
                        pass
                result = {
                    "model": data.get("model"),
                    "created_at": data.get("created_at"),
                    "message": message,
                    "response": parsed,
                    "meta": {
                        k: v
                        for k, v in data.items()
                        if k not in ("model", "created_at", "message")
                    },
                }
                if not isinstance(result, dict):
                    result = {"meta": {"error": "invalid_response_shape"}}
                if "response" not in result and "meta" not in result:
                    result["meta"] = {"note": "no_response"}
                return result
        except requests.exceptions.ConnectionError:
            msg = "Ollama service not available: connection error"
            warnings.warn(msg)
            return {"meta": {"error": msg}}
        except Exception as e:
            warnings.warn(f"chat failed: {e}")
            return {"meta": {"error": "chat_failed", "exception": str(e)}}

    def extract_tool_calls(self, chat_response):
        message = (
            chat_response.get("message") if isinstance(chat_response, dict) else None
        )
        if not message:
            return []
        tool_calls = message.get("tool_calls") or message.get("tool_calls", [])
        if tool_calls:
            return tool_calls
        content = message.get("content")
        parsed = self._parse_json_response_field(content)
        if isinstance(parsed, dict) and parsed.get("tool_call"):
            return [parsed.get("tool_call")]
        return []

    def _do_request(self, method: str, url: str, **kwargs):
        timeout = kwargs.pop("timeout", self.DEFAULT_TIMEOUT)
        m = method.lower()
        if m == "post":
            return requests.post(url, timeout=timeout, **kwargs)
        if m == "get":
            return requests.get(url, timeout=timeout, **kwargs)
        return requests.request(method, url, timeout=timeout, **kwargs)

    def _call_requests(self, method: str, url: str, **kwargs):
        return lm_call_requests(method, url, **kwargs)

    def _post_stream_compatible(self, url: str, payload: dict):
        return lm_post_stream_compatible(url, payload)
