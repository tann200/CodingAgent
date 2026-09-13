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


def _active_provider() -> dict:
    """Return ``{provider, model}`` for the active entry in providers.json."""
    try:
        import src.core.inference.provider_config as _pc

        cfg_path = _pc.resolve_providers_config_path(None, _pc.__file__)
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        providers = raw if isinstance(raw, list) else [raw]
        for p in providers:
            if not isinstance(p, dict):
                continue
            if p.get("active") is True:
                name = str(p.get("name") or p.get("type") or "")
                model = p.get("default_model")
                if not model:
                    models = p.get("models") or []
                    if models:
                        first = models[0]
                        model = first if isinstance(first, str) else (
                            first.get("id") or first.get("name") if isinstance(first, dict) else None
                        )
                return {"provider": name, "model": str(model) if model else ""}
    except Exception:
        pass
    return {"provider": "", "model": ""}


def run_status(args: Any) -> int:
    workdir = resolve_workdir(getattr(args, "workdir", None))
    active = _active_provider()

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
        from src.core.orchestration.session_store import list_sessions

        payload["sessions_available"] = len(list_sessions(limit=100))
    except Exception:
        payload["sessions_available"] = 0

    if getattr(args, "json", False):
        print_json(payload)
        return 0

    print(f"CodingAgent {payload['version']}")
    print(f"  Runtime:     {payload['runtime']['python']} ({payload['runtime']['os']})")
    print(f"  Provider:    {payload['provider'] or '(none configured)'}")
    print(f"  Model:       {payload['model'] or '(default)'}")
    print(f"  WorkDir:     {payload['workdir']}")
    if payload["git_branch"]:
        print(f"  Git branch:  {payload['git_branch']}")
    print(f"  Sessions:    {payload['sessions_available']} saved")
    return 0