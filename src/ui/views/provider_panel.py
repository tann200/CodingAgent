"""Provider panel view: displays providers, models and allows switching.

This is a non-UI testable controller that will be connected to the real TUI later.
"""
from __future__ import annotations

from typing import List
from src.core.llm_manager import get_provider_manager
from src.core.orchestration.event_bus import EventBus


class ProviderPanelController:
    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self.pm = get_provider_manager()
        self.providers = []
        if self.pm._initialized:
            self.providers = self.pm.list_providers()
        # subscribe for updates
        self.event_bus.subscribe('provider.models.list', self._on_models_list)

    def _on_models_list(self, payload):
        try:
            provider = payload.get('provider')
            models = payload.get('models')
            # store or update locally
            self.providers = list(set(self.providers + [provider]))
        except Exception:
            pass

    def list_providers(self) -> List[str]:
        return self.providers

