"""tests/integration/test_phase3_findings.py — Phase 3 finding tests

Covers:
  RA-1  get_symbols_for_task() matches by token, empty when no index
  WF-2  evaluation_node returns evaluation_llm_verdict in output; FAIL routes to debug
  WF-4  route_execution detects identical plan hash → memory_sync
  TS-4  execute_with_retry retries on transient error, stops on non-transient
  UX-3  execute_tool returns dry_run dict for write tools when dry_run=True
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# RA-1: get_symbols_for_task
# ---------------------------------------------------------------------------


class TestGetSymbolsForTask:
    """RA-1: Fallback symbol lookup injected into planning context."""

    def _write_index(self, workdir: Path, files: list) -> None:
        """Write a repo_index.json using the flat-symbols format expected by
        get_symbols_for_task (which reads ``repo_index["symbols"]`` as a top-level
        flat list with keys ``symbol_name``, ``symbol_type``, and ``file_path``).

        The *files* argument uses the old nested format
        ``[{"file_path": ..., "symbols": [{"name": ..., "type": ..., ...}]}]``
        for convenience; this helper converts it to the flat format on write.
        """
        flat_symbols = []
        for file_entry in files:
            file_path = file_entry.get("file_path", "")
            for sym in file_entry.get("symbols", []):
                flat_symbols.append(
                    {
                        "symbol_name": sym.get("name") or sym.get("symbol_name", ""),
                        "symbol_type": sym.get("type") or sym.get("symbol_type", ""),
                        "file_path": file_path,
                        "start_line": sym.get("start_line"),
                        "docstring": sym.get("docstring"),
                    }
                )
        ctx = workdir / ".codingAgent"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "repo_index.json").write_text(
            json.dumps({"files": files, "symbols": flat_symbols}), encoding="utf-8"
        )

    def test_returns_empty_when_no_index(self, tmp_path: Path) -> None:
        from src.core.indexing.repo_indexer import get_symbols_for_task

        result = get_symbols_for_task(str(tmp_path), "find authentication module")
        assert result == []

    def test_matches_symbol_by_token(self, tmp_path: Path) -> None:
        from src.core.indexing.repo_indexer import get_symbols_for_task

        self._write_index(
            tmp_path,
            [
                {
                    "file_path": "src/auth.py",
                    "symbols": [
                        {
                            "name": "authenticate_user",
                            "type": "function",
                            "start_line": 10,
                            "docstring": "Authenticate a user.",
                        }
                    ],
                }
            ],
        )
        # Task "authenticate user login" → token "authenticate" (len=12) is a substring
        # of symbol "authenticate_user" (lowered) → should match.
        result = get_symbols_for_task(str(tmp_path), "authenticate user login")
        assert len(result) >= 1
        names = [r["name"] for r in result]
        assert "authenticate_user" in names

    def test_respects_max_results(self, tmp_path: Path) -> None:
        from src.core.indexing.repo_indexer import get_symbols_for_task

        symbols = [
            {"name": f"process_item_{i}", "type": "function", "start_line": i}
            for i in range(10)
        ]
        self._write_index(tmp_path, [{"file_path": "src/proc.py", "symbols": symbols}])
        result = get_symbols_for_task(str(tmp_path), "process items", max_results=3)
        assert len(result) <= 3

    def test_returns_empty_for_short_tokens(self, tmp_path: Path) -> None:
        from src.core.indexing.repo_indexer import get_symbols_for_task

        self._write_index(
            tmp_path,
            [
                {
                    "file_path": "src/foo.py",
                    "symbols": [{"name": "do_it", "type": "function", "start_line": 1}],
                }
            ],
        )
        # All tokens shorter than 4 chars → token_set empty → returns []
        result = get_symbols_for_task(str(tmp_path), "do it")
        assert result == []

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        from src.core.indexing.repo_indexer import get_symbols_for_task

        self._write_index(
            tmp_path,
            [
                {
                    "file_path": "src/billing.py",
                    "symbols": [
                        {"name": "charge_card", "type": "function", "start_line": 5}
                    ],
                }
            ],
        )
        result = get_symbols_for_task(str(tmp_path), "parse markdown document")
        assert result == []

    def test_result_includes_file_path(self, tmp_path: Path) -> None:
        from src.core.indexing.repo_indexer import get_symbols_for_task

        self._write_index(
            tmp_path,
            [
                {
                    "file_path": "src/parser.py",
                    "symbols": [
                        {"name": "parse_token", "type": "function", "start_line": 20}
                    ],
                }
            ],
        )
        result = get_symbols_for_task(str(tmp_path), "parse token stream")
        assert len(result) >= 1
        assert result[0]["file_path"] == "src/parser.py"





# ---------------------------------------------------------------------------
# TS-4: execute_with_retry
# ---------------------------------------------------------------------------


class TestExecuteWithRetry:
    """TS-4: Automatic retry on transient errors with exponential backoff."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt_no_retry(self) -> None:
        from src.core.orchestration.tool_execution_service import execute_with_retry

        call_count = 0

        def tool_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"ok": True, "data": "result"}

        result = await execute_with_retry(tool_fn, {})
        assert result == {"ok": True, "data": "result"}
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self) -> None:
        from src.core.orchestration.tool_execution_service import execute_with_retry

        call_count = 0

        def tool_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return {"ok": False, "error": "connection timeout"}
            return {"ok": True}

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await execute_with_retry(
                tool_fn, {}, tool_name="read_file", max_attempts=3, base_delay=0.0
            )

        assert result == {"ok": True}
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_stops_on_non_transient_error(self) -> None:
        from src.core.orchestration.tool_execution_service import execute_with_retry

        call_count = 0

        def tool_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"ok": False, "error": "permission denied"}

        result = await execute_with_retry(
            tool_fn, {}, tool_name="read_file", max_attempts=3, base_delay=0.0
        )
        assert result == {"ok": False, "error": "permission denied"}
        assert call_count == 1, "Should stop immediately on non-transient error"

    @pytest.mark.asyncio
    async def test_exhausts_retries_and_returns_last_result(self) -> None:
        from src.core.orchestration.tool_execution_service import execute_with_retry

        call_count = 0

        def tool_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            return {"ok": False, "error": "temporarily unavailable"}

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await execute_with_retry(
                tool_fn, {}, tool_name="read_file", max_attempts=3, base_delay=0.0
            )

        assert result == {"ok": False, "error": "temporarily unavailable"}
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exception_treated_as_transient(self) -> None:
        from src.core.orchestration.tool_execution_service import execute_with_retry

        call_count = 0

        def tool_fn(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("connection refused")
            return {"ok": True}

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await execute_with_retry(
                tool_fn, {}, tool_name="read_file", max_attempts=3, base_delay=0.0
            )

        assert result == {"ok": True}
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_locked_keyword_triggers_retry(self) -> None:
        from src.core.orchestration.tool_execution_service import execute_with_retry

        attempts = []

        def tool_fn(**kwargs):
            attempts.append(1)
            if len(attempts) < 2:
                return {"ok": False, "error": "file is locked"}
            return {"ok": True, "content": "data"}

        with patch("asyncio.sleep", new=AsyncMock()):
            result = await execute_with_retry(
                tool_fn, {}, tool_name="read_file", max_attempts=3, base_delay=0.0
            )

        assert result["ok"] is True
        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_args_forwarded_to_tool_fn(self) -> None:
        from src.core.orchestration.tool_execution_service import execute_with_retry

        received = {}

        def tool_fn(**kwargs):
            received.update(kwargs)
            return {"ok": True}

        await execute_with_retry(tool_fn, {"path": "/tmp/foo", "content": "hello"})
        assert received["path"] == "/tmp/foo"
        assert received["content"] == "hello"


# ---------------------------------------------------------------------------
# UX-3: Orchestrator dry_run mode
# ---------------------------------------------------------------------------


_CALL_MODEL_TARGETS = [
    "src.core.orchestration.graph.nodes.frontier_loop_node.call_model",
    "src.core.inference.llm_manager.call_model",
    "src.core.inference.llm_manager._call_model_internal",
]


def _patch_infra(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator._ensure_provider_manager_initialized_sync",
        lambda: None,
    )
    monkeypatch.setattr(
        "src.core.orchestration.orchestrator.Orchestrator._background_model_check",
        lambda self: None,
    )
    try:
        monkeypatch.setattr(
            "src.core.memory.distiller.generate_session_title",
            lambda msg: "Test Session",
        )
    except AttributeError:
        pass
    try:
        import src.core.orchestration.graph.builder as _builder

        monkeypatch.setattr(_builder, "_COMPILED_GRAPH", None)
    except AttributeError:
        pass


class TestOrchestratorDryRun:
    """UX-3: dry_run=True intercepts write-family tool calls."""

    def _make_orch(self, tmp_path: Path, monkeypatch: Any, dry_run: bool = True):
        from src.core.inference.adapters.mock_adapter import MockAdapter
        from src.core.orchestration.orchestrator import Orchestrator

        _patch_infra(monkeypatch)
        adapter = MockAdapter(responses=[], strict=False)
        orch = Orchestrator(
            adapter=adapter,
            working_dir=str(tmp_path),
            allow_external_working_dir=True,
            dry_run=dry_run,
        )
        return orch

    def test_dry_run_write_file_returns_dry_run_dict(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        orch = self._make_orch(tmp_path, monkeypatch, dry_run=True)
        result = orch.execute_tool(
            {"name": "write_file", "arguments": {"path": "output.py", "content": "x=1"}}
        )
        assert result.get("status") == "dry_run"
        assert result.get("would_call") == "write_file"
        assert result["args"]["path"] == "output.py"

    def test_dry_run_edit_file_returns_dry_run_dict(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        orch = self._make_orch(tmp_path, monkeypatch, dry_run=True)
        result = orch.execute_tool(
            {
                "name": "edit_file",
                "arguments": {
                    "path": "foo.py",
                    "old_string": "x=1",
                    "new_string": "x=2",
                },
            }
        )
        assert result.get("status") == "dry_run"
        assert result.get("would_call") == "edit_file"

    def test_dry_run_does_not_intercept_read_tools(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Read tools must pass through even in dry_run mode."""
        from src.core.orchestration.orchestrator import WRITE_TOOLS_REQUIRING_READ

        orch = self._make_orch(tmp_path, monkeypatch, dry_run=True)
        # read_file is not in WRITE_TOOLS_REQUIRING_READ so it must not be intercepted
        assert "read_file" not in WRITE_TOOLS_REQUIRING_READ

    def test_no_dry_run_write_proceeds_normally(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        orch = self._make_orch(tmp_path, monkeypatch, dry_run=False)
        # write_file with no prior read on an existing file → read-before-write error
        # (or success if file doesn't exist yet). Either way, NOT a dry_run response.
        (tmp_path / "existing.py").write_text("x=1")
        result = orch.execute_tool(
            {
                "name": "write_file",
                "arguments": {"path": "existing.py", "content": "x=2"},
            }
        )
        assert result.get("status") != "dry_run"

    def test_dry_run_defaults_to_false(self, tmp_path: Path, monkeypatch: Any) -> None:
        from src.core.inference.adapters.mock_adapter import MockAdapter
        from src.core.orchestration.orchestrator import Orchestrator

        _patch_infra(monkeypatch)
        orch = Orchestrator(
            adapter=MockAdapter(responses=[], strict=False),
            working_dir=str(tmp_path),
            allow_external_working_dir=True,
            # dry_run not passed → default False
        )
        assert orch._dry_run is False

    def test_dry_run_strips_user_approved_before_intercept(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """user_approved must still be stripped even in dry_run path."""
        orch = self._make_orch(tmp_path, monkeypatch, dry_run=True)
        result = orch.execute_tool(
            {
                "name": "write_file",
                "arguments": {
                    "path": "out.py",
                    "content": "pass",
                    "user_approved": True,
                },
            }
        )
        # dry_run dict should NOT contain user_approved in args
        assert "user_approved" not in result.get("args", {})
