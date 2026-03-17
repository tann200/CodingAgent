"""
Workspace Safety Guard - Protects critical files from automatic modification.
"""

from pathlib import Path
from typing import Dict, Any, List, Optional


PROTECTED_PATTERNS = [
    ".git/",
    ".gitignore",
    ".gitattributes",
    ".env",
    ".env.local",
    ".env.example",
    "requirements.txt",
    "package-lock.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Cargo.lock",
    "go.sum",
    ".npmrc",
    ".pypirc",
    "poetry.lock",
    "Pipfile.lock",
    "yarn.lock",
    "bun.lockb",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".dockerignore",
    "Makefile",
    "CMakeLists.txt",
    "tsconfig.json",
    "jsconfig.json",
    ".eslintrc",
    ".prettierrc",
    "pytest.ini",
    "tox.ini",
    ".github/",
    ".gitlab-ci.yml",
    ".travis.yml",
]


class WorkspaceGuard:
    """Protects critical files from automatic modification."""

    def __init__(self, protected_patterns: Optional[List[str]] = None):
        self.protected_patterns = protected_patterns or PROTECTED_PATTERNS

    def is_protected(self, path: str) -> bool:
        """Check if path matches protected pattern."""
        path_obj = Path(path)
        path_str = str(path_obj)

        # Check exact matches and patterns
        for pattern in self.protected_patterns:
            if pattern.endswith("/"):
                # Directory pattern
                if path_str.startswith(pattern) or f"/{pattern}" in path_str:
                    return True
            else:
                # File pattern
                if path_str.endswith(pattern) or path_obj.name == pattern:
                    return True

        return False

    def require_approval(self, path: str) -> bool:
        """Return True if explicit user approval required for this path."""
        return self.is_protected(path)

    def guard_operation(
        self, operation: str, path: str, user_approved: bool = False
    ) -> Dict[str, Any]:
        """Validate operation against protected files.

        Args:
            operation: Type of operation (write_file, edit_file, delete_file, etc)
            path: File path being operated on
            user_approved: Whether user has explicitly approved this operation

        Returns:
            Dict with status and error message if blocked
        """
        if not self.is_protected(path):
            return {"status": "ok", "protected": False}

        if user_approved:
            return {
                "status": "ok",
                "protected": True,
                "user_approved": True,
                "message": f"Operation on protected file '{path}' approved by user",
            }

        return {
            "status": "error",
            "error": f"Protected file: '{path}'. Explicit user approval required for {operation}.",
            "requires_approval": True,
            "operation": operation,
            "protected": True,
        }

    def get_protected_info(self, path: str) -> Dict[str, Any]:
        """Get detailed info about protection status."""
        is_protected = self.is_protected(path)

        return {
            "path": path,
            "protected": is_protected,
            "requires_approval": is_protected,
            "reason": self._get_protection_reason(path) if is_protected else None,
        }

    def _get_protection_reason(self, path: str) -> str:
        """Explain why a path is protected."""
        path_obj = Path(path)
        name = path_obj.name

        if ".git" in str(path_obj):
            return "Git repository metadata (do not modify)"
        if name == ".env" or ".env." in name:
            return "Environment configuration (may contain secrets)"
        if name in ["requirements.txt", "package-lock.json", "pyproject.toml"]:
            return "Dependency lock file (use package manager)"
        if name in ["Dockerfile", "docker-compose.yml"]:
            return "Container configuration"

        return "Protected file pattern"


def create_workspace_guard() -> WorkspaceGuard:
    """Factory function to create WorkspaceGuard."""
    return WorkspaceGuard()


def guard_write_operation(
    tool_name: str, path: str, user_approved: bool = False
) -> Dict[str, Any]:
    """Convenience function to guard write operations."""
    guard = WorkspaceGuard()
    return guard.guard_operation(tool_name, path, user_approved)
