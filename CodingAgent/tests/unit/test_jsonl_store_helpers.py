import json
from pathlib import Path

from src.core.memory.jsonl_store_helpers import (
    build_fork_destination_path,
    build_snapshot_result_payload,
    build_snapshot_sidecar_payload,
    decode_snapshot_reference,
    parse_session_id_from_jsonl_filename,
)


def test_build_fork_destination_path_for_active_file(tmp_path: Path):
    dest = build_fork_destination_path(
        source_session_id="source",
        new_session_id="forked",
        source_name="source.jsonl",
        sessions_dir=tmp_path,
    )
    assert dest == tmp_path / "forked.jsonl"


def test_build_fork_destination_path_for_rotated_file(tmp_path: Path):
    dest = build_fork_destination_path(
        source_session_id="source",
        new_session_id="forked",
        source_name="source.2.jsonl",
        sessions_dir=tmp_path,
    )
    assert dest == tmp_path / "forked.2.jsonl"


def test_build_and_decode_snapshot_payload_round_trip(tmp_path: Path):
    payload = build_snapshot_sidecar_payload(
        snapshot_id="snap1",
        session_id="sess1",
        state_json='{"ok": true}',
        active_file=tmp_path / "sess1.jsonl",
        offset=42,
        timestamp="2026-05-04T00:00:00+00:00",
    )

    result_payload = build_snapshot_result_payload(payload)
    target_file, target_offset = decode_snapshot_reference(result_payload)

    assert payload["snapshot_id"] == "snap1"
    assert payload["session_id"] == "sess1"
    assert json.loads(result_payload)["state_json"] == '{"ok": true}'
    assert target_file == tmp_path / "sess1.jsonl"
    assert target_offset == 42


def test_parse_session_id_from_jsonl_filename_handles_active_and_rotated_names():
    assert parse_session_id_from_jsonl_filename("abc123.jsonl") == "abc123"
    assert parse_session_id_from_jsonl_filename("abc123.0.jsonl") == "abc123"
