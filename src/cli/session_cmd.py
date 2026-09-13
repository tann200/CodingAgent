"""``codingagent session`` — list, show, and export persisted session snapshots.

Mirrors the TUI ``/sessions`` list, ``/timeline`` view, and ``/share`` export
as headless subcommands so sessions are browsable from scripts or CI.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from src.cli._helpers import print_json, render_content


def run_session(args: Any) -> int:
    action = getattr(args, "session_action", "list") or "list"
    if action == "list":
        return _list_sessions(args)
    if action == "show":
        return _show_session(args)
    if action == "export":
        return _export_session(args)
    print(f"Error: unknown session action '{action}'", file=__import__("sys").stderr)
    return 2


def _to_json(s: Any) -> dict:
    return {
        "session_id": s.session_id,
        "task_name": s.task_name,
        "working_dir": s.working_dir,
        "message_count": s.message_count,
        "turn_count": s.turn_count,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "created_at": s.created_at,
        "timestamp": getattr(s, "timestamp", None),
    }


def _list_sessions(args: Any) -> int:
    from src.core.orchestration.session_store import list_sessions

    sessions = list_sessions(limit=args.limit or 50)
    if getattr(args, "json", False):
        print_json({"count": len(sessions), "sessions": [_to_json(s) for s in sessions]})
        return 0
    if not sessions:
        print("No saved sessions found.")
        return 0
    print(f"{'SESSION ID':<38} {'MSGS':>5} {'TURNS':>5}  CREATED_AT                TASK")
    for s in sessions:
        created = s.created_at[:19] if s.created_at else ""
        task = (s.task_name or "")[:40]
        print(
            f"{s.session_id:<38} {s.message_count:>5} {s.turn_count:>5}  "
            f"{created:<26} {task}"
        )
    return 0


def _show_session(args: Any) -> int:
    from src.core.orchestration.session_store import load_session

    s = load_session(args.session_id)
    if s is None:
        print(f"Error: session '{args.session_id}' not found", file=__import__("sys").stderr)
        return 1
    if getattr(args, "json", False):
        print_json(_to_json(s))
        return 0
    print(f"# Session {s.session_id}")
    print(f"Task:    {s.task_name}")
    print(f"Created: {s.created_at}")
    print(f"Workdir: {s.working_dir}")
    print(f"Messages: {s.message_count} | Turns: {s.turn_count} | "
          f"Tokens: {s.input_tokens} in / {s.output_tokens} out")
    print("\nTimeline:")
    for msg in s.messages:
        role = str(msg.get("role", "unknown")) if isinstance(msg, dict) else "unknown"
        content = render_content(msg.get("content", "") if isinstance(msg, dict) else msg)
        print("\n" + "-" * 60)
        print(f"**{role.upper()}**  {content}")
    return 0


def _export_session(args: Any) -> int:
    from pathlib import Path

    from src.core.orchestration.session_store import load_session

    s = load_session(args.session_id)
    if s is None:
        print(f"Error: session '{args.session_id}' not found", file=__import__("sys").stderr)
        return 1

    lines = [
        "# Conversation Export",
        f"_Exported: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_",
        f"_Session: {s.session_id}_",
        "",
    ]
    for msg in s.messages:
        role = str(msg.get("role", "unknown")) if isinstance(msg, dict) else "unknown"
        content = render_content(msg.get("content", "") if isinstance(msg, dict) else msg)
        lines.append(f"**{role.upper()}**\n\n{content}\n\n---\n")

    md_text = "\n".join(lines) + "\n"

    out_path: Optional[Path] = None
    if getattr(args, "out", None):
        out_path = Path(args.out)
    else:
        from src.core.paths import get_data_dir

        out_path = get_data_dir() / f"export_{s.session_id[:8]}.md"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            import tempfile
            import os

            fd, tmp = tempfile.mkstemp(dir=str(out_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    os.fsync(f.fileno())
                    f.write(md_text)
                os.replace(tmp, str(out_path))
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except Exception:
            out_path.write_text(md_text, encoding="utf-8")
    except Exception as exc:
        print(f"Error: export failed: {exc}", file=__import__("sys").stderr)
        return 1

    if getattr(args, "json", False):
        print_json({"session_id": s.session_id, "exported_to": str(out_path)})
    else:
        print(f"Exported {s.message_count} messages -> {out_path}")
    return 0