"""tests/unit/test_tool_idempotency.py — P3.4 idempotency guarantees.

The audit flagged, as NOT AUDITED, "idempotency in particular (e.g. retry of a
non-idempotent write after a transport failure)". Two defects were confirmed by
execution and are pinned here:

1. ``_dispatch_tool_call`` runs tools on a ThreadPoolExecutor and calls
   ``future.cancel()`` on timeout, which returns ``False`` for an
   already-running future. The tool keeps executing and lands its side effect
   *after* the pipeline has reported failure — so a plain "failed" result
   invites the model to re-run the call and apply the change twice.
2. ``execute_with_retry`` retried blindly on any transient-looking error string,
   including for ``bash``/``git_commit``-style tools.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.orchestration.tool_execution_pipeline import _timeout_result
from src.tools.tools_config import IDEMPOTENT_TOOLS, is_idempotent_tool, resolve_tool_alias


# ---------------------------------------------------------------------------
# Classification — fail-closed allowlist, alias-resolved
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        "name",
        ["read_file", "grep", "glob", "git_status", "write_file", "edit_file", "multiedit"],
    )
    def test_reads_and_overwrites_are_idempotent(self, name):
        assert is_idempotent_tool(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "bash",
            "git_commit",
            "git_stash",
            "manage_todo",
            "record_mistake",
            "memory_save",
            "delegate_task",
            "create_state_checkpoint",
            "revert_last_tool",
            "send_user_message",
        ],
    )
    def test_accumulating_and_side_effecting_tools_are_not(self, name):
        assert is_idempotent_tool(name) is False

    def test_unknown_and_future_tools_fail_closed(self):
        assert is_idempotent_tool("some_tool_added_next_year") is False
        assert is_idempotent_tool("") is False

    @pytest.mark.parametrize("alias,canonical", [("run", "bash"), ("cat", "read_file")])
    def test_aliases_are_resolved_before_the_check(self, alias, canonical):
        """``run`` must not read as safe merely because no allowlist mentions it."""
        assert is_idempotent_tool(alias) is is_idempotent_tool(canonical)

    def test_the_registry_does_not_shadow_the_allowlist(self):
        assert IDEMPOTENT_TOOLS is not None
        assert "bash" not in IDEMPOTENT_TOOLS
        # resolve_tool_alias must not invent an escape hatch.
        assert resolve_tool_alias("bash") == "bash"


# ---------------------------------------------------------------------------
# Timeout: "unknown outcome" vs "failed"
# ---------------------------------------------------------------------------


class TestTimeoutResult:
    def test_an_idempotent_tool_reports_a_plain_timeout(self):
        out = _timeout_result("read_file", 30.0)
        assert out["ok"] is False
        assert out["error_code"] == "tool.timeout"
        assert "outcome_unknown" not in out

    def test_a_non_idempotent_tool_reports_an_unknown_outcome(self):
        out = _timeout_result("bash", 30.0)
        assert out["ok"] is False
        assert out["outcome_unknown"] is True
        assert out["error_code"] == "tool.outcome_unknown"

    def test_the_unknown_outcome_message_forbids_a_blind_retry(self):
        """The message is read by the model, so it must steer it away from re-running."""
        out = _timeout_result("bash", 30.0)
        err = out["error"].lower()
        assert "unknown" in err
        assert "not cancelled" in err or "not cancel" in err
        assert "twice" in err

    def test_an_overwrite_tool_keeps_the_plain_timeout(self):
        """``write_file`` re-applies the same bytes, so it is safe to re-run."""
        out = _timeout_result("write_file", 30.0)
        assert "outcome_unknown" not in out
        assert "twice" not in out["error"].lower()

    def test_an_alias_cannot_dodge_the_unknown_outcome(self):
        assert _timeout_result("run", 30.0)["error_code"] == "tool.outcome_unknown"
        assert _timeout_result("shell", 30.0)["error_code"] == "tool.outcome_unknown"

    def test_it_never_raises_without_the_tools_config(self):
        with patch(
            "src.tools.tools_config.is_idempotent_tool",
            side_effect=RuntimeError("config gone"),
        ):
            out = _timeout_result("bash", 5.0)
        assert out["error_code"] == "tool.outcome_unknown"


class TestTimeoutDoesNotCancelARunningTool:
    """The underlying fact that makes the message necessary."""

    def test_a_timed_out_tool_keeps_running_and_lands_its_effect(self):
        import concurrent.futures as cf
        import time

        landed = []

        def slow_side_effect():
            time.sleep(0.4)
            landed.append("done")
            return {"ok": True}

        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(slow_side_effect)
            with pytest.raises(cf.TimeoutError):
                fut.result(timeout=0.05)
            # This is the crux: cancel() cannot stop an already-running tool.
            assert fut.cancel() is False
            assert landed == []
            time.sleep(0.6)
            assert landed == ["done"]


# ---------------------------------------------------------------------------
# execute_with_retry — retries only for provably idempotent tools
# ---------------------------------------------------------------------------


class TestExecuteWithRetryIdempotency:
    @staticmethod
    async def _run(tool_fn, **kw):
        from src.core.orchestration.tool_execution_service import execute_with_retry

        with patch("asyncio.sleep", new=AsyncMock()):
            return await execute_with_retry(tool_fn, {}, base_delay=0.0, **kw)

    @pytest.mark.asyncio
    async def test_an_idempotent_tool_still_retries(self):
        calls = []

        def tool_fn(**_kw):
            calls.append(1)
            if len(calls) < 2:
                return {"ok": False, "error": "connection timeout"}
            return {"ok": True}

        out = await self._run(tool_fn, tool_name="read_file")
        assert out == {"ok": True}
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_a_non_idempotent_tool_is_run_once(self):
        calls = []

        def tool_fn(**_kw):
            calls.append(1)
            return {"ok": False, "error": "connection timeout"}

        out = await self._run(tool_fn, tool_name="bash")
        assert len(calls) == 1, "a retried bash could apply its side effect twice"
        assert out["error_code"] == "tool.retry_suppressed"
        assert out["retry_suppressed"] is True
        assert "IDEMPOTENT_TOOLS" in out["retry_suppressed_reason"]
        assert out["error"] == "connection timeout"

    @pytest.mark.asyncio
    async def test_a_missing_tool_name_fails_closed(self):
        calls = []

        def tool_fn(**_kw):
            calls.append(1)
            return {"ok": False, "error": "resource busy"}

        out = await self._run(tool_fn)
        assert len(calls) == 1
        assert out["error_code"] == "tool.retry_suppressed"

    @pytest.mark.asyncio
    async def test_a_suppressed_retry_does_not_mask_success(self):
        """A non-transient error still returns unchanged — no spurious annotation."""
        def tool_fn(**_kw):
            return {"ok": False, "error": "permission denied"}

        out = await self._run(tool_fn, tool_name="bash")
        assert out == {"ok": False, "error": "permission denied"}

    @pytest.mark.asyncio
    async def test_a_successful_call_is_untouched(self):
        def tool_fn(**_kw):
            return {"ok": True, "data": "x"}

        out = await self._run(tool_fn, tool_name="bash")
        assert out == {"ok": True, "data": "x"}

    @pytest.mark.asyncio
    async def test_aliases_resolve_before_the_idempotency_check(self):
        calls = []

        def tool_fn(**_kw):
            calls.append(1)
            return {"ok": False, "error": "timeout"}

        out = await self._run(tool_fn, tool_name="run")  # alias of bash
        assert len(calls) == 1
        assert out["error_code"] == "tool.retry_suppressed"

    @pytest.mark.asyncio
    async def test_max_attempts_of_one_is_respected(self):
        calls = []

        def tool_fn(**_kw):
            calls.append(1)
            return {"ok": False, "error": "timeout"}

        await self._run(tool_fn, tool_name="read_file", max_attempts=1)
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# Wiring: the pipeline uses the shared helper
# ---------------------------------------------------------------------------


class TestPipelineWiring:
    def test_the_timeout_path_calls_the_helper(self):
        import inspect

        from src.core.orchestration import tool_execution_pipeline as p

        src = inspect.getsource(p.execute_tool_impl)
        assert "_timeout_result(name, timeout_seconds)" in src

    def test_it_needs_no_tool_registry_or_llm(self):
        """A pure function of (name, timeout) — cheap enough to always call."""
        assert _timeout_result("git_commit", 1.0)["error_code"] == "tool.outcome_unknown"


def test_tool_registry_still_loads_after_the_new_constant():
    from src.tools import build_registry

    reg = build_registry()
    names = set(reg.list())
    # A representative member of each family must still be registered.
    for name in ("bash", "read_file", "write_file", "git_commit", "manage_todo"):
        assert name in names, name
    assert isinstance(MagicMock(), MagicMock)
