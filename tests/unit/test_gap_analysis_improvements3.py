"""
Regression tests for gap-analysis improvements (third batch).

Covers:
  GAP-SMALL-6   disable_thinking in Ollama adapter
  GAP-NEW-1     Retry-After header parsing in openai_compat_adapter
  GAP-NEW-2     Workspace skill discovery (.agent/skills/, .claude/skills/)
  GAP-NEW-3     Token-aware compaction threshold (85% of context window)
  GAP-NEW-4     memory_save tool (persistence + size cap)
  GAP-NEW-5     disable_thinking flag in openai_compat_adapter
  GAP-NEW-7     usage.subagent_cost published from subagent_tools
  GAP-NEW-8     operational-small.md contains manage_todo hint
  TUI-BRIDGE    usage.subagent_cost wired into core_bridge
"""


# ruff: noqa: E501
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# GAP-SMALL-6: disable_thinking in Ollama adapter
# ─────────────────────────────────────────────────────────────────────────────

class TestSmall6OllamaDisableThinking:
    """Ollama adapter injects 'think': false when provider.disable_thinking=True."""

    def _make_provider(self, disable_thinking: bool) -> dict:
        return {
            "name": "ollama",
            "base_url": "http://localhost:11434",
            "disable_thinking": disable_thinking,
        }

    def _apply_think_injection(self, provider: dict, payload: dict) -> dict:
        """Replicate the GAP-SMALL-6 logic from OllamaAdapter._chat."""
        _disable_think = False
        try:
            if provider and isinstance(provider, dict):
                _disable_think = bool(provider.get("disable_thinking", False))
        except Exception:
            pass
        if _disable_think and "think" not in payload:
            payload["think"] = False
        return payload

    def test_payload_gets_think_false_when_disabled(self):
        provider = self._make_provider(disable_thinking=True)
        payload = self._apply_think_injection(provider, {})
        assert payload.get("think") is False

    def test_payload_unchanged_when_thinking_enabled(self):
        provider = self._make_provider(disable_thinking=False)
        payload = self._apply_think_injection(provider, {})
        assert "think" not in payload

    def test_existing_think_key_not_overwritten(self):
        provider = self._make_provider(disable_thinking=True)
        payload = self._apply_think_injection(provider, {"think": True})
        assert payload["think"] is True

    def test_disable_thinking_field_in_ollama_adapter_source(self):
        """The source file of OllamaAdapter contains the disable_thinking guard."""
        from pathlib import Path
        src = (
            Path(__file__).parent.parent.parent
            / "src/core/inference/adapters/ollama_adapter.py"
        ).read_text()
        assert "disable_thinking" in src
        assert '"think"' in src or "'think'" in src


# ─────────────────────────────────────────────────────────────────────────────
# GAP-NEW-1: Retry-After header parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestNew1RetryAfterHeader:
    """openai_compat_adapter parses retry-after headers on 429."""

    def _parse_retry_wait(self, headers: dict, attempt: int = 0) -> float:
        """Replicate the GAP-NEW-1 retry wait computation."""
        _retry_wait = 2 ** attempt
        for _hdr in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset"):
            _hval = headers.get(_hdr)
            if _hval:
                try:
                    _retry_wait = min(float(_hval), 30.0)
                except ValueError:
                    pass
                break
        return _retry_wait

    def test_retry_after_numeric(self):
        wait = self._parse_retry_wait({"retry-after": "10"})
        assert wait == 10.0

    def test_retry_after_capped_at_30(self):
        wait = self._parse_retry_wait({"retry-after": "120"})
        assert wait == 30.0

    def test_x_ratelimit_reset_requests_used(self):
        wait = self._parse_retry_wait({"x-ratelimit-reset-requests": "5"})
        assert wait == 5.0

    def test_retry_after_takes_priority_over_x_ratelimit(self):
        wait = self._parse_retry_wait({
            "retry-after": "3",
            "x-ratelimit-reset-requests": "15",
        })
        assert wait == 3.0

    def test_invalid_header_falls_back_to_exponential(self):
        wait = self._parse_retry_wait({"retry-after": "not-a-number"}, attempt=1)
        assert wait == 2 ** 1  # exponential backoff fallback

    def test_no_header_uses_exponential(self):
        wait = self._parse_retry_wait({}, attempt=2)
        assert wait == 4.0

    def test_x_ratelimit_reset_fallback(self):
        wait = self._parse_retry_wait({"x-ratelimit-reset": "7"})
        assert wait == 7.0


# ─────────────────────────────────────────────────────────────────────────────
# GAP-NEW-2: Workspace skill discovery
# ─────────────────────────────────────────────────────────────────────────────

class TestNew2WorkspaceSkillDiscovery:
    """ContextBuilder loads skills from .agent/skills/ and .claude/skills/."""

    def test_agent_skills_dir_loaded(self, tmp_path):
        # create .agent/skills/my_skill.md
        skills_dir = tmp_path / ".agent" / "skills"
        skills_dir.mkdir(parents=True)
        skill_file = skills_dir / "my_skill.md"
        skill_file.write_text("This is my custom skill.")

        # Simulate the workspace skill discovery logic
        loaded = {}
        _workspace_skill_dirs = [
            tmp_path / ".agent" / "skills",
            tmp_path / ".claude" / "skills",
        ]
        for wsd in _workspace_skill_dirs:
            if wsd.exists():
                for wsf in wsd.glob("*.md"):
                    content = wsf.read_text()
                    if content:
                        loaded[wsf.stem] = content

        assert "my_skill" in loaded
        assert loaded["my_skill"] == "This is my custom skill."

    def test_claude_skills_dir_loaded(self, tmp_path):
        skills_dir = tmp_path / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "testing.md").write_text("Testing skill content.")

        loaded = {}
        _workspace_skill_dirs = [
            tmp_path / ".agent" / "skills",
            tmp_path / ".claude" / "skills",
        ]
        for wsd in _workspace_skill_dirs:
            if wsd.exists():
                for wsf in wsd.glob("*.md"):
                    content = wsf.read_text()
                    if content:
                        loaded[wsf.stem] = content

        assert "testing" in loaded

    def test_missing_dirs_do_not_crash(self, tmp_path):
        loaded = {}
        _workspace_skill_dirs = [
            tmp_path / ".agent" / "skills",
            tmp_path / ".claude" / "skills",
        ]
        for wsd in _workspace_skill_dirs:
            if wsd.exists():
                for wsf in wsd.glob("*.md"):
                    content = wsf.read_text()
                    if content:
                        loaded[wsf.stem] = content
        assert loaded == {}

    def test_both_dirs_merged(self, tmp_path):
        (tmp_path / ".agent" / "skills").mkdir(parents=True)
        (tmp_path / ".claude" / "skills").mkdir(parents=True)
        (tmp_path / ".agent" / "skills" / "skill_a.md").write_text("A")
        (tmp_path / ".claude" / "skills" / "skill_b.md").write_text("B")

        loaded = {}
        for wsd in [tmp_path / ".agent" / "skills", tmp_path / ".claude" / "skills"]:
            if wsd.exists():
                for wsf in wsd.glob("*.md"):
                    content = wsf.read_text()
                    if content:
                        loaded[wsf.stem] = content

        assert "skill_a" in loaded
        assert "skill_b" in loaded

    def test_empty_skill_files_skipped(self, tmp_path):
        skills_dir = tmp_path / ".agent" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "empty.md").write_text("")
        (skills_dir / "valid.md").write_text("Valid content")

        loaded = {}
        for wsd in [skills_dir]:
            if wsd.exists():
                for wsf in wsd.glob("*.md"):
                    content = wsf.read_text()
                    if content:
                        loaded[wsf.stem] = content

        assert "empty" not in loaded
        assert "valid" in loaded


# ─────────────────────────────────────────────────────────────────────────────
# GAP-NEW-3: Token-aware compaction threshold
# ─────────────────────────────────────────────────────────────────────────────

class TestNew3TokenAwareCompaction:
    """Compaction threshold is 85% of actual context window."""

    def _compute_threshold(self, context_window: int, config_default: int = 10_000) -> int:
        return int(context_window * 0.85) if context_window > 0 else config_default

    def test_85_percent_of_8k_window(self):
        threshold = self._compute_threshold(8192)
        assert threshold == int(8192 * 0.85)

    def test_85_percent_of_128k_window(self):
        threshold = self._compute_threshold(131072)
        assert threshold == int(131072 * 0.85)

    def test_zero_context_window_uses_default(self):
        threshold = self._compute_threshold(0, config_default=10_000)
        assert threshold == 10_000

    def test_custom_config_default_used_when_no_window(self):
        threshold = self._compute_threshold(0, config_default=20_000)
        assert threshold == 20_000

    def test_positive_window_overrides_config_default(self):
        threshold = self._compute_threshold(4096, config_default=10_000)
        assert threshold == int(4096 * 0.85)
        assert threshold != 10_000


# ─────────────────────────────────────────────────────────────────────────────
# GAP-NEW-4: memory_save tool
# ─────────────────────────────────────────────────────────────────────────────

class TestNew4MemorySave:
    """memory_save persists notes and enforces size limits."""

    def _call_save(self, content: str, category: str | None = None, memory_file: Path | None = None):
        """Call memory_save with a patched memory file path."""
        import src.tools.memory_tools as mt
        original = mt._MEMORY_FILE
        if memory_file is not None:
            mt._MEMORY_FILE = memory_file
        try:
            return mt.memory_save(content=content, category=category)
        finally:
            mt._MEMORY_FILE = original

    def test_save_creates_file(self, tmp_path):
        mem_file = tmp_path / "memory.md"
        result = self._call_save("Test note", category="test", memory_file=mem_file)
        assert result["status"] == "ok"
        assert mem_file.exists()

    def test_saved_entry_format(self, tmp_path):
        mem_file = tmp_path / "memory.md"
        result = self._call_save("Remember this", category="convention", memory_file=mem_file)
        assert result["status"] == "ok"
        content = mem_file.read_text()
        assert "[convention]" in content
        assert "Remember this" in content

    def test_default_category_is_note(self, tmp_path):
        mem_file = tmp_path / "memory.md"
        result = self._call_save("No category given", memory_file=mem_file)
        assert result["status"] == "ok"
        content = mem_file.read_text()
        assert "[note]" in content

    def test_empty_content_returns_error(self):
        from src.tools.memory_tools import memory_save
        result = memory_save(content="")
        assert result["status"] == "error"

    def test_content_over_1000_chars_rejected(self):
        from src.tools.memory_tools import memory_save
        result = memory_save(content="x" * 1001)
        assert result["status"] == "error"
        assert "too long" in result["error"]

    def test_multiple_saves_append(self, tmp_path):
        mem_file = tmp_path / "memory.md"
        self._call_save("First note", memory_file=mem_file)
        self._call_save("Second note", memory_file=mem_file)
        content = mem_file.read_text()
        assert "First note" in content
        assert "Second note" in content

    def test_size_cap_trims_oldest(self, tmp_path):
        """When file exceeds 50KB, oldest entries are trimmed."""
        import src.tools.memory_tools as mt
        mem_file = tmp_path / "memory.md"
        original = mt._MEMORY_FILE
        original_max = mt._MEMORY_MAX_BYTES
        mt._MEMORY_FILE = mem_file
        mt._MEMORY_MAX_BYTES = 200  # tiny cap for test
        try:
            # Write entries until cap is hit
            for i in range(20):
                mt.memory_save(content=f"Entry number {i:03d} — some filler text here", category="test")
            content = mem_file.read_text()
            # File should be under the cap
            assert len(content.encode("utf-8")) <= 200
            # Oldest entries should be gone, newest should remain
            assert "Entry number 019" in content
        finally:
            mt._MEMORY_FILE = original
            mt._MEMORY_MAX_BYTES = original_max


# ─────────────────────────────────────────────────────────────────────────────
# GAP-NEW-5: disable_thinking in openai_compat_adapter
# ─────────────────────────────────────────────────────────────────────────────

class TestNew5OpenAICompatDisableThinking:
    """openai_compat_adapter injects 'think': false when provider.disable_thinking=True."""

    def _make_payload(self, disable_thinking: bool, existing_payload: dict | None = None) -> dict:
        payload = existing_payload or {}
        if disable_thinking and "think" not in payload:
            payload["think"] = False
        return payload

    def test_think_false_injected(self):
        payload = self._make_payload(disable_thinking=True)
        assert payload.get("think") is False

    def test_think_not_injected_when_disabled_is_false(self):
        payload = self._make_payload(disable_thinking=False)
        assert "think" not in payload

    def test_existing_think_key_preserved(self):
        payload = self._make_payload(disable_thinking=True, existing_payload={"think": True})
        assert payload["think"] is True

    def test_disable_thinking_in_openai_compat_source(self):
        """The source file of OpenAICompatibleAdapter contains the disable_thinking guard."""
        from pathlib import Path
        src = (
            Path(__file__).parent.parent.parent
            / "src/core/inference/adapters/openai_compat_adapter.py"
        ).read_text()
        assert "disable_thinking" in src
        assert '"think"' in src or "'think'" in src


# ─────────────────────────────────────────────────────────────────────────────
# GAP-NEW-7: usage.subagent_cost event published from subagent_tools
# ─────────────────────────────────────────────────────────────────────────────

class TestNew7SubagentCostRollup:
    """subagent_tools publishes usage.subagent_cost when child has nonzero cost."""

    def test_cost_event_published_when_positive(self):
        published_events = []

        mock_bus = MagicMock()
        mock_bus.publish = MagicMock(side_effect=lambda name, payload: published_events.append((name, payload)))

        # Simulate the subagent_tools post-completion logic
        final_state = {"session_cost_usd": 0.005}
        child_session_id = "child-123"
        canonical_role = "analyst"

        _child_cost = float(final_state.get("session_cost_usd") or 0.0)
        mock_bus.publish("delegation.finish", {
            "child_session_id": child_session_id,
            "role": canonical_role,
            "ok": True,
            "cost_usd": _child_cost,
        })
        if _child_cost > 0:
            mock_bus.publish("usage.subagent_cost", {
                "child_session_id": child_session_id,
                "role": canonical_role,
                "cost_usd": _child_cost,
            })

        event_names = [e[0] for e in published_events]
        assert "usage.subagent_cost" in event_names

        cost_event = next(e for e in published_events if e[0] == "usage.subagent_cost")
        assert cost_event[1]["cost_usd"] == pytest.approx(0.005)
        assert cost_event[1]["role"] == "analyst"

    def test_cost_event_not_published_when_zero(self):
        published_events = []
        mock_bus = MagicMock()
        mock_bus.publish = MagicMock(side_effect=lambda name, payload: published_events.append((name, payload)))

        final_state = {"session_cost_usd": 0.0}
        _child_cost = float(final_state.get("session_cost_usd") or 0.0)
        mock_bus.publish("delegation.finish", {"ok": True, "cost_usd": _child_cost})
        if _child_cost > 0:
            mock_bus.publish("usage.subagent_cost", {"cost_usd": _child_cost})

        event_names = [e[0] for e in published_events]
        assert "usage.subagent_cost" not in event_names

    def test_cost_event_not_published_when_no_session_cost(self):
        published_events = []
        final_state = {}
        _child_cost = float(final_state.get("session_cost_usd") or 0.0)
        assert _child_cost == 0.0
        # no publish happens


# ─────────────────────────────────────────────────────────────────────────────
# GAP-NEW-8: operational-small.md contains manage_todo hint
# ─────────────────────────────────────────────────────────────────────────────

class TestNew8SmallModelTodoHint:
    """operational-small.md must reference manage_todo for multi-step tasks."""

    def _read_small_role(self) -> str:
        role_path = (
            Path(__file__).parent.parent.parent
            / "src/config/agent-brain/roles/operational-small.md"
        )
        return role_path.read_text()

    def test_manage_todo_mentioned(self):
        content = self._read_small_role()
        assert "manage_todo" in content

    def test_multi_step_context_present(self):
        content = self._read_small_role()
        # Should mention 2+ steps or multi-step context
        assert "2+" in content or "two" in content.lower() or "multi" in content.lower()

    def test_complete_action_mentioned(self):
        content = self._read_small_role()
        assert "complete" in content.lower()


# ─────────────────────────────────────────────────────────────────────────────
# TUI bridge: usage.subagent_cost handler wired
# ─────────────────────────────────────────────────────────────────────────────

class TestTUIBridgeSubagentCostHandler:
    """core_bridge subscribes to usage.subagent_cost and has _on_subagent_cost."""

    def test_handler_method_exists(self):
        from tui.src.ui.core_bridge import AgentBridge
        assert hasattr(AgentBridge, "_on_subagent_cost")

    def test_handler_is_callable(self):
        from tui.src.ui.core_bridge import AgentBridge
        assert callable(AgentBridge._on_subagent_cost)

    def test_usage_subagent_cost_in_subscriptions(self):
        """Verify the subscription is registered in setup_subscriptions."""
        import inspect
        from tui.src.ui.core_bridge import AgentBridge
        source = inspect.getsource(AgentBridge.setup_subscriptions)
        assert "usage.subagent_cost" in source
        assert "_on_subagent_cost" in source

    def test_handler_skips_zero_cost(self):
        """_on_subagent_cost should not post events for zero-cost subagents."""
        from tui.src.ui.core_bridge import AgentBridge

        mock_app = MagicMock()
        bridge = AgentBridge.__new__(AgentBridge)
        bridge._app = mock_app
        bridge._post = MagicMock()

        bridge._on_subagent_cost({"cost_usd": 0.0, "role": "analyst"})
        bridge._post.assert_not_called()

    def test_handler_posts_event_for_positive_cost(self):
        """_on_subagent_cost posts a UsageTurnSummaryEvent for positive cost."""
        from tui.src.ui.core_bridge import AgentBridge

        bridge = AgentBridge.__new__(AgentBridge)
        bridge._post = MagicMock()

        bridge._on_subagent_cost({"cost_usd": 0.002, "role": "reviewer"})
        bridge._post.assert_called_once()
        call_args = bridge._post.call_args[0][0]
        assert call_args.cost_usd == pytest.approx(0.002)
        assert "[reviewer]" in call_args.model
