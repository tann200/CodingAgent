from __future__ import annotations
from typing import Any, Dict, Optional, Callable
import time
import logging

logger = logging.getLogger(__name__)

def publish_model_response(event_bus: Any, provider: str, model: str, prompt_tokens: int, completion_tokens: int, total_tokens: int, latency: float, extra: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        'provider': provider,
        'model': model,
        'prompt_tokens': int(prompt_tokens or 0),
        'completion_tokens': int(completion_tokens or 0),
        'total_tokens': int(total_tokens or 0),
        'latency': float(latency or 0.0),
        'ts': time.time(),
    }
    if extra:
        try:
            payload['extra'] = extra
        except Exception:
            pass
    try:
        if event_bus and hasattr(event_bus, 'publish'):
            event_bus.publish('model.response', payload)
    except Exception:
        # best-effort
        pass

def with_telemetry(func: Callable) -> Callable:
    """Telemetry wrapper to log latency, tokens, and model info."""
    def wrapper(self, *args, **kwargs):
        start_time = time.time()
        try:
            response = func(self, *args, **kwargs)
            latency = time.time() - start_time
            
            # Extract info from standardized payload
            if isinstance(response, dict):
                response['latency'] = latency
                provider = response.get('provider', getattr(self, 'name', 'unknown'))
                model = response.get('model', 'unknown')
                p_tokens = response.get('prompt_tokens', 0)
                c_tokens = response.get('completion_tokens', 0)
                
                logger.info(
                    f"[LLM Telemetry] provider={provider} "
                    f"model={model} "
                    f"latency={latency:.2f}s "
                    f"prompt_tokens={p_tokens} "
                    f"completion_tokens={c_tokens}"
                )
                
                # Attempt to publish event if provider manager is accessible
                try:
                    from src.core.llm_manager import _provider_manager
                    if hasattr(_provider_manager, '_event_bus'):
                        publish_model_response(
                            _provider_manager._event_bus,
                            provider,
                            model,
                            p_tokens,
                            c_tokens,
                            p_tokens + c_tokens,
                            latency
                        )
                except Exception:
                    pass
            
            return response
        except Exception as e:
            latency = time.time() - start_time
            logger.error(
                f"[LLM Telemetry] FAILED provider={getattr(self, 'name', 'unknown')} "
                f"latency={latency:.2f}s error={str(e)}"
            )
            raise e
    return wrapper
