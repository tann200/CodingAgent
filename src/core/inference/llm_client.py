from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import asyncio

class LLMClient(ABC):
    @abstractmethod
    def generate(self,
                 messages: List[Dict[str, str]],
                 model: Optional[str] = None,
                 stream: bool = False,
                 timeout: Optional[float] = None,
                 provider: Optional[str] = None,
                 **kwargs) -> Dict[str, Any]:
        """Synchronous call: return normalized payload (see below)."""

    async def agenerate(self, *args, **kwargs) -> Dict[str, Any]:
        """Optional async wrapper; adapters may implement via `asyncio.to_thread`."""
        return await asyncio.to_thread(self.generate, *args, **kwargs)

