from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def test_tokenizer_local_only_no_trust_remote_code(monkeypatch):
    from src.core.inference import tokenizer as tok_mod

    tok_mod._HF_TOKENIZERS.clear()
    calls: list[tuple[str, dict]] = []

    class FakeAutoTokenizer:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls.append((model_id, kwargs))
            return object()

    fake_transformers = types.SimpleNamespace(AutoTokenizer=FakeAutoTokenizer)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    res = tok_mod._get_hf_tokenizer("qwen2.5-coder")
    assert res is not None
    assert len(calls) == 1
    assert calls[0][1].get("local_files_only") is True
    assert "trust_remote_code" not in calls[0][1]

    # When offline local load raises, it should NOT retry with local_files_only=False
    tok_mod._HF_TOKENIZERS.clear()
    calls.clear()

    class FailingAutoTokenizer:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls.append((model_id, kwargs))
            raise OSError("missing offline")

    monkeypatch.setitem(
        sys.modules, "transformers", types.SimpleNamespace(AutoTokenizer=FailingAutoTokenizer)
    )
    assert tok_mod._get_hf_tokenizer("qwen2.5-missing") is None
    assert len(calls) == 1
    assert calls[0][1].get("local_files_only") is True


def test_decode_sse_line_split_utf8():
    from src.core.inference.streaming import decode_sse_line

    raw = "data: café 🚀".encode("utf-8")
    carry = bytearray()
    # Split right inside multi-byte rocket emoji (4-byte UTF-8)
    part1 = raw[:-2]
    part2 = raw[-2:]
    assert decode_sse_line(part1, carry=carry) is None
    assert len(carry) > 0
    decoded = decode_sse_line(part2, carry=carry)
    assert decoded == "café 🚀"
    assert len(carry) == 0


def test_split_thinking_content_across_chunks():
    from src.core.inference.streaming import split_thinking_content

    published: list[tuple[str, bool]] = []

    def _pub(txt: str, is_r: bool) -> None:
        published.append((txt, is_r))

    inside = False
    tag_buf: list[str] = []
    out_content: list[str] = []
    out_reason: list[str] = []

    chunks = ["Hi <th", "ink>secret ", "thought</thi", "nk> done"]
    for ch in chunks:
        r, c, inside, text_parts, _ = split_thinking_content(
            content_delta=ch,
            inside_think=inside,
            tag_split_enabled=True,
            publish_chunk=_pub,
            tag_buffer=tag_buf,
        )
        if c:
            out_content.append(c)
        if r:
            out_reason.append(r)

    assert inside is False
    assert tag_buf == []
    published_reason = "".join(t for t, is_r in published if is_r) + "".join(out_reason)
    published_content = "".join(t for t, is_r in published if not is_r) + "".join(out_content)
    assert published_reason == "secret thought"
    assert published_content == "Hi  done"


def test_consume_sse_stream_flushes_trailing_tag_buffer():
    from src.core.inference.llm_manager import _consume_sse_stream

    chunks = [
        b'data: {"choices": [{"delta": {"content": "hello <thi"}}]}\n',
        b"data: [DONE]\n",
    ]
    raw_resp = MagicMock()
    raw_resp.iter_lines.return_value = iter(chunks)
    result = _consume_sse_stream(raw_resp, model="qwen")
    assert str(result) == "hello <thi"


def test_consume_generator_stream_and_ollama_error(monkeypatch):
    from src.core.inference.streaming import consume_generator_stream
    from src.core.inference.adapters.ollama_adapter import OllamaAdapter

    def _gen():
        yield {"message": {"thinking": "plan ", "content": ""}}
        yield {"message": {"content": "answer"}}
        yield {"error": "model overloaded"}

    res = consume_generator_stream(_gen(), model="qwen3", provider="ollama")
    assert res["provider"] == "ollama"
    assert res["ok"] is False
    assert "model overloaded" in res.get("error", "")

    def _gen_ok():
        yield {"message": {"thinking": "step1 ", "content": "ok"}, "done": True}

    res_ok = consume_generator_stream(_gen_ok(), model="qwen3", provider="lm_studio")
    assert res_ok["ok"] is True
    assert res_ok["provider"] == "lm_studio"
    assert res_ok["choices"][0]["message"]["reasoning_content"] == "step1 "
    assert res_ok["choices"][0]["message"]["content"] == "ok"

    adapter = OllamaAdapter(models=["qwen3"])
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"error": "oom"}
    fake_resp.text = '{"error": "oom"}'
    monkeypatch.setattr(
        adapter,
        "_post_generation_with_retry",
        lambda *a, **kw: fake_resp,
    )
    out = adapter.generate([{"role": "user", "content": "hi"}], stream=False)
    assert out["ok"] is False
    assert "oom" in out.get("error", "")


def test_anthropic_adapter_custom_base_url_and_tool_id():
    from src.core.inference.adapters.anthropic_adapter import AnthropicAdapter

    adapter = AnthropicAdapter(
        api_key="test-key",
        base_url="https://custom.anthropic.local/v1/",
        models=["claude-sonnet-4-5"],
    )
    _payload, endpoint = adapter._build_payload(
        [{"role": "user", "content": "hi"}], "claude-sonnet-4-5", {}
    )
    assert endpoint == "https://custom.anthropic.local/v1/messages"

    norm = AnthropicAdapter._anthropic_to_openai_response(
        {
            "id": "msg_1",
            "model": "claude-sonnet-4-5",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "name": "read_file", "input": {"path": "a.py"}}],
        },
        "claude-sonnet-4-5",
    )
    tool_calls = norm["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_0"


def test_sqlite_session_store_snapshot_revert_redaction_and_tiebreak(tmp_path: Path):
    from src.core.memory.sqlite_session_store import SqliteSessionStore

    store = SqliteSessionStore(workdir=tmp_path)
    try:
        sid = "sess-1"
        store.add_message(sid, "user", "m1")
        snap1 = store.save_snapshot(sid, json.dumps({"step": 1}))
        store.add_message(sid, "assistant", "m2")
        snap2 = store.save_snapshot(sid, json.dumps({"step": 2}))
        assert snap1 and snap2

        rev = store.revert_session(sid, snap1)
        assert rev.get("ok") is True
        msgs = store.get_messages(sid)
        assert [m["content"] for m in msgs] == ["m1"]
        snap2_after = store.get_snapshot(sid, snap2)
        assert snap2_after is None

        secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEF"
        store.add_plan(sid, f"plan token {secret}")
        store.add_error(sid, "runtime_error", f"err token {secret}")
        store.save_plan(sid, [f"saved plan {secret}"], f"task {secret}", 1)

        plans = store.get_plans(sid)
        errs = store.get_errors(sid)
        assert all(secret not in json.dumps(p) for p in plans)
        assert all(secret not in json.dumps(e) for e in errs)
        loaded_plan = store.load_plan(sid)
        assert loaded_plan is not None and secret not in json.dumps(loaded_plan)
    finally:
        store.close()


def test_jsonl_session_store_security_revert_and_parity(tmp_path: Path):
    from src.core.memory.jsonl_session_store import JsonlSessionStore

    store = JsonlSessionStore(workdir=tmp_path)
    with pytest.raises(ValueError):
        store._session_files("../escape")
    with pytest.raises(ValueError):
        store.add_message("bad/id", "user", "hi")

    sid = "sess_ok.1"
    store.add_message(sid, "user", "first")
    snap = store.save_snapshot(sid, json.dumps({"step": 1}))
    assert snap is not None
    store.add_message(sid, "assistant", "second")

    # Simulate rotation where snap lived in rotated file 0, rotated file 1 is newer, and active has newer messages
    sdir = store._get_sessions_dir()
    active = sdir / f"{sid}.jsonl"
    rot0 = sdir / f"{sid}.0.jsonl"
    rot1 = sdir / f"{sid}.1.jsonl"
    active.rename(rot0)
    # Rewrite snapshot pointer in rot0? Or point snap to rot0: wait—update snapshot index/metadata
    snap_meta = store._snapshot_path(sid, snap)
    meta = json.loads(snap_meta.read_text(encoding="utf-8"))
    meta["_file"] = str(rot0)
    snap_meta.write_text(json.dumps(meta), encoding="utf-8")
    rot1.write_text(json.dumps({"type": "message", "role": "user", "content": "future1"}) + "\n", encoding="utf-8")
    active.write_text(json.dumps({"type": "message", "role": "user", "content": "future2"}) + "\n", encoding="utf-8")

    store.revert_session(sid, snap)
    assert not rot1.exists()
    assert [m["content"] for m in store.get_messages(sid)] == ["first"]

    secret = "ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD"
    store.add_plan(sid, f"step {secret}")
    store.add_error(sid, "runtime_error", f"boom {secret}")
    store.save_plan(sid, [f"active plan {secret}"], f"task {secret}", 1)
    loaded = store.load_plan(sid)
    assert loaded is not None and secret not in json.dumps(loaded)
    recent = store.get_recent_sessions(limit=5)
    assert recent and set(recent[0]) == {"session_id", "summary"}


def test_memory_search_redacts_secrets(tmp_path: Path):
    from src.core.memory.memory_tools import memory_search

    secret = "sk-proj-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJ"
    mem_dir = tmp_path / ".agent-context"
    mem_dir.mkdir(parents=True)
    (mem_dir / "notes.md").write_text(f"token: {secret}\nhello world", encoding="utf-8")
    res = memory_search("token", workdir=str(tmp_path))
    assert res["status"] == "ok"
    assert secret not in json.dumps(res)


def test_gui_logger_logs_to_stderr(capsys):
    from src.core.logger import GUILogger

    logger = GUILogger()
    logger.info("hello stderr test")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hello stderr test" in captured.err
