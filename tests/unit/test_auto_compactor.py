"""Unit tests for src/core/memory/auto_compactor.py (CP-6).

Tests mirror the scenarios validated by compact.rs in claw-code.
"""


# ruff: noqa: E501
from __future__ import annotations

import pytest

from src.core.memory.auto_compactor import (
    COMPACT_CONTINUATION_PREAMBLE,
    COMPACT_DIRECT_RESUME_INSTRUCTION,
    COMPACT_RECENT_MESSAGES_NOTE,
    AutoCompactConfig,
    CompactResult,
    _collect_key_files,
    _extract_existing_compacted_summary,
    _extract_summary_highlights,
    _extract_summary_timeline,
    _infer_pending_work,
    _merge_compact_summaries,
    _summarize_messages,
    compact_messages,
    estimate_message_tokens,
    estimate_messages_tokens,
    format_compact_summary,
    get_compact_continuation_message,
    should_compact,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_messages(n: int, chars_per_msg: int = 100, role: str = "user") -> list[dict]:
    """Create *n* simple messages of a given role."""
    return [{"role": role, "content": "x" * chars_per_msg} for _ in range(n)]


def _big_messages(n: int = 50, chars_per_msg: int = 1000) -> list[dict]:
    """Create enough messages to trigger compaction at default config."""
    msgs: list[dict] = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": "word " * (chars_per_msg // 5)})
    return msgs


# ---------------------------------------------------------------------------
# estimate_message_tokens
# ---------------------------------------------------------------------------


class TestEstimateMessageTokens:
    def test_basic_string_content(self):
        msg = {"role": "user", "content": "a" * 400}
        # 400 // 4 + 1 == 101
        assert estimate_message_tokens(msg) == 101

    def test_empty_content(self):
        msg = {"role": "user", "content": ""}
        # 0 // 4 + 1 == 1
        assert estimate_message_tokens(msg) == 1

    def test_none_content(self):
        msg = {"role": "user"}
        assert estimate_message_tokens(msg) == 1

    def test_multipart_content(self):
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "a" * 400},
                {"type": "text", "text": "b" * 400},
            ],
        }
        # (400//4+1) + (400//4+1) == 202
        assert estimate_message_tokens(msg) == 202

    def test_estimate_messages_tokens_sum(self):
        msgs = [
            {"role": "user", "content": "a" * 400},
            {"role": "assistant", "content": "b" * 400},
        ]
        assert estimate_messages_tokens(msgs) == 202


# ---------------------------------------------------------------------------
# should_compact
# ---------------------------------------------------------------------------


class TestShouldCompact:
    def test_false_when_too_few_messages(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=10_000)
        msgs = _make_messages(3)  # <= preserve_recent
        assert should_compact(msgs, config) is False

    def test_false_when_token_count_low(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=10_000)
        # 5 messages × 25 tokens each = 125 tokens — well under 10k
        msgs = _make_messages(5, chars_per_msg=100)
        assert should_compact(msgs, config) is False

    def test_true_when_exceeds_threshold(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        # 10 messages × (500//4+1 = 126 tokens each) > 100
        msgs = _make_messages(10, chars_per_msg=500)
        assert should_compact(msgs, config) is True

    def test_existing_prefix_excluded_from_compactable(self):
        """Messages starting with a compact prefix should exclude that prefix
        from the token count so we don't double-summarise it."""
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        big_summary_content = (
            COMPACT_CONTINUATION_PREAMBLE
            + "Summary:\nsome prior summary\n"
            + f"\n\n{COMPACT_RECENT_MESSAGES_NOTE}"
            + f"\n{COMPACT_DIRECT_RESUME_INSTRUCTION}"
        )
        prefix_msg = {"role": "system", "content": big_summary_content}
        # Only 3 additional short messages — should NOT compact (<=preserve_recent)
        msgs = [prefix_msg] + _make_messages(3, chars_per_msg=100)
        assert should_compact(msgs, config) is False


# ---------------------------------------------------------------------------
# compact_messages — no-op when below threshold
# ---------------------------------------------------------------------------


class TestCompactMessagesNoOp:
    def test_returns_original_when_no_compaction_needed(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=10_000)
        msgs = _make_messages(5, chars_per_msg=100)
        result = compact_messages(msgs, config)
        assert result.removed_message_count == 0
        assert result.compacted_messages == msgs
        assert result.summary == ""
        assert result.formatted_summary == ""


# ---------------------------------------------------------------------------
# compact_messages — actual compaction
# ---------------------------------------------------------------------------


class TestCompactMessages:
    def test_produces_compact_result_type(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(20)
        result = compact_messages(msgs, config)
        assert isinstance(result, CompactResult)

    def test_removed_count_is_positive(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(20)
        result = compact_messages(msgs, config)
        assert result.removed_message_count > 0

    def test_compacted_messages_starts_with_system(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(20)
        result = compact_messages(msgs, config)
        assert result.compacted_messages[0]["role"] == "system"

    def test_continuation_preamble_present(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(20)
        result = compact_messages(msgs, config)
        system_content = result.compacted_messages[0]["content"]
        assert system_content.startswith(COMPACT_CONTINUATION_PREAMBLE)

    def test_recent_messages_note_present(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(20)
        result = compact_messages(msgs, config)
        system_content = result.compacted_messages[0]["content"]
        assert COMPACT_RECENT_MESSAGES_NOTE in system_content

    def test_resume_instruction_present(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(20)
        result = compact_messages(msgs, config)
        system_content = result.compacted_messages[0]["content"]
        assert COMPACT_DIRECT_RESUME_INSTRUCTION in system_content

    def test_preserves_recent_messages_verbatim(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(10)
        result = compact_messages(msgs, config)
        # Last 4 original messages should appear after the system summary
        tail = result.compacted_messages[1:]
        assert tail == msgs[-4:]

    def test_total_message_count_reduced(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(20)
        result = compact_messages(msgs, config)
        # Should be 1 system summary + 4 preserved = 5, well below 20
        assert len(result.compacted_messages) <= len(msgs)
        assert len(result.compacted_messages) == 1 + config.preserve_recent

    def test_summary_contains_scope_line(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(10)
        result = compact_messages(msgs, config)
        assert "Scope:" in result.summary or "Scope:" in result.formatted_summary

    def test_formatted_summary_has_no_summary_tags(self):
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(10)
        result = compact_messages(msgs, config)
        assert "<summary>" not in result.formatted_summary
        assert "</summary>" not in result.formatted_summary

    def test_default_config_used_when_none(self):
        """compact_messages(msgs) should use AutoCompactConfig() defaults."""
        # Default max_tokens=10_000 — need enough data to exceed it
        msgs = _big_messages(n=100, chars_per_msg=600)
        result = compact_messages(msgs)
        assert result.removed_message_count > 0


# ---------------------------------------------------------------------------
# Re-compaction (merge path)
# ---------------------------------------------------------------------------


class TestRecompaction:
    def test_second_compaction_merges_summaries(self):
        """Compacting an already-compacted history should produce a merged summary."""
        config = AutoCompactConfig(preserve_recent=2, max_tokens=50)

        # First compaction
        msgs = _big_messages(10, chars_per_msg=200)
        first = compact_messages(msgs, config)
        assert first.removed_message_count > 0

        # Second compaction on already-compacted messages
        second = compact_messages(first.compacted_messages, config)
        if second.removed_message_count > 0:
            system_content = second.compacted_messages[0]["content"]
            assert COMPACT_CONTINUATION_PREAMBLE in system_content

    def test_extract_existing_compacted_summary_roundtrip(self):
        """A summary prefix built by compact_messages should be detectable."""
        config = AutoCompactConfig(preserve_recent=4, max_tokens=100)
        msgs = _big_messages(15)
        result = compact_messages(msgs, config)
        prefix_msg = result.compacted_messages[0]
        extracted = _extract_existing_compacted_summary(prefix_msg)
        assert extracted is not None
        assert len(extracted) > 0

    def test_extract_existing_returns_none_for_non_system(self):
        msg = {"role": "user", "content": COMPACT_CONTINUATION_PREAMBLE + "stuff"}
        assert _extract_existing_compacted_summary(msg) is None

    def test_extract_existing_returns_none_for_missing_preamble(self):
        msg = {"role": "system", "content": "some other system message"}
        assert _extract_existing_compacted_summary(msg) is None


# ---------------------------------------------------------------------------
# format_compact_summary
# ---------------------------------------------------------------------------


class TestFormatCompactSummary:
    def test_strips_summary_tags(self):
        raw = "<summary>\nConversation summary:\n- Scope: 5 messages.\n</summary>"
        formatted = format_compact_summary(raw)
        assert "<summary>" not in formatted
        assert "</summary>" not in formatted

    def test_adds_summary_prefix(self):
        raw = "<summary>\nConversation summary:\n- Scope: 5 messages.\n</summary>"
        formatted = format_compact_summary(raw)
        assert "Summary:" in formatted

    def test_strips_analysis_block(self):
        raw = "<analysis>some thinking</analysis><summary>\nConversation summary:\n</summary>"
        formatted = format_compact_summary(raw)
        assert "<analysis>" not in formatted
        assert "some thinking" not in formatted

    def test_collapses_blank_lines(self):
        raw = "line1\n\n\n\nline2"
        formatted = format_compact_summary(raw)
        assert "\n\n\n" not in formatted


# ---------------------------------------------------------------------------
# Key file collection
# ---------------------------------------------------------------------------


class TestCollectKeyFiles:
    def test_finds_python_files(self):
        msgs = [
            {"role": "user", "content": "I edited src/core/memory/auto_compactor.py"},
            {
                "role": "assistant",
                "content": "Done, see src/core/memory/auto_compactor.py",
            },
        ]
        files = _collect_key_files(msgs)
        assert any("auto_compactor.py" in f for f in files)

    def test_finds_ts_files(self):
        msgs = [{"role": "user", "content": "See src/components/App.tsx for the fix"}]
        files = _collect_key_files(msgs)
        assert any("App.tsx" in f for f in files)

    def test_ignores_tokens_without_slash(self):
        msgs = [{"role": "user", "content": "file.py is the module name"}]
        files = _collect_key_files(msgs)
        assert files == []

    def test_deduplicates(self):
        path = "src/foo/bar.py"
        msgs = [
            {"role": "user", "content": f"see {path}"},
            {"role": "assistant", "content": f"edited {path}"},
        ]
        files = _collect_key_files(msgs)
        assert files.count(path) == 1

    def test_capped_at_eight(self):
        content = " ".join(f"src/module{i}/file{i}.py" for i in range(20))
        msgs = [{"role": "user", "content": content}]
        files = _collect_key_files(msgs)
        assert len(files) <= 8


# ---------------------------------------------------------------------------
# Pending work inference
# ---------------------------------------------------------------------------


class TestInferPendingWork:
    def test_finds_todo_keyword(self):
        msgs = [
            {"role": "assistant", "content": "TODO: add tests"},
            {"role": "user", "content": "ok"},
        ]
        pending = _infer_pending_work(msgs)
        assert any("TODO" in p for p in pending)

    def test_finds_next_keyword(self):
        msgs = [{"role": "assistant", "content": "Next step is to run tests"}]
        pending = _infer_pending_work(msgs)
        assert any("Next" in p for p in pending)

    def test_capped_at_three(self):
        msgs = [{"role": "assistant", "content": f"TODO item {i}"} for i in range(10)]
        pending = _infer_pending_work(msgs)
        assert len(pending) <= 3


# ---------------------------------------------------------------------------
# get_compact_continuation_message
# ---------------------------------------------------------------------------


class TestGetCompactContinuationMessage:
    def test_contains_preamble(self):
        msg = get_compact_continuation_message("<summary>\nsome summary\n</summary>")
        assert msg.startswith(COMPACT_CONTINUATION_PREAMBLE)

    def test_contains_resume_instruction_when_enabled(self):
        msg = get_compact_continuation_message(
            "<summary>\nsome\n</summary>",
            suppress_follow_up_questions=True,
        )
        assert COMPACT_DIRECT_RESUME_INSTRUCTION in msg

    def test_no_resume_instruction_when_disabled(self):
        msg = get_compact_continuation_message(
            "<summary>\nsome\n</summary>",
            suppress_follow_up_questions=False,
        )
        assert COMPACT_DIRECT_RESUME_INSTRUCTION not in msg

    def test_recent_messages_note_when_preserved(self):
        msg = get_compact_continuation_message(
            "<summary>\nsome\n</summary>",
            recent_messages_preserved=True,
        )
        assert COMPACT_RECENT_MESSAGES_NOTE in msg

    def test_no_recent_messages_note_when_not_preserved(self):
        msg = get_compact_continuation_message(
            "<summary>\nsome\n</summary>",
            recent_messages_preserved=False,
        )
        assert COMPACT_RECENT_MESSAGES_NOTE not in msg


# ---------------------------------------------------------------------------
# merge_compact_summaries
# ---------------------------------------------------------------------------


class TestMergeCompactSummaries:
    def test_no_existing_returns_new(self):
        new = "<summary>\nConversation summary:\n- Scope: 5.\n</summary>"
        merged = _merge_compact_summaries(None, new)
        assert merged == new

    def test_merged_contains_previously_compacted(self):
        existing = "<summary>\nConversation summary:\n- Scope: 3.\n</summary>"
        new = "<summary>\nConversation summary:\n- Scope: 5.\n</summary>"
        merged = _merge_compact_summaries(existing, new)
        assert "Previously compacted context" in merged

    def test_merged_contains_newly_compacted(self):
        existing = "<summary>\nConversation summary:\n- Scope: 3.\n</summary>"
        new = "<summary>\nConversation summary:\n- Scope: 5.\n</summary>"
        merged = _merge_compact_summaries(existing, new)
        assert "Newly compacted context" in merged


# ---------------------------------------------------------------------------
# _summarize_messages
# ---------------------------------------------------------------------------


class TestSummarizeMessages:
    def test_has_summary_tags(self):
        msgs = _big_messages(5)
        summary = _summarize_messages(msgs)
        assert summary.startswith("<summary>")
        assert summary.endswith("</summary>")

    def test_scope_line_present(self):
        msgs = _make_messages(5, role="user")
        summary = _summarize_messages(msgs)
        assert "Scope:" in summary

    def test_timeline_present(self):
        msgs = _make_messages(3)
        summary = _summarize_messages(msgs)
        assert "Key timeline:" in summary

    def test_user_count_correct(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "bye"},
        ]
        summary = _summarize_messages(msgs)
        assert "user=2" in summary

    def test_key_files_included_when_present(self):
        msgs = [
            {"role": "user", "content": "see src/foo/bar.py for the bug"},
        ]
        summary = _summarize_messages(msgs)
        assert "bar.py" in summary


# ---------------------------------------------------------------------------
# extract_summary_highlights / timeline
# ---------------------------------------------------------------------------


class TestExtractHelpers:
    def test_highlights_excludes_timeline(self):
        raw = (
            "<summary>\nConversation summary:\n"
            "- Scope: 5.\n"
            "- Key timeline:\n"
            "  - user: hi\n"
            "</summary>"
        )
        highlights = _extract_summary_highlights(raw)
        assert all("user: hi" not in h for h in highlights)
        assert any("Scope" in h for h in highlights)

    def test_timeline_contains_entries(self):
        raw = (
            "<summary>\nConversation summary:\n"
            "- Key timeline:\n"
            "  - user: hi\n"
            "  - assistant: hello\n"
            "</summary>"
        )
        timeline = _extract_summary_timeline(raw)
        assert any("user: hi" in t for t in timeline)
        assert any("assistant: hello" in t for t in timeline)
