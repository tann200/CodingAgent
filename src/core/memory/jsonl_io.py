"""jsonl_io.py — Lightweight JSONL import/export utility for portable session sharing.

TASK-24: Replaces the 1,304-LOC dual JSONL storage backend with a small,
single-responsibility JSONL import/export layer over the canonical SQLite
``SessionStore``.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Optional

from src.core.security.redaction import redact_data, redact_secrets


def export_session_jsonl(
    store: Any,
    session_id: str,
    dest: Path,
) -> int:
    """Export all messages, tool calls, decisions, and errors for *session_id* to JSONL.

    All exported content is secret-redacted before writing.  Returns the number
    of JSONL records written.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    for msg in store.get_messages(session_id) or []:
        records.append(
            {
                "type": "message",
                "session_id": session_id,
                "role": msg.get("role", "user"),
                "content": redact_secrets(str(msg.get("content", ""))),
                "timestamp": msg.get("timestamp"),
            }
        )

    for tc in store.get_tool_calls(session_id) or []:
        records.append(
            {
                "type": "tool_call",
                "session_id": session_id,
                "tool_name": tc.get("tool_name", ""),
                "args": redact_data(tc.get("args") or {}),
                "result": redact_data(tc.get("result") or {}),
                "success": bool(tc.get("success", True)),
                "timestamp": tc.get("timestamp"),
            }
        )

    for dec in store.get_decisions(session_id) or []:
        records.append(
            {
                "type": "decision",
                "session_id": session_id,
                "decision": redact_secrets(str(dec.get("decision", ""))),
                "rationale": redact_secrets(str(dec.get("rationale", ""))),
            }
        )

    for err in store.get_errors(session_id) or []:
        records.append(
            {
                "type": "error",
                "session_id": session_id,
                "error_type": err.get("error_type", "Error"),
                "error_message": redact_secrets(str(err.get("error_message", ""))),
                "context": redact_secrets(str(err.get("context", ""))),
            }
        )

    lines = [json.dumps(rec, ensure_ascii=False) for rec in records]
    dest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(records)


def import_session_jsonl(
    store: Any,
    src: Path,
    *,
    session_id: Optional[str] = None,
) -> str:
    """Import records from a JSONL file into *store* under *session_id* (or a fresh UUID).

    Returns the target ``session_id`` into which records were imported.
    """
    src = Path(src)
    target_sid = session_id or f"import-{uuid.uuid4().hex[:12]}"
    for raw_line in src.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        rec_type = rec.get("type")
        if rec_type == "message":
            store.add_message(
                target_sid,
                str(rec.get("role") or "user"),
                str(rec.get("content") or ""),
            )
        elif rec_type == "tool_call":
            store.add_tool_call(
                target_sid,
                str(rec.get("tool_name") or ""),
                rec.get("args") if isinstance(rec.get("args"), dict) else {},
                rec.get("result") if isinstance(rec.get("result"), dict) else {},
                bool(rec.get("success", True)),
            )
        elif rec_type == "decision":
            store.add_decision(
                target_sid,
                str(rec.get("decision") or ""),
                str(rec.get("rationale") or ""),
            )
        elif rec_type == "error":
            store.add_error(
                target_sid,
                str(rec.get("error_type") or "Error"),
                str(rec.get("error_message") or ""),
                str(rec.get("context") or ""),
            )
    return target_sid
