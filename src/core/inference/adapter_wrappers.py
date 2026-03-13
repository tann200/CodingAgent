from __future__ import annotations
from typing import Any, Dict, List, Optional
from time import time


class AdapterWrapper:
    """Wrap existing adapters (which may provide chat/generate) into a common generate() API.

    Usage:
        wrapper = AdapterWrapper(adapter, provider_name='lm_studio')
        out = wrapper.generate(messages)
    """

    def __init__(self, adapter: Any, provider_name: Optional[str] = None, event_bus: Optional[Any] = None):
        self.adapter = adapter
        self.provider = provider_name or getattr(adapter, 'name', None) or 'unknown'
        self.event_bus = event_bus

    def _normalize_raw_models(self, models_raw: Any) -> List[str]:
        if not models_raw:
            return []
        out = []
        try:
            for m in models_raw:
                if isinstance(m, dict):
                    raw_key = m.get('id') or m.get('key') or m.get('name')
                    if raw_key:
                        out.append(str(raw_key))
                else:
                    out.append(str(m))
        except Exception:
            pass
        return out

    def generate(self, messages: List[Dict[str, Any]], model: Optional[str] = None, stream: bool = False, timeout: Optional[float] = None, **kwargs) -> Dict[str, Any]:
        start = time()
        # Prefer adapter.generate/chat if available
        try:
            if hasattr(self.adapter, 'chat'):
                # LMStudio/Ollama adapters typically expose chat(messages, model=...)
                res = self.adapter.chat(messages, model=model, stream=stream, format_json=False, **kwargs)
            elif hasattr(self.adapter, 'generate'):
                res = self.adapter.generate(messages, model=model, stream=stream, **kwargs)
            else:
                # Fallback: try call_model style (adapter may implement .call)
                if hasattr(self.adapter, 'call'):
                    res = self.adapter.call(messages, model=model, **kwargs)
                else:
                    return {"ok": False, "error": "adapter_missing_interface", "raw": None}
        except Exception as e:
            return {"ok": False, "error": str(e), "raw": None}

        elapsed = time() - start
        # Try to normalize common shapes
        try:
            # LMStudioAdapter.chat returns dict or json-like
            if isinstance(res, dict):
                # If it's a provider-style response, try to extract choices/message
                choices = []
                if res.get('choices') and isinstance(res.get('choices'), list):
                    for ch in res.get('choices'):
                        # try to find message content
                        msg = None
                        if isinstance(ch, dict):
                            if 'message' in ch:
                                msg = ch.get('message')
                            elif ch.get('text'):
                                msg = {'role': 'assistant', 'content': ch.get('text')}
                        if msg is None:
                            msg = {'role': 'assistant', 'content': str(ch)}
                        choices.append({'message': msg, 'finish_reason': ch.get('finish_reason')})
                elif res.get('message'):
                    # single message style
                    msg = res.get('message')
                    choices.append({'message': msg, 'finish_reason': res.get('finish_reason')})
                else:
                    # unknown structure -> embed raw as single assistant message
                    choices.append({'message': {'role': 'assistant', 'content': str(res)}})

                # token accounting may be missing; set 0 if not present
                prompt_tokens = int(res.get('usage', {}).get('prompt_tokens') or 0)
                completion_tokens = int(res.get('usage', {}).get('completion_tokens') or 0)
                total_tokens = int(res.get('usage', {}).get('total_tokens') or 0)

                out = {
                    'ok': True,
                    'provider': self.provider,
                    'model': model or res.get('model') or getattr(self.adapter, 'default_model', None),
                    'latency': elapsed,
                    'prompt_tokens': prompt_tokens,
                    'completion_tokens': completion_tokens,
                    'total_tokens': total_tokens,
                    'choices': choices,
                    'raw': res,
                }
                # publish telemetry if event_bus provided (best-effort)
                try:
                    if self.event_bus is not None:
                        try:
                            from src.core.inference.telemetry import publish_model_response
                        except Exception:
                            publish_model_response = None
                        try:
                            if publish_model_response:
                                publish_model_response(self.event_bus, out.get('provider'), out.get('model'), out.get('prompt_tokens', 0), out.get('completion_tokens', 0), out.get('total_tokens', 0), float(out.get('latency', 0.0)), extra={'adapter': 'wrapper'})
                        except Exception:
                            pass
                except Exception:
                    pass
                return out
            else:
                # non-dict response -> coerce
                out = {'ok': True, 'provider': self.provider, 'model': model, 'latency': elapsed, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'choices': [{'message': {'role': 'assistant', 'content': str(res)}}], 'raw': res}
                try:
                    if self.event_bus is not None:
                        try:
                            from src.core.inference.telemetry import publish_model_response
                        except Exception:
                            publish_model_response = None
                        try:
                            if publish_model_response:
                                publish_model_response(self.event_bus, out.get('provider'), out.get('model'), out.get('prompt_tokens', 0), out.get('completion_tokens', 0), out.get('total_tokens', 0), float(out.get('latency', 0.0)), extra={'adapter': 'wrapper'})
                        except Exception:
                            pass
                except Exception:
                    pass
                return out
        except Exception as e:
            return {'ok': False, 'error': str(e), 'raw': res}

