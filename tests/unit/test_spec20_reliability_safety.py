"""Unit tests for SPEC-20 reliability and safety improvements:
- FileLock timeout loop with non-blocking flock and remote host PID check guard
- Bounded shutdown hooks in independent threads preventing executor deadlocks
- Subprocess process group termination on timeout
- KVCacheGovernor threshold clamping
- Telemetry payload and error redaction
- Thinking mode wiring and active mode resolution
"""

import os
import signal
import socket
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from src.core.inference.kv_cache_governor import KVCacheGovernor, KVCacheState
from src.core.inference.telemetry import publish_model_response, with_telemetry
from src.core.inference.thinking_utils import (
    ThinkingMode,
    get_active_thinking_mode,
    resolve_thinking_mode,
    set_active_thinking_mode,
)
from src.core.orchestration.session_lifecycle import SessionLifecycleManager
from src.tools.file_lock import FileLock


def test_file_lock_timeout_and_remote_host(tmp_path):
    """FileLock raises TimeoutError when lock is held, and does not check PID for remote hosts."""
    target_file = tmp_path / "test.txt"
    target_file.write_text("initial")

    lock1 = FileLock(str(target_file), timeout=1.0)
    lock1.acquire()

    # Second lock with very short timeout should time out
    lock2 = FileLock(str(target_file), timeout=0.2)
    start = time.time()
    with pytest.raises(TimeoutError):
        lock2.acquire()
    elapsed = time.time() - start
    assert elapsed >= 0.2

    lock1.release()

    # Test lock._parse_lockfile remote vs same host
    probe_lock = FileLock(str(target_file))
    pid, host, ts, same_host, too_old = probe_lock._parse_lockfile("pid=999999 host=remote-host-xyz ts=100")
    assert host == "remote-host-xyz"
    assert same_host is False

    local_host = socket.gethostname()
    pid, host, ts, same_host, too_old = probe_lock._parse_lockfile(f"pid=999999 host={local_host} ts=100")
    assert host == local_host
    assert same_host is True

    # When fallback file lock sees a foreign host lock, it must never call os.kill(pid, 0)
    fallback_lock = FileLock(str(tmp_path / "fallback.txt"), timeout=0.2)
    fallback_lock._fcntl = None  # force fallback code path

    # Pre-create lockfile with foreign host
    lockfile_path = tmp_path / "fallback.txt.lock"
    lockfile_path.write_text("pid=88888 host=remote-node-123 ts=9999999999999")
    fallback_lock.lock_path = lockfile_path

    with patch("os.kill") as mock_kill:
        with pytest.raises(TimeoutError):
            fallback_lock.acquire()
        mock_kill.assert_not_called()


def test_session_lifecycle_bounded_daemon_hooks(tmp_path):
    """Shutdown hooks run in independent daemon threads so a hanging hook doesn't block subsequent hooks."""
    mgr = SessionLifecycleManager(str(tmp_path))
    executed_hooks = []

    def hanging_hook(sid):
        executed_hooks.append("hung_start")
        time.sleep(1.0)
        executed_hooks.append("hung_end")

    def normal_hook(sid):
        executed_hooks.append("normal")

    mgr.on_shutdown("hung", hanging_hook)
    mgr.on_shutdown("normal", normal_hook)

    start = time.time()
    # shutdown with a total timeout of 0.2s
    res = mgr.shutdown_session("test-session", timeout=0.2)
    elapsed = time.time() - start

    assert elapsed < 0.6  # should not block for 1.0s
    assert "hung_start" in executed_hooks
    assert "normal" in executed_hooks
    assert "hook:hung:timeout" in res.cleanup_actions
    assert "hook:normal" in res.cleanup_actions


def test_process_group_termination():
    """_kill_proc_group terminates an entire process group."""
    from src.core.orchestration.tool_execution_pipeline import _kill_proc_group

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    with patch("os.getpgid", return_value=12345) as mock_getpgid, patch("os.killpg") as mock_killpg:
        _kill_proc_group(mock_proc)
        mock_getpgid.assert_called_once_with(12345)
        mock_killpg.assert_called_once_with(12345, signal.SIGKILL)


def test_kv_cache_governor_threshold_clamping():
    """Governor clamps warning and compact thresholds to [0.0, 1.0] and enforces warning <= compact."""
    # Invalid negative warning and > 1.0 compact
    gov = KVCacheGovernor(
        max_vram_gb=16.0,
        model_weights_gb=4.0,
        warning_threshold=-0.5,
        compact_threshold=1.5,
    )
    assert gov.warning_threshold == 0.0
    assert gov.compact_threshold == 1.0

    # Inverted thresholds: warning > compact
    gov2 = KVCacheGovernor(
        max_vram_gb=16.0,
        model_weights_gb=4.0,
        warning_threshold=0.9,
        compact_threshold=0.7,
    )
    assert gov2.warning_threshold == 0.7
    assert gov2.compact_threshold == 0.7

    # Dataclass KVCacheState validation
    state = KVCacheState(
        current_tokens=100,
        max_tokens=1000,
        kv_cache_gb=1.0,
        max_kv_cache_gb=10.0,
        warning_threshold=0.95,
        compact_threshold=0.8,
    )
    assert state.warning_threshold == 0.8
    assert state.compact_threshold == 0.8


def test_telemetry_redaction():
    """publish_model_response redacts secrets in extra and with_telemetry redacts errors."""
    mock_bus = MagicMock()
    extra_data = {
        "api_key": "sk-ant-api03-abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890aa",
        "nested": {"secret_token": "ghp_123456789012345678901234567890123456"},
    }

    publish_model_response(
        event_bus=mock_bus,
        provider="anthropic",
        model="claude-3-5-sonnet",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        latency=0.5,
        extra=extra_data,
    )

    published_event = mock_bus.publish_typed.call_args[0][0]
    extra_field = getattr(published_event, "extra", {})
    assert "sk-ant-api03" not in str(extra_field)
    assert "[REDACTED" in str(extra_field)

    # Test with_telemetry error redaction
    class DummyProvider:
        name = "test-provider"

        @with_telemetry
        def fail_with_secret(self):
            raise ValueError("Error contacting sk-ant-api03-abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890aa")

    with patch("src.core.inference.telemetry.logger") as mock_logger:
        provider = DummyProvider()
        with pytest.raises(ValueError):
            provider.fail_with_secret()

        mock_logger.error.assert_called_once()
        logged_msg = mock_logger.error.call_args[0][0]
        assert "sk-ant-api03" not in logged_msg
        assert "[REDACTED" in logged_msg


def test_thinking_mode_wiring():
    """Active thinking mode can be set globally and is honored by resolve_thinking_mode."""
    try:
        set_active_thinking_mode("on")
        assert get_active_thinking_mode() == ThinkingMode.ON
        assert resolve_thinking_mode(model_id="gpt-4o") is True

        set_active_thinking_mode("off")
        assert get_active_thinking_mode() == ThinkingMode.OFF
        assert resolve_thinking_mode(model_id="deepseek-r1") is False

        set_active_thinking_mode("auto")
        assert get_active_thinking_mode() == ThinkingMode.AUTO
        assert resolve_thinking_mode(model_id="deepseek-r1") is True
        assert resolve_thinking_mode(model_id="gpt-4o") is False

        # Explicit cli_mode parameter overrides global setting
        assert resolve_thinking_mode(cli_mode="on", model_id="gpt-4o") is True
        assert resolve_thinking_mode(cli_mode=ThinkingMode.OFF, model_id="deepseek-r1") is False
    finally:
        set_active_thinking_mode(None)


def test_kv_cache_governor_zero_and_overcommitted_vram():
    """Governor handles zero VRAM and overcommitted VRAM cleanly without negative tokens."""
    # Zero VRAM (CPU-only)
    gov_cpu = KVCacheGovernor(max_vram_gb=0.0, model_weights_gb=0.0, cpu_token_budget=4096)
    assert gov_cpu.max_tokens == 4096
    assert gov_cpu.max_kv_cache_gb == 0.0
    assert gov_cpu.will_overflow(5000) is True
    assert gov_cpu.will_overflow(2000) is False

    # Overcommitted VRAM: weights exceed VRAM
    gov_over = KVCacheGovernor(max_vram_gb=4.0, model_weights_gb=8.0)
    assert gov_over.max_tokens == 0
    assert gov_over.max_kv_cache_gb == 0.0
    state = gov_over.current_state
    assert state.usage_ratio == 1.0
    assert state.should_warn is True
    assert state.should_compact is True
    assert gov_over.will_overflow(0) is True


def test_swebench_path_traversal_and_operational_tools(tmp_path):
    """SWEBenchRunner rejects path-traversal instance_ids and passes operational tools."""
    from src.core.evaluation.swebench import SWEBenchInstance, SWEBenchRunner, _invoke_swebench_agent

    runner = SWEBenchRunner(str(tmp_path))

    # Path traversal attempt
    evil_instance = SWEBenchInstance(
        instance_id="../../evil",
        repo="test/repo",
        base_commit="HEAD",
        problem_statement="do bad",
    )
    with pytest.raises(ValueError, match="Invalid instance_id path traversal"):
        runner.run_instance(evil_instance, None)

    # Operational tools passed to agent
    mock_agent = MagicMock()
    mock_agent.get_tools_for_role.return_value = ["tool_a", "tool_b"]
    good_instance = SWEBenchInstance(
        instance_id="good-1",
        repo="test/repo",
        base_commit="HEAD",
        problem_statement="fix bug",
    )
    _invoke_swebench_agent(mock_agent, good_instance, tmp_path)
    mock_agent.get_tools_for_role.assert_called_once_with("operational")
    mock_agent.run_agent_once.assert_called_once_with(
        system_prompt_name="operational",
        messages=[{"role": "user", "content": "fix bug"}],
        tools=["tool_a", "tool_b"],
    )


def test_session_export_fsync_order(tmp_path):
    """session export flushes and fsyncs after writing."""
    from src.cli.session_cmd import _export_session

    mock_session = MagicMock()
    mock_session.session_id = "test-session-12345"
    mock_session.messages = [{"role": "user", "content": "hello"}]
    mock_session.model = "gpt-4o"
    mock_session.provider = "openai"

    out_file = tmp_path / "export.md"
    args = MagicMock()
    args.session_id = "test-session-12345"
    args.out = str(out_file)

    with patch("src.core.orchestration.session_store.load_session", return_value=mock_session), patch("os.fsync") as mock_fsync:
        ret = _export_session(args)
        assert ret == 0
        assert out_file.exists()
        assert "hello" in out_file.read_text(encoding="utf-8")
        assert mock_fsync.called


def test_tracer_secret_redaction():
    """Tracer sanitizes secrets in span attributes and bridge events."""
    from src.core.telemetry.tracer import _bridge_event, span_node

    mock_tracer = MagicMock()
    mock_span = MagicMock()
    mock_tracer.start_as_current_span.return_value.__enter__.return_value = mock_span

    secret_key = "sk-ant-api03-abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890aa"

    with patch("src.core.telemetry.tracer.get_tracer", return_value=mock_tracer):
        with span_node("test", attributes={"key": secret_key}):
            pass

        attrs = mock_tracer.start_as_current_span.call_args[1]["attributes"]
        assert secret_key not in attrs["key"]
        assert "[REDACTED" in attrs["key"]


def test_work_summary_tool_call_history():
    """_generate_work_summary parses tool executions from tool_call_history and tool_execution_result."""
    from src.core.orchestration.work_summary import _generate_work_summary

    final_state = {
        "task": "do work",
        "rounds": 2,
        "tool_call_history": [
            {"tool_name": "read_file", "ok": True},
            {"type": "tool_execution_result", "tool_name": "write_file", "ok": True},
        ],
    }
    history = [
        {"type": "tool_execution_result", "tool_name": "edit_file", "ok": True},
    ]
    summary = _generate_work_summary(final_state, history)
    assert "read_file" in summary
    assert "write_file" in summary
    assert "edit_file" in summary


def test_tautological_evaluation_scenarios_remediated():
    """SPEC-20 item 6: add_type_hints, trace_call_chain, ambiguous_format_request
    must not have expected_files substrings present in setup_files."""
    from src.core.evaluation.scenario_evaluator import get_default_scenarios

    scenarios = {s.name: s for s in get_default_scenarios()}
    for name in ("add_type_hints", "trace_call_chain", "ambiguous_format_request"):
        assert name in scenarios, f"Scenario {name} missing"
        s = scenarios[name]
        for filename, expected_sub in s.expected_files.items():
            setup_content = s.setup_files.get(filename, "")
            assert expected_sub not in setup_content, (
                f"Scenario '{name}' checks for substring {expected_sub!r} which is "
                f"already present in setup_files for {filename}"
            )
