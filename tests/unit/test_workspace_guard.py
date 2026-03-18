"""
Tests for WorkspaceGuard - protects critical files from modification.
"""

import pytest
from pathlib import Path
from src.core.orchestration.workspace_guard import WorkspaceGuard


class TestWorkspaceGuard:
    """Tests for WorkspaceGuard class."""

    def test_workspace_guard_import(self):
        """Test WorkspaceGuard can be imported."""
        from src.core.orchestration.workspace_guard import WorkspaceGuard

        assert WorkspaceGuard is not None

    def test_default_protected_patterns(self):
        """Test default protected patterns are defined."""
        from src.core.orchestration.workspace_guard import PROTECTED_PATTERNS

        assert ".git/" in PROTECTED_PATTERNS
        assert ".env" in PROTECTED_PATTERNS
        assert "requirements.txt" in PROTECTED_PATTERNS

    def test_guard_initialization(self):
        """Test WorkspaceGuard can be initialized."""
        guard = WorkspaceGuard()
        assert guard is not None

    def test_guard_with_custom_patterns(self):
        """Test WorkspaceGuard with custom patterns."""
        custom_patterns = [".secret", "config.yaml"]
        guard = WorkspaceGuard(protected_patterns=custom_patterns)

        assert guard is not None


class TestWorkspaceGuardOperations:
    """Tests for WorkspaceGuard operations."""

    def test_check_path_allowed(self, tmp_path):
        """Test allowed path passes guard."""
        guard = WorkspaceGuard()

        # Create a normal file
        test_file = tmp_path / "src" / "main.py"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("print('hello')")

        result = guard.guard_operation(
            "write_file", str(test_file), user_approved=False
        )

        # Normal files should be allowed
        assert (
            result.get("status") != "error" or result.get("requires_approval") is True
        )

    def test_check_protected_file(self, tmp_path):
        """Test protected file is blocked."""
        guard = WorkspaceGuard()

        # Try to access a protected file
        protected_path = tmp_path / ".env"
        protected_path.write_text("SECRET=123")

        result = guard.guard_operation(
            "write_file", str(protected_path), user_approved=False
        )

        # Should be blocked or require approval
        assert (
            result.get("status") == "error" or result.get("requires_approval") is True
        )

    def test_check_git_directory(self, tmp_path):
        """Test .git directory is protected."""
        guard = WorkspaceGuard()

        git_path = tmp_path / ".git" / "config"
        git_path.parent.mkdir(parents=True, exist_ok=True)
        git_path.write_text("")

        result = guard.guard_operation("write_file", str(git_path), user_approved=False)

        # Should be blocked
        assert result.get("status") == "error"

    def test_user_approval_overrides(self, tmp_path):
        """Test user approval bypasses guard."""
        guard = WorkspaceGuard()

        # Try with user approval
        result = guard.guard_operation("write_file", ".env", user_approved=True)

        # With approval, should pass
        assert result.get("status") != "error"

    def test_different_operations(self, tmp_path):
        """Test different operations are handled."""
        guard = WorkspaceGuard()

        # Test read operation
        result_read = guard.guard_operation("read_file", "main.py", user_approved=False)

        # Test write operation
        result_write = guard.guard_operation(
            "write_file", "main.py", user_approved=False
        )

        # Both should return a result dict
        assert isinstance(result_read, dict)
        assert isinstance(result_write, dict)


class TestWorkspaceGuardPatterns:
    """Tests for protected pattern matching."""

    def test_protected_patterns_list(self):
        """Test that protected patterns list is comprehensive."""
        from src.core.orchestration.workspace_guard import PROTECTED_PATTERNS

        # Check key patterns are present
        expected = [
            ".git/",
            ".env",
            "requirements.txt",
            "package-lock.json",
            "Dockerfile",
            "Makefile",
        ]

        for pattern in expected:
            assert any(pattern in p for p in PROTECTED_PATTERNS), f"Missing: {pattern}"


class TestWorkspaceGuardIsProtected:
    """Direct unit tests for is_protected() method."""

    def test_env_file_is_protected(self):
        guard = WorkspaceGuard()
        assert guard.is_protected(".env") is True

    def test_git_dir_is_protected(self):
        guard = WorkspaceGuard()
        assert guard.is_protected(".git/config") is True

    def test_requirements_txt_is_protected(self):
        guard = WorkspaceGuard()
        assert guard.is_protected("requirements.txt") is True

    def test_dockerfile_is_protected(self):
        guard = WorkspaceGuard()
        assert guard.is_protected("Dockerfile") is True

    def test_normal_python_file_not_protected(self):
        guard = WorkspaceGuard()
        assert guard.is_protected("src/main.py") is False

    def test_require_approval_mirrors_is_protected(self):
        guard = WorkspaceGuard()
        assert guard.require_approval(".env") is True
        assert guard.require_approval("src/utils.py") is False

    def test_get_protected_info_protected(self):
        guard = WorkspaceGuard()
        info = guard.get_protected_info(".env")
        assert info["protected"] is True
        assert info["requires_approval"] is True
        assert info["reason"] is not None

    def test_get_protected_info_unprotected(self):
        guard = WorkspaceGuard()
        info = guard.get_protected_info("src/app.py")
        assert info["protected"] is False
        assert info["reason"] is None

    def test_guard_operation_blocked_without_approval(self):
        guard = WorkspaceGuard()
        result = guard.guard_operation("write_file", ".env", user_approved=False)
        assert result["status"] == "error"
        assert result["requires_approval"] is True

    def test_guard_operation_passes_with_approval(self):
        guard = WorkspaceGuard()
        result = guard.guard_operation("write_file", ".env", user_approved=True)
        assert result["status"] == "ok"

    def test_guard_operation_passes_for_unprotected(self):
        guard = WorkspaceGuard()
        result = guard.guard_operation("write_file", "src/new.py")
        assert result["status"] == "ok"
