"""Provider activation and role-model switching helpers for AgentBridge (TASK-36/38)."""

from __future__ import annotations

import json as _json
import os as _os
import tempfile as _tf
from typing import Any

from .logging import get_logger

logger = get_logger("bridge")


def normalize_orchestrator_history_on_switch(orch: Any) -> None:
    """Flatten native tool calls and strip reasoning signatures on model/provider switch (TASK-36)."""
    try:
        msg_mgr = getattr(orch, "msg_mgr", None)
        if msg_mgr is None or not getattr(msg_mgr, "messages", None):
            return
        from src.core.inference.history_normalizer import (
            normalize_history_for_model_switch,
        )

        lock = getattr(orch, "_msg_mgr_lock", None)
        if lock is not None:
            with lock:
                msg_mgr.messages = normalize_history_for_model_switch(msg_mgr.messages)
        else:
            msg_mgr.messages = normalize_history_for_model_switch(msg_mgr.messages)
    except Exception as exc:
        logger.debug("history normalization on switch skipped: %s", exc)


def activate_provider_impl(bridge: Any, provider_id: str) -> bool:
    """Persist active provider atomically via mkstemp + os.replace and swap live adapter."""
    try:
        from src.core.inference.llm_manager import (
            _providers_json_lock,
            canonical_provider,
            get_provider_manager,
            resolve_config_path,
        )

        norm_id = canonical_provider(provider_id)
        pm = get_provider_manager()
        cfg_path = resolve_config_path(None)
        try:
            from src.core.inference.provider_config import is_bundled_providers_config

            is_bundled = is_bundled_providers_config(cfg_path)
        except Exception:
            is_bundled = False

        if not is_bundled and cfg_path.exists():
            with _providers_json_lock:
                raw = _json.loads(cfg_path.read_text(encoding="utf-8"))
                entries = raw if isinstance(raw, list) else [raw]
                for p in entries:
                    if isinstance(p, dict):
                        key = canonical_provider(p.get("type") or p.get("name") or "")
                        p["active"] = key == norm_id
                new_text = _json.dumps(entries, indent=2)
                fd, tmp = _tf.mkstemp(dir=cfg_path.parent, suffix=".tmp")
                try:
                    with _os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(new_text)
                    _os.replace(tmp, cfg_path)
                except Exception:
                    try:
                        _os.unlink(tmp)
                    except OSError:
                        pass
                    raise

        try:
            from src.core.user_prefs import UserPrefs

            prefs = UserPrefs.load()
            prefs.selected_model_provider = norm_id
            prefs.save()
        except Exception:
            pass

        adapter = pm.get_provider(norm_id)
        orch = getattr(bridge, "_orchestrator", None)
        if orch is not None:
            orch.adapter = adapter
            normalize_orchestrator_history_on_switch(orch)
        try:
            bridge._publish_active_provider_status()
        except Exception:
            pass
        return True
    except Exception as exc:
        logger.error("activate_provider(%s) failed: %s", provider_id, exc)
        return False


def update_role_model_impl(
    bridge: Any, model_id: str, provider_id: str | None = None
) -> None:
    """Switch provider (if needed) and update the active adapter's model."""
    if provider_id:
        try:
            from src.core.inference.llm_manager import canonical_provider

            norm_req = canonical_provider(provider_id)
            orch = getattr(bridge, "_orchestrator", None)
            current_adapter = getattr(orch, "_adapter", None) if orch else None
            current_key = None
            if current_adapter is not None:
                prov_dict = getattr(current_adapter, "provider", None)
                raw = (
                    (prov_dict.get("name") or prov_dict.get("type") or "")
                    if isinstance(prov_dict, dict)
                    else ""
                )
                current_key = canonical_provider(
                    raw or getattr(current_adapter, "name", "") or ""
                )
            if current_key != norm_req:
                bridge.activate_provider(provider_id)
        except Exception as exc:
            logger.debug("update_role_model provider switch: %s", exc)

    try:
        orch = getattr(bridge, "_orchestrator", None)
        adapter = getattr(orch, "_adapter", None) if orch is not None else None
        if (
            adapter is None
            or not isinstance(model_id, str)
            or not model_id.strip()
            or "MagicMock" in model_id
        ):
            return
        clean_id = model_id.strip()
        for attr in ("default_model", "model", "_model"):
            if hasattr(adapter, attr):
                try:
                    setattr(adapter, attr, clean_id)
                except Exception:
                    pass
                break
        if hasattr(adapter, "models") and isinstance(adapter.models, list):
            valid = [
                m
                for m in adapter.models
                if isinstance(m, str) and m.strip() and "MagicMock" not in m
            ]
            if clean_id in valid:
                valid.remove(clean_id)
            valid.insert(0, clean_id)
            adapter.models[:] = valid
        if orch is not None:
            normalize_orchestrator_history_on_switch(orch)
            if hasattr(orch, "_publish_active_config"):
                orch._publish_active_config()
    except Exception as exc:
        logger.debug("update_role_model adapter update: %s", exc)
