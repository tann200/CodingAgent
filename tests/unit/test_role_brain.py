"""
Contract tests for agent-brain role files (Phase 3.6 / MC-5).

Guarantees:
- MC-5: No role brain file references defunct P2P broadcast topics
  (``agent.<role>.broadcast``) that do not exist in the current typed event
  architecture.
- The ``researcher`` role name is a canonical alias for ``analyst`` and must
  not exist as its own brain file — it would be dead content (subagents are
  compiled with ``compile_system_prompt(canonical)``).
- The ``scout`` / ``tester`` roles remain canonical and load real (non-stub)
  content.
"""

import re
from pathlib import Path

from src.core.orchestration.agent_brain import (
    AgentBrainManager,
    _parse_front_matter,
)
from src.core.orchestration.role_config import CANONICAL_ROLES, normalize_role


_ROLES_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "config" / "agent-brain" / "roles"
)

#: Defunct broadcast-topic pattern the stub roles used to instruct agents to publish to.
_BROADCAST_RE = re.compile(r"agent\.[a-z_]+\.broadcast")


def _role_brain_files():
    return sorted(p.name for p in _ROLES_DIR.glob("*.md"))


class TestNoDefunctBroadcastTopics:
    """MC-5: role brain files must not reference legacy P2P broadcast topics."""

    def test_no_role_file_references_broadcast_topic(self):
        bad = []
        for f in _ROLES_DIR.glob("*.md"):
            content = f.read_text(encoding="utf-8")
            if _BROADCAST_RE.search(content):
                bad.append(f.name)
        assert bad == [], (
            f"Role brain files reference defunct broadcast topics: {bad}. "
            "Remove the legacy 'Publish to agent.<role>.broadcast' instruction "
            "(subagents report via their returned result, not a publish topic)."
        )

    def test_no_typed_agent_broadcast_event_mappings(self):
        from src.core.orchestration.event_bus import _get_event_name_map

        event_name_map, _ = _get_event_name_map()
        names = set(event_name_map.keys())
        stub_references = {m for f in _ROLES_DIR.glob("*.md") for m in _BROADCAST_RE.findall(f.read_text(encoding="utf-8"))}
        overlapping = stub_references & names
        assert overlapping == set(), (
            f"Defunct broadcast events should not be typed-mapped: {overlapping}"
        )


class TestResearcherIsAlias:
    """researcher must resolve to analyst, never live on disk as its own role."""

    def test_researcher_not_a_brain_role_file(self):
        assert "researcher.md" not in _role_brain_files(), (
            "researcher.md was a defunct stub — 'researcher' canonicalizes to "
            "'analyst', so the file was never compiled into a subagent prompt."
        )

    def test_researcher_canonicalizes_to_analyst(self):
        assert normalize_role("researcher") == "analyst"

    def test_analyst_brain_file_exists(self):
        assert "analyst.md" in _role_brain_files()


class TestScoutTesterRolesFunctional:
    """scout/tester are canonical roles with real, non-stub brain content."""

    def test_scout_is_canonical_role(self):
        assert "scout" in CANONICAL_ROLES

    def test_tester_is_canonical_role(self):
        assert "tester" in CANONICAL_ROLES

    def test_scout_brain_file_has_substance(self):
        manager = AgentBrainManager()
        content = manager.get_role("scout")
        assert len(content) > 200, "scout role content is still a stub"
        assert "Scout" in content

    def test_tester_brain_file_has_substance(self):
        manager = AgentBrainManager()
        content = manager.get_role("tester")
        assert len(content) > 200, "tester role content is still a stub"
        assert "Tester" in content

    def test_role_files_have_valid_front_matter(self):
        for name in ("scout.md", "tester.md"):
            content = (_ROLES_DIR / name).read_text(encoding="utf-8")
            fm = _parse_front_matter(content)
            assert fm is not None, f"{name} is missing front-matter"
            assert fm.get("name"), f"{name} front-matter missing 'name'"
