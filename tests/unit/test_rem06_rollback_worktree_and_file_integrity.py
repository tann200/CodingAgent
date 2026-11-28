"""Regression tests for REM-06: Binary rollback integrity, plan-mode batch resume,
inter-wave cancellation tool pairing, worktree subagents, and write_file edge cases."""

from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.orchestration.graph.nodes.frontier_completion import (
    build_plan_mode_blocked_exit,
)
from src.core.orchestration.graph.nodes.frontier_turn_helpers import (
    call_llm_for_turn_impl,
    dispatch_tool_calls_impl,
    resume_pending_action,
)
from src.core.orchestration.pipeline.dispatch import _inject_confined_workdir
from src.core.orchestration.pipeline.permissions import (
    _ACTIVE_WORKDIR_CTX,
    assert_lethal_trifecta_isolated,
)
from src.core.orchestration.rollback_manager import RollbackManager
from src.tools._edit_tools import _atomic_write, edit_file_atomic
from src.tools._file_io import write_file
from src.tools.guardrails import mark_file_read
from src.tools.subagent_payloads import DELEGATION_DENYLIST
from src.tools.subagent_tools import delegate_task


class TestRollbackBinaryAndPrivateIntegrity:
    def test_persist_snapshot_disk_preserves_binary_encoding(self, tmp_path: Path) -> None:
        text_file = tmp_path / "readme.txt"
        bin_file = tmp_path / "logo.bin"
        orig_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe\x00\x01"
        text_file.write_text("hello", encoding="utf-8")
        bin_file.write_bytes(orig_bytes)

        mgr = RollbackManager(str(tmp_path))
        snap_id = mgr.snapshot_files(["readme.txt", "logo.bin"])

        # Commit text_file -> triggers _persist_snapshot_disk for remaining logo.bin
        committed = mgr.commit_files(["readme.txt"], snapshot_id=snap_id)
        assert "readme.txt" in committed

        # Corrupt binary file and reload a fresh manager from disk
        bin_file.write_bytes(b"CORRUPTED")
        mgr2 = RollbackManager(str(tmp_path))
        res = mgr2.rollback(snapshot_id=snap_id)
        assert res["ok"] is True
        assert bin_file.read_bytes() == orig_bytes
        # No .backup pollution in workspace root
        assert not (tmp_path / "logo.bin.backup").exists()

    def test_private_rollback_manager_writes_nothing_to_disk(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("top-secret", encoding="utf-8")
        mgr = RollbackManager(str(tmp_path), private=True)
        snap_id = mgr.snapshot_files(["secret.txt"])
        mgr.append_to_snapshot(snap_id, "other.txt")
        f.write_text("modified", encoding="utf-8")
        res = mgr.rollback(snapshot_id=snap_id)
        assert res["ok"] is True
        assert f.read_text(encoding="utf-8") == "top-secret"
        assert not (tmp_path / ".codingAgent").exists()

    def test_selective_rollback_respects_path_boundaries(self, tmp_path: Path) -> None:
        bar = tmp_path / "bar.py"
        not_bar = tmp_path / "not_bar.py"
        bar.write_text("orig_bar", encoding="utf-8")
        not_bar.write_text("orig_not_bar", encoding="utf-8")

        mgr = RollbackManager(str(tmp_path))
        snap_id = mgr.snapshot_files(["bar.py", "not_bar.py"])
        bar.write_text("mod_bar", encoding="utf-8")
        not_bar.write_text("mod_not_bar", encoding="utf-8")

        res = mgr.rollback(snapshot_id=snap_id, paths=["bar.py"])
        assert res["ok"] is True
        assert bar.read_text(encoding="utf-8") == "orig_bar"
        # not_bar.py must NOT have been rolled back by endswith("bar.py")!
        assert not_bar.read_text(encoding="utf-8") == "mod_not_bar"


class TestFrontierPlanBatchCancelAndPolicyTier:
    def test_plan_mode_suspension_and_resume_preserves_full_batch(self) -> None:
        batch = [
            {"id": "call_1", "name": "read_file", "arguments": {"path": "a.py"}},
            {"id": "call_2", "name": "write_file", "arguments": {"path": "b.py", "content": "x"}},
        ]
        exit_state = build_plan_mode_blocked_exit(
            tool_name="write_file",
            tool_args={"path": "b.py", "content": "x"},
            history=[],
            tool_call_count=0,
            last_result=None,
            last_tool_name=None,
            errors=[],
            turns_taken=1,
            state={},
            pending_tool_calls=batch,
            assistant_content="Planning edits",
        )
        resumed_calls, norm_calls, new_hist = resume_pending_action(
            exit_state["next_action"], [], turns_taken=2, use_native_tools=True
        )
        assert len(resumed_calls) == 2
        assert len(norm_calls) == 2
        assert new_hist[-1]["role"] == "assistant"
        assert new_hist[-1]["content"] == "Planning edits"
        assert len(new_hist[-1]["tool_calls"]) == 2

    @pytest.mark.asyncio
    async def test_dispatch_inter_wave_cancel_emits_paired_tool_stubs(self) -> None:
        cancel = threading.Event()
        executed: list[str] = []

        async def _fake_exec(orch, tc, tier, **kw):
            executed.append(tc["name"])
            cancel.set()  # cancel after first wave
            return {"ok": True}

        norm_calls = [
            ("call_1", "write_file", {"path": "a.py", "content": "1"}, {"id": "call_1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}),
            ("call_2", "write_file", {"path": "b.py", "content": "2"}, {"id": "call_2", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}),
        ]
        history = [{"role": "assistant", "content": "", "tool_calls": [c[3] for c in norm_calls]}]
        out_hist, count, *_ = await dispatch_tool_calls_impl(
            tool_calls=[],
            history=history,
            state={"cancel_event": cancel},
            orchestrator=None,
            model_tier="frontier",
            bus=None,
            turns_taken=1,
            tool_call_count=0,
            max_tool_calls=10,
            iv_baselines={},
            sanitize_fn=lambda r: r,
            truncate_fn=lambda r: r,
            execute_raw_tool_fn=_fake_exec,
            refresh_index_fn=lambda *a, **k: None,
            use_native_tools=True,
            normalized_calls=norm_calls,
        )
        assert count == 1
        tool_msgs = [m for m in out_hist if m.get("role") == "tool"]
        assert {m["tool_call_id"] for m in tool_msgs} == {"call_1", "call_2"}
        stub = [m for m in tool_msgs if m["tool_call_id"] == "call_2"][0]
        assert "cancelled" in stub["content"].lower()

    @pytest.mark.asyncio
    async def test_call_llm_for_turn_extracts_policy_tier_from_dict(self) -> None:
        captured: dict = {}

        async def _fake_call(**kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}}]}

        await call_llm_for_turn_impl(
            messages=[{"role": "user", "content": "hi"}],
            provider_name="openai",
            model_name="gpt-4o",
            orchestrator=None,
            turns_taken=1,
            call_model_fn=_fake_call,
            runtime_policy={"policy_tier": "compact"},
            tools_schema=[],
        )
        assert captured.get("policy_tier") == "compact"


class TestWorktreeSubagentsAndLethalTrifecta:
    def test_delegation_denylist_includes_batch_and_lethal_trifecta_checks_metadata(self) -> None:
        assert "batch" in DELEGATION_DENYLIST
        with pytest.raises(PermissionError, match="Lethal-trifecta"):
            assert_lethal_trifecta_isolated(["read_file", "batch"], role="scout")
        with pytest.raises(PermissionError, match="Lethal-trifecta"):
            assert_lethal_trifecta_isolated(["read_file", "ast_rename"], role="scout")

    def test_delegate_task_rejects_unsafe_task_id(self, tmp_path: Path) -> None:
        res = delegate_task(
            role="scout",
            subtask_description="Inspect repo",
            working_dir=str(tmp_path),
            task_id="../escape",
        )
        assert "Invalid task_id" in res

    def test_contextvar_workdir_override_confines_dispatch_to_worktree(
        self, tmp_path: Path
    ) -> None:
        parent_dir = tmp_path / "main"
        wt_dir = tmp_path / "worktree"
        parent_dir.mkdir()
        wt_dir.mkdir()
        orch = SimpleNamespace(working_dir=str(parent_dir))

        def _tool(path: str, workdir: Path | None = None) -> dict:
            return {"workdir": workdir}

        tok = _ACTIVE_WORKDIR_CTX.set(wt_dir)
        try:
            err, args = _inject_confined_workdir(orch, _tool, {"path": "a.py"})
            assert err is None
            assert Path(args["workdir"]).resolve() == wt_dir.resolve()
        finally:
            _ACTIVE_WORKDIR_CTX.reset(tok)

    def test_delegate_task_operational_in_git_repo_uses_worktree_and_returns_diff(
        self, tmp_path: Path
    ) -> None:
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=str(tmp_path), check=True)
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)

        observed_wd: dict[str, Path] = {}

        async def _fake_ainvoke(state, config=None):
            wd = Path(state["working_dir"])
            observed_wd["state_wd"] = wd
            observed_wd["ctx_wd"] = _ACTIVE_WORKDIR_CTX.get()
            (wd / "app.py").write_text("x = 2\n", encoding="utf-8")
            return {"task": state["task"], "history": [{"role": "assistant", "content": "updated app.py"}], "errors": []}

        mock_graph = MagicMock()
        mock_graph.ainvoke = AsyncMock(side_effect=_fake_ainvoke)

        with patch("src.tools.subagent_tools._resolve_subagent_graph", return_value=mock_graph):
            res = delegate_task(
                role="operational",
                subtask_description="Update app.py",
                working_dir=str(tmp_path),
            )

        assert observed_wd["state_wd"].resolve() != tmp_path.resolve()
        assert observed_wd["ctx_wd"] == observed_wd["state_wd"]
        # Main working tree remained clean while diff stat was captured before cleanup
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 1\n"
        assert "app.py" in res
        assert "Worktree" in res


class TestWriteFileAndAtomicEditIntegrity:
    def test_write_file_creates_empty_new_file(self, tmp_path: Path) -> None:
        res = write_file("pkg/__init__.py", "", workdir=tmp_path)
        assert res["status"] == "ok"
        target = tmp_path / "pkg" / "__init__.py"
        assert target.exists()
        assert target.read_text(encoding="utf-8") == ""

    def test_write_file_does_not_create_parent_dir_when_validation_fails(
        self, tmp_path: Path
    ) -> None:
        huge = "\n".join(f"line_{i}" for i in range(600))
        res = write_file("orphan_dir/big.txt", huge, workdir=tmp_path)
        assert res["status"] == "error"
        assert not (tmp_path / "orphan_dir").exists()

    def test_write_file_and_atomic_write_preserve_file_mode(self, tmp_path: Path) -> None:
        script = tmp_path / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        os.chmod(script, 0o755)
        mark_file_read(str(script.resolve()))

        res = write_file("run.sh", "#!/bin/sh\necho bye\n", workdir=tmp_path)
        assert res["status"] == "ok"
        assert stat.S_IMODE(script.stat().st_mode) == 0o755

        _atomic_write(script, "#!/bin/sh\necho again\n")
        assert stat.S_IMODE(script.stat().st_mode) == 0o755

    def test_preview_gate_timeout_fails_closed_for_write_and_edit(
        self, tmp_path: Path
    ) -> None:
        f = tmp_path / "a.txt"
        f.write_text("old line\n", encoding="utf-8")
        mark_file_read(str(f.resolve()))

        fake_ev = MagicMock()
        fake_ev.wait.return_value = False  # simulate timeout
        fake_bus = MagicMock()
        fake_bus.has_subscribers.return_value = True

        with (
            patch("src.tools.tools_config.requires_preview_confirmation", return_value=True),
            patch("src.tools._file_io._get_bus", return_value=fake_bus),
            patch("src.tools._file_io._register_preview_gate", return_value=fake_ev),
            patch("src.tools._file_io._publish_diff_preview"),
            patch("src.tools._file_io._pop_preview_rejection", return_value=False),
        ):
            res_w = write_file("a.txt", "new line\n", workdir=tmp_path)
        assert res_w["status"] == "rejected"
        assert f.read_text(encoding="utf-8") == "old line\n"

        with (
            patch("src.tools.tools_config.requires_preview_confirmation", return_value=True),
            patch("src.core.orchestration.event_bus.get_event_bus", return_value=fake_bus),
            patch("src.tools._edit_tools.register_preview_gate", return_value=fake_ev),
            patch("src.tools._edit_tools._publish_diff_preview"),
            patch("src.tools._diff_gate.pop_preview_rejection", return_value=False),
        ):
            res_e = edit_file_atomic("a.txt", "old line", "new line", workdir=tmp_path)
        assert res_e["status"] == "rejected"
        assert f.read_text(encoding="utf-8") == "old line\n"
