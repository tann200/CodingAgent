"""Regression tests for TASK-20: Unified pre-write guardrail parity & MCP SSRF hardening."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.mcp.mcp_http_client import McpHttpClient
from src.core.mcp.mcp_sse_client import McpSseClient
from src.core.mcp.mcp_ws_client import McpWsClient
from src.core.orchestration.orchestrator import Orchestrator
from src.tools import _edit_tools, git_tools, patch_tools
from src.tools.guardrails import mark_file_read
from src.tools.web_tools import validate_mcp_url


def _setup_deny_write_repo(tmp_path: Path, filename: str = "locked.py") -> Path:
    ctx_dir = tmp_path / ".codingAgent"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / "config.json").write_text(
        json.dumps({"deny_write": [filename, "secrets/*"]}),
        encoding="utf-8",
    )
    target = tmp_path / filename
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    mark_file_read(str(target.resolve()))
    return target


class TestPreWriteGuardrailParity:
    def test_edit_tools_and_patch_tools_enforce_deny_write(self, tmp_path: Path):
        target = _setup_deny_write_repo(tmp_path, "locked.py")

        res_atomic = _edit_tools.edit_file_atomic(
            "locked.py", "alpha = 1", "alpha = 99", workdir=tmp_path
        )
        assert res_atomic["status"] == "error"
        assert "deny_write" in res_atomic["error"]

        res_line = _edit_tools.edit_by_line_range(
            "locked.py", 1, 1, "alpha = 99\n", workdir=tmp_path
        )
        assert res_line["status"] == "error"
        assert "deny_write" in res_line["error"]

        res_multi = _edit_tools.multiedit(
            "locked.py",
            [{"old_string": "alpha = 1", "new_string": "alpha = 99"}],
            workdir=tmp_path,
        )
        assert res_multi["status"] == "error"
        assert "deny_write" in res_multi["error"]

        patch_txt = "--- locked.py\n+++ locked.py\n@@ -1,2 +1,2 @@\n-alpha = 1\n+alpha = 99\n beta = 2\n"
        res_edit = _edit_tools.edit_file("locked.py", patch_txt, workdir=tmp_path)
        assert res_edit["status"] == "error"
        assert "deny_write" in res_edit["error"]

        res_apply = patch_tools.apply_patch("locked.py", patch_txt, workdir=tmp_path)
        assert res_apply["status"] == "error"
        assert "deny_write" in res_apply["error"]

        res_block = patch_tools.edit_code_block(
            "locked.py", "alpha = 1", "alpha = 99", workdir=str(tmp_path)
        )
        assert res_block["status"] == "error"
        assert "deny_write" in res_block["error"]

        # Verify file remained completely untouched
        assert target.read_text(encoding="utf-8") == "alpha = 1\nbeta = 2\n"

    def test_orchestrator_execute_tool_enforces_deny_write_live_path(self, tmp_path: Path):
        target = _setup_deny_write_repo(tmp_path, "locked.py")
        orch = Orchestrator(
            working_dir=str(tmp_path),
            allow_external_working_dir=True,
            trusted=True,
            defer_provider_probe=True,
        )
        try:
            orch._session_read_files.add(str(target.resolve()))
            res = orch.execute_tool(
                {
                    "name": "edit_file_atomic",
                    "arguments": {
                        "path": "locked.py",
                        "old_string": "alpha = 1",
                        "new_string": "alpha = 2",
                    },
                }
            )
            assert res.get("ok") is False or (res.get("result") or {}).get("status") == "error"
            combined = json.dumps(res)
            assert "deny_write" in combined
            assert target.read_text(encoding="utf-8") == "alpha = 1\nbeta = 2\n"
        finally:
            orch.close()

    def test_git_restore_enforces_confinement_and_deny_write(self, tmp_path: Path):
        _setup_deny_write_repo(tmp_path, "locked.py")

        with patch("src.tools.git_tools._run_git") as mock_git:
            res_traversal = git_tools.git_restore("../outside.py", workdir=tmp_path)
            assert res_traversal["status"] == "error"
            assert not mock_git.called

            res_denied = git_tools.git_restore("locked.py", workdir=tmp_path)
            assert res_denied["status"] == "error"
            assert "deny_write" in res_denied["error"]
            assert not mock_git.called

    def test_edit_tools_diff_line_counts_exclude_unified_headers(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        mark_file_read(str(f.resolve()))

        res_atomic = _edit_tools.edit_file_atomic(
            "sample.py", "b = 2", "b = 20", workdir=tmp_path
        )
        assert res_atomic["status"] == "ok"
        assert res_atomic["lines_added"] == 1
        assert res_atomic["lines_removed"] == 1

        res_line = _edit_tools.edit_by_line_range(
            "sample.py", 3, 3, "c = 30\n", workdir=tmp_path
        )
        assert res_line["status"] == "ok"
        assert res_line["lines_added"] == 1
        assert res_line["lines_removed"] == 1

        res_multi = _edit_tools.multiedit(
            "sample.py",
            [{"old_string": "a = 1", "new_string": "a = 10"}],
            workdir=tmp_path,
        )
        assert res_multi["status"] == "ok"
        assert res_multi["lines_added"] == 1
        assert res_multi["lines_removed"] == 1


class TestMcpRemoteSsrfProtection:
    @pytest.mark.parametrize(
        "bad_url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://0177.0.0.1:8080/mcp",
            "http://0x7f.0.0.1/mcp",
            "http://2130706433/mcp",
            "http://0.0.0.0:8080/mcp",
            "http://10.0.0.5/mcp",
            "http://192.168.1.10/mcp",
            "file:///etc/passwd",
            "ftp://example.com/mcp",
        ],
    )
    def test_validate_mcp_url_blocks_ssrf_and_obfuscated_loopback(self, bad_url: str):
        with pytest.raises(PermissionError):
            validate_mcp_url(bad_url, allowed_schemes=("http", "https"))

    def test_validate_mcp_url_allows_explicit_localhost_but_blocks_redirect_localhost(self):
        validate_mcp_url("http://localhost:3000/mcp", allowed_schemes=("http", "https"))
        validate_mcp_url("http://127.0.0.1:3000/mcp", allowed_schemes=("http", "https"))
        validate_mcp_url("ws://localhost:8000/ws", allowed_schemes=("ws", "wss"))

        with pytest.raises(PermissionError):
            validate_mcp_url(
                "http://localhost:3000/mcp",
                allowed_schemes=("http", "https"),
                allow_localhost=False,
            )

    def test_validate_mcp_url_fails_closed_on_dns_error(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("DNS failed")):
            with pytest.raises(PermissionError, match="DNS resolution failed"):
                validate_mcp_url("https://unresolvable.invalid/mcp")

    @pytest.mark.asyncio
    async def test_mcp_http_client_blocks_ssrf_and_redirects(self):
        bad_client = McpHttpClient(name="evil", url="http://169.254.169.254/latest/meta-data/")
        with pytest.raises(PermissionError):
            await bad_client.connect()

        redirect_resp = SimpleNamespace(
            status_code=302,
            headers={"Location": "http://169.254.169.254/"},
            raise_for_status=MagicMock(),
            json=MagicMock(return_value={}),
        )
        fake_httpx = MagicMock()
        fake_httpx.post = AsyncMock(return_value=redirect_resp)
        fake_httpx.aclose = AsyncMock()

        client = McpHttpClient(name="local", url="http://localhost:3000/mcp")
        with patch("httpx.AsyncClient", return_value=fake_httpx) as mock_cls:
            with pytest.raises(PermissionError, match="blocked HTTP redirect"):
                await client.connect()
            _, init_kwargs = mock_cls.call_args
            assert init_kwargs.get("follow_redirects") is False

    def test_mcp_sse_client_blocks_ssrf_url_synchronously(self):
        sse = McpSseClient(name="evil-sse", url="http://169.254.169.254/sse")
        with pytest.raises(PermissionError):
            sse.connect()

        obf = McpSseClient(name="obf-sse", url="http://0177.0.0.1:8000/sse")
        with pytest.raises(PermissionError):
            obf.connect()

    @pytest.mark.asyncio
    async def test_mcp_ws_client_blocks_ssrf_and_wrong_scheme(self):
        ws_meta = McpWsClient(name="evil-ws", url="ws://169.254.169.254/ws")
        with pytest.raises(PermissionError):
            await ws_meta.connect()

        ws_obf = McpWsClient(name="obf-ws", url="ws://0177.0.0.1:8000/ws")
        with pytest.raises(PermissionError):
            await ws_obf.connect()

        ws_http = McpWsClient(name="bad-scheme", url="http://localhost:8000/ws")
        with pytest.raises(PermissionError):
            await ws_http.connect()
