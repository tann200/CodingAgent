from src.core.memory.sqlite_store_session_ops import (
    copy_missing_snapshot_rows,
    copy_session_rows,
    copy_session_snapshots,
    delete_rows_after_snapshot,
    group_snapshot_rows,
    keep_messages_delete_specs,
    restore_snapshot_rows,
)


class _Conn:
    def __init__(self, query_results=None):
        self.query_results = query_results or {}
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        result = self.query_results.get((sql, params), [])

        class _Cursor:
            def __init__(self, rows):
                self._rows = rows
                self.rowcount = len(rows) if isinstance(rows, list) else 1

            def fetchall(self):
                return self._rows if isinstance(self._rows, list) else []

            def fetchone(self):
                if isinstance(self._rows, list):
                    return self._rows[0] if self._rows else None
                return self._rows

        return _Cursor(result)


def test_group_snapshot_rows_groups_by_table_name():
    rows = [("messages", "[1]"), ("messages", "[2]"), ("plans", "[3]")]

    assert group_snapshot_rows(rows) == {
        "messages": ["[1]", "[2]"],
        "plans": ["[3]"],
    }


def test_keep_messages_delete_specs_toggles_messages_only():
    assert keep_messages_delete_specs(True)[0] == ("messages", False)
    assert keep_messages_delete_specs(False)[0] == ("messages", True)


def test_copy_session_rows_copies_across_known_tables():
    conn = _Conn(
        {
            ("SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY created_at", ("src",)): [
                {"role": "user", "content": "hello", "created_at": "t1"}
            ],
            ("SELECT tool_name, args, result, success, created_at FROM tool_calls WHERE session_id=? ORDER BY created_at", ("src",)): [],
            ("SELECT error_type, error_message, context, created_at FROM errors WHERE session_id=? ORDER BY created_at", ("src",)): [],
            ("SELECT plan, status, created_at FROM plans WHERE session_id=? ORDER BY created_at", ("src",)): [],
            ("SELECT decision, rationale, created_at FROM decisions WHERE session_id=? ORDER BY created_at", ("src",)): [],
            ("SELECT child_session_id, role, task, created_at FROM session_children WHERE parent_session_id=? ORDER BY created_at", ("src",)): [],
        }
    )

    copy_session_rows(conn=conn, source_session_id="src", dest_session_id="fork")

    assert any("INSERT INTO messages" in sql and params[0] == "fork" for sql, params in conn.calls)


def test_copy_session_snapshots_returns_snapshot_ids():
    conn = _Conn(
        {
            (
                "SELECT snapshot_id, state_json, role, task, saved_at FROM session_snapshots WHERE session_id=? ORDER BY saved_at",
                ("src",),
            ): [
                {"snapshot_id": "snap1", "state_json": "{}", "role": None, "task": None, "saved_at": "t1"}
            ]
        }
    )

    snapshot_ids = copy_session_snapshots(conn=conn, source_session_id="src", dest_session_id="fork")

    assert snapshot_ids == ["snap1"]
    assert any("INSERT INTO session_snapshots" in sql for sql, _params in conn.calls)


def test_copy_missing_snapshot_rows_inserts_only_when_missing():
    conn = _Conn(
        {
            ("SELECT table_name, rows_json, saved_at FROM session_snapshot_rows WHERE snapshot_id=?", ("snap1",)): [
                {"table_name": "messages", "rows_json": "[]", "saved_at": "t1"}
            ],
            (
                "SELECT 1 FROM session_snapshot_rows WHERE snapshot_id=? AND table_name=? AND rows_json=? LIMIT 1",
                ("snap1", "messages", "[]"),
            ): None,
        }
    )

    copy_missing_snapshot_rows(conn=conn, snapshot_ids=["snap1"])

    assert any("INSERT INTO session_snapshot_rows" in sql for sql, _params in conn.calls)


def test_restore_snapshot_rows_deletes_and_reinserts_session_rows():
    conn = _Conn()
    deleted = restore_snapshot_rows(
        conn=conn,
        session_id="s1",
        grouped_rows={"messages": ['[{"role": "user", "content": "hello"}]']},
        initial_deleted={"messages": 0},
    )

    assert "messages" in deleted
    assert any("DELETE FROM messages" in sql for sql, _params in conn.calls)
    assert any("INSERT INTO messages" in sql for sql, _params in conn.calls)


def test_delete_rows_after_snapshot_targets_non_message_tables_only():
    conn = _Conn()

    deleted = delete_rows_after_snapshot(conn=conn, session_id="s1", saved_at="t1")

    assert set(deleted.keys()) == {"messages", "tool_calls", "errors", "plans", "decisions"}
    assert deleted["messages"] == 0
    assert any("DELETE FROM tool_calls" in sql for sql, _params in conn.calls)
