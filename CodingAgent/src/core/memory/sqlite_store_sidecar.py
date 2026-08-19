from __future__ import annotations

import json
import os
import shutil
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def build_decision_records(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    return [
        {
            "session_id": row["session_id"],
            "decision": row["decision"],
            "rationale": row["rationale"],
            "ts": row["created_at"],
        }
        for row in rows
    ]


def resolve_agent_context_dir(
    *,
    workdir: Path,
    agent_context_dir: Optional[Path | str],
) -> Path:
    try:
        from src.tools.tools_config import agent_context_path

        return agent_context_path(workdir)
    except Exception:
        if agent_context_dir is not None:
            return Path(agent_context_dir)
        return workdir / ".codingAgent"


def write_json_sidecar_with_fallback(
    *,
    dest: Path,
    payload: Any,
    logger: Any,
    debug_prefix: str,
) -> bool:
    try:
        from src.core.io_utils import atomic_write_json

        ok = atomic_write_json(dest, payload, logger=logger)
        if ok:
            logger.debug("%s: atomic write succeeded: %s", debug_prefix, dest)
            return True
        logger.warning("%s: atomic_write_json returned False, falling back", debug_prefix)
    except Exception:
        logger.debug(
            "%s: atomic_write_json unavailable, using fallback\n%s",
            debug_prefix,
            traceback.format_exc(),
        )

    fd = None
    tmp = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, ensure_ascii=False)
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                pass
        try:
            os.replace(tmp, str(dest))
        except Exception:
            try:
                shutil.move(tmp, str(dest))
            except Exception:
                pass
        return True
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass


def build_write_failure_payload(
    *,
    db_path: Optional[Path],
    session_id: str,
    attempts: int,
    last_error: str,
    sql: str,
    params: Any,
    ts: int,
) -> Dict[str, Any]:
    return {
        "db_path": str(db_path) if db_path is not None else None,
        "session_id": session_id,
        "attempts": attempts,
        "last_error": last_error,
        "sql": sql,
        "params": params,
        "ts": ts,
    }


def read_decisions_sidecar(*, path: Path, max_entries: int) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return data[: max(0, int(max_entries))]
