"""``codingagent status`` — print agent/provider/model/runtime status.

Mirrors the TUI ``/status`` panel as a headless, scriptable output.
"""

from __future__ import annotations

import json
from typing import Any

from src.cli._helpers import (
    cli_version,
    git_branch,
    print_json,
    python_version,
    resolve_workdir,
    runtime_os,
)


def _active_provider(workdir: str | None = None) -> dict:
    """Return ``{provider, model}`` for the active provider (UserPrefs + workspace settings)."""
    provider_name = ""
    model_name = ""
    try:
        import src.core.inference.provider_config as _pc

        cfg_path = _pc.resolve_providers_config_path(None, _pc.__file__)
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        providers = raw if isinstance(raw, list) else [raw]
        if _pc.is_bundled_providers_config(cfg_path):
            providers = _pc.apply_user_prefs_to_providers(
                providers, _pc.canonical_provider_name
            )
        for p in providers:
            if not isinstance(p, dict):
                continue
            if p.get("active") is True:
                provider_name = str(p.get("name") or p.get("type") or "")
                model = p.get("default_model")
                if not model:
                    models = p.get("models") or []
                    if models:
                        first = models[0]
                        model = first if isinstance(first, str) else (
                            first.get("id") or first.get("name") if isinstance(first, dict) else None
                        )
                model_name = str(model) if model else ""
                break
    except Exception:
        pass

    # Fallback / explicit overrides from UserPrefs
    try:
        from src.core.user_prefs import UserPrefs

        prefs = UserPrefs.load()
        if not provider_name and prefs.selected_model_provider:
            provider_name = str(prefs.selected_model_provider)
        if not model_name and prefs.selected_model_name:
            model_name = str(prefs.selected_model_name)
    except Exception:
        pass

    return {"provider": provider_name, "model": model_name}


def _apply_workspace_provider_overrides(active: dict, workdir: str) -> dict:
    """Layer workspace-scoped config/settings provider & model overrides if present."""
    result = dict(active)
    try:
        from pathlib import Path

        ctx_dir = Path(workdir) / ".codingAgent"
        for fname in ("config.json", "config.local.json", "settings.json", "settings.local.json"):
            p = ctx_dir / fname
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            ws_prov = data.get("default_provider") or data.get("provider")
            ws_model = data.get("default_model") or data.get("model")
            if isinstance(ws_prov, str) and ws_prov.strip():
                result["provider"] = ws_prov.strip()
            if isinstance(ws_model, str) and ws_model.strip():
                result["model"] = ws_model.strip()
    except Exception:
        pass
    return result


def run_status(args: Any) -> int:
    workdir = resolve_workdir(getattr(args, "workdir", None))
    active = _apply_workspace_provider_overrides(_active_provider(), workdir)

    payload: dict = {
        "version": cli_version(),
        "runtime": {
            "python": python_version(),
            "os": runtime_os(),
        },
        "provider": active["provider"],
        "model": active["model"],
        "workdir": workdir,
        "git_branch": git_branch(workdir),
    }

    try:
        from src.core.memory.session_store import SessionStore

        store = SessionStore(workdir=workdir)
        try:
            payload["sessions_available"] = len(store.list_sessions())
        finally:
            store.close()
    except Exception:
        payload["sessions_available"] = 0

    # Phase 3.8: resolve ModelRuntimePolicy for active provider/model
    try:
        from src.core.orchestration.graph.runtime_policy import resolve_runtime_policy

        if active["model"]:
            pol = resolve_runtime_policy(model=active["model"], provider=active["provider"])
            payload["policy"] = {
                "tier": pol.policy_tier,
                "toolset": pol.toolset_name,
                "tool_limit": pol.tool_limit,
                "verification_mode": pol.verification_mode,
                "parallel_reads": pol.max_parallel_reads,
                "turn_limit": pol.turn_limit,
            }
    except Exception:
        pass

    if getattr(args, "json", False):
        print_json(payload)
        return 0

    print(f"CodingAgent {payload['version']}")
    print(f"  Runtime:     {payload['runtime']['python']} ({payload['runtime']['os']})")
    print(f"  Provider:    {payload['provider'] or '(none configured)'}")
    print(f"  Model:       {payload['model'] or '(default)'}")
    if "policy" in payload:
        p_info = payload["policy"]
        print(f"  Policy Tier: {p_info['tier']} (toolset: {p_info['toolset']} [{p_info['tool_limit']} tools], verify: {p_info['verification_mode']})")
    print(f"  WorkDir:     {payload['workdir']}")
    if payload["git_branch"]:
        print(f"  Git branch:  {payload['git_branch']}")
    print(f"  Sessions:    {payload['sessions_available']} saved")
    return 0
