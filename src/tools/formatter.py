"""formatter.py — Auto-formatter hook for file writes (S2-C).

Loaded by ``write_file`` and ``edit_file_atomic`` after a successful write.
Formatter availability is checked once per extension; missing formatters are
silently skipped (never block the write).

Usage::

    from src.tools.formatter import run_formatter
    run_formatter("/path/to/file.py")  # runs black if available
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parents[1] / "config" / "formatters.yaml"

# Cache: extension → command list (None = unavailable)
_CMD_CACHE: Dict[str, Optional[List[str]]] = {}


def _load_config() -> Dict:
    """Load formatters.yaml; return empty dict on error."""
    try:
        import yaml  # type: ignore[import]

        if _CONFIG_PATH.exists():
            return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except ImportError:
        pass
    except Exception:
        pass
    # Built-in minimal fallback (no yaml dependency required)
    return {
        ".py": {"cmd": ["black", "--quiet", "{file}"]},
        ".go": {"cmd": ["gofmt", "-w", "{file}"]},
        ".rs": {"cmd": ["rustfmt", "{file}"]},
    }


_CONFIG: Optional[Dict] = None


def _get_config() -> Dict:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = _load_config()
    return _CONFIG


def _get_cmd(ext: str, file_path: str) -> Optional[List[str]]:
    """Return the formatter command for *ext*, or None if unavailable."""
    if ext in _CMD_CACHE:
        cached: Optional[List[str]] = _CMD_CACHE[ext]
        if cached is None:
            return None
        return [part.replace("{file}", file_path) for part in cached]

    cfg = _get_config()
    entry = cfg.get(ext)
    if not entry:
        _CMD_CACHE[ext] = None
        return None

    template: List[str] = entry.get("cmd", [])
    if not template:
        _CMD_CACHE[ext] = None
        return None

    binary = template[0]
    if not shutil.which(binary):
        logger.debug("formatter: %s not found on PATH — skipping for %s", binary, ext)
        _CMD_CACHE[ext] = None
        return None

    _CMD_CACHE[ext] = template
    return [part.replace("{file}", file_path) for part in template]


def run_formatter(file_path: str, timeout: float = 15.0) -> bool:
    """Run the configured formatter for *file_path*.

    Returns ``True`` if the formatter ran successfully, ``False`` otherwise.
    Never raises — formatter failure is always treated as a warning.

    Parameters
    ----------
    file_path:
        Absolute or relative path to the file that was just written.
    timeout:
        Maximum time in seconds to wait for the formatter subprocess.
    """
    ext = Path(file_path).suffix.lower()
    cmd = _get_cmd(ext, str(Path(file_path).resolve()))
    if cmd is None:
        return False

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            logger.debug(
                "formatter: %s exited %d for %s: %s",
                cmd[0],
                result.returncode,
                file_path,
                result.stderr.strip()[:200],
            )
            return False
        logger.debug("formatter: %s ran successfully on %s", cmd[0], file_path)
        return True
    except subprocess.TimeoutExpired:
        logger.debug("formatter: %s timed out for %s", cmd[0], file_path)
        return False
    except Exception as exc:
        logger.debug("formatter: unexpected error running %s: %s", cmd[0], exc)
        return False
