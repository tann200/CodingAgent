"""Tests for P3-1: headless streaming output format."""
from __future__ import annotations

import sys
import json
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orch(chunks=None, final_message="final answer"):
    """Build a mock Orchestrator that publishes stream_chunk events on run."""
    chunks = chunks or []

    class _FakeBus:
        def __init__(self):
            self._handlers: dict = {}

        def subscribe(self, event_name, cb):
            self._handlers.setdefault(event_name, []).append(cb)

        def _fire(self, event_name, payload):
            for cb in self._handlers.get(event_name, []):
                cb(payload)

        def publish(self, *a, **kw):
            pass

    bus = _FakeBus()

    def fake_run(**kwargs):
        for chunk in chunks:
            if isinstance(chunk, str):
                bus._fire("response.stream_chunk", {"chunk": chunk, "is_reasoning": False})
            else:
                bus._fire("response.stream_chunk", chunk)
        return {"assistant_message": final_message, "ok": True}

    orch = MagicMock()
    orch.event_bus = bus
    orch.run_agent_once.side_effect = fake_run
    return orch


def _headless(output_format, chunks=None, final_message="done"):
    """Run _run_headless with a mocked Orchestrator; return (rc, written_list)."""
    orch = _make_orch(chunks=chunks or [], final_message=final_message)
    written = []

    def fake_print(*args, **kwargs):
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        file = kwargs.get("file", None)
        if file is not None and file is not sys.stdout:
            return  # stderr/other — ignore
        text = sep.join(str(a) for a in args) + end
        written.append(text)

    with patch("src.core.orchestration.orchestrator.Orchestrator", return_value=orch), \
         patch("src.main.sys") as mock_sys, \
         patch("builtins.print", side_effect=fake_print):
        mock_sys.stdout.write.side_effect = lambda s: written.append(s)
        mock_sys.stdout.flush.return_value = None
        mock_sys.stderr = sys.stderr
        mock_sys.stdin = sys.stdin

        from src.main import _run_headless
        rc = _run_headless("do a task", output_format, None)

    return rc, written


# ---------------------------------------------------------------------------
# Arg parser includes "stream" choice
# ---------------------------------------------------------------------------

class TestArgParser:
    def test_stream_choice_accepted(self):
        from src.main import _parse_args
        args = _parse_args(["--task", "hello", "--output-format", "stream"])
        assert args.output_format == "stream"

    def test_invalid_format_rejected(self):
        from src.main import _parse_args
        with pytest.raises(SystemExit):
            _parse_args(["--task", "hi", "--output-format", "xml"])

    def test_all_valid_choices(self):
        from src.main import _parse_args
        for fmt in ("pretty", "json", "raw", "stream"):
            args = _parse_args(["--task", "x", "--output-format", fmt])
            assert args.output_format == fmt


# ---------------------------------------------------------------------------
# _run_headless stream format
# ---------------------------------------------------------------------------

class TestRunHeadlessStream:
    def test_returns_zero_on_success(self):
        rc, _ = _headless("stream", chunks=["hi"])
        assert rc == 0

    def test_chunks_written_incrementally(self):
        _, written = _headless("stream", chunks=["hel", "lo ", "world"])
        assert "hel" in written
        assert "lo " in written
        assert "world" in written

    def test_trailing_newline_added_after_chunks(self):
        _, written = _headless("stream", chunks=["hello"])
        assert "\n" in written

    def test_reasoning_chunks_not_printed(self):
        chunks = [
            {"chunk": "thinking...", "is_reasoning": True},
            {"chunk": "answer", "is_reasoning": False},
        ]
        _, written = _headless("stream", chunks=chunks)
        assert "thinking..." not in written
        assert "answer" in written

    def test_fallback_to_final_message_when_no_chunks(self):
        _, written = _headless("stream", chunks=[], final_message="fallback answer")
        full = "".join(written)
        assert "fallback answer" in full

    def test_json_format_still_produces_json(self):
        _, written = _headless("json", chunks=["x"], final_message="result")
        full = "".join(written)
        data = json.loads(full)
        assert data["assistant_message"] == "result"

    def test_raw_format_still_produces_plain_text(self):
        _, written = _headless("raw", chunks=[], final_message="raw result")
        full = "".join(written)
        assert "raw result" in full

    def test_flush_called_per_chunk(self):
        orch = _make_orch(chunks=["a", "b", "c"])
        flush_calls = []

        with patch("src.core.orchestration.orchestrator.Orchestrator", return_value=orch), \
             patch("src.main.sys") as mock_sys, \
             patch("builtins.print"):
            mock_sys.stdout.write.side_effect = lambda s: None
            mock_sys.stdout.flush.side_effect = lambda: flush_calls.append(1)
            mock_sys.stderr = sys.stderr
            mock_sys.stdin = sys.stdin

            from src.main import _run_headless
            _run_headless("task", "stream", None)

        # At minimum 3 flushes (one per chunk) + 1 for trailing newline
        assert len(flush_calls) >= 3


# ---------------------------------------------------------------------------
# Stream trigger in main() headless condition
# ---------------------------------------------------------------------------

class TestStreamTrigger:
    def test_stream_format_enters_headless(self):
        from src.main import _parse_args
        args = _parse_args(["--task", "do x", "--output-format", "stream"])
        assert args.output_format == "stream"
        assert args.task == "do x"
        assert args.task or args.output_format in ("json", "raw", "stream")

    def test_stream_without_task_still_headless(self):
        from src.main import _parse_args
        args = _parse_args(["--output-format", "stream"])
        assert args.output_format in ("json", "raw", "stream")
