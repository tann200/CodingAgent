"""OP-9: Tool output truncation tests.

Tests for _truncate_tool_output() in execution_node.py — ensures tool results
exceeding 50 KB are truncated before entering LLM history, preventing context
blowout from large bash output, git diffs, or file reads.
"""
import json

import pytest

from src.core.orchestration.graph.nodes.execution_node import (
    _truncate_tool_output,
    _TOOL_OUTPUT_MAX_BYTES,
)


class TestTruncateToolOutputUnderLimit:
    """Results under 50 KB pass through unchanged."""

    def test_small_result_unchanged(self):
        res = {"ok": True, "output": "hello world"}
        result = _truncate_tool_output(res)
        assert result is res, "Small result should be returned as-is (same object)"

    def test_empty_result_unchanged(self):
        res = {}
        result = _truncate_tool_output(res)
        assert result is res

    def test_result_at_limit_unchanged(self):
        # Exactly at the limit — should not truncate
        payload = "x" * (_TOOL_OUTPUT_MAX_BYTES - 30)
        res = {"ok": True, "output": payload}
        serialized = json.dumps(res)
        if len(serialized.encode()) <= _TOOL_OUTPUT_MAX_BYTES:
            result = _truncate_tool_output(res)
            assert result is res

    def test_non_string_fields_not_touched(self):
        res = {"ok": True, "count": 12345, "items": list(range(100))}
        result = _truncate_tool_output(res)
        # Under limit — returned unchanged
        assert result.get("count") == 12345


class TestTruncateToolOutputOverLimit:
    """Results over 50 KB are truncated with a clear marker."""

    def _big_output(self, field: str = "output", chars: int = 60_000) -> dict:
        return {"ok": True, "status": "ok", field: "A" * chars}

    def test_output_field_truncated(self):
        res = self._big_output("output", 60_000)
        result = _truncate_tool_output(res)
        assert result is not res, "Over-limit result must be a new dict"
        serialized = json.dumps(result, default=str)
        assert len(serialized.encode()) <= _TOOL_OUTPUT_MAX_BYTES + 200, (
            f"Truncated result should be at most {_TOOL_OUTPUT_MAX_BYTES} bytes "
            f"(got {len(serialized.encode())})"
        )

    def test_content_field_truncated(self):
        res = {"ok": True, "content": "B" * 80_000}
        result = _truncate_tool_output(res)
        serialized = json.dumps(result, default=str)
        assert len(serialized.encode()) <= _TOOL_OUTPUT_MAX_BYTES + 200

    def test_diff_field_truncated(self):
        res = {"ok": True, "diff": "C" * 70_000}
        result = _truncate_tool_output(res)
        serialized = json.dumps(result, default=str)
        assert len(serialized.encode()) <= _TOOL_OUTPUT_MAX_BYTES + 200

    def test_truncation_marker_present(self):
        res = self._big_output("output", 60_000)
        result = _truncate_tool_output(res)
        assert "OP-9" in result["output"], (
            "Truncated field must contain 'OP-9' marker"
        )
        assert "truncated" in result["output"].lower()

    def test_output_truncated_flag_set(self):
        res = self._big_output("output", 60_000)
        result = _truncate_tool_output(res)
        assert result.get("_output_truncated") is True

    def test_ok_field_preserved(self):
        res = self._big_output("output", 60_000)
        res["ok"] = True
        result = _truncate_tool_output(res)
        assert result.get("ok") is True, "ok field must not be removed during truncation"

    def test_original_dict_not_mutated(self):
        original_output = "X" * 70_000
        res = {"ok": True, "output": original_output}
        _truncate_tool_output(res)
        assert res["output"] == original_output, (
            "_truncate_tool_output must not mutate the input dict"
        )

    def test_stdout_field_truncated(self):
        res = {"ok": True, "stdout": "D" * 60_000, "stderr": ""}
        result = _truncate_tool_output(res)
        serialized = json.dumps(result, default=str)
        assert len(serialized.encode()) <= _TOOL_OUTPUT_MAX_BYTES + 200

    def test_very_large_output_still_under_limit(self):
        res = {"ok": True, "output": "Z" * 500_000}
        result = _truncate_tool_output(res)
        serialized = json.dumps(result, default=str)
        assert len(serialized.encode()) <= _TOOL_OUTPUT_MAX_BYTES + 500, (
            "Even 500KB output should be trimmed to ~50KB"
        )


class TestTruncateToolOutputEdgeCases:
    """Edge cases: non-dict, unserializable, no large text field."""

    def test_result_with_no_large_text_field(self):
        # Over 50KB purely from many small fields — unlikely but handled gracefully
        res = {str(i): "val" for i in range(100)}
        # This is well under 50KB, so it should pass through
        result = _truncate_tool_output(res)
        assert result is res

    def test_result_with_non_string_output(self):
        # output is a list, not a string — should not crash
        res = {"ok": True, "output": list(range(1000))}
        result = _truncate_tool_output(res)
        # Under 50KB — unchanged
        assert result is res

    def test_constant_is_50kb(self):
        assert _TOOL_OUTPUT_MAX_BYTES == 50_000, (
            "OP-9 limit should be exactly 50,000 bytes"
        )
