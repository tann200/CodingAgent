"""TASK-36 tests: cross-model history normalization, /advisor, /btw, /queue, --output stream-json, --private, --parallel."""

from __future__ import annotations

import copy
import io
import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from textual.containers import VerticalScroll

from src.cli.headless_stream import (
    ParallelWorktreeSession,
    StreamJsonEmitter,
    classify_headless_status,
    exit_code_for_status,
)
from src.core.inference.history_normalizer import normalize_history_for_model_switch
from src.core.memory.session_store import NullSessionStore
from src.core.orchestration.btw_query import BTW_ALLOWED_TOOLS, filter_btw_tools, run_btw_query
from src.core.orchestration.orchestrator import Orchestrator
from src.main import _run_headless, main as cli_main
from src.tools.advisor_tool import AdvisorConfig, AdvisorSessionState, ask_advisor
from tui.tui_src.ui.app import AgentApp
from tui.tui_src.ui.events import SlashCommand


def test_history_normalizer_flattens_tool_calls_and_strips_provider_signatures() -> None:
    original = [
        {"role": "system", "content": "You are CodingAgent.", "cache_control": {"type": "ephemeral"}},
        {"role": "user", "content": "Fix bug in app.py"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "secret chain", "signature": "sig_12345"},
                {"type": "text", "text": "Let me inspect app.py."},
                {"type": "tool_use", "id": "toolu_01", "name": "read_file", "input": {"path": "app.py"}},
            ],
            "reasoning_content": "provider specific reasoning",
            "id": "msg_abc",
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "name": "read_file", "content": "print('hi')"}
            ],
        },
        {
            "role": "assistant",
            "content": "Checking with grep.",
            "tool_calls": [
                {
                    "id": "call_99",
                    "type": "function",
                    "function": {"name": "grep", "arguments": '{"pattern": "hi"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_99", "name": "grep", "content": "app.py:1:print('hi')"},
    ]
    snapshot = copy.deepcopy(original)

    normalized = normalize_history_for_model_switch(original)
    # Caller's list was never mutated
    assert original == snapshot

    assert normalized[0] == {"role": "system", "content": "You are CodingAgent."}
    assert normalized[1]["role"] == "user"
    assert normalized[2]["role"] == "assistant"
    assert "[Prior tool call: read_file(" in normalized[2]["content"]
    assert "secret chain" not in normalized[2]["content"]
    assert "sig_12345" not in str(normalized)
    assert "tool_calls" not in str(normalized)
    assert "tool_call_id" not in str(normalized)
    assert "reasoning_content" not in str(normalized)

    # Every message only has clean role + str content (no tool role left over)
    for msg in normalized:
        assert set(msg.keys()) == {"role", "content"}
        assert msg["role"] in ("system", "user", "assistant")
        assert isinstance(msg["content"], str)
    # No two adjacent user/assistant messages share the same role
    roles = [m["role"] for m in normalized if m["role"] != "system"]
    for a, b in zip(roles, roles[1:]):
        assert a != b


def test_advisor_tool_opt_in_dormancy_redaction_handoff_and_cap() -> None:
    # Dormant by default
    default_state = AdvisorSessionState(config=AdvisorConfig())
    dormant = ask_advisor("Should we refactor?", "ctx", state=default_state)
    assert dormant["status"] == "disabled"
    assert default_state.uses == 0

    # Configured model mode with secret redaction & hard max_uses=3 cap
    seen_calls: List[tuple[str, str]] = []

    def _fake_call(q: str, ctx: str, model: str | None, prov: str | None) -> str:
        seen_calls.append((q, ctx))
        return f"Advice from {model}: check edge case."

    state = AdvisorSessionState(config=AdvisorConfig(advisor_model="claude-sonnet", max_uses=3))
    for i in range(3):
        res = ask_advisor(
            f"Question {i} token=sk-1234567890abcdefghijklmnop",
            "context summary",
            state=state,
            call_fn=_fake_call,
        )
        assert res["status"] == "ok"
        assert res["uses"] == i + 1
        assert res["remaining"] == 3 - (i + 1)

    # Secrets must have been redacted before leaving session
    assert "sk-1234567890abcdefghijklmnop" not in seen_calls[0][0]

    # 4th call must fail closed with budget_exhausted
    over = ask_advisor("4th question", "ctx", state=state, call_fn=_fake_call)
    assert over["status"] == "budget_exhausted"
    assert len(seen_calls) == 3

    # Human handoff mode
    h_state = AdvisorSessionState(config=AdvisorConfig(handoff_enabled=True, max_uses=2))
    h_res = ask_advisor("Need product decision", "summary", state=h_state)
    assert h_res["status"] == "handoff_required"
    h_res2 = ask_advisor(
        "Need decision 2",
        "summary",
        state=h_state,
        handoff_fn=lambda q, c: "Choose option B",
    )
    assert h_res2["status"] == "ok"
    assert h_res2["advice"] == "Choose option B"


def test_stream_json_emitter_sanitizes_events_and_enforces_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    buf = io.StringIO()
    emitter = StreamJsonEmitter(buf)

    # Fake EventBus with subscribe/unsubscribe/publish
    listeners: Dict[str, List[Any]] = {}

    class _FakeBus:
        def subscribe(self, name: str, fn: Any) -> None:
            listeners.setdefault(name, []).append(fn)

        def unsubscribe(self, name: str, fn: Any) -> None:
            if name in listeners and fn in listeners[name]:
                listeners[name].remove(fn)

        def publish(self, name: str, payload: Any) -> None:
            for fn in list(listeners.get(name, [])):
                fn(payload)

    bus = _FakeBus()
    emitter.attach(bus)
    bus.publish("step.start", {"step": 1})
    bus.publish("tool.execute.start", {"tool": "read_file", "args": {"secret": "sk-1234567890abcdefghijklmnop"}})
    bus.publish("tool.execute.finish", {"tool": "read_file", "ok": True, "output": "RAW_BODY_SHOULD_NOT_APPEAR"})
    bus.publish("verification.completed", {"passed": True})
    bus.publish("retry.attempt", {"attempt_number": 1, "error_type": "timeout"})
    bus.publish("context.compacted", {"dropped_count": 4})
    bus.publish("step.finish", {"step": 1})
    emitter.emit_final_result(
        status="complete",
        result_text="Done with key sk-1234567890abcdefghijklmnop",
        usage={"input_tokens": 100, "output_tokens": 25},
    )
    emitter.detach()

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    types = [ev["type"] for ev in lines]
    assert types == [
        "turn_start",
        "tool_start",
        "tool_end",
        "verification",
        "retry",
        "compaction",
        "turn_end",
        "result",
    ]
    raw_dump = buf.getvalue()
    assert "RAW_BODY_SHOULD_NOT_APPEAR" not in raw_dump
    assert "sk-1234567890abcdefghijklmnop" not in raw_dump
    assert lines[-1]["status"] == "complete"
    assert lines[-1]["usage"] == {"input_tokens": 100, "output_tokens": 25}

    # Exit code contract: 0 strictly on complete
    assert exit_code_for_status(classify_headless_status({"assistant_message": "ok"})) == 0
    assert exit_code_for_status(classify_headless_status({"error": "boom"})) == 1
    assert exit_code_for_status(classify_headless_status({"cancelled": True})) == 130

    # End-to-end _run_headless with --output stream-json and mocked Orchestrator
    fake_orch = MagicMock()
    fake_orch.event_bus = _FakeBus()
    fake_orch._usage_buffer = {"input_tokens": 50, "output_tokens": 10}
    fake_orch.run_agent_once.return_value = {"status": "failed", "error": "tests failed", "assistant_message": ""}
    with patch("src.core.orchestration.orchestrator.Orchestrator", return_value=fake_orch):
        rc = _run_headless("fix tests", "stream-json", None, private=True)
    assert rc == 1
    out_lines = [json.loads(ln) for ln in capsys.readouterr().out.strip().splitlines()]
    assert out_lines[-1]["type"] == "result"
    assert out_lines[-1]["status"] == "failed"


def test_private_mode_creates_zero_disk_session_memory_or_trace_artifacts(tmp_path: Path) -> None:
    ws = tmp_path / "private_repo"
    ws.mkdir()

    orch = Orchestrator(
        working_dir=ws,
        allow_external_working_dir=True,
        defer_provider_probe=True,
        private=True,
    )
    try:
        assert orch.private is True
        assert isinstance(orch.session_store, NullSessionStore)
        orch._append_execution_trace({"tool": "read_file", "args": {"path": "a.py"}})
        orch.flush_execution_trace()
        orch._clear_execution_trace()
        orch.begin_step_transaction()
        orch.event_bus.publish("task.completed", {"session_id": "priv1"})

        # Normal workspace edits still work cleanly
        (ws / "hello.txt").write_text("workspace edit ok\n", encoding="utf-8")
        assert (ws / "hello.txt").read_text(encoding="utf-8") == "workspace edit ok\n"

        # Zero .codingAgent/ directory, SQLite DB, trace, or timings artifacts on disk
        assert not (ws / ".codingAgent").exists()
    finally:
        orch.close()


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=str(repo), check=True, capture_output=True)
    (repo / "app.txt").write_text("line1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo), check=True, capture_output=True)


def test_parallel_worktree_session_clean_squash_and_conflict_prompts(tmp_path: Path) -> None:
    # 1. Clean squash-merge
    repo1 = tmp_path / "repo_clean"
    _init_git_repo(repo1)
    sess = ParallelWorktreeSession(repo1)
    wt = sess.create()
    assert wt.exists() and wt != repo1
    (wt / "feature.txt").write_text("hello parallel\n", encoding="utf-8")
    res = sess.finalize(print_fn=lambda _: None)
    assert res["status"] == "merged"
    assert (repo1 / "feature.txt").read_text(encoding="utf-8") == "hello parallel\n"
    assert not wt.exists()

    # 2. Merge conflict -> [h]elp then [l]eave keeps worktree intact
    repo2 = tmp_path / "repo_conflict_leave"
    _init_git_repo(repo2)
    sess2 = ParallelWorktreeSession(repo2)
    wt2 = sess2.create()
    (wt2 / "app.txt").write_text("worktree version\n", encoding="utf-8")
    (repo2 / "app.txt").write_text("main branch version\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=str(repo2), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "main change"], cwd=str(repo2), check=True, capture_output=True)

    answers = iter(["h", "l"])
    messages: List[str] = []
    res2 = sess2.finalize(input_fn=lambda _: next(answers), print_fn=messages.append)
    assert res2["status"] == "left"
    assert Path(res2["worktree"]).exists()
    assert any("[a]bort" in m for m in messages)
    # Clean up left worktree after assertion
    subprocess.run(["git", "worktree", "remove", "--force", res2["worktree"]], cwd=str(repo2), capture_output=True)

    # 3. Merge conflict -> [a]bort removes temp worktree cleanly
    repo3 = tmp_path / "repo_conflict_abort"
    _init_git_repo(repo3)
    sess3 = ParallelWorktreeSession(repo3)
    wt3 = sess3.create()
    (wt3 / "app.txt").write_text("wt edit\n", encoding="utf-8")
    (repo3 / "app.txt").write_text("main edit\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=str(repo3), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "main change"], cwd=str(repo3), check=True, capture_output=True)

    res3 = sess3.finalize(input_fn=lambda _: "a", print_fn=lambda _: None)
    assert res3["status"] == "aborted"
    assert not wt3.exists()


def test_btw_query_read_only_tools_and_no_history_mutation() -> None:
    tools = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "grep"}},
        {"type": "function", "function": {"name": "write_file"}},
        {"type": "function", "function": {"name": "bash"}},
    ]
    filtered = filter_btw_tools(tools)
    names = {t["function"]["name"] for t in filtered}
    assert names == {"read_file", "grep"}
    assert names.issubset(BTW_ALLOWED_TOOLS)

    history = [{"role": "user", "content": "working on auth"}]
    history_before = copy.deepcopy(history)
    captured_tools: List[Any] = []

    def _fake_call(messages, **kwargs):
        captured_tools.extend(kwargs.get("tools") or [])
        return {"content": "Auth is in src/server/auth.py"}

    out = run_btw_query(
        "Where is auth defined?",
        history_snapshot=history,
        tools=tools,
        call_fn=_fake_call,
    )
    assert out["status"] == "ok"
    assert "src/server/auth.py" in out["answer"]
    assert out["history_mutated"] is False
    assert history == history_before
    assert {t["function"]["name"] for t in captured_tools} == {"read_file", "grep"}


@pytest.mark.asyncio
async def test_tui_slash_advisor_btw_and_queue_commands() -> None:
    app = AgentApp()
    async with app.run_test(size=(120, 35)) as pilot:
        # /advisor configure & reset
        await app.handle_slash_command(SlashCommand("advisor", "model claude-sonnet"))
        await pilot.pause(0.05)
        st = app._get_advisor_state()
        assert st.config.advisor_model == "claude-sonnet"
        assert st.config.is_enabled is True

        await app.handle_slash_command(SlashCommand("advisor", "handoff on"))
        await pilot.pause(0.05)
        assert st.config.handoff_enabled is True

        # /btw does not mutate bridge.history or msg_mgr.messages
        orch = app._bridge._orchestrator
        hist_before = list(app._bridge.history)
        msg_before = list(orch.msg_mgr.messages) if getattr(orch, "msg_mgr", None) else []
        app._btw_call_fn = lambda messages, **kwargs: {"content": "Quick read-only answer."}
        await app.handle_slash_command(SlashCommand("btw", "What port does Ollama use?"))
        await pilot.pause(0.05)
        assert list(app._bridge.history) == hist_before
        if getattr(orch, "msg_mgr", None):
            assert list(orch.msg_mgr.messages) == msg_before
        chat_log = app.query_one("#chat_log", VerticalScroll)
        assert any(w.has_class("btw_msg") for w in chat_log.children)

        # /queue enqueue, ls, pop, clear
        await app.handle_slash_command(SlashCommand("queue", "first follow-up"))
        await app.handle_slash_command(SlashCommand("queue", "second follow-up"))
        await pilot.pause(0.05)
        assert app._queued_messages == ["first follow-up", "second follow-up"]

        await app.handle_slash_command(SlashCommand("queue", "pop"))
        await pilot.pause(0.05)
        assert app._queued_messages == ["first follow-up"]

        await app.handle_slash_command(SlashCommand("queue", "clear"))
        await pilot.pause(0.05)
        assert app._queued_messages == []


def test_cli_flags_parse_output_private_and_parallel() -> None:
    from src.main import _parse_args

    ns = _parse_args(["--task", "hi", "--output", "stream-json", "--private", "--parallel"])
    assert ns.output == "stream-json"
    assert ns.private is True
    assert ns.parallel is True
    assert callable(cli_main)
