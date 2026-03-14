"""Settings panel controller for the TUI.

Provides provider selection, model selection and a "new session" action. The
controller reads the providers configuration file (via llm_manager.resolve_config_path)
and updates ONLY the `models` field for the selected provider when models are
fetched from the provider's runtime endpoint.

This controller is UI-framework-agnostic and can be used by both the headless
stub and the Textual implementation.
"""
from __future__ import annotations

from typing import List, Optional
from src.core.llm_manager import get_provider_manager
from src.core.logger import logger as guilogger
from src.core.orchestration.event_bus import get_event_bus
from src.core.user_prefs import UserPrefs
import json
import time
import threading


class SettingsPanelController:
    def __init__(self):
        self.pm = get_provider_manager()
        self.event_bus = get_event_bus()

    def list_providers(self) -> List[str]:
        try:
            return self.pm.list_providers()
        except Exception:
            return []

    def get_cached_models(self, provider_key: str) -> List[str]:
        try:
            return self.pm.get_cached_models(provider_key)
        except Exception:
            return []

    def fetch_models_from_provider(self, provider_key: str) -> bool:
        """Start a background fetch of models for provider_key.

        Returns True if the fetch was scheduled successfully. The actual models
        update is performed asynchronously and when completed the controller
        publishes 'provider.models.updated' and 'provider.models.cached' events.
        For tests that need synchronous behavior, call
        `fetch_models_from_provider_sync(provider_key)` instead.
        """
        try:
            t = threading.Thread(target=self.fetch_models_from_provider_sync, args=(provider_key,), daemon=True)
            t.start()
            return True
        except Exception as e:
            guilogger.warning(f"SettingsPanel: failed to start background fetch: {e}")
            return False

    def fetch_models_from_provider_sync(self, provider_key: str) -> List[str]:
        """Synchronously call the adapter's get_models_from_api() and update providers.json models field only.

        Returns the list of model identifiers (strings) on success, otherwise []
        """
        prov = self.pm.get_provider(provider_key)
        if not prov:
            guilogger.warning(f"SettingsPanel: provider '{provider_key}' not found")
            return []
        try:
            if not hasattr(prov, 'get_models_from_api'):
                guilogger.warning(f"SettingsPanel: provider adapter for {provider_key} has no get_models_from_api")
                return []
            resp = prov.get_models_from_api()
            models = []
            if isinstance(resp, dict):
                for m in resp.get('models', []):
                    if isinstance(m, dict):
                        fid = m.get('id') or m.get('key') or m.get('name')
                        if fid:
                            models.append(fid)
                    elif isinstance(m, str):
                        models.append(m)
            # update providers.json models for this provider only (do not overwrite other keys)
            try:
                # import dynamically so tests can monkeypatch src.core.llm_manager.resolve_config_path
                import src.core.llm_manager as _llm_manager
                cfg_path = _llm_manager.resolve_config_path(None)
                raw_text = cfg_path.read_text(encoding='utf-8')
                raw = json.loads(raw_text)
                changed = False
                if isinstance(raw, list):
                    for p in raw:
                        name = (p.get('name') or '').lower().replace(' ', '_')
                        if name == provider_key:
                            p['models'] = models
                            changed = True
                            break
                elif isinstance(raw, dict):
                    name = (raw.get('name') or '').lower().replace(' ', '_')
                    if name == provider_key:
                        raw['models'] = models
                        changed = True
                if changed:
                    try:
                        cfg_path.write_text(json.dumps(raw, indent=2), encoding='utf-8')
                        guilogger.info(f"SettingsPanel: updated models for provider {provider_key} in {cfg_path}")
                        # publish event so ProviderManager/Orchestrator and UI can refresh
                        try:
                            if self.event_bus:
                                self.event_bus.publish('provider.models.updated', {'provider': provider_key, 'models': models})
                        except Exception:
                            pass
                    except Exception as e:
                        guilogger.warning(f"SettingsPanel: failed to write providers.json: {e}")
                return models
            except Exception as e:
                guilogger.warning(f"SettingsPanel: failed to update providers.json: {e}")
                return models
        except Exception as e:
            guilogger.warning(f"SettingsPanel.fetch_models_from_provider failed: {e}")
            return []

    def select_provider_and_model(self, provider_key: str, model_name: Optional[str]) -> bool:
        """Select a provider and optionally a model. Publish events so other components
        (Orchestrator/UI) can react. This will persist selection to UserPrefs when available.
        """
        try:
            # persist to user prefs if available
            try:
                prefs = UserPrefs.load()
                # set attributes defensively
                try:
                    prefs.selected_model_provider = provider_key
                except Exception:
                    try:
                        prefs.data['selected_model_provider'] = provider_key
                    except Exception:
                        pass
                try:
                    prefs.selected_model_name = model_name
                except Exception:
                    try:
                        prefs.data['selected_model_name'] = model_name
                    except Exception:
                        pass
                # attempt to save
                try:
                    prefs.save()
                except Exception:
                    pass
            except Exception:
                # no prefs available; continue
                pass

            # publish selection event
            try:
                if self.event_bus:
                    self.event_bus.publish('provider.selection.changed', {'provider': provider_key, 'model': model_name})
            except Exception:
                pass
            return True
        except Exception as e:
            guilogger.error(f"SettingsPanel.select_provider_and_model failed: {e}")
            return False

    def start_new_session(self) -> None:
        """Emit an event to start a new session. Listeners should clear conversation state."""
        try:
            if self.event_bus:
                self.event_bus.publish('session.new', {'timestamp': time.time()})
        except Exception:
            try:
                # fallback if get_event_bus not available
                eb = get_event_bus()
                eb.publish('session.new', {'timestamp': time.time()})
            except Exception:
                pass
