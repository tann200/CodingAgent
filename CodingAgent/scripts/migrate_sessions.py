#!/usr/bin/env python3
"""Migrate SQLite session history to JSONL format.

Usage:
    python scripts/migrate_sessions.py [--workdir PATH] [--dry-run]

For each session in the SQLite DB under .agent-context/session.db:
  1. Read all rows from messages, tool_calls, errors, plans tables.
  2. Sort by timestamp.
  3. Write to .agent-context/sessions/<session_id>.jsonl
     (one JSON event per line, same schema as JsonlSessionStore).

Safe to re-run: skips session_ids that already have a .jsonl file.
Does not delete the SQLite DB — run with --delete-sqlite to clean up.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def migrate(workdir: Path, dry_run: bool, delete_sqlite: bool) -> None:
    """Migrate SQLite sessions to JSONL format."""
    db_path = workdir / ".agent-context" / "session.db"
    out_dir = workdir / ".agent-context" / "sessions"

    if not db_path.exists():
        print(f"No SQLite DB found at {db_path}; nothing to migrate.")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)

    # Collect session IDs
    session_ids = [
        r[0]
        for r in conn.execute("SELECT DISTINCT session_id FROM messages").fetchall()
    ]

    if not session_ids:
        print("No sessions found in SQLite DB.")
        conn.close()
        return

    print(f"Found {len(session_ids)} session(s) to migrate.")

    for sid in session_ids:
        out_file = out_dir / f"{sid}.jsonl"
        if out_file.exists():
            print(f"  SKIP {sid} (already migrated)")
            continue

        events = []
        # Messages
        for role, content, ts in conn.execute(
            "SELECT role, content, timestamp FROM messages "
            "WHERE session_id=? ORDER BY timestamp",
            (sid,),
        ):
            events.append(
                {
                    "type": "message",
                    "ts": ts,
                    "role": role,
                    "content": content,
                }
            )

        # Tool calls
        try:
            for name, args, result, ts in conn.execute(
                "SELECT tool_name, args_json, result_json, timestamp FROM tool_calls "
                "WHERE session_id=? ORDER BY timestamp",
                (sid,),
            ):
                events.append(
                    {
                        "type": "tool_call",
                        "ts": ts,
                        "name": name,
                        "args": json.loads(args) if args else {},
                        "result": json.loads(result) if result else {},
                    }
                )
        except Exception:
            pass  # Table might not exist in some schemas

        # Errors
        try:
            for error_type, message, context, ts in conn.execute(
                "SELECT error_type, message, context_json, timestamp FROM errors "
                "WHERE session_id=? ORDER BY timestamp",
                (sid,),
            ):
                events.append(
                    {
                        "type": "error",
                        "ts": ts,
                        "error_type": error_type,
                        "message": message,
                        "context": json.loads(context) if context else {},
                    }
                )
        except Exception:
            pass  # Table might not exist

        # Plans
        try:
            for plan_json, status, ts in conn.execute(
                "SELECT plan_json, status, timestamp FROM plans "
                "WHERE session_id=? ORDER BY timestamp",
                (sid,),
            ):
                events.append(
                    {
                        "type": "plan",
                        "ts": ts,
                        "plan": json.loads(plan_json) if plan_json else [],
                        "status": status,
                    }
                )
        except Exception:
            pass  # Table might not exist

        # Sort all events by timestamp
        events.sort(key=lambda e: e.get("ts", 0))

        if dry_run:
            print(f"  DRY-RUN {sid}: {len(events)} events → {out_file}")
        else:
            with out_file.open("w", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            print(f"  MIGRATED {sid}: {len(events)} events → {out_file}")

    conn.close()
    if delete_sqlite and not dry_run:
        db_path.unlink()
        print(f"Deleted {db_path}")

    print("\nMigration complete.")
    print("To switch to JSONL backend, run:")
    print("  export CODING_AGENT_STORAGE_BACKEND=jsonl")
    print("or add to .agent-context/config.json:")
    print('  {"storage_backend": "jsonl"}')


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite sessions to JSONL")
    parser.add_argument(
        "--workdir",
        default=".",
        help="Project working directory (default: current directory)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without creating files",
    )
    parser.add_argument(
        "--delete-sqlite",
        action="store_true",
        help="Delete the SQLite DB after successful migration",
    )
    args = parser.parse_args()

    workdir = Path(args.workdir).resolve()
    if not workdir.is_dir():
        print(f"Error: {workdir} is not a directory")
        sys.exit(1)

    print(f"Migrating sessions from {workdir}")
    migrate(workdir, args.dry_run, args.delete_sqlite)


if __name__ == "__main__":
    main()
