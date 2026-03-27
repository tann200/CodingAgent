"""
Unit tests for PRSW events - Phase 6: PRSW
"""

import pytest
from src.core.orchestration.event_bus import EventBus
from src.core.orchestration.prsw_topics import PRSWTopics, AgentTopics


class TestPRSWTopics:
    """Tests for PRSW topic constants."""

    def test_prsw_topics_exist(self):
        """Test that all PRSW topics are defined."""
        assert PRSWTopics.FILES_READY == "prsw.files.ready"
        assert PRSWTopics.CONTEXT_GATHERED == "prsw.context"
        assert PRSWTopics.CHANGES_APPLIED == "prsw.changes"
        assert PRSWTopics.NEW_FILES == "prsw.new_files"
        assert PRSWTopics.BLOCKED_ON_WRITE == "prsw.blocked"
        assert PRSWTopics.WRITE_COMPLETE == "prsw.write_done"

    def test_agent_topics_exist(self):
        """Test that all agent topics are defined."""
        assert AgentTopics.FILES_DISCOVERED == "agent.scout.broadcast"
        assert AgentTopics.DOC_SUMMARY == "agent.researcher.broadcast"
        assert AgentTopics.BUG_FOUND == "agent.reviewer.broadcast"
        assert AgentTopics.TEST_RESULT == "agent.tester.broadcast"


class TestPRSWEvents:
    """Tests for PRSW event publishing."""

    def test_files_ready_event(self):
        """Test FILES_READY event publishing."""
        eb = EventBus()
        received = []

        eb.subscribe(PRSWTopics.FILES_READY, lambda msg: received.append(msg))

        eb.publish(PRSWTopics.FILES_READY, {"files": ["a.py", "b.py"]})

        assert len(received) == 1
        assert received[0]["files"] == ["a.py", "b.py"]

    def test_write_complete_event(self):
        """Test WRITE_COMPLETE event publishing."""
        eb = EventBus()
        received = []

        eb.subscribe(PRSWTopics.WRITE_COMPLETE, lambda msg: received.append(msg))

        eb.publish(
            PRSWTopics.WRITE_COMPLETE,
            {"files": ["main.py"], "status": "completed"},
        )

        assert len(received) == 1

    def test_agent_broadcast_events(self):
        """Test agent broadcast events."""
        eb = EventBus()
        received = []

        eb.subscribe(AgentTopics.FILES_DISCOVERED, lambda msg: received.append(msg))

        eb.publish(
            AgentTopics.FILES_DISCOVERED,
            {"files": ["test.py"], "agent_id": "scout_1"},
        )

        assert len(received) == 1
        assert received[0]["files"] == ["test.py"]

    def test_multiple_subscribers(self):
        """Test that multiple subscribers can receive the same event."""
        eb = EventBus()
        received1 = []
        received2 = []

        eb.subscribe(PRSWTopics.WRITE_COMPLETE, lambda msg: received1.append(msg))
        eb.subscribe(PRSWTopics.WRITE_COMPLETE, lambda msg: received2.append(msg))

        eb.publish(PRSWTopics.WRITE_COMPLETE, {"status": "ok"})

        assert len(received1) == 1
        assert len(received2) == 1


class TestPRSWBuilder:
    """Tests for PRSW routing functions."""

    def test_should_use_prsw_with_multiple_delegations(self):
        """Test should_use_prsw returns True for mixed read/write delegations."""
        from src.core.orchestration.graph.builder import should_use_prsw

        state = {
            "delegations": [
                {"role": "scout", "task": "find files"},
                {"role": "coder", "task": "write code"},
            ]
        }

        assert should_use_prsw(state) is True

    def test_should_use_prsw_single_delegation(self):
        """Test should_use_prsw returns False for single delegation."""
        from src.core.orchestration.graph.builder import should_use_prsw

        state = {
            "delegations": [
                {"role": "scout", "task": "find files"},
            ]
        }

        assert should_use_prsw(state) is False

    def test_should_use_prsw_no_delegations(self):
        """Test should_use_prsw returns False for no delegations."""
        from src.core.orchestration.graph.builder import should_use_prsw

        state = {"delegations": []}

        assert should_use_prsw(state) is False

    def test_should_use_prsw_read_only(self):
        """Test should_use_prsw returns False for read-only delegations."""
        from src.core.orchestration.graph.builder import should_use_prsw

        state = {
            "delegations": [
                {"role": "scout", "task": "find files"},
                {"role": "researcher", "task": "analyze docs"},
            ]
        }

        assert should_use_prsw(state) is False

    def test_should_use_prsw_write_only(self):
        """Test should_use_prsw returns False for write-only delegations."""
        from src.core.orchestration.graph.builder import should_use_prsw

        state = {
            "delegations": [
                {"role": "coder", "task": "write code"},
                {"role": "tester", "task": "write tests"},
            ]
        }

        assert should_use_prsw(state) is False
