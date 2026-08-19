from src.core.orchestration.graph.nodes.execution_helpers import (
    build_no_action_result,
    build_execution_return_payload,
    build_read_then_write_result,
    build_tool_history_messages,
    compute_affected_files_update,
    compute_execution_post_tool_updates,
    compute_no_plan_fail_update,
    compute_plan_approval_consumed,
    compute_plan_exit_update,
    compute_plan_progress_payload,
    compute_plan_step_updates,
    compute_replan_trigger,
    emit_execution_step_finish,
    emit_execution_step_start,
    emit_plan_progress_and_sync_todo,
    extract_tool_call_from_response,
    dispatch_execution_tool,
    handle_execution_preflight_and_role_gate,
    handle_read_then_write_success,
    increment_step_retry_count,
    log_no_action_outcome,
    log_plan_step_execution,
    log_plan_and_wave_advancement,
    log_wave_execution_start,
    maybe_build_execution_cancellation_result,
    maybe_begin_step_transaction,
    maybe_build_preview_result,
    resolve_execution_orchestrator,
    schedule_async_post_tool_hook,
    select_execution_action,
    sync_execution_state_to_orchestrator,
    sync_tool_result_to_ui,
    update_tool_tracking,
)


def test_extract_tool_call_from_response_prefers_native_function_call():
    response = {
        "choices": [
            {
                "message": {
                    "content": "ignored",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "read",
                                "arguments": '{"path": "a.txt"}',
                            }
                        }
                    ],
                }
            }
        ]
    }

    content, tool_calls, tool_call = extract_tool_call_from_response(
        response,
        parse_tool_block=lambda _content: None,
    )

    assert content == "ignored"
    assert isinstance(tool_calls, list)
    assert tool_call == {"name": "read", "arguments": {"path": "a.txt"}}


def test_extract_tool_call_from_response_falls_back_to_parser():
    response = {"message": {"content": "yaml block"}}

    _content, _tool_calls, tool_call = extract_tool_call_from_response(
        response,
        parse_tool_block=lambda content: {"name": "write", "arguments": {"raw": content}},
    )

    assert tool_call == {"name": "write", "arguments": {"raw": "yaml block"}}


def test_build_no_action_result_returns_empty_model_response_error():
    result = build_no_action_result(
        content="   ",
        action=None,
        state={"step_retry_counts": {}},
        current_plan=None,
        current_step=2,
        original_task="task",
        inc_step_retry=lambda state, step: {step: 1},
    )

    assert result["last_result"]["ok"] is False
    assert "empty_model_response" in result["last_result"]["error"]
    assert result["step_retry_counts"] == {2: 1}


def test_build_no_action_result_marks_status_complete_step_done():
    current_plan = [{"description": "one"}, {"description": "two"}]

    result = build_no_action_result(
        content="STATUS: complete",
        action=None,
        state={},
        current_plan=current_plan,
        current_step=0,
        original_task="original",
        inc_step_retry=lambda state, step: {step: 1},
    )

    assert result["last_result"]["completed_without_tool"] is True
    assert result["current_step"] == 1
    assert result["current_plan"][0]["completed"] is True
    assert result["task"] == "two"


def test_build_no_action_result_returns_format_error_when_no_tool_call():
    result = build_no_action_result(
        content="I could not decide",
        action=None,
        state={"step_retry_counts": {"1": 2}},
        current_plan=None,
        current_step=1,
        original_task=None,
        inc_step_retry=lambda state, step: {1: 3},
    )

    assert result == {
        "last_result": {"ok": False, "error": "format_error: no tool call emitted"},
        "step_retry_counts": {1: 3},
    }


def test_increment_step_retry_count_normalizes_mixed_keys():
    result = increment_step_retry_count(
        {"step_retry_counts": {"1": "2", 3: 4, "bad": "value"}},
        1,
    )

    assert result == {"1": 3, "3": 4}


def test_resolve_execution_orchestrator_returns_orchestrator_when_found():
    logger = type("L", (), {"info": lambda *a, **k: None, "error": lambda *a, **k: None})()
    orchestrator = object()

    result, error = resolve_execution_orchestrator(
        state={},
        config={},
        resolve_orchestrator_fn=lambda state, config: orchestrator,
        logger=logger,
    )

    assert result is orchestrator
    assert error is None


def test_resolve_execution_orchestrator_allows_explicit_subagent_none():
    logged = []
    logger = type(
        "L",
        (),
        {
            "info": lambda *a, **k: logged.append(("info", a[1] if len(a) > 1 else a[0])),
            "error": lambda *a, **k: logged.append(("error", a[1] if len(a) > 1 else a[0])),
        },
    )()

    result, error = resolve_execution_orchestrator(
        state={},
        config={"configurable": {"orchestrator": None}},
        resolve_orchestrator_fn=lambda state, config: None,
        logger=logger,
    )

    assert result is None
    assert error is None
    assert ("info", "execution_node: subagent mode (orchestrator=None in config)") in logged


def test_resolve_execution_orchestrator_returns_missing_orchestrator_error():
    logged = []
    logger = type(
        "L",
        (),
        {
            "info": lambda *a, **k: logged.append(("info", a[1] if len(a) > 1 else a[0])),
            "error": lambda *a, **k: logged.append(("error", a[1] if len(a) > 1 else a[0])),
        },
    )()

    result, error = resolve_execution_orchestrator(
        state={},
        config={},
        resolve_orchestrator_fn=lambda state, config: None,
        logger=logger,
    )

    assert result is None
    assert error == {"last_result": None, "errors": ["orchestrator not found"]}
    assert ("error", "execution_node: orchestrator is None") in logged


def test_resolve_execution_orchestrator_returns_config_error_on_exception():
    logged = []
    logger = type(
        "L",
        (),
        {
            "info": lambda *a, **k: logged.append(("info", a[1] if len(a) > 1 else a[0])),
            "error": lambda *a, **k: logged.append(("error", a[1] if len(a) > 1 else a[0])),
        },
    )()

    def _raise(state, config):
        raise RuntimeError("boom")

    result, error = resolve_execution_orchestrator(
        state={},
        config={},
        resolve_orchestrator_fn=_raise,
        logger=logger,
    )

    assert result is None
    assert error == {"last_result": None, "errors": ["config error: boom"]}
    assert logged and logged[0][0] == "error"


def test_maybe_build_execution_cancellation_result_prefers_state_event():
    logger_calls = []

    class _Event:
        def is_set(self):
            return True

    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: logger_calls.append(a[1] if len(a) > 1 else a[0])},
    )()

    result = maybe_build_execution_cancellation_result(
        state={"cancel_event": _Event()},
        orchestrator=type("Orch", (), {"cancel_event": None})(),
        logger=logger,
    )

    assert result == {
        "last_result": {"ok": False, "error": "Task canceled by user"},
        "errors": ["canceled"],
        "next_action": None,
    }
    assert logger_calls == ["execution_node: Task canceled by user"]


def test_maybe_build_execution_cancellation_result_falls_back_to_orchestrator_event():
    class _Event:
        def is_set(self):
            return True

    logger = type("L", (), {"info": lambda *a, **k: None})()

    result = maybe_build_execution_cancellation_result(
        state={},
        orchestrator=type("Orch", (), {"cancel_event": _Event()})(),
        logger=logger,
    )

    assert result["errors"] == ["canceled"]


def test_maybe_build_execution_cancellation_result_returns_none_when_not_canceled():
    class _Event:
        def is_set(self):
            return False

    logger = type("L", (), {"info": lambda *a, **k: None})()

    result = maybe_build_execution_cancellation_result(
        state={"cancel_event": _Event()},
        orchestrator=None,
        logger=logger,
    )

    assert result is None


def test_select_execution_action_prefers_planned_action():
    logger = type("L", (), {"error": lambda *a, **k: None})()

    result = select_execution_action(
        state={
            "planned_action": {"name": "read_file"},
            "next_action": {"name": "bash"},
        },
        logger=logger,
    )

    assert result == {"name": "read_file"}


def test_select_execution_action_logs_and_returns_none_on_state_error():
    logged = []

    class _BadState(dict):
        def get(self, key, default=None):
            raise RuntimeError("boom")

    logger = type(
        "L",
        (),
        {"error": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    result = select_execution_action(state=_BadState(), logger=logger)

    assert result is None
    assert logged == ["execution_node: failed to get next_action: %s"]


def test_maybe_begin_step_transaction_starts_when_supported():
    calls = []
    logger_calls = []

    orchestrator = type(
        "Orch",
        (),
        {"begin_step_transaction": lambda self: calls.append("started")},
    )()
    logger = type(
        "L",
        (),
        {"debug": lambda *a, **k: logger_calls.append(a[1] if len(a) > 1 else a[0])},
    )()

    maybe_begin_step_transaction(orchestrator=orchestrator, logger=logger)

    assert calls == ["started"]
    assert logger_calls == ["execution_node: step transaction started"]


def test_maybe_begin_step_transaction_logs_non_fatal_error():
    logger_calls = []

    class _Orch:
        def begin_step_transaction(self):
            raise RuntimeError("boom")

    logger = type(
        "L",
        (),
        {"debug": lambda *a, **k: logger_calls.append(a[1] if len(a) > 1 else a[0])},
    )()

    maybe_begin_step_transaction(orchestrator=_Orch(), logger=logger)

    assert logger_calls == [
        "execution_node: step transaction init failed (non-fatal): %s"
    ]


def test_log_wave_execution_start_logs_current_wave_size():
    logged = []
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    log_wave_execution_start(
        execution_waves=[[0, 1], [2]],
        current_wave=0,
        logger=logger,
    )

    assert logged == ["Wave execution: wave %d/%d with %d steps (sequential)"]


def test_log_plan_and_wave_advancement_logs_progress_and_completion():
    logged = []
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    log_plan_and_wave_advancement(
        plan_advance={"current_step": 1, "current_plan": [{}, {}]},
        wave_advance={"current_wave": 1},
        current_plan=[{"description": "one"}, {"description": "two"}],
        current_step=0,
        execution_waves=[[0], [1]],
        current_wave=0,
        logger=logger,
    )

    assert logged == [
        "Step %d complete, advancing to step %d",
        "Wave %d complete, advancing to wave %d",
    ]


def test_log_plan_and_wave_advancement_logs_terminal_completion():
    logged = []
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    log_plan_and_wave_advancement(
        plan_advance={"current_step": 2, "current_plan": [{}, {}]},
        wave_advance={"current_wave": 2},
        current_plan=[{"description": "one"}, {"description": "two"}],
        current_step=1,
        execution_waves=[[0], [1]],
        current_wave=1,
        logger=logger,
    )

    assert logged == ["All plan steps completed", "All waves completed"]


def test_log_plan_step_execution_logs_decomposed_step():
    logged = []
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    log_plan_step_execution(
        current_plan=[{"description": "step one"}, {"description": "step two"}],
        current_step=0,
        task_decomposed=True,
        original_task="do the task",
        logger=logger,
    )

    assert logged == ["Plan execution: step %d/%d - %s"]


def test_log_plan_step_execution_skips_non_decomposed_tasks():
    logged = []
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: logged.append(a)},
    )()

    log_plan_step_execution(
        current_plan=[{"description": "step one"}],
        current_step=0,
        task_decomposed=False,
        original_task="do the task",
        logger=logger,
    )

    assert logged == []


def test_sync_tool_result_to_ui_appends_serialized_payload():
    calls = []

    class _MsgMgr:
        def append(self, role, content):
            calls.append((role, content))

    logger = type("L", (), {"debug": lambda *a, **k: None})()

    sync_tool_result_to_ui(
        orchestrator=type("Orch", (), {"msg_mgr": _MsgMgr()})(),
        result={"ok": True},
        logger=logger,
    )

    assert calls == [("user", '{"tool_execution_result": {"ok": true}}')]


def test_sync_tool_result_to_ui_logs_non_fatal_error():
    logged = []

    class _MsgMgr:
        def append(self, role, content):
            raise RuntimeError("boom")

    logger = type(
        "L",
        (),
        {"debug": lambda *a, **k: logged.append(a[1] if len(a) > 1 else a[0])},
    )()

    sync_tool_result_to_ui(
        orchestrator=type("Orch", (), {"msg_mgr": _MsgMgr()})(),
        result={"ok": True},
        logger=logger,
    )

    assert logged == ["UI sync failed: %s"]


def test_log_no_action_outcome_warns_for_empty_content():
    warnings = []
    infos = []
    logger = type(
        "L",
        (),
        {
            "warning": lambda *a, **k: warnings.append(a[1] if len(a) > 1 else a[0]),
            "info": lambda *a, **k: infos.append(a[1] if len(a) > 1 else a[0]),
        },
    )()

    log_no_action_outcome(content="   ", logger=logger)

    assert warnings == [
        "execution_node: model produced empty content with no tool call — context window may be full or model failed to generate output"
    ]
    assert infos == []


def test_log_no_action_outcome_logs_status_complete_info():
    warnings = []
    infos = []
    logger = type(
        "L",
        (),
        {
            "warning": lambda *a, **k: warnings.append(a),
            "info": lambda *a, **k: infos.append(a[1] if len(a) > 1 else a[0]),
        },
    )()

    log_no_action_outcome(content="STATUS: complete", logger=logger)

    assert warnings == []
    assert infos == [
        "execution_node: model declared STATUS: complete with no tool call — treating as successful step completion"
    ]


def test_log_no_action_outcome_is_quiet_for_other_content():
    calls = []
    logger = type(
        "L",
        (),
        {
            "warning": lambda *a, **k: calls.append(("warning", a)),
            "info": lambda *a, **k: calls.append(("info", a)),
        },
    )()

    log_no_action_outcome(content="I could not decide", logger=logger)

    assert calls == []


def test_sync_execution_state_to_orchestrator_propagates_enforcement_fields():
    orchestrator = type("Orch", (), {})()

    sync_execution_state_to_orchestrator(
        state={"plan_mode_approved": True, "affected_files": ["src/a.py"]},
        orchestrator=orchestrator,
    )

    assert orchestrator._plan_mode_approved is True
    assert orchestrator._affected_files == ["src/a.py"]


def test_sync_execution_state_to_orchestrator_noops_without_orchestrator():
    sync_execution_state_to_orchestrator(
        state={"plan_mode_approved": True, "affected_files": ["src/a.py"]},
        orchestrator=None,
    )


def test_update_tool_tracking_records_cooldown_key_and_caps_entries():
    state = {
        "tool_last_used": {f"t{i}:": i for i in range(105)},
        "files_read": {"/tmp/a": True},
        "tool_call_count": 7,
    }

    tool_last_used, files_read, current_count = update_tool_tracking(
        state=state,
        tool_name="read_file",
        path_arg="file.txt",
    )

    assert current_count == 7
    assert files_read == {"/tmp/a": True}
    assert tool_last_used["read_file:file.txt"] == 7
    assert len(tool_last_used) == 100


def test_compute_plan_step_updates_advances_step_and_wave():
    current_plan = [
        {"description": "step 1", "completed": False},
        {"description": "step 2", "completed": False},
        {"description": "step 3", "completed": False},
    ]

    plan_advance, wave_advance = compute_plan_step_updates(
        result={"ok": True},
        current_plan=current_plan,
        current_step=0,
        original_task="task",
        execution_waves=[[0], [1, 2]],
        current_wave=0,
        step_retry_counts={},
    )

    assert plan_advance["current_step"] == 1
    assert plan_advance["current_plan"][0]["completed"] is True
    assert plan_advance["task"] == "step 2"
    assert wave_advance == {"current_wave": 1}


def test_compute_plan_step_updates_respects_retry_exhausted_wave_members():
    current_plan = [
        {"description": "step 1", "completed": False},
        {"description": "step 2", "completed": False},
    ]

    _plan_advance, wave_advance = compute_plan_step_updates(
        result={"status": "ok"},
        current_plan=current_plan,
        current_step=0,
        original_task="task",
        execution_waves=[[0, 1]],
        current_wave=0,
        step_retry_counts={"1": 3},
    )

    assert wave_advance == {"current_wave": 1}


def test_compute_no_plan_fail_update_ignores_format_errors_and_resets_on_success():
    assert compute_no_plan_fail_update(
        state={"current_plan": None, "no_plan_fail_count": 2},
        result={"error": "format_error: no tool call emitted", "ok": False},
    ) == {}

    assert compute_no_plan_fail_update(
        state={"current_plan": None, "no_plan_fail_count": 2},
        result={"ok": True},
    ) == {"no_plan_fail_count": 0}

    assert compute_no_plan_fail_update(
        state={"current_plan": None, "no_plan_fail_count": 2},
        result={"ok": False, "error": "boom"},
    ) == {"no_plan_fail_count": 3}


def test_compute_plan_approval_consumed_only_on_successful_modifying_tool():
    assert compute_plan_approval_consumed(
        state={"plan_mode_approved": True},
        tool_name="write_file",
        result={"ok": True},
        modifying_tools=("write_file",),
    ) == {"plan_mode_approved": False}

    assert compute_plan_approval_consumed(
        state={"plan_mode_approved": True},
        tool_name="read_file",
        result={"ok": True},
        modifying_tools=("write_file",),
    ) == {}


def test_compute_affected_files_update_expands_and_clears_scope():
    class _Orchestrator:
        _affected_files = ["src/base.py"]

    orchestrator = _Orchestrator()
    expanded = compute_affected_files_update(
        tool_name="ask_user",
        result={"status": "ok", "answer": "Also include src/extra.py"},
        state={"affected_files": ["src/base.py"]},
        orchestrator=orchestrator,
    )
    cleared = compute_affected_files_update(
        tool_name="ask_user",
        result={"status": "ok", "answer": "yes all"},
        state={"affected_files": ["src/base.py"]},
        orchestrator=orchestrator,
    )

    assert expanded == {"affected_files": ["src/base.py", "src/extra.py"]}
    assert orchestrator._affected_files == ["src/base.py", "src/extra.py"]
    assert cleared == {"affected_files": []}


def test_build_read_then_write_result_returns_write_required_history_for_modifying_task(tmp_path):
    result = build_read_then_write_result(
        state={"task": "edit the file", "tool_call_count": 4},
        result={"ok": True, "result": {"status": "ok", "content": "hello"}},
        tool_name="read_file",
        path_arg="a.txt",
        working_dir=str(tmp_path),
        truncate_tool_output=lambda payload: {**payload, "truncated": True},
        tool_last_used={"read_file:a.txt": 4},
        files_read={},
    )

    assert result is not None
    assert result["tool_call_count"] == 5
    assert result["verified_reads"]
    payload = __import__("json").loads(result["history"][0]["content"])
    assert payload["orchestration_hint"] == "write_required"
    assert payload["file_path"] == "a.txt"
    assert "enhanced_context" in payload


def test_build_read_then_write_result_only_marks_verified_read_for_non_modifying_task(tmp_path):
    result = build_read_then_write_result(
        state={"task": "read the file", "tool_call_count": 1},
        result={"ok": True, "result": {"status": "ok", "content": "hello"}},
        tool_name="read_file",
        path_arg="a.txt",
        working_dir=str(tmp_path),
        truncate_tool_output=lambda payload: payload,
        tool_last_used={},
        files_read={},
    )

    assert result == {
        "verified_reads": [str((tmp_path / "a.txt").resolve())],
        "files_read": {str((tmp_path / "a.txt").resolve()): True},
    }


def test_build_tool_history_messages_wraps_truncated_tool_result():
    messages = build_tool_history_messages(
        result={"ok": True},
        truncate_tool_output=lambda payload: {**payload, "marker": True},
    )

    payload = __import__("json").loads(messages[0]["content"])
    assert payload == {"tool_execution_result": {"ok": True, "marker": True}}


def test_compute_replan_trigger_and_plan_progress_payload():
    replan = compute_replan_trigger(result={"requires_split": True, "error": "too large"})
    progress = compute_plan_progress_payload(
        state={"session_id": "s1"},
        current_plan=[{"description": "step 1"}],
        current_step=0,
        execution_ok=True,
    )

    assert replan == {
        "replan_required": "too large",
        "action_failed": True,
        "next_action": None,
    }
    assert progress["plan_progress"]["planId"] == "plan_s1"
    assert progress["plan_progress"]["status"] == "completed"


def test_compute_plan_exit_update_consumes_orchestrator_steps():
    class _Orchestrator:
        _committed_plan_steps = [{"description": "done"}]
        _plan_mode_approved = False

    orchestrator = _Orchestrator()
    update = compute_plan_exit_update(orchestrator=orchestrator)

    assert update == {
        "current_plan": [{"description": "done"}],
        "plan_mode_approved": True,
    }
    assert orchestrator._committed_plan_steps is None
    assert orchestrator._plan_mode_approved is True


def test_build_execution_return_payload_merges_updates():
    payload = build_execution_return_payload(
        result={"ok": True},
        tool_name="read_file",
        verified_reads=["/tmp/a"],
        history=[{"role": "user", "content": "x"}],
        tool_call_count=3,
        tool_last_used={"read_file:a": 2},
        files_read={"/tmp/a": True},
        recent_tool_calls=["fp"],
        plan_advance={"current_step": 1},
        wave_advance={"current_wave": 2},
        replan_triggered={"replan_required": "too large"},
        plan_progress_event={"plan_progress": {"status": "completed"}},
        plan_approval_consumed={"plan_mode_approved": False},
        no_plan_fail_update={"no_plan_fail_count": 0},
        affected_files_update={"affected_files": ["src/a.py"]},
        plan_exit_update={"current_plan": [{"description": "x"}]},
    )

    assert payload["last_tool_name"] == "read_file"
    assert payload["tool_call_count"] == 3
    assert payload["current_step"] == 1
    assert payload["current_wave"] == 2
    assert payload["replan_required"] == "too large"
    assert payload["plan_progress"] == {"status": "completed"}
    assert payload["affected_files"] == ["src/a.py"]


def test_handle_execution_preflight_and_role_gate_requires_orchestrator():
    result = handle_execution_preflight_and_role_gate(
        state={},
        config={},
        orchestrator=None,
        action={"name": "read_file"},
        tool_name="read_file",
        args={},
        logger=type("L", (), {"info": lambda *a, **k: None})(),
    )

    assert result == {
        "last_result": {"ok": False, "error": "Orchestrator required for tool execution"},
        "errors": ["orchestrator not available"],
    }


def test_handle_execution_preflight_and_role_gate_returns_sandbox_violation():
    class MM:
        def __init__(self):
            self.messages = []

        def append(self, role, content):
            self.messages.append((role, content))

    orchestrator = type(
        "Orch",
        (),
        {"preflight_check": lambda self, action: {"ok": False, "error": "blocked"}, "msg_mgr": MM()},
    )()
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: None, "error": lambda *a, **k: None},
    )()

    result = handle_execution_preflight_and_role_gate(
        state={"last_result": None, "rounds": 0},
        config={},
        orchestrator=orchestrator,
        action={"name": "bash"},
        tool_name="bash",
        args={},
        logger=logger,
    )

    assert result["last_result"] == {"ok": False, "error": "blocked"}
    assert result["history"] == [{"role": "user", "content": "[SANDBOX VIOLATION] blocked"}]


def test_handle_execution_preflight_and_role_gate_uses_completion_signal_shortcut():
    orchestrator = type(
        "Orch",
        (),
        {"preflight_check": lambda self, action: {"ok": False, "error": "tool not found"}, "msg_mgr": None},
    )()
    logger = type("L", (), {"info": lambda *a, **k: None})()

    result = handle_execution_preflight_and_role_gate(
        state={"last_result": {"ok": True}, "rounds": 2},
        config={},
        orchestrator=orchestrator,
        action={"name": "respond"},
        tool_name="respond",
        args={},
        logger=logger,
    )

    assert result["last_result"]["_completion_detected"] is True
    assert result["next_action"] is None


def test_handle_execution_preflight_and_role_gate_blocks_plan_mode(monkeypatch):
    class _PlanMode:
        def __init__(self, orchestrator):
            self.pending_plan = None

        def is_blocked(self, tool_name):
            return True

        def set_pending_plan(self, payload):
            self.pending_plan = payload

    import src.core.orchestration.plan_mode as plan_mode_module

    monkeypatch.setattr(plan_mode_module, "PlanMode", _PlanMode)

    orchestrator = type(
        "Orch",
        (),
        {"preflight_check": lambda self, action: {"ok": True}, "plan_mode": None},
    )()
    logger = type("L", (), {"info": lambda *a, **k: None})()

    result = handle_execution_preflight_and_role_gate(
        state={
            "plan_mode_enabled": True,
            "plan_mode_approved": False,
            "current_plan": [{"description": "x"}],
            "_modifying_tools": ("write_file",),
        },
        config={},
        orchestrator=orchestrator,
        action={"name": "write_file"},
        tool_name="write_file",
        args={"path": "a.txt"},
        logger=logger,
    )

    assert result["awaiting_plan_approval"] is True
    assert result["plan_mode_blocked_tool"] == "write_file"


def test_handle_execution_preflight_and_role_gate_blocks_disallowed_role(monkeypatch):
    import src.core.orchestration.graph.nodes.node_utils as node_utils_module
    import src.core.orchestration.role_config as role_config_module

    monkeypatch.setattr(node_utils_module, "get_current_role", lambda state, config: "planner")
    monkeypatch.setattr(role_config_module, "is_tool_allowed_for_role", lambda tool, role: False)

    orchestrator = type(
        "Orch",
        (),
        {"preflight_check": lambda self, action: {"ok": True}},
    )()
    logger = type(
        "L",
        (),
        {"warning": lambda *a, **k: None, "info": lambda *a, **k: None},
    )()

    result = handle_execution_preflight_and_role_gate(
        state={},
        config={},
        orchestrator=orchestrator,
        action={"name": "write_file"},
        tool_name="write_file",
        args={"path": "a.txt"},
        logger=logger,
    )

    assert result["last_result"]["ok"] is False
    assert "not permitted" in result["last_result"]["error"]


def test_emit_plan_progress_and_sync_todo_publishes_and_checks_step():
    events = []
    todo_calls = []

    class _EventBus:
        def publish(self, name, payload):
            events.append((name, payload))
        def publish_typed(self, event):
            events.append((event.__class__.__name__, event.to_dict()))

    orchestrator = type("Orch", (), {"event_bus": _EventBus()})()

    emit_plan_progress_and_sync_todo(
        orchestrator=orchestrator,
        state={"working_dir": "/tmp/project"},
        current_step=2,
        execution_ok=True,
        plan_progress_event={"plan_progress": {"status": "completed", "step": 3}},
        manage_todo_fn=lambda **kwargs: todo_calls.append(kwargs),
    )

    assert len(events) == 1
    assert events[0][0] == "PlanProgress"
    plan_progress = events[0][1]["plan_progress"]
    assert plan_progress["status"] == "completed"
    assert plan_progress["step"] == 3
    assert todo_calls == [{"action": "check", "workdir": "/tmp/project", "step_id": 2}]


def test_emit_plan_progress_and_sync_todo_skips_when_no_progress_or_unsuccessful():
    events = []
    todo_calls = []

    class _EventBus:
        def publish(self, name, payload):
            events.append((name, payload))
        def publish_typed(self, event):
            events.append((event.__class__.__name__, event.to_dict()))

    orchestrator = type("Orch", (), {"event_bus": _EventBus()})()

    emit_plan_progress_and_sync_todo(
        orchestrator=orchestrator,
        state={"working_dir": "/tmp/project"},
        current_step=0,
        execution_ok=False,
        plan_progress_event={"plan_progress": {"status": "in_progress"}},
        manage_todo_fn=lambda **kwargs: todo_calls.append(kwargs),
    )
    emit_plan_progress_and_sync_todo(
        orchestrator=orchestrator,
        state={"working_dir": "/tmp/project"},
        current_step=0,
        execution_ok=True,
        plan_progress_event=None,
        manage_todo_fn=lambda **kwargs: todo_calls.append(kwargs),
    )

    assert len(events) == 1
    assert events[0][0] == "PlanProgress"
    assert events[0][1]["plan_progress"]["status"] == "in_progress"
    assert todo_calls == []


def test_compute_execution_post_tool_updates_aggregates_sub_updates():
    class _Orchestrator:
        _affected_files = ["src/base.py"]
        _committed_plan_steps = [{"description": "done"}]
        _plan_mode_approved = False

    orchestrator = _Orchestrator()
    state = {
        "tool_call_count": 2,
        "current_plan": None,
        "no_plan_fail_count": 1,
        "plan_mode_approved": True,
        "affected_files": ["src/base.py"],
    }
    result = compute_execution_post_tool_updates(
        state=state,
        orchestrator=orchestrator,
        tool_name="ask_user",
        result={"status": "ok", "answer": "Also include src/extra.py"},
        modifying_tools=("write_file",),
    )

    assert result["tool_call_count"] == 3
    assert result["no_plan_fail_update"] == {"no_plan_fail_count": 0}
    assert result["plan_approval_consumed"] == {}
    assert result["affected_files_update"] == {"affected_files": ["src/base.py", "src/extra.py"]}
    assert result["plan_exit_update"] == {
        "current_plan": [{"description": "done"}],
        "plan_mode_approved": True,
    }


def test_emit_execution_step_start_publishes_event_and_returns_metadata():
    events = []

    class _EventBus:
        def publish(self, name, payload):
            events.append((name, payload))
        def publish_typed(self, event):
            events.append((event.__class__.__name__, event.to_dict()))

    orchestrator = type("Orch", (), {"event_bus": _EventBus()})()
    meta = emit_execution_step_start(
        orchestrator=orchestrator,
        state={"current_step": 1, "session_id": "s1"},
        current_plan=[{"description": "a"}, {"description": "b"}],
        current_step=1,
        tool_name="write_file",
        now_monotonic=123.0,
    )

    assert meta == {"step_start_ts": 123.0, "step_num": 2, "total_steps": 2}
    assert len(events) == 1
    assert events[0][0] == "StepStart"
    assert events[0][1]["step"] == 2
    assert events[0][1]["total"] == 2
    assert events[0][1]["tool"] == "write_file"


def test_emit_execution_step_finish_publishes_elapsed_and_ok_flag():
    events = []

    class _EventBus:
        def publish(self, name, payload):
            events.append((name, payload))
        def publish_typed(self, event):
            events.append((event.__class__.__name__, event.to_dict()))

    orchestrator = type("Orch", (), {"event_bus": _EventBus()})()
    emit_execution_step_finish(
        orchestrator=orchestrator,
        state={"tool_call_count": 4, "session_id": "s1"},
        tool_name="read_file",
        result={"ok": True},
        step_num=1,
        total_steps=3,
        step_start_ts=10.0,
        now_monotonic=10.042,
    )

    assert len(events) == 1
    assert events[0][0] == "StepFinish"
    assert events[0][1]["step"] == 1
    assert events[0][1]["total"] == 3
    assert events[0][1]["tool"] == "read_file"
    assert events[0][1]["ok"] is True
    assert events[0][1]["elapsed_ms"] == 41
    assert events[0][1]["tool_call_count"] == 5


def test_maybe_build_preview_result_returns_pending_preview(tmp_path):
    class _Preview:
        preview_id = "p1"

    class _PreviewService:
        def __init__(self):
            self.calls = []

        def generate_preview(self, **kwargs):
            self.calls.append(kwargs)
            return _Preview()

    preview_service = _PreviewService()
    orchestrator = type("Orch", (), {"preview_service": preview_service})()
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: None, "warning": lambda *a, **k: None},
    )()
    target = tmp_path / "a.txt"
    target.write_text("old")

    result = maybe_build_preview_result(
        state={"preview_mode_enabled": True, "working_dir": str(tmp_path)},
        orchestrator=orchestrator,
        tool_name="write_file",
        args={"path": "a.txt", "content": "new"},
        modifying_tools=("write_file",),
        logger=logger,
        path_cls=__import__("pathlib").Path,
    )

    assert result == {
        "pending_preview_id": "p1",
        "awaiting_user_input": True,
        "preview_confirmed": False,
    }
    assert preview_service.calls[0]["old_content"] == "old"
    assert preview_service.calls[0]["new_content"] == "new"


def test_handle_read_then_write_success_returns_early_result_and_updates_reads():
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: None, "error": lambda *a, **k: None},
    )()

    result = handle_read_then_write_success(
        state={"task": "edit the file"},
        result={"ok": True},
        tool_name="read_file",
        path_arg="a.txt",
        working_dir="/tmp",
        truncate_tool_output=lambda payload: payload,
        tool_last_used={"read_file:a.txt": 1},
        files_read={"/tmp/old.txt": True},
        build_read_then_write_result_fn=lambda **kwargs: {
            "verified_reads": ["/tmp/a.txt"],
            "files_read": {"/tmp/a.txt": True},
            "history": [{"role": "user", "content": "x"}],
        },
        logger=logger,
    )

    assert result["verified_update"] == ["/tmp/a.txt"]
    assert result["files_read_update"] == {"/tmp/a.txt": True}
    assert result["early_result"] == {
        "verified_reads": ["/tmp/a.txt"],
        "files_read": {"/tmp/a.txt": True},
        "history": [{"role": "user", "content": "x"}],
    }


def test_handle_read_then_write_success_keeps_existing_reads_when_no_result():
    logger = type(
        "L",
        (),
        {"info": lambda *a, **k: None, "error": lambda *a, **k: None},
    )()

    result = handle_read_then_write_success(
        state={"task": "read the file"},
        result={"ok": True},
        tool_name="read_file",
        path_arg="a.txt",
        working_dir="/tmp",
        truncate_tool_output=lambda payload: payload,
        tool_last_used={},
        files_read={"/tmp/old.txt": True},
        build_read_then_write_result_fn=lambda **kwargs: None,
        logger=logger,
    )

    assert result["verified_update"] == []
    assert result["files_read_update"] == {"/tmp/old.txt": True}
    assert result["early_result"] is None


def test_schedule_async_post_tool_hook_schedules_and_logs_callback_errors():
    logged = []

    class _Runner:
        async def async_run_post(self, tool_name, args, result):
            return None

    class _Task:
        def __init__(self):
            self.cb = None

        def add_done_callback(self, cb):
            self.cb = cb

        def cancelled(self):
            return False

        def exception(self):
            return RuntimeError("boom")

    task = _Task()
    captured = {}

    def _ensure_future(coro):
        captured["coro"] = coro
        return task

    logger = type("L", (), {"warning": lambda *a, **k: logged.append(a)})()
    orchestrator = type("Orch", (), {"_tool_hook_runner": _Runner()})()

    schedule_async_post_tool_hook(
        orchestrator=orchestrator,
        tool_name="write_file",
        args={"path": "a.txt"},
        result={"ok": True},
        ensure_future_fn=_ensure_future,
        logger=logger,
    )

    assert "coro" in captured
    assert task.cb is not None
    task.cb(task)
    assert logged and logged[0][1] == "async_run_post failed: %s"
    captured["coro"].close()


@__import__("pytest").mark.asyncio
async def test_dispatch_execution_tool_uses_lock_manager_when_prsw_active():
    called = {}

    async def _with_locks(tool_name, args, lock_manager, orchestrator, agent_id, model_tier=""):
        called["with_locks"] = (tool_name, args, lock_manager, agent_id, model_tier)
        return {"ok": True, "mode": "locks"}

    async def _dispatch(orchestrator, action, model_tier):
        called["dispatch"] = (action, model_tier)
        return {"ok": True, "mode": "direct"}

    result = await dispatch_execution_tool(
        state={"execution_waves": [[0]], "session_id": "s1", "model_tier": "medium"},
        orchestrator=object(),
        action={"name": "write_file", "arguments": {"path": "a.txt"}},
        tool_name="write_file",
        args={"path": "a.txt"},
        get_lock_manager_fn=lambda orch: "lm",
        execute_with_locks_fn=_with_locks,
        dispatch_tool_fn=_dispatch,
    )

    assert result == {"ok": True, "mode": "locks"}
    assert called["with_locks"] == ("write_file", {"path": "a.txt"}, "lm", "s1", "medium")
    assert "dispatch" not in called


@__import__("pytest").mark.asyncio
async def test_dispatch_execution_tool_falls_back_to_direct_dispatch():
    called = {}

    async def _with_locks(*args, **kwargs):
        called["with_locks"] = True
        return {"ok": True, "mode": "locks"}

    async def _dispatch(orchestrator, action, model_tier):
        called["dispatch"] = (action, model_tier)
        return {"ok": True, "mode": "direct"}

    result = await dispatch_execution_tool(
        state={"model_tier": "small"},
        orchestrator=object(),
        action={"name": "read_file", "arguments": {"path": "a.txt"}},
        tool_name="read_file",
        args={"path": "a.txt"},
        get_lock_manager_fn=lambda orch: None,
        execute_with_locks_fn=_with_locks,
        dispatch_tool_fn=_dispatch,
    )

    assert result == {"ok": True, "mode": "direct"}
    assert called["dispatch"] == ({"name": "read_file", "arguments": {"path": "a.txt"}}, "small")
    assert "with_locks" not in called
