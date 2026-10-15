"""Tests for Universal Secret Detection, Protected Paths, and Redaction."""

import asyncio
from pathlib import Path
import pytest

from src.core.security.redaction import (
    contains_secrets,
    detect_secrets,
    is_secret_path,
    redact_data,
    redact_secrets,
    REDACTED_PLACEHOLDER,
)
from src.core.orchestration.workspace_guard import WorkspaceGuard
from src.tools._file_io import (
    read_file,
    read_file_chunk,
    read_file_bytes,
    tail_log_file,
    create_directory,
)
from src.tools.system_tools import grep
from src.core.orchestration.graph.nodes.tool_result_safety import sanitize_tool_result


# ==============================================================================
# 1. Protected Paths & Classification
# ==============================================================================


class TestProtectedSecretPaths:
    """Tests for is_secret_path classification."""

    @pytest.mark.parametrize(
        "path",
        [
            ".env",
            ".env.local",
            ".env.production",
            ".env.staging",
            ".env.secret",
            "/home/user/.env",
            "project/sub/.env.development",
            ".netrc",
            ".pgpass",
            ".pypirc",
            ".npmrc",
            "id_rsa",
            "id_rsa.pub",
            "id_ed25519",
            "id_ed25519.pub",
            "id_ecdsa",
            "id_dsa",
            "authorized_keys",
            "known_hosts",
            "credentials.json",
            "service-account.json",
            "client_secret.json",
            "token.json",
            "client_secret_12345.json",
            "cert.pem",
            "server.key",
            "archive.pkcs12",
            "bundle.p12",
            "cert.pfx",
            "app.keystore",
            "/home/user/.ssh/id_rsa",
            "/home/user/.ssh/config",
            ".aws/credentials",
            ".aws/config",
            ".kube/config",
            ".docker/config.json",
        ],
    )
    def test_secret_paths_classified_as_secret(self, path):
        assert is_secret_path(path) is True
        guard = WorkspaceGuard()
        assert guard.is_secret(path) is True
        assert guard.is_protected(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "src/main.py",
            "tests/test_api.py",
            "README.md",
            "requirements.txt",
            "package.json",
            "package-lock.json",
            "pyproject.toml",
            ".gitignore",
            ".env.example",
            "Dockerfile",
            "Makefile",
        ],
    )
    def test_non_secret_paths_not_classified_as_secret(self, path):
        assert is_secret_path(path) is False
        guard = WorkspaceGuard()
        assert guard.is_secret(path) is False

    def test_workspace_guard_operation_blocks_secret_read(self):
        guard = WorkspaceGuard()
        res = guard.guard_operation("read_file", ".env", user_approved=False)
        assert res["status"] == "error"
        assert res["secret"] is True
        assert res["requires_approval"] is True
        assert "restricted" in res["error"].lower()

    def test_workspace_guard_operation_allows_secret_read_with_approval(self):
        guard = WorkspaceGuard()
        res = guard.guard_operation("read_file", ".env", user_approved=True)
        assert res["status"] == "ok"
        assert res["secret"] is True
        assert res["user_approved"] is True


# ==============================================================================
# 2. Secret Pattern Detection & String Redaction
# ==============================================================================


class TestSecretPatternRedaction:
    """Tests for regex-based secret detection and text redaction."""

    def test_openai_api_key_redaction(self):
        text = "key is sk-proj-12345678901234567890123456789012."
        assert contains_secrets(text) is True
        assert "openai_proj_api_key" in detect_secrets(text)
        redacted = redact_secrets(text)
        assert "sk-proj-" not in redacted
        assert REDACTED_PLACEHOLDER in redacted

    def test_anthropic_api_key_redaction(self):
        text = "ANTHROPIC_API_KEY=sk-ant-api03-abcdef1234567890abcdef1234567890abcdef"
        assert contains_secrets(text) is True
        redacted = redact_secrets(text)
        assert "sk-ant-" not in redacted
        assert REDACTED_PLACEHOLDER in redacted

    def test_google_api_key_redaction(self):
        text = "key: AIzaSyD-abc123DEF456ghi789JKL012mno345"
        assert contains_secrets(text) is True
        assert "google_api_key" in detect_secrets(text)
        redacted = redact_secrets(text)
        assert "AIzaSyD" not in redacted
        assert REDACTED_PLACEHOLDER in redacted

    def test_github_token_redaction(self):
        text = "token = ghp_1234567890abcdefghijklmnopqrstuvwxyz12"
        assert contains_secrets(text) is True
        assert "github_token" in detect_secrets(text)
        redacted = redact_secrets(text)
        assert "ghp_" not in redacted
        assert REDACTED_PLACEHOLDER in redacted

    def test_aws_access_key_redaction(self):
        text = "AWS key: AKIAIOSFODNN7EXAMPLE"
        assert contains_secrets(text) is True
        assert "aws_access_key" in detect_secrets(text)
        redacted = redact_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        assert REDACTED_PLACEHOLDER in redacted

    def test_slack_token_redaction(self):
        slack_token = "xoxb" + "-1234567890-123456789012-abcdefghijklmnopqrstuvwx"
        text = f"token: {slack_token}"
        assert contains_secrets(text) is True
        assert "slack_token" in detect_secrets(text)
        redacted = redact_secrets(text)
        assert "xoxb" not in redacted
        assert REDACTED_PLACEHOLDER in redacted

    def test_bearer_token_redaction(self):
        text = "Authorization: Bearer mysecrettoken1234567890abcdef"
        assert contains_secrets(text) is True
        redacted = redact_secrets(text)
        assert "mysecrettoken" not in redacted
        assert "Authorization: Bearer [REDACTED]" in redacted

    def test_private_key_redaction(self):
        text = (
            "header\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0m...\n"
            "-----END RSA PRIVATE KEY-----\n"
            "footer"
        )
        assert contains_secrets(text) is True
        assert "private_key" in detect_secrets(text)
        redacted = redact_secrets(text)
        assert "-----BEGIN RSA PRIVATE KEY-----" not in redacted
        assert "MIIEowIBAAKCAQEA0m" not in redacted
        assert f"header\n{REDACTED_PLACEHOLDER}\nfooter" in redacted

    def test_uri_credentials_redaction(self):
        text = "db_url: postgres://dbuser:supersecretpass123@db.prod.internal:5432/app"
        assert contains_secrets(text) is True
        redacted = redact_secrets(text)
        assert "supersecretpass123" not in redacted
        assert "postgres://dbuser:[REDACTED]@db.prod.internal:5432/app" in redacted

    def test_kv_secret_assignment_redaction(self):
        text = 'password = "supersecretpassword123"\nSECRET_KEY=abcdef123456789'
        assert contains_secrets(text) is True
        redacted = redact_secrets(text)
        assert "supersecretpassword123" not in redacted
        assert "abcdef123456789" not in redacted
        assert 'password = "[REDACTED]"' in redacted


# ==============================================================================
# 3. Recursive Data Redaction
# ==============================================================================


class TestRecursiveDataRedaction:
    """Tests for redact_data on complex, nested structures."""

    def test_nested_dict_and_list_redaction(self):
        data = {
            "name": "service_config",
            "secrets": [
                "sk-proj-12345678901234567890123456789012",
                {"api_key": "custom_api_key_value_999"},
            ],
            "auth": {
                "password": "plain_password_here",
                "nested": {
                    "token": "token_value_abc_123",
                    "safe": "normal_data",
                },
            },
        }
        redacted = redact_data(data)
        assert "sk-proj" not in str(redacted)
        assert "custom_api_key_value_999" not in str(redacted)
        assert "plain_password_here" not in str(redacted)
        assert "token_value_abc_123" not in str(redacted)
        assert redacted["auth"]["nested"]["safe"] == "normal_data"
        assert redacted["auth"]["password"] == REDACTED_PLACEHOLDER
        assert redacted["auth"]["nested"]["token"] == REDACTED_PLACEHOLDER

    def test_cyclic_data_structure(self):
        data: dict = {"key": "safe_value"}
        data["self"] = data
        redacted = redact_data(data)
        assert redacted["key"] == "safe_value"
        assert "self" in redacted


# ==============================================================================
# 4. File Tools Denial & Redaction
# ==============================================================================


class TestFileToolsSecretProtection:
    """Tests that file I/O tools enforce secret boundaries and redact secrets."""

    def test_read_file_denies_secret_path(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("API_KEY=sk-proj-12345678901234567890123456789012")

        res = read_file(".env", workdir=tmp_path)
        assert res["status"] == "error"
        assert "protected secret file" in res["error"].lower()

    def test_read_file_redacts_secrets_in_normal_file(self, tmp_path):
        config_file = tmp_path / "config.txt"
        config_file.write_text(
            "Service key: sk-proj-12345678901234567890123456789012\nNormal text"
        )

        res = read_file("config.txt", workdir=tmp_path)
        assert res["status"] == "ok"
        assert "sk-proj-" not in res["content"]
        assert REDACTED_PLACEHOLDER in res["content"]
        assert "Normal text" in res["content"]

    def test_read_file_chunk_denies_secret_path(self, tmp_path):
        key_file = tmp_path / "id_rsa"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----")

        res = read_file_chunk("id_rsa", offset=0, limit=100, workdir=tmp_path)
        assert res["status"] == "error"
        assert "protected secret file" in res["error"].lower()

    def test_read_file_chunk_bounds_limit_and_redacts(self, tmp_path):
        f = tmp_path / "data.log"
        f.write_text("API_KEY=sk-proj-12345678901234567890123456789012\nLine 2")

        # limit=-1 should be clamped to chunk max bytes (64KB), not read unbounded
        res = read_file_chunk("data.log", offset=0, limit=-1, workdir=tmp_path)
        assert res["status"] == "ok"
        assert res["limit"] == 65_536
        assert "sk-proj-" not in res["content"]
        assert REDACTED_PLACEHOLDER in res["content"]

    def test_read_file_bytes_denies_secret_path(self, tmp_path):
        cert_file = tmp_path / "server.key"
        cert_file.write_bytes(b"dummy key content")

        res = read_file_bytes("server.key", workdir=tmp_path)
        assert res["status"] == "error"
        assert "protected secret file" in res["error"].lower()

    def test_tail_log_file_denies_secret_path(self, tmp_path):
        netrc_file = tmp_path / ".netrc"
        netrc_file.write_text("machine example.com login user password secretpass")

        res = tail_log_file(".netrc", workdir=tmp_path)
        assert res["status"] == "error"
        assert "protected secret file" in res["error"].lower()

    def test_tail_log_file_redacts_secrets(self, tmp_path):
        log_file = tmp_path / "app.log"
        log_file.write_text(
            "INFO 2026-10-15 Token used: ghp_1234567890abcdefghijklmnopqrstuvwxyz12\n"
        )

        res = tail_log_file("app.log", workdir=tmp_path)
        assert res["status"] == "ok"
        assert "ghp_" not in res["content"]
        assert REDACTED_PLACEHOLDER in res["content"]

    def test_grep_denies_secret_path(self, tmp_path):
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "id_rsa").write_text("private key")

        res = grep("private", path=".ssh/id_rsa", workdir=tmp_path)
        assert res.get("status") == "error" or not res.get("ok", True)
        assert "protected secret file" in str(res).lower()

    def test_grep_redacts_secret_matches(self, tmp_path):
        src_file = tmp_path / "api.py"
        src_file.write_text("API_KEY = 'sk-proj-12345678901234567890123456789012'\n")

        res = grep("API_KEY", path="api.py", workdir=tmp_path)
        assert res.get("ok") is True or res.get("status") == "ok"
        result_data = res.get("data") or res.get("result") or res
        assert "sk-proj-" not in str(result_data)
        assert REDACTED_PLACEHOLDER in str(result_data)


# ==============================================================================
# 5. Pipeline & Frontier Node Sanitization
# ==============================================================================


class TestPipelineAndNodeSanitization:
    """Tests universal redaction in the tool execution pipeline and safety nodes."""

    def test_sanitize_tool_result_redacts_and_truncates(self):
        raw_result = {
            "status": "ok",
            "content": "Secret key: sk-proj-12345678901234567890123456789012",
            "api_key": "some_other_secret_value",
        }
        sanitized = sanitize_tool_result(raw_result)
        assert "sk-proj-" not in str(sanitized)
        assert sanitized["api_key"] == REDACTED_PLACEHOLDER
        assert REDACTED_PLACEHOLDER in sanitized["content"]

    def test_pipeline_execution_trace_and_session_redacted(self, tmp_path):
        from unittest.mock import MagicMock, patch
        from src.core.orchestration.tool_execution_pipeline import execute_tool_impl

        mock_orch = MagicMock()
        mock_orch.working_dir = tmp_path
        mock_orch._session_read_files = set()
        mock_orch._session_modified_files = set()
        mock_orch._usage_buffer = {}
        mock_orch.cost_tracker = MagicMock()
        mock_orch.event_bus = MagicMock()
        mock_orch.session_store = MagicMock()
        mock_orch._dry_run = False
        mock_orch.explore_mode = False
        mock_orch.plan_mode = None
        mock_orch.current_role = None
        mock_orch.active_agent = None
        mock_orch.role_manager = None
        mock_orch._tool_executor = None
        mock_orch._append_execution_trace = MagicMock()
        mock_orch._normalize_tool_result = lambda r: r
        mock_orch._normalize_args = lambda a: a
        mock_orch._get_tool_timeout = MagicMock(return_value=0)
        mock_orch._sync_session_state = MagicMock()
        mock_orch.permission_policy = MagicMock()
        mock_orch.permission_policy.is_denied = MagicMock(return_value=False)

        fn = MagicMock(return_value={"status": "ok", "content": "normal"})
        mock_orch.tool_registry = MagicMock()
        mock_orch.tool_registry.get = MagicMock(return_value={"fn": fn, "side_effects": []})

        tool_call = {
            "name": "my_test_tool",
            "arguments": {
                "param": "hello",
                "api_key": "should_be_redacted_key_value",
            },
        }

        with (
            patch(
                "src.core.orchestration.tool_execution_pipeline.get_tool_contract",
                return_value=None,
            ),
            patch(
                "src.core.orchestration.tool_execution_pipeline.PERMISSION_REQUIRED_TOOLS",
                set(),
            ),
        ):
            res = execute_tool_impl(mock_orch, tool_call)

        assert res.get("ok") is True

        # Check trace recorded redacted args
        mock_orch._append_execution_trace.assert_called_once()
        trace_args = mock_orch._append_execution_trace.call_args[0][0]["args"]
        assert trace_args["api_key"] == REDACTED_PLACEHOLDER

        # Check session store recorded redacted args
        mock_orch.session_store.add_tool_call.assert_called_once()
        session_args = mock_orch.session_store.add_tool_call.call_args[1]["args"]
        assert session_args["api_key"] == REDACTED_PLACEHOLDER


# ==============================================================================
# 6. Context Builder & Prompt Sanitization
# ==============================================================================


class TestContextBuilderSanitization:
    """Tests that ContextBuilder redacts secrets before building prompts."""

    def test_build_prompt_redacts_secrets_in_messages_and_task(self, tmp_path):
        from src.core.context.context_builder import ContextBuilder

        cb = ContextBuilder(working_dir=tmp_path)
        conversation = [
            {
                "role": "user",
                "content": "My secret is sk-proj-12345678901234567890123456789012",
            },
            {
                "role": "assistant",
                "content": "I see your token ghp_1234567890abcdefghijklmnopqrstuvwxyz12",
            },
        ]
        task = "Process secret: AKIAIOSFODNN7EXAMPLE"

        messages = cb.build_prompt(
            role_name="coder",
            active_skills=[],
            task_description=task,
            tools=[],
            conversation=conversation,
        )

        all_text = " ".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))
        assert "sk-proj-" not in all_text
        assert "ghp_" not in all_text
        assert "AKIAIOSFODNN7EXAMPLE" not in all_text
        assert REDACTED_PLACEHOLDER in all_text


# ==============================================================================
# 7. Session Store Sanitization (SQLite + JSONL)
# ==============================================================================


class TestSessionStoreSanitization:
    """Tests that SQLite and JSONL session stores scrub secrets on persistence."""

    def test_sqlite_session_store_redaction(self, tmp_path):
        from src.core.memory.sqlite_session_store import SqliteSessionStore

        store = SqliteSessionStore(workdir=str(tmp_path))

        store.add_message(
            "sess-1",
            "user",
            "Key: sk-proj-12345678901234567890123456789012 and password: mysecretpassword123",
        )
        messages = store.get_messages("sess-1")
        assert len(messages) == 1
        assert "sk-proj-" not in messages[0]["content"]
        assert "mysecretpassword123" not in messages[0]["content"]
        assert REDACTED_PLACEHOLDER in messages[0]["content"]

        store.add_tool_call(
            "sess-1",
            "write_file",
            args={"path": "foo.py", "password": "pass1234567890"},
            result={"status": "ok", "api_key": "secret_api_key_value"},
        )
        tool_calls = store.get_tool_calls("sess-1")
        assert len(tool_calls) == 1
        assert "pass1234567890" not in str(tool_calls[0])
        assert "secret_api_key_value" not in str(tool_calls[0])

    def test_jsonl_session_store_redaction(self, tmp_path):
        from src.core.memory.jsonl_session_store import JsonlSessionStore

        store = JsonlSessionStore(workdir=tmp_path)

        store.add_message(
            "sess-2",
            "user",
            "Token: ghp_1234567890abcdefghijklmnopqrstuvwxyz12",
        )
        messages = store.get_messages("sess-2")
        assert len(messages) == 1
        assert "ghp_" not in messages[0]["content"]
        assert REDACTED_PLACEHOLDER in messages[0]["content"]

        store.add_tool_call(
            "sess-2",
            "bash",
            args={"cmd": "curl -H 'Authorization: Bearer mysecrettoken1234567890abcdef'"},
            result={"output": "Returned: AIzaSyD-abc123DEF456ghi789JKL012mno345"},
        )
        tool_calls = store.get_tool_calls("sess-2")
        assert len(tool_calls) == 1
        assert "mysecrettoken" not in str(tool_calls[0])
        assert "AIzaSyD" not in str(tool_calls[0])


# ==============================================================================
# 8. Server SSE & WebSocket Sanitization
# ==============================================================================


class TestServerDeliverySanitization:
    """Tests that SSE and WebSocket streams redact payloads."""

    @pytest.mark.asyncio
    async def test_sse_adapter_redacts_events(self):
        import json
        from unittest.mock import MagicMock
        from src.server.sse_adapter import ServerEventBusAdapter

        mock_bus = MagicMock()
        subscribed_handlers = {}

        def fake_sub(event_name, handler):
            subscribed_handlers[event_name] = handler

        mock_bus.subscribe = fake_sub
        mock_bus.unsubscribe = MagicMock()

        adapter = ServerEventBusAdapter(event_bus=mock_bus, keepalive_interval=999)
        generator = adapter.event_generator("session-123")

        # Start generator
        gen_task = asyncio.create_task(generator.__anext__())
        await asyncio.sleep(0.01)

        # Trigger event with secret payload
        handler = subscribed_handlers.get("tool.end")
        if handler:
            handler({
                "session_id": "session-123",
                "tool": "read_file",
                "result": {
                    "content": "Secret sk-proj-12345678901234567890123456789012",
                    "api_key": "some_secret_key",
                },
            })

        sse_line = await asyncio.wait_for(gen_task, timeout=2.0)
        await generator.aclose()

        assert "sk-proj-" not in sse_line
        assert "some_secret_key" not in sse_line
        assert REDACTED_PLACEHOLDER in sse_line

