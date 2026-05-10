from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


def fork_copy_specs() -> Sequence[Tuple[str, str, Tuple[str, ...]]]:
    return (
        ("messages", "session_id", ("role", "content", "created_at")),
        ("tool_calls", "session_id", ("tool_name", "args", "result", "success", "created_at")),
        ("errors", "session_id", ("error_type", "error_message", "context", "created_at")),
        ("plans", "session_id", ("plan", "status", "created_at")),
        ("decisions", "session_id", ("decision", "rationale", "created_at")),
        ("session_children", "parent_session_id", ("child_session_id", "role", "task", "created_at")),
    )


def copy_session_rows(*, conn: Any, source_session_id: str, dest_session_id: str) -> None:
    for table_name, source_column, columns in fork_copy_specs():
        select_columns = ", ".join(columns)
        rows = conn.execute(
            f"SELECT {select_columns} FROM {table_name} WHERE {source_column}=? ORDER BY created_at",
            (source_session_id,),
        ).fetchall()
        insert_columns = ", ".join((source_column, *columns))
        placeholders = ", ".join(["?" for _ in range(len(columns) + 1)])
        for row in rows:
            values = [dest_session_id, *[row[column] for column in columns]]
            conn.execute(
                f"INSERT INTO {table_name} ({insert_columns}) VALUES ({placeholders})",
                tuple(values),
            )


def copy_session_snapshots(*, conn: Any, source_session_id: str, dest_session_id: str) -> List[str]:
    rows = conn.execute(
        "SELECT snapshot_id, state_json, role, task, saved_at FROM session_snapshots WHERE session_id=? ORDER BY saved_at",
        (source_session_id,),
    ).fetchall()
    snapshot_ids: List[str] = []
    for row in rows:
        snapshot_ids.append(row["snapshot_id"])
        conn.execute(
            "INSERT INTO session_snapshots (session_id, snapshot_id, state_json, role, task, saved_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                dest_session_id,
                row["snapshot_id"],
                row["state_json"],
                row["role"],
                row["task"],
                row["saved_at"],
            ),
        )
    return snapshot_ids


def copy_missing_snapshot_rows(*, conn: Any, snapshot_ids: Iterable[str]) -> None:
    for snapshot_id in snapshot_ids:
        snapshot_rows = conn.execute(
            "SELECT table_name, rows_json, saved_at FROM session_snapshot_rows WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
        for snapshot_row in snapshot_rows:
            try:
                exists = conn.execute(
                    "SELECT 1 FROM session_snapshot_rows WHERE snapshot_id=? AND table_name=? AND rows_json=? LIMIT 1",
                    (snapshot_id, snapshot_row["table_name"], snapshot_row["rows_json"]),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO session_snapshot_rows (snapshot_id, table_name, rows_json, saved_at) VALUES (?, ?, ?, ?)",
                    (
                        snapshot_id,
                        snapshot_row["table_name"],
                        snapshot_row["rows_json"],
                        snapshot_row["saved_at"],
                    ),
                )
            except Exception:
                continue


def group_snapshot_rows(rows: Sequence[Any]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for row in rows:
        table_name = row[0]
        rows_json = row[1]
        grouped.setdefault(table_name, []).append(rows_json)
    return grouped


def restore_snapshot_rows(
    *,
    conn: Any,
    session_id: str,
    grouped_rows: Mapping[str, Sequence[str]],
    initial_deleted: Dict[str, int] | None = None,
) -> Dict[str, int]:
    deleted = dict(initial_deleted or {})
    for table_name, json_rows_list in grouped_rows.items():
        try:
            if table_name == "session_children":
                cur = conn.execute(
                    "DELETE FROM session_children WHERE parent_session_id=?",
                    (session_id,),
                )
            else:
                cur = conn.execute(
                    f"DELETE FROM {table_name} WHERE session_id=?",
                    (session_id,),
                )
            if cur is not None:
                try:
                    deleted[table_name] = cur.rowcount
                except Exception:
                    pass
        except Exception:
            pass

        for rows_json in json_rows_list:
            try:
                items = json.loads(rows_json)
                for item in items:
                    if table_name == "session_children":
                        item["parent_session_id"] = session_id
                    else:
                        item["session_id"] = session_id
                    columns = list(item.keys())
                    values = [item[column] for column in columns]
                    placeholders = ",".join(["?" for _ in columns])
                    conn.execute(
                        f"INSERT INTO {table_name} ({','.join(columns)}) VALUES ({placeholders})",
                        tuple(values),
                    )
            except Exception:
                pass
    return deleted


def delete_rows_after_snapshot(*, conn: Any, session_id: str, saved_at: str) -> Dict[str, int]:
    deleted = {
        "messages": 0,
        "tool_calls": 0,
        "errors": 0,
        "plans": 0,
        "decisions": 0,
    }
    for table_name in ("tool_calls", "errors", "plans", "decisions"):
        try:
            cur = conn.execute(
                f"DELETE FROM {table_name} WHERE session_id=? AND datetime(created_at) > datetime(?)",
                (session_id, saved_at),
            )
            deleted[table_name] = cur.rowcount if cur is not None and cur.rowcount is not None else 0
        except Exception:
            pass
    return deleted


def keep_messages_delete_specs(keep_messages: bool) -> Sequence[Tuple[str, bool]]:
    return (
        ("messages", not keep_messages),
        ("tool_calls", True),
        ("errors", True),
        ("plans", True),
        ("decisions", True),
    )
