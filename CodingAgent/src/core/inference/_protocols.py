"""Structural Protocol definitions for the inference layer.

These protocols replace ``object`` type hints in provider_config.py,
provider_discovery.py, provider_loading.py, and provider_probe.py so that
mypy can verify structural compatibility without requiring concrete base classes
or circular imports.
"""

from __future__ import annotations

from typing import Any, List, Optional
from typing import Protocol, runtime_checkable


@runtime_checkable
class LockProtocol(Protocol):
    """Anything that can be used as a context-manager lock."""

    def __enter__(self) -> object:
        ...

    def __exit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> Optional[bool]:
        ...


@runtime_checkable
class LoggerProtocol(Protocol):
    """Structural equivalent of ``logging.Logger`` for type-checking only."""

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        ...

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        ...

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        ...

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        ...


@runtime_checkable
class ProviderManagerProtocol(Protocol):
    """Structural interface for ProviderManager as seen by the discovery helpers."""

    providers_config_path: Any  # Path | None; optional attribute

    def get_cached_models(self, provider_key: str) -> Optional[List[str]]:
        ...

    def get_provider(self, provider_key: str) -> Any:
        ...

    def get_active_provider_name(self) -> Optional[str]:
        ...
