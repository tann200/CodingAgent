"""Shared JSON sidecar write helpers for JSONL-backed stores."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json_with_fallback(path: Path, payload: Any, *, logger=None) -> bool:
    """Write JSON atomically, falling back to mkstemp+replace when needed."""
    try:
        from src.core.io_utils import atomic_write_json

        ok = atomic_write_json(path, payload, logger=logger)
    except Exception:
        ok = False

    if ok:
        return True

    tmp = None
    fd = None
    try:
        Path(path.parent).mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(payload, f, ensure_ascii=False)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        try:
            os.replace(tmp, str(path))
        except Exception:
            shutil.move(tmp, str(path))
        return True
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass
