#!/usr/bin/env python3
"""Diagnostic script: verify ProviderManager probes models only once at startup.

Usage: PYTHONPATH=. python3 tests/scripts/check_models_probe.py
"""
import importlib
import sys
import time

# Ensure project is importable
# Monkeypatch LMStudioAdapter.get_models_from_api to count calls
from src.adapters import lm_studio_adapter as lm_mod
importlib.reload(lm_mod)
from src.adapters.lm_studio_adapter import LMStudioAdapter
orig = LMStudioAdapter.get_models_from_api
call_count = {'n': 0}


def fake_get_models(self):
    call_count['n'] += 1
    print(f'[diag] LMStudioAdapter.get_models_from_api called #{call_count["n"]}', flush=True)
    # return a plausible shape
    return {"models": [{"id": "qwen/qwen3.5-9b", "display_name": "qwen3.5-9b"}]}

LMStudioAdapter.get_models_from_api = fake_get_models

# Reset ProviderManager singleton state
import src.core.llm_manager as lm
# Reset internals to force re-initialize
lm._provider_manager._initialized = False
lm._provider_manager._providers = {}
lm._provider_manager._models_cache = {}
print('[diag] Reset _provider_manager internal state', flush=True)

# Run initializer (sync shim)
print('[diag] Running ProviderManager.initialize...', flush=True)
lm._ensure_provider_manager_initialized_sync()
print('[diag] ProviderManager.initialize completed', flush=True)
pm = lm.get_provider_manager()
print('[diag] Providers:', pm.list_providers(), flush=True)
print('[diag] Cached models for lm_studio:', pm.get_cached_models('lm_studio'), flush=True)
print('[diag] fake call count after init:', call_count['n'], flush=True)

# Create adapter instance and orchestrator
ad = LMStudioAdapter(base_url='http://localhost:1234/v1')
print('[diag] Adapter instantiated; static models in adapter:', getattr(ad, 'models', None), flush=True)
from src.core.orchestration.orchestrator import Orchestrator
print('[diag] Creating Orchestrator (which spawns background probe)...', flush=True)
orch = Orchestrator(adapter=ad)
# give background thread a moment to run
time.sleep(1)
print('[diag] Background probe likely ran; final fake call count:', call_count['n'], flush=True)

# Call get_available_models twice to ensure cache is used
import asyncio
print('[diag] First get_available_models call result:', asyncio.run(lm.get_available_models('', '', 'lm_studio')), flush=True)
print('[diag] Second get_available_models call result:', asyncio.run(lm.get_available_models('', '', 'lm_studio')), flush=True)
print('[diag] Final fake call count:', call_count['n'], flush=True)

# restore original method
LMStudioAdapter.get_models_from_api = orig
print('[diag] Restored original method, exiting.', flush=True)

