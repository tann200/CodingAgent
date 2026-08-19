from src.core.memory.sqlite_store_schema import (
    fts_creation_script,
    fts_trigger_statements,
    schema_creation_script,
    schema_version_from_row,
    serialise_snapshot_rows,
)


def test_schema_creation_script_contains_core_tables():
    script = schema_creation_script()

    assert "CREATE TABLE IF NOT EXISTS messages" in script
    assert "CREATE TABLE IF NOT EXISTS tool_calls" in script
    assert "CREATE TABLE IF NOT EXISTS session_snapshots" in script


def test_fts_helpers_expose_expected_tables_and_triggers():
    script = fts_creation_script()
    triggers = fts_trigger_statements()

    assert "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts" in script
    assert "CREATE VIRTUAL TABLE IF NOT EXISTS mistakes_fts" in script
    assert len(triggers) == 3
    assert any("messages_ai" in statement for statement in triggers)
    assert any("mistakes_ai" in statement for statement in triggers)


def test_schema_version_from_row_handles_missing_and_present_rows():
    assert schema_version_from_row({"value": "3"}) == 3
    assert schema_version_from_row(None) == 1


def test_serialise_snapshot_rows_drops_id_and_session_id():
    rows = [
        {"id": 1, "session_id": "s1", "role": "user", "content": "hello"},
        {"id": 2, "session_id": "s1", "tool_name": "read_file", "args": "{}"},
    ]

    result = serialise_snapshot_rows(rows)

    assert result == [
        {"role": "user", "content": "hello"},
        {"tool_name": "read_file", "args": "{}"},
    ]
