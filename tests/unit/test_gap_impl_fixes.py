"""
Regression tests for gap implementation bug fixes.

Covers:
- BUG-1: interaction_tools unsubscribe uses callback not None
- BUG-2: project_tools glob brace expansion workaround
- BUG-3: file_tools write_file hard size guard fires PRE-write
- BUG-4: ast_tools preserves comments when renaming
- BUG-5: guardrails uses contextvars (propagates across executor threads)
- BUG-6: todo_tools _save_todo renders new status fields
- BUG-7: todo_tools check action syncs status field
- BUG-8: web_tools blocks file:// and non-http schemes
- BUG-9: lint_dispatch Go build uses file directory not relative path
- BUG-10: lint_dispatch tsc includes --module/--target for standalone
- BUG-11: interaction_tools unsubscribe in finally (exception safety)
- BUG-12: session_registry redundant status assignment removed
- BUG-13: preview_service diff uses actual file path not workdir
- BUG-14: agent_session_manager singleton thread-safe (double-checked lock)
- BUG-15: file_lock_manager singleton thread-safe (double-checked lock)
- BUG-16: _tool.py VAR_KEYWORD params excluded by kind not name
- BUG-17: ast_tools dead _RenameTransformer class removed
- BUG-22: delete_file enforces read-before-write guardrail
- BUG-23: rename_file enforces read-before-write guardrail
- BUG-25: edit_code_block calls mark_file_read after reading
"""
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# BUG-1: interaction_tools — unsubscribe with callback object, not None
# ---------------------------------------------------------------------------
class TestInteractionToolsUnsubscribe:
    def test_ask_user_unsubscribes_with_callback(self):
        """subscribe() returns None; unsubscribe must receive the callback fn."""
        from src.tools.interaction_tools import ask_user

        subscribed_callbacks = []
        unsubscribed_callbacks = []

        def fake_subscribe(event, cb):
            subscribed_callbacks.append((event, cb))
            return None  # EventBus.subscribe returns None

        def fake_unsubscribe(event, cb):
            unsubscribed_callbacks.append((event, cb))

        def fake_publish(event, payload):
            # Simulate immediate reply so the tool doesn't block
            if event == "agent.waiting_for_user":
                # Find and call the subscribed callback
                for ev, cb in subscribed_callbacks:
                    if ev == "user.response":
                        cb({"answer": "42"})

        mock_bus = MagicMock()
        mock_bus.subscribe.side_effect = fake_subscribe
        mock_bus.unsubscribe.side_effect = fake_unsubscribe
        mock_bus.publish.side_effect = fake_publish

        with patch("src.core.orchestration.event_bus.get_event_bus", return_value=mock_bus):
            result = ask_user("What is 6x7?")

        assert result["status"] == "ok"
        assert result["answer"] == "42"

        # The unsubscribe call must pass the same callback object, not None
        assert len(unsubscribed_callbacks) == 1
        ev, cb = unsubscribed_callbacks[0]
        assert ev == "user.response"
        assert cb is not None, "unsubscribe was called with None — callback leak!"
        assert callable(cb), "unsubscribe must receive a callable"

    def test_submit_plan_for_review_unsubscribes_with_callback(self):
        from src.tools.interaction_tools import submit_plan_for_review

        subscribed_callbacks = []
        unsubscribed_callbacks = []

        def fake_subscribe(event, cb):
            subscribed_callbacks.append((event, cb))
            return None

        def fake_unsubscribe(event, cb):
            unsubscribed_callbacks.append((event, cb))

        def fake_publish(event, payload):
            if event == "agent.plan_review_requested":
                for ev, cb in subscribed_callbacks:
                    if ev == "plan_review.response":
                        cb({"decision": "approved", "feedback": ""})

        mock_bus = MagicMock()
        mock_bus.subscribe.side_effect = fake_subscribe
        mock_bus.unsubscribe.side_effect = fake_unsubscribe
        mock_bus.publish.side_effect = fake_publish

        with patch("src.core.orchestration.event_bus.get_event_bus", return_value=mock_bus):
            result = submit_plan_for_review("do stuff", ["step 1"])

        assert result["status"] == "ok"
        assert result["decision"] == "approved"

        ev, cb = unsubscribed_callbacks[0]
        assert cb is not None
        assert callable(cb)


# ---------------------------------------------------------------------------
# BUG-2: project_tools — brace expansion not supported by pathlib.glob
# ---------------------------------------------------------------------------
class TestProjectToolsTsDetection:
    def test_typescript_detected_without_brace_expansion(self, tmp_path):
        """TypeScript detection must work even though pathlib.glob doesn't
        support {ts,tsx} brace expansion."""
        # Create a .ts file under a subdirectory
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.ts").write_text("const x: number = 1;")
        (tmp_path / "package.json").write_text('{"name":"test"}')

        from src.tools.project_tools import fingerprint_tech_stack
        result = fingerprint_tech_stack(workdir=str(tmp_path))

        assert result["status"] == "ok"
        assert "typescript" in result["languages"], (
            "TypeScript not detected — brace expansion may be broken"
        )

    def test_tsx_detected(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"test"}')
        (tmp_path / "App.tsx").write_text("export default function App() {}")

        from src.tools.project_tools import fingerprint_tech_stack
        result = fingerprint_tech_stack(workdir=str(tmp_path))

        assert "typescript" in result["languages"]


# ---------------------------------------------------------------------------
# BUG-3: file_tools — size guard must block BEFORE write
# ---------------------------------------------------------------------------
class TestWriteFileSizeGuardPreWrite:
    def test_size_guard_blocks_before_write(self, tmp_path):
        """A 501-line write must be rejected WITHOUT the file being created."""
        from src.tools.file_tools import write_file

        target = tmp_path / "big.py"
        assert not target.exists()

        huge_content = "\n".join(f"x = {i}" for i in range(501))
        result = write_file(str(target), huge_content, workdir=tmp_path)

        assert result["status"] == "error"
        assert "500" in result["error"]
        # The file must NOT have been written
        assert not target.exists(), (
            "Size guard fired AFTER write — file was created despite the error"
        )

    def test_size_guard_allows_500_lines(self, tmp_path):
        """Exactly 500 lines should be allowed."""
        from src.tools.file_tools import write_file

        target = tmp_path / "ok.py"
        content = "\n".join(f"x = {i}" for i in range(500))
        result = write_file(str(target), content, workdir=tmp_path)

        assert result["status"] == "ok"
        assert target.exists()


# ---------------------------------------------------------------------------
# BUG-4: ast_tools — rename must preserve comments
# ---------------------------------------------------------------------------
class TestAstRenamePreservesComments:
    def test_rename_preserves_comments(self, tmp_path):
        """Renaming a function must not strip comments or blank lines."""
        src = tmp_path / "foo.py"
        src.write_text(
            "# This is a module comment\n"
            "\n"
            "def old_func(x):  # inline comment\n"
            "    # body comment\n"
            "    return old_func(x - 1) if x > 0 else x\n"
        )

        from src.tools.ast_tools import ast_rename
        result = ast_rename(str(src), "old_func", "new_func", workdir=str(tmp_path))

        assert result["status"] == "ok"
        assert result["changes_made"] is True

        new_content = src.read_text()
        assert "# This is a module comment" in new_content, "Module comment stripped!"
        assert "# inline comment" in new_content, "Inline comment stripped!"
        assert "# body comment" in new_content, "Body comment stripped!"
        assert "new_func" in new_content
        assert "old_func" not in new_content

    def test_rename_does_not_rename_inside_strings(self, tmp_path):
        """AST-guided rename must not rename symbols inside string literals."""
        src = tmp_path / "bar.py"
        src.write_text(
            'def old_func(): pass\n'
            'old_func()\n'
            'doc = "call old_func here"  # string — should not be renamed\n'
        )

        from src.tools.ast_tools import ast_rename
        result = ast_rename(str(src), "old_func", "new_func", workdir=str(tmp_path))

        assert result["status"] == "ok"
        content = src.read_text()
        # The string literal should be unchanged
        assert '"call old_func here"' in content, (
            "String literal was renamed — AST boundary check broken"
        )
        assert "def new_func" in content
        assert "new_func()" in content


# ---------------------------------------------------------------------------
# BUG-5: guardrails — ContextVar propagates across executor threads
# ---------------------------------------------------------------------------
class TestGuardrailsContextVar:
    def test_mark_and_check_same_thread(self, tmp_path):
        """Basic: mark then check in the same thread works."""
        from src.tools.guardrails import mark_file_read, check_read_before_write, reset_guardrail_state

        target = tmp_path / "x.py"
        target.write_text("x = 1")

        reset_guardrail_state()
        # Before marking — should fail
        result = check_read_before_write(str(target.resolve()))
        assert result.get("requires_read_first") is True

        mark_file_read(str(target.resolve()))
        # After marking — should pass
        result = check_read_before_write(str(target.resolve()))
        assert result == {}

    def test_global_set_visible_across_threads(self, tmp_path):
        """mark_file_read in main thread must be visible via global set in a worker thread.

        Python 3.11's run_in_executor does NOT propagate ContextVar to new threads,
        but the global Lock-protected set is always visible across all threads.
        """
        import concurrent.futures
        from src.tools.guardrails import mark_file_read, check_read_before_write, reset_guardrail_state

        target = tmp_path / "y.py"
        target.write_text("y = 2")

        reset_guardrail_state()
        mark_file_read(str(target.resolve()))  # written to global set

        results = {}

        def check_in_thread():
            # This thread has a fresh ContextVar context (no read in this thread),
            # but the global set was populated by mark_file_read above.
            results["check"] = check_read_before_write(str(target.resolve()))

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(check_in_thread)
            future.result()

        assert results.get("check") == {}, (
            "Global set not visible from worker thread — "
            "thread-local was used instead of a Lock-protected global set"
        )

    def test_reset_clears_state(self, tmp_path):
        from src.tools.guardrails import mark_file_read, check_read_before_write, reset_guardrail_state

        target = tmp_path / "z.py"
        target.write_text("z = 3")

        mark_file_read(str(target.resolve()))
        reset_guardrail_state()

        result = check_read_before_write(str(target.resolve()))
        assert result.get("requires_read_first") is True, (
            "reset_guardrail_state did not clear the read set"
        )


# ---------------------------------------------------------------------------
# BUG-6: todo_tools — _save_todo renders new status fields
# ---------------------------------------------------------------------------
class TestTodoStatusMarkdown:
    def _create_todo(self, tmp_path, steps):
        from src.tools.todo_tools import _save_todo
        _save_todo(str(tmp_path), steps)
        md = (tmp_path / ".agent-context" / "TODO.md").read_text()
        return md

    def test_in_progress_shows_tilde(self, tmp_path):
        steps = [{"description": "Do work", "done": False, "status": "in_progress", "depends_on": []}]
        md = self._create_todo(tmp_path, steps)
        assert "[~]" in md, f"Expected [~] for in_progress, got:\n{md}"

    def test_blocked_shows_exclamation(self, tmp_path):
        steps = [{"description": "Blocked step", "done": False, "status": "blocked",
                  "blocked_reason": "waiting on API", "depends_on": []}]
        md = self._create_todo(tmp_path, steps)
        assert "[!]" in md
        assert "waiting on API" in md

    def test_verified_shows_checkmark(self, tmp_path):
        steps = [{"description": "Verified", "done": True, "status": "verified", "depends_on": []}]
        md = self._create_todo(tmp_path, steps)
        assert "[✓]" in md

    def test_done_shows_x(self, tmp_path):
        steps = [{"description": "Done", "done": True, "status": "done", "depends_on": []}]
        md = self._create_todo(tmp_path, steps)
        assert "[x]" in md

    def test_pending_shows_empty(self, tmp_path):
        steps = [{"description": "Pending", "done": False, "status": "pending", "depends_on": []}]
        md = self._create_todo(tmp_path, steps)
        assert "[ ]" in md


# ---------------------------------------------------------------------------
# BUG-7: todo_tools — check action syncs status field
# ---------------------------------------------------------------------------
class TestTodoCheckSyncsStatus:
    def test_check_sets_status_done(self, tmp_path):
        from src.tools.todo_tools import manage_todo

        manage_todo("create", str(tmp_path), steps=["step 0", "step 1"])
        result = manage_todo("check", str(tmp_path), step_id=0)

        assert result["status"] == "ok"
        step = result["steps"][0]
        assert step["done"] is True
        assert step.get("status") == "done", (
            f"check action did not set status='done', got: {step.get('status')!r}"
        )


# ---------------------------------------------------------------------------
# BUG-8: web_tools — block non-http schemes
# ---------------------------------------------------------------------------
class TestWebToolsUrlBlocking:
    def test_file_scheme_blocked(self):
        from src.tools.web_tools import read_web_page
        result = read_web_page("file:///etc/passwd")
        assert result["status"] == "error"
        assert "private" in result["error"].lower() or "blocked" in result["error"].lower()

    def test_ftp_scheme_blocked(self):
        from src.tools.web_tools import read_web_page
        result = read_web_page("ftp://example.com/file.txt")
        assert result["status"] == "error"

    def test_http_not_blocked(self):
        from src.tools.web_tools import _is_url_blocked
        assert not _is_url_blocked("http://example.com/page")

    def test_https_not_blocked(self):
        from src.tools.web_tools import _is_url_blocked
        assert not _is_url_blocked("https://docs.python.org/3/")

    def test_localhost_blocked(self):
        from src.tools.web_tools import _is_url_blocked
        assert _is_url_blocked("http://localhost:8080/api")

    def test_private_ip_blocked(self):
        from src.tools.web_tools import _is_url_blocked
        assert _is_url_blocked("http://192.168.1.1/admin")
        assert _is_url_blocked("http://10.0.0.1/api")

    def test_metadata_endpoint_blocked(self):
        from src.tools.web_tools import _is_url_blocked
        assert _is_url_blocked("http://169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# BUG-9: lint_dispatch — Go build uses file directory
# ---------------------------------------------------------------------------
class TestLintDispatchGo:
    def test_go_lint_uses_file_directory_not_relative_path(self, tmp_path):
        """_lint_go must not raise ValueError when path is not relative to workdir."""
        from src.tools.lint_dispatch import _lint_go

        # Create a Go file in a subdirectory
        go_dir = tmp_path / "cmd" / "server"
        go_dir.mkdir(parents=True)
        go_file = go_dir / "main.go"
        go_file.write_text("package main\nimport \"fmt\"\nfunc main() { fmt.Println(\"hi\") }\n")

        # A different workdir that doesn't contain the file
        other_dir = tmp_path / "other"
        other_dir.mkdir()

        # This must NOT raise ValueError — old code used relative_to(workdir)
        try:
            result = _lint_go(str(go_file), other_dir, timeout=10)
            # Result should be a dict with lint_errors key (may be empty if go not installed)
            assert isinstance(result, dict)
            assert "lint_errors" in result
        except ValueError as e:
            pytest.fail(f"_lint_go raised ValueError: {e}")


# ---------------------------------------------------------------------------
# BUG-10: lint_dispatch — tsc includes --module and --target
# ---------------------------------------------------------------------------
class TestLintDispatchTs:
    def test_tsc_called_with_module_and_target(self, tmp_path):
        """tsc must be called with --module and --target for standalone file check."""
        from src.tools import lint_dispatch

        ts_file = tmp_path / "app.ts"
        ts_file.write_text("const x: number = 1;\n")

        captured_args = []

        def fake_run(cmd, **kwargs):
            captured_args.extend(cmd)
            m = MagicMock()
            m.stdout = ""
            m.stderr = ""
            m.returncode = 0
            return m

        with patch("src.tools.lint_dispatch.subprocess.run", side_effect=fake_run):
            lint_dispatch._lint_ts(str(ts_file), timeout=10)

        # Check that --module and --target were included
        assert "--module" in captured_args, f"--module missing from tsc call: {captured_args}"
        assert "--target" in captured_args, f"--target missing from tsc call: {captured_args}"
        assert "--noEmit" in captured_args


# ---------------------------------------------------------------------------
# BUG-11: interaction_tools — unsubscribe in finally (exception safety)
# ---------------------------------------------------------------------------
class TestInteractionToolsUnsubscribeOnException:
    def test_ask_user_unsubscribes_even_when_publish_raises(self):
        """If publish() raises, unsubscribe must still be called."""
        from src.tools.interaction_tools import ask_user

        unsubscribed_callbacks = []

        def fake_subscribe(event, cb):
            return None

        def fake_unsubscribe(event, cb):
            unsubscribed_callbacks.append((event, cb))

        def fake_publish(event, payload):
            raise RuntimeError("bus publish failed")

        mock_bus = MagicMock()
        mock_bus.subscribe.side_effect = fake_subscribe
        mock_bus.unsubscribe.side_effect = fake_unsubscribe
        mock_bus.publish.side_effect = fake_publish

        with patch("src.core.orchestration.event_bus.get_event_bus", return_value=mock_bus):
            result = ask_user("What?")

        # publish raised → result should be error
        assert result["status"] == "error"
        # But unsubscribe MUST still have been called
        assert len(unsubscribed_callbacks) == 1, (
            "unsubscribe not called when publish() raised — subscription leaked"
        )
        ev, cb = unsubscribed_callbacks[0]
        assert ev == "user.response"
        assert callable(cb)

    def test_submit_plan_for_review_unsubscribes_even_when_publish_raises(self):
        """If publish() raises, submit_plan_for_review must still unsubscribe."""
        from src.tools.interaction_tools import submit_plan_for_review

        unsubscribed_callbacks = []

        def fake_subscribe(event, cb):
            return None

        def fake_unsubscribe(event, cb):
            unsubscribed_callbacks.append((event, cb))

        def fake_publish(event, payload):
            raise RuntimeError("bus publish failed")

        mock_bus = MagicMock()
        mock_bus.subscribe.side_effect = fake_subscribe
        mock_bus.unsubscribe.side_effect = fake_unsubscribe
        mock_bus.publish.side_effect = fake_publish

        with patch("src.core.orchestration.event_bus.get_event_bus", return_value=mock_bus):
            result = submit_plan_for_review("do stuff", ["step 1"])

        assert result["status"] == "error"
        assert len(unsubscribed_callbacks) == 1, (
            "unsubscribe not called when publish() raised — subscription leaked"
        )
        ev, cb = unsubscribed_callbacks[0]
        assert ev == "plan_review.response"
        assert callable(cb)


# ---------------------------------------------------------------------------
# BUG-12: session_registry — redundant status assignment removed
# ---------------------------------------------------------------------------
class TestSessionRegistryStatusUpdate:
    def test_update_to_running_sets_status_once(self):
        """update_session_status(RUNNING) must set status correctly (no double-assign)."""
        from src.core.orchestration.session_registry import SessionRegistry, SessionStatus

        reg = SessionRegistry()
        reg.register_session("s1", metadata={"task": "test"})

        result = reg.update_session_status("s1", SessionStatus.RUNNING)
        assert result is True

        info = reg.get_session("s1")
        assert info.status == SessionStatus.RUNNING

    def test_update_to_failed_sets_status(self):
        """update_session_status(FAILED) must set status to FAILED."""
        from src.core.orchestration.session_registry import SessionRegistry, SessionStatus

        reg = SessionRegistry()
        reg.register_session("s2")

        reg.update_session_status("s2", SessionStatus.RUNNING)
        reg.update_session_status("s2", SessionStatus.FAILED, error="boom")

        info = reg.get_session("s2")
        assert info.status == SessionStatus.FAILED
        assert info.error_count == 1


# ---------------------------------------------------------------------------
# BUG-13: preview_service — diff uses actual file path, not workdir
# ---------------------------------------------------------------------------
class TestPreviewServiceDiffPath:
    def test_diff_labels_use_file_path(self, tmp_path):
        """Diff output must reference the file being modified, not the workdir."""
        from src.core.orchestration.preview_service import PreviewService

        # Reset singleton so we get a fresh instance for this test
        PreviewService._instance = None
        svc = PreviewService(workdir=str(tmp_path))

        preview = svc.generate_preview(
            tool_name="write_file",
            args={"path": "/some/project/src/foo.py"},
            old_content="x = 1\n",
            new_content="x = 2\n",
        )

        assert preview.diff != "", "Expected non-empty diff"
        assert "foo.py" in preview.diff, (
            f"Diff should reference the file name, got:\n{preview.diff}"
        )
        # Specifically should NOT show the workdir as the diff path
        assert str(tmp_path) not in preview.diff.splitlines()[0], (
            "Diff is labelling with workdir instead of the actual file path"
        )
        # Reset singleton
        PreviewService._instance = None

    def test_diff_empty_when_contents_identical(self, tmp_path):
        """No diff should be produced when old and new content are identical."""
        from src.core.orchestration.preview_service import PreviewService

        PreviewService._instance = None
        svc = PreviewService(workdir=str(tmp_path))
        preview = svc.generate_preview(
            tool_name="write_file",
            args={"path": "/some/file.py"},
            old_content="x = 1\n",
            new_content="x = 1\n",
        )
        assert preview.diff == ""
        PreviewService._instance = None


# ---------------------------------------------------------------------------
# BUG-14: agent_session_manager — singleton thread-safe
# ---------------------------------------------------------------------------
class TestAgentSessionManagerSingleton:
    def test_concurrent_get_instance_returns_same_object(self):
        """Multiple threads calling get_instance() simultaneously must get the same object."""
        from src.core.orchestration.agent_session_manager import AgentSessionManager

        # Reset singleton for a clean test
        original = AgentSessionManager._instance
        AgentSessionManager._instance = None

        instances = []
        errors = []

        def get_it():
            try:
                instances.append(AgentSessionManager.get_instance())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_it) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent get_instance: {errors}"
        assert len(set(id(i) for i in instances)) == 1, (
            "Multiple AgentSessionManager instances created — singleton not thread-safe"
        )

        # Restore original (if any)
        AgentSessionManager._instance = original


# ---------------------------------------------------------------------------
# BUG-15: file_lock_manager — singleton thread-safe
# ---------------------------------------------------------------------------
class TestFileLockManagerSingleton:
    def test_concurrent_get_instance_returns_same_object(self, tmp_path):
        """Multiple threads calling get_instance() simultaneously must get the same object."""
        from src.core.orchestration.file_lock_manager import FileLockManager

        original = FileLockManager._instance
        FileLockManager._instance = None

        instances = []
        errors = []

        def get_it():
            try:
                instances.append(FileLockManager.get_instance(workdir=str(tmp_path)))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=get_it) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent get_instance: {errors}"
        assert len(set(id(i) for i in instances)) == 1, (
            "Multiple FileLockManager instances created — singleton not thread-safe"
        )

        FileLockManager._instance = original


# ---------------------------------------------------------------------------
# BUG-16: _tool.py — VAR_KEYWORD params excluded by kind, not just by name
# ---------------------------------------------------------------------------
class TestToolSchemaVarKeyword:
    def test_custom_kwargs_name_excluded_from_schema(self):
        """**options (non-standard kwargs name) must not appear in JSON schema."""
        import inspect
        from src.tools._tool import tool as tool_decorator

        @tool_decorator(tags=["coding"])
        def my_func(x: str, **options) -> dict:
            """A tool with non-standard **kwargs name."""
            return {}

        schema = my_func.__tool_meta__.to_openai_schema()
        props = schema["function"]["parameters"]["properties"]
        assert "options" not in props, (
            "'**options' was included in the JSON schema — "
            "VAR_KEYWORD params must be excluded by kind, not just by name 'kwargs'"
        )
        assert "x" in props

    def test_var_positional_excluded_from_schema(self):
        """*args must not appear in JSON schema."""
        from src.tools._tool import tool as tool_decorator

        @tool_decorator(tags=["coding"])
        def my_func(x: str, *args) -> dict:
            """A tool with *args."""
            return {}

        schema = my_func.__tool_meta__.to_openai_schema()
        props = schema["function"]["parameters"]["properties"]
        assert "args" not in props, (
            "'*args' was included in the JSON schema — VAR_POSITIONAL must be excluded"
        )


# ---------------------------------------------------------------------------
# BUG-17: ast_tools — dead _RenameTransformer class removed
# ---------------------------------------------------------------------------
class TestAstToolsNoDeadCode:
    def test_rename_transformer_not_defined(self):
        """_RenameTransformer should not exist — it was dead code never used by ast_rename."""
        import src.tools.ast_tools as ast_mod
        assert not hasattr(ast_mod, "_RenameTransformer"), (
            "_RenameTransformer was supposed to be removed (dead code)"
        )

    def test_ast_rename_marks_file_read_before_write(self, tmp_path):
        """ast_rename must call mark_file_read so the guardrail is satisfied."""
        from src.tools.guardrails import reset_guardrail_state, check_read_before_write
        from src.tools.ast_tools import ast_rename

        src_file = tmp_path / "mod.py"
        src_file.write_text("def old_fn(): pass\n")

        reset_guardrail_state()
        # Before rename — guardrail should fire (not yet read in this session)
        pre = check_read_before_write(str(src_file.resolve()))
        assert pre.get("requires_read_first") is True

        ast_rename(str(src_file), "old_fn", "new_fn", workdir=str(tmp_path))

        # After rename — file should be marked as read in the global set
        post = check_read_before_write(str(src_file.resolve()))
        assert post == {}, "ast_rename did not call mark_file_read — guardrail still fires"

    def test_ast_rename_still_works_after_cleanup(self, tmp_path):
        """Removing _RenameTransformer must not break ast_rename functionality."""
        from src.tools.ast_tools import ast_rename

        src_file = tmp_path / "mod.py"
        src_file.write_text("def old_fn():\n    return old_fn()\n")

        result = ast_rename(str(src_file), "old_fn", "new_fn", workdir=str(tmp_path))
        assert result["status"] == "ok"
        assert "new_fn" in src_file.read_text()
        assert "old_fn" not in src_file.read_text()


class TestDeleteFileGuardrail:
    """BUG-22: delete_file must enforce read-before-write guardrail."""

    def test_delete_blocked_without_prior_read(self, tmp_path):
        """delete_file on an unread file must be rejected by the guardrail."""
        from src.tools.guardrails import reset_guardrail_state
        from src.tools.file_tools import delete_file

        target = tmp_path / "to_delete.py"
        target.write_text("x = 1\n")

        reset_guardrail_state()
        result = delete_file(str(target), workdir=tmp_path)
        assert result.get("status") == "error", (
            "delete_file should be blocked before reading the file"
        )
        assert result.get("requires_read_first") is True or "read" in result.get("error", "").lower()
        assert target.exists(), "delete_file must not delete the file when guardrail fires"

    def test_delete_allowed_after_prior_read(self, tmp_path):
        """delete_file on a read file must succeed."""
        from src.tools.guardrails import reset_guardrail_state, mark_file_read
        from src.tools.file_tools import delete_file

        target = tmp_path / "safe_to_delete.py"
        target.write_text("x = 1\n")

        reset_guardrail_state()
        mark_file_read(str(target.resolve()))
        result = delete_file(str(target), workdir=tmp_path)
        assert result.get("status") == "ok", (
            f"delete_file should succeed after marking as read, got: {result}"
        )


class TestRenameFileGuardrail:
    """BUG-23: rename_file must enforce read-before-write guardrail on src."""

    def test_rename_blocked_without_prior_read(self, tmp_path):
        """rename_file on an unread source must be rejected."""
        from src.tools.guardrails import reset_guardrail_state
        from src.tools.file_tools import rename_file

        src = tmp_path / "src.py"
        src.write_text("x = 1\n")
        dst = tmp_path / "dst.py"

        reset_guardrail_state()
        result = rename_file(str(src), str(dst), workdir=tmp_path)
        assert result.get("status") == "error", (
            "rename_file should be blocked before reading the source file"
        )
        assert src.exists(), "rename_file must not move the file when guardrail fires"
        assert not dst.exists()

    def test_rename_allowed_after_prior_read(self, tmp_path):
        """rename_file on a read source must succeed."""
        from src.tools.guardrails import reset_guardrail_state, mark_file_read
        from src.tools.file_tools import rename_file

        src = tmp_path / "src2.py"
        src.write_text("x = 1\n")
        dst = tmp_path / "dst2.py"

        reset_guardrail_state()
        mark_file_read(str(src.resolve()))
        result = rename_file(str(src), str(dst), workdir=tmp_path)
        assert result.get("status") == "ok", (
            f"rename_file should succeed after marking src as read, got: {result}"
        )
        assert dst.exists()


class TestEditCodeBlockGuardrail:
    """BUG-25: edit_code_block must call mark_file_read so write_file guardrail passes."""

    def test_edit_code_block_marks_file_read(self, tmp_path):
        """edit_code_block must mark the file as read before writing."""
        from src.tools.guardrails import reset_guardrail_state, check_read_before_write
        from src.tools.patch_tools import edit_code_block

        src = tmp_path / "module.py"
        src.write_text("def foo():\n    return 1\n")

        reset_guardrail_state()
        # Confirm guardrail would fire before edit_code_block is called
        pre = check_read_before_write(str(src.resolve()))
        assert pre.get("requires_read_first") is True

        result = edit_code_block(
            path=str(src),
            block_to_find="return 1",
            new_block="return 2",
            workdir=str(tmp_path),
        )
        assert result.get("status") == "ok", (
            f"edit_code_block should succeed (mark_file_read satisfies guardrail), got: {result}"
        )
        assert "return 2" in src.read_text()
