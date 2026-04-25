"""Small filesystem helpers used across core modules.

Provides an atomic JSON writer to ensure readers never observe partially
written JSON files. The implementation uses tempfile.mkstemp + os.replace and
falls back to a simple write_text path when necessary.
"""

from pathlib import Path
import tempfile as _tempfile
import os as _os
import json as _json
import logging as _logging
from typing import Any


def atomic_write_json(target: Path, obj: Any, logger=None) -> bool:
    """Atomically write ``obj`` as JSON to ``target``.

    Returns True on success, False on failure. Best-effort fsync is attempted.
    """
    if logger is None:
        logger = _logging.getLogger(__name__)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _fd, _tmp = _tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with _os.fdopen(_fd, "w", encoding="utf-8") as _f:
                _json.dump(obj, _f, ensure_ascii=False, indent=2, default=str)
                _f.flush()
                try:
                    _os.fsync(_f.fileno())
                except Exception:
                    # Non-fatal: best-effort fsync
                    pass
            _os.replace(_tmp, str(target))
        except Exception:
            try:
                _os.unlink(_tmp)
            except Exception:
                pass
            raise
        try:
            st = target.stat()
            logger.info("atomic_write_json: written %s (%d bytes)", target, st.st_size)
        except Exception:
            logger.info("atomic_write_json: written %s", target)
        return True
    except Exception as _aw:
        logger.warning("atomic_write_json: atomic write failed: %s", _aw)
        # Fallback: try the simple write_text path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(__import__("json").dumps(obj, indent=2), encoding="utf-8")
            try:
                st = target.stat()
                logger.info(
                    "atomic_write_json: written (fallback): %s (%d bytes)",
                    target,
                    st.st_size,
                )
            except Exception:
                logger.info("atomic_write_json: written (fallback): %s", target)
            return True
        except Exception as _fb:
            logger.error("atomic_write_json: write failed (fallback): %s", _fb)
            return False
