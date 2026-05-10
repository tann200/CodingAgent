from src.tools.subagent_payloads import (
    build_graph_state_base,
    build_delegate_result_text,
    build_child_session_file_path,
    build_subagent_initial_state,
    build_subagent_manifest,
    build_subagent_roles_payload,
    build_subagent_session_payload,
    canonicalize_subagent_role,
    compute_effective_tool_policy,
    extract_child_session_messages,
    select_dispatch_result_content,
)


def test_canonicalize_subagent_role_maps_legacy_aliases():
    assert canonicalize_subagent_role("researcher") == "analyst"
    assert canonicalize_subagent_role("coder") == "operational"
    assert canonicalize_subagent_role("planner") == "strategic"
    assert canonicalize_subagent_role("analyst") == "analyst"


def test_compute_effective_tool_policy_strips_delegate_tools_from_explicit_allowlist():
    allowed, denied = compute_effective_tool_policy(
        explicit_allowed_tools=["read_file", "delegate_task", "delegate_task_async"],
        registry_allowed_tools=None,
        registry_denied_tools={"bash"},
    )
    assert allowed == {"read_file"}
    assert "delegate_task" in denied
    assert "delegate_task_async" in denied
    assert "bash" in denied


def test_compute_effective_tool_policy_uses_registry_when_explicit_allowlist_missing():
    allowed, denied = compute_effective_tool_policy(
        explicit_allowed_tools=None,
        registry_allowed_tools={"read_file", "delegate_task"},
        registry_denied_tools=None,
    )
    assert allowed == {"read_file"}
    assert denied == {"delegate_task", "delegate_task_async"}


def test_build_subagent_manifest_sets_running_status():
    payload = build_subagent_manifest(
        child_session_id="child1",
        parent_session_id="parent1",
        canonical_role="analyst",
        task="inspect repo",
        working_dir="/tmp/work",
        spawned_at=123.0,
    )
    assert payload["status"] == "running"
    assert payload["role"] == "analyst"
    assert payload["working_dir"] == "/tmp/work"


def test_build_subagent_session_payload_supports_success_and_failure():
    success = build_subagent_session_payload(
        child_session_id="child1",
        parent_session_id="parent1",
        task_name="inspect repo",
        canonical_role="analyst",
        working_dir="/tmp/work",
        timestamp=123.0,
        messages=[{"role": "assistant", "content": "done"}],
        ok=True,
    )
    failure = build_subagent_session_payload(
        child_session_id="child2",
        parent_session_id="parent1",
        task_name="inspect repo",
        canonical_role="debugger",
        working_dir="/tmp/work",
        timestamp=456.0,
        messages=[],
        ok=False,
        error="boom",
    )
    assert success["message_count"] == 1
    assert success["ok"] is True
    assert failure["ok"] is False
    assert failure["error"] == "boom"


def test_build_subagent_roles_payload_exposes_aliases():
    payload = build_subagent_roles_payload()
    assert payload["status"] == "ok"
    assert "researcher" in payload["available_roles"]["analyst"]["aliases"]
    assert "coder" in payload["available_roles"]["operational"]["aliases"]


def test_build_graph_state_base_populates_shared_defaults():
    state = build_graph_state_base(
        task="inspect repo",
        session_id="child1",
        working_dir="/tmp/work",
        system_prompt="sys",
        history=[{"role": "user", "content": "hi"}],
        verified_reads=["a.py"],
        parent_session_id="parent1",
        delegation_depth=2,
        override_model="gpt-4o-mini",
        current_role="analyst",
    )

    assert state["task"] == "inspect repo"
    assert state["session_id"] == "child1"
    assert state["history"] == [{"role": "user", "content": "hi"}]
    assert state["verified_reads"] == ["a.py"]
    assert state["delegation_depth"] == 2
    assert state["override_model"] == "gpt-4o-mini"
    assert state["current_role"] == "analyst"
    assert state["next_action"] is None
    assert state["delegations"] == []


def test_build_subagent_initial_state_merges_resumed_fields_without_reusing_identity():
    resumed = {
        "history": [{"role": "user", "content": "hi"}],
        "current_plan": ["step1"],
        "current_step": 2,
        "affected_files": ["a.py"],
        "plan_mode_approved": True,
    }
    state = build_subagent_initial_state(
        subtask_description="new task",
        child_session_id="child1",
        working_dir="/tmp/work",
        system_prompt="sys",
        current_role="analyst",
        parent_session_id="parent1",
        delegation_depth=2,
        override_model="gpt-4o-mini",
        resumed_state=resumed,
    )
    assert state["task"] == "new task"
    assert state["session_id"] == "child1"
    assert state["delegation_depth"] == 2
    assert state["history"] == resumed["history"]
    assert state["history"] is not resumed["history"]
    assert state["current_plan"] == ["step1"]
    assert state["affected_files"] == ["a.py"]


def test_build_delegate_result_text_formats_successful_result():
    text = build_delegate_result_text(
        role="analyst",
        child_session_id="child1",
        final_state={
            "task": "inspect repo",
            "history": [{"role": "assistant", "content": "done"}],
            "errors": [],
            "last_result": {"status": "ok", "file": "a.py"},
        },
    )
    assert "## Subagent [analyst] Execution Complete" in text
    assert "**Summary:** done" in text
    assert "**Status:** ok" in text
    assert "**File:** a.py" in text
    assert "**child_session_id:** child1" in text


def test_build_delegate_result_text_formats_error_summary_and_unexpected_type():
    text = build_delegate_result_text(
        role="debugger",
        child_session_id="child2",
        final_state={"errors": ["boom", "bad"]},
    )
    unexpected = build_delegate_result_text(
        role="debugger",
        child_session_id="child3",
        final_state=["not", "a", "dict"],
    )
    assert "completed with errors" in text
    assert "boom" in text
    assert "unexpected result type" in unexpected


def test_extract_child_session_messages_prefers_history_then_message_objects():
    class _Msg:
        def __init__(self, type_, content):
            self.type = type_
            self.content = content

    history_first = extract_child_session_messages(
        {"history": [{"role": "assistant", "content": "done"}]}
    )
    fallback = extract_child_session_messages(
        {"messages": [_Msg("assistant", "done")]}
    )
    assert history_first == [{"role": "assistant", "content": "done"}]
    assert fallback == [{"role": "assistant", "content": "done"}]


def test_build_child_session_file_path_uses_expected_filename(tmp_path):
    path = build_child_session_file_path(str(tmp_path), "child123")
    assert path.endswith("session_child123.json")


def test_select_dispatch_result_content_prefers_work_summary_then_assistant_history():
    assert select_dispatch_result_content(
        {
            "work_summary": "final summary",
            "history": [{"role": "assistant", "content": "assistant fallback"}],
        }
    ) == "final summary"
    assert select_dispatch_result_content(
        {
            "history": [
                {"role": "user", "content": "u"},
                {"role": "assistant", "content": "assistant fallback"},
            ]
        }
    ) == "assistant fallback"
    assert select_dispatch_result_content({"history": []}) == ""
    assert select_dispatch_result_content(None) == ""
