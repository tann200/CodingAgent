from src.core.memory.sqlite_store_queries import (
    base_summary,
    build_fts_mistake_query,
    build_like_search_params,
    build_like_search_sql,
    build_mistake_like_search_params,
    build_mistake_like_search_sql,
    build_mistake_search_params,
    build_mistake_search_sql,
    build_message_search_params,
    build_message_search_sql,
    build_recent_decisions_params,
    build_recent_decisions_sql,
    count_summary_fields,
    decode_json_field,
    extract_session_ids,
    map_child_session_rows,
    map_error_rows,
    map_like_search_rows,
    map_mistake_rows,
    map_search_rows,
    map_tool_call_rows,
)


def test_decode_json_field_returns_parsed_or_original_value():
    assert decode_json_field('{"a": 1}') == {"a": 1}
    assert decode_json_field("not-json") == "not-json"
    assert decode_json_field(None) is None


def test_map_tool_call_rows_and_error_rows_decode_json_fields():
    tool_rows = [
        {"tool_name": "read_file", "args": '{"path": "a.py"}', "result": '{"ok": true}', "success": 1}
    ]
    error_rows = [
        {"error_type": "ValueError", "error_message": "oops", "context": '{"x": 1}'}
    ]

    assert map_tool_call_rows(tool_rows) == [
        {"tool_name": "read_file", "args": {"path": "a.py"}, "result": {"ok": True}, "success": True}
    ]
    assert map_error_rows(error_rows) == [
        {"error_type": "ValueError", "error_message": "oops", "context": {"x": 1}}
    ]


def test_map_search_related_rows_and_child_rows():
    search_rows = [{"session_id": "s1", "role": "user", "content": "hello", "rank": -1.0}]
    like_rows = [{"session_id": "s2", "role": "assistant", "content": "world"}]
    child_rows = [{"parent_session_id": "p1", "child_session_id": "c1", "role": "analyst", "task": "t"}]
    mistake_rows = [{"summary": "bad", "context": "ctx", "tool": "bash", "created_at": "t1"}]

    assert map_search_rows(search_rows)[0]["rank"] == -1.0
    assert map_like_search_rows(like_rows) == [{"session_id": "s2", "role": "assistant", "content": "world"}]
    assert map_child_session_rows(child_rows) == [{"parent_session_id": "p1", "child_session_id": "c1", "role": "analyst", "task": "t"}]
    assert map_mistake_rows(mistake_rows) == [{"summary": "bad", "context": "ctx", "tool": "bash", "ts": "t1"}]


def test_build_fts_mistake_query_extracts_tokens_and_limits_size():
    query = build_fts_mistake_query("boom, parser failed badly!!!")
    assert query == "boom OR parser OR failed OR badly"
    assert build_fts_mistake_query("!!!") == ""


def test_build_mistake_search_sql_and_params():
    assert "FROM mistakes_fts" in build_mistake_search_sql()
    assert build_mistake_search_params(fts_query="boom OR parser", limit=3) == (
        "boom OR parser",
        3,
    )
    assert "WHERE summary LIKE ?" in build_mistake_like_search_sql()
    assert build_mistake_like_search_params(query="boom parser", limit=3) == (
        "%boom parser%",
        3,
    )


def test_build_recent_decisions_sql_and_params():
    assert "FROM decisions" in build_recent_decisions_sql()
    assert "ORDER BY created_at DESC" in build_recent_decisions_sql()
    assert build_recent_decisions_params(7) == (7,)


def test_build_message_and_like_search_sql_and_params():
    sql_with_session, _ = build_message_search_sql(session_id="s1")
    sql_without_session, _ = build_message_search_sql(session_id=None)
    assert "m.session_id = ?" in sql_with_session
    assert "m.session_id = ?" not in sql_without_session
    assert build_message_search_params(query="needle", limit=5, session_id="s1") == (
        "needle",
        "s1",
        5,
    )
    assert build_message_search_params(query="needle", limit=5, session_id=None) == (
        "needle",
        5,
    )

    like_with_session = build_like_search_sql(session_id="s1")
    like_without_session = build_like_search_sql(session_id=None)
    assert "session_id = ? AND content LIKE ?" in like_with_session
    assert "WHERE content LIKE ?" in like_without_session
    assert build_like_search_params(query="needle", limit=5, session_id="s1") == (
        "s1",
        "%needle%",
        5,
    )
    assert build_like_search_params(query="needle", limit=5, session_id=None) == (
        "%needle%",
        5,
    )


def test_extract_session_ids_and_summary_templates():
    rows = [("b",), (None,), ("a",), ("b",)]

    assert extract_session_ids(rows) == ["a", "b"]
    assert base_summary("s1")["session_id"] == "s1"
    assert ("messages", "messages", "message_count") in count_summary_fields()
