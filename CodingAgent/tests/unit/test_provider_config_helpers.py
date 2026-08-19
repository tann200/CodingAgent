import json
from pathlib import Path

from src.core.inference.provider_config import (
    canonical_provider_name,
    get_active_provider_name,
    normalize_provider_models,
    resolve_providers_config_path,
    set_provider_active_flag,
)


class _Logger:
    def debug(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


def test_canonical_provider_name_normalizes_copilot_aliases():
    assert canonical_provider_name("copilot") == "github_copilot"
    assert canonical_provider_name("GitHub Copilot") == "github_copilot"
    assert canonical_provider_name("github-copilot") == "github_copilot"


def test_resolve_providers_config_path_uses_explicit_path(tmp_path):
    target = tmp_path / "providers.json"
    assert resolve_providers_config_path(str(target), __file__) == target


def test_normalize_provider_models_expands_lmstudio_models():
    models = normalize_provider_models(
        {"type": "lm_studio", "models": ["qwen3.5-9b"]},
        valid_str=lambda value: bool(value),
        canonical_provider=canonical_provider_name,
        lmstudio_full_id=lambda model: f"full/{model}",
    )

    assert models == ["full/qwen3.5-9b"]


def test_set_provider_active_flag_updates_matching_provider(tmp_path):
    cfg = tmp_path / "providers.json"
    cfg.write_text(
        json.dumps([
            {"type": "github_copilot", "active": False},
            {"type": "openai", "active": False},
        ]),
        encoding="utf-8",
    )

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    set_provider_active_flag(
        provider_type="copilot",
        active=True,
        resolve_config_path=lambda _path: cfg,
        canonical_provider=canonical_provider_name,
        lock=_Lock(),
        logger=_Logger(),
    )

    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data[0]["active"] is True
    assert data[1]["active"] is False


def test_get_active_provider_name_returns_first_active_provider(tmp_path):
    cfg = tmp_path / "providers.json"
    cfg.write_text(
        json.dumps([
            {"type": "openai", "active": False},
            {"name": "LM Studio", "active": True},
        ]),
        encoding="utf-8",
    )

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    assert (
        get_active_provider_name(
            providers_config_path=str(cfg),
            resolve_config_path=lambda path: Path(path) if path else cfg,
            canonical_provider=canonical_provider_name,
            lock=_Lock(),
        )
        == "lm_studio"
    )


def test_get_active_provider_name_returns_none_for_missing_or_inactive_config(tmp_path):
    missing = tmp_path / "missing.json"

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    assert (
        get_active_provider_name(
            providers_config_path=str(missing),
            resolve_config_path=lambda path: Path(path) if path else missing,
            canonical_provider=canonical_provider_name,
            lock=_Lock(),
        )
        is None
    )

    inactive = tmp_path / "inactive.json"
    inactive.write_text(json.dumps([{"type": "openai", "active": False}]), encoding="utf-8")

    assert (
        get_active_provider_name(
            providers_config_path=str(inactive),
            resolve_config_path=lambda path: Path(path) if path else inactive,
            canonical_provider=canonical_provider_name,
            lock=_Lock(),
        )
        is None
    )
