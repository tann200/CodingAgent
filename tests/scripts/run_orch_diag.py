#!/usr/bin/env python3
"""Run a quick orchestrator diagnostic: instantiate Orchestrator and listen to events."""

import time
import traceback


try:
    from src.core.orchestration.orchestrator import Orchestrator
    from src.core.inference.llm_manager import get_provider_manager

    print("Imported Orchestrator and ProviderManager")
except Exception as e:
    print("Import error:", e)
    traceback.print_exc()
    raise

try:
    orch = Orchestrator()
    print("Orchestrator created")
    pm = get_provider_manager()
    print("Provider manager initialized:", pm._initialized)
    print("Providers:", pm.list_providers())

    # subscribe to some orchestrator events
    def on_startup(payload):
        print("EVENT: orchestrator.startup", payload)

    def on_models_cached(payload):
        print("EVENT: provider.models.cached", payload)

    def on_models_empty(payload):
        print("EVENT: provider.models.empty", payload)

    orch.event_bus.subscribe("orchestrator.startup", on_startup)
    orch.event_bus.subscribe("provider.models.cached", on_models_cached)
    orch.event_bus.subscribe("provider.models.empty", on_models_empty)

    print("Waiting 3 seconds for background probes...")
    time.sleep(3)
    print("Done waiting")
except Exception as e:
    print("Runtime error:", e)
    traceback.print_exc()
