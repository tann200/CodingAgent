"""
Topics for Parallel Reads, Sequential Writes coordination.
"""

import logging

logger = logging.getLogger(__name__)


class PRSWTopics:
    FILES_READY = "prsw.files.ready"
    CONTEXT_GATHERED = "prsw.context"
    CHANGES_APPLIED = "prsw.changes"
    NEW_FILES = "prsw.new_files"
    BLOCKED_ON_WRITE = "prsw.blocked"
    WRITE_COMPLETE = "prsw.write_done"


class AgentTopics:
    FILES_DISCOVERED = "agent.scout.broadcast"
    FILE_ANALYSIS = "agent.scout.broadcast"
    DOC_SUMMARY = "agent.researcher.broadcast"
    API_USAGE = "agent.researcher.broadcast"
    BUG_FOUND = "agent.reviewer.broadcast"
    CODE_QUALITY = "agent.reviewer.broadcast"
    TEST_RESULT = "agent.tester.broadcast"
    COVERAGE_UPDATE = "agent.tester.broadcast"
    STATUS_UPDATE = "agent.broadcast"
    ERROR_REPORT = "agent.broadcast"
    RESOURCE_NEEDED = "agent.broadcast"
    # Phase B: Delegation results for P2P cross-agent context
    RESEARCHER_RESULT = "agent.researcher.result"
    CODER_RESULT = "agent.coder.result"
    REVIEWER_RESULT = "agent.reviewer.result"
    TESTER_RESULT = "agent.tester.result"
    ANALYST_RESULT = "agent.analyst.result"
    SCOUT_RESULT = "agent.scout.result"
