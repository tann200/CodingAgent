from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import asyncio


class LLMClient(ABC):
    @abstractmethod
    def generate(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        stream: bool = False,
        timeout: Optional[float] = None,
        provider: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Synchronous call: return normalized payload (see below)."""

    async def agenerate(self, *args, **kwargs) -> Dict[str, Any]:
        """Optional async wrapper; adapters may implement via `asyncio.to_thread`."""
        # Prefer run_with_correlation so ContextVars (eg. correlation id) are
        # preserved when adapters run in worker threads. Fall back to
        # asyncio.to_thread if the helper isn't available.
        loop = asyncio.get_running_loop()
        run_with_corr = None
        try:
            # Preferred: central helper that copies context into thread
            from src.core.orchestration.event_bus import (
                run_with_correlation as run_with_corr,
            )
        except Exception:
            try:
                # Fallback: the llm_manager defines a compatible helper when
                # direct event_bus import would create a circular import.
                from src.core.inference.llm_manager import (
                    run_with_correlation as run_with_corr,
                )
            except Exception:
                run_with_corr = None

        if run_with_corr:
            return await run_with_corr(loop, None, self.generate, *args, **kwargs)

        # Best-effort fallback: try to preserve ContextVars by copying the
        # current context and running ctx.run in the thread executor. If that
        # fails, finally fall back to asyncio.to_thread.
        try:
            import contextvars as _contextvars
            import functools as _functools

            ctx = _contextvars.copy_context()
            fn = _functools.partial(ctx.run, self.generate, *args, **kwargs)
            from typing import cast

            return cast(Dict[str, Any], await loop.run_in_executor(None, fn))
        except Exception:
            return await asyncio.to_thread(self.generate, *args, **kwargs)
