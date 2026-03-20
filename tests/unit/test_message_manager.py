from src.core.orchestration.message_manager import MessageManager


def test_message_manager_truncation():
    # Set small token window so truncation happens quickly
    mm = MessageManager(max_tokens=20)

    # Append a system message (should be preserved if possible)
    mm.append('system', 'system initialization instructions')

    # Append user and assistant messages until we exceed window
    for i in range(10):
        mm.append('user', f'user message {i} ' + ('x'*50))
        mm.append('assistant', f'assistant reply {i} ' + ('y'*50))

    msgs = mm.all()
    # Ensure total tokens under limit
    total_tokens = sum(mm._estimate_tokens(m['content']) for m in msgs)
    assert total_tokens <= mm.max_tokens
    # Ensure last messages are preserved (assistant last)
    assert msgs[-1]['role'] == 'assistant'
    # System message should either be preserved or only removed if budget impossible
    roles = [m['role'] for m in msgs]
    assert 'assistant' in roles


def test_message_manager_preserves_recent():
    mm = MessageManager(max_tokens=50)
    for i in range(6):
        mm.append('user', 'short ' + str(i))
    # After appending, ensure at least the last 2 messages are present
    msgs = mm.all()
    assert len(msgs) >= 2
    assert msgs[-1]['content'].startswith('short')


# ── Compaction tests ──────────────────────────────────────────────────────────

class TestCompaction:
    """Tests for inline context compaction via compact_callback."""

    def _make_mm(self, max_tokens=40, callback=None):
        return MessageManager(max_tokens=max_tokens, compact_callback=callback)

    def test_compact_callback_called_on_overflow(self):
        """compact_callback receives the dropped messages when overflow occurs."""
        captured = []

        def cb(msgs):
            captured.extend(msgs)
            return "summary of dropped messages"

        mm = self._make_mm(max_tokens=30, callback=cb)
        mm.append("system", "sys")
        for i in range(8):
            mm.append("user", f"user turn {i} " + "x" * 20)
            mm.append("assistant", f"assistant turn {i} " + "y" * 20)

        assert len(captured) > 0, "compact_callback should have been called"

    def test_compacted_context_injected_inline(self):
        """The summary returned by compact_callback is present in the conversation."""
        summary_text = "PRIOR CONTEXT: user asked to fix auth.py"

        def cb(msgs):
            return summary_text

        mm = self._make_mm(max_tokens=30, callback=cb)
        mm.append("system", "sys")
        for i in range(8):
            mm.append("user", f"message {i} " + "x" * 20)
            mm.append("assistant", f"reply {i} " + "y" * 20)

        contents = [m["content"] for m in mm.all()]
        assert any(summary_text in c for c in contents), (
            "Compacted summary should be present in conversation history"
        )

    def test_compacted_context_contains_xml_wrapper(self):
        """Compacted message is wrapped in <compacted_context> tags."""

        def cb(msgs):
            return "the summary"

        mm = self._make_mm(max_tokens=30, callback=cb)
        mm.append("system", "sys")
        for i in range(8):
            mm.append("user", f"u{i} " + "x" * 25)
            mm.append("assistant", f"a{i} " + "y" * 25)

        all_content = "\n".join(m["content"] for m in mm.all())
        assert "<compacted_context>" in all_content

    def test_compacted_message_placed_after_system(self):
        """The compacted context message is inserted right after the system message."""

        def cb(msgs):
            return "summary"

        mm = self._make_mm(max_tokens=30, callback=cb)
        mm.append("system", "sys")
        for i in range(8):
            mm.append("user", f"u{i} " + "x" * 20)
            mm.append("assistant", f"a{i} " + "y" * 20)

        msgs = mm.all()
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "<compacted_context>" in msgs[1]["content"]

    def test_no_compaction_without_callback(self):
        """Without compact_callback, old silent-drop behaviour is preserved."""
        mm = MessageManager(max_tokens=30)  # no callback
        mm.append("system", "sys")
        for i in range(8):
            mm.append("user", f"u{i} " + "x" * 20)
            mm.append("assistant", f"a{i} " + "y" * 20)

        contents = "\n".join(m["content"] for m in mm.all())
        assert "<compacted_context>" not in contents

    def test_compact_callback_failure_is_non_fatal(self):
        """If compact_callback raises, truncation still completes normally."""
        def bad_cb(msgs):
            raise RuntimeError("LLM unavailable")

        mm = self._make_mm(max_tokens=30, callback=bad_cb)
        mm.append("system", "sys")
        for i in range(8):
            mm.append("user", f"u{i} " + "x" * 20)
            mm.append("assistant", f"a{i} " + "y" * 20)

        # Should not raise; token count should be within limit
        total = sum(mm._estimate_tokens(m["content"]) for m in mm.all())
        assert total <= mm.max_tokens + mm._COMPACT_BUDGET


class TestContextBuilderSessionSummary:
    """Tests that TASK_STATE.md is auto-injected into the system prompt."""

    def test_task_state_injected_when_present(self, tmp_path, monkeypatch):
        from src.core.context.context_builder import ContextBuilder

        agent_ctx = tmp_path / ".agent-context"
        agent_ctx.mkdir()
        task_state = agent_ctx / "TASK_STATE.md"
        task_state.write_text(
            "# Current Task\nFix auth bug\n\n"
            "# Current State\nIn progress\n\n"
            "# Next Step\nRun tests"
        )
        monkeypatch.chdir(tmp_path)

        cb = ContextBuilder()
        msgs = cb.build_prompt(
            identity="I am an agent",
            role="coder",
            active_skills=[],
            task_description="do something",
            tools=[],
            conversation=[],
        )
        system_content = msgs[0]["content"]
        assert "<session_summary>" in system_content
        assert "Fix auth bug" in system_content

    def test_empty_task_state_not_injected(self, tmp_path, monkeypatch):
        from src.core.context.context_builder import ContextBuilder

        agent_ctx = tmp_path / ".agent-context"
        agent_ctx.mkdir()
        task_state = agent_ctx / "TASK_STATE.md"
        task_state.write_text("# Current Task\n\n# Completed Steps\n\n# Next Step")
        monkeypatch.chdir(tmp_path)

        cb = ContextBuilder()
        msgs = cb.build_prompt(
            identity="I am an agent",
            role="coder",
            active_skills=[],
            task_description="do something",
            tools=[],
            conversation=[],
        )
        system_content = msgs[0]["content"]
        assert "<session_summary>" not in system_content

    def test_missing_task_state_file_does_not_crash(self, tmp_path, monkeypatch):
        from src.core.context.context_builder import ContextBuilder

        monkeypatch.chdir(tmp_path)  # no .agent-context dir

        cb = ContextBuilder()
        msgs = cb.build_prompt(
            identity="I am an agent",
            role="coder",
            active_skills=[],
            task_description="do something",
            tools=[],
            conversation=[],
        )
        assert len(msgs) >= 1  # should not raise


class TestDistillerRicherFormat:
    """Tests for the enriched TASK_STATE.md format."""

    def test_compact_messages_to_prose_fallback(self):
        """compact_messages_to_prose returns a fallback when LLM is unavailable."""
        from src.core.memory.distiller import compact_messages_to_prose
        import unittest.mock as mock

        with mock.patch(
            "src.core.memory.distiller._call_llm_sync", side_effect=Exception("no LLM")
        ):
            result = compact_messages_to_prose(
                [{"role": "user", "content": "fix the bug"}]
            )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_compact_messages_empty_returns_empty(self):
        from src.core.memory.distiller import compact_messages_to_prose
        result = compact_messages_to_prose([])
        assert result == ""

    def test_distill_context_writes_richer_format(self, tmp_path):
        """distill_context writes the enriched TASK_STATE.md when LLM succeeds."""
        from src.core.memory.distiller import distill_context
        import json
        import unittest.mock as mock

        (tmp_path / ".agent-context").mkdir()
        rich_state = {
            "current_task": "Fix login bug",
            "current_state": "Identified root cause",
            "files_modified": ["src/auth.py"],
            "completed_steps": ["Read auth.py", "Found bug"],
            "errors_resolved": ["AttributeError fixed"],
            "next_step": "Apply fix",
        }

        with mock.patch(
            "src.core.memory.distiller._call_llm_sync",
            return_value=json.dumps(rich_state),
        ):
            _ = distill_context(
                [{"role": "user", "content": "fix auth"}],
                working_dir=tmp_path,
            )

        task_state = (tmp_path / ".agent-context" / "TASK_STATE.md").read_text()
        assert "Fix login bug" in task_state
        assert "Identified root cause" in task_state
        assert "src/auth.py" in task_state
        assert "AttributeError fixed" in task_state
        assert "# Files Modified" in task_state
        assert "# Errors Resolved" in task_state

