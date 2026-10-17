"""Unit tests for evidence-based completion contracts and minimum tier verification (audit Phase 1 Item 1)."""

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.orchestration.graph.completion_contract import (
    TaskType,
    classify_task,
    evaluate_completion_contract,
)
from src.core.orchestration.graph.nodes.frontier_loop_node import frontier_loop_node
from src.core.orchestration.graph.nodes.verification_node import verification_node
from src.core.orchestration.graph.nodes.evaluation_node import evaluation_node
from src.core.orchestration.graph.tier_graph_routing import route_frontier_loop_exit_lite


class TestTaskClassification:
    def test_classify_mutation_task_by_file_keywords(self):
        assert classify_task("Create a file hello.py with hello function") == TaskType.MUTATING
        assert classify_task("edit calc.py to return a + b") == TaskType.MUTATING
        assert classify_task("fix the bug in test_auth.py") == TaskType.MUTATING
        assert classify_task("refactor database connection in db.py") == TaskType.MUTATING
        assert classify_task("delete obsolete test file test_old.py") == TaskType.MUTATING
        assert classify_task("add a new test case for login") == TaskType.MUTATING

    def test_classify_mutation_by_plan(self):
        plan = [{"action": "write_file", "description": "Write new implementation"}]
        assert classify_task("do the thing", current_plan=plan) == TaskType.MUTATING

    def test_classify_mutation_by_affected_files(self):
        assert classify_task("do something", affected_files=["src/main.py"]) == TaskType.MUTATING

    def test_classify_read_only_task(self):
        assert classify_task("find where User class is defined") == TaskType.READ_ONLY
        assert classify_task("search for get_user in src/") == TaskType.READ_ONLY
        assert classify_task("list all files in the project") == TaskType.READ_ONLY
        assert classify_task("inspect the error log") == TaskType.READ_ONLY

    def test_classify_informational_task(self):
        assert classify_task("explain how authentication works") == TaskType.INFORMATIONAL
        assert classify_task("what is the purpose of this repository?") == TaskType.INFORMATIONAL
        assert classify_task("say hi") == TaskType.INFORMATIONAL


class TestCompletionContractEvaluation:
    def test_empty_response_without_tools_is_rejected(self):
        verdict = evaluate_completion_contract(
            task="explain the architecture",
            content_text="",
            tool_calls_count=0,
        )
        assert verdict.is_complete is False
        assert verdict.error_code == "empty_response"

    def test_whitespace_response_without_tools_is_rejected(self):
        verdict = evaluate_completion_contract(
            task="say hi",
            content_text="   \n\t  ",
            tool_calls_count=0,
        )
        assert verdict.is_complete is False
        assert verdict.error_code == "empty_response"

    def test_mutating_task_with_text_only_response_is_rejected(self):
        verdict = evaluate_completion_contract(
            task="edit calc.py to fix add function",
            content_text="I have edited calc.py and the bug is now fixed.",
            tool_calls_count=0,
            modified_files=[],
        )
        assert verdict.is_complete is False
        assert verdict.task_type == TaskType.MUTATING
        assert verdict.error_code == "unverified_text_only_completion"
        assert "no mutating tools were executed" in verdict.failure_reason

    def test_mutating_task_with_tools_but_no_modified_files_is_rejected(self):
        verdict = evaluate_completion_contract(
            task="create file new_file.txt with content test",
            content_text="Done creating the file.",
            tool_calls_count=1,
            modified_files=[],
            last_result={"status": "error", "error": "Disk full"},
        )
        assert verdict.is_complete is False
        assert verdict.task_type == TaskType.MUTATING

    def test_mutating_task_with_modified_files_is_accepted(self):
        verdict = evaluate_completion_contract(
            task="edit calc.py to return a + b",
            content_text="I have updated calc.py.",
            tool_calls_count=1,
            modified_files=["/workspace/calc.py"],
            last_result={"ok": True, "status": "ok"},
        )
        assert verdict.is_complete is True
        assert verdict.task_type == TaskType.MUTATING
        assert verdict.evidence["modified_files"] == ["/workspace/calc.py"]

    def test_informational_task_with_text_response_is_accepted(self):
        verdict = evaluate_completion_contract(
            task="explain how the router works",
            content_text="The router maps URL patterns to handler functions.",
            tool_calls_count=0,
        )
        assert verdict.is_complete is True
        assert verdict.task_type == TaskType.INFORMATIONAL

    def test_read_only_task_with_inspection_is_accepted(self):
        verdict = evaluate_completion_contract(
            task="find where Router is defined",
            content_text="Router is defined in src/router.py at line 15.",
            tool_calls_count=1,
        )
        assert verdict.is_complete is True
        assert verdict.task_type == TaskType.READ_ONLY


class TestFrontierLoopCompletionContract:
    @pytest.mark.asyncio
    async def test_frontier_loop_rejects_text_only_mutation_attempt(self):
        orch = SimpleNamespace(
            working_dir=None,
            llm_client=None,
            tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
            event_bus=None,
            _session_modified_files=set(),
        )
        state = {
            "task": "edit calc.py to add multiply function",
            "history": [],
            "tool_call_count": 0,
            "max_tool_calls": 5,
            "model_tier": "frontier",
        }

        # Model returns text saying it finished, but NEVER called a tool
        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(return_value={"choices": [{"message": {"content": "I finished editing calc.py"}}]}),
            ),
        ):
            result = await frontier_loop_node(state, config={})

        assert result["last_result"]["ok"] is False
        assert result["last_result"]["_completion_detected"] is False
        assert "unverified_text_only_completion" in result["errors"]

    @pytest.mark.asyncio
    async def test_frontier_loop_rejects_empty_model_response(self):
        orch = SimpleNamespace(
            working_dir=None,
            llm_client=None,
            tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
            event_bus=None,
            _session_modified_files=set(),
        )
        state = {
            "task": "say hi",
            "history": [],
            "tool_call_count": 0,
            "max_tool_calls": 5,
            "model_tier": "frontier",
        }

        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(return_value={"choices": [{"message": {"content": ""}}]}),
            ),
        ):
            result = await frontier_loop_node(state, config={})

        assert result["last_result"]["ok"] is False
        assert result["last_result"]["_completion_detected"] is False
        assert "empty_response" in result["errors"]

    @pytest.mark.asyncio
    async def test_frontier_loop_accepts_informational_text_completion(self):
        orch = SimpleNamespace(
            working_dir=None,
            llm_client=None,
            tool_registry=SimpleNamespace(get_openai_functions=lambda: []),
            event_bus=None,
            _session_modified_files=set(),
        )
        state = {
            "task": "say hi",
            "history": [],
            "tool_call_count": 0,
            "max_tool_calls": 5,
            "model_tier": "frontier",
        }

        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(return_value={"choices": [{"message": {"content": "Hello! How can I help?"}}]}),
            ),
        ):
            result = await frontier_loop_node(state, config={})

        assert result["last_result"]["ok"] is True
        assert result["last_result"]["completed_without_tool"] is True
        assert result["last_result"]["_completion_detected"] is True


class TestVerificationNodeMinimumTierParity:
    @pytest.mark.asyncio
    async def test_small_tier_does_not_skip_verification_if_files_modified(self, tmp_path):
        test_py = tmp_path / "mod.py"
        test_py.write_text("def ok():\n    return 42\n", encoding="utf-8")

        orch = SimpleNamespace(
            working_dir=str(tmp_path),
            _session_modified_files={str(test_py)},
        )
        state = {
            "model_tier": "small",
            "last_tool_name": "read_file",  # last tool was read, BUT session had file writes!
            "modified_files": [str(test_py)],
            "working_dir": str(tmp_path),
            "last_result": {"status": "ok", "path": str(test_py)},
        }

        with (
            patch("src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator", return_value=orch),
            patch("src.tools.verification_tools.run_tests", return_value={"status": "ok"}),
            patch("src.tools.verification_tools.run_linter", return_value={"status": "ok"}),
        ):
            res = await verification_node(state, config={})

        # Verification must NOT be skipped as 'skipped_small_readonly'
        assert res["verification_result"].get("status") != "skipped_small_readonly"
        assert res["verification_passed"] is True

    @pytest.mark.asyncio
    async def test_verification_fails_when_command_exits_nonzero(self, tmp_path):
        orch = SimpleNamespace(
            working_dir=str(tmp_path),
            _session_modified_files=set(),
        )
        state = {
            "model_tier": "small",
            "last_tool_name": "bash",
            "working_dir": str(tmp_path),
            "last_result": {
                "ok": False,
                "result": {"returncode": 1, "stderr": "command not found: foo"},
            },
        }

        with patch("src.core.orchestration.graph.nodes.verification_node._resolve_orchestrator", return_value=orch):
            res = await verification_node(state, config={})

        assert res["verification_passed"] is False
        assert res["verification_result"]["command"]["status"] == "fail"


class TestTierGraphRoutingMinimumVerification:
    def test_lite_routes_to_verification_if_files_modified(self):
        state = {
            "last_tool_name": "read_file",
            "modified_files": ["/workspace/test.py"],
        }
        assert route_frontier_loop_exit_lite(state) == "verification"

    def test_lite_routes_to_verification_if_last_result_failed(self):
        state = {
            "last_tool_name": "bash",
            "last_result": {"ok": False, "status": "error"},
        }
        assert route_frontier_loop_exit_lite(state) == "verification"

    def test_lite_routes_to_memory_sync_if_purely_readonly(self):
        state = {
            "last_tool_name": "read_file",
            "modified_files": [],
            "last_result": {"ok": True, "status": "ok"},
        }
        assert route_frontier_loop_exit_lite(state) == "memory_sync"


class TestEvaluationNodeEvidenceCheck:
    @pytest.mark.asyncio
    async def test_evaluation_node_fails_when_mutating_task_has_no_modifications(self):
        state = {
            "task": "edit calc.py to add divide function",
            "history": [
                {"role": "user", "content": "edit calc.py to add divide function"},
                {"role": "assistant", "content": "I have updated calc.py."},
            ],
            "current_plan": [],
            "tool_call_count": 0,
            "modified_files": [],
            "errors": [],
            "verification_passed": True,
            "verification_result": {},
        }

        res = await evaluation_node(state, config={})
        assert res["evaluation_result"] != "complete"


class TestSpec04RegressionContracts:
    def test_mutating_task_with_readonly_tool_and_ok_status_is_rejected(self):
        verdict = evaluate_completion_contract(
            task="Fix the bug in app.py",
            content_text="I inspected app.py and fixed the bug.",
            tool_calls_count=1,
            last_tool_name="read_file",
            last_result={"ok": True, "status": "ok"},
            modified_files=[],
        )
        assert verdict.is_complete is False
        assert verdict.task_type == TaskType.MUTATING
        assert verdict.error_code == "unverified_mutation_missing"

    @pytest.mark.asyncio
    async def test_frontier_loop_node_failure_in_earlier_turn_is_not_lost(self, tmp_path):
        orch = SimpleNamespace(
            working_dir=str(tmp_path),
            _session_modified_files={str(tmp_path / "a.py")},
            msg_mgr=SimpleNamespace(messages=[]),
            plan_mode=None,
            _current_task_id="t-1",
            _tool_executor=None,
        )
        state = {
            "task": "Fix the bug in a.py",
            "history": [],
            "tool_call_count": 2,
            "max_tool_calls": 10,
            "model_tier": "frontier",
            "working_dir": str(tmp_path),
            "errors": [],
        }

        # Model emits no tool calls on turn 1
        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(return_value={"choices": [{"message": {"content": "All done!"}}]}),
            ),
        ):
            # Pass last_result as an error from an earlier turn
            state_with_failed_prior_turn = {
                **state,
                "last_result": {"ok": False, "error": "pytest failed with exit code 1"},
            }
            result = await frontier_loop_node(state_with_failed_prior_turn, config={})

        assert result["last_result"]["ok"] is False
        assert result["last_result"]["error_code"] == "mutation_failed"

    def test_informational_task_with_repo_context_classified_as_informational(self):
        user_task = "How does login work?"
        assert classify_task(user_task) == TaskType.INFORMATIONAL

    def test_lite_routing_with_write_then_read_routes_to_verification(self):
        state = {
            "last_tool_name": "read_file",
            "modified_files": ["/workspace/a.py"],
            "last_result": {"ok": True, "status": "ok"},
        }
        assert route_frontier_loop_exit_lite(state) == "verification"

    def test_route_verification_lite(self):
        from src.core.orchestration.graph.tier_graph_routing import route_verification_lite

        # Passes -> memory_sync
        assert route_verification_lite({"verification_passed": True}) == "memory_sync"
        # Fails and debug_attempts < max -> frontier_loop
        assert route_verification_lite({"verification_passed": False, "debug_attempts": 0, "max_debug_attempts": 1}) == "frontier_loop"
        # Fails and attempts exhausted -> memory_sync
        assert route_verification_lite({"verification_passed": False, "debug_attempts": 1, "max_debug_attempts": 1}) == "memory_sync"

    @pytest.mark.asyncio
    async def test_informational_task_with_analyst_findings_in_frontier_loop_node(self, tmp_path):
        orch = SimpleNamespace(
            working_dir=str(tmp_path),
            _session_modified_files=set(),
            msg_mgr=SimpleNamespace(messages=[]),
            plan_mode=None,
            _current_task_id="t-2",
            _tool_executor=None,
        )
        state = {
            "task": "How does login work?",
            "analyst_findings": "You need to edit auth.py to fix the bug in update_login",
            "history": [],
            "tool_call_count": 0,
            "max_tool_calls": 10,
            "model_tier": "frontier",
            "working_dir": str(tmp_path),
            "errors": [],
        }

        with (
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node._resolve_orchestrator",
                return_value=orch,
            ),
            patch(
                "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
                new=AsyncMock(return_value={"choices": [{"message": {"content": "Login works via OAuth."}}]}),
            ),
        ):
            result = await frontier_loop_node(state, config={})

        assert result["last_result"]["ok"] is True
        assert result["last_result"]["task_type"] == TaskType.INFORMATIONAL.value
