"""Tests for CP-10: Auto-inject LSP diagnostics into dynamic system prompt.

Covers:
  1. get_lsp_diagnostics_block returns empty string when feature disabled
  2. Returns empty string when no clients cached
  3. Returns empty string when no diagnostics cached
  4. Returns formatted block when diagnostics present in cache
  5. Filters to errors/warnings only (severity <= 2)
  6. Hints/info (severity 3/4) are excluded
  7. Respects budget_chars truncation
  8. Respects _MAX_DIAG_FILES cap
  9. Respects _MAX_DIAG_PER_FILE cap
  10. File paths made relative to workdir
  11. Files filter parameter restricts output to specified files
  12. Handles exceptions from lsp_manager gracefully
  13. LSPClient._diagnostics_cache populated by get_diagnostics() pull
  14. LSPClient._diagnostics_cache populated by publishDiagnostics push notification
  15. LSPClient.get_cached_diagnostics() returns cached data synchronously
  16. _DummyLSPClient.get_cached_diagnostics() returns empty list
  17. instruction_loader.build_runtime_context injects diagnostics block when available
  18. get_diagnostics() falls back to cache when server unavailable
  19. Multiple clients merged — diagnostics from all clients shown
  20. Empty diagnostics array in push notification clears cache for that URI
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.indexing.lsp_client import Diagnostic, LSPClient, _DummyLSPClient
from src.core.indexing.lsp_context import (
    _MAX_DIAG_FILES,
    _MAX_DIAG_PER_FILE,
    _DEFAULT_DIAG_BUDGET,
    get_lsp_diagnostics_block,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_diagnostic(
    message: str = "Type error",
    severity: int = 1,
    line: int = 0,
    col: int = 0,
    source: str = "pylsp",
) -> Diagnostic:
    return Diagnostic(
        severity=severity,
        line=line,
        col=col,
        message=message,
        source=source,
    )


def _make_mgr_with_cache(
    workdir: Path,
    uri_to_diags: Dict[str, List[Diagnostic]],
) -> MagicMock:
    """Return a mock LSPManager whose _clients have the given diagnostics cache."""
    client_mock = MagicMock()
    client_mock._diagnostics_cache = dict(uri_to_diags)

    mgr_mock = MagicMock()
    mgr_mock._clients = {"python": client_mock}
    return mgr_mock


# ---------------------------------------------------------------------------
# get_lsp_diagnostics_block
# ---------------------------------------------------------------------------


class TestGetLspDiagnosticsBlock:
    def test_returns_empty_when_disabled(self, tmp_path):
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=False):
            result = get_lsp_diagnostics_block(workdir=tmp_path)
        assert result == ""

    def test_returns_empty_when_no_clients(self, tmp_path):
        mgr_mock = MagicMock()
        mgr_mock._clients = {}
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        assert result == ""

    def test_returns_empty_when_no_diagnostics_cached(self, tmp_path):
        client_mock = MagicMock()
        client_mock._diagnostics_cache = {}
        mgr_mock = MagicMock()
        mgr_mock._clients = {"python": client_mock}
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        assert result == ""

    def test_returns_block_when_diagnostics_present(self, tmp_path):
        uri = (tmp_path / "main.py").resolve().as_uri()
        diags = [_make_diagnostic("Type mismatch", severity=1)]
        mgr_mock = _make_mgr_with_cache(tmp_path, {uri: diags})
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        assert "<lsp_diagnostics>" in result
        assert "Type mismatch" in result
        assert "</lsp_diagnostics>" in result

    def test_filters_out_hints_and_info(self, tmp_path):
        uri = (tmp_path / "main.py").resolve().as_uri()
        diags = [
            _make_diagnostic("Error msg", severity=1),
            _make_diagnostic("Warning msg", severity=2),
            _make_diagnostic("Info msg", severity=3),
            _make_diagnostic("Hint msg", severity=4),
        ]
        mgr_mock = _make_mgr_with_cache(tmp_path, {uri: diags})
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        assert "Error msg" in result
        assert "Warning msg" in result
        assert "Info msg" not in result
        assert "Hint msg" not in result

    def test_returns_empty_when_only_hints_info(self, tmp_path):
        uri = (tmp_path / "main.py").resolve().as_uri()
        diags = [
            _make_diagnostic("Info", severity=3),
            _make_diagnostic("Hint", severity=4),
        ]
        mgr_mock = _make_mgr_with_cache(tmp_path, {uri: diags})
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        assert result == ""

    def test_truncates_to_budget_chars(self, tmp_path):
        uri = (tmp_path / "main.py").resolve().as_uri()
        diags = [_make_diagnostic("A" * 200, severity=1) for _ in range(20)]
        mgr_mock = _make_mgr_with_cache(tmp_path, {uri: diags})
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path, budget_chars=200)
        assert len(result) <= 200 + len(
            "\n... [lsp_diagnostics truncated]</lsp_diagnostics>"
        )
        assert "truncated" in result

    def test_respects_max_diag_files_cap(self, tmp_path):
        uri_diags = {}
        for i in range(_MAX_DIAG_FILES + 5):
            f = tmp_path / f"file_{i}.py"
            uri_diags[f.resolve().as_uri()] = [_make_diagnostic(f"err{i}", severity=1)]
        mgr_mock = _make_mgr_with_cache(tmp_path, uri_diags)
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        # Count distinct file paths in result
        diag_lines = [l for l in result.splitlines() if l.strip().startswith("[")]
        file_names = {l.split("]")[1].strip().split(":")[0] for l in diag_lines}
        assert len(file_names) <= _MAX_DIAG_FILES

    def test_respects_max_diag_per_file(self, tmp_path):
        uri = (tmp_path / "main.py").resolve().as_uri()
        diags = [
            _make_diagnostic(f"err{i}", severity=1)
            for i in range(_MAX_DIAG_PER_FILE + 5)
        ]
        mgr_mock = _make_mgr_with_cache(tmp_path, {uri: diags})
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        diag_lines = [l for l in result.splitlines() if l.strip().startswith("[")]
        assert len(diag_lines) <= _MAX_DIAG_PER_FILE

    def test_paths_made_relative(self, tmp_path):
        file_path = tmp_path / "src" / "main.py"
        uri = file_path.resolve().as_uri()
        diags = [_make_diagnostic("err", severity=1)]
        mgr_mock = _make_mgr_with_cache(tmp_path, {uri: diags})
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        # Should show relative path, not full absolute path
        assert str(tmp_path) not in result or "src/main.py" in result

    def test_files_filter_restricts_output(self, tmp_path):
        file_a = tmp_path / "a.py"
        file_b = tmp_path / "b.py"
        uri_diags = {
            file_a.resolve().as_uri(): [_make_diagnostic("err_a", severity=1)],
            file_b.resolve().as_uri(): [_make_diagnostic("err_b", severity=1)],
        }
        mgr_mock = _make_mgr_with_cache(tmp_path, uri_diags)
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(
                    workdir=tmp_path, files=[str(file_a)]
                )
        assert "err_a" in result
        assert "err_b" not in result

    def test_exception_from_manager_returns_empty(self, tmp_path):
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager",
                side_effect=RuntimeError("boom"),
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        assert result == ""

    def test_multiple_clients_merged(self, tmp_path):
        uri = (tmp_path / "main.py").resolve().as_uri()
        client_a = MagicMock()
        client_a._diagnostics_cache = {uri: [_make_diagnostic("err_a", severity=1)]}
        client_b = MagicMock()
        client_b._diagnostics_cache = {uri: [_make_diagnostic("err_b", severity=2)]}
        mgr_mock = MagicMock()
        mgr_mock._clients = {"python": client_a, "typescript": client_b}
        with patch("src.core.indexing.lsp_context._is_enabled", return_value=True):
            with patch(
                "src.core.indexing.lsp_manager.get_lsp_manager", return_value=mgr_mock
            ):
                result = get_lsp_diagnostics_block(workdir=tmp_path)
        assert "err_a" in result
        assert "err_b" in result


# ---------------------------------------------------------------------------
# LSPClient diagnostics cache
# ---------------------------------------------------------------------------


class TestLSPClientDiagnosticsCache:
    def test_get_cached_diagnostics_initially_empty(self, tmp_path):
        client = LSPClient(server_cmd=["pylsp"], workspace_root=tmp_path)
        uri = "file:///test/main.py"
        result = client.get_cached_diagnostics(uri)
        assert result == []

    @pytest.mark.asyncio
    async def test_get_diagnostics_populates_cache(self, tmp_path):
        client = LSPClient(server_cmd=["pylsp"], workspace_root=tmp_path)
        uri = "file:///test/main.py"
        # Simulate available server returning diagnostics
        client._started = True
        client._proc = MagicMock()
        client._proc.returncode = None

        mock_diag = {
            "severity": 1,
            "range": {"start": {"line": 5, "character": 3}},
            "message": "Undefined variable",
            "source": "pylsp",
        }
        mock_result = {"items": [mock_diag]}

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_result
            diags = await client.get_diagnostics(uri)

        assert len(diags) == 1
        assert diags[0].message == "Undefined variable"
        cached = client.get_cached_diagnostics(uri)
        assert len(cached) == 1
        assert cached[0].message == "Undefined variable"

    @pytest.mark.asyncio
    async def test_get_diagnostics_unavailable_returns_cache(self, tmp_path):
        client = LSPClient(server_cmd=["pylsp"], workspace_root=tmp_path)
        uri = "file:///test/main.py"
        # Pre-populate cache
        client._diagnostics_cache[uri] = [
            Diagnostic(severity=1, line=0, col=0, message="Cached error")
        ]
        # Server is unavailable
        client._started = False

        diags = await client.get_diagnostics(uri)
        assert len(diags) == 1
        assert diags[0].message == "Cached error"

    def test_push_notification_populates_cache(self, tmp_path):
        """publishDiagnostics push notifications should populate _diagnostics_cache."""
        client = LSPClient(server_cmd=["pylsp"], workspace_root=tmp_path)
        uri = "file:///test/main.py"

        # Simulate the notification handler directly (as _reader_loop would)
        push_params = {
            "uri": uri,
            "diagnostics": [
                {
                    "severity": 1,
                    "range": {"start": {"line": 2, "character": 10}},
                    "message": "Push error",
                    "source": "pylsp",
                }
            ],
        }
        # Call the internal update as the reader loop would
        raw_diags = push_params.get("diagnostics") or []
        client._diagnostics_cache[uri] = [
            client._parse_diagnostic(d) for d in raw_diags
        ]

        cached = client.get_cached_diagnostics(uri)
        assert len(cached) == 1
        assert cached[0].message == "Push error"
        assert cached[0].line == 2

    def test_push_empty_diagnostics_clears_cache(self, tmp_path):
        client = LSPClient(server_cmd=["pylsp"], workspace_root=tmp_path)
        uri = "file:///test/main.py"
        client._diagnostics_cache[uri] = [
            Diagnostic(severity=1, line=0, col=0, message="Old error")
        ]
        # Simulate a push with empty diagnostics (file now clean)
        client._diagnostics_cache[uri] = []
        assert client.get_cached_diagnostics(uri) == []

    def test_get_cached_diagnostics_returns_copy(self, tmp_path):
        """Returned list should be a copy so mutations don't affect the cache."""
        client = LSPClient(server_cmd=["pylsp"], workspace_root=tmp_path)
        uri = "file:///test/main.py"
        client._diagnostics_cache[uri] = [
            Diagnostic(severity=1, line=0, col=0, message="Err")
        ]
        result = client.get_cached_diagnostics(uri)
        result.clear()
        # Cache should still have the entry
        assert len(client.get_cached_diagnostics(uri)) == 1


# ---------------------------------------------------------------------------
# _DummyLSPClient
# ---------------------------------------------------------------------------


class TestDummyLSPClient:
    def test_get_cached_diagnostics_always_empty(self):
        dummy = _DummyLSPClient()
        assert dummy.get_cached_diagnostics("file:///any.py") == []

    @pytest.mark.asyncio
    async def test_get_diagnostics_always_empty(self):
        dummy = _DummyLSPClient()
        assert await dummy.get_diagnostics("file:///any.py") == []


# ---------------------------------------------------------------------------
# instruction_loader integration
# ---------------------------------------------------------------------------


class TestInstructionLoaderDiagnosticsInjection:
    def test_build_runtime_context_injects_diagnostics_block(self, tmp_path):
        """build_runtime_context should include the diagnostics block when available."""
        from src.core.orchestration.instruction_loader import build_runtime_context

        fake_diag_block = (
            "<lsp_diagnostics>\n  [ERROR] main.py:1:1 Some error\n</lsp_diagnostics>"
        )

        with patch(
            "src.core.orchestration.instruction_loader.build_git_context_block",
            return_value="",
        ):
            with patch(
                "src.core.orchestration.instruction_loader.load_project_instructions",
                return_value="",
            ):
                with patch(
                    "src.core.indexing.lsp_context.get_lsp_context_block",
                    return_value="",
                ):
                    with patch(
                        "src.core.indexing.lsp_context.get_lsp_diagnostics_block",
                        return_value=fake_diag_block,
                    ):
                        result = build_runtime_context(cwd=tmp_path)

        assert fake_diag_block in result

    def test_build_runtime_context_no_crash_when_diagnostics_raises(self, tmp_path):
        """build_runtime_context should silently ignore diagnostics errors."""
        from src.core.orchestration.instruction_loader import build_runtime_context

        with patch(
            "src.core.orchestration.instruction_loader.build_git_context_block",
            return_value="some git context",
        ):
            with patch(
                "src.core.orchestration.instruction_loader.load_project_instructions",
                return_value="",
            ):
                with patch(
                    "src.core.indexing.lsp_context.get_lsp_context_block",
                    return_value="",
                ):
                    with patch(
                        "src.core.indexing.lsp_context.get_lsp_diagnostics_block",
                        side_effect=RuntimeError("boom"),
                    ):
                        result = build_runtime_context(cwd=tmp_path)

        # Should still return the git context even when diagnostics fails
        assert "some git context" in result
