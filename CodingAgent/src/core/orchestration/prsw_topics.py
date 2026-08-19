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
    # MED-12 fix: each constant uses a unique topic string so subscribers can
    # distinguish message types without decoding payload fields.  The old design
    # shared a single string per agent role, making targeted subscription
    # impossible and silently delivering unrelated messages to every listener.
    FILES_DISCOVERED = "agent.scout.files_discovered"
    FILE_ANALYSIS = "agent.scout.file_analysis"
    DOC_SUMMARY = "agent.researcher.doc_summary"
    API_USAGE = "agent.researcher.api_usage"
    BUG_FOUND = "agent.reviewer.bug_found"
    CODE_QUALITY = "agent.reviewer.code_quality"
    TEST_RESULT = "agent.tester.test_result"
    COVERAGE_UPDATE = "agent.tester.coverage_update"
    STATUS_UPDATE = "agent.status_update"
    ERROR_REPORT = "agent.error_report"
    RESOURCE_NEEDED = "agent.resource_needed"
    # Phase B: Delegation results for P2P cross-agent context
    RESEARCHER_RESULT = "agent.researcher.result"
    CODER_RESULT = "agent.coder.result"
    REVIEWER_RESULT = "agent.reviewer.result"
    TESTER_RESULT = "agent.tester.result"
    ANALYST_RESULT = "agent.analyst.result"
    SCOUT_RESULT = "agent.scout.result"
