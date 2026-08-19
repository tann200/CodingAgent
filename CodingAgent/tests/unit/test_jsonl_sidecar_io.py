import json

from src.core.memory.jsonl_sidecar_io import atomic_write_json_with_fallback


def test_atomic_write_json_with_fallback_writes_when_primary_writer_unavailable(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("no shared writer")

    monkeypatch.setattr("src.core.io_utils.atomic_write_json", _boom)

    path = tmp_path / "sidecars" / "state.json"
    payload = {"ok": True, "n": 1}

    assert atomic_write_json_with_fallback(path, payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload
