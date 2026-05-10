import json

from src.core.memory.sqlite_store_sidecar import (
    build_decision_records,
    build_write_failure_payload,
    read_decisions_sidecar,
    resolve_agent_context_dir,
    write_json_sidecar_with_fallback,
)


class _Logger:
    def debug(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


def test_build_decision_records_maps_rows_to_serializable_dicts():
    rows = [
        {
            "session_id": "s1",
            "decision": "use read_file",
            "rationale": "needed",
            "created_at": "t1",
        }
    ]

    assert build_decision_records(rows) == [
        {
            "session_id": "s1",
            "decision": "use read_file",
            "rationale": "needed",
            "ts": "t1",
        }
    ]


def test_build_write_failure_payload_includes_expected_fields(tmp_path):
    payload = build_write_failure_payload(
        db_path=tmp_path / "session.db",
        session_id="s1",
        attempts=3,
        last_error="SQLITE_BUSY/LOCKED",
        sql="INSERT",
        params=(1,),
        ts=123,
    )

    assert payload["session_id"] == "s1"
    assert payload["attempts"] == 3
    assert payload["last_error"] == "SQLITE_BUSY/LOCKED"


def test_resolve_agent_context_dir_falls_back_to_dot_coding_agent(tmp_path):
    result = resolve_agent_context_dir(
        workdir=tmp_path,
        agent_context_dir=None,
    )

    assert result == tmp_path / ".codingAgent"


def test_write_json_sidecar_with_fallback_writes_file(tmp_path):
    dest = tmp_path / "decisions.json"
    ok = write_json_sidecar_with_fallback(
        dest=dest,
        payload=[{"decision": "x"}],
        logger=_Logger(),
        debug_prefix="test",
    )

    assert ok is True
    assert json.loads(dest.read_text(encoding="utf-8")) == [{"decision": "x"}]


def test_read_decisions_sidecar_returns_list_prefix(tmp_path):
    path = tmp_path / "decisions.json"
    path.write_text(
        json.dumps([
            {"decision": "a"},
            {"decision": "b"},
        ]),
        encoding="utf-8",
    )

    assert read_decisions_sidecar(path=path, max_entries=1) == [{"decision": "a"}]
