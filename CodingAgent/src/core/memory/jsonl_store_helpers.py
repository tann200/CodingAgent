"""Pure helper functions for JSONL session-store path and payload handling."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple


def build_fork_destination_path(
    *,
    source_session_id: str,
    new_session_id: str,
    source_name: str,
    sessions_dir: Path,
) -> Path:
    """Return the destination path for a copied session JSONL file."""
    if source_name == f"{source_session_id}.jsonl":
        return sessions_dir / f"{new_session_id}.jsonl"

    suffix = source_name[len(source_session_id) :]
    return sessions_dir / f"{new_session_id}{suffix}"


def build_snapshot_sidecar_payload(
    *,
    snapshot_id: str,
    session_id: str,
    state_json: str,
    active_file: Path,
    offset: int,
    timestamp: str,
) -> Dict[str, Any]:
    """Build the persisted snapshot sidecar payload."""
    return {
        "snapshot_id": snapshot_id,
        "session_id": session_id,
        "state_json": state_json,
        "_file": str(active_file),
        "_offset": offset,
        "ts": timestamp,
    }


def decode_snapshot_reference(snapshot_text: str) -> Tuple[Path, int]:
    """Decode snapshot metadata into a target file path and byte offset."""
    meta = json.loads(snapshot_text)
    return Path(meta["_file"]), int(meta["_offset"])


def build_snapshot_result_payload(data: Dict[str, Any]) -> str:
    """Build the stable JSON string returned by ``get_snapshot``."""
    return json.dumps(
        {
            "state_json": data.get("state_json", ""),
            "_file": data.get("_file", ""),
            "_offset": data.get("_offset", 0),
        }
    )


def parse_session_id_from_jsonl_filename(filename: str) -> str:
    """Extract the logical session ID from active or rotated JSONL filenames."""
    name = Path(filename).stem
    if "." not in name:
        return name
    return name.rsplit(".", 1)[0]
